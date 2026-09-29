import importlib.util
import json
from pathlib import Path

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request


_SUPERVISOR_PATH = Path(__file__).resolve().parents[2] / "docker" / "supervisor.py"
_SPEC = importlib.util.spec_from_file_location("bazarr_docker_supervisor", _SUPERVISOR_PATH)
supervisor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(supervisor)


class _Backend:
    def __init__(self, state="running"):
        self.state = state

    def get_status(self):
        return {"state": self.state, "stage_index": 0}


async def _proxy_marker(request):
    return web.Response(text="proxied")


async def _static_marker(request):
    return None


@pytest.mark.asyncio
async def test_backup_download_path_is_proxied_to_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "proxy_handler", _proxy_marker)
    monkeypatch.setattr(supervisor, "create_static_handler", lambda config_dir, backend=None: _static_marker)

    app = supervisor.create_app(str(tmp_path), _Backend())
    request = make_mocked_request(
        "GET",
        "/system/backup/download/bazarr_backup_vlatest_2026.05.03_03.00.00.zip",
        app=app,
    )

    match_info = await app.router.resolve(request)

    assert match_info.handler is _proxy_marker


@pytest.mark.asyncio
async def test_supervisor_routes_honor_base_url(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """
general:
  base_url: /bazarr
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(supervisor, "proxy_handler", _proxy_marker)

    app = supervisor.create_app(str(tmp_path), _Backend(state="starting"))
    request = make_mocked_request("GET", "/bazarr/_supervisor/status", app=app)

    match_info = await app.router.resolve(request)
    response = await match_info.handler(request)

    assert response.status == 200
    assert json.loads(response.text) == {"state": "starting", "stage_index": 0}


@pytest.mark.asyncio
async def test_spa_routes_are_proxied_when_backend_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "proxy_handler", _proxy_marker)

    app = supervisor.create_app(str(tmp_path), _Backend(state="running"))
    request = make_mocked_request("GET", "/system/releases", app=app)

    match_info = await app.router.resolve(request)
    response = await match_info.handler(request)

    assert response.text == "proxied"


@pytest.mark.asyncio
async def test_spa_routes_do_not_boot_app_while_backend_is_starting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        supervisor,
        "_get_index_html",
        lambda config_dir: '<script>window.Bazarr = {"apiKey": ""}</script><script src="/assets/app.js"></script>',
    )

    handler = supervisor.create_static_handler(str(tmp_path), _Backend(state="starting"))
    request = make_mocked_request("GET", "/system/releases")

    response = await handler(request)

    assert response.status == 503
    assert "Bazarr+ is starting up" in response.text
    assert "window.Bazarr" not in response.text
    assert "/assets/app.js" not in response.text


@pytest.mark.asyncio
async def test_crashed_backend_serves_app_for_reconnection_flow(monkeypatch, tmp_path):
    app_shell = '<script>window.Bazarr = {"apiKey": ""}</script><script src="/assets/app.js"></script>'
    monkeypatch.setattr(supervisor, "_get_index_html", lambda config_dir: app_shell)

    handler = supervisor.create_static_handler(str(tmp_path), _Backend(state="crashed"))
    request = make_mocked_request("GET", "/system/releases")

    response = await handler(request)

    assert response.status == 200
    assert response.text == app_shell


def test_supervisor_config_does_not_inject_configured_api_key(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        """
general:
  base_url: /bazarr
  secrets_encryption_key: test-master-key
auth:
  apikey: live-supervisor-secret
""",
        encoding="utf-8",
    )

    defaults = supervisor._read_bazarr_config(str(tmp_path))

    assert defaults["baseUrl"] == "/bazarr"
    assert defaults["apiKey"] == ""


@pytest.mark.asyncio
async def test_the_proxy_follows_the_current_backend_port_and_hides_the_identity_header(monkeypatch):
    # The backend port is picked per launch, so the proxy has to read it at
    # request time. The identity header is between the supervisor and its own
    # backend; a browser has no use for it.
    async def status(request):
        return web.json_response(
            {"data": {"ok": True}},
            headers={"X-Bazarr-Supervisor-Token": "per-boot-secret", "X-Other": "kept"},
        )

    backend_app = web.Application()
    backend_app.router.add_get("/api/system/status", status)
    backend_runner = web.AppRunner(backend_app, access_log=None)
    await backend_runner.setup()
    backend_site = web.TCPSite(backend_runner, "127.0.0.1", 0)
    await backend_site.start()
    backend_port = backend_site._server.sockets[0].getsockname()[1]

    monkeypatch.setattr(supervisor, "BACKEND_PORT", backend_port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "per-boot-secret")

    front = web.Application()
    front.router.add_route("*", "/api/{path:.*}", supervisor.proxy_handler)
    server = TestServer(front)
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get("/api/system/status")
        body = await response.json()
    finally:
        await client.close()
        await backend_runner.cleanup()

    assert response.status == 200
    assert body == {"data": {"ok": True}}
    assert response.headers.get("X-Other") == "kept"
    assert "X-Bazarr-Supervisor-Token" not in response.headers


@pytest.mark.asyncio
async def test_the_proxy_relays_nothing_until_a_backend_port_is_verified(monkeypatch):
    # Before the readiness poll has seen this supervisor's token on a port,
    # whatever answers there may be another instance's backend. API calls and
    # websocket upgrades both get the startup answer instead.
    monkeypatch.setattr(supervisor, "BACKEND_PORT", None)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", None)

    front = web.Application()
    front.router.add_route("*", "/api/{path:.*}", supervisor.proxy_handler)
    client = TestClient(TestServer(front))
    await client.start_server()
    try:
        response = await client.get("/api/system/status")
        body = await response.json()
        with pytest.raises(WSServerHandshakeError) as handshake:
            await client.ws_connect("/api/socket.io/?EIO=4&transport=websocket")
    finally:
        await client.close()

    assert response.status == 503
    assert body == {"error": "Backend is starting up"}
    assert handshake.value.status == 503


# --- every relayed answer is checked, not only the first one ------------------
#
# The readiness poll opens the proxy once this supervisor's backend has
# answered with its token. When that backend dies without closing cleanly,
# bazarr.py only notices on its next check a few seconds later, and until then
# the port stays open in the proxy. On a shared network namespace another
# instance can bind the freed port in that window, so each answer has to carry
# the token too.

async def _start_backend(headers):
    async def status(request):
        return web.json_response({"data": {"bazarr_version": "neighbour"}}, headers=headers)

    async def socket(request):
        ws = web.WebSocketResponse()
        ws.headers.update(headers)
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str("echo:" + msg.data)
        return ws

    app = web.Application()
    app.router.add_get("/api/system/status", status)
    app.router.add_get("/api/socket.io/", socket)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _proxy_client():
    front = web.Application()
    front.router.add_route("*", "/api/{path:.*}", supervisor.proxy_handler)
    client = TestClient(TestServer(front))
    await client.start_server()
    return client


_FOREIGN_ANSWERS = [
    pytest.param({}, "mine", id="no-token"),
    pytest.param({"X-Bazarr-Supervisor-Token": "somebody-elses-token"}, "mine", id="another-token"),
    pytest.param({"X-Bazarr-Supervisor-Token": "mine"}, None, id="no-verified-token"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_headers, verified_token", _FOREIGN_ANSWERS)
async def test_the_proxy_refuses_an_answer_that_is_not_from_its_own_backend(
        monkeypatch, backend_headers, verified_token):
    runner, port = await _start_backend(backend_headers)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", verified_token)
    client = await _proxy_client()
    try:
        response = await client.get("/api/system/status")
        body = await response.json()
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 503
    assert body == {"error": "Backend is starting up"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_headers, verified_token", _FOREIGN_ANSWERS)
async def test_the_proxy_refuses_a_websocket_that_is_not_from_its_own_backend(
        monkeypatch, backend_headers, verified_token):
    runner, port = await _start_backend(backend_headers)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", verified_token)
    client = await _proxy_client()
    try:
        with pytest.raises(WSServerHandshakeError) as handshake:
            ws = await client.ws_connect("/api/socket.io/?EIO=4&transport=websocket")
            # Reached only when the proxy let the socket through.
            await ws.send_str("hello")
            await ws.receive(timeout=2)
            await ws.close()
    finally:
        await client.close()
        await runner.cleanup()

    assert handshake.value.status == 503


@pytest.mark.asyncio
async def test_the_proxy_relays_a_websocket_from_its_own_backend(monkeypatch):
    runner, port = await _start_backend({"X-Bazarr-Supervisor-Token": "mine"})
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    client = await _proxy_client()
    try:
        ws = await client.ws_connect("/api/socket.io/?EIO=4&transport=websocket")
        await ws.send_str("hello")
        msg = await ws.receive(timeout=5)
        await ws.close()
    finally:
        await client.close()
        await runner.cleanup()

    assert msg.type == WSMsgType.TEXT
    assert msg.data == "echo:hello"

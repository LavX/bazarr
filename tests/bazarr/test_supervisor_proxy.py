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


# --- request bodies are streamed, not buffered --------------------------------
#
# The proxy used to read every request body whole before relaying it, through
# aiohttp's default 1 MiB request limit. A larger upload raised inside the
# handler, which answered it as "Backend is starting up", and the page hides
# that answer. So in the image no upload over 1 MiB ever reached the backend.

async def _start_body_backend(received, delay=0):
    import asyncio

    async def upload(request):
        # Read from the stream: request.read() is what the proxy must not call.
        body = await request.content.read()
        received.append({"size": len(body), "content_length": request.headers.get("Content-Length"),
                         "chunked": request.headers.get("Transfer-Encoding"),
                         "encoding": request.headers.get("Content-Encoding")})
        received[-1]["body"] = body
        await asyncio.sleep(delay)
        return web.json_response({"size": len(body)}, headers={"X-Bazarr-Supervisor-Token": "mine"})

    async def status(request):
        await asyncio.sleep(delay)
        return web.json_response({}, headers={"X-Bazarr-Supervisor-Token": "mine"})

    app = web.Application()
    app.router.add_post("/api/upload", upload)
    app.router.add_get("/api/status", status)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _front_client(tmp_path):
    client = TestClient(TestServer(supervisor.create_app(str(tmp_path), _Backend())))
    await client.start_server()
    return client


def _never_buffered(monkeypatch):
    async def buffered(self):
        raise AssertionError("the proxy read the whole request body")

    monkeypatch.setattr(web.BaseRequest, "read", buffered)


async def _slow_body(total, seconds, parts=8):
    import asyncio

    for _ in range(parts):
        await asyncio.sleep(seconds / parts)
        yield b"x" * (total // parts)


@pytest.mark.asyncio
async def test_a_large_upload_is_streamed_to_the_backend_with_its_length(monkeypatch, tmp_path):
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    client = await _front_client(tmp_path)
    _never_buffered(monkeypatch)
    try:
        response = await client.post("/api/upload", data=b"x" * (3 * 1024 * 1024))
        body = await response.json()
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 200
    assert body == {"size": 3 * 1024 * 1024}
    assert received[0]["body"] == b"x" * (3 * 1024 * 1024)
    assert {key: received[0][key] for key in ("content_length", "chunked", "encoding")} == {
        "content_length": str(3 * 1024 * 1024), "chunked": None, "encoding": None}


@pytest.mark.asyncio
async def test_a_chunked_upload_reaches_the_backend_whole(monkeypatch, tmp_path):
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    client = await _front_client(tmp_path)
    _never_buffered(monkeypatch)
    try:
        response = await client.post("/api/upload", data=_slow_body(2 * 1024 * 1024, 0.1))
        body = await response.json()
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 200
    assert body == {"size": 2 * 1024 * 1024}
    assert received[0]["size"] == 2 * 1024 * 1024


@pytest.mark.asyncio
async def test_a_body_declared_over_the_ceiling_is_refused_without_the_backend(monkeypatch, tmp_path):
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "MAX_REQUEST_BODY_SIZE", 1024)
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=b"x" * 1025)
        body = await response.json()
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 413
    assert "too large" in body["error"]
    assert received == []


@pytest.mark.asyncio
async def test_a_chunked_body_over_the_ceiling_is_refused_before_the_backend_gets_it_all(monkeypatch, tmp_path):
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "MAX_REQUEST_BODY_SIZE", 1024 * 1024)
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=_slow_body(2 * 1024 * 1024, 0.1))
        body = await response.json()
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 413
    assert "too large" in body["error"]
    assert received == []


def test_the_body_ceiling_is_256_mib():
    assert supervisor.MAX_REQUEST_BODY_SIZE == 256 * 1024 * 1024


@pytest.mark.asyncio
async def test_a_slow_upload_is_timed_from_its_last_byte(monkeypatch, tmp_path):
    # A large file over a slow link can take longer than the answer's deadline
    # to arrive, and the backend has nothing to answer until it has.
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "ANSWER_TIMEOUT", 0.5)
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=_slow_body(64 * 1024, 1.2))
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 200
    assert received[0]["size"] == 64 * 1024


@pytest.mark.asyncio
async def test_the_answer_to_an_upload_still_has_a_deadline(monkeypatch, tmp_path):
    # Reading the body whole used to happen before the deadline started, so the
    # answer had its full 300 seconds after the last byte. Streaming the body
    # must not take that bound away.
    import time

    received = []
    runner, port = await _start_body_backend(received, delay=2)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "ANSWER_TIMEOUT", 0.5)
    client = await _front_client(tmp_path)
    try:
        started = time.monotonic()
        response = await client.post("/api/upload", data=b"x" * 1024)
        elapsed = time.monotonic() - started
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 503
    assert elapsed < 1.5


@pytest.mark.asyncio
async def test_an_answer_without_a_body_keeps_its_deadline(monkeypatch, tmp_path):
    import time

    received = []
    runner, port = await _start_body_backend(received, delay=2)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "ANSWER_TIMEOUT", 0.5)
    client = await _front_client(tmp_path)
    try:
        started = time.monotonic()
        response = await client.get("/api/status")
        elapsed = time.monotonic() - started
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 503
    assert elapsed < 1.5


def test_the_answer_deadline_is_300_seconds():
    assert supervisor.ANSWER_TIMEOUT == 300


# --- compressed request bodies -----------------------------------------------
#
# aiohttp decodes a gzip, deflate, br or zstd body as it reads it, so what the
# proxy relays is the decoded body. Its declared length is the compressed one.

@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_a_compressed_body_reaches_the_backend_whole_and_decoded(monkeypatch, tmp_path, encoding):
    import gzip
    import zlib

    raw = json.dumps({"series": list(range(1000))}).encode()
    packed = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    assert len(packed) < len(raw)
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=packed,
                                     headers={"Content-Encoding": encoding, "Content-Type": "application/json"})
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 200
    [seen] = received
    assert seen["body"] == raw
    # The relayed body is no longer encoded, so it must not say it is.
    assert seen["encoding"] is None
    assert seen["content_length"] != str(len(packed))


@pytest.mark.asyncio
async def test_a_body_in_an_encoding_aiohttp_leaves_alone_is_relayed_as_sent(monkeypatch, tmp_path):
    received = []
    runner, port = await _start_body_backend(received)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=b"x" * 2048, headers={"Content-Encoding": "identity"})
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 200
    [seen] = received
    assert (seen["body"], seen["content_length"], seen["encoding"]) == (b"x" * 2048, "2048", "identity")


# --- the ceiling against waitress itself --------------------------------------
#
# Waitress refuses a declared length at its ceiling, not only above it, and it
# counts a chunked body with its chunk framing. Its refusal carries no token,
# so the proxy would answer it as "Backend is starting up", which the page
# hides. The proxy has to refuse first, and never refuse what waitress takes.

_WAITRESS_CEILING = 1024 * 1024


@pytest.fixture
def waitress_backend(monkeypatch):
    import threading

    from waitress.server import create_server

    received = []

    def app(environ, start_response):
        body = environ["wsgi.input"].read()
        received.append(len(body))
        start_response("200 OK", [("Content-Type", "application/json"), ("X-Bazarr-Supervisor-Token", "mine")])
        return [json.dumps({"size": len(body)}).encode()]

    server = create_server(app, host="127.0.0.1", port=0, threads=2,
                           max_request_body_size=_WAITRESS_CEILING)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    monkeypatch.setattr(supervisor, "BACKEND_PORT", server.effective_port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "MAX_REQUEST_BODY_SIZE", _WAITRESS_CEILING)
    try:
        yield received
    finally:
        server.close()
        server.task_dispatcher.shutdown()


async def _pieces(total, size):
    sent = 0
    while sent < total:
        piece = min(size, total - sent)
        sent += piece
        yield b"x" * piece


@pytest.mark.asyncio
async def test_a_body_declared_at_the_ceiling_is_refused_by_the_proxy(waitress_backend, tmp_path):
    client = await _front_client(tmp_path)
    try:
        at = await client.post("/api/upload", data=b"x" * _WAITRESS_CEILING)
        under = await client.post("/api/upload", data=b"x" * (_WAITRESS_CEILING - 1))
        under_body = await under.json()
    finally:
        await client.close()

    assert at.status == 413
    assert under.status == 200 and under_body == {"size": _WAITRESS_CEILING - 1}
    assert waitress_backend == [_WAITRESS_CEILING - 1]


@pytest.mark.asyncio
async def test_a_chunked_body_near_the_ceiling_gets_an_answer_the_page_shows(waitress_backend, tmp_path):
    client = await _front_client(tmp_path)
    try:
        near = await client.post("/api/upload", data=_pieces(_WAITRESS_CEILING - 1024, 4096))
    finally:
        await client.close()

    assert near.status == 413
    assert waitress_backend == []


@pytest.mark.asyncio
async def test_a_chunked_body_within_the_streamed_limit_reaches_waitress_whole(waitress_backend, tmp_path):
    size = _WAITRESS_CEILING - supervisor.STREAM_FRAMING_ALLOWANCE
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=_pieces(size, 4096))
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200 and body == {"size": size}
    assert waitress_backend == [size]


class _Trickle:
    """A request stream that arrives a few bytes at a time."""

    def __init__(self, pieces):
        self._pieces = pieces

    async def _read(self):
        for piece in self._pieces:
            yield piece

    def iter_any(self):
        return self._read()


@pytest.mark.asyncio
async def test_a_trickled_body_is_relayed_in_chunks_whose_framing_fits_the_allowance(monkeypatch):
    # Every relayed piece becomes one chunk frame for waitress to count. Relayed
    # as it arrived, a body sent 64 bytes at a time would carry more framing
    # than data allowance and be refused by waitress instead.
    monkeypatch.setattr(supervisor, "MAX_REQUEST_BODY_SIZE", _WAITRESS_CEILING)
    limit = _WAITRESS_CEILING - supervisor.STREAM_FRAMING_ALLOWANCE
    finished = []

    relayed = [chunk async for chunk in supervisor._bounded_body(
        _Trickle([b"x" * 64] * (limit // 64)), limit, lambda: finished.append(True))]

    assert b"".join(relayed) == b"x" * limit
    framed = sum(len(f"{len(chunk):x}") + 4 + len(chunk) for chunk in relayed) + len(b"0\r\n\r\n")
    assert framed < _WAITRESS_CEILING
    assert finished == [True]


@pytest.mark.asyncio
async def test_a_trickled_body_over_its_limit_stops_before_the_excess_is_relayed():
    piece = supervisor.RELAY_CHUNK_SIZE
    limit = 3 * piece + piece // 2
    relayed = []
    finished = []
    with pytest.raises(supervisor._BodyTooLarge):
        async for chunk in supervisor._bounded_body(_Trickle([b"x" * piece] * 10), limit,
                                                    lambda: finished.append(True)):
            relayed.append(chunk)  # noqa: PERF401

    assert sum(len(chunk) for chunk in relayed) == 3 * piece
    assert finished == []


# --- the proxy's own refusal and the answer's deadline ------------------------

async def _serve_upload(handler):
    app = web.Application()
    app.router.add_post("/api/upload", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_a_body_declared_at_the_ceiling_is_never_relayed(monkeypatch, tmp_path):
    # Waitress refuses that length itself, with no token. Relayed, its refusal
    # races the proxy's, so the request must not reach the backend at all.
    arrivals = []

    async def upload(request):
        arrivals.append(request.headers.get("Content-Length"))
        await request.content.read()
        return web.json_response({}, headers={"X-Bazarr-Supervisor-Token": "mine"})

    ceiling = 4 * supervisor.RELAY_CHUNK_SIZE
    runner, port = await _serve_upload(upload)
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "MAX_REQUEST_BODY_SIZE", ceiling)
    client = await _front_client(tmp_path)
    try:
        # Sent in two pieces, so a relay would have begun before the last byte.
        response = await client.post("/api/upload", headers={"Content-Length": str(ceiling)},
                                     data=_first_piece_then_trickle(supervisor.RELAY_CHUNK_SIZE,
                                                                    ceiling - supervisor.RELAY_CHUNK_SIZE,
                                                                    0.2, parts=1))
        body = await response.json()
    finally:
        await client.close()
        await runner.cleanup()

    assert response.status == 413
    assert "too large" in body["error"]
    assert arrivals == []



def _answer_then_stall(release):
    """A backend that answers at once, sends part of its answer, then stalls
    until ``release`` is set, whatever it has been sent of the body."""
    import asyncio
    import contextlib

    async def upload(request):
        response = web.StreamResponse(headers={"X-Bazarr-Supervisor-Token": "mine"})
        await response.prepare(request)
        await response.write(b"partial")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(release.wait(), 10)
        return response

    return upload


async def _first_piece_then_trickle(first, rest, seconds, parts=8):
    import asyncio

    yield b"x" * first
    for _ in range(parts):
        await asyncio.sleep(seconds / parts)
        yield b"x" * (rest // parts)


@pytest.mark.asyncio
async def test_an_answer_before_the_last_byte_starts_the_deadline(monkeypatch, tmp_path):
    # Waitress answers a body it refuses before the rest of it arrives. The
    # deadline has to run from that answer: the last byte may be minutes away,
    # and a stalled answer would otherwise hold the request until it came.
    import asyncio
    import contextlib
    import time

    from aiohttp import ClientError

    release = asyncio.Event()
    runner, port = await _serve_upload(_answer_then_stall(release))
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "ANSWER_TIMEOUT", 0.5)
    client = await _front_client(tmp_path)
    try:
        started = time.monotonic()
        response = await client.post("/api/upload", data=_first_piece_then_trickle(
            supervisor.RELAY_CHUNK_SIZE, 64 * 1024, 4))
        with contextlib.suppress(ClientError, asyncio.TimeoutError):
            await asyncio.wait_for(response.read(), 6)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        await client.close()
        await runner.cleanup()

    assert response.status == 200
    assert elapsed < 2.5


@pytest.mark.asyncio
async def test_an_answer_cut_short_reaches_the_client_cut_short(monkeypatch, tmp_path):
    # Part of the answer is already relayed when its deadline passes, so no
    # other answer can follow it on that connection. It used to be followed by
    # "Backend is starting up" inside the first answer's body, and the
    # connection stayed open, so the client waited on it indefinitely.
    import asyncio

    from aiohttp import ClientPayloadError

    release = asyncio.Event()
    runner, port = await _serve_upload(_answer_then_stall(release))
    monkeypatch.setattr(supervisor, "BACKEND_PORT", port)
    monkeypatch.setattr(supervisor, "BACKEND_TOKEN", "mine")
    monkeypatch.setattr(supervisor, "ANSWER_TIMEOUT", 0.5)
    client = await _front_client(tmp_path)
    try:
        response = await client.post("/api/upload", data=b"x" * 1024)
        with pytest.raises(ClientPayloadError):
            await asyncio.wait_for(response.read(), 5)
    finally:
        release.set()
        await client.close()
        await runner.cleanup()

    assert response.status == 200

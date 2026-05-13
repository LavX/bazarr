import importlib.util
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


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

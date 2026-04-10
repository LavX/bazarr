#!/usr/bin/env python3
"""
Bazarr+ Process Supervisor

Single entrypoint that runs two independent services:
  1. Frontend server (Python aiohttp): serves static files instantly, proxies
     API/websocket requests to the backend. Starts in <1 second.
  2. Backend (bazarr.py subprocess): the full application. Takes 10-30 seconds.

Frontend shows the startup screen while backend boots. If the backend crashes,
the frontend keeps running and shows a reconnection banner. Each service
auto-restarts independently.

Usage:
    python supervisor.py [--config /config] [--no-update] [--port 6767]
"""

import asyncio
import json
import mimetypes
import os
import signal
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

# Add bundled libs to path so we can import yaml (PyYAML)
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR / "libs"))
import yaml  # noqa: E402

# Unbuffered print so logs appear immediately when redirected to a file
_print = print


def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _print(*args, **kwargs)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATIC_DIR = APP_DIR / "frontend" / "build"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 6768  # internal port for bazarr backend
DEFAULT_PORT = 6767  # external port users connect to

# Paths that get proxied to the backend
PROXY_PREFIXES = ("/api/", "/images/", "/test/", "/bazarr.log")


# ---------------------------------------------------------------------------
# Backend process manager
# ---------------------------------------------------------------------------
class BackendManager:
    # Possible states exposed to the frontend
    STATE_STARTING = "starting"
    STATE_RUNNING = "running"
    STATE_CRASHED = "crashed"
    STATE_STOPPING = "stopping"

    # Ordered startup stages (must match actual stdout from bazarr subprocess)
    STAGES = [
        "Launching process",
        "Checking for updates",
        "Starting scheduler",
        "Starting jobs queue",
        "Starting HTTP server",
        "Connecting to Sonarr/Radarr",
        "Ready",
    ]

    # Map log fragments to stage index (only markers that ACTUALLY appear in stdout)
    _STAGE_MARKERS = [
        ("starting child process", 0),                  # Launching process
        ("check_update", 1),                            # Checking for updates
        ("Scheduler will use this timezone", 2),        # Starting scheduler
        ("jobs queue started", 3),                      # Starting jobs queue
        ("waiting for requests on", 4),                 # Starting HTTP server
        ("SignalR client for", 5),                      # Connecting to Sonarr/Radarr
    ]

    def __init__(self, bazarr_args: list[str]):
        self.bazarr_args = bazarr_args
        self.process = None
        self._should_run = True
        self.state = self.STATE_STARTING
        self._stage_index = 0  # index into STAGES
        self._last_exit_code = None

    async def run(self):
        """Start bazarr as a subprocess, auto-restart on crash."""
        bazarr_py = str(APP_DIR / "bazarr.py")
        env = os.environ.copy()

        while self._should_run:
            self.state = self.STATE_STARTING
            self._stage_index = 0
            cmd = [sys.executable, bazarr_py, "--port", str(BACKEND_PORT)] + self.bazarr_args
            print(f"[supervisor] Starting backend: {' '.join(cmd)}")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *cmd, env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                # Read stdout in background to detect stages and forward to our stdout
                reader_task = asyncio.create_task(self._read_stdout())
                # Wait for backend to actually accept connections
                asyncio.create_task(self._wait_for_ready())
                code = await self.process.wait()
                reader_task.cancel()
                self._last_exit_code = code
                if self._should_run:
                    self.state = self.STATE_CRASHED
                    print(f"[supervisor] Backend exited ({code}), restarting in 3s...")
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_exit_code = -1
                if self._should_run:
                    self.state = self.STATE_CRASHED
                    print(f"[supervisor] Backend error: {e}, restarting in 3s...")
                    await asyncio.sleep(3)

    async def _read_stdout(self):
        """Read backend stdout line by line, detect stage transitions, forward output."""
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                # Forward to supervisor's stdout
                print(text, flush=True)
                # Check for stage markers (only advance forward)
                if self.state == self.STATE_STARTING:
                    for marker, idx in self._STAGE_MARKERS:
                        if marker in text and idx > self._stage_index:
                            self._stage_index = idx
                            break
        except asyncio.CancelledError:
            pass

    async def _wait_for_ready(self):
        """Poll the backend until it responds, then set state to RUNNING."""
        url = f"http://{BACKEND_HOST}:{BACKEND_PORT}/api/system/status"
        timeout = ClientTimeout(total=2, connect=1)
        for _ in range(120):  # up to ~2 minutes
            if self.state != self.STATE_STARTING:
                return
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status < 500:
                            self.state = self.STATE_RUNNING
                            self._stage_index = len(self.STAGES) - 1
                            print("[supervisor] Backend is ready")
                            return
            except Exception:
                pass
            await asyncio.sleep(1)

    def get_status(self) -> dict:
        """Return status dict for the /_supervisor/status endpoint."""
        return {
            "state": self.state,
            "stage": self.STAGES[self._stage_index] if self._stage_index < len(self.STAGES) else "Ready",
            "stage_index": self._stage_index,
            "stage_total": len(self.STAGES),
            "stages": self.STAGES,
            "pid": self.process.pid if self.process and self.process.returncode is None else None,
            "last_exit_code": self._last_exit_code,
        }

    async def stop(self):
        self._should_run = False
        self.state = self.STATE_STOPPING
        if self.process and self.process.returncode is None:
            print("[supervisor] Stopping backend...")
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                print("[supervisor] Backend didn't stop, killing...")
                self.process.kill()


# ---------------------------------------------------------------------------
# Frontend server with API proxy
# ---------------------------------------------------------------------------
async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """Proxy API/image requests to the backend."""
    target_url = f"http://{BACKEND_HOST}:{BACKEND_PORT}{request.path_qs}"

    # WebSocket upgrade
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_websocket(request, target_url)

    try:
        timeout = ClientTimeout(total=300, connect=5)
        async with ClientSession(timeout=timeout) as session:
            async with session.request(
                method=request.method,
                url=target_url,
                headers={k: v for k, v in request.headers.items()
                         if k.lower() not in ("host", "content-length")},
                data=await request.read(),
                allow_redirects=False,
            ) as resp:
                response = web.StreamResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")},
                )
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
    except Exception:
        return web.json_response(
            {"error": "Backend is starting up"},
            status=503,
        )


async def _proxy_websocket(request: web.Request, target_url: str) -> web.WebSocketResponse:
    """Proxy WebSocket connections to the backend."""
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    ws_url = target_url.replace("http://", "ws://")
    try:
        async with ClientSession() as session:
            async with session.ws_connect(ws_url) as ws_server:

                async def forward(src, dst):
                    async for msg in src:
                        if msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break

                await asyncio.gather(
                    forward(ws_client, ws_server),
                    forward(ws_server, ws_client),
                )
    except Exception:
        pass

    return ws_client


def _read_bazarr_config(config_dir: str) -> dict:
    """Read the bazarr config.yaml to extract apiKey and baseUrl."""
    config_path = Path(config_dir) / "config" / "config.yaml"
    defaults = {"baseUrl": "/", "apiKey": "", "canUpdate": False, "hasUpdate": False}
    try:
        if config_path.is_file():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            auth = cfg.get("auth", {})
            general = cfg.get("general", {})
            if auth.get("apikey"):
                defaults["apiKey"] = auth["apikey"]
            if general.get("base_url"):
                defaults["baseUrl"] = general["base_url"]
    except Exception as e:
        print(f"[supervisor] Warning: could not read config: {e}")
    return defaults


def _get_index_html(config_dir: str) -> str:
    """Read index.html and replace Jinja template with config values."""
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return "<html><body>Frontend not built</body></html>"
    content = index.read_text()
    config = _read_bazarr_config(config_dir)
    inject = json.dumps(config)
    base_url = config.get("baseUrl", "/")
    if not base_url.endswith("/"):
        base_url += "/"
    # Replace Jinja templates with actual config values
    content = content.replace("{{baseUrl}}", base_url)
    content = content.replace(
        '`{{BAZARR_SERVER_INJECT | tojson | safe}}`',
        f"'{inject}'",
    )
    content = content.replace(
        "{{BAZARR_SERVER_INJECT | tojson | safe}}",
        inject,
    )
    return content


STATIC_FILE_EXTENSIONS = {
    ".js", ".css", ".map", ".json", ".webmanifest",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".wav", ".ogg",
}


def create_static_handler(config_dir: str):
    async def static_handler(request: web.Request) -> web.StreamResponse:
        """Serve static frontend files, fallback to index.html for SPA routing."""
        path = request.path.lstrip("/")
        file_path = STATIC_DIR / path

        if file_path.is_file() and path != "index.html":
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            return web.FileResponse(file_path, headers={"Content-Type": content_type})

        # If the request looks like a static file (has a known extension), return 404
        # instead of the SPA fallback. This prevents serving index.html as JavaScript
        # when the browser requests old/stale asset filenames.
        ext = os.path.splitext(path)[1].lower()
        if ext in STATIC_FILE_EXTENSIONS:
            return web.Response(status=404, text="Not found")

        # Serve patched index.html for SPA routing
        return web.Response(
            text=_get_index_html(config_dir),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )
    return static_handler


def create_supervisor_status_handler(backend: BackendManager):
    async def supervisor_status_handler(request: web.Request) -> web.Response:
        """Return backend process state without proxying."""
        return web.json_response(backend.get_status())
    return supervisor_status_handler


def create_supervisor_sse_handler(backend: BackendManager):
    async def supervisor_sse_handler(request: web.Request) -> web.StreamResponse:
        """Stream backend status changes via Server-Sent Events."""
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        last_sent = None
        try:
            while True:
                status = backend.get_status()
                # Only send when something changed
                key = (status["state"], status["stage_index"])
                if key != last_sent:
                    data = json.dumps(status)
                    await response.write(f"data: {data}\n\n".encode())
                    last_sent = key
                    # Stop streaming once backend is ready
                    if status["state"] == "running":
                        break
                await asyncio.sleep(0.3)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response
    return supervisor_sse_handler


def create_app(config_dir: str, backend: BackendManager) -> web.Application:
    app = web.Application()

    # Supervisor-handled endpoints (not proxied)
    app.router.add_route("GET", "/_supervisor/status", create_supervisor_status_handler(backend))
    app.router.add_route("GET", "/_supervisor/events", create_supervisor_sse_handler(backend))

    # API/image proxy routes (must be registered before the catch-all)
    for prefix in PROXY_PREFIXES:
        app.router.add_route("*", prefix + "{path:.*}", proxy_handler)

    # Static file catch-all
    app.router.add_route("GET", "/{path:.*}", create_static_handler(config_dir))

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    # Parse our args, pass the rest to bazarr
    port = DEFAULT_PORT
    config_dir = "/config"
    bazarr_args = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--config" and i + 1 < len(args):
            config_dir = args[i + 1]
            bazarr_args.extend([args[i], args[i + 1]])
            i += 2
        else:
            bazarr_args.append(args[i])
            i += 1

    # Start backend manager
    backend = BackendManager(bazarr_args)
    backend_task = asyncio.create_task(backend.run())

    # Start frontend server
    app = create_app(config_dir, backend)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[supervisor] Frontend serving on http://0.0.0.0:{port}")
    print(f"[supervisor] Backend will start on internal port {BACKEND_PORT}")

    # Wait for shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    print("[supervisor] Shutting down...")
    backend_task.cancel()
    await backend.stop()
    await runner.cleanup()
    print("[supervisor] Done")


if __name__ == "__main__":
    asyncio.run(main())

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
import base64
import html
import json
import mimetypes
import os
import signal
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

APP_DIR = Path(__file__).resolve().parent.parent
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
PROXY_PREFIXES = ("/api/", "/images/", "/test/", "/system/backup/download/", "/bazarr.log")


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
            # Strip hop-by-hop / framing headers before forwarding. The body
            # has already been fully read via request.read() (aiohttp de-chunks
            # at parse time), so forwarding Transfer-Encoding: chunked would
            # either cause aiohttp to re-chunk a body that's no longer
            # chunked, or tell waitress to expect more chunk frames that
            # never come - waitress then hangs waiting for the trailer.
            # This manifested as silent 100s+ hangs on any POST from clients
            # like .NET's HttpClient that default to chunked request bodies.
            _drop = {"host", "content-length", "transfer-encoding",
                     "connection", "keep-alive", "expect"}
            forwarded_headers = {k: v for k, v in request.headers.items()
                                 if k.lower() not in _drop}
            # Advertise the CLIENT-facing URL to the backend. Without
            # these, Flask's request.host is the internal 127.0.0.1:6768
            # and any absolute URL it builds (download links, base_url,
            # etc.) is unreachable from the outside. If an outer reverse
            # proxy already set these, preserve them; otherwise fill
            # them in from what THIS supervisor sees.
            # X-Forwarded-Host: always overwrite from request.host (the Host
            # header from the immediate upstream). Preserving client-supplied
            # values would let an attacker inject an arbitrary host into
            # compat download links, leaking the Api-Key off-box.
            #
            # X-Forwarded-Proto: preserve if already set by an outer TLS
            # terminator (nginx/traefik sets this to "https" while
            # forwarding to us over plain http). Only fill in our own
            # scheme when absent, since request.scheme here is always
            # "http" behind a TLS proxy and overwriting would downgrade
            # all download links to http://.
            forwarded_headers["X-Forwarded-Host"] = request.host
            fwd_proto_lower = {k.lower() for k in forwarded_headers}
            if "x-forwarded-proto" not in fwd_proto_lower:
                forwarded_headers["X-Forwarded-Proto"] = request.scheme
            if request.remote:
                forwarded_headers["X-Forwarded-For"] = request.remote
            async with session.request(
                method=request.method,
                url=target_url,
                headers=forwarded_headers,
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
    """Read the bazarr config.yaml to extract frontend defaults. The apiKey
    has to match the running bazarr's value: this shell is served during
    crash/restart, and once bazarr recovers the SPA keeps using whatever
    window.Bazarr.apiKey it was bootstrapped with as its X-API-KEY. An empty
    value strands every /api/* call at 401 with no recovery path under
    auth.type=None until the user manually reloads. The on-disk value is
    encrypted with the secrets_encryption_key (enc:v1: marker), so we have
    to decrypt before handing it to the SPA."""
    config_path = Path(config_dir) / "config" / "config.yaml"
    defaults = {"baseUrl": "/", "apiKey": "", "canUpdate": False, "hasUpdate": False}
    try:
        if config_path.is_file():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            general = cfg.get("general", {})
            if general.get("base_url"):
                defaults["baseUrl"] = general["base_url"]
            auth = cfg.get("auth", {})
            apikey = auth.get("apikey") or ""
            if apikey:
                master_key = general.get("secrets_encryption_key") or ""
                defaults["apiKey"] = _decrypt_apikey(apikey, master_key)
    except Exception as e:
        print(f"[supervisor] Warning: could not read config: {e}")
    return defaults


def _decrypt_apikey(value: str, master_key: str) -> str:
    """Decrypt an enc:v1:-prefixed apikey using the bazarr master key, or
    return the value unchanged when it isn't encrypted. The supervisor runs
    in the same container as bazarr and shares its cryptography install, so
    we lean on bazarr.secret_store.crypto rather than duplicating Fernet
    logic. On any failure we return the empty string so the SPA bootstrap
    treats the apiKey as missing rather than serving ciphertext."""
    marker = "enc:v1:"
    if not value.startswith(marker):
        return value
    if not master_key:
        print("[supervisor] Warning: encrypted apikey but no secrets_encryption_key in config")
        return ""
    try:
        sys.path.insert(0, str(APP_DIR / "bazarr"))
        try:
            from secret_store.crypto import decrypt_secret
        finally:
            try:
                sys.path.remove(str(APP_DIR / "bazarr"))
            except ValueError:
                pass
        return decrypt_secret(value, master_key=master_key) or ""
    except Exception as e:
        print(f"[supervisor] Warning: could not decrypt apikey: {e}")
        return ""


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


_LOGO_DATA_URI_CACHE: "str | None" = None


def _get_logo_data_uri() -> str:
    """Return the Bazarr+ logo as a base64 data URI, cached after first read.

    Embedded inline so the startup screen renders without depending on any
    static-asset route or revealing the SPA bundle while the backend boots.
    """
    global _LOGO_DATA_URI_CACHE
    if _LOGO_DATA_URI_CACHE is not None:
        return _LOGO_DATA_URI_CACHE
    logo_path = STATIC_DIR / "images" / "logo_no_orb128.png"
    try:
        if logo_path.is_file():
            encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
            _LOGO_DATA_URI_CACHE = f"data:image/png;base64,{encoded}"
        else:
            _LOGO_DATA_URI_CACHE = ""
    except OSError:
        _LOGO_DATA_URI_CACHE = ""
    return _LOGO_DATA_URI_CACHE


def _get_startup_html(status: dict, base_url: str = "/") -> str:
    """Return the startup page: logo, staged checklist, live SSE updates.

    The SPA bundle is intentionally not served while the backend is starting
    (avoids exposing window.Bazarr / app.js before auth is wired). Instead
    this page renders the same visual language as the SPA loading screen,
    subscribes to /_supervisor/events for stage progress, and reloads to
    boot the SPA once the backend reports running.
    """
    base = base_url.rstrip("/")
    sse_url = f"{base}/_supervisor/events"
    initial_status = {
        "state": status.get("state", "starting"),
        "stage": status.get("stage", ""),
        "stage_index": status.get("stage_index", -1),
        "stages": status.get("stages") or list(BackendManager.STAGES),
    }
    logo = _get_logo_data_uri()
    initial_json = json.dumps(initial_status)
    sse_json = json.dumps(sse_url)
    logo_attr = f' src="{html.escape(logo, quote=True)}"' if logo else ""

    return (
        '<!doctype html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Bazarr+ is starting up</title>\n'
        '<style>\n'
        ':root {\n'
        '  --bg: #121125;\n'
        '  --text-primary: #f5f5f7;\n'
        '  --text-tertiary: #8b8b9a;\n'
        '  --text-disabled: #4a4a59;\n'
        '  --accent: #f59f00;\n'
        '  --done: #2f9e44;\n'
        '  --crashed: #e03131;\n'
        '}\n'
        '* { box-sizing: border-box; }\n'
        'html, body { height: 100%; }\n'
        'body {\n'
        '  margin: 0;\n'
        '  display: grid;\n'
        '  place-items: center;\n'
        '  background: var(--bg);\n'
        '  color: var(--text-primary);\n'
        '  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;\n'
        '}\n'
        'main {\n'
        '  display: flex;\n'
        '  flex-direction: column;\n'
        '  align-items: center;\n'
        '  gap: 1.5rem;\n'
        '}\n'
        '.logo { width: 64px; height: 64px; opacity: 0.8; }\n'
        '.title { font-size: 1.125rem; font-weight: 600; margin: 0; }\n'
        '.stages { display: flex; flex-direction: column; gap: 4px; min-width: 220px; }\n'
        '.stage { display: flex; align-items: center; gap: 8px; font-size: 0.75rem; line-height: 1.4; }\n'
        '.stage svg { width: 14px; height: 14px; flex-shrink: 0; }\n'
        '.stage[data-state="done"] svg { color: var(--done); }\n'
        '.stage[data-state="done"] .label { color: var(--text-tertiary); }\n'
        '.stage[data-state="active"] svg { color: var(--accent); }\n'
        '.stage[data-state="active"] .label { color: var(--text-primary); font-weight: 500; }\n'
        '.stage[data-state="pending"] svg { color: var(--text-disabled); }\n'
        '.stage[data-state="pending"] .label { color: var(--text-disabled); }\n'
        '.spin { transform-origin: 50% 50%; animation: bz-spin 1s linear infinite; }\n'
        '@keyframes bz-spin { to { transform: rotate(360deg); } }\n'
        '.loader { width: 22px; height: 22px; border: 2px solid rgba(245, 159, 0, 0.18); border-top-color: var(--accent); border-radius: 50%; animation: bz-spin 0.75s linear infinite; }\n'
        '.crashed-msg { color: var(--crashed); font-size: 0.875rem; text-align: center; max-width: 320px; }\n'
        '.crashed .loader { border-color: rgba(224, 49, 49, 0.18); border-top-color: var(--crashed); }\n'
        '</style>\n'
        '</head>\n'
        '<body>\n'
        '<main>\n'
        f'<img class="logo" alt="Bazarr+"{logo_attr}>\n'
        '<h1 class="title">Bazarr+ is starting up</h1>\n'
        '<div class="stages" id="bz-stages"></div>\n'
        '<div class="loader" id="bz-loader"></div>\n'
        '</main>\n'
        '<template id="bz-icon-check"><svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M13.485 4.515a1 1 0 0 1 0 1.414l-6 6a1 1 0 0 1-1.414 0l-3-3a1 1 0 0 1 1.414-1.414L6.778 9.808l5.293-5.293a1 1 0 0 1 1.414 0z"/></svg></template>\n'
        '<template id="bz-icon-spinner"><svg viewBox="0 0 16 16" class="spin" aria-hidden="true"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-dasharray="20" stroke-dashoffset="10"/></svg></template>\n'
        '<template id="bz-icon-circle"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" fill="currentColor"/></svg></template>\n'
        '<script>\n'
        '(function () {\n'
        f'  var INITIAL = {initial_json};\n'
        f'  var SSE_URL = {sse_json};\n'
        '  var iconTpl = {\n'
        '    done: document.getElementById("bz-icon-check"),\n'
        '    active: document.getElementById("bz-icon-spinner"),\n'
        '    pending: document.getElementById("bz-icon-circle")\n'
        '  };\n'
        '  function deriveStages(status) {\n'
        '    var raw = (status.stages || []).slice();\n'
        '    if (raw.length === 0) return [];\n'
        '    return raw.slice(0, -1).concat(["Loading configuration"]);\n'
        '  }\n'
        '  function clearChildren(node) {\n'
        '    while (node.firstChild) node.removeChild(node.firstChild);\n'
        '  }\n'
        '  function render(status) {\n'
        '    var body = document.body;\n'
        '    var stagesEl = document.getElementById("bz-stages");\n'
        '    clearChildren(stagesEl);\n'
        '    if (status.state === "crashed") {\n'
        '      body.classList.add("crashed");\n'
        '      var msg = document.createElement("div");\n'
        '      msg.className = "crashed-msg";\n'
        '      msg.textContent = "Backend process crashed. Restarting...";\n'
        '      stagesEl.appendChild(msg);\n'
        '      return;\n'
        '    }\n'
        '    body.classList.remove("crashed");\n'
        '    var stages = deriveStages(status);\n'
        '    var idx = status.state === "running" ? stages.length - 1 : (status.stage_index == null ? -1 : status.stage_index);\n'
        '    for (var i = 0; i < stages.length; i++) {\n'
        '      var s = i < idx ? "done" : i === idx ? "active" : "pending";\n'
        '      var row = document.createElement("div");\n'
        '      row.className = "stage";\n'
        '      row.setAttribute("data-state", s);\n'
        '      var tpl = iconTpl[s];\n'
        '      if (tpl && tpl.content) row.appendChild(tpl.content.cloneNode(true));\n'
        '      var label = document.createElement("span");\n'
        '      label.className = "label";\n'
        '      label.textContent = stages[i];\n'
        '      row.appendChild(label);\n'
        '      stagesEl.appendChild(row);\n'
        '    }\n'
        '  }\n'
        '  render(INITIAL);\n'
        '  var fallbackTimer = null;\n'
        '  function scheduleReloadFallback() {\n'
        '    if (fallbackTimer) return;\n'
        '    fallbackTimer = setTimeout(function () { window.location.reload(); }, 4000);\n'
        '  }\n'
        '  try {\n'
        '    var es = new EventSource(SSE_URL);\n'
        '    es.onmessage = function (ev) {\n'
        '      try {\n'
        '        var data = JSON.parse(ev.data);\n'
        '        if (data.state === "running") { es.close(); window.location.reload(); return; }\n'
        '        render(data);\n'
        '      } catch (e) {}\n'
        '    };\n'
        '    es.onerror = function () { scheduleReloadFallback(); };\n'
        '  } catch (e) {\n'
        '    scheduleReloadFallback();\n'
        '  }\n'
        '})();\n'
        '</script>\n'
        '</body>\n'
        '</html>\n'
    )


STATIC_FILE_EXTENSIONS = {
    ".js", ".css", ".map", ".json", ".webmanifest",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".wav", ".ogg",
}


def _build_static_allowlist(static_root: Path) -> "dict[str, str]":
    """Scan ``static_root`` once at startup and return a dict mapping every
    relative asset path (as-is) to its trusted absolute-path string.

    Using a constant-populated dict + ``.get()`` is the CodeQL-recognised
    sanitizer shape for ``py/path-injection``: the tainted request path is
    used only as a dict key; the VALUE returned by ``.get`` comes from the
    dict population (a trusted filesystem walk at startup), so the returned
    string carries no taint from the caller's perspective.
    """
    if not static_root.is_dir():
        return {}
    mapping: dict[str, str] = {}
    for entry in static_root.rglob("*"):
        if entry.is_file():
            rel = entry.relative_to(static_root).as_posix()
            mapping[rel] = str(entry)
    return mapping


def create_static_handler(config_dir: str, backend: BackendManager | None = None):
    static_root = STATIC_DIR.resolve()
    # Trust anchor: enumerated at startup from the filesystem, never from user input.
    # Dict values are the trusted absolute asset paths; dict.get(user_key) yields
    # a value that CodeQL's taint tracker treats as untainted (comes from dict
    # population, not from the tainted key).
    ALLOWED_ASSETS: "dict[str, str]" = _build_static_allowlist(static_root)
    _config = _read_bazarr_config(config_dir)
    _startup_base_url = _config.get("baseUrl", "/") or "/"

    async def static_handler(request: web.Request) -> web.StreamResponse:
        """Serve static frontend files, fallback to index.html for SPA routing."""
        path = request.path.lstrip("/")

        # Dict .get() with a tainted key returns a value sourced from the trusted
        # dict population. No taint flows from `path` into `safe_path`.
        if path and path != "index.html":
            safe_path = ALLOWED_ASSETS.get(path)
            if safe_path is not None:
                content_type = mimetypes.guess_type(safe_path)[0] or "application/octet-stream"
                return web.FileResponse(safe_path, headers={"Content-Type": content_type})

        # If the request looks like a static file (has a known extension), return 404
        # instead of the SPA fallback. This prevents serving index.html as JavaScript
        # when the browser requests old/stale asset filenames.
        ext = os.path.splitext(path)[1].lower()
        if ext in STATIC_FILE_EXTENSIONS:
            return web.Response(status=404, text="Not found")

        status = backend.get_status() if backend else {}
        state = status.get("state")

        if backend and state == BackendManager.STATE_RUNNING:
            return await proxy_handler(request)

        if backend and state == BackendManager.STATE_STARTING:
            return web.Response(
                status=503,
                text=_get_startup_html(status, _startup_base_url),
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        # Serve patched index.html for SPA routing when no backend manager is wired.
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

    config = _read_bazarr_config(config_dir)
    base = config.get("baseUrl", "/").strip("/")

    # Supervisor-handled endpoints (not proxied)
    app.router.add_route("GET", "/_supervisor/status", create_supervisor_status_handler(backend))
    app.router.add_route("GET", "/_supervisor/events", create_supervisor_sse_handler(backend))

    # API/image proxy routes: register both root and base_url-prefixed versions
    # so deployments with general.base_url (e.g. /bazarr) work correctly
    for prefix in PROXY_PREFIXES:
        app.router.add_route("*", prefix + "{path:.*}", proxy_handler)
        if base:
            app.router.add_route("*", f"/{base}{prefix}" + "{path:.*}", proxy_handler)

    # Static file catch-all
    app.router.add_route("GET", "/{path:.*}", create_static_handler(config_dir, backend))

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

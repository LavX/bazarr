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

The listen port is --port when given, otherwise general.port from config.yaml,
otherwise 6767. The backend gets a free loopback port picked at each launch.
config.yaml is the one the backend reads: under -c/--config when given,
otherwise BAZARR_CONFIG_DIR, otherwise the data directory beside the code.
"""

import asyncio
import base64
import html
import importlib.util
import json
import mimetypes
import os
import secrets
import signal
import socket
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from aiohttp import (ClientConnectorError, ClientSession, ClientTimeout,
                     ConnectionTimeoutError, TraceConfig, WSMsgType, web)

APP_DIR = Path(__file__).resolve().parent.parent
import yaml  # noqa: E402

# The request-host policy, shared with the backend so both layers apply the
# same rules. Loaded from the file rather than imported as a package: the
# supervisor starts before the backend exists and must not import any of it.
_POLICY_PATH = APP_DIR / "bazarr" / "app" / "host_policy.py"
_policy_spec = importlib.util.spec_from_file_location("bazarr_host_policy", _POLICY_PATH)
host_policy = importlib.util.module_from_spec(_policy_spec)
_policy_spec.loader.exec_module(host_policy)

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
# The loopback port proxy_handler forwards to. BackendManager picks a free
# port at every launch, and the readiness poll sets it here only after the
# backend on it has answered with this supervisor's token. It goes back to
# None when that backend exits or restarts from inside the app. On a network
# namespace shared with another instance (network_mode: host, or one VPN
# container) an unverified port can be that instance's backend.
BACKEND_PORT = None
# The token that backend proved itself with. The proxy checks it on every
# answer it relays as well, since a backend that crashes without closing its
# listener leaves BACKEND_PORT set until bazarr.py notices, a few seconds on,
# and on a shared network namespace another instance can bind the port then.
BACKEND_TOKEN = None
DEFAULT_PORT = 6767  # external port users connect to, unless general.port says otherwise
# The port the front listener bound, for the container health check. /tmp is a
# tmpfs under read_only, and it is private to the container even when the
# network namespace is shared.
PORT_FILE = Path("/tmp/bazarr-supervisor.port")
# The backend stamps this header on its responses with the token the supervisor
# gave it, so the readiness poll can tell its own backend from a neighbour's.
TOKEN_HEADER = "X-Bazarr-Supervisor-Token"

# The backend's acknowledgement of a refused-host report carries its own
# accounting on these headers: the times of the lines its hour has spent,
# with the hostname each of them was logged for, the suppressed window it
# holds with its stable ID and with the identities of every window that
# window counts and the counts each contributed, and the IDs of the
# windows it has logged and cleared, so this console can claim the whole
# aggregate its mirror stands for and drop every copy it holds of a
# window whose summary has been paid. The backend defines the same names
# on its side, because the two processes share no imports. The per-host
# pace travels as two parallel lists, the same shape the report's newest
# names use, because a hostname itself is not a safe separator.
ACCOUNTING_HOUR_LINES_HEADER = "X-Bazarr-Hour-Lines"
ACCOUNTING_HOUR_LINES_IDS_HEADER = "X-Bazarr-Hour-Line-Ids"
ACCOUNTING_HOUR_HOSTS_HEADER = "X-Bazarr-Hour-Hosts"
ACCOUNTING_HOUR_HOST_LINES_HEADER = "X-Bazarr-Hour-Host-Lines"
ACCOUNTING_SUPPRESSED_HEADER = "X-Bazarr-Suppressed"
ACCOUNTING_SUPPRESSED_SINCE_HEADER = "X-Bazarr-Suppressed-Since"
ACCOUNTING_SUPPRESSED_ID_HEADER = "X-Bazarr-Suppressed-Id"
ACCOUNTING_SUPPRESSED_IDS_HEADER = "X-Bazarr-Suppressed-Ids"
ACCOUNTING_SUPPRESSED_COUNTS_HEADER = "X-Bazarr-Suppressed-Counts"
ACCOUNTING_COMPLETED_WINDOWS_HEADER = "X-Bazarr-Completed-Windows"

# Paths that get proxied to the backend
PROXY_PREFIXES = ("/api/", "/images/", "/test/", "/system/backup/download/", "/bazarr.log")

# The backend's own request body ceiling (MAX_REQUEST_BODY_SIZE in
# bazarr/app/app.py). Waitress refuses a declared length at the ceiling, not
# only above it, and it counts a chunked body with its chunk framing. Its
# refusal carries no token, so relayed it would read as a neighbour's, and the
# proxy refuses first: a declared length at the ceiling, and a body relayed in
# chunks once it comes within STREAM_FRAMING_ALLOWANCE of it.
MAX_REQUEST_BODY_SIZE = 256 * 1024 * 1024  # 256 MiB

# A body relayed in chunks is sent in pieces of at least RELAY_CHUNK_SIZE, so
# its framing stays well inside the allowance: under 50 KiB at the ceiling.
RELAY_CHUNK_SIZE = 64 * 1024
STREAM_FRAMING_ALLOWANCE = 64 * 1024

# The encodings aiohttp decodes as it reads a request body. What the proxy
# relays for one of these is the decoded body, so its declared length and its
# Content-Encoding no longer describe it.
DECODED_ENCODINGS = {"gzip", "deflate", "br", "zstd"}

# An answer has 300 seconds, counted from the request's last byte: a large
# upload over a slow link can take longer than that to arrive, and the backend
# has nothing to answer until it has.
ANSWER_TIMEOUT = 300
PROXY_TIMEOUT = ClientTimeout(total=None, connect=5)


def _set_backend_port(port, token=None) -> None:
    global BACKEND_PORT, BACKEND_TOKEN
    BACKEND_PORT = port
    BACKEND_TOKEN = token if port is not None else None


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
        "Installing providers",
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
        ("Provider Hub startup auto-install", 1),       # Installing providers
        ("check_update", 2),                            # Checking for updates
        ("Scheduler will use this timezone", 3),        # Starting scheduler
        ("jobs queue started", 4),                      # Starting jobs queue
        ("waiting for requests on", 5),                 # Starting HTTP server
        ("SignalR client for", 6),                      # Connecting to Sonarr/Radarr
    ]

    # bazarr.py prints this each time it starts bazarr/main.py. The first time
    # in a launch is the child the startup poll checks; any later one is an
    # in-app restart on the same port.
    _CHILD_START_MARKER = "starting child process"
    # bazarr/app/server.py prints this line just before it closes its listener.
    _LISTENER_CLOSE_LINE = "Closing webserver..."

    def __init__(self, bazarr_args: list[str]):
        self.bazarr_args = bazarr_args
        self.token = secrets.token_hex(16)
        self.process = None
        self.port = None  # the loopback port of the current launch
        self._should_run = True
        self.state = self.STATE_STARTING
        self._stage_index = 0  # index into STAGES
        self._last_exit_code = None
        self._child_starts = 0
        self._ready_task = None

    async def run(self):
        """Start bazarr as a subprocess, auto-restart on crash."""
        bazarr_py = str(APP_DIR / "bazarr.py")
        env = os.environ.copy()
        # Loopback only, whatever general.ip says: the supervisor is the only
        # client, and under host networking '*' published the backend too.
        env["BAZARR_BACKEND_HOST"] = BACKEND_HOST
        env["BAZARR_SUPERVISOR_TOKEN"] = self.token

        while self._should_run:
            self.state = self.STATE_STARTING
            self._stage_index = 0
            self._child_starts = 0
            _set_backend_port(None)
            try:
                # A fresh port per launch. If something takes it before the
                # backend binds, the backend exits and the next launch picks
                # another one.
                self.port = _pick_backend_port()
                cmd = [sys.executable, bazarr_py, "--port", str(self.port)] + self.bazarr_args
                print(f"[supervisor] Starting backend: {' '.join(cmd)}")
                self.process = await asyncio.create_subprocess_exec(
                    *cmd, env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                # Read stdout in background to detect stages and forward to our stdout
                reader_task = asyncio.create_task(self._read_stdout())
                # Wait for backend to actually accept connections
                self._ready_task = asyncio.create_task(self._wait_for_ready())
                code = await self.process.wait()
                _set_backend_port(None)
                reader_task.cancel()
                self._last_exit_code = code
                if self._should_run:
                    self.state = self.STATE_CRASHED
                    print(f"[supervisor] Backend exited ({code}), restarting in 3s...")
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                _set_backend_port(None)
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
                self._watch_for_restart(text)
                # Check for stage markers (only advance forward)
                if self.state == self.STATE_STARTING:
                    for marker, idx in self._STAGE_MARKERS:
                        if marker in text and idx > self._stage_index:
                            self._stage_index = idx
                            break
        except asyncio.CancelledError:
            pass

    def _watch_for_restart(self, text: str) -> None:
        """Hold the proxy while an in-app restart swaps the backend.

        bazarr.py starts a new bazarr/main.py on the same port while this
        supervisor's process keeps running, so the port is free from the
        moment the old listener closes until the new one binds. The proxy
        waits for the new child to answer with this supervisor's token, as
        it did at startup. The state stays as it is, so the health check
        does not count a restart from the UI as an outage.
        """
        if text.strip() == self._LISTENER_CLOSE_LINE:
            _set_backend_port(None)
        elif self._CHILD_START_MARKER in text:
            self._child_starts += 1
            if self._child_starts > 1:
                _set_backend_port(None)
                if self._ready_task is None or self._ready_task.done():
                    self._ready_task = asyncio.create_task(self._wait_for_ready())

    async def _wait_for_ready(self):
        """Poll the backend until it answers as this supervisor's own, then
        let the proxy through to it and, at startup, set state to RUNNING.

        Only an answer carrying this supervisor's token counts. Anything else
        on the port is not our backend, and trusting it would put this UI in
        front of another instance.

        Poll for as long as the backend process is alive and unverified,
        rather than giving up after a fixed number of attempts. A long first-boot
        migration (e.g. auto-installing providers that build dependency venvs) can
        delay the backend binding past a fixed window; abandoning the poll would
        leave the startup screen stuck even though the process is healthy and
        about to bind. Process exit is handled by run() (which flips state to
        CRASHED); the dead-process guard below also ends this loop.
        """
        process, port = self.process, self.port
        url = f"http://{BACKEND_HOST}:{port}/api/system/status"
        timeout = ClientTimeout(total=2, connect=1)

        def launch_is_current():
            return process is not None and process.returncode is None and self.process is process

        while self._should_run and BACKEND_PORT is None:
            if not launch_is_current():
                return
            try:
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if (resp.status < 500 and resp.headers.get(TOKEN_HEADER) == self.token
                                and launch_is_current()):
                            _set_backend_port(port, self.token)
                            if self.state == self.STATE_STARTING:
                                self.state = self.STATE_RUNNING
                                self._stage_index = len(self.STAGES) - 1
                                print("[supervisor] Backend is ready")
                            else:
                                print("[supervisor] Backend is ready again after a restart")
                            # The backend can take the refused hostnames it
                            # missed while it was unreachable: the delivery
                            # worker is woken, and everything pending is due.
                            if _active_reporter is not None:
                                _active_reporter.ready_to_deliver()
                            return
            except Exception:
                pass
            _print_due_refusal_summary_for_active_reporter()
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
def _backend_starting_response() -> web.Response:
    return web.json_response(
        {"error": "Backend is starting up"},
        status=503,
    )


def _body_too_large_response() -> web.Response:
    return web.json_response(
        {"error": f"Request body is too large: the limit is {MAX_REQUEST_BODY_SIZE // (1024 * 1024)} MiB"},
        status=413,
    )


class _BodyTooLarge(Exception):
    pass


async def _bounded_body(content, limit, finished):
    """Relay the body as it arrives, and stop it once more than ``limit`` bytes
    have come, before the excess is relayed. ``finished`` is called once the
    last byte has been handed on."""
    size = 0
    pending = bytearray()
    async for chunk in content.iter_any():
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge()
        pending += chunk
        if len(pending) >= RELAY_CHUNK_SIZE:
            yield bytes(pending)
            pending.clear()
    if pending:
        yield bytes(pending)
    finished()


def _caused_by(error, kind) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, kind):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _from_own_backend(headers, token, port) -> bool:
    """Whether an answer carries this supervisor's token. Anything else on the
    port is not this instance's backend and is never relayed."""
    if headers is not None and headers.get(TOKEN_HEADER) == token:
        return True
    print(f"[supervisor] Refused an answer on port {port} that did not come from this instance's backend")
    return False


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """Proxy API/image requests to the backend."""
    port, token = BACKEND_PORT, BACKEND_TOKEN
    if port is None or token is None:
        # No backend has proved itself yet, so nothing is relayed, not even a
        # websocket upgrade.
        return _backend_starting_response()
    target_url = f"http://{BACKEND_HOST}:{port}{request.path_qs}"

    # WebSocket upgrade
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_websocket(request, target_url, token, port)

    if request.content_length is not None and request.content_length >= MAX_REQUEST_BODY_SIZE:
        return _body_too_large_response()
    encoding = request.headers.get("Content-Encoding", "")
    decoded = encoding.isascii() and encoding.lower() in DECODED_ENCODINGS
    # The length the body is relayed with, or None when it goes in chunks.
    length = None if decoded else request.content_length

    loop = asyncio.get_running_loop()
    response = None
    try:
        async with asyncio.timeout(None) as deadline, ClientSession(timeout=PROXY_TIMEOUT) as session:
            def start_answer_deadline():
                if deadline.when() is None:
                    deadline.reschedule(loop.time() + ANSWER_TIMEOUT)

            # Strip hop-by-hop / framing headers before forwarding. aiohttp
            # de-chunks the body as it arrives, so forwarding
            # Transfer-Encoding: chunked would tell waitress to expect chunk
            # frames that never come, and waitress then hangs waiting for the
            # trailer. This manifested as silent 100s+ hangs on any POST from
            # clients like .NET's HttpClient that default to chunked request
            # bodies. The body is streamed through instead of read whole: with
            # its declared length when it had one and aiohttp did not decode
            # it, otherwise re-chunked by the client session itself. Reading it
            # whole went through aiohttp's 1 MiB request limit, so every larger
            # upload was answered as "Backend is starting up".
            _drop = {"host", "content-length", "transfer-encoding",
                     "connection", "keep-alive", "expect"}
            if decoded:
                _drop.add("content-encoding")
            forwarded_headers = {k: v for k, v in request.headers.items()
                                 if k.lower() not in _drop}
            if length is not None:
                forwarded_headers["Content-Length"] = str(length)
            # Advertise the CLIENT-facing URL to the backend. Without
            # these, Flask's request.host is the internal loopback address
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
            if request.body_exists:
                limit = MAX_REQUEST_BODY_SIZE - 1 if length is not None \
                    else MAX_REQUEST_BODY_SIZE - STREAM_FRAMING_ALLOWANCE
                body = _bounded_body(request.content, limit, start_answer_deadline)
            else:
                body = b""
                start_answer_deadline()
            async with session.request(
                method=request.method,
                url=target_url,
                headers=forwarded_headers,
                data=body,
                allow_redirects=False,
            ) as resp:
                # Waitress answers a body it refuses before the body is done.
                start_answer_deadline()
                if not _from_own_backend(resp.headers, token, port):
                    return _backend_starting_response()
                response = web.StreamResponse(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in ("transfer-encoding", "content-encoding", "content-length",
                                                  TOKEN_HEADER.lower())},
                )
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
    except Exception as error:
        if response is not None and response.prepared:
            # Part of the answer is already relayed, so no other answer can
            # follow it. Closing the connection leaves it visibly cut short.
            if request.transport is not None:
                request.transport.close()
            return response
        if _caused_by(error, _BodyTooLarge):
            return _body_too_large_response()
        return _backend_starting_response()


async def _proxy_websocket(request: web.Request, target_url: str, token: str,
                           port: int) -> web.StreamResponse:
    """Proxy WebSocket connections to the backend.

    The backend's handshake has to carry this supervisor's token before the
    browser's upgrade is accepted, the same check every HTTP answer gets.
    aiohttp only hands the handshake response to trace hooks.
    """
    handshake = {}

    async def remember_handshake(session, context, params):
        handshake["headers"] = params.response.headers

    trace = TraceConfig()
    trace.on_request_end.append(remember_handshake)

    ws_client = None
    ws_url = target_url.replace("http://", "ws://")
    try:
        async with ClientSession(trace_configs=[trace]) as session:
            async with session.ws_connect(ws_url) as ws_server:
                if not _from_own_backend(handshake.get("headers"), token, port):
                    return _backend_starting_response()
                ws_client = web.WebSocketResponse()
                await ws_client.prepare(request)

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

    if ws_client is None:
        # The backend refused the socket or could not be reached, so the
        # browser's upgrade was never accepted.
        return _backend_starting_response()
    return ws_client


# ---------------------------------------------------------------------------
# Request-host check
# ---------------------------------------------------------------------------

def _print_refusal_line(text: str) -> None:
    """The supervisor's channel for bounded refusal lines: its own console."""
    print(f"[supervisor] {text}")


class _HostCheckState:
    """The host-check inputs from config.yaml, refreshed as the file changes.

    The backend rewrites config.yaml on every settings save, and an approval
    reaches it through this proxy, so the inputs are re-read whenever the
    file's stamp moves. The stamp carries the nanosecond mtime, the size and
    the inode, so a rewrite inside one coarse-mtime tick still reads fresh,
    and the route that relays an approval invalidates the cache besides. A
    read that fails, or one that comes back without both the general and auth
    sections (a write that is still in flight can), keeps the last good
    values rather than answering a new question with none.
    """

    def __init__(self, config_dir: str):
        self._path = _config_yaml(config_dir)
        self._stamp = None
        self._auth_type = None
        self._trusted_proxy = None  # None: the setting is absent, its default applies
        self._allowed_hosts: list = []

    def refresh(self) -> None:
        try:
            info = self._path.stat()
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            return
        if stamp == self._stamp:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
        except Exception as error:
            print(f"[supervisor] Could not read config.yaml for the host check: {error}")
            return
        general = config.get("general") if isinstance(config, dict) else None
        auth = config.get("auth") if isinstance(config, dict) else None
        if not isinstance(general, dict) or not isinstance(auth, dict):
            return
        auth_type = auth.get("type")
        allowed = general.get("allowed_hosts")
        if isinstance(allowed, str):
            # The same hand-edit form the backend accepts: one name on its
            # own line reads as a comma-separated string.
            allowed = [part.strip() for part in allowed.split(",") if part.strip()]
        raw_trusted = general.get("trusted_proxy")
        self._auth_type = auth_type if isinstance(auth_type, str) else None
        self._trusted_proxy = None if raw_trusted is None else str(raw_trusted).strip()
        self._allowed_hosts = allowed if isinstance(allowed, list) else []
        self._stamp = stamp

    def invalidate(self) -> None:
        self._stamp = None

    def auth_type(self):
        return self._auth_type

    def trusted_proxy(self):
        return self._trusted_proxy

    def allowed_hosts(self) -> list:
        return self._allowed_hosts


def host_check_decision(request_host, remote, forwarded_host, auth_type, trusted_proxy,
                        allowed_hosts, environ=None):
    """(allowed, refused hostnames): the same rule the backend applies.

    The forwarded host is believed only from an address in
    general.trusted_proxy, mirroring the backend: any other peer is judged by
    the hostname it sent. When a forwarded host is believed, the hostname the
    peer sent has to pass too, so one request cannot arrive carrying two names
    where only one is recognized. A configured form or basic login turns the
    check off, and the policy answers the rest.

    A refusal names the hostname that failed, the one the peer sent first,
    and both of them when both failed, so the page and the report to the
    backend name a hostname the allowlist has to grow, exactly as the
    backend's own refusal does.
    """
    effective = request_host
    if forwarded_host and host_policy.peer_is_trusted_proxy(remote, trusted_proxy):
        effective = forwarded_host.split(",")[-1].strip()
    if auth_type in ("form", "basic"):
        return True, []
    refused = host_policy.refused_hostnames(request_host, effective, allowed_hosts, environ)
    if not refused:
        return True, []
    return False, [host_policy.effective_host(name) for name in refused]


def _host_refusal_html(host: str, base: str, also: str | None = None) -> str:
    """The static refusal page, which doubles as the approval form."""
    shown = html.escape(host or "")
    named = shown if shown else "a request that carried no hostname"
    forwarded = html.escape(also) if also else ""
    if also:
        named += f" (and its forwarded hostname {forwarded})"
    action = html.escape(f"{base}/{host_policy.APPROVAL_SEGMENT}")
    code_note = ("The code is printed to this container's logs (<code>docker logs</code>) "
                 "and nowhere else; it is replaced after each use and by every restart: "
                 "use the most recent line.")
    if also:
        # Two refused hostnames are two entries, and the composite display
        # string that names the arrival is never one of them: an entry that
        # reads "first (and its forwarded hostname second)" matches neither
        # hostname. Each entry is named on its own, the environment gets
        # the one comma-separated value that carries both, and the approval
        # code approves one hostname per submission, so the form below
        # carries the first and a request for the second answers with its
        # own refusal page and code.
        instruction = (
            f'To answer it, add <code>{shown}</code> and <code>{forwarded}</code> to '
            '<code>general.allowed_hosts</code> in config.yaml, or set '
            f'<code>{host_policy.ALLOWED_HOSTS_ENV}={shown},{forwarded}</code> in the '
            'environment, or enter the one-time approval code below. The code approves one '
            f'hostname per submission: the form below carries <code>{shown}</code>, and a '
            f'request for <code>{forwarded}</code> answers with its own refusal page and '
            f'code. {code_note}')
    else:
        instruction = (
            f'To answer it, add <code>{named}</code> to <code>general.allowed_hosts</code> '
            'in config.yaml, or set '
            f'<code>{host_policy.ALLOWED_HOSTS_ENV}</code> in the '
            f'environment, or enter the one-time approval code below. {code_note}')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bazarr+ does not recognize this address</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }}
code {{ background: #f0f0f0; padding: 0 0.2rem; }}
</style>
</head>
<body>
<h1>Bazarr+ does not recognize this address</h1>
<p>While no login is configured, Bazarr+ only answers requests for hostnames it
recognizes. This one arrived for <code>{named}</code>.</p>
<p>{instruction}</p>
<form method="post" action="{action}">
<input type="hidden" name="host" value="{shown}">
<p><label for="host-approval-code">Approval code</label>
<input id="host-approval-code" type="text" name="code" autocomplete="off"></p>
<p><button type="submit">Allow this hostname</button></p>
</form>
</body>
</html>"""


def _host_refusal_response(request: web.Request, host: str, base: str,
                           also: str | None = None) -> web.Response:
    path = request.path
    # The API answers JSON under the base and at the root, which a
    # prefix-stripping proxy leaves it at: the same root path the approval
    # form is served at, so a refused root API request gets the API's own
    # refusal and not the page, exactly as under the prefix.
    api_prefixes = [f"{base}/api", "/api"]
    if any(path == prefix or path.startswith(prefix + "/") for prefix in api_prefixes):
        body = {
            "code": host_policy.REFUSED_REASON,
            "host": host,
            "message": "Bazarr+ does not answer requests for hostnames it does not recognize while no "
                       "login is configured. Allow the hostname under Settings > General > Allowed Hosts, "
                       f"or set {host_policy.ALLOWED_HOSTS_ENV}, or use the approval code on the "
                       "refused-host page.",
        }
        if also:
            body["also_refused"] = also
        return web.json_response(body, status=421)
    return web.Response(status=421, text=_host_refusal_html(host, base, also=also),
                        content_type="text/html", headers={"Cache-Control": "no-store"})


# How long a report that no backend acknowledged waits before it may be tried
# again, so a backend that is down or failing is not asked about the same
# hostname on every refusal. While the last delivery failed it holds back
# every later attempt as well, one per pace, so a failing backend is not
# asked in a stream.
RETRY_PACE_SECONDS = 60

# The delivery worker never waits less than this before another look, so a
# delay that computes to zero, an overdue pace under an unset port or a
# spent hour, can never turn its loop into a busy spin.
MIN_RETRY_WAIT_SECONDS = 1

# The separate global bound for the reports themselves: at most
# REFUSED_HOSTS_LIMIT acknowledged deliveries an hour in total, the same
# ceiling the backend's refused-host record holds, so a long roll of
# distinct hostnames never becomes a long roll of requests. A delivery the
# backend did not acknowledge does not count against it, so a crash window
# of refused connections cannot spend the hour instead of the recovery;
# the retry pace above holds those attempts back instead. Hostnames past
# the bound stay pending and go out as the hour rolls over.
REPORTS_PER_HOUR = host_policy.REFUSED_HOSTS_LIMIT

# The reporter of the one host check this process fronts, so the readiness
# poll can wake the delivery worker for the reports held while the backend
# could not be reached.
_active_reporter: "_RefusalReporter | None" = None


class _RefusalReporter:
    """Bounded refusal lines and reports for the front listener's host check.

    The console lines and the backend reports are kept apart on purpose. A
    line is written at most once per hostname per hour, and past the hour's
    ceiling one summary line counts the rest; but every refused hostname is
    reported to the backend at its own once-an-hour-per-host pace, because
    once the backend has the hostname its health page and its console code
    can name it. A report no backend acknowledged stays pending: at most
    REFUSED_HOSTS_LIMIT of them, the newest of the roll as the backend's
    record keeps the newest refused, and the one delivery worker below
    sends them one at a time, woken by refusals and by readiness, retrying
    on its own timer, under the hour's global reporting bound above. The
    per-host cooldown is kept by time alone, never by a count, so no
    amount of other hostnames makes a told one tellable again within its
    hour.

    While the backend answers, its console owns the refusal lines: the
    report reaches it and its own record writes them. This console writes
    them while the backend cannot be told, and again whenever a delivery
    fails, because from that moment this console is the only place the
    hostname is named. A hostname this console spent a line on, or held
    back under its ceiling, carries the time of that with its report, so
    the backend writes no second line for it in the same hour, and every
    report carries the times of the lines this console spent within the
    hour, so a hostname refused for the first time once the backend answers
    draws on the one merged budget, which rolls over exactly when those
    lines do.

    The repeats of a hostname the hour has already acknowledged are the
    case no report covers: no delivery goes out for them, so no backend
    counts them, and while the backend answers every one of them is
    counted into this console's own window instead, the same idempotent
    aggregate the outage holds its lines in, so the pair owes the repeats
    one summary whichever recorder ends up holding it.

    The first report a backend acknowledges also carries this
    console's suppressed window, so the summary the window owes is paid
    once by whichever layer holds it: the hour's line budget and its one
    summary are the two processes' shared allowance, even though they
    share no memory. A pending notice keeps the time of the refusal it
    reports on as well, carried with the report when it finally lands:
    the newest names are bounded and the line the console spent is kept
    for its hour alone, so the notice's own stamp is the one record of
    the refusal's time that survives to the delivery.

    The allowance flows back the other way as well: every acknowledgement
    carries the backend's own hour with it, and this console folds the
    spent line times into its recorder exactly as it folds its own, so a
    backend that spent the hour and then became unreachable leaves this
    console nothing more to spend. The pace of each of those lines travels
    with the times, the hostname every one was logged for, so a hostname
    the backend named is not named again by this console in the same
    hour, whichever layer ends up the only one that can write. The window the acknowledgement carried
    is kept beside this console's own, never inside it, because one window
    is owed one summary by whichever layer holds it when its hour runs:
    a window held in two recorders would be reported twice, and this copy
    is never reported here at all. It is handed back on every report
    instead, under its ID, so the backend that still holds it folds it
    nowhere and a backend that lost it to a restart folds it in and owes
    its summary again.

    A window a report has carried is never folded back into this console's
    recorder either, which is what keeps a lost answer to a report the
    backend processed from leaving the pair with two summaries of one
    window: a delivery no backend acknowledged, and an acknowledgement that
    left its window unresolved, hold it in an in-flight slot that the next
    report carries again, and this console emits it only in the
    sustained-outage case, when no acknowledgement of any kind has arrived
    since the carry began, the window's hour has fully run past with it and
    no delivery for it ever reached the backend at the transport level,
    because only then could nothing have summarized it. A window a
    delivery sent but no answer resolved is never emitted here however
    long the outage runs: the backend may have logged its summary before
    the answer was lost, so the pair leaves the window to the
    acknowledgement protocol instead. The acknowledgement also names the
    windows the backend has logged and cleared, and this console discards
    the carried and mirrored copies it holds of them, so a window whose
    summary has been paid is carried nowhere anymore. A window an answer
    carries back, held by the backend and not yet paid, is owned from
    the answer on by the aggregate the answer carries: the backend folded
    the carried window into it before the answer was built, so the
    mirror of the aggregate already stands for the folded lines, and
    this console keeps no copy of its own beside the mirror, which a
    restarted backend would count a second time beside the aggregate
    that already counts them. Whether a mirrored window's summary has
    been paid is read from the record of completed windows the two
    processes share, not from anything a delivery of this console could
    observe: the backend pays at the arrival, and the moment a request
    went out is not the arrival, so the send time can prove nothing,
    while the payment is on the shared record before the answer is
    built. A marked copy, one the record shows paid, is carried while
    the backend's own refusal of its folds lasts, dropped at a backend
    that starts fresh, and dropped by the envelope the payment's memory
    runs in, so a copy this console still carries meets a backend that
    still remembers paying it and the pair never folds a paid window in
    twice. A delivery whose answer is still unresolved when readiness
    swears in a fresh backend speaks for the one it was sent to, and
    the payment its reach could report is left with that backend: the
    copy is dropped with the swap rather than carried to a generation
    it cannot vouch for.

    Every report also carries the newest bounded refused names with this
    console's own recency stamps, and the backend's record takes the newer
    stamp per host: a hostname this console already told within the hour is
    skipped by its reports, and the reporting bound holds the newest names
    of a burst back, so without the set the record would hold older names
    while the newest refusals waited out the hour.
    """

    def __init__(self, notice_path: str, now=time.time):
        self.notice_path = notice_path
        self._now = now
        self._lines = host_policy.BoundedRefusalRecorder(now=now)
        self._last_report = OrderedDict()  # host -> time of its last acknowledged report
        self._pending = OrderedDict()  # host -> time of its last attempt (None until tried)
        self._pending_at = OrderedDict()  # host -> when this console last refused a waiting host
        self._pending_ids = OrderedDict()  # host -> the stable identity of its waiting notice
        self._accounted_notices = set()  # identities of notices whose refusal this console counted
        self._attempted_notices = set()  # identities of notices the worker has taken for a delivery, however their retries read
        self._accounted = OrderedDict()  # host -> time this console last spent its line
        self._recent = OrderedDict()  # host -> time this console last refused it
        self._report_times = []  # times of this hour's acknowledged report deliveries
        self._last_failure = None  # when the last delivery failed, until an ack or readiness
        self._mirrored = None  # the window the last acknowledgement carried back
        self._mirrored_paid = False  # whether a delivery reached the backend past its hour
        self._mirrored_owner = False  # whether this console claimed the mirrored window's payment
        self._mirrored_released = False  # whether a carry returned the mirrored window's claim to the backend
        self._orphaned = None  # a released mirror whose counts this console owes, its only remaining record
        self._recovered = None  # the unpaid window ownership a lost console left on the shared record, read back at the start
        self._carried = None  # the window a report has carried, held in flight here
        self._carried_at = None  # when the carry began
        self._carried_sent = False  # whether a delivery for it ever reached the backend
        self._last_ack = None  # when the last acknowledgement of any kind arrived
        self._in_flight = None  # the hostname whose delivery the worker took, until it resolves
        self._in_flight_evicted = False  # whether the queue evicted that notice mid-delivery
        self._in_flight_id = None  # the identity of the notice the in-flight delivery carries
        self._ready_generation = 0  # how many fresh backends readiness has sworn in
        self._delivery_generation = None  # the generation the in-flight delivery was taken in
        self._wake = asyncio.Event()  # refusals and readiness wake the worker
        self._drain_task: "asyncio.Task | None" = None
        self._recovered = self._recover_unpaid_ownership()
        if self._recovered is not None:
            # The debt a lost console left on the shared record is this
            # console's from the start, and it owes the pair no refusal to
            # carry it: the worker starts here, so its own paced look pays
            # the one summary the ownership counts once its hour has run,
            # and a console that serves quietly after its restart still
            # pays what the console before it claimed.
            self.start_drain()

    def _recover_unpaid_ownership(self):
        """The unpaid window ownership the shared record still holds.

        The console that takes the claim on a mirrored window's
        identities before either hour runs owns the one summary the two
        windows owe, and the ownership travels with the decision, the
        window's own facts written beside it on the record the two
        processes share: the console that replaces a lost one holds none
        of the promise the dead one made, and the record's entry is the
        one place it survives. The identities a standing ownership covers
        read as paid to the backend that still holds the window, so no
        timer of the pair pays the counts while the ownership holds
        them, and the debt a restart would have orphaned is read back
        here instead, to be paid the way the window it stands for was
        owed: the worker the recovery starts pays it on its own paced
        look, once the hour it counts has run, with no refusal needed to
        carry the debt to it. Every unpaid ownership folds into the one
        debt the way the
        windows they stand for fold: the counts add, the earliest
        opening keeps its place, and the one line the hour's budget rules
        name pays them together.
        """
        ownerships = host_policy.pending_window_ownerships()
        if not ownerships:
            return None
        count = 0
        opened = None
        ids = []
        seen = []
        for ownership in ownerships:
            ownership_ids = ownership.get('ids')
            if not isinstance(ownership_ids, list) or tuple(ownership_ids) in seen:
                # One claim writes the window's facts under every
                # identity it covers, so the same ownership reads back
                # once however many entries carry it, and the debt it
                # stands for joins the rest the one time it is owed.
                continue
            seen.append(tuple(ownership_ids))
            ownership_count = ownership.get('count')
            if isinstance(ownership_count, (int, float)):
                count += ownership_count
            ownership_opened = ownership.get('opened')
            if (isinstance(ownership_opened, (int, float))
                    and (opened is None or ownership_opened < opened)):
                opened = ownership_opened
            for window_id in ownership_ids:
                if window_id and window_id not in ids:
                    ids.append(window_id)
        if not ids:
            return None
        return count, (opened if opened is not None else self._now()), ids[0], ids

    def note_line(self, host):
        """Record a refusal; True when a console line may be written now."""
        accounted = self._lines.note(host)
        self._account_line(host)
        return accounted

    def _account_line(self, host, notice_id=None):
        """Remember that this console spent a line naming this refusal.

        The report the backend eventually takes carries the time, so the
        backend writes no second line for the same refusal in the same
        hour: the two processes share the hour's line budget even though
        they share no memory. The line a failed delivery named is spent
        for the notice it was named for, and is remembered under the
        notice's own identity: the retries of the failed notice still carry
        the time, so the backend folds the line the failure wrote and
        writes none of its own. The hostname is remembered beside the
        notice, because the hour's line budget the line bought belongs to
        the hostname and not to the notice whose delivery failed: a fresh
        notice of the same hostname carries the pace in its report, the
        backend folds the fresh refusal's line, and the hostname draws no
        second line within the hour, whichever notice its refusal arrived
        on. A line this console wrote itself, at a refusal the backend
        could not be told about, is remembered under the hostname alone,
        because the notice of that refusal is minted after the line is
        spent and its report carries the time either way. The bookkeeping
        is bounded and rolled past its hour like every other roll here.
        """
        now = self._now()
        self._accounted = OrderedDict(
            (name, stamp) for name, stamp in self._accounted.items()
            if now - stamp < host_policy.HOUR_SECONDS)
        if notice_id is not None:
            self._accounted[notice_id] = now
        # The hostname is written after the notice, so the bound the map
        # keeps holds a resolved notice's identity before the hostname's
        # own pace: the pace serves every later notice of the hostname,
        # while the identity it was named for is one delivery's fact.
        self._accounted[host] = now
        while len(self._accounted) > host_policy.REFUSED_HOSTS_LIMIT:
            self._accounted.popitem(last=False)

    def line_time(self, host, notice_id=None):
        """When this console last spent a line naming this hostname's
        refusal, within the hour, or None.

        The notice the report carries is asked first, because the line a
        failed delivery named belongs to the notice it failed for, and the
        hostname is asked for the lines this console named itself: the
        hour's line budget a line bought belongs to the hostname, so a
        fresh notice of a hostname whose line this console spent carries
        that pace in its report and inherits nothing else, while the
        report of a refusal whose line nobody here spent names its
        refusal afresh. A line a backend logged is not a pace this
        console hands a fresh notice, because the backend that holds the
        hostname's pace counts what its own pace holds back, and a
        backend that restarts knows nothing and names the refusal in its
        own right.
        """
        now = self._now()
        keys = (notice_id, host) if notice_id is not None else (host,)
        for key in keys:
            stamp = self._accounted.get(key)
            if stamp is not None and now - stamp < host_policy.HOUR_SECONDS:
                return stamp
        return None

    def hour_line_times(self):
        """The times of the refusal lines this console has spent within the hour.

        The report the backend eventually takes carries them, so a hostname
        refused for the first time once the backend answers draws on the one
        merged hour budget rather than a fresh cap, and the budget rolls
        over exactly when these lines do: the two processes share the
        hour's line budget even though they share no memory.
        """
        return self._lines.hour_line_times()

    def hour_line_ids(self):
        """The identities of this console's spent lines, parallel to their
        times.

        The report carries them beside the times, so the backend's merged
        budget spends one slot per line and not one per distinct timestamp:
        two refusals can share a timestamp and the lines written for them
        are still two lines.
        """
        return self._lines.hour_line_ids()

    def completed_windows(self):
        """The IDs of the windows this console's own recorder has paid."""
        return self._lines.completed_windows()

    def recent_refusals(self):
        """The newest bounded refused names with their stamps, oldest first.

        Every report carries them, so the backend's record takes the newer
        stamp per host without a report of each name's own: the newest
        refusals reach it whichever layer saw them.
        """
        return list(self._recent.items())

    def _note_recent(self, host, now):
        """Refresh the newest-refused set, which every refusal refreshes.

        A hostname this console already told within the hour is skipped by
        its reports, and the reporting bound holds the newest names of a
        burst back, so the set stands in for the record the backend keeps:
        it is bounded and rolled past its day the same way, and the next
        report carries it with this console's own recency stamps.
        """
        self._recent = OrderedDict(
            (name, stamp) for name, stamp in self._recent.items()
            if now - stamp < host_policy.DAY_SECONDS)
        if host in self._recent:
            self._recent.move_to_end(host)
        self._recent[host] = now
        while len(self._recent) > host_policy.REFUSED_HOSTS_LIMIT:
            self._recent.popitem(last=False)

    def merge_acknowledged(self, carried, hour_lines, hour_line_ids, hour_hosts, held,
                           completed):
        """Fold the accounting the backend's acknowledgement carried back.

        The line times join this console's hour budget exactly as its own
        do, so the two layers stay on the one spent allowance whichever of
        them spent it. The per-host pace of those lines travels with them,
        each hostname with the time its line was logged for, and joins this
        console's recorder as a line spent elsewhere within the hour: the
        fold charges no second hourly slot, because the aggregate of times
        the same answer carries already counts each of those lines once,
        and it holds this console from naming a hostname again in the
        same hour the backend named it, whichever layer ends up the only
        one that can write.
        The window the acknowledgement carried is kept
        beside this console's own, never inside it: whichever layer holds
        a window when its hour runs is the one that reports it, so a
        window held in two recorders would be reported twice, and this
        copy is never reported here at all. The acknowledgement replaces
        whatever the last one carried, so a repeated one cannot double it,
        and a held window the same answer reports completed is dropped
        instead, because the two cannot both be true of one window and
        the completed list is the one that says the summary has been paid.
        A fresh copy starts unpaid as well: whether a mirrored window's
        summary has been paid is read from the record of completed
        windows the two processes share, and the window an answer names
        is the one the backend holds now, whose payment no record has
        shown yet. A fresh copy starts unclaimed as well: the claim on a
        mirrored window's payment belongs to the window it was taken
        for, so an answer that carries a different window back leaves the
        claim behind with the one it replaced, and the same window
        carried back keeps it, whatever else the answer changed. The
        answer decides a release the same way: a mirror whose claim left
        with this console's own window in a report keeps its counts
        wherever the answer proves they live. A window carried back under
        the same identity proves the backend still holds them, and the
        copy starts over as the fresh mirror of the window that now
        counts them; a different window, or none, with no payment the
        record names, proves the backend holds nothing of them, and the
        counts become this console's own debt, held apart to be paid here
        and never handed back, because the backend that deferred them
        under the claim no longer remembers counting their identity.

        The acknowledgement also resolves the window this report carried:
        its ID named among the windows the backend has logged and cleared
        means the one summary the window owes has been paid, and this
        console discards its copy; one the answer carries back, held by
        the backend, is owned from the answer on by the aggregate the
        answer carries, which folded the carried window in before the
        answer was built and already counts its lines: the mirrored copy
        of the aggregate is what tells a restarted backend about them,
        exactly as it tells one about the backend's own, so this console
        keeps no second copy of the folded window beside the mirror,
        which a restarted backend would count a second time beside the
        aggregate that already counts them. An answer that carries no
        window back, because the backend took nothing in, leaves the copy
        in the in-flight slot, re-carried on every later report until an
        answer resolves it. Either way the carry counts as sent from
        here on, because the report that carried it was answered, so it
        reached the backend at the transport level whatever the answer
        said about the window, and the answer is an acknowledgement of
        some kind as well, so from here on the sustained-outage emission
        of a carried window is barred.
        """
        self._lines.merge_hour_lines(hour_lines, hour_line_ids)
        now = self._now()
        for host, stamp in hour_hosts:
            if now - stamp < host_policy.HOUR_SECONDS:
                # A line the backend logged holds this console's pace for
                # the hostname without spending anything here: the hourly
                # slot was already charged in the aggregate above, and a
                # hostname the backend named is not named again in the
                # same hour by whichever layer ends up the only one that
                # can write.
                self._lines.note(host, line_at=stamp)
        if held is not None and held[2] is not None and held[2] in completed:
            held = None
        if (self._mirrored is not None and self._mirrored_released
                and (held is None or held[2] != self._mirrored[2])
                and not host_policy.completed_windows_record_has(self._mirrored[2])):
            # The answer resolves the release: the mirror's claim left with
            # this console's own window in a report, and the window the
            # answer carries back is one the mirror never stood for, or no
            # window at all, while no payment anywhere names the counts the
            # mirror counts. They are this console's debt alone now: the
            # backend met the claim by deferring them to the line this
            # console owes, and it holds no copy of its own, so the copy
            # stays here as the only remaining record of the counts, never
            # handed back to a backend that would fold it in beside nothing
            # that remembers counting it.
            released = self._mirrored
            if self._orphaned is None:
                self._orphaned = released
            else:
                # A second debt joins the first: the two mirrors stood for
                # windows no backend holds any more, and the one payment
                # this console owes can claim every identity both carried,
                # opened at whichever window opened first, so no count
                # waits behind another for a place in the line.
                first = self._orphaned
                fresh = [(known, released[4][place] if place < len(released[4]) else 0)
                         for place, known in enumerate(released[3])
                         if known not in first[3]]
                self._orphaned = (first[0] + released[0],
                                  min(first[1], released[1]),
                                  first[2],
                                  first[3] + [known for known, _count in fresh],
                                  first[4] + [count for _known, count in fresh])
        if (self._mirrored is None or held is None
                or held[2] != self._mirrored[2]):
            # The acknowledgement replaces the mirror, and a claim taken
            # on the payment of the window the last one carried belongs to
            # nothing here: a fresh copy starts unclaimed, while the same
            # window carried back keeps the claim this console took on it.
            self._mirrored_owner = False
        # The release is decided by the answer alone: a window carried back
        # under the same identity puts the counts back with the backend that
        # holds them, and a different one or none leaves them with the debt
        # above or with a payment the record already named, so no later
        # answer decides what this one already has.
        self._mirrored_released = False
        self._mirrored = held
        self._mirrored_paid = False
        self._last_ack = now
        if carried is None:
            return
        if carried[2] is not None and carried[2] in completed:
            return
        if held is not None:
            # The backend folds the carried window into the one it holds
            # before it builds the answer, and this answer carries that
            # window back: the aggregate already counts the carried copy's
            # lines, so the copy belongs to the aggregate now, and the
            # mirror of it is what tells a restarted backend about them,
            # exactly as it tells one about the backend's own. A copy
            # kept beside the mirror would hand a restarted backend the
            # same lines twice, once in the aggregate and once as this
            # console's own, so it does not return to the slot.
            return
        self._carried = carried
        self._carried_sent = True

    def mirrored_summary(self):
        """The window the last acknowledgement carried back, for the report
        to hand to a backend that lost it.

        A backend that answers holds its window still, and a report hands
        this copy back to it: the backend folds a window whose ID it has
        already counted nowhere, so the copy costs the backend that still
        holds the window nothing, while a backend that lost it to a
        restart folds it in and owes its summary again. The copy stands
        for every window the aggregate counts, the backend's own and each
        one folded into it, so the one mirror is what re-delivers them
        all to a backend that lost them to a restart, and the console
        keeps no copy of a folded window beside it. An unmarked copy, one
        the record of completed windows names no payment for, is kept
        until an answer reconciles its payment and not dropped by the
        hour the window had left to run, nor by any span of silence
        since the last answer carried it: an aggregate whose hour ends
        while it is still unpaid, and whose backend restarts before it
        pays, has this copy for its only remaining record, however long
        the outage runs, so the next report tells the restarted backend
        again, and the answer that names the window completed clears the
        mirror here.

        A marked copy is a different hold, because its payment is on the
        record of completed windows the two processes share: the backend
        that holds the window paid its summary in the flush that follows
        every arrival, before its answer was built, and the payment is
        read here whatever became of that answer, of this console's own
        deliveries and of every backend that restarted since. The answer
        that reconciles the payment may simply be lost, so the marked
        copy is still carried while the backend's completed record can
        refuse its folds, renewed by every replay that reaches it. The
        hold ends within the envelope that record's own lease runs in:
        the payment cannot have happened before the window's hour, so
        past the whole span that begins there no backend can still
        remember paying the window, and the copy is dropped before any
        report can carry it to one that forgot, because a fold into a
        backend that forgot would count the paid lines a second time. A
        copy this console still carries therefore always meets a backend
        that still remembers paying it, and the summary already logged
        is never logged a second time.
        """
        if (self._mirrored is not None and not self._mirrored_paid
                and not self._mirrored_owner
                and host_policy.completed_windows_record_has(self._mirrored[2])):
            # The payment of the window this copy stands for is on the
            # shared record, written by the backend that paid it before
            # its answer was built: the copy is marked paid here however
            # that answer turned out, because the pair keeps one summary
            # per window and no send time of this console's own could
            # prove the arrival that paid it. An entry this console's own
            # claim wrote is not that proof, and the owner flag holds it
            # apart: the claim is the payment this console owes the
            # window, so the copy is never marked paid from it.
            self._mirrored_paid = True
        if (self._mirrored is not None and self._mirrored_paid
                and self._now() - self._mirrored[1]
                >= host_policy.HOUR_SECONDS + host_policy.CARRY_RETENTION_SECONDS):
            # The payment's whole envelope, the span that begins at the
            # window's hour, has run past with no arrival to renew the
            # record the payment left behind: no backend remembers paying
            # the window any more, so a report carrying the copy would
            # hand it to one that folds it in as a window it has never
            # seen and logs a second line for lines a first summary
            # already paid. The pair keeps the one summary the window
            # owes, which was logged when the copy was paid, rather than
            # risk the second line.
            self._mirrored = None
            self._mirrored_paid = False
            self._mirrored_released = False
        return self._mirrored

    def take_carried_summary(self):
        """Take the window the next report carries, the in-flight slot first.

        The first report a backend acknowledges carries this console's
        suppressed window along, so the backend folds it into its own and
        the two layers together owe the one summary the window counts. A
        window a report has carried before waits in the slot when the
        delivery came back unresolved, and is carried before anything the
        recorder still holds, because a window any report has carried is
        owed its summary by the backend and is never folded back into this
        console's own recorder: a lost answer to a report the backend
        processed must not leave the pair with two summaries of one window.
        A window taken fresh from this console's recorder begins its carry
        unsent, and the delivery attempt marks it once its request reaches
        the backend at the transport level, which ``take_summary`` below
        reads when it decides what this console may still emit. A carry
        that takes this console's own window out returns the claim it
        holds on the mirrored window's payment: the fold the backend
        makes of the carried window is the merged one's to report from
        then on, and the claim would hold its payment back. The answer
        that follows decides where the mirror's counts live: the backend
        keeps them if it answers with the same window, and this console
        owes them their one line if it answers with another or none, so
        the release preserves them instead of abandoning them with the
        claim.

        A copy waiting in the slot ages with it: a sent copy no answer
        resolved is dropped once the carry has run past the span the
        backend remembers the window's ID and the record of completed
        windows the two processes share proves the one summary the window
        owes was paid, before any report carries it. An unpaid copy keeps
        its place past that span as well: no layer ever logged its summary,
        because the backend that took the delivery in processed nothing
        and wrote no payment, so the copy is the only record of the lines
        the window counts, and the report that next carries it meets a
        backend that never counted its ID, folds it in once and pays the
        one summary it owes. A copy with no ID to prove its payment keeps
        the old bounded trade: nothing can tell it paid from unpaid, so it
        is dropped rather than carried to a backend that forgot it. An
        unsent copy is never dropped by age: no request byte of it ever
        left for any backend, nothing could have counted it, and the
        sustained-outage emission below may still read it. The age runs
        from the take alone and never from a restore, because a restore
        keeps the same carry going rather than starting one, so the span
        this hold covers cannot outlast the backend's memory of the ID:
        a copy this console can still re-carry always meets a backend
        that still remembers it. After a stale slot is dropped the take
        proceeds to the recorder's fresh window exactly as normal, and
        the recorder may owe nothing.
        """
        now = self._now()
        if self._carried is not None:
            if (self._carried_sent
                    and now - self._carried_at >= host_policy.CARRY_RETENTION_SECONDS
                    and (self._carried[2] is None
                         or host_policy.completed_windows_record_has(self._carried[2]))):
                # Sent, never resolved, and held past the whole span the
                # backend remembers the window's ID, with the record of
                # completed windows proving the one summary it owes was
                # paid, or no ID that could prove anything: a report
                # carrying the paid copy would hand it to a backend that
                # has pruned the ID and would fold it in as a window it
                # has never seen, logging a second line for lines a first
                # summary already paid, so the slot drops the copy before
                # any report can carry it, on the one retention span both
                # sides share. An unpaid copy never reaches this drop: it
                # keeps its place below, whatever its age, because no
                # summary of it was ever logged anywhere and the backend
                # that receives it next counts its lines exactly once.
                self._carried = None
            else:
                carried, self._carried = self._carried, None
                return carried
        carried = self._lines.take_carried_summary()
        if carried is not None:
            if self._mirrored_owner and self._mirrored is not None:
                # This console's own window is leaving in the report, to be
                # folded into the window the mirror stands for, and the
                # merged window is the backend's to report from here on:
                # the claim this console took on the mirror's identities,
                # every constituent the mirror counts, is returned with the
                # window, so the backend's own payment takes the merged one
                # whole instead of settling around a claim no window here
                # can pay anymore. The release is marked, because the
                # answer that comes back decides where the mirror's counts
                # live now: the backend holds them if it answers with the
                # same window, and this console owes them their one line if
                # it answers with another or none.
                host_policy.release_summary_claim(self._mirrored[3])
                self._mirrored_owner = False
                self._mirrored_released = True
            self._carried_at = self._now()
            self._carried_sent = False
        return carried

    def restore_carried_summary(self, carried, reached=False):
        """Hold a carried window back in the slot, after a delivery no
        backend acknowledged.

        ``reached`` says whether the attempt sent the request on an
        established connection, whatever answer came back or none at all.
        A window whose delivery reached the backend is marked sent,
        because the backend may have logged its summary before the answer
        was lost, so this console never emits it locally however long the
        outage runs. A window no delivery ever connected for keeps the
        sustained-outage emission ``take_summary`` below decides, because
        no request byte of it ever left for any backend and nothing could
        have summarized it. The copy waits in flight either way, never in
        this console's recorder, and the next report carries it again.

        A copy whose delivery reached a backend that readiness has since
        swapped for a fresh one is decided by the record of completed
        windows the two processes share, the same way a mirror's payment
        is: a copy the record names paid is dropped, because the old
        backend logged the one summary the window owes and the fresh
        generation holds no record that refuses a fold of it, and a copy
        the record names unpaid keeps its place, because the backend the
        delivery reached folded nothing and paid nothing it can vouch
        for, so the copy is the only remaining record of the lines the
        window counts and the next report carries it to the fresh backend,
        which owes its summary. A copy no delivery ever connected for
        keeps its place whatever generation is current, because nothing
        could have counted it anywhere.
        """
        if carried is None:
            return
        if reached:
            if (self._delivery_generation != self._ready_generation
                    and host_policy.completed_windows_record_has(carried[2])):
                # The backend this delivery reached has been swapped for a
                # fresh one while its answer was unresolved, and the shared
                # record of completed windows shows the one summary this
                # window owes was paid: the fold the delivery's arrival may
                # have made is gone with the backend that made it, but the
                # payment outlives it on the record, so a copy restored and
                # re-carried would hand paid lines to a backend that counts
                # them a second time, and the pair keeps the one summary
                # the old backend logged.
                return
            self._carried_sent = True
        self._carried = carried

    def take_summary(self):
        """The suppressed window this console may report, if due.

        Its own recorder answers as before: a window no report has carried
        is this console's to report once its hour has run, exactly as the
        outage always worked. A window a report has carried waits in the
        slot instead, and this console emits it only in the
        sustained-outage case: no acknowledgement of any kind has arrived
        since the carry began, so a full hour of reports reached no answer,
        the window's hour has fully run past with it, because it opened
        before the carry began, and no delivery for it ever reached the
        backend at the transport level, so no request byte of it arrived
        anywhere and nothing could have summarized it. An emission the
        pair's ceiling holds waits in the slot with its copy: the record
        of emission moments the two processes share names the hour the
        pair last paid a line, and the emission goes out once that hour
        has run, so two windows no acknowledgement ever carried between
        the layers still pay an hour apart.

        A window a delivery sent but no answer resolved is never emitted
        here however long the outage runs: the backend may have logged its
        summary before the answer was lost, so the pair leaves the window
        to the acknowledgement protocol, and the copy waits in the slot to
        be carried by every later report, discarded when an answer names
        its ID completed and folded by a backend that still counts it,
        and a backend that never answers loses the one summary rather
        than the pair logging a second line for a window the backend may
        already have summarized. The wait is bounded all the same: once
        the carry has run past the span the backend remembers the
        window's ID and the record of completed windows the two processes
        share proves the one summary the copy owes was paid, the copy is
        dropped outright, emitted nowhere and carried by no later report,
        on the one retention span both sides share so this hold cannot
        outlast what any backend remembers of the window. An unpaid copy
        is kept past that span as well, because no layer ever logged its
        summary and the backend that receives it next counts its lines
        exactly once, so the pair loses nothing a reconciliation could
        still deliver. Any acknowledgement since the
        carry began bars the emission as well and drops the copy once its
        hour has run, because the backend has taken the window in and
        holds it or has paid it, and the mirrored copy or the completed
        IDs carry its accounting, so the pair stays on the one summary
        whichever layer pays it.

        The recorder's own due window is paid with the backend's beside
        it: the window the last acknowledgement mirrored back overlaps the
        hour this console's window counts, the two windows' counts one
        summary owes under the hour's budget rules, and no report needs to
        go out for the two to meet, because the mirror is already here.
        The claim on the mirror's identity is taken as soon as the console
        holds the two, before either hour runs, and returned when its own
        window leaves in a report: the merged window the report builds is
        the backend's to report, so the backend's own timer meets the
        proof while this console holds the two, drops the copy it holds
        and logs nothing, and takes the merged one whole once it holds it
        alone. Once both windows' hours have run, the one line pays the
        combined count, opened at whichever window opened first, and the
        payment completes both windows' IDs on the record the two
        processes share, so the backend's own payment drops the copy it
        holds instead of logging a second line for counts the one line
        already paid. A mirror whose hour has not run holds the line back
        until it has, and a paid mirror joins nothing, because its
        counts are logged already; a mirror with no ID joins nothing
        either, because the backend that holds it could not be told the
        combined payment and would log its own second line. A console
        that holds no mirror pays its own window alone, and one that
        holds a paid mirror pays its own alone the same way, beside the
        line the backend already logged: the own window's counts were
        never folded into the mirror's payment, or the claim on their ID
        would have dropped the window, so the pair owes them their one
        summary from this console. One that holds no window of its own
        emits nothing here for a mirror: the mirror is the backend's
        window to pay, on its own arrivals and its own timer. A released
        mirror whose answer proved the backend holds nothing of it is a
        different hold, this console's own debt and the only remaining
        record of its counts, and that joins the recorder's own due
        window in the one line the hour's budget rules name for the
        counts both hold, at whichever of their hours runs last, paying
        alone only when the recorder holds nothing due of its own.
        """
        now = self._now()
        carried = self._carried
        if carried is not None and now - self._carried_at >= host_policy.HOUR_SECONDS:
            if self._carried_sent and (self._last_ack is None or self._last_ack < self._carried_at):
                if (now - self._carried_at >= host_policy.CARRY_RETENTION_SECONDS
                        and (carried[2] is None
                             or host_policy.completed_windows_record_has(carried[2]))):
                    # Sent, never answered, and carried past the whole span
                    # the backend remembers the window's ID, with the record
                    # of completed windows proving the one summary it owes
                    # was paid or no ID that could prove anything: a later
                    # report would re-carry the paid copy to a backend that
                    # folds it in as a window it has never seen and logs a
                    # second line, so the copy is dropped outright, emitted
                    # nowhere and carried by no later report, on the one
                    # retention span both sides share so this hold cannot
                    # outlast what any backend remembers of the window. An
                    # unpaid copy never reaches this drop: it keeps its
                    # place for the acknowledgement protocol, whatever its
                    # age, because no summary of it was ever logged
                    # anywhere and no backend ever paid it.
                    self._carried = None
                    return self._combined_summary(now)
                # Sent but never answered: the backend may have logged the
                # summary before its answer was lost, so the window stays in
                # the slot for the acknowledgement protocol and is never
                # emitted here, however long the outage runs.
                return self._combined_summary(now)
            if self._last_ack is None or self._last_ack < self._carried_at:
                stamp = host_policy.reserve_summary_emission(self._now)
                if stamp is None:
                    # The pair logged a summary line within the hour,
                    # whichever layer paid it, so the ceiling the two
                    # processes share holds this emission back with the
                    # copy it would pay: the hour's one emission slot is
                    # reserved in the same locked step as the decision,
                    # so a payment the other layer decided inside this
                    # very moment holds this one with it, no other layer
                    # can be told, and the next paced look emits it once
                    # the hour since the pair's last line has run.
                    return self._combined_summary(now)
                self._carried = None
                return carried[0], carried[1], stamp, carried[2]
            self._carried = None
        return self._combined_summary(now)

    def _combined_summary(self, now):
        """The recorder's own due window, paid with the mirrored one beside
        it, or alone.

        The two windows overlap in the hour they count, and the hour's
        budget rules name one summary for them, so the layer that holds
        both pays both as one line: the counts add, the earlier opening
        keeps its place, and the payment completes every identity the
        mirrored window counts on the record the two processes share, so
        the backend's own payment reads the proof on each of them and
        drops the copy it holds rather than log a second line for counts
        the one line already paid. The claim on the mirror's identities,
        every constituent the mirror counts, is taken the moment this
        console comes to hold both windows, before either hour runs:
        whichever timer of the pair reaches the mirror first, the
        decision is already this console's on every constituent alike,
        so the backend's flush meets the proof, drops the copy and logs
        nothing, and the console pays the one line the two windows owe at
        whichever of their hours runs last. The claim is a promise this
        early and not a payment's proof, and the promise travels with
        the decision, the window's own facts written beside it on the
        record as their own shape, so a console that restarts before the
        payment leaves the ownership the one that replaces it reads back
        and pays. The mirror's
        hour must still have run for the counts to join, so no line
        reports a window held back for its hour, and a mirror with no ID
        joins nothing, because the backend that holds it could not be
        told the combined payment.

        The one payment each window owes is claimed before any line is
        logged, on the record both processes share and under the lock
        the record's own updates hold, so the console's aggregate and the
        backend's timer cannot both pay the same window however closely
        their turns overlap. The claim this console takes is its own:
        the record's entry for it proves a payment this console owes, not
        one the backend logged, so the read-back above leaves the copy
        unmarked, a readiness keeps it, and the combined payment takes
        the decision as already made rather than claiming again. A
        backend whose flush claimed the identity before this console
        came to hold both windows settles the mirror alone, the copy
        dropped with the payment it stands for, and this console's own
        window pays beside it. The console's own window is never part of
        the mirror's payment, whichever timer wins: a fold that carried
        the window's counts into the backend's line would have claimed
        the window's ID on the record, and the take above claims that ID
        unopposed or drops the window it holds as already paid, so a
        window that survives its own take holds counts no line anywhere
        has named, refusals no report carried between the layers, and
        they owe this console their own summary beside the one the
        backend logged. A claim is returned when this console's own
        window leaves in a report and folds into the window the mirror
        stands for: the merged window is the backend's to report then,
        and its own payment takes the merged one whole. The release
        preserves the mirror's counts as well: the answer that follows
        either carries the same window back, and the backend keeps them,
        or leaves them this console's, and a released mirror whose
        answer proved the backend holds nothing of it waits here as this
        console's own debt, the only remaining record of the counts, and
        joins the one line this console's own due window pays at
        whichever of their hours runs last, paying alone only when the
        recorder holds nothing due of its own. A second debt joins the
        first, and the one line pays every identity both carried.
        The pair keeps the one summary a window owes, and the
        lose-not-double trade with it: a window the record names paid is
        dropped here rather than logged a second time, whatever this
        console's own hour says. The pair's ceiling of one summary an
        hour holds the lines here as well, on the record of emission
        moments the two processes share: the hour's one emission slot is
        reserved in the same locked step as the decision itself, so a
        line the pair paid within the hour holds this console's payment
        back until the hour has run, the claims a held payment took
        return with it, and every payment path asks the slot the same
        way, the whole claim and the settlement by identity alike. A
        mirror with no ID joins nothing
        either, because the backend that holds it could not be told the
        combined payment.
        """
        orphan = self._orphaned
        own = self._lines.hour_suppressed_window()
        if orphan is not None and host_policy.completed_windows_record_has(orphan[2]):
            # A payment reached the record under the debt's identity: the
            # counts are paid, wherever the line came from, and the debt
            # leaves with the proof.
            self._orphaned = None
            orphan = None
        mirror = (self.mirrored_summary()
                  if (own is not None or self._mirrored_owner
                      or orphan is not None) else None)
        if (mirror is not None and mirror[2] is not None
                and not self._mirrored_owner
                and host_policy.claim_summary_payment(
                    mirror[3], now, ownership=(mirror[0], mirror[1]))):
            # The two windows overlap in the hour they count and the one
            # summary the pair owes them is this console's, because this
            # console holds both: the claim on the mirror's identities,
            # every constituent the mirror counts, is taken here, before
            # either hour runs and before any line is logged, so whichever
            # timer of the pair reaches the mirror first, the backend's
            # own flush meets the proof on every constituent alike and
            # drops the copy it holds instead of logging a second line
            # beside the one this console owes. The claim is a promise
            # this early, not a payment's proof, and the promise travels
            # with the decision, the window's own facts written beside it
            # on the record as their own shape: a console that restarts
            # before the payment holds none of the memory this promise
            # lived in, and the one that replaces it reads the unpaid
            # ownership back and pays the debt, so the pair loses no
            # summary a claim already owns. A debt the release carved
            # stands for the second window when the recorder holds none of
            # its own: the debt's counts overlap the mirror's the same way
            # in the hour they count, so the one line the hour allows pays
            # them together, and the console takes the decision on the
            # mirror's identities before either hour runs.
            self._mirrored_owner = True
        if own is None:
            # Nothing this console's own recorder holds is due, so nothing
            # of its own combines here: the window the acknowledgement
            # mirrored is the backend's to pay, on its own arrivals and
            # its own timer, and a claim this console returned with its
            # own window's carry leaves the merged window the backend's
            # to pay whole. A mirror this console claimed for a debt
            # beside it is the exception: the two windows' counts overlap
            # in the hour they count, so the hour's budget rules name one
            # summary for them and the payment is this console's, one
            # line for the debt's counts beside the mirror's at whichever
            # of their hours runs last. A debt with no claimed mirror is
            # the only thing the hour holds of this console's, and it
            # pays alone once its hour has run, claiming every identity
            # the released mirror carried, and the payment completes them
            # so whichever layer meets the proof drops its copy. The
            # unpaid ownership a lost console left on the shared record
            # is a debt of this console's as well, read back at the
            # start, and it pays the same way, alone once its hour has
            # run: the claim the lost console took already owns the one
            # decision on the window, so the payment completes the
            # identities on the record rather than claiming them again.
            if (self._recovered is not None
                    and now - self._recovered[1] >= host_policy.HOUR_SECONDS):
                stamp = host_policy.reserve_summary_emission(self._now)
                if stamp is not None:
                    # The unpaid ownership the shared record held for this
                    # console has come due with the restart behind it: the
                    # claim the lost console took on the mirror's identities
                    # reads as paid to the backend that still holds the
                    # window, so the pair's one summary for the counts is
                    # this console's debt to pay, and the payment completes
                    # the identities on the record, taking the payment's
                    # stamp the way every completed window's does, so the
                    # backend's own flush reads the proof on the copy it
                    # holds and drops it rather than logging a second line.
                    # The stamp the reservation captured under the lock is
                    # the moment the payment records and the line carries,
                    # so a debt that waited behind another process's
                    # update pays inside the hour the pair's newest line
                    # opened.
                    debt = self._recovered
                    self._recovered = None
                    host_policy.record_completed_windows(debt[3], stamp)
                    return debt[0], debt[1], stamp, debt[2]
            if orphan is not None and self._mirrored_owner and mirror is not None:
                if (now - mirror[1] < host_policy.HOUR_SECONDS
                        or now - orphan[1] < host_policy.HOUR_SECONDS):
                    # One of the two windows the one line joins is still
                    # inside its hour: the line waits, and the worker's
                    # next paced look pays what has come due by then.
                    return None
                paid = host_policy.claim_summary_payment(orphan[3], now)
                stamp = host_policy.reserve_summary_emission(self._now)
                if stamp is None:
                    # The pair logged a summary line within the hour, so
                    # the ceiling the two processes share holds the line
                    # back: the hour's one emission slot is reserved in
                    # the same locked step as the decision, so a payment
                    # the other layer decided inside this very moment
                    # holds this one with it. The claim the debt's
                    # payment took returns with the hold, because the
                    # decision the record keeps is the payment and not
                    # the wait, the counts wait with the debt, and the
                    # worker's next paced look pays what has come due by
                    # then.
                    if paid:
                        host_policy.release_summary_claim(orphan[3])
                    return None
                self._orphaned = None
                if paid:
                    for window_id in orphan[3]:
                        self._lines.complete_window(window_id)
                for mirror_id in mirror[3]:
                    # The one line pays every constituent the mirror
                    # counts, and the payment completes each of them: the
                    # backend's own flush reads the proof on any copy it
                    # still holds, and a backend that lost the window is
                    # told nothing owed, on any of the identities the
                    # answer carried.
                    self._lines.complete_window(mirror_id)
                self._mirrored = None
                self._mirrored_paid = False
                self._mirrored_owner = False
                self._mirrored_released = False
                if paid:
                    return (mirror[0] + orphan[0],
                            min(mirror[1], orphan[1]), stamp, mirror[2])
                # The claim on the debt met another payment's proof: the
                # debt's counts are paid elsewhere, and the line pays the
                # claimed mirror's alone.
                return mirror[0], mirror[1], stamp, mirror[2]
            if (orphan is not None
                    and now - orphan[1] >= host_policy.HOUR_SECONDS):
                # The debt's hour has run, so the one summary the debt's
                # counts owe is this console's to pay: the claim on its
                # identities and the pair's one emission slot are both
                # taken with the decision, and whichever of them another
                # payment holds, the counts wait and the next paced look
                # pays them.
                self._orphaned = None
                if host_policy.claim_summary_payment(orphan[3], now):
                    stamp = host_policy.reserve_summary_emission(self._now)
                    if stamp is None:
                        # The pair logged a summary line within the hour,
                        # so the ceiling the two processes share holds the
                        # debt's payment back: the hour's one emission
                        # slot is reserved in the same locked step as the
                        # decision, so a payment the other layer decided
                        # inside this very moment holds this one with it.
                        # The claim returns with the hold, the debt keeps
                        # its counts, and the worker's next paced look
                        # pays what has come due by then.
                        host_policy.release_summary_claim(orphan[3])
                        self._orphaned = orphan
                        return None
                    for window_id in orphan[3]:
                        self._lines.complete_window(window_id)
                    return orphan[0], orphan[1], stamp, orphan[2]
                # The claim met another payment's proof: the counts are
                # paid elsewhere, and nothing is owed here.
            return self._lines.take_summary()
        if now - own[1] < host_policy.HOUR_SECONDS:
            # Nothing this console's own recorder holds is due, so nothing
            # combines here yet: the two windows pay as one line once the
            # own window's hour has run, and the worker's next paced look
            # pays what has come due by then.
            return self._lines.take_summary()
        if mirror is not None and now - mirror[1] < host_policy.HOUR_SECONDS:
            # The console's own window is due and the backend's mirrored
            # one is not: the two pay as one line, so the line waits for
            # the mirror's hour, and the worker's next paced look pays
            # what has come due by then.
            return None
        if orphan is not None and now - orphan[1] < host_policy.HOUR_SECONDS:
            # The console's own window is due and the debt's is not: the
            # hour's budget rules name one summary for the counts both
            # hold, so the one line waits for the debt's hour and pays
            # them together, and the worker's next paced look pays what
            # has come due by then.
            return None
        summary = self._lines.take_summary()
        if summary is None:
            return None
        count, opened, closed, window_id = summary
        if mirror is not None and mirror[2] is not None:
            if self._mirrored_paid:
                # The shared record proves the backend paid this window while
                # this console held the copy, and the one summary it owes is
                # logged there already: the copy is resolved here rather than
                # carried again. The counts this console's own window holds
                # are none of them: the fold that would have carried the
                # window into the backend's payment claims its ID on the
                # record, and the take above has just claimed that ID
                # unopposed, so no line anywhere has counted these refusals
                # and they owe this console their own summary beside the one
                # the backend logged. The pair keeps the one summary a
                # window owes, not one per hour: the two windows count
                # refusals no report carried between them, and the mirror's
                # payment settles the mirror's alone.
                self._mirrored = None
                self._mirrored_paid = False
                self._mirrored_released = False
            elif (not self._mirrored_owner
                    and not host_policy.claim_summary_payment(mirror[3], closed)):
                # The backend's own timer claimed the payment of the mirrored
                # window inside this very decision, so the one summary the
                # mirror owes is logged there. This console's own window is
                # not part of that payment, and the take above proved it the
                # same way: the fold that would have carried its counts into
                # the backend's line claims their ID, so a window that
                # survives its own take holds counts no line ever named, and
                # they pay here as their own summary, whichever timer reached
                # the mirror first. The claim is the pair's one payment
                # decision on each window, and the copy is dropped with the
                # payment it stands for.
                self._mirrored = None
                self._mirrored_paid = False
                self._mirrored_released = False
            else:
                # The one line pays every constituent the mirror counts, and
                # the payment completes each of them: the backend's own flush
                # reads the proof on any copy it still holds, and a backend
                # that lost the window is told nothing owed, on any of the
                # identities the answer carried.
                count += mirror[0]
                opened = min(opened, mirror[1])
                for mirror_id in mirror[3]:
                    self._lines.complete_window(mirror_id)
                self._mirrored = None
                self._mirrored_paid = False
                self._mirrored_owner = False
                self._mirrored_released = False
        if orphan is not None:
            # The debt joins the one line the hour's budget rules name for
            # the counts it holds beside the console's own: the counts add,
            # the earlier opening keeps its place, and the payment
            # completes every identity the released mirror carried, so
            # whichever layer meets the proof drops its copy. A claim that
            # meets another payment's proof leaves the debt paid elsewhere,
            # and the line pays the counts it holds of its own.
            if host_policy.claim_summary_payment(orphan[3], closed):
                count += orphan[0]
                opened = min(opened, orphan[1])
                for window_id in orphan[3]:
                    self._lines.complete_window(window_id)
            self._orphaned = None
        return count, opened, closed, window_id

    def report_due(self, host):
        """Queue the hostname for the delivery worker, or leave it alone.

        True when the hostname is newly waiting to be told: one acknowledged
        within the hour is done until the hour runs, and one already waiting
        is the worker's to deliver. A hostname refused again while it waits
        moves to the end of the queue, the newest place, so the eviction
        drops the oldest waiting and a frequently refused hostname is not
        pushed out by names that arrived after its last refusal.

        A hostname the hour has already acknowledged is skipped by its
        reports, so no backend will count a repeat of it: while the
        backend answers, the repeat is counted into this console's own
        window here, one suppressed line each, because the pair owes the
        repeats one summary whichever recorder holds them and the line
        the pair spent on the hostname this hour is already spent. While
        the backend cannot be told, the refusal path has counted the
        repeat already, and nothing is counted here.

        The pending
        set holds the REFUSED_HOSTS_LIMIT newest of them, the same number the
        backend's record keeps, so a longer roll of hostnames cannot grow a
        longer queue: the ones the eviction drops are the oldest waiting,
        the ones the backend's record has dropped too. The eviction resolves
        the notices it drops, their first-failure accounting with them: the
        accounting lives exactly as long as the notice, so a fresh notice
        of an evicted host counts its own first failure anew. The eviction
        counts the refusal of a notice it drops only when no count of that
        refusal is held already: the outage's refusal path counted a notice
        it queued while the backend could not be told, and a failed delivery
        named its notice's refusal at its failure, so the eviction counts
        neither of those a second time. An eviction
        that catches the notice whose delivery is still in flight is
        remembered for the failure that delivery will eventually report,
        so the mark the failure counts resolves with the notice it was
        counted for, and not with whatever fresh notice of the same
        hostname the queue may hold by then. The eviction resolves the
        notice's place in the queue and not the delivery itself, so the
        accounting the delivery still carries keeps its place too: the
        failure or the acknowledgement that ends the delivery resolves
        it, and a retry the backend never processed reads it and counts
        the refusal nowhere again, one refusal owing the one count the
        first failure made wherever its report carried it. The notice
        keeps the time of the refusal it reports on as well, refreshed by
        every refusal while it waits: the newest-refused set is bounded and
        a roll of newer names rolls a waiting hostname out of it, and the
        line the console spent for it is kept for its hour alone, so the
        notice's own stamp is what its report carries when neither of the
        others survives to the delivery. The eviction resolves the notice
        on the record of notices the pair shares instead, its lifecycle
        state written under the lock the record's updates hold, and the
        notices a delivery carried alone: an outcome
        that arrives after its notice was evicted is a fact about a notice
        nothing will deliver again, and the state refuses it, so no
        orphaned outcome of a resolved notice stays on the record and the
        accounting bound reads no resolved notice as one still waiting,
        while a notice the worker never took for a delivery left no
        request byte any backend could be holding, so it is resolved
        nowhere and a burst of never-sent evictions writes no record at
        all.

        Every refusal refreshes the newest-refused set first, whatever the
        queue decides, because the skipped and the waiting names are still
        the newest news about the refusals this console saw, and the
        reports carry the set to the backend's record. A notice is an
        identity as well, minted at its queue entry and resolved only by
        the acknowledgement of the delivery that carried it: the queue can
        evict a notice while its delivery waits, and a fresh refusal of the
        same hostname queues a new notice with a new identity, so the
        acknowledgement of the old delivery resolves the notice it
        answered alone and never the fresh one whose refusal the live
        backend was never told about.
        """
        now = self._now()
        self._prune_reports(now)
        self._note_recent(host, now)
        if host in self._last_report:
            if BACKEND_PORT is not None:
                # No report goes out for a hostname the hour has already
                # acknowledged, so no backend will count this refusal: this
                # console is the pair's only recorder for it, and it counts
                # the repeat into its own window the way the outage's
                # held-back lines are counted, while the per-host pace of
                # the line the backend logged, or the hour's shared ceiling
                # once that line was held back, holds the repeat back as
                # one more suppressed line, whose summary the pair owes
                # exactly once. A repeat that arrives once the pace has run,
                # while the report cooldown still holds it back, is granted
                # its line, and the console writes it: the refusal reached
                # no backend and no summary counts it, so the line is
                # written by the only layer that can rather than spent on
                # a slot that neither layer ever names.
                if self.note_line(host):
                    _print_refusal_line(_refusal_line_text(host))
            return False
        if host in self._pending:
            # The repeat coalesces into the notice already waiting, so no
            # delivery will ever tell any recorder about it: while the
            # backend answers, this console is the pair's only recorder for
            # it, and it counts the refusal into its own window at the
            # refusal itself, exactly once, the way an evicted notice's is
            # counted below. While the backend cannot be told, the refusal
            # path has counted the repeat already, and nothing is counted
            # here.
            self._pending.move_to_end(host)
            self._pending_at[host] = now
            if BACKEND_PORT is not None:
                self._lines.note_unreported()
            return False
        self._pending[host] = None
        self._pending_at[host] = now
        notice_id = host_policy.mint_notice_id()
        self._pending_ids[host] = notice_id
        if BACKEND_PORT is None:
            # The backend cannot be told, so the refusal path counted this
            # refusal already, in a line or in the window its held-back
            # lines count into: the notice owes no count of its own, and an
            # eviction after the recovery counts nothing for it.
            self._accounted_notices.add(notice_id)
        elif self.line_time(host) is not None:
            # The hour's line budget for this hostname is spent, by the
            # line a failed delivery of an earlier notice named, by a line
            # this console wrote itself or by the line the backend logged:
            # the report this fresh notice carries tells the backend to
            # fold, so no layer will ever write the fresh refusal a line,
            # and this console is the pair's only count for it. It counts
            # the refusal into its own window at the queueing, exactly
            # once, the way the repeats and the evictions of held-back
            # notices are counted, and the mark that holds the count is
            # the notice's first-failure mark: a retry of the notice that
            # later fails counts nothing beside it, and an eviction after
            # it counts nothing either. A fresh refusal that arrives once
            # the pace has run carries no spent line and gets its own.
            self._accounted_notices.add(notice_id)
            self._lines.note_held_back(host)
        while len(self._pending) > host_policy.REFUSED_HOSTS_LIMIT:
            dropped, _attempt = self._pending.popitem(last=False)
            self._pending_at.pop(dropped, None)
            dropped_id = self._pending_ids.pop(dropped, None)
            in_flight_drop = dropped == self._in_flight
            if in_flight_drop:
                # The notice whose delivery is in flight is among the ones
                # this eviction drops: its resolution has to reach the
                # failure that delivery will eventually report, so the
                # failure clears the mark it counts instead of leaving it
                # for a notice nothing will ever resolve. The delivery
                # still owns the accounting of the refusal it carries,
                # whichever way this console counted it: the eviction
                # resolves the notice's place in the queue and not the
                # delivery itself, and the failure or the acknowledgement
                # that ends the delivery resolves the count with it, so
                # the marks the notice holds keep their places here and a
                # retry the backend never processed reads them and counts
                # the refusal nowhere again.
                self._in_flight_evicted = True
            elif (BACKEND_PORT is not None
                    and dropped_id not in self._accounted_notices
                    and self.line_time(dropped) is None
                    and not self._lines.first_failure_is_counted(dropped)):
                # The evicted notice will never be delivered, so no backend
                # will count the refusal it carries: while the backend
                # answers, this console is the pair's only recorder for
                # that refusal, and it counts the notice's one refusal into
                # its own window at the eviction, exactly once, so the
                # queue's bound loses no refusal. A notice this console
                # already counted is owed nothing here: the outage's
                # refusal path counted it while the backend could not be
                # told, and a failed delivery named it at its failure, so
                # the eviction counts neither of those a second time.
                # While the backend cannot be told, the refusal path
                # counted the notice already, and nothing is counted here.
                # The notice in flight is left to its delivery: the
                # backend counts its refusal when the report lands, and
                # the failure names it here when it does not.
                self._lines.note_unreported()
            if (dropped_id is not None
                    and (dropped_id in self._attempted_notices
                         or _attempt is not None)):
                # The notice is resolved on the record of notices the pair
                # shares, its lifecycle state written under the lock the
                # record's updates hold: the facts of its delivery leave
                # with the resolution, and a write that lags behind the
                # eviction, the receipt or the outcome of a backend whose
                # answer a timeout already ended, finds the state and
                # lands nothing, so no orphaned outcome of a notice
                # nothing will ever deliver again stays on the record. A
                # notice the worker took for a delivery is a fact of its
                # life a readiness cannot erase: the retry markers are
                # reset to make every waiting notice due at once for a
                # fresh backend, while the request bytes of the attempt
                # were already spent telling the old one, so the take
                # marks the notice as attempted beside the retry marker,
                # and an eviction that follows a readiness still resolves
                # it. A
                # notice the worker never took for a delivery is resolved
                # nowhere, because no request byte of it ever left for any
                # backend and nothing can be holding its facts in flight:
                # a burst of fresh hostnames that rolls a thousand such
                # notices out leaves the record empty rather than a
                # thousand resolutions deep, and the resolutions a
                # delivery did carry stay bounded on the span the pair's
                # own bound keeps.
                host_policy.trim_notice_record(dropped_id)
            if dropped_id is not None and not in_flight_drop:
                # The identity's own mark resolves with it, so a
                # fresh notice of the same hostname starts with a mark
                # of its own. A notice still in delivery keeps its
                # mark: the delivery owns it and resolves it.
                self._accounted_notices.discard(dropped_id)
                self._attempted_notices.discard(dropped_id)
            if not in_flight_drop:
                self._lines.first_failure_resolved(dropped)
        self._wake.set()
        return True

    def take_due_report(self):
        """The next hostname the worker may tell the backend about, or None.

        A hostname never attempted is taken before any retry, the oldest
        waiting among them, and the retries that remain are taken least
        recently attempted first, once their pace has run: a failed
        delivery leaves its notice pending, and a take that always
        reached for the oldest waiting entry would ask the same failing
        notice again and again while a later one nobody has attempted
        waits behind every retry of it, for as long as the backend keeps
        refusing, so the take prefers work nobody has tried and falls
        back to the attempt that has waited longest, and one
        persistently failing notice holds no later notice behind its own
        retry pace. The queue's own order is untouched by the preference,
        so the eviction still drops the oldest waiting refusal first and
        the refusals keep their recency for the bound the queue keeps.
        Taking one only marks the attempt: the
        hour's global reporting bound is spent by acknowledged deliveries
        alone, and a delivery that failed holds every later attempt back
        for one pace, so a failing backend is asked once per pace and not
        in a stream, and cannot spend the hour instead of the recovery.
        The take also remembers the notice the delivery belongs to: the
        pending queue can evict the hostname while the delivery awaits
        its response, and the failure that eventually arrives must resolve
        the accounting of the notice it was taken for, and not of whatever
        fresh notice of the same hostname the queue may hold by then.
        The take remembers the backend generation as well: a readiness
        that swears in a fresh backend while this delivery's answer is
        unresolved leaves it speaking for the old one, and the payment
        its reach could report is left with that backend rather than
        carried to the fresh generation on the mirror it could vouch
        for. The take remembers the identity of the notice it delivers:
        the queue can evict the notice and a fresh refusal can queue a
        new one while the answer is unresolved, so the acknowledgement
        that eventually arrives is matched against the notice that was
        really sent and resolves no notice it did not answer. The take
        marks the notice as attempted beside the retry marker it sets:
        the markers are reset when a readiness swears in a fresh backend,
        so the queue holds every waiting notice due at once, but the
        request bytes of an attempt were spent telling the old backend
        however the readiness makes the queue read, and the eviction that
        drops an attempted notice after a readiness resolves it on the
        record, so its late outcome lands nowhere.
        """
        now = self._now()
        self._prune_reports(now)
        if len(self._report_times) >= REPORTS_PER_HOUR:
            return None
        if self._last_failure is not None and now - self._last_failure < RETRY_PACE_SECONDS:
            return None
        for host, last_try in self._pending.items():
            if last_try is None:
                self._pending[host] = now
                self._in_flight = host
                self._in_flight_evicted = False
                self._in_flight_id = self._pending_ids.get(host)
                self._delivery_generation = self._ready_generation
                if self._in_flight_id is not None:
                    self._attempted_notices.add(self._in_flight_id)
                return host
        chosen = None
        chosen_try = None
        for host, last_try in self._pending.items():
            if last_try is None or now - last_try < RETRY_PACE_SECONDS:
                continue
            if chosen_try is None or last_try < chosen_try:
                chosen, chosen_try = host, last_try
        if chosen is None:
            return None
        self._pending[chosen] = now
        self._in_flight = chosen
        self._in_flight_evicted = False
        self._in_flight_id = self._pending_ids.get(chosen)
        self._delivery_generation = self._ready_generation
        if self._in_flight_id is not None:
            self._attempted_notices.add(self._in_flight_id)
        return chosen

    def retry_delay(self):
        """How long the worker may wait before another look, nothing due.

        Never less than MIN_RETRY_WAIT_SECONDS, so a wait that computes to
        zero can never turn the worker into a busy spin. While the hour's
        bound is spent, the next delivery can only happen once the oldest
        acknowledged report rolls out of the hour, so the paces are not
        consulted: a hostname past its pace cannot be taken while the bound
        holds, and its overdue pace would otherwise read as no wait at all.
        """
        now = self._now()
        self._prune_reports(now)
        if len(self._report_times) >= REPORTS_PER_HOUR:
            return max(MIN_RETRY_WAIT_SECONDS,
                       self._report_times[0] + host_policy.HOUR_SECONDS - now)
        waits = [RETRY_PACE_SECONDS - (now - last_try)
                 for last_try in self._pending.values() if last_try is not None]
        wait = min(waits) if waits else RETRY_PACE_SECONDS
        if self._last_failure is not None:
            # A failed delivery holds every attempt back for a pace, so the
            # worker does not hammer a failing backend.
            wait = max(wait, RETRY_PACE_SECONDS - (now - self._last_failure))
        return max(MIN_RETRY_WAIT_SECONDS, min(wait, RETRY_PACE_SECONDS))

    def report_sent(self, host, notice_id=None):
        """The backend acknowledged the report; it is not pending any more.

        The cooldown is kept by time alone, never by a count: however many
        hostnames are acknowledged around it, a told one stays told for its
        full hour. The acknowledgement is also what spends the hour's global
        reporting bound and lifts the failure backoff, so a backend that
        refuses or drops reports cannot spend the hour instead of the
        recovery. The notice's first-failure accounting resolves with it as
        well: the accounting lives exactly as long as the notice, so a
        fresh notice of the same host counts its own first failure anew.
        The delivery that carried the notice is resolved with it too, so
        an eviction that happened while it waited in flight is forgotten
        rather than held against a later notice of the same host. The
        identity the answer resolves also leaves the record of notices
        the pair shares, trimmed here, so the backend's bounded
        accounting of notices reads the notice as resolved and drops its
        mark before the mark of any notice still waiting, however many
        acknowledged notices arrive around one that is.

        The acknowledgement resolves the notice it answers and nothing
        else: the queue can evict the notice whose delivery waited and a
        fresh refusal of the same hostname can queue a new one, and the
        identity the delivery carried tells the two apart. A mismatched
        answer resolves the delivery and the notice it answered alone,
        spending the bound a real
        acknowledged delivery spent and leaving the fresh notice its own
        place, its own first failure and its own delivery, because the
        live backend was never told about the fresh refusal and no
        cooldown of the old one may hold its report back for an hour. With
        no notice waiting under the hostname, the acknowledgement is the
        hostname's own: the notice it answered is gone and nothing it
        could resolve is left, so the hostname is told for its hour.
        """
        now = self._now()
        self._report_times.append(now)
        self._last_failure = None
        if self._in_flight == host:
            self._in_flight = None
            self._in_flight_evicted = False
            self._in_flight_id = None
        if notice_id is not None:
            # The answer resolved the notice it answered, whichever notice
            # the queue holds now, so its identity leaves the record of
            # notices the pair shares: the backend's bounded accounting of
            # notices then reads it as resolved and drops its mark before
            # the mark of any notice still waiting, however many
            # acknowledged notices arrive around one that is. The
            # attempt is a fact of the notice's life the same way, and it
            # leaves with the resolution, because the queue holds the
            # notice no longer and no eviction can reach it.
            host_policy.trim_notice_record(notice_id)
            self._attempted_notices.discard(notice_id)
        if (notice_id is not None and host in self._pending
                and self._pending_ids.get(host) != notice_id):
            # The queue holds a fresh notice of this hostname, one the
            # answer never named, because it answered the delivery of a
            # notice the queue evicted while it waited: the fresh notice
            # keeps its place and is due at once, and the hostname's
            # cooldown is not started by an acknowledgement of a refusal
            # the backend heard an hour's worth of news before. The
            # answered notice resolves with its delivery all the same,
            # its counted identity leaving with it the way the attempt
            # above did, so no mark of a notice nothing will ever answer
            # again holds a fresh notice of the same hostname to a count
            # it never made.
            self._accounted_notices.discard(notice_id)
            self._wake.set()
            return
        self._last_report[host] = now
        self._pending.pop(host, None)
        self._pending_at.pop(host, None)
        self._pending_ids.pop(host, None)
        if notice_id is not None:
            # The acknowledgement resolves the notice it answered, and
            # the counted mark the notice holds leaves with it: the queue
            # can hold a fresh notice of the same hostname in a moment,
            # and its refusal is counted on its own terms.
            self._accounted_notices.discard(notice_id)
        self._lines.first_failure_resolved(host)

    def report_failed(self, host, notice_id=None):
        """The backend did not take the report: it stays pending, pace
        permitting, and this console names the hostname, because from this
        moment this console is the only place that does until it lands. The
        failure also holds every later attempt back for one pace, and the
        line below asks its pace without counting a held-back line as
        suppressed, so an hour of failing retries does not add up to a
        summary that says a stream of refusals was held back.

        A failure whose notice the record of notices proves the backend
        accounted is not that: the refusal the notice carried was named or
        merged there, whichever its own pace and cap decided, and this
        console does not name the hostname again from a failure whose
        answer was merely lost. The assignment the outcome records
        survives the notice's own resolution as well, so an eviction
        that caught the notice after its backend named the refusal holds
        this console to the same nothing. A failure whose notice this
        console
        counted already is held to the same nothing: the outage's refusal
        path counted the refusal into the window the notice's report
        carries, and an earlier failure of the same notice named it in a
        line of its own, so the pair holds the refusal's one naming in
        the count the console made and a fresh failure adds nothing
        beside it. The receipt alone proves only that the
        backend took the notice in, before it processed anything, and a
        backend that died between its receipt and its record leaves the
        refusal named nowhere: a notice the record shows taken in but
        never accounted, and counted nowhere by the console either, is
        named here, the only other layer that saw it.
        The notice keeps its place in both cases, because the backend that
        took it in owes the acknowledgement the loss ate, and the retry
        reaches it and its own accounting holds the hostname from a
        second line. The naming a failure would deliver is claimed before
        any of it is delivered, on the record the pair shares, under the
        same lock the backend's own accounting claims its naming: a
        backend that took the report in before the client gave up on its
        answer can still be holding its processing, and a failure whose
        claim that late accounting beat names nothing here at all, no
        line and no held-back count beside the one the backend delivered,
        because the one naming the pair keeps is whichever channel
        reached the refusal first and the console's own accounting is
        owed nothing beside it. A claim the record refuses with no live
        accounting standing behind it holds nothing either, because the
        resolution a notice's own eviction wrote is the claim's cause and
        not an assignment: the console is the only layer that saw the
        refusal, and the failure names it in its own right with the
        notice's marks resolving beside it. The claim that wins carries
        the naming, and the late accounting that follows it meets the
        claim and names the refusal nowhere a second time. A notice this
        console named in the line of its first failure holds its retries
        to that one naming wherever the hour has
        rolled to, because the notice is unchanged and its own lifetime,
        the acknowledgement or the eviction that resolves it, is the
        boundary the pair keeps, past the hour the hostname's pace rolls
        on.

        The failure resolves the notice it was taken for, the one whose
        delivery has just come back, rather than whatever the queue holds
        now: the pending queue can evict a notice while its delivery waits
        for its response, and a mark counted for that evicted notice would
        be a mark nothing will ever resolve, because the hostname's next
        refusal arrives as a fresh notice. The failure of an evicted notice
        still counts its own line, the news the notice carried, but the
        mark it counts resolves together with it: the mark lives exactly
        as long as the notice, and the notice is already gone, so the mark
        a later notice of the same hostname counts is its own and a
        genuine first failure still reads as one. Both exits the failure
        can take resolve the marks, the one that counts its own line and
        the one that finds the refusal counted already: a counted notice
        whose delivery the queue evicted would otherwise hold marks
        nothing will ever resolve, and the fresh notice of the same
        hostname that follows would read them as its own refusal already
        counted, adding nothing to the window while being marked counted
        itself. The eviction resolved
        the notice on the record the pair shares, under the lock its
        updates hold, so the writes a backend whose answer a timeout ended
        would land behind the eviction find the resolution there and
        write nothing: no late outcome of a notice nothing will ever
        deliver again reaches the record, no late accounting of one names
        its refusal a second time, and the failure holds the
        console to the one count it made rather than reconciling an
        outcome the record refused. Every exit the failure can take
        resolves the marks, the one that counts its own line, the one
        that finds the refusal counted already and the one whose claim
        the backend's late accounting beat alike.
        """
        self._last_failure = self._now()
        if host in self._pending:
            self._pending[host] = self._now()
        evicted = self._in_flight == host and self._in_flight_evicted
        if self._in_flight == host:
            self._in_flight = None
            self._in_flight_evicted = False
            self._in_flight_id = None
        if (notice_id is not None
                and (notice_id in self._accounted_notices
                     or host_policy.notice_named_elsewhere(notice_id))):
            # The refusal this notice carries is counted already: the
            # backend completed the notice's accounting before the
            # exchange failed and named or merged the refusal there, or
            # this console counted it at the refusal that queued the
            # notice, in a line it wrote or in the window its held-back
            # lines count into, or an earlier failure of the same notice
            # named it in a line. The backend's assignment of the naming
            # outlives the notice's own resolution, because an eviction
            # that ends the notice's life while the backend's naming
            # stands keeps the assignment on the record, and the pair
            # owes the refusal one naming whichever channel reached it
            # first, so the failure asks the record for the assignment
            # and not for the notice's own state alone. The failure
            # counts nothing here: no
            # line is written, no slot of the hour is spent and no count
            # joins a window, because the pair names the refusal once,
            # whichever channel reached it first, and the count it holds
            # is already made. The notice is owed
            # no eviction count either, and a fresh notice the queue holds
            # under the hostname is owed everything of its own. The receipt
            # alone would say less: a backend that took the notice in and
            # died before it recorded the refusal named it nowhere, so the
            # fallback below accounts it here. A notice this failure was
            # taken for and the queue already evicted is resolved here
            # with its marks, the same resolution the failure's own count
            # takes below: nothing will ever resolve the marks it holds
            # otherwise, and the fresh notice of the same hostname that
            # follows is owed its own first failure anew.
            if not evicted and host in self._pending:
                self._accounted_notices.add(notice_id)
            if evicted:
                self._lines.first_failure_resolved(host)
                self._accounted_notices.discard(notice_id)
                self._attempted_notices.discard(notice_id)
            return
        if (notice_id is not None
                and not host_policy.record_notice_accounted(
                    notice_id, self._now(),
                    claimant=host_policy.NOTICE_ACCOUNTED_BY_CONSOLE)
                and host_policy.notice_accounted_has(notice_id)):
            # The backend's own accounting won the naming while this
            # failure was deciding its line: the late processing of a
            # report the client gave up on has landed on the record under
            # the lock, and the refusal keeps the one naming it owes, the
            # naming the backend delivered. The failure owes nothing
            # beside it: no line is written, no slot of the hour is spent
            # and no held-back count joins a window, and the notice
            # carries no account of its own. The marks of an evicted
            # notice resolve together with it, the same resolution the
            # counted exits take, so the fresh notice of the same hostname
            # that follows counts its own first failure anew. A claim the
            # record refuses with no live accounting standing behind it,
            # the resolution a resolution of the notice already wrote,
            # holds nothing: the queue's own eviction ended the notice's
            # life before this failure, and the console is the only layer
            # that saw the refusal, so the failure names it below the way
            # its own eviction resolves its marks with it.
            if evicted:
                self._lines.first_failure_resolved(host)
                self._accounted_notices.discard(notice_id)
                self._attempted_notices.discard(notice_id)
            return
        counted = self._lines.note_retry(host, notice_id)
        if counted:
            # The hour named this refusal in a line of this console's
            # own, so the line is stamped for the reports the retries
            # carry and the backend folds it: a line no layer wrote is
            # stamped nowhere, so a notice whose line the hour's pace or
            # cap held back carries no time it never spent, and the
            # recovery names its refusal in its own right instead of
            # folding a line that exists nowhere.
            self._account_line(host, notice_id)
            _print_refusal_line(_refusal_line_text(host))
        if (not evicted and host in self._pending and notice_id is not None
                and (counted or self._lines.first_failure_is_counted(host))):
            # The failure named this notice's refusal here, in the line it
            # wrote or in the first failure the hour's cap held back, or an
            # earlier failure of the same notice already did, so the
            # refusal is counted and an eviction after the failure counts
            # nothing for it a second time. The claim above already holds
            # the naming on the record the pair shares, so the late
            # processing of a backend that took the report in before the
            # client gave up on its answer meets the claim and writes no
            # line and no count of its own, and the refusal keeps the one
            # naming it owes, whichever channel reached it first.
            self._accounted_notices.add(notice_id)
        if evicted:
            # The notice this failure was taken for has already left the
            # queue, so nothing will ever resolve the marks it holds: the
            # failure still counts the notice's own line, the news it
            # carried, and the marks resolve here together with the
            # notice, leaving the next notice of the same hostname to
            # count its own first failure anew.
            self._lines.first_failure_resolved(host)
            if notice_id is not None:
                self._accounted_notices.discard(notice_id)
                self._attempted_notices.discard(notice_id)

    def delivery_spoke_for_the_live_backend(self):
        """Whether the delivery the worker took still speaks for the backend
        that answers now.

        The take remembered the generation it was taken in, and a readiness
        that swears in a fresh backend while the delivery's answer is
        unresolved leaves it speaking for the old one: whatever its answer
        says once it arrives, it counts for the backend that gave it alone.
        """
        return self._delivery_generation == self._ready_generation

    def report_requeued_for_the_fresh_backend(self, host, notice_id=None):
        """Resolve a delivery the swapped-out backend acknowledged, without
        telling the fresh one anything.

        The answer came from a generation readiness has since sworn out, so
        the backend that would have counted the report is gone and the one
        that answers now was told nothing: the notice keeps its place and is
        due at once, the way readiness makes everything due, and neither the
        hour's bound nor the cooldown is spent, because the fresh backend
        spends them when its own acknowledgement lands. The notice's
        first-failure mark is left as it is in that case, and so are the
        identities the delivery carried: the notice is still the same
        notice, waiting for the backend that never received it. A delivery
        whose notice the queue no longer holds, evicted while it waited or
        replaced under its hostname by a fresh one, resolves the notice here
        the way the failure and the acknowledgement of a gone notice do:
        the identities leave with it, so no mark of a notice nothing will
        ever answer again holds a fresh notice of the same hostname to a
        count it never made.
        """
        if host in self._pending:
            self._pending[host] = None
        if self._in_flight == host:
            self._in_flight = None
            self._in_flight_evicted = False
            self._in_flight_id = None
        if notice_id is not None and self._pending_ids.get(host) != notice_id:
            self._lines.first_failure_resolved(host)
            self._accounted_notices.discard(notice_id)
            self._attempted_notices.discard(notice_id)
        self._wake.set()

    def in_flight_notice_id(self):
        """The identity of the notice the in-flight delivery carries."""
        return self._in_flight_id

    def pending_reports(self):
        """The hostnames the backend has not been told about, oldest first."""
        return list(self._pending)

    def pending_refusal_time(self, host):
        """When this console last refused the waiting hostname, or None.

        The pending notice keeps the refusal's own time beside its retry
        bookkeeping, because every other record of the refusal can expire
        before the notice lands: the newest-refused set is bounded and a
        roll of newer names rolls a waiting hostname out of it, and the
        line the console spent for it is kept for its hour alone. The
        report carries this time when neither of the others survives to
        the delivery, so the backend's record holds when the refusal
        happened and not when its notice was delivered.
        """
        return self._pending_at.get(host)

    def notice_accounted(self, notice_id):
        """Whether this console counted the refusal the notice carries.

        The answer covers the refusal path's own counting, the line it
        wrote at the refusal itself and the window its held-back lines
        count into, and the failures of the notice that named the refusal
        before this one, so the report the notice carries says beside its
        identity that the refusal is counted here already. The line times
        the hour rolls away cannot say it for a notice that waits past
        them, so the explicit state travels independently of them.
        """
        return notice_id in self._accounted_notices

    def ready_to_deliver(self):
        """The backend answers again: everything it was not told is due now.

        The failure backoff is lifted with it, and so are the per-host
        cooldown stamps, because a backend that was restarted knows none of
        the hostnames the old one acknowledged: its refusal record died with
        it, and the stamps would hold those hostnames back for up to an hour
        while its status page names none of them. The hour's global bound
        still caps what goes out. A marked mirror, one the record of
        completed windows shows the summary of paid, is dropped with the
        cooldown stamps rather than handed back: the fresh backend holds
        no record that refuses its fold, so a marked copy given to it
        would be folded in as a window it has never seen and its paid
        lines would be counted a second time, while an unmarked copy is
        a backend's only remaining record of a window it holds unpaid
        and keeps its place for the next report. A claimed mirror is
        kept the same way: the record's entry for its claim is this
        console's own promise to pay, not a payment the old backend
        logged, so the claim survives the swap with the copy, and the
        one line it owes the window's counts is still this console's to
        pay. The record the payment
        was written to is the one the two processes share, so it says
        the same after the restart as it did before, and the send time
        of no delivery of this console is consulted at all.

        The readiness also swears in a new backend generation, and any
        delivery whose answer is still unresolved belongs to the one that
        went before: the carried copy it could have delivered is decided
        by the record of completed windows the two processes share, so a
        copy whose payment the record proves is dropped at the restore,
        and an unpaid one keeps its place for the fresh generation, which
        holds no record of a fold the backend it reached may never have
        made.
        """
        self._ready_generation += 1
        for host in self._pending:
            self._pending[host] = None
        self._last_failure = None
        self._last_report.clear()
        if (self._mirrored is not None and not self._mirrored_owner
                and (self._mirrored_paid
                     or host_policy.completed_windows_record_has(self._mirrored[2]))):
            # The record of completed windows the two processes share shows
            # this window's summary was paid, and the backend that paid it
            # has just been swapped for one that holds nothing about it: the
            # completed record that refused the copy's folds is gone, so
            # handing the copy to the fresh backend would re-deliver lines
            # its payment already covered. The old backend kept the summary
            # logged, and the pair keeps one summary per window, paid where
            # it reached. An entry this console's own claim wrote is not a
            # payment's proof, and the owner flag holds it apart: the claim
            # is the payment this console owes the window, so the copy keeps
            # its place for the combined line this console still owes it,
            # whichever generation the backend holds.
            self._mirrored = None
            self._mirrored_paid = False
            self._mirrored_released = False
        self._wake.set()

    def _prune_reports(self, now):
        """Drop the hour-old report bookkeeping, by time and nothing else."""
        self._report_times = [stamp for stamp in self._report_times
                              if now - stamp < host_policy.HOUR_SECONDS]
        self._last_report = OrderedDict(
            (name, stamp) for name, stamp in self._last_report.items()
            if now - stamp < host_policy.HOUR_SECONDS)

    def wake_event(self):
        """The event refusals and readiness set to wake the worker."""
        return self._wake

    def start_drain(self):
        """Start the one delivery worker if it is not running.

        A refusal is not the only thing that owes the worker: a console
        that recovers a debt at its start starts the worker here as
        well, so the debt pays on the worker's paced look without a
        refusal to wake it.
        """
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(_drain_refusal_notices(self))

    async def stop_drain(self):
        """Stop the delivery worker and close its session."""
        task = self._drain_task
        self._drain_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _refusal_line_text(host: str) -> str:
    """The one shape of this console's per-host refusal line."""
    return (f"Refused a request for an unrecognized host: {host}. Allow it under "
            f"general.allowed_hosts in config.yaml, or set {host_policy.ALLOWED_HOSTS_ENV}.")


async def _send_refusal_notice(session: ClientSession, port, token: str, host: str,
                               reporter: _RefusalReporter) -> None:
    """Report a hostname this listener refused to the backend behind it.

    The backend never sees the request, so its health page and its console
    code know nothing about it until this arrives. The report carries the
    token this supervisor started the backend with, and only the answer that
    carries the token back counts: the one success status this endpoint gives,
    acknowledged by this backend. Anything else, a refusal, an error or an
    answer some other process on the port gave, leaves the hostname pending
    for the next try. Redirects are not followed on this call, because an
    ordinary page reached by following one is an answer this backend did not
    give about this report.

    The report also carries the time this console spent the hostname's line,
    while the backend could not be told and this console was the only place
    a refusal was named, and the times of the refusal lines it spent within
    the hour: the backend then writes no second line for the same hostname
    in the same hour, and a hostname refused for the first time once it
    answers draws on the one merged hour budget, which rolls over exactly
    at those lines' times. The first report a backend acknowledges carries
    the suppressed window this console holds as well, under the window's
    stable ID, so the summary the window owes is paid once by whichever
    layer holds it: the two processes share the hour's line budget and its
    one summary even though they share no memory.

    The report also hands the backend back the window the last
    acknowledgement carried, and the acknowledgement that lands carries the
    backend's own hour back: this console folds the spent line times in and
    keeps the window beside its own, so the two layers stay on the one
    allowance whichever of them spent it, and a window owed a summary is
    held by the one layer that will report it. Every report carries the
    newest bounded refused names with this console's recency stamps as
    well, so the backend's record reflects the newest refusals without a
    report of each name's own. The report of a hostname no backend has
    acknowledged also carries the time of the refusal it waits on, kept
    beside the retry bookkeeping and independently of the newest names
    and the line history, because the newest names are bounded and a roll
    of newer ones rolls a waiting hostname out of the set while the line
    the console spent for it is kept for its hour alone: a notice that
    outlives them all still tells the backend when the refusal happened
    and not when its delivery landed. The report also carries the
    identities of the lines it names, beside their times, so the backend
    spends one slot of the merged budget per line and not one per
    distinct timestamp. The acknowledgement names the
    windows the backend has logged and cleared, so this console discards
    the copies it holds of a window whose summary has been paid. A carried
    window the answer did not resolve waits in flight for the next report
    and never folds back into this console's recorder, so one window is
    owed one summary by the pair, whichever layer holds it. The restore
    carries the delivery's transport outcome with the copy as well, and
    the marking watches the exchange rather than the answer's arrival: a
    window whose report went out on a connection this delivery opened is
    held as sent and never emitted here, because the backend may have
    logged its summary before whatever failure ended the exchange, and
    one no delivery ever connected for keeps the sustained-outage
    emission, because only then could nothing have summarized it. A
    success whose delivery belongs to a generation readiness has since
    swapped out counts for the backend that gave it alone: the notice is
    requeued for the fresh one rather than cooled down, and neither the
    hour's bound nor the cooldown is spent by an answer the live backend
    never gave. The
    mirrored copy's payment is not this delivery's to know: the backend
    pays at the arrival, and the moment this request went out is not the
    arrival, so the payment is read from the record of completed windows
    the two processes share, which the backend wrote before its answer
    was built, whenever this console next needs to know it. The notice
    this delivery carries has an identity of its own, minted at its queue
    entry and travelling in the report's form: the backend writes the
    receipt of it to the record the two processes share before it
    processes the notice, so a delivery whose answer was lost is
    reconciled from the receipt and the failure path names no refusal a
    backend already took in, and the acknowledgement that arrives is
    matched against the identity the delivery carried, so it resolves the
    notice it answered and never a fresh notice of the same hostname the
    queue holds, whose refusal the backend it answers was never told
    about. The report also says when this console counted the refusal the
    notice carries, in a line it wrote or in the window the report
    carries, beside the identity itself: the line times the report folds
    in are the only other thing that says it, and the hour rolls them
    away while a notice still waits for its answer, so the explicit
    state keeps the backend from counting a refusal the console's window
    already holds.
    """
    data = {"host": host}
    notice_id = reporter.in_flight_notice_id()
    if notice_id is not None:
        # The notice's stable identity travels with the report, so the
        # backend writes its receipt under it before it processes the
        # notice: a delivery whose answer was lost is reconciled from the
        # receipt, and the acknowledgement is matched against the notice
        # that was really sent, never whatever fresh notice of the same
        # hostname the queue holds by then.
        data["notice"] = notice_id
        if reporter.notice_accounted(notice_id):
            # The refusal the notice carries is counted by this console
            # already, in a line it wrote or in the window the report
            # carries: the state travels beside the identity, because the
            # line times the hour rolls away cannot say it for a notice
            # that waits past them.
            data["notice_counted"] = "1"
    logged = reporter.line_time(host, notice_id)
    if logged is not None:
        data["logged"] = str(logged)
    refused_at = reporter.pending_refusal_time(host)
    if refused_at is not None:
        data["refused_at"] = str(refused_at)
    hour_lines = reporter.hour_line_times()
    if hour_lines:
        data["hour_lines"] = ",".join(str(stamp) for stamp in hour_lines)
        hour_line_ids = reporter.hour_line_ids()
        if hour_line_ids:
            data["hour_lines_ids"] = ",".join(hour_line_ids)
    carried = reporter.take_carried_summary()
    if carried is not None:
        data["suppressed"] = str(carried[0])
        data["suppressed_since"] = str(carried[1])
        if carried[2] is not None:
            data["suppressed_id"] = carried[2]
    mirrored = reporter.mirrored_summary()
    if mirrored is not None:
        data["mirrored"] = str(mirrored[0])
        data["mirrored_since"] = str(mirrored[1])
        if mirrored[2] is not None:
            data["mirrored_id"] = mirrored[2]
        if mirrored[3] and mirrored[4] and len(mirrored[4]) == len(mirrored[3]):
            # The copy stands for every window the aggregate counts, and
            # the backend that receives it back needs each constituent's
            # identity with the counts it contributed, so its own window
            # keeps the parts the aggregate was built from and a payment
            # that finds some of the identities claimed elsewhere can
            # settle the rest by identity.
            data["mirrored_ids"] = ",".join(mirrored[3])
            data["mirrored_counts"] = ",".join(str(value) for value in mirrored[4])
    recent = reporter.recent_refusals()
    if recent:
        data["recent"] = ",".join(name for name, _stamp in recent)
        data["recent_at"] = ",".join(str(stamp) for _name, stamp in recent)
    delivered = False
    reached = False
    try:
        async with session.post(f"http://{BACKEND_HOST}:{port}{reporter.notice_path}",
                                headers={TOKEN_HEADER: token},
                                data=data,
                                allow_redirects=False) as response:
            # Entering the context sent the request on an established
            # connection, whatever answer comes back or none at all, so the
            # window this report carried is marked sent on its way back to
            # the slot: the backend may have logged its summary before the
            # answer was lost, and this console never emits a sent window.
            reached = True
            await response.read()
            if response.status == 204 and response.headers.get(TOKEN_HEADER) == token:
                delivered = True
                if reporter.delivery_spoke_for_the_live_backend():
                    reporter.report_sent(host, notice_id)
                else:
                    # The answer comes from the generation readiness swapped
                    # out while this delivery waited, so the backend that
                    # would have counted the report is gone and the one that
                    # answers now was told nothing: the notice keeps its
                    # place for the fresh backend, and neither the hour's
                    # bound nor the cooldown is spent by an answer the live
                    # backend never gave.
                    reporter.report_requeued_for_the_fresh_backend(host, notice_id)
                reporter.merge_acknowledged(carried, *_acknowledged_accounting(response))
                return
    except (ClientConnectorError, ConnectionTimeoutError):
        # No connection ever opened for this request, refused, unreachable,
        # unresolvable or timed out at the connect, so no request byte of
        # this report ever left for any backend: nothing could have
        # summarized the carried window, and it keeps the sustained-outage
        # emission.
        pass
    except Exception:
        # Any other failure of the exchange, the server closing the
        # connection, a reset, a payload or timeout failure, leaves a
        # connection that was open for this request, so the report counts
        # as possibly delivered: the backend may have taken it in and
        # logged the summary before the failure, and this console never
        # emits a window its report may have reached the backend with.
        reached = True
    finally:
        # A delivery the backend did not acknowledge, whatever ended it,
        # leaves the carried window in flight here: the next report carries
        # it again, and the console never folds it back into its own
        # recorder, where a later refusal could emit a second summary. The
        # transport outcome travels with the copy, so a window the request
        # reached the backend with is never emitted here, one no delivery
        # ever connected for keeps the sustained-outage emission, and one
        # whose delivery belongs to a backend generation readiness has
        # since swapped away is dropped rather than handed to a fresh
        # generation that holds no record of the fold its arrival made.
        if not delivered and carried is not None:
            reporter.restore_carried_summary(carried, reached)
    reporter.report_failed(host, notice_id)


def _acknowledged_accounting(response):
    """The accounting a backend's acknowledgement carries back, if any.

    The headers hold the times of the lines the backend's hour has spent
    with the identity of each, the hostnames those lines were logged for
    with the time of each, the suppressed window it holds with the
    identity of every window that window counts and the counts each
    contributed, and the IDs of the windows it has logged and cleared,
    and an answer that carries none of them folds nothing. Every value is
    parsed the defensive way the backend's own notice route parses what
    arrives, because the two processes only ever share words on the wire.
    A window whose identities arrive without a set of counts that answers
    them one for one keeps the one identity the wire could vouch for: the
    claim the console takes covers what the answer actually named, and the
    fold a backend makes of the copy handed back to it takes the whole
    aggregate under the travelling ID alone.
    """
    times = []
    for stamp in response.headers.get(ACCOUNTING_HOUR_LINES_HEADER, "").split(","):
        try:
            times.append(float(stamp))
        except ValueError:
            pass
    ids = [line_id for line_id in
           response.headers.get(ACCOUNTING_HOUR_LINES_IDS_HEADER, "").split(",") if line_id]
    hosts = [name for name in response.headers.get(ACCOUNTING_HOUR_HOSTS_HEADER,
                                                   "").split(",") if name]
    stamps = []
    for stamp in response.headers.get(ACCOUNTING_HOUR_HOST_LINES_HEADER, "").split(","):
        try:
            stamps.append(float(stamp))
        except ValueError:
            stamps.append(None)
    hour_hosts = [(name, stamp) for name, stamp in zip(hosts, stamps) if stamp is not None]
    count = response.headers.get(ACCOUNTING_SUPPRESSED_HEADER, "")
    try:
        suppressed = int(count) if count else 0
    except ValueError:
        suppressed = 0
    since = response.headers.get(ACCOUNTING_SUPPRESSED_SINCE_HEADER, "")
    try:
        opened = float(since) if since else None
    except ValueError:
        opened = None
    window_id = response.headers.get(ACCOUNTING_SUPPRESSED_ID_HEADER) or None
    window_ids = [known_id for known_id in
                  response.headers.get(ACCOUNTING_SUPPRESSED_IDS_HEADER, "").split(",")
                  if known_id]
    window_counts = []
    for value in response.headers.get(ACCOUNTING_SUPPRESSED_COUNTS_HEADER, "").split(","):
        try:
            window_counts.append(int(value))
        except ValueError:
            window_counts = []
            break
    if not window_id:
        window_ids, window_counts = [], []
    elif not window_ids or len(window_counts) != len(window_ids):
        window_ids, window_counts = [window_id], []
    held = ((suppressed, opened, window_id, window_ids, window_counts)
            if suppressed and opened is not None else None)
    completed = [window_id for window_id in
                 response.headers.get(ACCOUNTING_COMPLETED_WINDOWS_HEADER, "").split(",")
                 if window_id]
    return times, ids, hour_hosts, held, completed


async def _drain_refusal_notices(reporter: _RefusalReporter) -> None:
    """The one worker that delivers refused-hostname reports to the backend.

    One client session and one report in flight at a time, so the reports
    held through an outage arrive as a paced line of requests against a
    backend that has just come up rather than a burst. It is woken by every
    refusal and by readiness, and its own timer retries what no backend
    acknowledged, so a failed report needs no further request and no manual
    call to be tried again. While the backend cannot be reached it sends
    nothing and waits on its wake event alone, with the retry pace for a
    fallback: nothing but readiness can make a delivery possible before the
    port is set again, and a pending report past its retry pace must not
    turn this loop into a busy spin against a port no backend answers.
    """
    wake = reporter.wake_event()
    session = None
    try:
        while True:
            wake.clear()
            port, token = BACKEND_PORT, BACKEND_TOKEN
            host = None
            if port and token:
                host = reporter.take_due_report()
            if host is None:
                if port is None:
                    delay = RETRY_PACE_SECONDS
                else:
                    # While the backend answers, this worker is the only
                    # timer the console has: a stream of refusals that
                    # stops leaves the window of its last counted lines
                    # owed a summary no request will ever come back to
                    # pay, so each turn of the paced wait below pays the
                    # due summary itself, once per pace and never in a
                    # spin, the way the readiness poll pays it while the
                    # backend cannot be told.
                    _print_due_refusal_summary(reporter)
                    delay = reporter.retry_delay()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                if session is None:
                    # The connect timeout is the shorter of the two, so a
                    # stall while the connection is still being opened,
                    # waiting for a pool slot or connecting the socket, ends
                    # as the connector's own timeout in the never-connected
                    # handler of the send below: a timeout that fires while
                    # the connection is still being opened leaves the window
                    # unsent, because no request byte of the report ever
                    # left for any backend. A total timeout firing there
                    # instead would read as a failure of an exchange that
                    # had its connection open and mark the window sent,
                    # withholding a summary no backend ever received.
                    session = ClientSession(timeout=ClientTimeout(total=5, connect=3))
                await _send_refusal_notice(session, port, token, host, reporter)
            except asyncio.CancelledError:
                raise
            except Exception:
                reporter.report_failed(host, reporter.in_flight_notice_id())
    finally:
        if session is not None:
            await session.close()


def _clock_minutes(stamp: float) -> str:
    """A timestamp as HH:MM, on the clock the operator reads."""
    return time.strftime("%H:%M", time.localtime(stamp))


def _print_due_refusal_summary(reporter: _RefusalReporter) -> None:
    """This console's summary line, once the hour it counts has run.

    The refusal path calls it, and so does the readiness poll while the
    backend cannot be told, which is the main time this console writes
    refusal lines: a stream of refusals that stops does not take the hour's
    summary with it. The delivery worker's paced wait calls it while the
    backend answers, for the summaries its own counted repeats owe once
    the traffic has stopped. The other time is a failed delivery, whose
    host line the failure path writes itself.

    The payment decision is claimed on the record of completed windows
    the two processes share inside the take itself, before the line is
    logged and before this console builds anything on it, the same way
    the backend claims its own, so a console that holds a copy of the
    window, and any readiness that follows, verifies the payment from
    there however its own answers turn out, even from a payer that died
    between its line and this write. The write below renews the same
    proof for the answer that will carry it. The moment the line landed
    joins the record of emission moments the two processes share as
    well, so the pair's ceiling of one summary an hour holds whichever
    payment comes next, in this console or in the backend: two windows
    no acknowledgement ever carried between the layers share no
    identity the claim could decide on, and the moments name the hour
    the pair last paid.
    """
    summary = reporter.take_summary()
    if summary:
        count, opened, closed, _window_id = summary
        _print_refusal_line(f"Suppressed {count} host-refusal lines between "
                            f"{_clock_minutes(opened)} and {_clock_minutes(closed)}.")
        host_policy.record_completed_windows(reporter.completed_windows(), closed)
        host_policy.record_summary_emission(closed)


def _print_due_refusal_summary_for_active_reporter() -> None:
    """The readiness poll's share of the summary above."""
    if _active_reporter is not None:
        _print_due_refusal_summary(_active_reporter)


def create_host_check_middleware(config_dir: str, base_url: str = "/"):
    """The front listener's share of the request-host check.

    The backend applies the same policy behind its own listener, but between
    the two only this layer sees the client-facing hostname when
    general.trusted_proxy names an outer proxy: the backend's peer is always
    this supervisor on loopback, so with any other trusted_proxy value than
    127.0.0.1 the backend sees a loopback literal and its own check answers
    everything. Both layers enforce, so a hostname has to pass both.

    The approval path stays open for exactly the one POST that submits the
    form on the refusal page, at the base and at the root (a proxy that
    strips the prefix reaches the root one); any other request for it is
    judged by its hostname like everything else. The Host header itself is
    what a request is judged by: the web library's own fallback to the
    socket address would admit a request that carried no hostname.
    """
    global _active_reporter
    state = _HostCheckState(config_dir)
    base = str(base_url or "").strip().strip("/")
    prefix = f"/{base}" if base else ""
    segment = host_policy.APPROVAL_SEGMENT
    approval_paths = {f"/{segment}"} if not prefix else {f"{prefix}/{segment}", f"/{segment}"}
    reporter = _RefusalReporter(f"{prefix}/_supervisor/host_refusal")
    _active_reporter = reporter

    @web.middleware
    async def host_check_middleware(request: web.Request, handler):
        if request.method == "POST" and request.path in approval_paths:
            return await handler(request)
        state.refresh()
        allowed, refused = host_check_decision(
            request.headers.get("Host", ""),
            request.remote,
            request.headers.get("X-Forwarded-Host", ""),
            state.auth_type(),
            state.trusted_proxy(),
            state.allowed_hosts(),
        )
        if allowed:
            return await handler(request)
        shown = refused[0]
        also = refused[1] if len(refused) > 1 else None
        reporter.start_drain()
        if BACKEND_PORT is None:
            # The backend cannot be told about these hostnames, so this
            # console is the only place their refusals are named.
            for name in (shown, also):
                if name and reporter.note_line(name):
                    _print_refusal_line(_refusal_line_text(name))
        _print_due_refusal_summary(reporter)
        for name in (shown, also):
            if name:
                reporter.report_due(name)
        return _host_refusal_response(request, shown, prefix, also=also)

    host_check_middleware.reporter = reporter
    host_check_middleware.state = state
    host_check_middleware.approval_paths = approval_paths
    return host_check_middleware


def _read_bazarr_config(config_dir: str) -> dict:
    """Read config.yaml to extract non-secret frontend defaults."""
    config_path = Path(config_dir) / "config" / "config.yaml"
    defaults = {"baseUrl": "/", "apiKey": "", "canUpdate": False, "hasUpdate": False}
    try:
        if config_path.is_file():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            general = cfg.get("general", {})
            if general.get("base_url"):
                defaults["baseUrl"] = general["base_url"]
    except Exception as e:
        print(f"[supervisor] Warning: could not read config: {e}")
    return defaults


def _config_yaml(config_dir: str) -> Path:
    return Path(config_dir) / "config" / "config.yaml"


def _split_listen_port(args: list[str]) -> tuple[int | None, list[str]]:
    """The listen port from the command line, and the arguments without it.

    Takes every spelling the backend's parser accepts for -p/--port ("-p N",
    "-pN", "-p=N", "--port N", "--port=N" or an abbreviation such as
    "--po N"), the last one winning. None of them may reach the backend: it is
    started with its own loopback port first, and a later port option would
    override that one while the readiness poll waits on it.
    """
    port = None
    rest = []
    i = 0
    while i < len(args):
        arg = args[i]
        value = None
        if arg.startswith("--"):
            name, has_value, attached = arg.partition("=")
            # No other backend option starts with "--p".
            if len(name) >= 3 and "--port".startswith(name):
                if has_value:
                    value = attached
                elif i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
        elif arg.startswith("-p"):
            if len(arg) > 2:
                value = arg[3:] if arg[2] == "=" else arg[2:]
            elif i + 1 < len(args):
                value = args[i + 1]
                i += 1
        if value is None:
            rest.append(arg)
        else:
            port = int(value)
        i += 1
    return port, rest


def _backend_config_dir(bazarr_args: list[str], environ=os.environ) -> str:
    """The configuration directory the backend will use with these arguments.

    Follows bazarr/app/get_args.py, so the listen port and index.html come
    from the same config.yaml the backend reads: the last -c/--config wins,
    in any form argparse accepts ("-c DIR", "-cDIR", "-c=DIR", "--config DIR",
    "--config=DIR" or an abbreviation such as "--conf DIR"), then
    BAZARR_CONFIG_DIR, then the data directory beside the code.
    """
    config_dir = None
    i = 0
    while i < len(bazarr_args):
        arg = bazarr_args[i]
        if arg.startswith("--"):
            name, has_value, value = arg.partition("=")
            # "--c" alone is ambiguous to the backend, which also has
            # --create-db-revision, and refuses to start.
            if len(name) >= 4 and "--config".startswith(name):
                if has_value:
                    config_dir = value
                elif i + 1 < len(bazarr_args):
                    config_dir = bazarr_args[i + 1]
                    i += 1
        elif arg.startswith("-c"):
            if len(arg) > 2:
                config_dir = arg[3:] if arg[2] == "=" else arg[2:]
            elif i + 1 < len(bazarr_args):
                config_dir = bazarr_args[i + 1]
                i += 1
        i += 1
    if config_dir is not None:
        return config_dir
    return environ.get("BAZARR_CONFIG_DIR", "").strip() or str(APP_DIR / "data")


def _configured_listen_port(config_dir: str) -> tuple[int, str]:
    """general.port from config.yaml, or DEFAULT_PORT when unset or invalid,
    with "general.port" or "default" to say which.

    Kept apart from _read_bazarr_config, whose result is injected into
    index.html. Accepts what the backend's validator accepts, an int in
    1..65535, because the backend resets anything else to 6767.
    """
    default = (DEFAULT_PORT, "default")
    config_path = _config_yaml(config_dir)
    try:
        if not config_path.is_file():
            return default
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        general = cfg.get("general") if isinstance(cfg, dict) else None
        value = general.get("port") if isinstance(general, dict) else None
    except Exception as e:
        print(f"[supervisor] Warning: could not read the port from config: {e}")
        return default
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        print(f"[supervisor] Warning: general.port {value!r} is not a port number, using {DEFAULT_PORT}")
        return default
    return value, "general.port"


def _bind_failure_message(port: int, source: str, config_dir: str, error: OSError) -> str:
    """One line that says which setting chose the port and how to recover.

    By the time this prints the UI is unreachable, so this line is the only
    place an operator learns what to change.
    """
    config_path = _config_yaml(config_dir)
    if source == "--port":
        origin, fix = "from --port", "Pass a free port to --port, or free this one."
    elif source == "general.port":
        origin = f"from general.port in {config_path}"
        fix = "Change general.port there, or free the port."
    else:
        origin = "the default"
        fix = f"Free the port, or set general.port in {config_path} to a free one."
    return f"Cannot listen on port {port} ({origin}): {error}. {fix}"


def _pick_backend_port() -> int:
    """A loopback port nothing holds right now, chosen by the kernel."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((BACKEND_HOST, 0))
        return sock.getsockname()[1]


def _write_port_file(port: int, path: Path) -> None:
    """Record the bound port for the health check. Best effort.

    Written to a temporary file and renamed into place, so the health check
    never reads a half-written file and a symlink planted at the path is
    replaced rather than followed. It is left in place on shutdown: the
    container, and its /tmp, go with the supervisor, and an empty /tmp would
    send the health check to 6767, which may be another instance.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(f"{port}\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception as e:
        consequence = ""
        if port != DEFAULT_PORT:
            consequence = (f" Without it the container health check probes port {DEFAULT_PORT}, not {port},"
                           " and on a shared network that can be another instance. Under read_only,"
                           " mount a tmpfs on /tmp.")
        print(f"[supervisor] Warning: could not write {path}: {e}.{consequence}")


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
    _base_prefix = _startup_base_url.strip("/")  # e.g. "bazarr", or "" when not set

    async def static_handler(request: web.Request) -> web.StreamResponse:
        """Serve static frontend files, fallback to index.html for SPA routing."""
        path = request.path.lstrip("/")

        # When base_url is set (e.g. /bazarr), the browser resolves ./assets/...
        # relative to <base href="/bazarr/"> and requests /bazarr/assets/....
        # Strip the prefix before the allowlist lookup so the file is found.
        if _base_prefix and path.startswith(_base_prefix + "/"):
            path = path[len(_base_prefix) + 1:]

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
    config = _read_bazarr_config(config_dir)
    base = config.get("baseUrl", "/").strip("/")

    host_check = create_host_check_middleware(config_dir, config.get("baseUrl", "/"))
    app = web.Application(middlewares=[host_check])

    # The delivery worker of the host check above outlives the routes: stop
    # it and close its client session when the application goes away.
    async def _stop_refusal_drain(_app):
        await host_check.reporter.stop_drain()

    app.on_cleanup.append(_stop_refusal_drain)

    # Supervisor-handled endpoints (not proxied)
    status_handler = create_supervisor_status_handler(backend)
    sse_handler = create_supervisor_sse_handler(backend)
    app.router.add_route("GET", "/_supervisor/status", status_handler)
    app.router.add_route("GET", "/_supervisor/events", sse_handler)
    if base:
        app.router.add_route("GET", f"/{base}/_supervisor/status", status_handler)
        app.router.add_route("GET", f"/{base}/_supervisor/events", sse_handler)

    # The approval endpoint for a refused hostname: not proxied by any
    # PROXY_PREFIXES, so it gets its own route to the backend, at the base and
    # at the root (a prefix-stripping proxy reaches the root one), and the
    # host check above leaves exactly the one submission open. The backend
    # rewrites config.yaml to add the hostname, so the cached read of it does
    # not survive the answer.
    async def approval_proxy(request: web.Request) -> web.StreamResponse:
        response = await proxy_handler(request)
        host_check.state.invalidate()
        return response

    for path in sorted(host_check.approval_paths):
        app.router.add_route("POST", path, approval_proxy)

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
    port, bazarr_args = _split_listen_port(sys.argv[1:])
    config_dir = _backend_config_dir(bazarr_args)
    if not os.environ.get("BAZARR_CONFIG_DIR", "").strip():
        # The record of completed refusal windows lives in the
        # configuration directory, resolved by both processes through the
        # environment: export the directory this supervisor resolved from
        # its own arguments, so the backend it spawns and this process
        # read and write the same record, restarts and all.
        os.environ["BAZARR_CONFIG_DIR"] = config_dir

    if port is None:
        port, source = _configured_listen_port(config_dir)
    else:
        source = "--port"

    backend = BackendManager(bazarr_args)

    # Bind the front listener before launching the backend, so a port that is
    # already taken fails here and does not leave a backend starting up.
    app = create_app(config_dir, backend)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
    except OSError as e:
        # No fallback to 6767: on a shared network that is the collision this
        # listener exists to avoid.
        print(f"[supervisor] {_bind_failure_message(port, source, config_dir, e)}")
        await runner.cleanup()
        return 1
    origin = "(default)" if source == "default" else f"(from {source})"
    print(f"[supervisor] Frontend serving on http://0.0.0.0:{port} {origin}")
    _write_port_file(port, PORT_FILE)

    backend_task = asyncio.create_task(backend.run())

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
    sys.exit(asyncio.run(main()))

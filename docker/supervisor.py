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
# the suppressed window it holds with its stable ID, and the IDs of the
# windows it has logged and cleared, so this console can drop every copy it
# holds of a window whose summary has been paid. The backend defines the
# same names on its side, because the two processes share no imports.
ACCOUNTING_HOUR_LINES_HEADER = "X-Bazarr-Hour-Lines"
ACCOUNTING_SUPPRESSED_HEADER = "X-Bazarr-Suppressed"
ACCOUNTING_SUPPRESSED_SINCE_HEADER = "X-Bazarr-Suppressed-Since"
ACCOUNTING_SUPPRESSED_ID_HEADER = "X-Bazarr-Suppressed-Id"
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
    if also:
        named += f" (and its forwarded hostname {html.escape(also)})"
    action = html.escape(f"{base}/{host_policy.APPROVAL_SEGMENT}")
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
<p>To answer it, add <code>{named}</code> to <code>general.allowed_hosts</code>
in config.yaml, or set <code>{host_policy.ALLOWED_HOSTS_ENV}</code> in the
environment, or enter the one-time approval code below. The code is printed to
this container's logs (<code>docker logs</code>) and nowhere else; it is
replaced after each use and by every restart: use the most recent line.</p>
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
    lines do. The first report a backend acknowledges also carries this
    console's suppressed window, so the summary the window owes is paid
    once by whichever layer holds it: the hour's line budget and its one
    summary are the two processes' shared allowance, even though they
    share no memory.

    The allowance flows back the other way as well: every acknowledgement
    carries the backend's own hour with it, and this console folds the
    spent line times into its recorder exactly as it folds its own, so a
    backend that spent the hour and then became unreachable leaves this
    console nothing more to spend. The window the acknowledgement carried
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
    that already counts them.

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
        self._accounted = OrderedDict()  # host -> time this console last spent its line
        self._recent = OrderedDict()  # host -> time this console last refused it
        self._report_times = []  # times of this hour's acknowledged report deliveries
        self._last_failure = None  # when the last delivery failed, until an ack or readiness
        self._mirrored = None  # the window the last acknowledgement carried back
        self._carried = None  # the window a report has carried, held in flight here
        self._carried_at = None  # when the carry began
        self._carried_sent = False  # whether a delivery for it ever reached the backend
        self._last_ack = None  # when the last acknowledgement of any kind arrived
        self._wake = asyncio.Event()  # refusals and readiness wake the worker
        self._drain_task: "asyncio.Task | None" = None

    def note_line(self, host):
        """Record a refusal; True when a console line may be written now."""
        accounted = self._lines.note(host)
        self._account_line(host)
        return accounted

    def _account_line(self, host):
        """Remember that this console spent this hostname's line for the hour.

        The report the backend eventually takes carries the time, so the
        backend writes no second line for the same hostname in the same
        hour: the two processes share the hour's line budget even though
        they share no memory. The bookkeeping is bounded and rolled past
        its hour like every other roll here.
        """
        now = self._now()
        self._accounted = OrderedDict(
            (name, stamp) for name, stamp in self._accounted.items()
            if now - stamp < host_policy.HOUR_SECONDS)
        self._accounted[host] = now
        while len(self._accounted) > host_policy.REFUSED_HOSTS_LIMIT:
            self._accounted.popitem(last=False)

    def line_time(self, host):
        """When this console last spent this hostname's line, within the hour."""
        now = self._now()
        stamp = self._accounted.get(host)
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

    def merge_acknowledged(self, carried, hour_lines, held, completed):
        """Fold the accounting the backend's acknowledgement carried back.

        The line times join this console's hour budget exactly as its own
        do, so the two layers stay on the one spent allowance whichever of
        them spent it. The window the acknowledgement carried is kept
        beside this console's own, never inside it: whichever layer holds
        a window when its hour runs is the one that reports it, so a
        window held in two recorders would be reported twice, and this
        copy is never reported here at all. The acknowledgement replaces
        whatever the last one carried, so a repeated one cannot double it,
        and a held window the same answer reports completed is dropped
        instead, because the two cannot both be true of one window and
        the completed list is the one that says the summary has been paid.

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
        self._lines.merge_hour_lines(hour_lines)
        if held is not None and held[2] is not None and held[2] in completed:
            held = None
        self._mirrored = held
        self._last_ack = self._now()
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
        keeps no copy of a folded window beside it. The copy waits
        only the hour the window has left to run, on the horizon the line
        times use, so it never outlives the accounting it stands for.
        """
        now = self._now()
        held = self._mirrored
        if held is None:
            return None
        if now - held[1] >= host_policy.HOUR_SECONDS:
            self._mirrored = None
            return None
        return held

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
        reads when it decides what this console may still emit.

        A copy waiting in the slot ages with it: a sent copy no answer
        resolved is dropped once the carry has run past the span the
        backend remembers the window's ID, before any report carries it,
        on the same bounded lose-not-double trade the local emission
        below makes, because a backend that no longer remembers the ID
        would fold the copy in as a window it has never seen and log a
        second line for lines a first summary may already have paid. An
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
                    and now - self._carried_at >= host_policy.CARRY_RETENTION_SECONDS):
                # Sent, never resolved, and held past the whole span the
                # backend remembers the window's ID: a report carrying the
                # copy again would hand it to a backend that has pruned the
                # ID and would fold it in as a window it has never seen,
                # logging a second line for lines a first summary may
                # already have paid, so the slot drops the copy before any
                # report can carry it, on the one retention span both
                # sides share. The age runs from the take and not from a
                # restore, because a restore keeps the same carry going,
                # so this hold ends within the same span the backend's
                # memory of the ID does and a copy this console can still
                # re-carry always meets a backend that still remembers it.
                # The pair keeps the one summary the backend logged, or
                # loses the one the backend never took, rather than risk
                # the second line.
                self._carried = None
            else:
                carried, self._carried = self._carried, None
                return carried
        carried = self._lines.take_carried_summary()
        if carried is not None:
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
        """
        if carried is not None:
            if reached:
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
        anywhere and nothing could have summarized it.

        A window a delivery sent but no answer resolved is never emitted
        here however long the outage runs: the backend may have logged its
        summary before the answer was lost, so the pair leaves the window
        to the acknowledgement protocol, and the copy waits in the slot to
        be carried by every later report, discarded when an answer names
        its ID completed and folded by a backend that still counts it,
        and a backend that never answers loses the one summary rather
        than the pair logging a second line for a window the backend may
        already have summarized. The wait is bounded all the same: once the
        carry has run past the span the backend remembers the window's ID,
        held for exactly that purpose on the one constant both sides share
        so the retention and this hold cannot drift apart, the copy is
        dropped outright, emitted nowhere and carried by no later report,
        because a backend that no longer remembers the ID would fold it in
        again and log a second line, so the pair keeps the one summary the
        backend logged, or loses the one the backend never took, rather
        than risk logging the window twice. Any acknowledgement since the
        carry began bars the emission as well and drops the copy once its
        hour has run, because the backend has taken the window in and
        holds it or has paid it, and the mirrored copy or the completed
        IDs carry its accounting, so the pair stays on the one summary
        whichever layer pays it.
        """
        now = self._now()
        carried = self._carried
        if carried is not None and now - self._carried_at >= host_policy.HOUR_SECONDS:
            if self._carried_sent and (self._last_ack is None or self._last_ack < self._carried_at):
                if now - self._carried_at >= host_policy.CARRY_RETENTION_SECONDS:
                    # Sent, never answered, and carried past the whole span
                    # the backend remembers the window's ID: a later report
                    # would re-carry the copy to a backend that folds it in
                    # as a window it has never seen and logs a second line,
                    # so the copy is dropped outright, emitted nowhere and
                    # carried by no later report, on the one retention span
                    # both sides share so this hold cannot outlast what any
                    # backend remembers of the window. The pair keeps the
                    # one summary the backend logged, or loses the one the
                    # backend never took, rather than risk a second line for
                    # a window whose ID no backend remembers.
                    self._carried = None
                    return self._lines.take_summary()
                # Sent but never answered: the backend may have logged the
                # summary before its answer was lost, so the window stays in
                # the slot for the acknowledgement protocol and is never
                # emitted here, however long the outage runs.
                return self._lines.take_summary()
            self._carried = None
            if self._last_ack is None or self._last_ack < self._carried_at:
                return carried[0], carried[1], now, carried[2]
        return self._lines.take_summary()

    def report_due(self, host):
        """Queue the hostname for the delivery worker, or leave it alone.

        True when the hostname is newly waiting to be told: one acknowledged
        within the hour is done until the hour runs, and one already waiting
        is the worker's to deliver. A hostname refused again while it waits
        moves to the end of the queue, the newest place, so the eviction
        drops the oldest waiting and a frequently refused hostname is not
        pushed out by names that arrived after its last refusal. The pending
        set holds the REFUSED_HOSTS_LIMIT newest of them, the same number the
        backend's record keeps, so a longer roll of hostnames cannot grow a
        longer queue: the ones the eviction drops are the oldest waiting,
        the ones the backend's record has dropped too.

        Every refusal refreshes the newest-refused set first, whatever the
        queue decides, because the skipped and the waiting names are still
        the newest news about the refusals this console saw, and the
        reports carry the set to the backend's record.
        """
        now = self._now()
        self._prune_reports(now)
        self._note_recent(host, now)
        if host in self._last_report:
            return False
        if host in self._pending:
            self._pending.move_to_end(host)
            return False
        self._pending[host] = None
        while len(self._pending) > host_policy.REFUSED_HOSTS_LIMIT:
            self._pending.popitem(last=False)
        self._wake.set()
        return True

    def take_due_report(self):
        """The next hostname the worker may tell the backend about, or None.

        Oldest waiting first, once its retry pace has run; a hostname never
        attempted is due at once. Taking one only marks the attempt: the
        hour's global reporting bound is spent by acknowledged deliveries
        alone, and a delivery that failed holds every later attempt back
        for one pace, so a failing backend is asked once per pace and not
        in a stream, and cannot spend the hour instead of the recovery.
        """
        now = self._now()
        self._prune_reports(now)
        if len(self._report_times) >= REPORTS_PER_HOUR:
            return None
        if self._last_failure is not None and now - self._last_failure < RETRY_PACE_SECONDS:
            return None
        for host, last_try in self._pending.items():
            if last_try is None or now - last_try >= RETRY_PACE_SECONDS:
                self._pending[host] = now
                return host
        return None

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

    def report_sent(self, host):
        """The backend acknowledged the report; it is not pending any more.

        The cooldown is kept by time alone, never by a count: however many
        hostnames are acknowledged around it, a told one stays told for its
        full hour. The acknowledgement is also what spends the hour's global
        reporting bound and lifts the failure backoff, so a backend that
        refuses or drops reports cannot spend the hour instead of the
        recovery.
        """
        now = self._now()
        self._last_report[host] = now
        self._pending.pop(host, None)
        self._report_times.append(now)
        self._last_failure = None

    def report_failed(self, host):
        """The backend did not take the report: it stays pending, pace
        permitting, and this console names the hostname, because from this
        moment this console is the only place that does until it lands. The
        failure also holds every later attempt back for one pace, and the
        line below asks its pace without counting a held-back line as
        suppressed, so an hour of failing retries does not add up to a
        summary that says a stream of refusals was held back."""
        self._last_failure = self._now()
        if host in self._pending:
            self._pending[host] = self._now()
        self._account_line(host)
        if self._lines.note_retry(host):
            _print_refusal_line(_refusal_line_text(host))

    def pending_reports(self):
        """The hostnames the backend has not been told about, oldest first."""
        return list(self._pending)

    def ready_to_deliver(self):
        """The backend answers again: everything it was not told is due now.

        The failure backoff is lifted with it, and so are the per-host
        cooldown stamps, because a backend that was restarted knows none of
        the hostnames the old one acknowledged: its refusal record died with
        it, and the stamps would hold those hostnames back for up to an hour
        while its status page names none of them. The hour's global bound
        still caps what goes out.
        """
        for host in self._pending:
            self._pending[host] = None
        self._last_failure = None
        self._last_report.clear()
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
        """Start the one delivery worker if it is not running."""
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
    report of each name's own, and the acknowledgement names the windows
    the backend has logged and cleared, so this console discards the
    copies it holds of a window whose summary has been paid. A carried
    window the answer did not resolve waits in flight for the next report
    and never folds back into this console's recorder, so one window is
    owed one summary by the pair, whichever layer holds it. The restore
    carries the delivery's transport outcome with the copy as well, and the
    marking watches the exchange rather than the answer's arrival: a window
    whose report went out on a connection this delivery opened is held as
    sent and never emitted here, because the backend may have logged its
    summary before whatever failure ended the exchange, and one no delivery
    ever connected for keeps the sustained-outage emission, because only
    then could nothing have summarized it.
    """
    data = {"host": host}
    logged = reporter.line_time(host)
    if logged is not None:
        data["logged"] = str(logged)
    hour_lines = reporter.hour_line_times()
    if hour_lines:
        data["hour_lines"] = ",".join(str(stamp) for stamp in hour_lines)
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
                reporter.report_sent(host)
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
        # reached the backend with is never emitted here, and one no
        # delivery ever connected for keeps the sustained-outage emission.
        if not delivered and carried is not None:
            reporter.restore_carried_summary(carried, reached)
    reporter.report_failed(host)


def _acknowledged_accounting(response):
    """The accounting a backend's acknowledgement carries back, if any.

    The headers hold the times of the lines the backend's hour has spent,
    the suppressed window it holds, and the IDs of the windows it has
    logged and cleared, and an answer that carries none of them folds
    nothing. Every value is parsed the defensive way the backend's own
    notice route parses what arrives, because the two processes only ever
    share words on the wire.
    """
    times = []
    for stamp in response.headers.get(ACCOUNTING_HOUR_LINES_HEADER, "").split(","):
        try:
            times.append(float(stamp))
        except ValueError:
            pass
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
    held = (suppressed, opened, window_id) if suppressed and opened is not None else None
    completed = [window_id for window_id in
                 response.headers.get(ACCOUNTING_COMPLETED_WINDOWS_HEADER, "").split(",")
                 if window_id]
    return times, held, completed


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
                reporter.report_failed(host)
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
    summary with it. The other time is a failed delivery, whose host line
    the failure path writes itself.
    """
    summary = reporter.take_summary()
    if summary:
        count, opened, closed, _window_id = summary
        _print_refusal_line(f"Suppressed {count} host-refusal lines between "
                            f"{_clock_minutes(opened)} and {_clock_minutes(closed)}.")


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

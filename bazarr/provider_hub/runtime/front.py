# coding=utf-8
"""The authenticating front: the only way to a managed daemon.

This file runs inside a small container started from Bazarr+'s own image (see ``front_runtime``, the host
half). It imports nothing from Bazarr+, only the standard library, so ``python -I front.py`` is the whole program.

What it is: a threaded reverse proxy with one job. Bazarr+ gives each lease a random token, and the front
forwards ``POST /l/<token>/v1`` to the daemon's ``/v1`` when, and only when, that token is in the set Bazarr+
last pushed to it. FlareSolverr has no authentication of its own, and the nine catalog providers POST to one
configured URL with fixed headers, so the credential has to travel in the path.

The token set:

* The front starts empty and holds the set in memory. Bazarr+ replaces it as a whole with
  ``PUT /_admin/leases`` and reads its size and a digest of the set with ``GET /_admin/leases``. A revoked token
  is gone with the next push; nothing is written to disk.
* The admin path is its own listener (``ADMIN_PORT``) with its own small connection limit, and the proxy listener
  answers 401 to it. A busy proxy can therefore never starve a push, and a push is how a revoke is delivered.
* The set holds SHA-256 digests, not tokens: the push carries no token, and nothing in this process can
  hand a token back. A request's token is hashed and compared with every digest in constant time.
* The admin path answers only to ``Authorization: Bearer <secret>`` where the secret hashes to the digest in
  ``BAZARR_FRONT_ADMIN_SHA256``. The container's configuration, which anyone who can inspect the container reads,
  therefore holds a digest of the secret and never the secret.

Everything else is refused with 401 and the same body, whether the token is unknown, stale or malformed, and no
route answers without a credential (there is no health route: Docker's health check only opens the port). The
request path and the credential are never logged. Only ``POST /l/<token>/v1`` with a ``Content-Length`` up to the
body cap is forwarded, to the one configured upstream, and only the ``Content-Type`` header goes with it. The
reply is passed on only when the upstream states a ``Content-Length`` up to the response cap.

When ``BAZARR_FRONT_ALLOW_NET`` is set, a connection is served only if it arrived on an address inside that
network, and is closed before anything is read from it otherwise. The front sits on two networks, and this keeps
the daemon's side from reaching it at all.

Time is bounded in wall-clock terms: a request head must arrive within ``HEAD_DEADLINE``, a body within
``BODY_DEADLINE``, and a proxied exchange, reply included, within the configured upstream timeout. A peer that
trickles bytes therefore cannot hold a connection slot for longer than that.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time

from typing import Any, Mapping

__all__ = [
    "ADMIN_PATH",
    "ADMIN_PORT",
    "ENV_ADMIN_SHA256",
    "ENV_ADMIN_PORT",
    "ENV_ALLOW_NET",
    "ENV_MAX_BODY",
    "ENV_MAX_RESPONSE",
    "ENV_PORT",
    "ENV_UPSTREAM",
    "ENV_UPSTREAM_TIMEOUT",
    "FRONT_PORT",
    "FrontConfig",
    "FrontServer",
    "LeaseSet",
    "MAX_ADMIN_CONCURRENT",
    "MAX_CONCURRENT",
    "MAX_LEASES",
    "main",
    "token_digest",
]

FRONT_PORT = 8080
ADMIN_PORT = 8081
ADMIN_PATH = "/_admin/leases"
ENV_ADMIN_SHA256 = "BAZARR_FRONT_ADMIN_SHA256"
ENV_UPSTREAM = "BAZARR_FRONT_UPSTREAM"
ENV_PORT = "BAZARR_FRONT_PORT"
ENV_ADMIN_PORT = "BAZARR_FRONT_ADMIN_PORT"
ENV_ALLOW_NET = "BAZARR_FRONT_ALLOW_NET"
ENV_MAX_BODY = "BAZARR_FRONT_MAX_BODY"
ENV_MAX_RESPONSE = "BAZARR_FRONT_MAX_RESPONSE"
ENV_UPSTREAM_TIMEOUT = "BAZARR_FRONT_UPSTREAM_TIMEOUT"

MAX_LEASES = 256
DEFAULT_MAX_BODY = 1024 * 1024
DEFAULT_MAX_RESPONSE = 32 * 1024 * 1024
DEFAULT_UPSTREAM_TIMEOUT = 180.0
MAX_ADMIN_BODY = 64 * 1024
MAX_CONCURRENT = 16
MAX_ADMIN_CONCURRENT = 4
SOCKET_TIMEOUT = 30.0
HEAD_DEADLINE = 10.0
BODY_DEADLINE = 30.0
CHUNK = 64 * 1024

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROXY_RE = re.compile(r"/l/([^/?#]+)/v1\Z")
_UPSTREAM_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([0-9]{1,5})\Z")
_CONTENT_TYPE_RE = re.compile(r'[A-Za-z0-9!#$&^_.+/;=" -]{1,200}\Z')
_UNAUTHORIZED = b'{"error": "unauthorized"}'
_REFUSAL_LOG_INTERVAL = 60.0


def token_digest(token: str) -> bytes:
    """The SHA-256 digest of a lease token, as the set holds it."""
    return hashlib.sha256(token.encode("utf-8", "surrogateescape")).digest()


class LeaseSet:
    """The digests of the live lease tokens, replaced whole. Reads never block a replace for long."""

    def __init__(self) -> None:
        self._digests: tuple[bytes, ...] = ()

    def __len__(self) -> int:
        return len(self._digests)

    def fingerprint(self) -> str:
        """One digest of the whole set, so Bazarr+ can tell that the front holds exactly the set it pushed."""
        return hashlib.sha256(b"".join(self._digests)).hexdigest()

    def replace(self, digests) -> int:
        fresh = tuple(sorted(set(digests)))
        self._digests = fresh
        return len(fresh)

    def allows(self, token: str) -> bool:
        """Whether ``token`` is in the set. Every digest is compared, whatever the first ones say."""
        if not _TOKEN_RE.fullmatch(token):
            return False
        probe = token_digest(token)
        found = False
        for known in self._digests:
            if hmac.compare_digest(probe, known):
                found = True
        return found


class FrontConfig:
    """What the front is told at start, read from the environment. Raises ``ValueError`` on a bad value."""

    def __init__(self, *, admin_sha256: str, upstream_host: str, upstream_port: int, port: int = FRONT_PORT,
                 admin_port: int = ADMIN_PORT, allow_net: str | None = None, max_body: int = DEFAULT_MAX_BODY,
                 max_response: int = DEFAULT_MAX_RESPONSE, upstream_timeout: float = DEFAULT_UPSTREAM_TIMEOUT):
        if not _DIGEST_RE.fullmatch(admin_sha256 or ""):
            raise ValueError("the admin digest must be a lowercase sha256 hex string")
        if not 1 <= upstream_port <= 65535 or not 0 <= port <= 65535 or not 0 <= admin_port <= 65535:
            raise ValueError("a port is out of range")
        if not 1 <= max_body <= 64 * 1024 * 1024 or not 1 <= max_response <= 256 * 1024 * 1024:
            raise ValueError("a size cap is out of range")
        if not 1 <= upstream_timeout <= 900:
            raise ValueError("the upstream timeout is out of range")
        self.admin_digest = bytes.fromhex(admin_sha256)
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.port = port
        self.admin_port = admin_port
        self.allow_net = ipaddress.ip_network(allow_net, strict=False) if allow_net else None
        self.max_body = max_body
        self.max_response = max_response
        self.upstream_timeout = float(upstream_timeout)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "FrontConfig":
        env = os.environ if environ is None else environ
        found = _UPSTREAM_RE.fullmatch(env.get(ENV_UPSTREAM, ""))
        if not found:
            raise ValueError("the upstream must be host:port")
        return cls(
            admin_sha256=env.get(ENV_ADMIN_SHA256, ""),
            upstream_host=found.group(1),
            upstream_port=int(found.group(2)),
            port=int(env.get(ENV_PORT) or FRONT_PORT),
            admin_port=int(env.get(ENV_ADMIN_PORT) or ADMIN_PORT),
            allow_net=env.get(ENV_ALLOW_NET) or None,
            max_body=int(env.get(ENV_MAX_BODY) or DEFAULT_MAX_BODY),
            max_response=int(env.get(ENV_MAX_RESPONSE) or DEFAULT_MAX_RESPONSE),
            upstream_timeout=float(env.get(ENV_UPSTREAM_TIMEOUT) or DEFAULT_UPSTREAM_TIMEOUT),
        )


class _Deadline:
    """A wall-clock limit: when it passes, the sockets it was given are shut down, which wakes whatever waits on them.

    A socket timeout restarts with every byte, so a peer that trickles data never trips it. This does not restart.
    """

    def __init__(self, seconds: float, *sockets) -> None:
        self._sockets = sockets
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        for getter in self._sockets:
            try:
                sock = getter()
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def cancel(self) -> None:
        self._timer.cancel()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    timeout = SOCKET_TIMEOUT
    server_version = "front"
    sys_version = ""

    def handle(self) -> None:
        self._head_deadline = _Deadline(HEAD_DEADLINE, lambda: self.request)
        try:
            super().handle()
        finally:
            self._head_deadline.cancel()

    def log_message(self, *args: Any) -> None:
        # Never a request line: the path holds a credential.
        return None

    def log_error(self, *args: Any) -> None:
        return None

    def version_string(self) -> str:
        return "front"

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._reply(code, b"{}")

    # ---------------------------------------------------------------- replies

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (OSError, ValueError):
            pass
        self.close_connection = True

    def _unauthorized(self) -> None:
        self.server.note_refusal(self.client_address[0])  # type: ignore[attr-defined]
        self._reply(401, _UNAUTHORIZED)

    # ---------------------------------------------------------------- dispatch

    def _handle(self) -> None:
        self._head_deadline.cancel()
        server: FrontServer = self.server  # type: ignore[assignment]
        target = self.path
        if server.role == ROLE_ADMIN:
            if target != ADMIN_PATH:
                return self._unauthorized()
            return self._admin(server)
        found = _PROXY_RE.fullmatch(target)
        if found is None or not server.leases.allows(found.group(1)):
            return self._unauthorized()
        if self.command != "POST":
            return self._reply(405, b"{}")
        self._proxy(server)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _handle

    def _bearer_ok(self, server: "FrontServer") -> bool:
        header = self.headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value:
            return False
        probe = hashlib.sha256(value.strip().encode("latin-1", "replace")).digest()
        return hmac.compare_digest(probe, server.config.admin_digest)

    # ---------------------------------------------------------------- admin

    def _admin(self, server: "FrontServer") -> None:
        if not self._bearer_ok(server):
            return self._unauthorized()
        if self.command == "GET":
            return self._reply(200, json.dumps({"count": len(server.leases),
                                                "fingerprint": server.leases.fingerprint()}).encode())
        if self.command != "PUT":
            return self._reply(405, b"{}")
        body = self._read_body(MAX_ADMIN_BODY)
        if body is None:
            return
        try:
            document = json.loads(body)
            tokens = document["tokens"]
            if (not isinstance(tokens, list) or len(tokens) > MAX_LEASES
                    or not all(isinstance(item, str) and _DIGEST_RE.fullmatch(item) for item in tokens)):
                raise ValueError("bad lease set")
        except (ValueError, KeyError, TypeError):
            return self._reply(400, b'{"error": "bad request"}')
        count = server.leases.replace(bytes.fromhex(item) for item in tokens)
        self._reply(200, json.dumps({"count": count}).encode())

    # ---------------------------------------------------------------- proxy

    def _read_body(self, cap: int) -> bytes | None:
        """The request body, or None after a reply was sent. Needs a ``Content-Length`` within ``cap``."""
        if self.headers.get("Transfer-Encoding"):
            self._reply(411, b"{}")
            return None
        raw = self.headers.get("Content-Length")
        if raw is None or not (raw.isascii() and raw.isdigit()) or len(raw) > 10:
            self._reply(411, b"{}")
            return None
        length = int(raw)
        if length > cap:
            self._reply(413, b"{}")
            return None
        deadline = _Deadline(BODY_DEADLINE, lambda: self.request)
        try:
            data = self.rfile.read(length) if length else b""
        except (OSError, ValueError):
            self.close_connection = True
            return None
        finally:
            deadline.cancel()
        if len(data) != length:
            self.close_connection = True
            return None
        return data

    def _proxy(self, server: "FrontServer") -> None:
        config = server.config
        body = self._read_body(config.max_body)
        if body is None:
            return
        content_type = self.headers.get("Content-Type") or "application/json"
        if not _CONTENT_TYPE_RE.fullmatch(content_type):
            content_type = "application/json"
        connection = http.client.HTTPConnection(config.upstream_host, config.upstream_port,
                                                timeout=config.upstream_timeout)
        connection.set_debuglevel(0)
        # The whole exchange, reply included, is bounded: a daemon or a client that dribbles cannot hold the slot.
        deadline = _Deadline(config.upstream_timeout, lambda: self.request, lambda: connection.sock)
        try:
            try:
                connection.request("POST", "/v1", body=body, headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                    "Host": "%s:%d" % (config.upstream_host, config.upstream_port),
                    "Connection": "close",
                })
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                return self._reply(502, b'{"error": "upstream unavailable"}')
            length = response.getheader("Content-Length")
            if not 200 <= response.status <= 599:
                return self._reply(502, b'{"error": "upstream reply refused"}')
            if length is None or not (length.isascii() and length.isdigit()) or len(length) > 12 or int(length) > config.max_response:
                return self._reply(502, b'{"error": "upstream reply refused"}')
            remaining = int(length)
            upstream_type = response.getheader("Content-Type") or "application/json"
            if not _CONTENT_TYPE_RE.fullmatch(upstream_type):
                upstream_type = "application/json"
            try:
                self.send_response(response.status)
                self.send_header("Content-Type", upstream_type)
                self.send_header("Content-Length", str(remaining))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while remaining > 0:
                    chunk = response.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (OSError, ValueError, http.client.HTTPException):
                pass
            # A short body ends the connection, and the client sees the length it was promised go unmet.
            self.close_connection = True
        finally:
            deadline.cancel()
            connection.close()


ROLE_PROXY = "proxy"
ROLE_ADMIN = "admin"


class FrontServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """One listener: the ``proxy`` role forwards leased requests, the ``admin`` role takes lease pushes.

    Each has its own connection limit (``MAX_CONCURRENT`` and ``MAX_ADMIN_CONCURRENT``); a connection beyond it
    is answered 503 at once and closed. They share one ``LeaseSet``.
    """

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], config: FrontConfig, leases: LeaseSet | None = None,
                 *, role: str = ROLE_PROXY, clock=time.monotonic):
        if role not in (ROLE_PROXY, ROLE_ADMIN):
            raise ValueError("role must be proxy or admin")
        self.role = role
        self.config = config
        self.leases = leases if leases is not None else LeaseSet()
        self._slots = threading.BoundedSemaphore(MAX_ADMIN_CONCURRENT if role == ROLE_ADMIN else MAX_CONCURRENT)
        self._clock = clock
        self._refusal_lock = threading.Lock()
        self._refused = 0
        self._last_logged = clock() - _REFUSAL_LOG_INTERVAL
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer's own bind resolves the address back to a name, which can stall in a container.
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]

    def on_allowed_network(self, connection: socket.socket) -> bool:
        net = self.config.allow_net
        if net is None:
            return True
        try:
            local = ipaddress.ip_address(connection.getsockname()[0])
        except (OSError, ValueError, IndexError):
            return False
        return local in net

    def verify_request(self, request, client_address) -> bool:
        """Drop a connection from outside the allowed network before a byte of it is read."""
        return self.on_allowed_network(request)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(2.0)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        # A client that hangs up mid-reply is routine. Nothing else is printed either: a traceback could
        # carry a request line.
        return None

    def note_refusal(self, peer: str) -> None:
        """Count a refused request and log a summary at most once a minute: the count and a peer, never a path."""
        with self._refusal_lock:
            self._refused += 1
            now = self._clock()
            if now - self._last_logged < _REFUSAL_LOG_INTERVAL:
                return
            count, self._refused, self._last_logged = self._refused, 0, now
        print("front: %d unauthorized request(s), the latest from %s" % (count, peer), file=sys.stderr, flush=True)


def main(environ: Mapping[str, str] | None = None) -> int:
    try:
        config = FrontConfig.from_env(environ)
    except ValueError as error:
        print("front: bad configuration: %s" % error, file=sys.stderr, flush=True)
        return 2
    # Smaller header limits than the library's, in this process only: a request head is read before any
    # credential is checked.
    http.client._MAXLINE = 16384
    http.client._MAXHEADERS = 32
    leases = LeaseSet()
    proxy = FrontServer(("0.0.0.0", config.port), config, leases, role=ROLE_PROXY)
    admin = FrontServer(("0.0.0.0", config.admin_port), config, leases, role=ROLE_ADMIN)

    def stop(_signum, _frame) -> None:
        for server in (proxy, admin):
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("front: listening on %d and %d" % (config.port, config.admin_port), file=sys.stderr, flush=True)
    threading.Thread(target=admin.serve_forever, name="front-admin", daemon=True).start()
    proxy.serve_forever()
    admin.shutdown()
    proxy.server_close()
    admin.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

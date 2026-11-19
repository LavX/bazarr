# coding=utf-8
"""A small Docker Engine API client for managed containers.

Plain ``http.client`` over a Unix socket (the default) or a TCP address, so a
``tecnativa/docker-socket-proxy`` in front of the socket works too. No new
dependency, no Docker SDK.

Whoever can ``POST /containers/create`` on a Docker daemon can start a
privileged container with ``/`` mounted, so access to the daemon is
root-equivalent. A socket proxy that filters by path cannot see the request body and
does not change that. This client therefore narrows what Bazarr+ can do with the
daemon, as a bound on its own bugs and not as a boundary against a compromised process:

* Only the routes in ``ALLOWED_ROUTES`` are ever sent: the ones a proxy that exposes
  ``CONTAINERS``, ``NETWORKS``, ``IMAGES`` and ``POST`` lets through, and nothing else
  (no exec, volumes, swarm, system or build calls).
* Every identifier that goes into a path is checked first, so a name cannot add a segment.
* ``check_create_body`` refuses a container configuration that asks for privileged mode,
  host namespaces, bind mounts, devices, extra capabilities or published ports. It is
  the one place that reads the body a path filter cannot read.
* Registry credentials are never sent: only public images can be pulled.

The endpoint is ``DOCKER_HOST`` (``unix://``, ``tcp://`` or ``http://``, plain HTTP) or
the default socket. TLS and ssh endpoints are refused.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse

from dataclasses import dataclass
from typing import Any, Mapping

from secret_store.scrub import scrub_text

__all__ = [
    "ALLOWED_ROUTES",
    "API_VERSION_CEILING",
    "API_VERSION_FLOOR",
    "DEFAULT_SOCKET",
    "DockerApiError",
    "DockerCancelled",
    "DockerClient",
    "DockerNotFound",
    "DockerRefused",
    "DockerUnavailable",
    "Endpoint",
    "check_create_body",
    "endpoint_from_env",
    "parse_endpoint",
]

DEFAULT_SOCKET = "/var/run/docker.sock"
# The API versions this client speaks. The floor is the oldest it accepts (Docker 20.10). The ceiling is the
# newest version whose routes and fields these calls were written against (Docker 25): the version used is the
# daemon's own, capped at the ceiling. A newer daemon is therefore spoken to at the ceiling, which Docker keeps
# supporting down to the daemon's own minimum. A daemon whose minimum is above the ceiling cannot be used and
# is refused with that reason (see ``_too_old``).
API_VERSION_CEILING = (1, 44)
API_VERSION_FLOOR = (1, 41)

MIB = 1024 * 1024
# The most bytes one answer may hold. A pull's progress stream is read in lines and only counted.
MAX_BODY_BYTES = 8 * MIB
MAX_STREAM_BYTES = 32 * MIB
MAX_MESSAGE_CHARS = 300
CONNECT_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30.0

# A container, network or image identifier or name as Docker accepts it, with no character that
# could add a path segment or a query.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_API_VERSION_RE = re.compile(r"(?P<major>[0-9]{1,2})\.(?P<minor>[0-9]{1,3})\Z")
# The longest image name a manifest may carry (255) plus "@sha256:" and a digest, so a reference the manifest
# accepts is a reference this client can ask about.
MAX_IMAGE_REF_CHARS = 255 + len("@sha256:") + 64
_IMAGE_SEGMENT_RE = re.compile(r"[a-z0-9][a-z0-9._:-]*\Z")

_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
# (method, path after the version prefix). The pull route is the one streaming answer.
ALLOWED_ROUTES: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (method, re.compile(pattern)) for method, pattern in (
        ("GET", r"^/_ping$"),
        ("GET", r"^/containers/json$"),
        ("POST", r"^/containers/create$"),
        ("GET", rf"^/containers/{_ID}/json$"),
        ("POST", rf"^/containers/{_ID}/start$"),
        ("POST", rf"^/containers/{_ID}/stop$"),
        ("DELETE", rf"^/containers/{_ID}$"),
        ("GET", r"^/images/(?!.*(?:\.\.|//))[a-z0-9][a-z0-9._:/@-]{0,%d}/json$" % (MAX_IMAGE_REF_CHARS - 1)),
        ("POST", r"^/images/create$"),
        ("GET", r"^/networks$"),
        ("POST", r"^/networks/create$"),
        ("GET", rf"^/networks/{_ID}$"),
        ("DELETE", rf"^/networks/{_ID}$"),
        ("POST", rf"^/networks/{_ID}/connect$"),
        ("POST", rf"^/networks/{_ID}/disconnect$"),
    )
)


class DockerApiError(Exception):
    """The daemon answered with an error. ``status`` is the HTTP status; ``message`` is scrubbed and short."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message


class DockerNotFound(DockerApiError):
    """The daemon has no such container, network or image (HTTP 404)."""


class DockerUnavailable(DockerApiError):
    """The daemon could not be reached, or answered something that is not the Docker API."""


class DockerCancelled(DockerApiError):
    """The caller cancelled the call (a stop during a long pull)."""


class DockerRefused(DockerApiError):
    """This client refused to send the request. Nothing reached the daemon."""


@dataclass(frozen=True)
class Endpoint:
    """Where the daemon is: a Unix socket path, or a host and port."""

    kind: str  # "unix" or "tcp"
    path: str | None = None
    host: str | None = None
    port: int | None = None

    def describe(self) -> str:
        return f"unix://{self.path}" if self.kind == "unix" else f"tcp://{self.host}:{self.port}"


def parse_endpoint(value: str | None) -> Endpoint:
    """An endpoint from a ``DOCKER_HOST`` style value; the default socket when it is empty."""
    text = (value or "").strip()
    if not text:
        return Endpoint("unix", path=DEFAULT_SOCKET)
    if text.startswith("/"):
        return Endpoint("unix", path=text)
    parsed = urllib.parse.urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme == "unix":
        if not parsed.path.startswith("/") or parsed.netloc:
            raise DockerRefused("a unix endpoint must look like unix:///path/to/docker.sock")
        return Endpoint("unix", path=parsed.path)
    if scheme in ("tcp", "http"):
        try:
            port = parsed.port
        except ValueError:
            port = None
        if not parsed.hostname or port is None or parsed.path not in ("", "/") or parsed.username or parsed.password:
            raise DockerRefused("a tcp endpoint must look like tcp://host:port")
        return Endpoint("tcp", host=parsed.hostname, port=port)
    raise DockerRefused("the Docker endpoint must be a unix socket or a plain tcp address")


def endpoint_from_env(environ: Mapping[str, str]) -> Endpoint | None:
    """The configured endpoint, or None when neither ``DOCKER_HOST`` nor the default socket is there.

    Docker access is opt-in: the user adds the socket as a volume, or points ``DOCKER_HOST`` at a proxy.
    With neither there is nothing to talk to, and no connection is tried.
    """
    value = environ.get("DOCKER_HOST")
    if value is not None and value.strip():
        return parse_endpoint(value)
    return Endpoint("unix", path=DEFAULT_SOCKET) if os.path.exists(DEFAULT_SOCKET) else None


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


_MIN_VERSION_RE = re.compile(r"[Mm]inimum supported API version is (?P<minimum>[0-9]{1,2}\.[0-9]{1,3})")


def _too_old(message: str) -> str | None:
    """The reason to show when the daemon says our API version is below its minimum, else None."""
    found = _MIN_VERSION_RE.search(message)
    if not found or "too old" not in message:
        return None
    needed = _version_tuple(found.group("minimum"))
    if needed is not None and needed <= API_VERSION_CEILING:
        return None
    return "this Docker daemon needs API %s or newer, and this Bazarr+ speaks up to %d.%d" % (
        found.group("minimum"), *API_VERSION_CEILING)


class _TcpConnection(http.client.HTTPConnection):
    """A plain TCP connection. A class of its own so a test can refuse every real address."""


def _clean(text: Any) -> str:
    return scrub_text(str(text)).replace("\n", " ").strip()[:MAX_MESSAGE_CHARS]


def _version_tuple(value: str | None) -> tuple[int, int] | None:
    match = _API_VERSION_RE.fullmatch(value or "")
    return (int(match.group("major")), int(match.group("minor"))) if match else None


_FORBIDDEN_HOST_MODES = ("host", "container:")


def check_create_body(body: Mapping[str, Any]) -> None:
    """Refuse a container configuration a managed container must never have.

    Raises ``DockerRefused`` naming the first field that breaks a rule. It cannot make a configuration
    safe, only keep the obvious escapes out of anything this client sends.
    """
    host = body.get("HostConfig")
    if not isinstance(host, dict):
        raise DockerRefused("a container needs a HostConfig that states its limits")
    if host.get("Privileged"):
        raise DockerRefused("privileged containers are never created")
    for key in ("Binds", "Devices", "DeviceRequests", "CapAdd", "VolumesFrom", "Links", "Sysctls",
                "CgroupParent", "ExtraHosts", "GroupAdd", "DeviceCgroupRules", "Runtime"):
        if host.get(key):
            raise DockerRefused(f"HostConfig.{key} is not allowed on a managed container")
    for key in ("PidMode", "IpcMode", "UsernsMode", "UTSMode", "CgroupnsMode"):
        value = str(host.get(key) or "")
        if value == "host" or value.startswith("container:"):
            raise DockerRefused(f"HostConfig.{key} must not share a namespace with the host or a container")
    network = str(host.get("NetworkMode") or "")
    if not network or network in ("default", "bridge", "none") or any(
            network.startswith(prefix) for prefix in _FORBIDDEN_HOST_MODES):
        raise DockerRefused("a managed container joins one named network, never the host, default or another container")
    if host.get("PublishAllPorts") or host.get("PortBindings"):
        raise DockerRefused("a managed container publishes no ports")
    if body.get("ExposedPorts"):
        raise DockerRefused("a managed container publishes no ports")
    for option in host.get("SecurityOpt") or ():
        text = str(option).lower().replace(":", "=", 1)
        if "unconfined" in text or text.startswith(("systempaths", "seccomp=", "apparmor=", "label=disable")):
            raise DockerRefused("a managed container keeps the default security profiles")
        if text.startswith("no-new-privileges") and text.split("=", 1)[-1] not in ("", "true", "no-new-privileges"):
            raise DockerRefused("a managed container never allows new privileges")
    for key in ("MaskedPaths", "ReadonlyPaths"):
        if key in host and not host[key]:
            raise DockerRefused(f"HostConfig.{key} must keep the daemon's protected paths")
    ipc = str(host.get("IpcMode") or "")
    if ipc not in ("", "private", "none"):
        raise DockerRefused("HostConfig.IpcMode must be private or none")
    endpoints = ((body.get("NetworkingConfig") or {}).get("EndpointsConfig")) or {}
    if not isinstance(endpoints, dict) or set(endpoints) - {network}:
        raise DockerRefused("a managed container joins only its one private network")
    for mount in host.get("Mounts") or ():
        if not isinstance(mount, dict) or mount.get("Type") != "tmpfs":
            raise DockerRefused("a managed container mounts nothing from the host")
    if host.get("Tmpfs") is not None and not isinstance(host.get("Tmpfs"), dict):
        raise DockerRefused("HostConfig.Tmpfs must be an object")
    for key in ("Volumes", "VolumeDriver"):
        if body.get(key) or host.get(key):
            raise DockerRefused(f"{key} is not allowed on a managed container")
    if not host.get("Memory") or host.get("Memory") != host.get("MemorySwap"):
        raise DockerRefused("a managed container needs a memory limit and no swap beyond it")
    pids = host.get("PidsLimit")
    if isinstance(pids, bool) or not isinstance(pids, int) or pids <= 0:
        raise DockerRefused("a managed container needs a process limit")


class DockerClient:
    """Calls the routes in ``ALLOWED_ROUTES``. Stateless apart from the negotiated version; safe across threads.

    ``connection_factory(timeout)`` returns an ``http.client`` connection; tests pass their own.
    """

    def __init__(self, endpoint: Endpoint, *, connection_factory=None, timeout: float = REQUEST_TIMEOUT):
        self.endpoint = endpoint
        self.timeout = timeout
        self._factory = connection_factory
        self._version: tuple[int, int] | None = None

    # ------------------------------------------------------------ transport

    def _connection(self, timeout: float):
        if self._factory is not None:
            return self._factory(timeout)
        if self.endpoint.kind == "unix":
            return _UnixConnection(self.endpoint.path, timeout)
        return _TcpConnection(self.endpoint.host, self.endpoint.port, timeout=timeout)

    @property
    def api_version(self) -> str | None:
        return None if self._version is None else "%d.%d" % self._version

    def _prefix(self, path: str) -> str:
        if path == "/_ping" or self._version is None:
            return path
        return "/v%d.%d%s" % (self._version[0], self._version[1], path)

    @staticmethod
    def _check_route(method: str, path: str) -> None:
        for allowed, pattern in ALLOWED_ROUTES:
            if allowed == method and pattern.fullmatch(path):
                return
        raise DockerRefused(f"{method} {path} is not a Docker route this client uses")

    def _send(self, method: str, path: str, *, query: Mapping[str, str] | None = None, body: Any = None,
              timeout: float | None = None, cancel: threading.Event | None = None):
        """Open a connection, send, and return ``(connection, response)``. The caller reads and closes.

        With ``cancel``, the connection is made first (within ``CONNECT_TIMEOUT``) and a watcher is running before
        the request is sent, so setting the event shuts the socket under whatever is blocked: the send or the wait
        for the response headers. The caller sets ``connection.cancel_finished`` when it is done with the connection.
        """
        self._check_route(method, path)
        target = self._prefix(path)
        if query:
            target += "?" + urllib.parse.urlencode(query)
        headers = {"User-Agent": "bazarr-plus", "Accept": "application/json", "Connection": "close"}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        wait = self.timeout if timeout is None else timeout
        connection = self._connection(wait)
        finished = threading.Event()
        connection.cancel_finished = finished
        try:
            if cancel is not None:
                if cancel.is_set():
                    raise DockerCancelled("the call was cancelled")
                connection.timeout = min(wait, CONNECT_TIMEOUT)
                connection.connect()
                connection.sock.settimeout(wait)
                # A response that will close detaches the socket from the connection; keep it for a cancel.
                connection.cancel_socket = connection.sock
                threading.Thread(target=self._watch_cancel, args=(cancel, finished, connection),
                                 name="docker-cancel", daemon=True).start()
            connection.request(method, target, body=payload, headers=headers)
            return connection, connection.getresponse()
        except DockerCancelled:
            finished.set()
            connection.close()
            raise
        except (OSError, http.client.HTTPException) as error:
            finished.set()
            connection.close()
            if cancel is not None and cancel.is_set():
                raise DockerCancelled("the call was cancelled") from error
            raise DockerUnavailable(f"the Docker daemon could not be reached: {type(error).__name__}") from error

    @staticmethod
    def _raise_for(status: int, raw: bytes) -> None:
        message = ""
        try:
            parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            if isinstance(parsed, dict):
                message = str(parsed.get("message") or "")
        except ValueError:
            message = raw.decode("utf-8", "replace")
        message = _clean(message) or f"HTTP {status}"
        too_old = _too_old(message)
        if too_old:
            raise DockerUnavailable(too_old, status=status)
        if status == 404:
            raise DockerNotFound(message, status=status)
        raise DockerApiError(message, status=status)

    def _read(self, response, limit: int) -> bytes:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise DockerUnavailable("the Docker daemon answered with more data than expected")
        return data

    def _call(self, method: str, path: str, *, query=None, body=None, timeout=None,
              ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        connection, response = self._send(method, path, query=query, body=body, timeout=timeout)
        try:
            try:
                raw = self._read(response, MAX_BODY_BYTES)
            except (OSError, http.client.HTTPException) as error:
                raise DockerUnavailable(
                    f"the Docker daemon dropped the connection: {type(error).__name__}") from error
            if response.status not in ok:
                self._raise_for(response.status, raw)
            if not raw or response.status == 204:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError as error:
                raise DockerUnavailable("the Docker daemon answered with something that is not JSON") from error
        finally:
            connection.close()

    # ------------------------------------------------------------ daemon

    def ping(self) -> str:
        """Check the daemon answers and settle the API version. Returns the version used, such as ``1.44``.

        Only ``/_ping`` is used: its ``API-Version`` header names the daemon's newest version, and a socket
        proxy answers it without any permission.
        """
        connection, response = self._send("GET", "/_ping", timeout=min(self.timeout, CONNECT_TIMEOUT))
        try:
            try:
                raw = self._read(response, 1024)
            except (OSError, http.client.HTTPException) as error:
                raise DockerUnavailable(
                    f"the Docker daemon dropped the connection: {type(error).__name__}") from error
            if response.status != 200 or raw.strip() != b"OK":
                raise DockerUnavailable("that address does not answer as a Docker daemon")
            offered = _version_tuple(response.getheader("API-Version")) or API_VERSION_FLOOR
        finally:
            connection.close()
        if offered < API_VERSION_FLOOR:
            raise DockerUnavailable("this Docker daemon is older than API %d.%d" % API_VERSION_FLOOR)
        self._version = min(offered, API_VERSION_CEILING)
        return self.api_version  # type: ignore[return-value]

    # ------------------------------------------------------------ containers

    @staticmethod
    def _name(value: str, what: str = "name") -> str:
        if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
            raise DockerRefused(f"{what} {str(value)[:40]!r} is not a valid Docker name or id")
        return value

    @staticmethod
    def _image(value: str) -> str:
        if not isinstance(value, str) or len(value) > MAX_IMAGE_REF_CHARS:
            raise DockerRefused("that is not a valid image reference")
        name, _, digest = value.partition("@")
        segments = name.split("/")
        if (digest and not _DIGEST_RE.fullmatch(digest)) or not all(_IMAGE_SEGMENT_RE.fullmatch(part)
                                                                    for part in segments) or ".." in name:
            raise DockerRefused("that is not a valid image reference")
        return value

    def list_containers(self, *, labels: Mapping[str, str] | None = None, network: str | None = None,
                        include_stopped: bool = True) -> list[dict]:
        filters: dict[str, list[str]] = {}
        if labels:
            filters["label"] = [f"{key}={value}" for key, value in sorted(labels.items())]
        if network:
            filters["network"] = [self._name(network, "network")]
        query = {"all": "1" if include_stopped else "0"}
        if filters:
            query["filters"] = json.dumps(filters, separators=(",", ":"))
        result = self._call("GET", "/containers/json", query=query)
        return result if isinstance(result, list) else []

    def inspect_container(self, ident: str) -> dict:
        result = self._call("GET", f"/containers/{self._name(ident, 'container')}/json")
        if not isinstance(result, dict):
            raise DockerUnavailable("the Docker daemon answered with something unexpected")
        return result

    def create_container(self, name: str, body: Mapping[str, Any]) -> tuple[str, list[str]]:
        """Create a container. Returns ``(id, warnings)``. The body is checked by ``check_create_body`` first."""
        check_create_body(body)
        result = self._call("POST", "/containers/create", query={"name": self._name(name)}, body=dict(body))
        if not isinstance(result, dict) or not isinstance(result.get("Id"), str):
            raise DockerUnavailable("the Docker daemon answered with something unexpected")
        warnings = result.get("Warnings")
        return result["Id"], [str(item) for item in warnings] if isinstance(warnings, list) else []

    def start_container(self, ident: str) -> None:
        self._call("POST", f"/containers/{self._name(ident, 'container')}/start", ok=(204, 304))

    def stop_container(self, ident: str, timeout: int = 10) -> None:
        """Send SIGTERM, then SIGKILL after ``timeout`` seconds. A container that is already stopped is fine."""
        seconds = max(0, min(int(timeout), 600))
        self._call("POST", f"/containers/{self._name(ident, 'container')}/stop", query={"t": str(seconds)},
                   timeout=seconds + 15.0, ok=(204, 304))

    def remove_container(self, ident: str, *, force: bool = True) -> None:
        """Remove a container and its anonymous volumes. A missing container is not an error."""
        try:
            self._call("DELETE", f"/containers/{self._name(ident, 'container')}",
                       query={"force": "1" if force else "0", "v": "1"}, ok=(204,))
        except DockerNotFound:
            pass

    # ------------------------------------------------------------ images

    def inspect_image(self, reference: str) -> dict:
        result = self._call("GET", f"/images/{urllib.parse.quote(self._image(reference), safe='/@:')}/json")
        if not isinstance(result, dict):
            raise DockerUnavailable("the Docker daemon answered with something unexpected")
        return result

    def pull_image(self, repository: str, digest: str, *, deadline: float = 900.0,
                   idle_timeout: float = 120.0, cancel: threading.Event | None = None) -> None:
        """Pull ``repository@digest``. Only a digest is accepted, never a tag.

        The daemon checks the content against the digest. The progress stream is read and dropped: only an error
        in it matters. ``deadline`` bounds the whole pull; ``idle_timeout`` the wait for the next progress line.
        Setting ``cancel`` from another thread ends the pull within a fraction of a second (the connection is
        shut down under the blocked read) and raises ``DockerCancelled``.
        """
        if not _DIGEST_RE.fullmatch(str(digest)):
            raise DockerRefused("an image is pulled by a sha256 digest, never by a tag")
        reference = f"{self._image(repository)}@{digest}"
        if "@" in repository or ":" in repository.rsplit("/", 1)[-1]:
            raise DockerRefused("the image name must carry neither a tag nor a digest")
        connection, response = self._send("POST", "/images/create", query={"fromImage": reference},
                                          timeout=idle_timeout, cancel=cancel)
        finished = connection.cancel_finished
        try:
            if response.status != 200:
                self._raise_for(response.status, self._read(response, MAX_BODY_BYTES))
            stop = time.monotonic() + deadline
            seen = 0
            try:
                while True:
                    line = response.readline(MIB)
                    if not line:
                        break
                    seen += len(line)
                    if seen > MAX_STREAM_BYTES or time.monotonic() > stop:
                        raise DockerUnavailable("the image pull did not finish in time")
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and (event.get("error") or event.get("errorDetail")):
                        detail = event.get("error") or (event.get("errorDetail") or {}).get("message")
                        raise DockerApiError(_clean(detail) or "the image pull failed")
            except (OSError, http.client.HTTPException) as error:
                if cancel is not None and cancel.is_set():
                    raise DockerCancelled("the image pull was cancelled") from error
                raise DockerUnavailable(f"the image pull was interrupted: {type(error).__name__}") from error
            if cancel is not None and cancel.is_set():
                # A shut-down connection reads as the end of the stream, not as an error.
                raise DockerCancelled("the image pull was cancelled")
        finally:
            finished.set()
            connection.close()

    @staticmethod
    def _watch_cancel(cancel: threading.Event, finished: threading.Event, connection) -> None:
        while not finished.is_set():
            if cancel.wait(0.1):
                sock = getattr(connection, "cancel_socket", None)
                if sock is not None and not finished.is_set():
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return

    # ------------------------------------------------------------ networks

    def list_networks(self, *, name: str | None = None, labels: Mapping[str, str] | None = None) -> list[dict]:
        filters: dict[str, list[str]] = {}
        if name:
            filters["name"] = [self._name(name, "network")]
        if labels:
            filters["label"] = [f"{key}={value}" for key, value in sorted(labels.items())]
        query = {"filters": json.dumps(filters, separators=(",", ":"))} if filters else None
        result = self._call("GET", "/networks", query=query)
        return result if isinstance(result, list) else []

    def inspect_network(self, ident: str) -> dict:
        result = self._call("GET", f"/networks/{self._name(ident, 'network')}")
        if not isinstance(result, dict):
            raise DockerUnavailable("the Docker daemon answered with something unexpected")
        return result

    def create_network(self, name: str, *, labels: Mapping[str, str], internal: bool = False) -> str:
        """Create a user-defined bridge network, no IPv6, with outbound access unless ``internal``. Returns its id."""
        body = {
            "Name": self._name(name, "network"),
            "Driver": "bridge",
            "Internal": bool(internal),
            "EnableIPv6": False,
            "Attachable": False,
            "CheckDuplicate": True,
            "Labels": dict(labels),
        }
        result = self._call("POST", "/networks/create", body=body)
        if not isinstance(result, dict) or not isinstance(result.get("Id"), str):
            raise DockerUnavailable("the Docker daemon answered with something unexpected")
        return result["Id"]

    def remove_network(self, ident: str) -> None:
        try:
            self._call("DELETE", f"/networks/{self._name(ident, 'network')}", ok=(204,))
        except DockerNotFound:
            pass

    def connect_network(self, network: str, container: str, *, aliases: tuple[str, ...] = ()) -> None:
        body: dict[str, Any] = {"Container": self._name(container, "container")}
        if aliases:
            body["EndpointConfig"] = {"Aliases": [self._name(alias, "alias") for alias in aliases]}
        self._call("POST", f"/networks/{self._name(network, 'network')}/connect", body=body, ok=(200,))

    def disconnect_network(self, network: str, container: str, *, force: bool = False) -> None:
        body = {"Container": self._name(container, "container"), "Force": bool(force)}
        try:
            self._call("POST", f"/networks/{self._name(network, 'network')}/disconnect", body=body, ok=(200,))
        except DockerNotFound:
            pass

# coding=utf-8
"""The OCI daemon runtime: a managed container Bazarr+ starts, watches and removes.

This is the runtime behind ``(oci, daemon)`` plugins such as FlareSolverr. It plugs into
``WorkerPool`` through the ``WorkerRuntime`` seam (see ``oci_worker_definition``), so the pool's
states, crash-loop breaker, idle stop and process cap apply unchanged. What it adds is the container
lifecycle on a Docker daemon the user has opted into (a socket volume, or ``DOCKER_HOST``).

Offered only when all of these hold (``check_daemon_host``): Bazarr+ runs in a container, a Docker
endpoint is configured and answers, the daemon knows the container Bazarr+ runs in (so it is the same
host, and the private network can be shared), and that container is not on the host network.

What a managed container is:

* Run from ``image@sha256:<index digest>``, never a tag. The image is pulled by digest when it is not
  local, and ``RepoDigests`` of the local image must name that digest before anything is created.
* Alone on a private bridge network of its own. Bazarr+ itself is not on it, so a plugin worker, which
  only has Bazarr+'s network, cannot reach the container. The authenticating front that reaches it later
  joins that network and Bazarr+'s side. Nothing else may be attached to it.
* No published ports, no bind mounts, no devices, no privileged mode, no host namespaces, all
  capabilities dropped, ``no-new-privileges``, a read-only root filesystem with small tmpfs mounts, a hard
  memory cap with no swap, a process cap, bounded logs, no restart policy (the pool's breaker decides).
* Labelled as Bazarr+'s own (``MANAGED_LABEL``, ``OWNER_LABEL``, ``PLUGIN_LABEL``, ``ROLE_LABEL``). Every removal
  checks those labels on what the daemon listed, so a container Bazarr+ did not create is never touched.
* Inspected after creation and before start: the daemon's own record of the container must match what was
  asked, or the container is removed and the start fails.

The container is removed whenever the pool stops it (idle, drain, uninstall, failure): FlareSolverr keeps
no state, so a start is always from the pinned image. ``cleanup_orphans`` removes what a crash left behind
at boot, ``remove_plugin`` is the call an uninstall makes.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import threading
import time

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..plugin_manifest import OCI, OCI_IMAGE_RE, OCI_INDEX_DIGEST_RE, PLUGIN_ID_RE, PluginManifest
from .docker_api import (
    DockerApiError,
    DockerClient,
    DockerNotFound,
    DockerRefused,
    endpoint_from_env,
)
from .errors import RequestError, SessionClosed
from .frames import Codes
from .pool import WorkerDefinition
from .session import RequestResult

logger = logging.getLogger(__name__)

__all__ = [
    "CleanupReport",
    "ContainerSpec",
    "DAEMON_ALIAS",
    "DaemonEndpoint",
    "HealthSpec",
    "HostCheck",
    "MANAGED_LABEL",
    "OWNER_LABEL",
    "OciDaemonRuntime",
    "OciError",
    "OciUnavailable",
    "PLUGIN_LABEL",
    "ROLE_DAEMON",
    "ROLE_LABEL",
    "cleanup_orphans",
    "check_daemon_host",
    "container_config",
    "container_name",
    "flaresolverr_spec",
    "load_owner_id",
    "network_name",
    "new_owner_id",
    "oci_worker_definition",
    "offered_runtimes",
    "own_container_id",
    "remove_plugin",
]

MIB = 1024 * 1024
GIB = 1024 * MIB

MANAGED_LABEL = "bazarr-plus.managed"
OWNER_LABEL = "bazarr-plus.owner"
PLUGIN_LABEL = "bazarr-plus.plugin"
ROLE_LABEL = "bazarr-plus.role"
ROLE_DAEMON = "daemon"
# The name a daemon answers to on its private network, besides its container name.
DAEMON_ALIAS = "daemon"

OWNER_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
_MOUNTINFO_ID_RE = re.compile(r"/containers/([0-9a-f]{64})/")

DEFAULT_MEMORY_BYTES = 1536 * MIB
MIN_MEMORY_BYTES = 64 * MIB
MAX_MEMORY_BYTES = 16 * GIB
DEFAULT_PIDS_LIMIT = 512
DEFAULT_SHM_BYTES = 256 * MIB
DEFAULT_START_TIMEOUT = 120.0
DEFAULT_STOP_TIMEOUT = 10
DEFAULT_IDLE_TIMEOUT = 600.0
LOG_MAX_SIZE = "10m"
LOG_MAX_FILES = "2"

_OFFER_TTL = 60.0
_OFFER_FAIL_TTL = 30.0


class OciError(Exception):
    """A managed container could not be started, checked or ended. The text is short and fit to show."""


class OciUnavailable(OciError):
    """This host cannot run managed containers, or the daemon cannot be used now."""


# --------------------------------------------------------------------------- identity and names


def new_owner_id() -> str:
    """A fresh random id for one Bazarr+ install, written on everything it creates.

    Two Bazarr+ containers can share a Docker host (a test copy beside the real one), and each must only
    ever clean up its own containers.
    """
    return secrets.token_hex(8)


def load_owner_id(path: str | os.PathLike) -> str:
    """The install's owner id from ``path``, creating the file (mode 0600) on first use."""
    path = os.fspath(path)
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
        if OWNER_ID_RE.match(value):
            return value
    except OSError:
        pass
    value = new_owner_id()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(value + "\n")
    os.replace(tmp, path)
    return value


def container_name(owner_id: str, plugin_id: str, role: str = ROLE_DAEMON) -> str:
    return f"bazarr-plus-{owner_id[:8]}-{plugin_id}-{role}"


def network_name(owner_id: str, plugin_id: str) -> str:
    """The private network of one plugin's managed containers, the only one they join."""
    return f"bazarr-plus-{owner_id[:8]}-{plugin_id}-net"


def _labels(owner_id: str, plugin_id: str, role: str) -> dict[str, str]:
    return {MANAGED_LABEL: "true", OWNER_LABEL: owner_id, PLUGIN_LABEL: plugin_id, ROLE_LABEL: role}


# --------------------------------------------------------------------------- the spec


@dataclass(frozen=True)
class HealthSpec:
    """A health check Docker runs inside the container. ``test`` is a command and its arguments."""

    test: tuple[str, ...]
    interval: float = 10.0
    timeout: float = 5.0
    retries: int = 3
    start_period: float = 20.0

    def __post_init__(self):
        if not self.test or not all(isinstance(item, str) and item and "\x00" not in item for item in self.test):
            raise ValueError("a health check needs a command")
        if not (1 <= self.interval <= 300 and 1 <= self.timeout <= 60 and 1 <= self.retries <= 20
                and 0 <= self.start_period <= 600):
            raise ValueError("health check timings are out of range")

    def docker(self) -> dict[str, Any]:
        return {
            "Test": ["CMD", *self.test],
            "Interval": int(self.interval * 1e9),
            "Timeout": int(self.timeout * 1e9),
            "Retries": int(self.retries),
            "StartPeriod": int(self.start_period * 1e9),
        }


@dataclass(frozen=True)
class ContainerSpec:
    """What to run, and the limits it runs under. Validated when built."""

    plugin_id: str
    image: str
    index_digest: str
    port: int
    env: Mapping[str, str] = field(default_factory=dict)
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    pids_limit: int = DEFAULT_PIDS_LIMIT
    shm_bytes: int = DEFAULT_SHM_BYTES
    read_only_rootfs: bool = True
    # (absolute path, size in bytes) of each writable tmpfs mount. These are the only writable places
    # besides what the image declares as volumes.
    tmpfs: Sequence[tuple[str, int]] = (("/tmp", 256 * MIB),)
    user: str | None = None
    health: HealthSpec | None = None
    start_timeout: float = DEFAULT_START_TIMEOUT
    stop_timeout: int = DEFAULT_STOP_TIMEOUT

    def __post_init__(self):
        if not PLUGIN_ID_RE.match(str(self.plugin_id)):
            raise ValueError("plugin_id is not a plugin id")
        if not isinstance(self.image, str) or "@" in self.image or ":" in self.image.rsplit("/", 1)[-1] \
                or not OCI_IMAGE_RE.match(self.image):
            raise ValueError("image must be a name without a tag or a digest")
        if not isinstance(self.index_digest, str) or not OCI_INDEX_DIGEST_RE.match(self.index_digest):
            raise ValueError("index_digest must be a sha256 digest")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port must be a TCP port")
        if not MIN_MEMORY_BYTES <= self.memory_bytes <= MAX_MEMORY_BYTES:
            raise ValueError("memory_bytes is out of range")
        if not 16 <= self.pids_limit <= 32768:
            raise ValueError("pids_limit is out of range")
        if not 0 <= self.shm_bytes <= self.memory_bytes:
            raise ValueError("shm_bytes is out of range")
        for key, value in dict(self.env).items():
            if not isinstance(key, str) or not _ENV_KEY_RE.match(key) or not isinstance(value, str) \
                    or "\x00" in value:
                raise ValueError("env must map names to text values")
        for path, size in self.tmpfs:
            if (not isinstance(path, str) or not path.startswith("/") or path == "/" or ".." in path.split("/")
                    or "," in path or "\x00" in path or not 1 * MIB <= size <= self.memory_bytes):
                raise ValueError("tmpfs entries must be an absolute path and a size")
        if self.user is not None and not re.match(r"^[A-Za-z0-9_.-]{1,64}(?::[A-Za-z0-9_.-]{1,64})?$", self.user):
            raise ValueError("user is not a user or a user:group")
        if not 1 <= self.start_timeout <= 900 or not 0 <= self.stop_timeout <= 120:
            raise ValueError("timeouts are out of range")

    @property
    def reference(self) -> str:
        return f"{self.image}@{self.index_digest}"

    @classmethod
    def from_manifest(cls, plugin: PluginManifest, *, port: int, **kwargs) -> "ContainerSpec":
        """A spec for a parsed ``(oci, daemon)`` manifest. The image and its digest come from the manifest."""
        if plugin.artifact != OCI or plugin.oci_image is None or plugin.oci_index_digest is None:
            raise ValueError("the manifest does not declare an oci runtime")
        return cls(plugin_id=plugin.plugin_id, image=plugin.oci_image, index_digest=plugin.oci_index_digest,
                   port=port, **kwargs)


def flaresolverr_spec(plugin_id: str, image: str, index_digest: str, **overrides) -> ContainerSpec:
    """The spec for a managed FlareSolverr: its port, a 1.5 GiB cap and ``LOG_LEVEL=warning``.

    Logging stays at warning because FlareSolverr logs cookies at info. The root filesystem is read-only
    with tmpfs for ``/tmp`` and the home folder; the first box run decides whether it needs more.
    """
    values: dict[str, Any] = {
        "plugin_id": plugin_id,
        "image": image,
        "index_digest": index_digest,
        "port": 8191,
        "env": {"LOG_LEVEL": "warning", "LOG_HTML": "false", "CAPTCHA_SOLVER": "none"},
        "memory_bytes": 1536 * MIB,
        "tmpfs": (("/tmp", 256 * MIB), ("/home/flaresolverr", 128 * MIB)),
        "health": HealthSpec(test=("python3", "-c",
                                   "import urllib.request,sys;"
                                   "sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8191/health',"
                                   "timeout=4).status==200 else 1)")),
    }
    values.update(overrides)
    env = dict(values["env"])
    # Whatever an override adds, the log level stays at warning.
    env["LOG_LEVEL"] = "warning"
    values["env"] = env
    return ContainerSpec(**values)


def container_config(spec: ContainerSpec, *, owner_id: str, role: str = ROLE_DAEMON,
                     network: str | None = None) -> dict[str, Any]:
    """The body of ``POST /containers/create`` for ``spec``. Every limit is stated; nothing is left to a default."""
    network = network or network_name(owner_id, spec.plugin_id)
    memory = int(spec.memory_bytes)
    host: dict[str, Any] = {
        "NetworkMode": network,
        "Privileged": False,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": bool(spec.read_only_rootfs),
        "Memory": memory,
        "MemorySwap": memory,
        "PidsLimit": int(spec.pids_limit),
        "ShmSize": int(spec.shm_bytes),
        "RestartPolicy": {"Name": "no"},
        "AutoRemove": False,
        "PublishAllPorts": False,
        "PortBindings": {},
        "IpcMode": "private",
        "LogConfig": {"Type": "json-file", "Config": {"max-size": LOG_MAX_SIZE, "max-file": LOG_MAX_FILES}},
        "Tmpfs": {path: f"rw,nosuid,nodev,size={size},mode=1777" for path, size in spec.tmpfs},
    }
    body: dict[str, Any] = {
        "Image": spec.reference,
        "Env": [f"{key}={value}" for key, value in sorted(dict(spec.env).items())],
        "Labels": _labels(owner_id, spec.plugin_id, role),
        "StopTimeout": int(spec.stop_timeout),
        "HostConfig": host,
        "NetworkingConfig": {"EndpointsConfig": {network: {"Aliases": [DAEMON_ALIAS]}}},
    }
    if spec.user:
        body["User"] = spec.user
    if spec.health is not None:
        body["Healthcheck"] = spec.health.docker()
    return body


# --------------------------------------------------------------------------- the host


@dataclass(frozen=True)
class HostCheck:
    """Whether managed containers can run here, and why not. ``reason`` is fit to show."""

    available: bool
    reason: str | None = None
    container_id: str | None = None
    image_id: str | None = None
    networks: tuple[str, ...] = ()
    api_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason, "api_version": self.api_version}


def own_container_id(mountinfo: str | None = None, hostname: str | None = None) -> str | None:
    """The id of the container this process runs in.

    The container's own mounts name it (``/var/lib/docker/containers/<id>/hostname``), which holds when the
    container has a custom hostname. A hostname that looks like a short container id is the fallback.
    """
    text = mountinfo
    if text is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
                text = handle.read(1024 * 1024)
        except OSError:
            text = ""
    found = _MOUNTINFO_ID_RE.search(text or "")
    if found:
        return found.group(1)
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
    return hostname if _CONTAINER_ID_RE.match(hostname or "") else None


def check_daemon_host(client: DockerClient | None, *, in_container: bool, container_id: str | None) -> HostCheck:
    """The four conditions for offering managed containers; the first that fails is the reason."""
    if not in_container:
        return HostCheck(False, "managed containers need Bazarr+ itself to run in a container")
    if client is None:
        return HostCheck(False, "no Docker socket or DOCKER_HOST is configured")
    try:
        version = client.ping()
    except DockerApiError as error:
        return HostCheck(False, "the Docker daemon cannot be used: " + error.message[:120])
    if not container_id:
        return HostCheck(False, "Bazarr+ cannot tell which container it runs in", api_version=version)
    try:
        info = client.inspect_container(container_id)
    except DockerNotFound:
        return HostCheck(False, "the Docker daemon does not know the container Bazarr+ runs in, "
                                "so it is not the same host", api_version=version)
    except DockerApiError as error:
        return HostCheck(False, "the Docker daemon cannot be used: " + error.message[:120], api_version=version)
    full_id = str(info.get("Id") or "")
    if not full_id.startswith(container_id[:12]) or not _CONTAINER_ID_RE.match(full_id):
        return HostCheck(False, "the Docker daemon answered for another container", api_version=version)
    mode = str((info.get("HostConfig") or {}).get("NetworkMode") or "")
    if mode == "host":
        return HostCheck(False, "Bazarr+ runs on the host network, so a private network cannot be shared with it",
                         api_version=version)
    if mode in ("none", "") or mode.startswith("container:"):
        return HostCheck(False, "Bazarr+ has no network of its own to share", api_version=version)
    networks = tuple(sorted((info.get("NetworkSettings") or {}).get("Networks") or ()))
    return HostCheck(True, None, container_id=full_id, image_id=str(info.get("Image") or "") or None,
                     networks=networks, api_version=version)


_offer_lock = threading.Lock()
_offer_cache: dict[str, Any] = {"at": 0.0, "check": None, "ttl": 0.0}


def reset_offer_cache() -> None:
    with _offer_lock:
        _offer_cache.update(at=0.0, check=None, ttl=0.0)


def offer_status(*, environ: Mapping[str, str] | None = None, in_container: bool | None = None,
                 client_factory: Callable[[Any], DockerClient] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> HostCheck:
    """``check_daemon_host`` for this process, cached for a minute (30 seconds after a failure).

    Nothing is contacted when Bazarr+ is not in a container or no Docker endpoint is configured.
    """
    with _offer_lock:
        cached = _offer_cache["check"]
        if cached is not None and clock() - _offer_cache["at"] < _offer_cache["ttl"]:
            return cached
        if in_container is None:
            from ..compatibility import running_in_container

            in_container = running_in_container()
        environ = os.environ if environ is None else environ
        client = None
        if in_container:
            try:
                endpoint = endpoint_from_env(environ)
            except DockerRefused as error:
                result = HostCheck(False, error.message)
            else:
                if endpoint is not None:
                    client = (client_factory or DockerClient)(endpoint)
                result = None
        else:
            result = None
        if result is None:
            result = check_daemon_host(client, in_container=in_container,
                                       container_id=own_container_id() if client is not None else None)
        _offer_cache.update(at=clock(), check=result, ttl=_OFFER_TTL if result.available else _OFFER_FAIL_TTL)
        return result


def offered_runtimes(**kwargs) -> frozenset[tuple[str, str]]:
    """The runtime pairs the daemon runtime adds to this host: ``{(oci, daemon)}`` or nothing."""
    return frozenset({(OCI, "daemon")}) if offer_status(**kwargs).available else frozenset()


# --------------------------------------------------------------------------- the runtime


@dataclass(frozen=True)
class DaemonEndpoint:
    """Where a running daemon listens, on its private network. For the authenticating front, not for plugins."""

    network: str
    alias: str
    ip: str | None
    port: int
    container_id: str

    def as_dict(self) -> dict[str, Any]:
        return {"network": self.network, "alias": self.alias, "ip": self.ip, "port": self.port,
                "container": self.container_id[:12]}


class _DoneHandle:
    """A request that was answered when it was made: a daemon has no work queue."""

    def __init__(self, request_id: int, result: RequestResult | None = None, error: BaseException | None = None):
        self.id = request_id
        self.error = error
        self._result = result

    def done(self) -> bool:
        return True

    def add_done_callback(self, callback) -> None:
        callback()

    def cancel(self, reason: str = "cancelled") -> None:
        return None

    def wait(self, timeout=None):
        if self.error is not None:
            raise self.error
        return self._result


class OciDaemonRuntime:
    """One managed container, as the pool's ``WorkerRuntime``.

    ``submit`` answers two operations locally: ``status`` and ``endpoint``. A call marks the container as
    used, which is what keeps the pool from stopping it for idleness. Traffic to the daemon itself does not
    pass through here.

    ``alive`` never calls Docker (the pool reads it under its lock): a monitor thread polls the container
    and ``alive`` reports what it saw. After ``close`` it stays true until the container is confirmed
    removed, so the pool keeps the slot and retries.
    """

    process = None
    recycle_needed = False

    def __init__(self, spec: ContainerSpec, *, client: DockerClient, owner_id: str,
                 host_check: Callable[[DockerClient], HostCheck] | None = None,
                 poll_interval: float = 5.0, ready_interval: float = 0.5, clock=time.monotonic,
                 sleep=time.sleep, on_event: Callable[[str, dict], None] | None = None):
        if not OWNER_ID_RE.match(str(owner_id)):
            raise ValueError("owner_id must be an install id")
        self.spec = spec
        self.owner_id = owner_id
        self.exit_code: int | None = None
        self._client = client
        self._host_check = host_check or self._default_host_check
        self._poll = poll_interval
        self._ready_interval = ready_interval
        self._clock = clock
        self._sleep = sleep
        self._on_event = on_event
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._next_id = 1
        self._started = False
        self._ready = False
        self._closing = False
        self._gone = False
        self._container_id: str | None = None
        self._network_id: str | None = None
        self._image_id: str | None = None
        self._healthy = False
        self._health: str | None = None
        self._failures = 0
        self._self_id: str | None = None
        self.last_error: str | None = None
        self.names = (container_name(owner_id, spec.plugin_id), network_name(owner_id, spec.plugin_id))

    # -------------------------------------------------------------- WorkerRuntime surface

    @property
    def alive(self) -> bool:
        with self._lock:
            if self._gone:
                return False
            if self._closing:
                return True
            return self._ready and self._healthy

    @property
    def in_flight(self) -> int:
        return 0

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def network(self) -> str:
        return self.names[1]

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._closing:
                raise SessionClosed("the container runtime is closed")
            if self._started:
                raise RuntimeError("container runtime already started")
            self._started = True
        try:
            info = self._start()
        except BaseException:
            self._abort_start()
            raise
        with self._lock:
            self._ready = True
            self._healthy = True
            self._record_state(info)
        self._monitor = threading.Thread(target=self._watch, name="oci-monitor-" + self.spec.plugin_id, daemon=True)
        self._monitor.start()
        return {"container": (self._container_id or "")[:12], "network": self.names[1]}

    def submit(self, op, params=None, *, timeout=None, max_result_bytes=None, secrets=None, on_progress=None):
        if secrets:
            raise ValueError("a managed container takes no per-request secrets")
        if not self.alive or self._closing:
            raise SessionClosed("the container is not running")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        if op == "status":
            return _DoneHandle(request_id, RequestResult(result=self.status()))
        if op == "endpoint":
            try:
                result = self.endpoint().as_dict()
            except OciError as error:
                return _DoneHandle(request_id, error=RequestError(str(error), code=Codes.HOST_CALL_FAILED))
            return _DoneHandle(request_id, RequestResult(result=result))
        return _DoneHandle(request_id, error=RequestError("unknown operation", code=Codes.UNKNOWN_OP))

    def request(self, op, params=None, *, timeout=None, **kwargs):
        return self.submit(op, params, timeout=timeout, **kwargs).wait()

    def close(self, grace: float = 5.0) -> None:
        """Stop and remove the container. Idempotent; a failure leaves ``alive`` true so the pool tries again."""
        with self._lock:
            self._closing = True
            container = self._container_id
        self._stop.set()
        gone = True
        if container is not None:
            gone = self._remove(container, grace)
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        with self._lock:
            self._gone = gone

    # -------------------------------------------------------------- what the pool and the front read

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._gone:
                state = "removed"
            elif self._ready and self._healthy:
                state = "running" if self._health in (None, "healthy") else "starting"
            elif self._ready:
                state = "unhealthy"
            else:
                state = "starting"
            return {"state": state, "health": self._health, "container": (self._container_id or "")[:12],
                    "image": self.spec.reference, "error": self.last_error}

    def endpoint(self) -> DaemonEndpoint:
        """The daemon's address on its private network, read from the daemon now."""
        with self._lock:
            container = self._container_id
        if container is None or not self.alive:
            raise OciError("the container is not running")
        try:
            info = self._client.inspect_container(container)
        except DockerApiError as error:
            raise OciError("the Docker daemon cannot be used: " + error.message[:120]) from error
        entry = ((info.get("NetworkSettings") or {}).get("Networks") or {}).get(self.names[1]) or {}
        return DaemonEndpoint(network=self.names[1], alias=DAEMON_ALIAS, ip=entry.get("IPAddress") or None,
                              port=self.spec.port, container_id=container)

    # -------------------------------------------------------------- start

    def _default_host_check(self, client: DockerClient) -> HostCheck:
        from ..compatibility import running_in_container

        return check_daemon_host(client, in_container=running_in_container(), container_id=own_container_id())

    def _start(self) -> dict[str, Any]:
        client = self._client
        try:
            check = self._host_check(client)
            if not check.available:
                raise OciUnavailable(check.reason or "managed containers are not available here")
            self._self_id = check.container_id
            self._ensure_network()
            self._audit_network()
            self._ensure_image()
            self._remove_stale()
            self._create()
            self._audit_container()
            client.start_container(self._container_id)
            return self._wait_ready()
        except DockerApiError as error:
            raise OciError("the Docker daemon refused the request: " + error.message[:160]) from error

    def _ensure_network(self) -> None:
        client, name = self._client, self.names[1]
        labels = _labels(self.owner_id, self.spec.plugin_id, ROLE_DAEMON)
        for _attempt in range(2):
            found = [net for net in client.list_networks(name=name) if net.get("Name") == name]
            if found:
                net = found[0]
                have = net.get("Labels") or {}
                if have.get(MANAGED_LABEL) != "true" or have.get(OWNER_LABEL) != self.owner_id:
                    raise OciError("a network named %s exists that Bazarr+ did not create" % name)
                self._network_id = str(net.get("Id"))
                return
            try:
                self._network_id = client.create_network(name, labels={
                    key: labels[key] for key in (MANAGED_LABEL, OWNER_LABEL, PLUGIN_LABEL)})
                return
            except DockerApiError as error:
                if error.status != 409:
                    raise
        raise OciError("the private network could not be created")

    def _audit_network(self) -> None:
        """Nothing but this plugin's own managed containers may share the private network, and Bazarr+ may not."""
        members = self._client.list_containers(network=self.names[1], include_stopped=True)
        for member in members:
            labels = member.get("Labels") or {}
            identity = str(member.get("Id") or "")
            if identity == self._self_id:
                raise OciError("Bazarr+ itself is attached to the plugin's private network")
            if labels.get(MANAGED_LABEL) != "true" or labels.get(OWNER_LABEL) != self.owner_id:
                raise OciError("a container that Bazarr+ did not create is attached to the plugin's private network")

    def _ensure_image(self) -> None:
        client, spec = self._client, self.spec
        info = None
        try:
            info = client.inspect_image(spec.reference)
        except DockerNotFound:
            info = None
        if info is None or not _names_digest(info, spec):
            client.pull_image(spec.image, spec.index_digest)
            try:
                info = client.inspect_image(spec.reference)
            except DockerNotFound as error:
                raise OciError("the image is not there after the pull") from error
            if not _names_digest(info, spec):
                raise OciError("the pulled image does not carry the pinned digest")
        self._image_id = str(info.get("Id") or "")

    def _remove_stale(self) -> None:
        """A container left under our name by an earlier run is removed, if it is ours; a stranger's is never touched."""
        name = self.names[0]
        try:
            info = self._client.inspect_container(name)
        except DockerNotFound:
            return
        labels = (info.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true" or labels.get(OWNER_LABEL) != self.owner_id:
            raise OciError("a container named %s exists that Bazarr+ did not create" % name)
        self._client.stop_container(name, 0)
        self._client.remove_container(name, force=True)

    def _create(self) -> None:
        body = container_config(self.spec, owner_id=self.owner_id)
        identity, warnings = self._client.create_container(self.names[0], body)
        self._container_id = identity
        for warning in warnings:
            text = warning.lower()
            if "not support memory limit" in text or "pids limit" in text:
                raise OciError("the Docker host cannot enforce the container's limits: " + warning[:120])

    def _audit_container(self) -> None:
        info = self._client.inspect_container(self._container_id)
        problem = _audit(info, self.spec, self.owner_id, self.names[1], self._image_id)
        if problem:
            raise OciError("the container Docker built does not match what was asked: " + problem)

    def _wait_ready(self) -> dict[str, Any]:
        deadline = self._clock() + self.spec.start_timeout
        while True:
            info = self._client.inspect_container(self._container_id)
            state = info.get("State") or {}
            if not state.get("Running"):
                raise OciError(_exit_text("the container exited while it started", state))
            health = (state.get("Health") or {}).get("Status")
            if self.spec.health is None or health == "healthy":
                return info
            if health == "unhealthy":
                raise OciError("the container reported itself unhealthy while it started")
            if self._clock() >= deadline:
                raise OciError("the container did not become healthy in %d seconds" % self.spec.start_timeout)
            self._sleep(self._ready_interval)

    def _abort_start(self) -> None:
        with self._lock:
            self._closing = True
            container = self._container_id
        self._stop.set()
        gone = True
        if container is not None:
            gone = self._remove(container, 0)
        with self._lock:
            self._gone = gone

    # -------------------------------------------------------------- watch and end

    def _record_state(self, info: Mapping[str, Any]) -> None:
        state = info.get("State") or {}
        health = (state.get("Health") or {}).get("Status")
        running = bool(state.get("Running"))
        healthy = running and health != "unhealthy"
        changed = health != self._health or healthy != self._healthy
        self._health = health
        self._healthy = healthy
        if not running:
            self.exit_code = state.get("ExitCode") if isinstance(state.get("ExitCode"), int) else None
            self.last_error = _exit_text("the container stopped", state)
        elif not healthy:
            self.last_error = "the container reported itself unhealthy"
        else:
            self.last_error = None
        if changed and self._on_event is not None:
            try:
                self._on_event("container", {"running": running, "health": health})
            except Exception as error:
                logger.debug("a container event handler failed (%s)", type(error).__name__)

    def _watch(self) -> None:
        container = self._container_id
        while not self._stop.wait(self._poll):
            try:
                info = self._client.inspect_container(container)
            except DockerNotFound:
                with self._lock:
                    self._healthy = False
                    self.last_error = "the container was removed from outside"
                return
            except DockerApiError:
                # One missed answer is not a death; several in a row are.
                with self._lock:
                    self._failures += 1
                    if self._failures >= 3:
                        self._healthy = False
                        self.last_error = "the Docker daemon stopped answering"
                continue
            with self._lock:
                self._failures = 0
                if self._closing:
                    return
                self._record_state(info)

    def _remove(self, container: str, grace: float) -> bool:
        """Stop and remove ``container``. True only when the daemon no longer knows it."""
        client = self._client
        try:
            client.stop_container(container, int(max(0, grace)))
        except DockerNotFound:
            pass
        except DockerApiError as error:
            # The forced removal below stops it too; a failed stop is only worth a line in the log.
            logger.debug("%s: stopping the container failed: %s", self.spec.plugin_id, error.message[:120])
        try:
            client.remove_container(container, force=True)
            for _attempt in range(5):
                try:
                    client.inspect_container(container)
                except DockerNotFound:
                    return True
                self._sleep(0.2)
            return False
        except DockerApiError as error:
            logger.warning("%s: removing the container failed: %s", self.spec.plugin_id, error.message[:120])
            return False


def _familiar(name: str) -> str:
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name[len("library/"):] if name.startswith("library/") else name


def _names_digest(info: Mapping[str, Any], spec: ContainerSpec) -> bool:
    """Whether the local image's ``RepoDigests`` name this image at the pinned index digest."""
    wanted = _familiar(spec.image) + "@" + spec.index_digest
    return any(_familiar(str(item)) == wanted for item in info.get("RepoDigests") or ())


def _exit_text(prefix: str, state: Mapping[str, Any]) -> str:
    code = state.get("ExitCode")
    text = prefix
    if isinstance(code, int):
        text += " (exit code %d)" % code
    if state.get("OOMKilled"):
        text += ": it ran out of memory"
    return text


def _audit(info: Mapping[str, Any], spec: ContainerSpec, owner_id: str, network: str, image_id: str | None) -> str | None:
    """The first way the daemon's record of a container differs from what was asked, or None."""
    host = info.get("HostConfig") or {}
    config = info.get("Config") or {}
    labels = config.get("Labels") or {}
    if image_id and info.get("Image") != image_id:
        return "it is not the verified image"
    for key, value in _labels(owner_id, spec.plugin_id, ROLE_DAEMON).items():
        if labels.get(key) != value:
            return "its labels are wrong"
    if host.get("Privileged"):
        return "it is privileged"
    for key in ("Binds", "Devices", "CapAdd", "VolumesFrom", "Links", "ExtraHosts", "GroupAdd"):
        if host.get(key):
            return "it sets " + key
    if host.get("PortBindings") or host.get("PublishAllPorts"):
        return "it publishes ports"
    for key in ("PidMode", "UsernsMode", "UTSMode"):
        if host.get(key) not in (None, "", "private"):
            return "it shares a namespace (%s)" % key
    if host.get("IpcMode") not in ("private", ""):
        return "it shares a namespace (IpcMode)"
    if str(host.get("NetworkMode") or "") != network:
        return "it is on the wrong network"
    attached = set((info.get("NetworkSettings") or {}).get("Networks") or ())
    if attached - {network}:
        return "it is on another network"
    if "ALL" not in [str(item).upper() for item in host.get("CapDrop") or ()]:
        return "it keeps capabilities"
    if not any("no-new-privileges" in str(item) for item in host.get("SecurityOpt") or ()):
        return "it may gain privileges"
    if host.get("Memory") != spec.memory_bytes or host.get("MemorySwap") != spec.memory_bytes:
        return "its memory cap differs"
    if host.get("PidsLimit") != spec.pids_limit:
        return "its process cap differs"
    if bool(host.get("ReadonlyRootfs")) != bool(spec.read_only_rootfs):
        return "its root filesystem differs"
    if (host.get("RestartPolicy") or {}).get("Name") not in ("no", "", None):
        return "it restarts by itself"
    for mount in info.get("Mounts") or ():
        if mount.get("Type") not in ("tmpfs", "volume"):
            return "it mounts something from the host"
    return None


# --------------------------------------------------------------------------- removal


@dataclass(frozen=True)
class CleanupReport:
    containers: int = 0
    networks: int = 0
    errors: tuple[str, ...] = ()


def _ours(labels: Mapping[str, str] | None, owner_id: str, plugin_id: str | None) -> bool:
    labels = labels or {}
    if labels.get(MANAGED_LABEL) != "true" or labels.get(OWNER_LABEL) != owner_id:
        return False
    return plugin_id is None or labels.get(PLUGIN_LABEL) == plugin_id


def _remove_managed(client: DockerClient, owner_id: str, plugin_id: str | None, self_id: str | None,
                    stop_timeout: int) -> CleanupReport:
    if not OWNER_ID_RE.match(str(owner_id)):
        raise ValueError("owner_id must be an install id")
    wanted = {MANAGED_LABEL: "true", OWNER_LABEL: owner_id}
    if plugin_id is not None:
        wanted[PLUGIN_LABEL] = plugin_id
    containers = networks = 0
    errors: list[str] = []
    try:
        for item in client.list_containers(labels=wanted, include_stopped=True):
            identity = str(item.get("Id") or "")
            # The labels are read again on what the daemon returned; the filter alone is not trusted.
            if not identity or identity == self_id or not _ours(item.get("Labels"), owner_id, plugin_id):
                continue
            try:
                client.stop_container(identity, stop_timeout)
            except DockerNotFound:
                pass
            except DockerApiError as error:
                errors.append("stopping a container failed: " + error.message[:100])
            try:
                client.remove_container(identity, force=True)
                containers += 1
            except DockerApiError as error:
                errors.append("removing a container failed: " + error.message[:100])
        for net in client.list_networks(labels=wanted):
            identity = str(net.get("Id") or "")
            if not identity or not _ours(net.get("Labels"), owner_id, plugin_id):
                continue
            try:
                client.remove_network(identity)
                networks += 1
            except DockerApiError as error:
                # A network something else is still attached to stays; that is not ours to force.
                errors.append("removing a network failed: " + error.message[:100])
    except DockerApiError as error:
        errors.append("the Docker daemon cannot be used: " + error.message[:100])
    return CleanupReport(containers, networks, tuple(errors))


def cleanup_orphans(client: DockerClient, owner_id: str, *, self_id: str | None = None,
                    stop_timeout: int = 5) -> CleanupReport:
    """At boot: remove every managed container and network of this install that an earlier run left behind.

    The pool is empty at boot, so anything carrying this install's labels is an orphan. Containers of
    another install on the same host carry another owner id and are skipped, and so is Bazarr+'s own.
    """
    return _remove_managed(client, owner_id, None, self_id, stop_timeout)


def remove_plugin(client: DockerClient, owner_id: str, plugin_id: str, *, self_id: str | None = None,
                  stop_timeout: int = 5) -> CleanupReport:
    """On uninstall: remove the plugin's containers, then its private network."""
    if not PLUGIN_ID_RE.match(str(plugin_id)):
        raise ValueError("plugin_id is not a plugin id")
    return _remove_managed(client, owner_id, plugin_id, self_id, stop_timeout)


# --------------------------------------------------------------------------- the pool seam


def oci_worker_definition(key: str, spec: ContainerSpec, *, owner_id: str,
                          client_factory: Callable[[], DockerClient],
                          idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT, poll_interval: float = 5.0,
                          host_check: Callable[[DockerClient], HostCheck] | None = None) -> WorkerDefinition:
    """A ``WorkerDefinition`` the pool starts as a managed container.

    The pool's memory sampler skips it (there is no local process); the container's own cap, taken from the
    spec, is what limits it.
    """

    def runtime(definition, limits, host_calls, on_event, on_stderr):
        return OciDaemonRuntime(spec, client=client_factory(), owner_id=owner_id, host_check=host_check,
                                poll_interval=poll_interval, on_event=on_event)

    return WorkerDefinition(key=key, kind="service", runtime=runtime, memory_limit_bytes=spec.memory_bytes,
                            writes_data=False, idle_timeout=idle_timeout, name=spec.plugin_id)

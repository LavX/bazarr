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
  only has Bazarr+'s network, cannot reach the container. The authenticating front that reaches it
  (``front_runtime``) joins that network and a second one it shares with Bazarr+. Nothing else may be
  attached to it.
* No published ports, no bind mounts, no devices, no privileged mode, no host namespaces, all
  capabilities dropped, ``no-new-privileges``, a read-only root filesystem with small tmpfs mounts, a hard
  memory cap with no swap, a process cap, bounded logs, no restart policy (the pool's breaker decides).
* Labelled as Bazarr+'s own (``MANAGED_LABEL``, ``OWNER_LABEL``, ``PLUGIN_LABEL``, ``ROLE_LABEL``). Every removal
  checks those labels on what the daemon listed, so a container Bazarr+ did not create is never touched.
* Inspected after creation and before start: the daemon's own record of the container must match what was
  asked, or the container is removed and the start fails.

Not wired in yet: ``cleanup_orphans`` must be called once at boot, after the install's owner id is loaded
(``load_owner_id``) and before any plugin starts, by the code that starts the plugin runtimes, and
``remove_plugin`` by the uninstall path. Until that is done the offer stays off: ``current_host`` adds
``(oci, daemon)`` only when ``BAZARR_MANAGED_CONTAINERS`` is set to a true value (see ``offer_status``).

The container is removed whenever the pool stops it (idle, drain, uninstall, failure): FlareSolverr keeps
no state, so a start is always from the pinned image. ``cleanup_orphans`` removes what a crash left behind
at boot, ``remove_plugin`` is the call an uninstall makes.
"""
from __future__ import annotations

import errno
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
    DockerCancelled,
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
    "CREATOR_LABEL",
    "MANAGED_LABEL",
    "OWNER_LABEL",
    "OciDaemonRuntime",
    "OciError",
    "OciUnavailable",
    "PLUGIN_LABEL",
    "ROLE_DAEMON",
    "ROLE_LABEL",
    "audit_network_members",
    "check_create_warnings",
    "cleanup_orphans",
    "check_daemon_host",
    "container_config",
    "container_name",
    "ensure_private_network",
    "flaresolverr_spec",
    "load_owner_id",
    "network_name",
    "new_owner_id",
    "oci_worker_definition",
    "offered_runtimes",
    "own_container_id",
    "remove_if_ours",
    "remove_plugin",
    "remove_private_network",
    "remove_stale_container",
]

MIB = 1024 * 1024
GIB = 1024 * MIB

MANAGED_LABEL = "bazarr-plus.managed"
OWNER_LABEL = "bazarr-plus.owner"
PLUGIN_LABEL = "bazarr-plus.plugin"
ROLE_LABEL = "bazarr-plus.role"
# The id of the Bazarr+ container that created the object. Two installs that share an owner id (a copied config)
# still tell their objects apart by it: what a different, still existing container made is never removed.
CREATOR_LABEL = "bazarr-plus.creator"
ROLE_DAEMON = "daemon"
# The name a daemon answers to on its private network, besides its container name.
DAEMON_ALIAS = "daemon"

OWNER_ID_RE = re.compile(r"[0-9a-f]{16}\Z")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}\Z")
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


_owner_lock = threading.Lock()


def load_owner_id(path: str | os.PathLike) -> str:
    """The install's owner id from ``path``, minted (mode 0600) only when the file is missing.

    A file that cannot be read, or holds something other than an id, is never replaced: the id on it may be on
    containers that are still running, so Bazarr+ refuses to start the runtime and says why. The first
    creation is race-safe: the id goes to a uniquely named temp file that is linked into place (the link fails
    if the file now exists), and whatever ended up at ``path`` is read back, so concurrent first uses agree.
    """
    path = os.fspath(path)
    with _owner_lock:
        found = _read_owner_id(path)
        if found is not None:
            return found
        value = new_owner_id()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(value + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass
                except OSError as error:
                    if error.errno not in _NO_HARD_LINKS:
                        raise
                    # A filesystem without hard links: create the final file exclusively instead.
                    _create_exclusively(path, value)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError as error:
            raise OciUnavailable("the install id file cannot be created: %s" % type(error).__name__) from error
        found = _read_owner_id(path)
        if found is None:
            raise OciUnavailable("the install id file is missing after it was created")
        return found


_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP})


def _create_exclusively(path: str, value: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_owner_id(path: str) -> str | None:
    """The id in ``path``; None when the file does not exist. Anything else wrong raises.

    An empty file is looked at again for a moment: a process on a filesystem without hard links creates the file
    before it writes the id, and is caught mid-write.
    """
    value = ""
    for _attempt in range(20):
        try:
            with open(path, encoding="ascii") as handle:
                value = handle.read(256).strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            raise OciUnavailable("the install id file cannot be read (%s), so it is left alone"
                                 % type(error).__name__) from error
        if value:
            break
        time.sleep(0.05)
    if not OWNER_ID_RE.fullmatch(value):
        raise OciUnavailable("the install id file does not hold an install id, so it is left alone")
    return value


def container_name(owner_id: str, plugin_id: str, role: str = ROLE_DAEMON) -> str:
    return f"bazarr-plus-{owner_id[:8]}-{plugin_id}-{role}"


def network_name(owner_id: str, plugin_id: str) -> str:
    """The private network of one plugin's managed containers, the only one they join."""
    return f"bazarr-plus-{owner_id[:8]}-{plugin_id}-net"


def _labels(owner_id: str, plugin_id: str, role: str, creator: str | None = None) -> dict[str, str]:
    labels = {MANAGED_LABEL: "true", OWNER_LABEL: owner_id, PLUGIN_LABEL: plugin_id, ROLE_LABEL: role}
    if creator:
        labels[CREATOR_LABEL] = creator
    return labels


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
        if not PLUGIN_ID_RE.fullmatch(str(self.plugin_id)):
            raise ValueError("plugin_id is not a plugin id")
        if not isinstance(self.image, str) or "@" in self.image or ":" in self.image.rsplit("/", 1)[-1] \
                or not OCI_IMAGE_RE.fullmatch(self.image):
            raise ValueError("image must be a name without a tag or a digest")
        if not isinstance(self.index_digest, str) or not OCI_INDEX_DIGEST_RE.fullmatch(self.index_digest):
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
            if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key) or not isinstance(value, str) \
                    or "\x00" in value:
                raise ValueError("env must map names to text values")
        for path, size in self.tmpfs:
            if (not isinstance(path, str) or not path.startswith("/") or path == "/" or ".." in path.split("/")
                    or "," in path or "\x00" in path or not 1 * MIB <= size <= self.memory_bytes):
                raise ValueError("tmpfs entries must be an absolute path and a size")
        if self.user is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}(?::[A-Za-z0-9_.-]{1,64})?", self.user):
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


def _endpoint(network_id: str | None, aliases: Sequence[str] = (DAEMON_ALIAS,)) -> dict[str, Any]:
    entry: dict[str, Any] = {"Aliases": list(aliases)}
    if network_id:
        entry["NetworkID"] = network_id
    return entry


def container_config(spec: ContainerSpec, *, owner_id: str, role: str = ROLE_DAEMON,
                     network: str | None = None, creator: str | None = None,
                     network_id: str | None = None, aliases: Sequence[str] = (DAEMON_ALIAS,),
                     entrypoint: Sequence[str] | None = None, cmd: Sequence[str] | None = None,
                     extra_tmpfs: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The body of ``POST /containers/create`` for ``spec``. Every limit is stated; nothing is left to a default.

    ``network_id`` is the id of the label-checked private network. It goes into the endpoint entry, so the daemon
    attaches the container to that exact network and not to whatever the name resolves to later.

    ``spec`` may be any object with the fields ``ContainerSpec`` has (the authenticating front's resolved spec is
    one). ``aliases`` are the names the container answers to on its network, ``entrypoint`` and ``cmd`` replace
    the image's own, and ``extra_tmpfs`` adds mounts whose option string is stated in full (the front uses it to
    cover the image's declared ``/config`` volume with an empty read-only mount).
    """
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
    host["Tmpfs"].update(dict(extra_tmpfs or {}))
    body: dict[str, Any] = {
        "Image": spec.reference,
        "Env": [f"{key}={value}" for key, value in sorted(dict(spec.env).items())],
        "Labels": _labels(owner_id, spec.plugin_id, role, creator),
        "StopTimeout": int(spec.stop_timeout),
        "HostConfig": host,
        "NetworkingConfig": {"EndpointsConfig": {network: _endpoint(network_id, aliases)}},
    }
    if entrypoint is not None:
        body["Entrypoint"] = list(entrypoint)
    if cmd is not None:
        body["Cmd"] = list(cmd)
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
    return hostname if _CONTAINER_ID_RE.fullmatch(hostname or "") else None


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
    if not full_id.startswith(container_id[:12]) or not _CONTAINER_ID_RE.fullmatch(full_id):
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


# The switch for offering managed containers. Off unless set to a true value: nothing installs an oci plugin
# yet, and the boot cleanup that must go with it is not called yet, so no host offers the runtime by itself.
ENABLE_ENV = "BAZARR_MANAGED_CONTAINERS"
PROBE_TIMEOUT = 3.0

_offer_lock = threading.Lock()
_offer_cache: dict[str, Any] = {"at": 0.0, "check": None, "ttl": 0.0, "probing": False}


def reset_offer_cache() -> None:
    with _offer_lock:
        _offer_cache.update(at=0.0, check=None, ttl=0.0, probing=False)


def managed_containers_enabled(environ: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(ENABLE_ENV, "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _probe(environ: Mapping[str, str], in_container: bool,
           client_factory: Callable[[Any], DockerClient] | None) -> HostCheck:
    if not managed_containers_enabled(environ):
        return HostCheck(False, "managed containers are switched off (%s)" % ENABLE_ENV)
    client = None
    if in_container:
        try:
            endpoint = endpoint_from_env(environ)
        except DockerRefused as error:
            return HostCheck(False, error.message)
        if endpoint is not None:
            client = (client_factory or (lambda ep: DockerClient(ep, timeout=PROBE_TIMEOUT)))(endpoint)
    return check_daemon_host(client, in_container=in_container,
                             container_id=own_container_id() if client is not None else None)


def offer_status(*, environ: Mapping[str, str] | None = None, in_container: bool | None = None,
                 client_factory: Callable[[Any], DockerClient] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> HostCheck:
    """``check_daemon_host`` for this process, cached for a minute (30 seconds after a failure).

    Off unless ``ENABLE_ENV`` is set. Nothing is contacted when the switch is off, Bazarr+ is not in a container
    or no Docker endpoint is configured. The probe is read-only (a ping and an inspect of Bazarr+'s own
    container) with a short timeout, and runs outside the cache lock: a caller that arrives while another
    probes gets the last answer, or "still checking" the first time, and never waits for the daemon.
    """
    with _offer_lock:
        cached = _offer_cache["check"]
        if cached is not None and clock() - _offer_cache["at"] < _offer_cache["ttl"]:
            return cached
        if _offer_cache["probing"]:
            return cached or HostCheck(False, "still checking the Docker daemon")
        _offer_cache["probing"] = True
    try:
        if in_container is None:
            from ..compatibility import running_in_container

            in_container = running_in_container()
        result = _probe(os.environ if environ is None else environ, in_container, client_factory)
    except BaseException:
        with _offer_lock:
            _offer_cache["probing"] = False
        raise
    with _offer_lock:
        _offer_cache.update(at=clock(), check=result, ttl=_OFFER_TTL if result.available else _OFFER_FAIL_TTL,
                            probing=False)
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


def _creator_elsewhere(client, labels: Mapping[str, str] | None, self_id: str | None) -> bool:
    """Whether ``labels`` name a creator that is not this Bazarr+ and still exists on the daemon.

    Such an object belongs to another running install that shares this owner id (a copied config). A creator
    that is gone (its container was recreated) leaves its objects to whoever runs now. A daemon that cannot say
    counts as "still exists": the safe answer is to leave the object alone.
    """
    creator = (labels or {}).get(CREATOR_LABEL)
    if not creator or creator == self_id:
        return False
    try:
        client.inspect_container(creator)
    except DockerNotFound:
        return False
    except DockerApiError:
        return True
    return True


def _ownership(client, ident: str, owner_id: str, plugin_id: str | None, self_id: str | None) -> tuple[str, str | None]:
    """Who owns the container ``ident``: ``("ours", full id)``, ``("gone", None)`` or ``("foreign", full id)``.

    Every stop and removal goes through this first, and acts on the full id it returns, never on a name. A
    container is ours only when the daemon's own record carries our managed, owner and plugin labels, it is not
    Bazarr+'s own container, and no other running Bazarr+ created it. Raises ``DockerApiError`` when the daemon
    cannot answer.
    """
    try:
        info = client.inspect_container(ident)
    except DockerNotFound:
        return "gone", None
    full = str(info.get("Id") or "")
    labels = (info.get("Config") or {}).get("Labels") or {}
    if not full or full == self_id or (self_id and len(self_id) < 64 and full.startswith(self_id)):
        return "foreign", full or None
    if not _ours(labels, owner_id, plugin_id) or _creator_elsewhere(client, labels, self_id):
        return "foreign", full
    return "ours", full


def _negotiate(client) -> None:
    """Make the client ping, so its requests carry the daemon's API version, before anything else is sent."""
    if getattr(client, "api_version", "set") is None:
        client.ping()


class OciDaemonRuntime:
    """One managed container, as the pool's ``WorkerRuntime``.

    ``submit`` answers two operations locally: ``status`` and ``endpoint``. A call marks the container as
    used, which is what keeps the pool from stopping it for idleness. Traffic to the daemon itself does not
    pass through here.

    ``alive`` never calls Docker (the pool reads it under its lock): a monitor thread polls the container
    and ``alive`` reports what it saw. After ``close`` it stays true until the container is confirmed
    removed, so the pool keeps the slot and retries.

    Crash-loop rule: a locally answered operation proves nothing about the container, so
    ``answers_prove_health`` is False and the pool does not clear its failure history when one is answered.
    Every death the monitor reports, however long the container ran before it, is a failure in the pool's
    window (five in five minutes open the breaker), and failures leave the window by age alone. A container
    that runs for a long time between crashes never opens the breaker, one that keeps dying does.

    Nothing is stopped or removed before the daemon's own record of it shows the labels this install put on
    it (``_ownership``); a container that fails the check is left alone and reported in ``last_error``.
    """

    process = None
    recycle_needed = False
    answers_prove_health = False

    def __init__(self, spec: ContainerSpec, *, client: DockerClient, owner_id: str,
                 host_check: Callable[[DockerClient], HostCheck] | None = None,
                 poll_interval: float = 5.0, ready_interval: float = 0.5, clock=time.monotonic,
                 sleep=time.sleep, on_event: Callable[[str, dict], None] | None = None):
        if not OWNER_ID_RE.fullmatch(str(owner_id)):
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
        self._swap_unlimited = False
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
            event = self._record_state(info)
        self._emit(event)
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
            if not check.available or not check.container_id:
                raise OciUnavailable(check.reason or "managed containers are not available here")
            _negotiate(client)
            self._self_id = check.container_id
            self._check_cancelled()
            self._ensure_network()
            self._audit_network()
            self._ensure_image()
            self._check_cancelled()
            self._remove_stale()
            self._create()
            self._audit_container()
            self._check_cancelled()
            client.start_container(self._container_id)
            return self._wait_ready()
        except DockerCancelled as error:
            raise OciError("the start was cancelled") from error
        except DockerApiError as error:
            raise OciError("the Docker daemon refused the request: " + error.message[:160]) from error

    def _ensure_network(self) -> None:
        self._network_id = ensure_private_network(self._client, self.names[1], owner_id=self.owner_id,
                                                  plugin_id=self.spec.plugin_id, self_id=self._self_id)

    def _audit_network(self) -> None:
        """Nothing but this plugin's own managed containers may share the private network, and Bazarr+ may not."""
        audit_network_members(self._client, self._network_id, owner_id=self.owner_id,
                              plugin_id=self.spec.plugin_id, self_id=self._self_id)

    def _ensure_image(self) -> None:
        client, spec = self._client, self.spec
        info = None
        try:
            info = client.inspect_image(spec.reference)
        except DockerNotFound:
            info = None
        if info is None or not _names_digest(info, spec):
            client.pull_image(spec.image, spec.index_digest, cancel=self._stop)
            try:
                info = client.inspect_image(spec.reference)
            except DockerNotFound as error:
                raise OciError("the image is not there after the pull") from error
            if not _names_digest(info, spec):
                raise OciError("the pulled image does not carry the pinned digest")
        self._image_id = str(info.get("Id") or "")

    def _remove_stale(self) -> None:
        remove_stale_container(self._client, self.names[0], owner_id=self.owner_id,
                               plugin_id=self.spec.plugin_id, self_id=self._self_id)

    def _create(self) -> None:
        body = container_config(self.spec, owner_id=self.owner_id, creator=self._self_id, network_id=self._network_id)
        identity, warnings = self._client.create_container(self.names[0], body)
        self._container_id = identity
        if check_create_warnings(warnings):
            self._swap_unlimited = True

    def _audit_container(self) -> None:
        info = self._client.inspect_container(self._container_id)
        problem = _audit(info, self.spec, self.owner_id, self.names[1], self._network_id, self._image_id,
                         self._self_id, self._swap_unlimited)
        if problem:
            raise OciError("the container Docker built does not match what was asked: " + problem)

    def _check_attached(self, info: Mapping[str, Any]) -> None:
        """After the start: the daemon connected the container to the checked network and to no other."""
        attached = (info.get("NetworkSettings") or {}).get("Networks") or {}
        if set(attached) != {self.names[1]}:
            raise OciError("the started container is on the wrong networks")
        if (attached.get(self.names[1]) or {}).get("NetworkID") != self._network_id:
            raise OciError("the started container is on a network with the wrong id")

    def _check_cancelled(self) -> None:
        if self._stop.is_set():
            raise OciError("the start was cancelled")

    def cancel_start(self) -> None:
        """Ask a start in progress to end soon. Safe from any thread and never blocks: a pull in flight is cut."""
        self._stop.set()

    def _wait_ready(self) -> dict[str, Any]:
        deadline = self._clock() + self.spec.start_timeout
        first = True
        while True:
            self._check_cancelled()
            info = self._client.inspect_container(self._container_id)
            if first:
                self._check_attached(info)
                first = False
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

    def _record_state(self, info: Mapping[str, Any]) -> tuple[str, dict] | None:
        """Take in an inspect. Caller holds the lock. Returns the event to emit once the lock is let go."""
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
            return "container", {"running": running, "health": health}
        return None

    def _emit(self, event: tuple[str, dict] | None) -> None:
        if event is None or self._on_event is None:
            return
        try:
            self._on_event(*event)
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
                event = self._record_state(info)
            self._emit(event)

    def _remove(self, container: str, grace: float) -> bool:
        """Stop and remove ``container`` if it is ours. True only when it is certainly gone or was never ours.

        See ``remove_if_ours``. A container that is not ours is reported in ``last_error`` and counts as released.
        """
        gone, refusal = remove_if_ours(self._client, container, owner_id=self.owner_id,
                                       plugin_id=self.spec.plugin_id, self_id=self._self_id, grace=grace,
                                       sleep=self._sleep)
        if refusal:
            self.last_error = refusal
        return gone


def check_create_warnings(warnings: Sequence[str]) -> bool:
    """Read the warnings a create returned. Raises when a limit cannot be enforced; True when swap is unlimited.

    A host without swap accounting (cgroup v1) keeps the memory cap and stores the swap limit as -1. That one
    value is accepted, and only on the word of this warning.
    """
    swap_unlimited = False
    for warning in warnings:
        text = warning.lower()
        if "not support memory limit" in text or "pids limit" in text:
            raise OciError("the Docker host cannot enforce the container's limits: " + warning[:120])
        if "not support swap limit" in text:
            swap_unlimited = True
    return swap_unlimited


def ensure_private_network(client: DockerClient, name: str, *, owner_id: str, plugin_id: str,
                           self_id: str | None, internal: bool = False) -> str:
    """The id of the plugin's private network ``name``: found and label-checked, or created. Raises ``OciError``.

    A network of that name that Bazarr+ did not create, or that another running install created under a shared
    owner id, is never used. ``internal`` networks have no outbound route; one that exists without the flag is
    refused, because a network cannot be changed after it is made.
    """
    labels = _labels(owner_id, plugin_id, ROLE_DAEMON, self_id)
    wanted = {key: labels[key] for key in (MANAGED_LABEL, OWNER_LABEL, PLUGIN_LABEL, CREATOR_LABEL) if key in labels}
    for _attempt in range(2):
        found = [net for net in client.list_networks(name=name) if net.get("Name") == name]
        if found:
            net = found[0]
            have = net.get("Labels") or {}
            if not _ours(have, owner_id, plugin_id):
                raise OciError("a network named %s exists that Bazarr+ did not create" % name)
            if _creator_elsewhere(client, have, self_id):
                raise OciError("the network %s belongs to another running Bazarr+ that shares this install id" % name)
            if internal and not net.get("Internal"):
                raise OciError("the network %s is not internal" % name)
            return str(net.get("Id"))
        try:
            if internal:
                return client.create_network(name, labels=wanted, internal=True)
            return client.create_network(name, labels=wanted)
        except DockerApiError as error:
            if error.status != 409:
                raise
    raise OciError("the private network could not be created")


def remove_private_network(client: DockerClient, network: str, *, owner_id: str, plugin_id: str,
                           self_id: str | None) -> bool:
    """Remove ``network`` if it is this plugin's own: Bazarr+'s container is disconnected first, then it is removed.

    True when the network is gone or was never ours (left alone), False when it is still there.
    """
    try:
        net = client.inspect_network(network)
    except DockerNotFound:
        return True
    labels = net.get("Labels") or {}
    if not _ours(labels, owner_id, plugin_id) or _creator_elsewhere(client, labels, self_id):
        return True
    identity = str(net.get("Id") or network)
    try:
        _detach_self(client, identity, self_id)
        client.remove_network(identity)
    except DockerApiError as error:
        logger.warning("%s: removing the network failed: %s", plugin_id, error.message[:120])
        return False
    return True


def audit_network_members(client: DockerClient, network_id: str | None, *, owner_id: str, plugin_id: str,
                          self_id: str | None, allow_self: bool = False) -> None:
    """Nothing but this plugin's own managed containers may share the network, and Bazarr+ only when allowed."""
    members = client.list_containers(network=network_id, include_stopped=True)
    for member in members:
        labels = member.get("Labels") or {}
        identity = str(member.get("Id") or "")
        if identity == self_id:
            if allow_self:
                continue
            raise OciError("Bazarr+ itself is attached to the plugin's private network")
        if not _ours(labels, owner_id, plugin_id):
            raise OciError("a container that Bazarr+ did not create is attached to the plugin's private network")


def remove_stale_container(client: DockerClient, name: str, *, owner_id: str, plugin_id: str,
                           self_id: str | None) -> None:
    """A container left under ``name`` by an earlier run is removed, if it is ours; a stranger's is never touched."""
    verdict, full = _ownership(client, name, owner_id, plugin_id, self_id)
    if verdict == "gone":
        return
    if verdict == "foreign":
        raise OciError("a container named %s exists that Bazarr+ did not create" % name)
    client.stop_container(full, 0)
    client.remove_container(full, force=True)


def remove_if_ours(client: DockerClient, container: str, *, owner_id: str, plugin_id: str, self_id: str | None,
                   grace: float, sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str | None]:
    """Stop and remove ``container`` if it is ours. Returns ``(gone, refusal)``.

    ``gone`` is True only when the container is certainly gone or was never ours. The daemon's record is checked
    first. A container that is not ours (wrong or missing labels, Bazarr+'s own id, another running install's) is
    left alone: ``refusal`` says so and it counts as released, because there is nothing of ours to wait for.
    Everything after the check uses the id the daemon returned.
    """
    try:
        verdict, full = _ownership(client, container, owner_id, plugin_id, self_id)
    except DockerApiError as error:
        logger.warning("%s: checking the container failed: %s", plugin_id, error.message[:120])
        return False, None
    if verdict == "gone":
        return True, None
    if verdict == "foreign":
        refusal = "refused to remove a container that Bazarr+ does not own"
        logger.warning("%s: %s", plugin_id, refusal)
        return True, refusal
    try:
        client.stop_container(full, int(max(0, grace)))
    except DockerNotFound:
        pass
    except DockerApiError as error:
        # The forced removal below stops it too; a failed stop is only worth a line in the log.
        logger.debug("%s: stopping the container failed: %s", plugin_id, error.message[:120])
    try:
        client.remove_container(full, force=True)
        for _attempt in range(5):
            try:
                client.inspect_container(full)
            except DockerNotFound:
                return True, None
            sleep(0.2)
        return False, None
    except DockerApiError as error:
        logger.warning("%s: removing the container failed: %s", plugin_id, error.message[:120])
        return False, None


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


def _audit(info: Mapping[str, Any], spec: ContainerSpec, owner_id: str, network: str, network_id: str | None,
           image_id: str | None, creator: str | None, swap_unlimited: bool = False,
           role: str = ROLE_DAEMON) -> str | None:
    """The first way the daemon's record of a container differs from what was asked, or None.

    ``swap_unlimited`` is set only when create warned that the host cannot limit swap: then, and only then, a
    swap limit of -1 is what was asked for. This runs between create and start, when the daemon has stored the
    endpoint exactly as it was sent: the network id there is the one that was sent or still empty. The id the
    daemon assigns when it connects the container is checked after the start (``_check_attached``).
    """
    host = info.get("HostConfig") or {}
    config = info.get("Config") or {}
    labels = config.get("Labels") or {}
    if image_id and info.get("Image") != image_id:
        return "it is not the verified image"
    for key, value in _labels(owner_id, spec.plugin_id, role, creator).items():
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
    attached = (info.get("NetworkSettings") or {}).get("Networks") or {}
    if set(attached) - {network}:
        return "it is on another network"
    if network_id and (attached.get(network) or {}).get("NetworkID") not in ("", None, network_id):
        return "it is on a network with the wrong id"
    if "ALL" not in [str(item).upper() for item in host.get("CapDrop") or ()]:
        return "it keeps capabilities"
    if not any("no-new-privileges" in str(item) for item in host.get("SecurityOpt") or ()):
        return "it may gain privileges"
    expected_swap = -1 if swap_unlimited else spec.memory_bytes
    if host.get("Memory") != spec.memory_bytes or host.get("MemorySwap") != expected_swap:
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


def _detach_self(client: DockerClient, network: str, self_id: str | None) -> None:
    """Disconnect Bazarr+'s own container from ``network`` if it is attached, so the network can be removed.

    Only networks that carry this install's labels get here. The one Bazarr+ joins itself is the front's
    (see ``front_runtime``): the front must be reachable from Bazarr+, and Docker refuses to remove a network
    that something is still attached to.
    """
    if not self_id:
        return
    try:
        attached = client.inspect_network(network).get("Containers") or {}
    except DockerNotFound:
        return
    if any(str(key) == self_id or str(key).startswith(self_id) or self_id.startswith(str(key)) for key in attached):
        client.disconnect_network(network, self_id, force=True)


def _remove_managed(client: DockerClient, owner_id: str, plugin_id: str | None, self_id: str,
                    stop_timeout: int) -> CleanupReport:
    if not OWNER_ID_RE.fullmatch(str(owner_id)):
        raise ValueError("owner_id must be an install id")
    if not isinstance(self_id, str) or not _CONTAINER_ID_RE.fullmatch(self_id):
        raise ValueError("self_id must be the id of the Bazarr+ container")
    wanted = {MANAGED_LABEL: "true", OWNER_LABEL: owner_id}
    if plugin_id is not None:
        wanted[PLUGIN_LABEL] = plugin_id
    containers = networks = 0
    errors: list[str] = []
    try:
        # The API version is settled first: nothing here is sent as an unversioned path.
        _negotiate(client)
        for item in client.list_containers(labels=wanted, include_stopped=True):
            identity = str(item.get("Id") or "")
            if not identity:
                continue
            # The filter alone is not trusted: the daemon's own record of each container is read again, and
            # the removal acts on the id it returns.
            try:
                verdict, full = _ownership(client, identity, owner_id, plugin_id, self_id)
            except DockerApiError as error:
                errors.append("checking a container failed: " + error.message[:100])
                continue
            if verdict != "ours":
                continue
            try:
                client.stop_container(full, stop_timeout)
            except DockerNotFound:
                pass
            except DockerApiError as error:
                errors.append("stopping a container failed: " + error.message[:100])
            try:
                client.remove_container(full, force=True)
                containers += 1
            except DockerApiError as error:
                errors.append("removing a container failed: " + error.message[:100])
        for net in client.list_networks(labels=wanted):
            identity = str(net.get("Id") or "")
            if (not identity or not _ours(net.get("Labels"), owner_id, plugin_id)
                    or _creator_elsewhere(client, net.get("Labels"), self_id)):
                continue
            try:
                _detach_self(client, identity, self_id)
                client.remove_network(identity)
                networks += 1
            except DockerApiError as error:
                # A network something else is still attached to stays; that is not ours to force.
                errors.append("removing a network failed: " + error.message[:100])
    except DockerApiError as error:
        errors.append("the Docker daemon cannot be used: " + error.message[:100])
    return CleanupReport(containers, networks, tuple(errors))


def cleanup_orphans(client: DockerClient, owner_id: str, *, self_id: str, stop_timeout: int = 5) -> CleanupReport:
    """At boot: remove every managed container and network of this install that an earlier run left behind.

    The pool is empty at boot, so anything carrying this install's labels is an orphan. Containers of
    another install on the same host carry another owner id and are skipped, and so is Bazarr+'s own.
    ``self_id`` is required: the id of the Bazarr+ container (``HostCheck.container_id``). An object that a
    different, still running Bazarr+ created under a shared owner id (a copied config) is left alone.
    """
    return _remove_managed(client, owner_id, None, self_id, stop_timeout)


def remove_plugin(client: DockerClient, owner_id: str, plugin_id: str, *, self_id: str,
                  stop_timeout: int = 5) -> CleanupReport:
    """On uninstall: remove the plugin's containers, then its private network."""
    if not PLUGIN_ID_RE.fullmatch(str(plugin_id)):
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

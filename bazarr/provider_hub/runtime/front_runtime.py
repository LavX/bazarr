# coding=utf-8
"""The host half of the authenticating front: the container Bazarr+ starts, and the lease tokens it hands out.

A managed daemon (see ``oci``) answers to anyone who can reach it, and FlareSolverr has no login. So the daemon
sits alone on a private network, and the only thing that reaches it is a second container, the front, that checks
a per-lease token (``front`` is the program that runs in it). Bazarr+ reaches the daemon only through the front.

Topology, per plugin:

* Network B is the daemon's own (``oci.network_name``). The daemon and the front are on it, and Bazarr+ is not.
* Network A (``front_network_name``) is made here: internal (no route out), with only the front and Bazarr+'s own
  container on it. Plugin workers run inside Bazarr+'s container, so they reach the front on it, and they can
  reach it only with a token.
* The front is created on A, connected to B before it starts, and publishes no port. It runs from Bazarr+'s own
  image, read from the daemon's record of Bazarr+'s container and never pulled: the front is exactly the code
  that is running. The image is named by its id (``sha256:...``), the content hash, not by a tag.

What the front container is: a fixed entrypoint (``python3 -I front.py``), a non-root numeric user, all
capabilities dropped, ``no-new-privileges``, a read-only root filesystem, a read-only empty mount over the
image's declared ``/config`` volume (so there is no ``/config`` and no anonymous volume), a memory and process
cap, bounded logs, no restart policy. What Bazarr+ sends as its environment is the digest of the admin secret, the upstream address, the ports, the size
caps and network A's subnet (the front serves only connections that arrive inside it); the image's own ``ENV`` is
merged in by Docker. Docker's own record of the container is read back and compared with what was asked, between
create and start.

Tokens:

* A lease gets its token from ``FrontRuntime.open_lease``: 32 random bytes, URL-safe. Bazarr+ keeps only the
  SHA-256 digests of live tokens. The caller holds the token in the ``FrontLease``; its repr, copy and pickle
  never show it.
* Bazarr+ pushes the whole digest set to the front (``PUT /_admin/leases``) authenticated by a per-start secret.
  The secret is random, lives in this object's memory, and reaches the front only as its digest in the
  container's environment, so ``docker inspect`` shows nothing that opens the admin path. ``open_lease`` returns
  only after the front has acknowledged the new set; a revoke is a push of the set without the token.
* A revoke is always tried while a front container exists, whatever the runtime believes about its health: Docker's
  health, a failing Docker API or a half-finished close do not make a revoked token valid again. One that cannot be
  delivered stops and removes the front (fail closed), retrying the removal until it is confirmed, and whoever
  supervises the front restarts it. A restarted front starts empty, and ``start`` and the monitor push the held set
  again.
* A request carries the token in its path, ``/l/<token>/v1``, because the providers POST to one configured URL
  with fixed headers. That is the only place the token appears in a URL, and the front never logs a path.

``cleanup_orphans`` and ``remove_plugin`` (in ``oci``) remove the front and network A with everything else this
install made, and disconnect Bazarr+'s own container from network A first.

Known limits, for the box checks and the slice that wires this in:

* Bazarr+ listens on every interface, so the front can connect to Bazarr+'s own port over network A. The front is
  Bazarr+'s own unprivileged code and the API still wants its key.
* The front sits on two networks. Whether its network namespace forwards between them depends on the host's
  ``ip_forward``; with every capability dropped neither side can add a route, and Docker's bridge isolation drops
  the traffic anyway. Read ``/proc/sys/net/ipv4/ip_forward`` in the front on the box.
* The lease token is in the URL path. The code that hands the URL to a provider must keep the front's address out
  of any HTTP proxy setting, and must not log the URL; the token is registered with the credential scrubber so a
  log line that carries it is masked.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import re
import secrets
import threading
import time

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from secret_store.scrub import add_database_secret_values

from . import front as front_server
from .docker_api import DockerApiError, DockerCancelled, DockerClient, DockerNotFound
from .oci import (
    DAEMON_ALIAS,
    HealthSpec,
    HostCheck,
    OciError,
    OciUnavailable,
    OWNER_ID_RE,
    _audit,
    _negotiate,
    audit_network_members,
    check_create_warnings,
    check_daemon_host,
    container_config,
    container_name,
    ensure_private_network,
    network_name,
    own_container_id,
    remove_if_ours,
    remove_private_network,
    remove_stale_container,
)
from ..plugin_manifest import PLUGIN_ID_RE

logger = logging.getLogger(__name__)

__all__ = [
    "FRONT_ENTRYPOINT",
    "FRONT_ADMIN_PORT",
    "FRONT_PORT",
    "FRONT_SCRIPT",
    "FRONT_USER",
    "FrontError",
    "FrontLease",
    "FrontRuntime",
    "FrontSpec",
    "ROLE_FRONT",
    "front_container_name",
    "front_network_name",
    "new_lease_token",
]

MIB = 1024 * 1024

ROLE_FRONT = "front"
FRONT_PORT = front_server.FRONT_PORT
FRONT_ADMIN_PORT = front_server.ADMIN_PORT
# The front program, as it is laid out in Bazarr+'s image (the front runs from the same image).
FRONT_SCRIPT = "/app/bazarr/bazarr/provider_hub/runtime/front.py"
FRONT_ENTRYPOINT = ("python3", "-I", FRONT_SCRIPT)
# nobody:nogroup by number, so the front needs no user database and cannot be a name that something redefines.
FRONT_USER = "65534:65534"
# The image declares /config as a volume. An empty read-only mount at the same path takes its place, so the
# front has no /config and no anonymous volume.
_CONFIG_MOUNT = {"/config": "ro,nosuid,nodev,noexec,size=1m,mode=0555"}

_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ADMIN_TIMEOUT = 5.0
MAX_ADMIN_REPLY = 4096


class FrontError(OciError):
    """The front could not be started, reached or told something. The text is short and fit to show."""


def front_container_name(owner_id: str, plugin_id: str) -> str:
    return container_name(owner_id, plugin_id, ROLE_FRONT)


def front_network_name(owner_id: str, plugin_id: str) -> str:
    """The network Bazarr+ shares with the front of one plugin. Only those two are on it."""
    return f"bazarr-plus-{owner_id[:8]}-{plugin_id}-front-net"


def new_lease_token() -> str:
    """A fresh lease token: 256 random bits, URL-safe, 43 characters."""
    return secrets.token_urlsafe(32)


def _digest_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrontSpec:
    """What the front in front of one daemon is run with. Validated when built."""

    plugin_id: str
    upstream_port: int
    upstream_host: str = DAEMON_ALIAS
    memory_bytes: int = 128 * MIB
    pids_limit: int = 128
    max_body_bytes: int = front_server.DEFAULT_MAX_BODY
    max_response_bytes: int = front_server.DEFAULT_MAX_RESPONSE
    upstream_timeout: float = front_server.DEFAULT_UPSTREAM_TIMEOUT
    start_timeout: float = 60.0
    stop_timeout: int = 5

    def __post_init__(self):
        if not PLUGIN_ID_RE.fullmatch(str(self.plugin_id)):
            raise ValueError("plugin_id is not a plugin id")
        if isinstance(self.upstream_port, bool) or not isinstance(self.upstream_port, int) \
                or not 1 <= self.upstream_port <= 65535:
            raise ValueError("upstream_port must be a TCP port")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", str(self.upstream_host)):
            raise ValueError("upstream_host is not a host name")
        if not 16 * MIB <= self.memory_bytes <= 2048 * MIB or not 16 <= self.pids_limit <= 1024:
            raise ValueError("a limit is out of range")
        if not 1 <= self.max_body_bytes <= 64 * MIB or not 1 <= self.max_response_bytes <= 256 * MIB:
            raise ValueError("a size cap is out of range")
        if not 1 <= self.upstream_timeout <= 900 or not 1 <= self.start_timeout <= 900 \
                or not 0 <= self.stop_timeout <= 120:
            raise ValueError("a timeout is out of range")

    @classmethod
    def for_daemon(cls, daemon, **kwargs) -> "FrontSpec":
        """The front for a daemon's ``ContainerSpec``: it forwards to the daemon's alias and port."""
        return cls(plugin_id=daemon.plugin_id, upstream_port=daemon.port, **kwargs)


@dataclass(frozen=True)
class _FrontContainer:
    """The front as ``container_config`` reads a container: the spec of one start, with its image and env."""

    plugin_id: str
    reference: str
    env: Mapping[str, str]
    memory_bytes: int
    pids_limit: int
    stop_timeout: int
    shm_bytes: int = 1 * MIB
    read_only_rootfs: bool = True
    tmpfs: tuple = ()
    user: str = FRONT_USER
    health: HealthSpec | None = None


class FrontLease:
    """One lease's right to use the front. Close it, or use it as a context manager, when the work is done.

    The token is in ``token`` and in the URL ``url()`` builds; ``repr``, ``copy`` and ``pickle`` never show it.
    """

    __slots__ = ("_runtime", "_id", "_token", "_closed", "_guard")

    def __init__(self, runtime: "FrontRuntime", lease_id: int, token: str):
        self._runtime = runtime
        self._id = lease_id
        self._token = token
        self._closed = False
        self._guard = threading.Lock()

    @property
    def token(self) -> str:
        return self._token

    @property
    def closed(self) -> bool:
        return self._closed

    def url(self) -> str:
        """The URL a provider POSTs to, built from the front's address now (it changes when the front restarts)."""
        if self._closed:
            raise FrontError("the lease is closed")
        return self._runtime.url_for(self._token)

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
        self._runtime._revoke(self._id)

    def __enter__(self) -> "FrontLease":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<FrontLease #%d%s>" % (self._id, " closed" if self._closed else "")

    def __reduce__(self):
        raise TypeError("a lease is not copied or pickled")


class FrontRuntime:
    """The front container of one managed daemon, with its lease set.

    Start it after the daemon (or before: it makes the daemon's network if it is missing and checks it the same
    way). ``open_lease`` gives a token that works until it is closed; ``close`` removes the front and its network.
    One object serves one plugin for good: after ``close``, ``start`` runs it again on a fresh container, and the
    lease set it held is pushed to the new front, so a lease taken before the restart keeps working.

    Like the daemon runtime, nothing is stopped or removed before the daemon's own record of it shows the labels
    this install put on it, and ``alive`` never calls Docker: a monitor thread polls the container and the front's
    lease count, and pushes the held set again when the front lost it (a restart in place).
    """

    answers_prove_health = False

    def __init__(self, spec: FrontSpec, *, client: DockerClient, owner_id: str,
                 host_check: Callable[[DockerClient], HostCheck] | None = None,
                 connect: Callable[[str, int, float], Any] | None = None,
                 poll_interval: float = 5.0, ready_interval: float = 0.5, clock=time.monotonic,
                 sleep=time.sleep):
        if not OWNER_ID_RE.fullmatch(str(owner_id)):
            raise ValueError("owner_id must be an install id")
        self.spec = spec
        self.owner_id = owner_id
        self._client = client
        self._host_check = host_check or self._default_host_check
        self._connect = connect or (lambda host, port, timeout: http.client.HTTPConnection(host, port, timeout=timeout))
        self._poll = poll_interval
        self._ready_interval = ready_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._push_lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._started = False
        self._ready = False
        self._healthy = False
        self._closing = False
        self._gone = False
        self._book: dict[int, str] = {}
        self._next_lease = 1
        self._container_id: str | None = None
        self._self_id: str | None = None
        self._image_id: str | None = None
        self._net_a_id: str | None = None
        self._net_b_id: str | None = None
        self._subnet: ipaddress.IPv4Network | None = None
        self._ip: str | None = None
        self._secret: str | None = None
        self._swap_unlimited = False
        self._resolved: _FrontContainer | None = None
        self._failures = 0
        self.last_error: str | None = None
        self.names = (front_container_name(owner_id, spec.plugin_id), front_network_name(owner_id, spec.plugin_id),
                      network_name(owner_id, spec.plugin_id))

    # -------------------------------------------------------------- state

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._ready and self._healthy and not self._closing and not self._gone

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def lease_count(self) -> int:
        with self._lock:
            return len(self._book)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._gone:
                state = "removed"
            elif self._closing:
                state = "stopping"
            elif self._ready and self._healthy:
                state = "running"
            elif self._ready:
                state = "unhealthy"
            else:
                state = "starting"
            return {"state": state, "container": (self._container_id or "")[:12], "leases": len(self._book),
                    "error": self.last_error}

    def address(self) -> tuple[str, int]:
        """The front's address on network A, for Bazarr+ and its workers: ``(ip, port)``."""
        with self._lock:
            ip = self._ip
        if not ip or not self.alive:
            raise FrontError("the front is not running")
        return ip, FRONT_PORT

    def url_for(self, token: str) -> str:
        host, port = self.address()
        return "http://%s:%d/l/%s/v1" % (host, port, token)

    # -------------------------------------------------------------- leases

    def open_lease(self) -> FrontLease:
        """Mint a token and have the front accept it. Returns after the front has acknowledged the new set."""
        if not self.alive:
            raise FrontError("the front is not running")
        token = new_lease_token()
        add_database_secret_values([token])
        with self._lock:
            if len(self._book) >= front_server.MAX_LEASES:
                raise FrontError("too many leases are open")
            lease_id = self._next_lease
            self._next_lease += 1
            self._book[lease_id] = _digest_hex(token)
        try:
            self._push()
        except FrontError:
            with self._lock:
                self._book.pop(lease_id, None)
            raise
        return FrontLease(self, lease_id, token)

    def _revoke(self, lease_id: int) -> None:
        """Take the lease's digest out of the front's set. Delivered, or the front is verifiably gone.

        This does not look at ``alive``: a front that Docker calls unhealthy, that the monitor lost for a while or
        that a failed close left running can still be serving the token. It is skipped only when no container
        with an address and the secret exists (never started, or confirmed removed), because then nothing holds
        the token, and a front that is still coming up gets its set from the push that ends its start.
        """
        with self._lock:
            if self._book.pop(lease_id, None) is None:
                return
            serving = bool(self._container_id and self._ip and self._secret and not self._gone)
        if not serving:
            return
        for attempt in range(3):
            try:
                self._push()
                return
            except FrontError:
                self._sleep(0.2 * attempt)
        # A token that cannot be taken back is a hole: the front goes away instead.
        self.last_error = "the front could not be told that a lease ended, so it was stopped"
        logger.warning("%s: %s", self.spec.plugin_id, self.last_error)
        self._ensure_removed()

    def _ensure_removed(self) -> None:
        """Close the front and keep at it until the daemon confirms it is gone, a few tries at most."""
        for attempt in range(4):
            self.close(grace=0)
            if self._gone:
                return
            self._sleep(0.2 * (attempt + 1))
        self.last_error = "the front could not be told that a lease ended, and could not be removed"
        logger.error("%s: %s", self.spec.plugin_id, self.last_error)

    def _admin(self, method: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            ip, secret = self._ip, self._secret
        if not ip or not secret:
            raise FrontError("the front has no address yet")
        connection = None
        try:
            connection = self._connect(ip, FRONT_ADMIN_PORT, ADMIN_TIMEOUT)
            # A global debug level would print the request, secret included, to stdout.
            connection.set_debuglevel(0)
            headers = {"Authorization": "Bearer " + secret}
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("ascii")
                headers["Content-Type"] = "application/json"
            connection.request(method, front_server.ADMIN_PATH, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_ADMIN_REPLY)
            if response.status != 200:
                raise FrontError("the front refused the request (HTTP %d)" % response.status)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("unexpected reply")
            return data
        except FrontError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise FrontError("the front cannot be reached (%s)" % type(error).__name__) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _push(self) -> None:
        """Replace the front's lease set with the one held here. Pushes are serialised, so the last one is the newest."""
        with self._push_lock:
            with self._lock:
                digests = sorted(self._book.values())
            reply = self._admin("PUT", {"tokens": digests})
            if reply.get("count") != len(digests):
                raise FrontError("the front did not take the lease set")

    def _sync(self) -> None:
        """Push the held set again if the front's own copy differs (it was restarted in place)."""
        with self._lock:
            held = sorted(self._book.values())
        expected = hashlib.sha256(b"".join(bytes.fromhex(item) for item in held)).hexdigest()
        reply = self._admin("GET")
        if reply.get("count") != len(held) or reply.get("fingerprint") != expected:
            logger.info("%s: the front's lease set differed, so it is pushed again", self.spec.plugin_id)
            self._push()

    # -------------------------------------------------------------- start

    def _default_host_check(self, client: DockerClient) -> HostCheck:
        from ..compatibility import running_in_container

        return check_daemon_host(client, in_container=running_in_container(), container_id=own_container_id())

    def start(self) -> dict[str, Any]:
        monitor = self._monitor
        if self._closing and monitor is not None and monitor is not threading.current_thread():
            # Outside the lock: the monitor takes it on its way out.
            monitor.join(timeout=2.0)
        with self._lock:
            if self._started and not self._closing:
                raise RuntimeError("the front already started")
            if self._closing:
                self._reset_for_restart()
            self._started = True
        try:
            info = self._start()
        except BaseException:
            self._abort_start()
            raise
        with self._lock:
            self._ready = True
            self._healthy = True
            self._ip = self._address_from(info)
        self._monitor = threading.Thread(target=self._watch, name="front-monitor-" + self.spec.plugin_id,
                                         daemon=True)
        self._monitor.start()
        return {"container": (self._container_id or "")[:12], "network": self.names[1]}

    def _reset_for_restart(self) -> None:
        """After a close or a failed start: ready to start again. The lease set is kept, so a restart pushes it."""
        self._stop = threading.Event()
        self._monitor = None
        self._ready = self._healthy = self._closing = self._gone = False
        self._container_id = self._ip = self._secret = None
        self._failures = 0
        self.last_error = None

    def cancel_start(self) -> None:
        self._stop.set()

    def _check_cancelled(self) -> None:
        if self._stop.is_set():
            raise OciError("the start was cancelled")

    def _start(self) -> dict[str, Any]:
        client, spec = self._client, self.spec
        try:
            check = self._host_check(client)
            if not check.available or not check.container_id:
                raise OciUnavailable(check.reason or "managed containers are not available here")
            _negotiate(client)
            self._self_id = check.container_id
            self._image_id = self._local_image(check.image_id)
            self._check_cancelled()
            common = dict(owner_id=self.owner_id, plugin_id=spec.plugin_id, self_id=self._self_id)
            self._net_b_id = ensure_private_network(client, self.names[2], **common)
            audit_network_members(client, self._net_b_id, **common)
            self._net_a_id = ensure_private_network(client, self.names[1], internal=True, **common)
            audit_network_members(client, self._net_a_id, allow_self=True, **common)
            self._subnet = self._subnet_of(self._net_a_id)
            remove_stale_container(client, self.names[0], **common)
            self._check_cancelled()
            self._create()
            self._audit_container()
            client.connect_network(self._net_b_id, self._container_id)
            self._attach_self()
            self._check_cancelled()
            client.start_container(self._container_id)
            info = self._wait_ready()
            self._ip = self._address_from(info)
            self._push()
            return info
        except DockerCancelled as error:
            raise OciError("the start was cancelled") from error
        except DockerApiError as error:
            raise OciError("the Docker daemon refused the request: " + error.message[:160]) from error

    def _local_image(self, image_id: str | None) -> str:
        """Bazarr+'s own image, by id. It must already be on the host: the front is never pulled."""
        if not image_id or not _IMAGE_ID_RE.fullmatch(image_id):
            raise OciError("Bazarr+ cannot tell which image it runs from")
        try:
            info = self._client.inspect_image(image_id)
        except DockerNotFound as error:
            raise OciError("the image Bazarr+ runs from is not on the Docker host, and the front is never pulled") \
                from error
        if info.get("Id") != image_id:
            raise OciError("the Docker daemon answered for another image")
        return image_id

    def _subnet_of(self, network_id: str) -> ipaddress.IPv4Network:
        info = self._client.inspect_network(network_id)
        for entry in ((info.get("IPAM") or {}).get("Config") or ()):
            try:
                found = ipaddress.ip_network(str(entry.get("Subnet") or ""), strict=False)
            except ValueError:
                continue
            if found.version == 4:
                return found
        raise OciError("the front's network has no IPv4 subnet")

    def _create(self) -> None:
        secret = secrets.token_urlsafe(32)
        spec = self.spec
        env = {
            front_server.ENV_ADMIN_SHA256: _digest_hex(secret),
            front_server.ENV_UPSTREAM: "%s:%d" % (spec.upstream_host, spec.upstream_port),
            front_server.ENV_PORT: str(FRONT_PORT),
            front_server.ENV_ADMIN_PORT: str(FRONT_ADMIN_PORT),
            front_server.ENV_ALLOW_NET: str(self._subnet),
            front_server.ENV_MAX_BODY: str(spec.max_body_bytes),
            front_server.ENV_MAX_RESPONSE: str(spec.max_response_bytes),
            front_server.ENV_UPSTREAM_TIMEOUT: str(int(spec.upstream_timeout)),
        }
        resolved = _FrontContainer(
            plugin_id=spec.plugin_id, reference=self._image_id or "", env=env, memory_bytes=spec.memory_bytes,
            pids_limit=spec.pids_limit, stop_timeout=spec.stop_timeout,
            health=HealthSpec(test=("python3", "-I", "-c",
                                    "import socket;[socket.create_connection(('127.0.0.1',p),3).close() for p in (%d,%d)]"
                                    % (FRONT_PORT, FRONT_ADMIN_PORT)),
                              interval=5.0, timeout=4.0, retries=3, start_period=3.0))
        self._resolved = resolved
        body = container_config(resolved, owner_id=self.owner_id, role=ROLE_FRONT, network=self.names[1],
                                creator=self._self_id, network_id=self._net_a_id, aliases=(),
                                entrypoint=FRONT_ENTRYPOINT, cmd=(), extra_tmpfs=_CONFIG_MOUNT)
        identity, warnings = self._client.create_container(self.names[0], body)
        with self._lock:
            self._container_id = identity
            # Set only once the container exists: from here an abort removes it.
            self._secret = secret
        add_database_secret_values([secret])
        self._swap_unlimited = check_create_warnings(warnings)

    def _audit_container(self) -> None:
        info = self._client.inspect_container(self._container_id)
        resolved = self._resolved
        problem = _audit(info, resolved, self.owner_id, self.names[1], self._net_a_id, self._image_id,
                         self._self_id, self._swap_unlimited, role=ROLE_FRONT) or _audit_front(info, resolved)
        if problem:
            raise OciError("the front Docker built does not match what was asked: " + problem)

    def _attach_self(self) -> None:
        """Connect Bazarr+'s own container to network A, so it and its workers can reach the front."""
        client, net, me = self._client, self._net_a_id, self._self_id
        if _attached(client, net, me):
            return
        try:
            client.connect_network(net, me)
        except DockerApiError:
            # Two connects racing: the one that lost sees the endpoint already there.
            if not _attached(client, net, me):
                raise

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
                raise OciError("the front exited while it started")
            health = (state.get("Health") or {}).get("Status")
            if health == "healthy":
                return info
            if health == "unhealthy":
                raise OciError("the front reported itself unhealthy while it started")
            if self._clock() >= deadline:
                raise OciError("the front did not become healthy in %d seconds" % self.spec.start_timeout)
            self._sleep(self._ready_interval)

    def _check_attached(self, info: Mapping[str, Any]) -> None:
        """After the start: the front is on networks A and B, those checked ones and no others."""
        attached = (info.get("NetworkSettings") or {}).get("Networks") or {}
        if set(attached) != {self.names[1], self.names[2]}:
            raise OciError("the started front is on the wrong networks")
        for name, wanted in ((self.names[1], self._net_a_id), (self.names[2], self._net_b_id)):
            if (attached.get(name) or {}).get("NetworkID") != wanted:
                raise OciError("the started front is on a network with the wrong id")

    def _address_from(self, info: Mapping[str, Any]) -> str:
        entry = ((info.get("NetworkSettings") or {}).get("Networks") or {}).get(self.names[1]) or {}
        try:
            ip = ipaddress.ip_address(str(entry.get("IPAddress") or ""))
        except ValueError as error:
            raise OciError("the front has no address on its network") from error
        if self._subnet is None or ip.version != 4 or ip not in self._subnet:
            raise OciError("the front's address is not on its network")
        return str(ip)

    # -------------------------------------------------------------- watch and end

    def _watch(self) -> None:
        container = self._container_id
        while not self._stop.wait(self._poll):
            try:
                info = self._client.inspect_container(container)
            except DockerNotFound:
                with self._lock:
                    self._healthy = False
                    self.last_error = "the front was removed from outside"
                return
            except DockerApiError:
                self._miss("the Docker daemon stopped answering")
                continue
            state = info.get("State") or {}
            running = bool(state.get("Running"))
            health = (state.get("Health") or {}).get("Status")
            with self._lock:
                if self._closing:
                    return
                if not running:
                    self._healthy = False
                    self.last_error = "the front stopped"
                    continue
            # A front Docker calls unhealthy may still serve, so its lease set is kept right all the same.
            docker_ok = health != "unhealthy"
            try:
                ip = self._address_from(info)
                self._sync()
            except FrontError:
                self._miss("the front stopped answering")
                continue
            except OciError:
                self._miss("the front has no address on its network")
                continue
            with self._lock:
                self._ip = ip
                self._failures = 0
                self._healthy = docker_ok
                self.last_error = None if docker_ok else "the front reported itself unhealthy"

    def _miss(self, reason: str) -> None:
        """One missed answer is not a death; three in a row are."""
        with self._lock:
            self._failures += 1
            if self._failures >= 3:
                self._healthy = False
                self.last_error = reason

    def _abort_start(self) -> None:
        with self._lock:
            self._closing = True
        self._stop.set()
        self._teardown(0)

    def close(self, grace: float = 5.0) -> None:
        """Stop and remove the front and network A. Idempotent; a front that cannot be removed stays reported."""
        with self._lock:
            self._closing = True
            self._healthy = False
        self._stop.set()
        self._teardown(grace)
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)

    def _teardown(self, grace: float) -> None:
        container = self._container_id
        gone = True
        if container is not None:
            gone, refusal = remove_if_ours(self._client, container, owner_id=self.owner_id,
                                           plugin_id=self.spec.plugin_id, self_id=self._self_id, grace=grace,
                                           sleep=self._sleep)
            if refusal:
                self.last_error = refusal
        if gone and self._net_a_id is not None:
            gone = remove_private_network(self._client, self._net_a_id, owner_id=self.owner_id,
                                          plugin_id=self.spec.plugin_id, self_id=self._self_id)
        with self._lock:
            self._gone = gone
            # The secret is only useful with a front to talk to.
            if gone:
                self._secret = None


def _attached(client: DockerClient, network: str | None, container: str | None) -> bool:
    if not network or not container:
        return False
    members = client.inspect_network(network).get("Containers") or {}
    return any(str(key).startswith(container) or container.startswith(str(key)) for key in members)


def _audit_front(info: Mapping[str, Any], resolved: _FrontContainer) -> str | None:
    """What the generic audit does not read: the front's user, program, environment and mounts."""
    config = info.get("Config") or {}
    host = info.get("HostConfig") or {}
    if config.get("User") != FRONT_USER:
        return "it runs as another user"
    if list(config.get("Entrypoint") or ()) != list(FRONT_ENTRYPOINT):
        return "it runs another program"
    if list(config.get("Cmd") or ()):
        return "it has a command"
    have = set(config.get("Env") or ())
    if any("%s=%s" % pair not in have for pair in resolved.env.items()):
        return "its environment differs"
    tmpfs = host.get("Tmpfs") or {}
    if set(tmpfs) != set(_CONFIG_MOUNT) or not str(tmpfs.get("/config", "")).startswith("ro,"):
        return "its mounts differ"
    mounts = info.get("Mounts") or ()
    if any(mount.get("Type") != "tmpfs" or mount.get("Destination") != "/config" for mount in mounts):
        return "it has a mount besides the empty /config"
    return None

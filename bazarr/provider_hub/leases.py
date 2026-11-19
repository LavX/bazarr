# coding=utf-8
"""Leases: how a provider call gets the address of a service it requires.

A service is something a plugin needs and does not carry, such as FlareSolverr. Where it runs is the user's
business, not the plugin's: the address of an instance the user typed (``endpoints``), or a container Bazarr+
manages behind its authenticating front (``runtime.front_runtime``). Either way a worker is handed the address
through a lease, for the length of one call, and never stores it.

* A lease is taken when a call needs the address (``capabilities.CallLeases``) and closed when the call ends.
* A remote service, one the user typed, gets a ``DirectLease``: the address as typed, no token. Bazarr+ does not
  start, stop or limit what it does not run. The lease is only the record of who is using it.
* A managed service gets a ``ManagedLease`` over a front lease: an address with a token in its path that the front
  accepts until the lease is closed. Closing the lease takes the token back.
* A release of a provider that goes away (``registry`` drains its instances) closes what its instances still hold,
  and a service that goes away (``ServiceKind.detach``) closes every lease it gave out, so nothing keeps working
  against a service or a provider that is gone.

The managed tier is dormant until something registers a running service here (``register_managed``). Nothing in
this module starts a container.

An address, and above all a token, is never logged: a lease prints without its address, cannot be copied or
pickled, and the credential parts of a typed address and every token are registered with the scrubber.
"""
from __future__ import annotations

import itertools
import logging
import threading

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from secret_store.scrub import add_runtime_secret_values

logger = logging.getLogger(__name__)

SOURCE_SETTING = "setting"
SOURCE_ENV = "env"
SOURCE_MANAGED = "managed"

__all__ = [
    "DirectLease",
    "Lease",
    "LeaseError",
    "ManagedLease",
    "ManagedService",
    "SOURCE_ENV",
    "SOURCE_MANAGED",
    "SOURCE_SETTING",
    "detach_service",
    "has_managed",
    "managed_service_for",
    "open_count",
    "open_managed",
    "register_managed",
    "release_owner",
    "release_service",
    "unregister_managed",
]


class LeaseError(RuntimeError):
    """The lease is closed, or the service behind it is gone. The text is short and never carries an address."""


_ids = itertools.count(1)


class _Ledger:
    """Every open lease, so what a detached provider or service held can be found and closed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[int, "Lease"] = {}

    def add(self, lease: "Lease") -> None:
        with self._lock:
            self._open[lease.lease_id] = lease

    def discard(self, lease: "Lease") -> None:
        with self._lock:
            self._open.pop(lease.lease_id, None)

    def select(self, *, owner: str | None = None, service_id: str | None = None,
               capability: str | None = None) -> "list[Lease]":
        with self._lock:
            held = list(self._open.values())
        return [lease for lease in held
                if (owner is None or lease.owner == owner)
                and (service_id is None or lease.service_id == service_id)
                and (capability is None or lease.capability == capability)]


_LEDGER = _Ledger()


class Lease:
    """One call's right to use a service. Close it, or use it as a context manager, when the call is done.

    ``owner`` names who holds it (one provider instance), ``service_id`` the managed service that issued it (None
    for a remote one). ``url()`` builds the address at the time it is asked, so a front that restarted is found at
    its new place. A closed lease answers nothing.
    """

    external = False

    def __init__(self, capability: str, owner: str, source: str, service_id: str | None = None):
        self.capability = capability
        self.owner = owner
        self.source = source
        self.service_id = service_id
        self.lease_id = next(_ids)
        self._closed = False
        self._guard = threading.Lock()
        _LEDGER.add(self)

    @property
    def closed(self) -> bool:
        return self._closed

    def url(self) -> str:
        if self._closed:
            raise LeaseError("the lease is closed")
        return self._address()

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
        _LEDGER.discard(self)
        try:
            self._release()
        except Exception as error:
            # The credential-bearing text of the error is not logged: only what kind of failure it was.
            logger.warning("A %s lease could not be released cleanly (%s)", self.capability, type(error).__name__)

    def _address(self) -> str:
        raise NotImplementedError

    def _release(self) -> None:
        """Give back whatever the lease took. Runs once."""

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<%s #%d %s%s>" % (type(self).__name__, self.lease_id, self.capability, " closed" if self._closed else "")

    def __reduce__(self):
        raise TypeError("a lease is not copied or pickled")


def _credential_parts(address: str) -> list[str]:
    """The parts of a typed address that are secrets: a user name or password in its authority."""
    try:
        parts = urlsplit(address)
        found = [parts.username, parts.password]
    except ValueError:
        return []
    values = []
    for item in found:
        if item:
            values.extend([item, unquote(item)])
    return values


class DirectLease(Lease):
    """A service someone else runs, reached at the address the user typed. No token, no lifecycle.

    Whatever the address carries in its authority (a user name or password the user typed) is masked in logs from
    here on.
    """

    external = True

    def __init__(self, capability: str, owner: str, address: str, source: str):
        self._target = address
        secrets = _credential_parts(address)
        if secrets:
            add_runtime_secret_values(secrets)
        super().__init__(capability, owner, source)

    def _address(self) -> str:
        return self._target


class ManagedLease(Lease):
    """A lease on a service Bazarr+ runs, over a front lease: an address that carries a token, valid until closed."""

    def __init__(self, capability: str, owner: str, service_id: str, front_lease: Any):
        self._front = front_lease
        super().__init__(capability, owner, SOURCE_MANAGED, service_id)

    def _address(self) -> str:
        try:
            return self._front.url()
        except Exception as error:
            raise LeaseError("the service is not reachable (%s)" % type(error).__name__) from None

    def _release(self) -> None:
        self._front.close()


@dataclass(frozen=True)
class ManagedService:
    """A service Bazarr+ runs that is up and can hand out leases.

    ``open_lease`` returns something with ``url()`` and ``close()`` (a front lease). ``ready`` says whether it can
    be used right now. ``stop`` ends what the service runs, and is called off the caller's thread when the service
    is detached.
    """

    service_id: str
    capabilities: frozenset[str]
    open_lease: Callable[[], Any]
    ready: Callable[[], bool] = lambda: True
    stop: Callable[[], None] | None = None


_MANAGED: dict[str, ManagedService] = {}
_MANAGED_LOCK = threading.Lock()


def register_managed(service_id: str, capabilities, open_lease: Callable[[], Any], *,
                     ready: Callable[[], bool] | None = None, stop: Callable[[], None] | None = None) -> ManagedService:
    """Offer a running service to the plugins that require what it provides. Replaces an earlier entry of the id."""
    service = ManagedService(str(service_id), frozenset(capabilities), open_lease, ready or (lambda: True), stop)
    with _MANAGED_LOCK:
        _MANAGED[service.service_id] = service
    return service


def unregister_managed(service_id: str) -> ManagedService | None:
    with _MANAGED_LOCK:
        return _MANAGED.pop(str(service_id), None)


def managed_service_for(capability: str) -> ManagedService | None:
    """The managed service that provides ``capability`` and belongs to a plugin that is live, or None.

    A service whose plugin is switched off, removed, revoked or held back is not offered even while its entry is
    still here: the state is read at the time of use, as trust is.
    """
    with _MANAGED_LOCK:
        candidates = [_MANAGED[key] for key in sorted(_MANAGED) if capability in _MANAGED[key].capabilities]
    for service in candidates:
        if _service_live(service.service_id):
            return service
    return None


def has_managed(capability: str) -> bool:
    """Whether a managed service that provides ``capability`` is registered. Cheap: it does not read the state."""
    with _MANAGED_LOCK:
        return any(capability in service.capabilities for service in _MANAGED.values())


def _service_live(service_id: str) -> bool:
    try:
        from . import closure
        from .capabilities import live_row

        row = live_row(service_id)
        return row is not None and closure.is_on(row)
    except Exception as error:
        logger.warning("Could not tell whether service %s is live (%s)", service_id, type(error).__name__)
        return False


def open_managed(capability: str, owner: str) -> ManagedLease | None:
    """A lease on a managed service that provides ``capability``, or None when there is none that is ready."""
    service = managed_service_for(capability)
    if service is None:
        return None
    try:
        if not service.ready():
            return None
        front_lease = service.open_lease()
    except Exception as error:
        logger.warning("Could not lease service %s (%s)", service.service_id, type(error).__name__)
        return None
    lease = ManagedLease(capability, owner, service.service_id, front_lease)
    with _MANAGED_LOCK:
        still = _MANAGED.get(service.service_id) is service
    if not still:
        # The service was detached while the lease was being opened: nothing may keep it.
        lease.close()
        return None
    return lease


def release_owner(owner: str) -> int:
    """Close every lease one holder still has. Idempotent. Returns how many were open."""
    held = _LEDGER.select(owner=owner)
    for lease in held:
        lease.close()
    return len(held)


def release_service(service_id: str) -> int:
    """Close every lease a managed service issued, which takes its tokens back. Returns how many were open."""
    held = _LEDGER.select(service_id=service_id)
    for lease in held:
        lease.close()
    return len(held)


def open_count(*, service_id: str | None = None, owner: str | None = None, capability: str | None = None) -> int:
    """How many leases are open. A supervisor that idle-stops a service reads this before it stops."""
    return len(_LEDGER.select(owner=owner, service_id=service_id, capability=capability))


def detach_service(service_id: str) -> "list[threading.Thread]":
    """A service leaves while Bazarr+ runs: nothing new is leased from it, every lease it gave is closed, and what it
    runs is stopped on a thread of its own. Returns that thread, so the caller can wait for the end of it.

    The managed entry goes first, so a lease being opened right now notices and closes itself.
    """
    service = unregister_managed(service_id)
    release_service(service_id)
    if service is None or service.stop is None:
        return []

    def stop() -> None:
        try:
            service.stop()
        except Exception as error:
            logger.warning("Could not stop service %s (%s)", service_id, type(error).__name__)

    thread = threading.Thread(target=stop, name=f"hub-stop-{service_id}", daemon=True)
    thread.start()
    return [thread]


# coding=utf-8
"""Detaching a running release while Bazarr+ runs: stop serving it, let what it took finish, then let it go.

Disable, remove, revoke and the update of a kind that attaches live all end with a release that must not be
used again. This module is the consumer side of that. The state side (what the rows say, and when a release
retires) is in ``service``, which calls here after it has committed, because every consumer reads the state:
once the row says "removed" or "held back", a pool that asks for the provider finds nothing to run.

The sequence, for a release that leaves:

1. The state commits first (the row is marked removed or held back), so the sources the consumers read from
   (``running_installations``, the registry, the host's trust record) already say the release is gone.
2. The kind detaches its consumers (``Kind.detach``): the provider kind takes the class out of the provider
   registry, which gives back a built-in it had replaced, replaces the compat and Discover pool, takes the host
   capabilities back from its workers and drops its instances from every search pool; the translator kind stops
   sending jobs to the worker. The calls the release already took are drained: they finish, or, for a release
   that must stop at once (a revoked one), are cut when ``REVOKE_GRACE_SECONDS`` have passed.
3. When every drain has ended, what is left of the release's processes is stopped, and whatever waited for
   that runs: a removed row is deleted, a replaced release retires.

Locks, in the order they are taken. The state lock is never held across a drain: the drains run on threads of
their own and the completion steps take the state lock themselves. Registry changes share
``registry._REGISTRY_GUARD`` and are single steps under it. A pool's own store lock is the one
``detach_provider`` takes. A proxy's ``_PROXY_LOCK`` guards its retirement and its count of admitted calls.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

from .kinds import DETACH_LIVE, UnknownKindError, get_kind

logger = logging.getLogger(__name__)

# How long the calls of a revoked release may go on once it is revoked. A release the catalog revokes may be
# acting against the person, so it is not left to run out a request that can last hours; a call a few seconds
# from done still finishes.
REVOKE_GRACE_SECONDS = 15.0
# How long the calls of a plugin the person removed may go on. The removal is theirs, so a call that is nearly done
# still finishes, but a request that can last hours does not keep the removal open for that long.
REMOVE_GRACE_SECONDS = 300.0


def detaches_live(kind_name: str) -> bool:
    """Whether releases of this kind can be stopped while Bazarr+ runs."""
    try:
        return get_kind(kind_name).detach_policy == DETACH_LIVE
    except UnknownKindError:
        return False


def detach(plugin_id: str, kind_name: str, *, withdraw: bool, grace: float | None = None,
           release_key: str | None = None) -> list[threading.Thread]:
    """Detach the consumers of a release. Returns the drain threads; a failure is raised.

    ``withdraw`` is True for a release that leaves for good and False for a plugin that is only switched off.
    """
    kind = get_kind(kind_name)
    return list(kind.detach(plugin_id, withdraw=withdraw, grace=grace, release_key=release_key) or ())


def snapshot_workers(plugin_id: str, release_key: str | None = None) -> list:
    """The workers the plugin has now, taken before its consumers are detached.

    What is stopped after the drain is exactly these: a worker started later (a plugin that was switched on again
    and searched before the drain ended) belongs to an instance that is in use, and is never swept.
    """
    from .worker import live_workers

    return live_workers(plugin_id, release_key)


def stop_stragglers(clients: Iterable, *, cut: bool = False) -> int:
    """Stop the workers of a snapshot that no pool instance reached (one a build was still starting). With ``cut``
    they are killed at once, otherwise asked to shut down. Returns how many. (A connection test's worker is not
    among them: it carries no provider id, and ends with its own request.)"""
    stopped = 0
    for client in clients:
        try:
            client.cancel() if cut else client.stop()
            stopped += 1
        except Exception:
            logger.warning("Provider Hub: could not stop a worker of %s", getattr(client, "provider_id", "?"),
                           exc_info=True)
    return stopped


# The threads that wait for a drain and then finish what waited for it, so a caller that needs them all done (a
# shutdown, a test) can wait for exactly those.
_PENDING: "set[threading.Thread]" = set()
_PENDING_LOCK = threading.Lock()


def when_drained(drains: Iterable[threading.Thread], then: Callable[[], None], *, name: str) -> threading.Thread:
    """Run ``then`` once every drain thread has ended, on a thread of its own. The thread is returned, so a
    caller that needs the end of it can join it, and ``wait_idle`` waits for every one."""
    drains = list(drains)

    def run() -> None:
        try:
            for drain in drains:
                drain.join()
            try:
                then()
            except Exception:
                logger.exception("Provider Hub: the step after detaching %s failed", name)
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(threading.current_thread())

    thread = threading.Thread(target=run, name=f"hub-detach-{name}", daemon=True)
    with _PENDING_LOCK:
        _PENDING.add(thread)
    thread.start()
    return thread


def wait_idle(timeout: float = 30.0) -> bool:
    """Wait until every release that is being detached has finished (its calls done, its workers gone, whatever
    waited for that done). True when nothing is left, False when ``timeout`` seconds were not enough."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        with _PENDING_LOCK:
            pending = list(_PENDING)
        if not pending:
            return True
        for thread in pending:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            thread.join(left)

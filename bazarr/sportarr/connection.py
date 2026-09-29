"""Connection snapshots and cancellation shared by sports readers."""
import logging
import time
from contextlib import contextmanager
from threading import Lock, RLock

CONNECTION_FIELDS = ('ip', 'port', 'base_url', 'ssl', 'verify_ssl', 'api_key',
                     'http_timeout', 'path_mappings')
_locks = {}
_waiting = {}
_guard = Lock()


def connection_identity(instance):
    return tuple(getattr(instance, name) for name in CONNECTION_FIELDS)


class SportsSyncBusy(RuntimeError):
    """Raised when a bounded wait for an instance's sync lock expires.

    Deliberately not a ValueError: the sports layer converts ValueError into a
    400-shaped "bad request" in several places, and a busy instance is neither
    the caller's fault nor permanent.
    """


def check_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError('Sportarr synchronization stopped')


def sync_waiting(owner):
    """Whether a counted caller is queued for ``owner``'s sync lock right now."""
    with _guard:
        return _waiting.get(owner, 0) > 0


@contextmanager
def owner_sync_lock(owner, cancel=None, timeout=None, count_waiter=True):
    """Serialize fetch and apply, so an older concurrent fetch cannot win last.

    ``timeout`` bounds the wait, in seconds. Callers that run on a request
    thread MUST pass one: with ``cancel=None`` the wait loop below has no exit
    at all, because ``check_cancelled(None)`` does nothing. A webhook that
    arrived while a scheduled full sync held this lock therefore parked its
    Waitress worker permanently, and enough of them stopped answering the UI.
    Background callers keep the unbounded wait, which is what they want, but
    now say so in the log rather than spinning in silence.

    A caller that finds the lock taken is counted in ``sync_waiting`` before
    it starts waiting, and until it holds the lock, gives up or is cancelled,
    so the scheduled recording index can step aside for it. The index waits
    with ``count_waiter=False``: it must never make another holder yield to it.
    """
    with _guard:
        lock = _locks.setdefault(owner, RLock())
    deadline = None if timeout is None else time.monotonic() + timeout
    counted = False
    try:
        if not lock.acquire(blocking=False):
            if count_waiter:
                with _guard:
                    _waiting[owner] = _waiting.get(owner, 0) + 1
                counted = True
                logging.debug('Waiting for the Sportarr sync lock on instance %s.', owner)
            else:
                logging.debug('The Sports recording index is waiting for the Sportarr sync lock on '
                              'instance %s.', owner)
            while not lock.acquire(timeout=0.1):
                check_cancelled(cancel)
                if deadline is not None and time.monotonic() >= deadline:
                    raise SportsSyncBusy(
                        f'A Sportarr synchronization is already running for instance {owner}')
    finally:
        if counted:
            with _guard:
                if _waiting[owner] > 1:
                    _waiting[owner] -= 1
                else:
                    del _waiting[owner]
    try:
        check_cancelled(cancel)
        yield
    finally:
        lock.release()


def revalidate(session, owner, expected, cancel=None):
    from sportarr.sync.leagues import require_sportarr
    check_cancelled(cancel)
    instance = require_sportarr(session, owner)
    if connection_identity(instance) != expected:
        raise ValueError('Sportarr connection changed during synchronization')
    return instance

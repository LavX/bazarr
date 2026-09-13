"""Connection snapshots and cancellation shared by sports readers."""
import logging
import time
from contextlib import contextmanager
from threading import Lock, RLock

CONNECTION_FIELDS = ('ip', 'port', 'base_url', 'ssl', 'verify_ssl', 'api_key',
                     'http_timeout', 'path_mappings')
_locks = {}
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


@contextmanager
def owner_sync_lock(owner, cancel=None, timeout=None):
    """Serialize fetch and apply, so an older concurrent fetch cannot win last.

    ``timeout`` bounds the wait, in seconds. Callers that run on a request
    thread MUST pass one: with ``cancel=None`` the wait loop below has no exit
    at all, because ``check_cancelled(None)`` does nothing. A webhook that
    arrived while a scheduled full sync held this lock therefore parked its
    Waitress worker permanently, and enough of them stopped answering the UI.
    Background callers keep the unbounded wait, which is what they want, but
    now say so in the log rather than spinning in silence.
    """
    with _guard:
        lock = _locks.setdefault(owner, RLock())
    deadline = None if timeout is None else time.monotonic() + timeout
    waited = False
    while not lock.acquire(timeout=0.1):
        check_cancelled(cancel)
        if deadline is not None and time.monotonic() >= deadline:
            raise SportsSyncBusy(
                f'A Sportarr synchronization is already running for instance {owner}')
        if not waited:
            waited = True
            logging.debug('Waiting for the Sportarr sync lock on instance %s.', owner)
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

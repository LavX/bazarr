"""Connection snapshots and cancellation shared by sports readers."""
from contextlib import contextmanager
from threading import Lock, RLock

CONNECTION_FIELDS = ('ip', 'port', 'base_url', 'ssl', 'verify_ssl', 'api_key',
                     'http_timeout', 'path_mappings')
_locks = {}
_guard = Lock()


def connection_identity(instance):
    return tuple(getattr(instance, name) for name in CONNECTION_FIELDS)


def check_cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise ValueError('Sportarr synchronization stopped')


@contextmanager
def owner_sync_lock(owner, cancel=None):
    # Serialize fetch and apply, so an older concurrent fetch cannot win last.
    with _guard:
        lock = _locks.setdefault(owner, RLock())
    while not lock.acquire(timeout=0.1):
        check_cancelled(cancel)
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

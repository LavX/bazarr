from __future__ import annotations
import time
from threading import Lock

_lock = Lock()
_denied: dict[str, int] = {}  # jti -> exp epoch


def revoke(jti: str, exp: int) -> None:
    """Record a jti as revoked until `exp` (unix epoch seconds).

    Prunes already-expired entries on every call so the map can't grow
    unbounded across restarts-less deployments. Entries past `exp` are
    treated as not-revoked regardless (a leaked JWT that expired on its
    own doesn't need explicit denial).
    """
    if not jti:
        return
    now = int(time.time())
    with _lock:
        # Prune first so the map stays bounded.
        for j in [j for j, e in _denied.items() if e <= now]:
            _denied.pop(j, None)
        if exp > now:
            _denied[str(jti)] = int(exp)


def is_revoked(jti: str) -> bool:
    """Check whether the given jti is on the denylist AND still within its
    original exp window. Entries past exp are not-revoked (the JWT would
    already fail its own exp check)."""
    if not jti:
        return False
    now = int(time.time())
    with _lock:
        exp = _denied.get(str(jti))
        if exp is None:
            return False
        if exp <= now:
            _denied.pop(str(jti), None)
            return False
        return True


def reset() -> None:
    """Test helper. Not for production use."""
    with _lock:
        _denied.clear()

"""Per-key sliding-window counter for /download quota enforcement.

Window is fixed-interval (not true rolling), so within a window a caller
can consume up to `limit` downloads; at window boundary the count resets.
In-memory only, single-node. Keyed by JWT jti for the compat flow."""
from __future__ import annotations
import time
from threading import Lock

_lock = Lock()
# key -> (window_start_epoch, count)
_counts: dict[str, tuple[int, int]] = {}


def _window_key(now: int, window_seconds: int) -> int:
    return (now // window_seconds) * window_seconds


def try_consume(key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    """Consume one unit against the caller's quota.

    Returns (allowed, remaining_after, reset_epoch). When allowed=False,
    remaining_after is 0 and the caller should emit 406. reset_epoch is
    always the end of the current window so the UI can display a useful
    retry-after.
    """
    if not key:
        return True, limit, int(time.time()) + int(window_seconds)
    now = int(time.time())
    win = _window_key(now, window_seconds)
    reset = win + window_seconds
    with _lock:
        entry = _counts.get(key)
        if entry is None or entry[0] != win:
            _counts[key] = (win, 1)
            return True, max(0, limit - 1), reset
        start, count = entry
        if count >= limit:
            return False, 0, reset
        _counts[key] = (start, count + 1)
        return True, max(0, limit - (count + 1)), reset


def inspect(key: str, limit: int, window_seconds: int) -> tuple[int, int]:
    """Return (remaining, reset_epoch) without consuming."""
    now = int(time.time())
    win = _window_key(now, window_seconds)
    reset = win + window_seconds
    if not key:
        return limit, reset
    with _lock:
        entry = _counts.get(key)
        if entry is None or entry[0] != win:
            return limit, reset
        _, count = entry
        return max(0, limit - count), reset


def reset() -> None:
    """Test helper."""
    with _lock:
        _counts.clear()

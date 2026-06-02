"""DB-persisted per-key usage meter for the Distribution Hub.

Counters are bucketed by hour: one row per (key_id, kind, hour_start). This
is the single source of truth for both rate-limit window sums and usage
statistics, so weekly/monthly limits survive restarts and redeploys. A short
in-memory cache fronts the window-sum reads on the hot path; it is invalidated
for a key whenever that key records a new hit.

`record()` uses a dialect-aware upsert (the module-level `insert` exported by
app.database is the sqlite/postgres dialect insert, both of which support
on_conflict_do_update) so concurrent fanouts can't violate the unique index.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from threading import Lock

from app.database import (database, select, insert, delete as sa_delete,
                          func, TableCompatUsage)

logger = logging.getLogger("bazarr.compat.meter")

WINDOWS = ("hour", "day", "week", "month")
_WINDOW_DELTA = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=30),
}
_UNIQUE_COLS = ("key_id", "kind", "hour_start")

_lock = Lock()
_sum_cache: dict[tuple[int, str, str], tuple[float, int]] = {}
_SUM_TTL = 5.0


def _truncate_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _invalidate(key_id: int) -> None:
    with _lock:
        for ck in [c for c in _sum_cache if c[0] == key_id]:
            _sum_cache.pop(ck, None)


def record(key_id: int, kind: str, *, blocked: bool = False) -> None:
    """Increment the current hour bucket for (key_id, kind).

    blocked=True increments the `blocked` column (a rate-limited rejection,
    counted for statistics) instead of `count`."""
    key_id = int(key_id)
    hour = _truncate_hour(datetime.now())
    inc_count = 0 if blocked else 1
    inc_blocked = 1 if blocked else 0
    stmt = insert(TableCompatUsage).values(
        key_id=key_id, kind=kind, hour_start=hour,
        count=inc_count, blocked=inc_blocked)
    stmt = stmt.on_conflict_do_update(
        index_elements=list(_UNIQUE_COLS),
        set_={
            "count": TableCompatUsage.count + inc_count,
            "blocked": TableCompatUsage.blocked + inc_blocked,
        },
    )
    # Metering is best-effort telemetry: a DB hiccup (or a not-yet-migrated
    # table in a partial environment) must never 500 a subtitle request.
    try:
        database.execute(stmt)
    except Exception:
        logger.debug("compat meter: record failed", exc_info=True)
        return
    _invalidate(key_id)


def window_sum(key_id: int, kind: str, window: str) -> int:
    """Total successful hits for (key_id, kind) within the rolling window."""
    key_id = int(key_id)
    ck = (key_id, kind, window)
    now = time.monotonic()
    with _lock:
        hit = _sum_cache.get(ck)
        if hit and hit[0] > now:
            return hit[1]
    # Inclusive of the current hour bucket: subtract the window then add one
    # hour back so e.g. the "day" window covers the trailing 24 hour buckets.
    since = _truncate_hour(datetime.now()) - _WINDOW_DELTA[window] + timedelta(hours=1)
    # Fail-open on read errors: a limit check that can't read usage must allow
    # the request (return 0 used), never block or 500.
    try:
        total = database.execute(
            select(func.coalesce(func.sum(TableCompatUsage.count), 0))
            .where(TableCompatUsage.key_id == key_id,
                   TableCompatUsage.kind == kind,
                   TableCompatUsage.hour_start >= since)).scalar() or 0
        total = int(total)
    except Exception:
        logger.debug("compat meter: window_sum failed", exc_info=True)
        return 0
    with _lock:
        _sum_cache[ck] = (now + _SUM_TTL, total)
    return total


def usage_for_key(key_id: int) -> dict:
    """{'search': {window: n, ...}, 'download': {...}} for all windows."""
    return {kind: {w: window_sum(key_id, kind, w) for w in WINDOWS}
            for kind in ("search", "download")}


def prune(retention_days: int) -> None:
    cutoff = _truncate_hour(datetime.now()) - timedelta(days=int(retention_days))
    database.execute(sa_delete(TableCompatUsage)
                     .where(TableCompatUsage.hour_start < cutoff))
    with _lock:
        _sum_cache.clear()


def reset_cache() -> None:
    """Test helper."""
    with _lock:
        _sum_cache.clear()

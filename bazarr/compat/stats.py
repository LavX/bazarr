"""Usage statistics aggregation for the Distribution Hub.

Reads the hourly compat_usage buckets and rolls them up into the overview
cards and the daily timeseries the management UI renders. Day bucketing is
dialect-aware (SQLite strftime vs Postgres to_char) so both backends produce
the same 'YYYY-MM-DD' keys.
"""
from __future__ import annotations
from datetime import datetime, timedelta

from app.database import (database, select, func, engine,
                          TableCompatUsage, TableCompatApiKeys)


def _since(days: int) -> datetime:
    return (datetime.now().replace(minute=0, second=0, microsecond=0)
            - timedelta(days=int(days)))


def _kind_total(kind: str, days: int, key_id: int | None = None) -> int:
    conds = [TableCompatUsage.kind == kind,
             TableCompatUsage.hour_start >= _since(days)]
    if key_id is not None:
        conds.append(TableCompatUsage.key_id == int(key_id))
    return int(database.execute(
        select(func.coalesce(func.sum(TableCompatUsage.count), 0)).where(*conds)
    ).scalar() or 0)


def _blocked_total(days: int) -> int:
    return int(database.execute(
        select(func.coalesce(func.sum(TableCompatUsage.blocked), 0))
        .where(TableCompatUsage.hour_start >= _since(days))
    ).scalar() or 0)


def overview() -> dict:
    """Totals for today/7d/30d per kind, active keys, blocked, top keys."""
    out = {"totals": {}, "blocked_30d": _blocked_total(30)}
    for label, days in (("today", 1), ("d7", 7), ("d30", 30)):
        out["totals"][label] = {
            "search": _kind_total("search", days),
            "download": _kind_total("download", days),
        }

    key_rows = database.execute(
        select(TableCompatApiKeys.id, TableCompatApiKeys.name,
               TableCompatApiKeys.key_prefix, TableCompatApiKeys.enabled,
               TableCompatApiKeys.last_used_at)).all()
    out["key_count"] = len(key_rows)
    out["enabled_count"] = sum(1 for r in key_rows if int(r.enabled) == 1)
    active_since = _since(30)
    out["active_count"] = sum(
        1 for r in key_rows if r.last_used_at and r.last_used_at >= active_since)

    # Top keys by 30d total usage.
    usage_rows = database.execute(
        select(TableCompatUsage.key_id,
               func.coalesce(func.sum(TableCompatUsage.count), 0).label("total"))
        .where(TableCompatUsage.hour_start >= _since(30))
        .group_by(TableCompatUsage.key_id)
        .order_by(func.coalesce(func.sum(TableCompatUsage.count), 0).desc())
        .limit(5)).all()
    names = {r.id: (r.name, r.key_prefix) for r in key_rows}
    out["top_keys"] = [
        {"key_id": r.key_id,
         "name": names.get(r.key_id, ("(deleted)", ""))[0],
         "prefix": names.get(r.key_id, ("", ""))[1],
         "total": int(r.total)}
        for r in usage_rows]
    return out


def _day_expr():
    if engine.dialect.name == "postgresql":
        return func.to_char(TableCompatUsage.hour_start, "YYYY-MM-DD")
    # sqlite (and others that support strftime)
    return func.strftime("%Y-%m-%d", TableCompatUsage.hour_start)


def timeseries(range_days: int = 30, key_id: int | None = None) -> dict:
    """Daily {date: {search, download}} for the trailing range, zero-filled."""
    range_days = max(1, min(int(range_days), 366))
    day = _day_expr()
    conds = [TableCompatUsage.hour_start >= _since(range_days)]
    if key_id is not None:
        conds.append(TableCompatUsage.key_id == int(key_id))
    rows = database.execute(
        select(day.label("day"), TableCompatUsage.kind,
               func.coalesce(func.sum(TableCompatUsage.count), 0).label("total"))
        .where(*conds)
        .group_by("day", TableCompatUsage.kind)).all()

    buckets: dict[str, dict] = {}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(range_days):
        d = (today - timedelta(days=range_days - 1 - i)).strftime("%Y-%m-%d")
        buckets[d] = {"date": d, "search": 0, "download": 0}
    for r in rows:
        b = buckets.get(r.day)
        if b is not None:
            b[r.kind] = int(r.total)
    return {"range_days": range_days,
            "series": [buckets[k] for k in sorted(buckets)]}

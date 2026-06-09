"""Tiered multi-window rate-limit evaluation for the Distribution Hub.

A key's effective limits resolve as: per-key custom override -> tier limits.
Every window (hour/day/week/month) is checked against the persisted meter; a
limit of 0 means unlimited for that window. The tightest binding window wins
the reported `remaining`/`reset`. If any window is exceeded the request is
denied with the offending window named.
"""
from __future__ import annotations
import time
from collections import namedtuple

from . import meter, tiers

Decision = namedtuple("Decision", "allowed window limit remaining reset_epoch")

_WINDOW_SECONDS = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000}
_ORDER = ("hour", "day", "week", "month")


def effective_limits(key_rec: dict, kind: str) -> dict:
    """Resolve {window: limit} for a key + kind (custom over tier)."""
    tier = tiers.get_tier(key_rec.get("tier"))
    base = dict(tier.get(kind, {}))
    custom = (key_rec.get("custom_limits") or {}).get(kind) or {}
    for w in _ORDER:
        if w in custom and custom[w] is not None:
            try:
                base[w] = int(custom[w])
            except (TypeError, ValueError):
                pass
    return {w: int(base.get(w, 0) or 0) for w in _ORDER}


def check(key_rec: dict, kind: str) -> Decision:
    """Evaluate all windows. Does NOT consume - call meter.record separately."""
    limits_map = effective_limits(key_rec, kind)
    key_id = int(key_rec["id"])
    now = int(time.time())
    binding = None
    for w in _ORDER:
        limit = limits_map[w]
        if limit <= 0:
            continue                       # unlimited for this window
        used = meter.window_sum(key_id, kind, w)
        # The meter rolls on hour-aligned buckets, so the soonest the count can
        # change is the next hour boundary. For the hour window report that boundary
        # instead of now+3600, which over-throttled a key capped at HH:59 by telling
        # it to wait until ~HH+1:59 even though the bucket resets at HH+1:00. Coarser
        # windows keep now+window.
        if w == "hour":
            reset = ((now // 3600) + 1) * 3600
        else:
            reset = now + _WINDOW_SECONDS[w]
        if used >= limit:
            return Decision(False, w, limit, 0, reset)
        remaining = limit - used
        if binding is None or remaining < binding.remaining:
            binding = Decision(True, w, limit, remaining, reset)
    if binding is None:
        return Decision(True, "none", 0, -1, now)   # fully unlimited
    return binding

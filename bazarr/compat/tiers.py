"""Rate-limit tier presets + config accessors for the Distribution Hub.

Tiers map a kind ('search' | 'download') to four window limits
(hour/day/week/month). A limit of 0 means unlimited for that window. The
operator can override preset numbers (or add tiers) via
settings.compat_endpoint.tiers; those are merged over the built-in presets.
"""
from __future__ import annotations
from app.config import settings

WINDOWS = ("hour", "day", "week", "month")

_PRESETS = {
    "free":      {"label": "Free",
                  "search":   {"hour": 100,  "day": 1000,  "week": 5000,   "month": 15000},
                  "download": {"hour": 20,   "day": 100,   "week": 400,    "month": 1000}},
    "basic":     {"label": "Basic",
                  "search":   {"hour": 500,  "day": 5000,  "week": 25000,  "month": 80000},
                  "download": {"hour": 100,  "day": 500,   "week": 2000,   "month": 6000}},
    "pro":       {"label": "Pro",
                  "search":   {"hour": 2000, "day": 25000, "week": 120000, "month": 400000},
                  "download": {"hour": 500,  "day": 3000,  "week": 12000,  "month": 35000}},
    "unlimited": {"label": "Unlimited",
                  "search":   {"hour": 0, "day": 0, "week": 0, "month": 0},
                  "download": {"hour": 0, "day": 0, "week": 0, "month": 0}},
}


def preset_tiers() -> dict:
    """A deep-ish copy of the built-in presets."""
    return {tid: {"label": spec["label"],
                  "search": dict(spec["search"]),
                  "download": dict(spec["download"])}
            for tid, spec in _PRESETS.items()}


def _merge_kind(base_kind: dict, override_kind) -> dict:
    merged = dict(base_kind)
    if isinstance(override_kind, dict):
        for w in WINDOWS:
            if w in override_kind and override_kind[w] is not None:
                try:
                    merged[w] = int(override_kind[w])
                except (TypeError, ValueError):
                    pass
    return merged


def all_tiers() -> dict:
    """Configured tiers merged over the presets (config overrides numbers)."""
    cfg = dict(settings.compat_endpoint.tiers or {})
    merged = preset_tiers()
    for tid, spec in cfg.items():
        if not isinstance(spec, dict):
            continue
        base = merged.get(tid) or {"label": tid,
                                   "search": {w: 0 for w in WINDOWS},
                                   "download": {w: 0 for w in WINDOWS}}
        merged[tid] = {
            "label": spec.get("label", base.get("label", tid)),
            "search": _merge_kind(base.get("search", {}), spec.get("search")),
            "download": _merge_kind(base.get("download", {}), spec.get("download")),
        }
    return merged


def default_tier_id() -> str:
    tid = settings.compat_endpoint.default_tier or "free"
    return tid if tid in all_tiers() else "free"


def get_tier(tier_id: str | None) -> dict:
    tiers = all_tiers()
    if tier_id and tier_id in tiers:
        return tiers[tier_id]
    return tiers.get(default_tier_id()) or preset_tiers()["free"]

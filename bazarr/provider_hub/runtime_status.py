"""Volatile, validated provider status reported by worker events."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Any

_lock = RLock()
_statuses: dict[str, dict[str, Any]] = {}
_generations: dict[str, int] = {}
_configuration_generations: dict[str, int] = {}
_MAX_COUNT = 10_000_000


def _nullable_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _nullable_count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else None


def _nullable_reset(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 64 else None


def _reset_has_passed(reset_at: str | None) -> bool:
    """A date-only reset counts as passed from the start of that day, UTC. A
    null or unparseable reset never counts as passed."""
    if not reset_at:
        return False
    try:
        parsed = datetime.fromisoformat(reset_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


def generation(provider_id: str) -> int:
    with _lock:
        return _generations.get(provider_id, 0)


def configuration_generation(provider_id: str) -> int:
    with _lock:
        return _configuration_generations.get(provider_id, 0)


def worker_started(provider_id: str) -> None:
    """Drop status for a fresh worker without disguising a config change."""
    if not isinstance(provider_id, str) or not provider_id:
        return
    with _lock:
        _statuses.pop(provider_id, None)
        _generations[provider_id] = _generations.get(provider_id, 0) + 1


def consume(provider_id: str, events: Any, expected_generation: int | None = None) -> None:
    if not isinstance(provider_id, str) or not provider_id or not isinstance(events, list):
        return
    for event in events[:8]:
        if not isinstance(event, dict) or event.get("type") != "translation_quota":
            continue
        status = {
            "entitled": _nullable_bool(event.get("entitled")),
            "exhausted": _nullable_bool(event.get("exhausted")),
            "remaining": _nullable_count(event.get("remaining")),
            "limit": _nullable_count(event.get("limit")),
            "reset_at": _nullable_reset(event.get("reset_at")),
            "reported_at": datetime.now(timezone.utc).isoformat(),
        }
        if all(status[field] is None for field in
               ("entitled", "exhausted", "remaining", "limit", "reset_at")):
            continue
        with _lock:
            if expected_generation is not None and _generations.get(provider_id, 0) != expected_generation:
                return
            _statuses[provider_id] = status


def get(provider_id: str) -> dict[str, Any] | None:
    with _lock:
        status = _statuses.get(provider_id)
        if status is None:
            return None
        # A passed reset makes the status stale, so it reads as unknown. A
        # not-entitled status carries no reset promise and stays visible.
        if status["entitled"] is not False and _reset_has_passed(status["reset_at"]):
            return None
        return dict(status)


def clear(provider_id: str | None = None) -> None:
    with _lock:
        if provider_id is None:
            _statuses.clear()
            _generations.clear()
            _configuration_generations.clear()
        else:
            _statuses.pop(provider_id, None)
            _generations[provider_id] = _generations.get(provider_id, 0) + 1
            _configuration_generations[provider_id] = (
                _configuration_generations.get(provider_id, 0) + 1
            )

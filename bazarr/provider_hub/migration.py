# coding=utf-8
from __future__ import annotations

MIGRATED_BUILT_IN_PROVIDER_IDS = frozenset({
    "gestdown",
})


def can_shadow_built_in_provider(provider_id: str, trusted: bool) -> bool:
    return bool(trusted) and str(provider_id or "") in MIGRATED_BUILT_IN_PROVIDER_IDS


def validation_built_in_provider_ids(
    provider_id: str,
    built_in_provider_ids: set[str] | None,
    trusted: bool,
) -> set[str]:
    provider_ids = set(built_in_provider_ids or set())
    if can_shadow_built_in_provider(provider_id, trusted):
        provider_ids.discard(str(provider_id or ""))
    return provider_ids

# coding=utf-8
"""Bringing what the runtime stores in line with a newly activated translator release.

A release can declare a field secret that was plain text, drop a field, or ask for one that was never set.
On activation every engine instance and back-end connection of the plugin is checked against the new
manifest: those whose settings still fit are migrated (a newly secret value moves into the secret store, a
dropped field leaves the settings), and those that do not are left as they were, turned off and flagged for
the person to review. Nothing here prints a value.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from provider_hub.plugin_manifest import parse_plugin_manifest

from .destinations import DestinationSchema
from .field_schema import is_secret_field, validate_values
from .store import CONNECTION, INSTANCE, TranslatorStore

logger = logging.getLogger(__name__)

# Kept by the host in columns of their own, so never part of a connection's settings JSON.
HOST_KEYS = frozenset({"model", "price_input_per_million", "price_output_per_million"})

NEEDS_ATTENTION = ("Some saved translation settings no longer fit this version of the plugin and were turned "
                   "off. Open Settings > Translation to review them.")


def bring_up_to_date(store: TranslatorStore, plugin_id: str, manifest: Mapping[str, Any]) -> str | None:
    """Migrate the plugin's owners to ``manifest``. Returns a message when some had to be held back."""
    parsed = parse_plugin_manifest(dict(manifest))
    backend_fields = dict(parsed.backend_fields)
    backend_endpoints = dict(parsed.backend_endpoints)

    def schema_for(kind: str, row) -> DestinationSchema:
        if kind == INSTANCE:
            return DestinationSchema(fields=parsed.settings_fields, endpoints=parsed.endpoints)
        fields = backend_fields.get(row.backend_type)
        if fields is None:
            return DestinationSchema()
        return DestinationSchema(fields=fields, endpoints=backend_endpoints.get(row.backend_type, ()))

    def fields_of(kind: str, row) -> list[Mapping[str, Any]] | None:
        if kind == INSTANCE:
            return list(parsed.settings_fields)
        if row.backend_type not in backend_fields:
            return None
        return [item for item in backend_fields[row.backend_type] if item["key"] not in HOST_KEYS]

    blocked = []
    for kind, row in store.owners_of_plugin(plugin_id):
        fields = fields_of(kind, row)
        if fields is None or not _fits(store, (kind, row.id), row, fields):
            blocked.append((kind, row.id))

    def prune(kind: str, row):
        """The settings without keys the new release no longer declares, or None when nothing is dropped."""
        fields = fields_of(kind, row)
        if fields is None:
            return None
        known = {item["key"] for item in fields}
        stored = store.settings_of(row)
        kept = {key: value for key, value in stored.items() if key in known}
        return kept if kept != stored else None

    store.apply_plugin_update(plugin_id, schema_for, new_settings=prune, plugin_version=parsed.version,
                              settings_version=parsed.settings_version, skip=blocked)
    if blocked:
        store.hold_back_for_attention(blocked)
        logger.warning("%d saved setting(s) of translator %s do not fit its new release and were turned off",
                       len(blocked), plugin_id)
        return NEEDS_ATTENTION
    return None


def _fits(store: TranslatorStore, owner, row, fields) -> bool:
    """Whether the owner's settings, once its newly secret values have moved, satisfy the new manifest."""
    settings = store.settings_of(row)
    secret_keys = {item["key"] for item in fields if is_secret_field(item)}
    known = {item["key"] for item in fields}
    moved = [key for key in secret_keys if settings.get(key) not in (None, "", [])]
    remaining = {key: value for key, value in settings.items() if key in known and key not in secret_keys}
    held = frozenset(store.secret_summary(owner, frozenset())) | frozenset(moved)
    return not validate_values(fields, remaining, stored_secrets=held).errors

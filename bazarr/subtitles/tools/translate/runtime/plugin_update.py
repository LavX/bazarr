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
from .field_schema import is_secret_field
from .host_fields import HOST_KEYS, host_of, validate_owner
from .store import INSTANCE, TranslatorStore

logger = logging.getLogger(__name__)

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
        return list(backend_fields[row.backend_type])

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
        known = _settings_keys(kind, fields)
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


def _settings_keys(kind: str, fields) -> set[str]:
    """The keys of the fields an owner keeps in its settings JSON. A connection keeps its model and prices in
    columns of their own, so those are not among them; an engine has no such columns, and a field of an engine
    that happens to be called ``model`` is an ordinary setting."""
    keys = {item["key"] for item in fields}
    return keys if kind == INSTANCE else keys - HOST_KEYS


def _fits(store: TranslatorStore, owner, row, fields) -> bool:
    """Whether the owner's settings, once its newly secret values have moved, satisfy the new manifest.

    A connection is checked together with the model and prices its columns hold, with the same function a save
    uses, so a release that requires a model, tightens a price limit or shows a field only for one model holds
    back the connections that do not meet it.
    """
    settings = store.settings_of(row)
    secret_keys = {item["key"] for item in fields if is_secret_field(item)}
    known = _settings_keys(owner[0], fields)
    moved = [key for key in secret_keys if settings.get(key) not in (None, "", [])]
    remaining = {key: value for key, value in settings.items() if key in known and key not in secret_keys}
    held = frozenset(store.secret_summary(owner, frozenset())) | frozenset(moved)
    return not validate_owner(fields, remaining, held, None if owner[0] == INSTANCE else host_of(row)).errors

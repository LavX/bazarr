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

from .destinations import DestinationSchema, effective_destinations
from .field_schema import is_secret_field
from .host_fields import HOST_KEYS, host_of, validate_owner
from .store import INSTANCE, TranslatorStore

logger = logging.getLogger(__name__)

NEEDS_ATTENTION = ("Some saved translation settings no longer fit this version of the plugin and were turned "
                   "off. Open Translation in the main menu to review them.")


def bring_up_to_date(store: TranslatorStore, plugin_id: str, manifest: Mapping[str, Any],
                     retained_manifest: Mapping[str, Any] | None = None) -> str | None:
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

    # First of all, and for every owner whether or not it fits: a value of a field that is secret now leaves the
    # settings for the secret store. Whatever fails after this cannot leave a credential in the clear.
    store.move_newly_secret_values(plugin_id, schema_for)

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

    retained = parse_plugin_manifest(dict(retained_manifest)) if retained_manifest else None

    def retained_schema(kind: str, row) -> DestinationSchema:
        return DestinationSchema(fields=tuple(_owner_fields(retained, kind, row))) if retained else DestinationSchema()

    store.drop_undeclared_secrets(plugin_id, schema_for, retained_schema=retained_schema)
    store.apply_plugin_update(plugin_id, schema_for, new_settings=prune, plugin_version=parsed.version,
                              settings_version=parsed.settings_version, skip=blocked)
    if blocked:
        store.hold_back_for_attention(blocked)
        logger.warning("%d saved setting(s) of translator %s do not fit its new release and were turned off",
                       len(blocked), plugin_id)
        return NEEDS_ATTENTION
    return None


def _owner_fields(plugin, kind, row):
    if kind == INSTANCE:
        return plugin.settings_fields
    return dict(plugin.backend_fields).get(row.backend_type, ())


def _owner_identity(kind, row):
    identity = [row.created_at.isoformat(), row.plugin_id]
    if kind != INSTANCE:
        identity.extend((row.instance_id, row.backend_type))
    return identity


def _secret_fields(fields):
    return {item["key"] for item in fields if is_secret_field(item)}


def capture_release_settings(store: TranslatorStore, plugin_id: str, manifest, next_manifest) -> dict:
    """Capture just the prior release's settings for each owner it serves."""
    old = parse_plugin_manifest(manifest)
    new = parse_plugin_manifest(next_manifest)
    snapshots = {}
    for kind, row in store.owners_of_plugin(plugin_id):
        fields = _owner_fields(old, kind, row)
        future = _owner_fields(new, kind, row)
        excluded = _secret_fields(fields) | _secret_fields(future) | store.secret_field_names((kind, row.id))
        old_keys = {item["key"] for item in fields} - excluded
        if not fields and not future:
            continue
        keys = _settings_keys(kind, fields) & old_keys
        values = {key: value for key, value in store.settings_of(row).items() if key in keys}
        if kind != INSTANCE:
            values.update({key: value for key, value in host_of(row).items()
                           if key in old_keys})
        snapshots[f"{kind}:{row.id}"] = {"identity": _owner_identity(kind, row), "revision": row.revision,
                                         "values": values}
    return snapshots


def rollback_settings_destinations(store: TranslatorStore, plugin_id: str, target_manifest, snapshots) -> set[str]:
    """Previously approved typed addresses that the surviving settings copies contain."""
    target = parse_plugin_manifest(target_manifest)
    restored = set()
    for kind, row in store.owners_of_plugin(plugin_id):
        snapshot = snapshots.get(f"{kind}:{row.id}")
        if snapshot is None or _owner_identity(kind, row) != snapshot["identity"]:
            continue
        stored = snapshot["values"]
        fields = _owner_fields(target, kind, row)
        held = store.secret_field_names((kind, row.id))
        schema = DestinationSchema(fields=tuple(item for item in fields
                                                if item["key"] not in held and not is_secret_field(item)))
        restored.update(destination for destination in effective_destinations(schema, stored)
                        if not destination.startswith("invalid:"))
    return restored


def restore_release_settings(store: TranslatorStore, plugin_id, current_manifest, target_manifest,
                             snapshots, revisions) -> None:
    """Restore surviving owners at their cutover revisions. A replay cannot undo a later save."""
    current = parse_plugin_manifest(current_manifest)
    target = parse_plugin_manifest(target_manifest)
    with store.transaction():
        for kind, row in store.owners_of_plugin(plugin_id):
            key = f"{kind}:{row.id}"
            snapshot = snapshots.get(key)
            if row.revision != revisions.get(key):
                continue
            matches = snapshot is not None and _owner_identity(kind, row) == snapshot["identity"]
            before = _owner_fields(current, kind, row)
            after = _owner_fields(target, kind, row)
            # A later plain-text edit must never overwrite a stored credential or undo a clear.
            target_secrets = _secret_fields(after)
            excluded = _secret_fields(before) | target_secrets | store.secret_field_names((kind, row.id))
            owned = (_settings_keys(kind, before) | _settings_keys(kind, after)) - excluded if matches else set()
            settings = {key: value for key, value in store.settings_of(row).items() if key not in owned | target_secrets}
            host = {}
            if matches:
                incoming = _settings_keys(kind, after) - excluded
                settings.update({key: value for key, value in snapshot["values"].items() if key in incoming})
                if kind != INSTANCE:
                    host = {key: value for key, value in snapshot["values"].items()
                            if key in HOST_KEYS and key not in excluded and any(item["key"] == key for item in after)}
            store.restore_release_settings(kind, row, settings, host)


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
    remaining = {key: value for key, value in settings.items() if key in known and key not in secret_keys}
    held = frozenset(store.secret_summary(owner, frozenset(), fields=secret_keys))
    # What the stored secrets hold is checked too, in memory, against the new limits (a key list that is now
    # too long, a key that is now too long), not just that they are there.
    contents = store.secret_contents(owner, fields)
    return not validate_owner(fields, remaining, held, None if owner[0] == INSTANCE else host_of(row),
                              contents).errors

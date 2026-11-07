# coding=utf-8
"""The typed settings service behind the Settings > Translation page.

It reads and writes engine instances, their back-end connections, their secrets and the two engine pointers
through ``TranslatorStore``, with the manifest as the only source of what a setting is. Nothing here knows
the name of a setting: a value is checked by the type its field declares, a secret is whatever the field
declares secret, and a destination is whatever the field marks as one.

Rules kept here:

* A secret goes in and never comes out. The snapshot says whether one is set (and, for a long one, its last
  four characters, or for a list the number of entries); it never carries a value.
* A value that names a new destination (a base URL the user typed) needs the user's consent in the same
  request, and a changed destination never keeps a stored secret: the store parks the owner as awaiting its
  secrets until they are entered again.
* The host owns the connection's model and its two prices. They live in columns, not in the settings JSON,
  and the snapshot adds them to every back-end type's fields so one generic renderer shows them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from provider_hub.plugin_manifest import parse_plugin_manifest

from .destinations import DestinationSchema, effective_destinations, normalize_destination
from .field_schema import ValidatedValues, field_visible, is_secret_field, validate_values
from .store import (CONNECTION, INSTANCE, OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore, TranslatorStoreError)

logger = logging.getLogger(__name__)

MODEL_KEY = "model"
PRICE_KEYS = ("price_input_per_million", "price_output_per_million")
HOST_KEYS = frozenset({MODEL_KEY, *PRICE_KEYS})

# The fields every back-end type gets, ahead of what the manifest declares. A manifest that declares its own
# ``model`` field keeps its declaration (label, help, limits) and only loses the duplicate.
HOST_FIELDS: tuple[dict[str, Any], ...] = (
    {"key": MODEL_KEY, "type": "options", "label": "Model", "host": True},
    {"key": PRICE_KEYS[0], "type": "number", "label": "Input price per million tokens", "min": 0, "default": 0,
     "group": "Prices", "host": True},
    {"key": PRICE_KEYS[1], "type": "number", "label": "Output price per million tokens", "min": 0, "default": 0,
     "group": "Prices", "host": True},
)


class ServiceError(Exception):
    """A request the service refuses. ``status`` is the HTTP status it maps to."""

    status = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}

    code = "bad_request"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class StaleRevisionError(ServiceError):
    status = 409
    code = "stale_revision"


class ConsentRequiredError(ServiceError):
    status = 409
    code = "consent_required"


class InvalidValuesError(ServiceError):
    status = 400
    code = "invalid"


class PluginUnavailableError(ServiceError):
    status = 409
    code = "plugin_unavailable"


@dataclass(frozen=True)
class InstalledPlugin:
    plugin_id: str
    name: str
    version: str
    active: bool
    manifest: Mapping[str, Any]


def installed_plugin(plugin_id: str) -> InstalledPlugin | None:
    """The plugin's active installation from the Provider Hub state, or None."""
    from provider_hub import state as hub_state

    item = (hub_state.load_state().get("installations") or {}).get(plugin_id)
    if not isinstance(item, dict) or not isinstance(item.get("manifest"), dict):
        return None
    manifest = item["manifest"]
    active = bool(item.get("active_version")) and item.get("state") != "removed"
    return InstalledPlugin(plugin_id, str(item.get("name") or manifest.get("name") or plugin_id),
                           str(item.get("active_version") or manifest.get("version") or ""), active, manifest)


# Hooks D8 binds to the worker. With no worker, a value that passes the schema is accepted as is and the
# model list is unavailable, which the page shows as a free-text model field.
PluginValidator = Callable[[str, str, str, Mapping[str, Any]], Mapping[str, str] | None]
# (engine instance id, back-end type or '' for the engine's own field, field key, draft values without secrets)
OptionsSource = Callable[[int, str, str, Mapping[str, Any]], Mapping[str, Any] | None]


def _json_secret(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _register_for_scrubbing(value: Any) -> None:
    try:
        from secret_store.scrub import register_secret_values

        register_secret_values([item for item in (value if isinstance(value, list) else [value]) if item])
    except Exception:
        logger.debug("Could not register a translator secret for log scrubbing", exc_info=True)


class TranslatorSettingsService:
    def __init__(self, store: TranslatorStore, *, plugin_lookup: Callable[[str], InstalledPlugin | None] | None = None,
                 plugin_validator: PluginValidator | None = None, options_source: OptionsSource | None = None):
        self._store = store
        self._plugin = plugin_lookup or installed_plugin
        self._validator = plugin_validator
        self._options_source = options_source

    # ------------------------------------------------------------- schema
    def _parsed(self, plugin: InstalledPlugin | None):
        if plugin is None:
            return None
        try:
            return parse_plugin_manifest(dict(plugin.manifest))
        except Exception:
            logger.warning("The manifest of translator %s does not parse", plugin.plugin_id, exc_info=True)
            return None

    def _engine_schema(self, plugin: InstalledPlugin | None) -> DestinationSchema:
        parsed = self._parsed(plugin)
        if parsed is None:
            return DestinationSchema()
        return DestinationSchema(fields=parsed.settings_fields, endpoints=parsed.endpoints)

    def _backend_schema(self, plugin: InstalledPlugin | None, backend_type: str) -> DestinationSchema | None:
        parsed = self._parsed(plugin)
        if parsed is None:
            return None
        fields = dict(parsed.backend_fields)
        if backend_type not in fields:
            return None
        return DestinationSchema(fields=fields[backend_type], endpoints=dict(parsed.backend_endpoints)[backend_type])

    @staticmethod
    def _with_host_fields(fields) -> list[dict[str, Any]]:
        declared = {item["key"] for item in fields}
        return [dict(item) for item in HOST_FIELDS if item["key"] not in declared] + [dict(item) for item in fields]

    def _serving_plugin(self, instance, connection=None) -> InstalledPlugin | None:
        plugin_id = (connection.plugin_id if connection is not None and connection.plugin_id else instance.plugin_id)
        return self._plugin(plugin_id)

    def _display_schema(self, plugin: InstalledPlugin | None) -> dict[str, Any] | None:
        parsed = self._parsed(plugin)
        if parsed is None:
            return None
        raw_types = ((parsed.raw.get("settings") or {}).get("backend_types") or {})
        backend_types = {}
        for type_id, fields in parsed.backend_fields:
            raw = raw_types.get(type_id) if isinstance(raw_types.get(type_id), dict) else {}
            backend_types[type_id] = {
                "label": str(raw.get("label") or type_id), "description": str(raw.get("description") or ""),
                "fields": self._with_host_fields(fields),
                "endpoints": list(dict(parsed.backend_endpoints).get(type_id, ()))}
        return {"settings_version": parsed.settings_version, "fields": [dict(item) for item in parsed.settings_fields],
                "endpoints": list(parsed.endpoints), "backend_types": backend_types}

    # ----------------------------------------------------------- snapshot
    def pointers(self) -> tuple[int | None, int | None]:
        return self._store.pointers()

    def snapshot(self) -> dict[str, Any]:
        engine, fallback = self.pointers()
        return {"enabled": True, "engine": engine, "fallback_engine": fallback,
                "instances": [self.instance_view(row) for row in self._store.list_instances()]}

    def instance_view(self, instance) -> dict[str, Any]:
        store = self._store
        plugin = self._plugin(instance.plugin_id)
        schema = self._engine_schema(plugin)
        owner = (INSTANCE, instance.id)
        settings = store.settings_of(instance)
        destinations = effective_destinations(schema, settings)
        return {
            "id": instance.id, "plugin_id": instance.plugin_id, "name": instance.name or (plugin.name if plugin else ""),
            "official": instance.plugin_id == OFFICIAL_ENGINE_PLUGIN_ID, "enabled": bool(instance.enabled),
            "revision": instance.revision, "awaiting_secrets": bool(instance.awaiting_secrets),
            "configured": store.instance_configured(instance), "settings_version": instance.settings_version,
            "plugin_version": instance.plugin_version,
            "plugin": None if plugin is None else {"name": plugin.name, "version": plugin.version,
                                                    "active": plugin.active},
            "schema": self._display_schema(plugin), "values": settings,
            "secrets": self._secret_view(owner, schema.fields, destinations),
            "connections": [self.connection_view(instance, row) for row in store.list_connections(instance.id)],
        }

    def connection_view(self, instance, connection) -> dict[str, Any]:
        plugin = self._serving_plugin(instance, connection)
        schema = self._backend_schema(plugin, connection.backend_type) or DestinationSchema()
        settings = self._store.settings_of(connection)
        destinations = effective_destinations(schema, settings)
        values = dict(settings)
        values[MODEL_KEY] = connection.model
        values[PRICE_KEYS[0]] = connection.price_input_per_million
        values[PRICE_KEYS[1]] = connection.price_output_per_million
        return {
            "id": connection.id, "instance_id": connection.instance_id, "position": connection.position,
            "backend_type": connection.backend_type, "plugin_id": connection.plugin_id, "model": connection.model,
            "enabled": bool(connection.enabled), "awaiting_secrets": bool(connection.awaiting_secrets),
            "usable": self._store.connection_usable(connection), "settings_version": connection.settings_version,
            "values": values, "secrets": self._secret_view((CONNECTION, connection.id), schema.fields, destinations),
        }

    def _secret_view(self, owner, fields, destinations) -> dict[str, dict[str, Any]]:
        """What may be said about each stored secret: that it is set, stale, and a hint. Never a value."""
        summary = self._store.secret_summary(owner, destinations)
        lists = {item["key"] for item in fields if item.get("type") in ("key_list", "list")}
        # frozenset() is covered by every binding, so this reads every secret this owner holds. The values
        # are counted here and dropped; only the count leaves.
        held = self._store.secrets_for_request(owner, frozenset()) if lists & set(summary) else {}
        view = {}
        for name, entry in summary.items():
            entry = dict(entry)
            if name in lists:
                entry.pop("last4", None)
                try:
                    count = len(json.loads(held[name])) if name in held else None
                except (ValueError, TypeError):
                    count = None
                if count is not None:
                    entry["count"] = count
            view[name] = entry
        return view

    # ------------------------------------------------------------ pointers
    def set_pointers(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        notices = []
        try:
            for key, apply in (("engine", self._store.set_engine), ("fallback_engine", self._store.set_fallback)):
                if key in payload:
                    value = payload[key]
                    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                        raise ServiceError(f"{key} must be an engine instance id or null")
                    notices += [self._notice(item) for item in apply(value).notices]
        except TranslatorStoreError as error:
            raise ServiceError(str(error)) from error
        return notices

    @staticmethod
    def _notice(item) -> dict[str, Any]:
        return {"code": item.code, "message": item.message, "instance_id": item.instance_id}

    # ------------------------------------------------------------ engines
    def _instance(self, instance_id: int):
        row = self._store.get_instance(instance_id)
        if row is None:
            raise NotFoundError(f"no engine instance {instance_id}")
        return row

    def save_engine(self, instance_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        instance = self._instance(instance_id)
        notices: list[dict[str, Any]] = []
        expected = payload.get("expected_revision")
        if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
            raise ServiceError("expected_revision must be a number")
        if expected is not None and instance.revision != expected:
            raise StaleRevisionError("The engine settings changed since they were loaded.", revision=instance.revision)
        values = payload.get("values")
        if values is not None and not isinstance(values, dict):
            raise ServiceError("values must be an object")
        clear = self._clear_list(payload)
        enabled = payload.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ServiceError("enabled must be true or false")
        if values is not None or clear:
            plugin = self._plugin(instance.plugin_id)
            if plugin is None or not plugin.active:
                raise PluginUnavailableError("The plugin for this engine is not installed.")
            self._save_engine_values(instance, plugin, values, clear, payload)
        if enabled is not None and bool(instance.enabled) != enabled:
            notices += [self._notice(item) for item in self._store.set_instance_enabled(instance_id, enabled).notices]
        return {"engine": self.instance_view(self._instance(instance_id)), "notices": notices}

    def _save_engine_values(self, instance, plugin, values, clear, payload) -> None:
        schema = self._engine_schema(plugin)
        owner = (INSTANCE, instance.id)
        before = self._store.settings_of(instance)
        stored = frozenset(self._store.secret_summary(owner, frozenset()))
        validated = ValidatedValues()
        if values is not None:
            validated = self._validate(schema, dict(values), stored, plugin.plugin_id, "engine", "")
        validated = self._drop_cleared(validated, clear, schema)
        after = validated.settings if values is not None else before
        self._require_consent(schema, before, after, payload, new_owner=False)
        if values is not None:
            parsed = self._parsed(plugin)
            self._store.save_instance_settings(
                instance.id, after, schema=schema, settings_version=parsed.settings_version if parsed else None,
                plugin_version=plugin.version)
        self._write_secrets(owner, validated, clear, effective_destinations(schema, after))

    # -------------------------------------------------------- connections
    def create_connection(self, instance_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        instance = self._instance(instance_id)
        backend_type = payload.get("backend_type")
        if not isinstance(backend_type, str) or not backend_type:
            raise ServiceError("backend_type is required")
        plugin_id = payload.get("plugin_id") or ""
        if not isinstance(plugin_id, str):
            raise ServiceError("plugin_id must be text")
        plugin = self._plugin(plugin_id or instance.plugin_id)
        if plugin is None or not plugin.active:
            raise PluginUnavailableError("The plugin that serves this back end is not installed.")
        schema = self._backend_schema(plugin, backend_type)
        if schema is None:
            raise ServiceError(f"{plugin.name} has no back end of type {backend_type}")
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            raise ServiceError("values must be an object")
        host, plain = self._split_host(values)
        validated = self._validate(schema, plain, frozenset(), plugin.plugin_id, "connection", backend_type)
        self._require_consent(schema, {}, validated.settings, payload, new_owner=True)
        parsed = self._parsed(plugin)
        row = self._store.add_connection(
            instance_id, backend_type, plugin_id=plugin_id, model=host[MODEL_KEY], settings=validated.settings,
            settings_version=parsed.settings_version if parsed else None, plugin_version=plugin.version,
            price_input_per_million=host[PRICE_KEYS[0]], price_output_per_million=host[PRICE_KEYS[1]],
            enabled=payload.get("enabled", True) is not False)
        self._write_secrets((CONNECTION, row.id), validated, [], effective_destinations(schema, validated.settings))
        return {"connection": self.connection_view(instance, self._store.get_connection(row.id)), "notices": []}

    def update_connection(self, connection_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        connection = self._connection(connection_id)
        instance = self._instance(connection.instance_id)
        plugin = self._serving_plugin(instance, connection)
        schema = self._backend_schema(plugin, connection.backend_type) if plugin and plugin.active else None
        enabled = payload.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ServiceError("enabled must be true or false")
        values = payload.get("values")
        if values is not None and not isinstance(values, dict):
            raise ServiceError("values must be an object")
        clear = self._clear_list(payload)
        if schema is None:
            raise PluginUnavailableError("The plugin that serves this back end is not installed.")
        if values is None and not clear:
            if enabled is not None:
                self._store.update_connection(connection_id, schema=schema, enabled=enabled)
        else:
            owner = (CONNECTION, connection_id)
            before = self._store.settings_of(connection)
            stored = frozenset(self._store.secret_summary(owner, frozenset()))
            host, plain = self._split_host(values or {}, keep=connection)
            validated = ValidatedValues()
            if values is not None:
                validated = self._validate(schema, plain, stored, plugin.plugin_id, "connection",
                                           connection.backend_type)
            validated = self._drop_cleared(validated, clear, schema)
            after = validated.settings if values is not None else before
            self._require_consent(schema, before, after, payload, new_owner=False)
            parsed = self._parsed(plugin)
            self._store.update_connection(
                connection_id, schema=schema, model=host[MODEL_KEY], settings=after,
                settings_version=parsed.settings_version if parsed else None, plugin_version=plugin.version,
                price_input_per_million=host[PRICE_KEYS[0]], price_output_per_million=host[PRICE_KEYS[1]],
                enabled=enabled)
            self._write_secrets(owner, validated, clear, effective_destinations(schema, after))
        return {"connection": self.connection_view(instance, self._connection(connection_id)), "notices": []}

    def delete_connection(self, connection_id: int) -> None:
        self._connection(connection_id)
        self._store.delete_connection(connection_id)

    def reorder_connections(self, instance_id: int, ordered: Any) -> list[dict[str, Any]]:
        instance = self._instance(instance_id)
        if (not isinstance(ordered, list)
                or any(isinstance(item, bool) or not isinstance(item, int) for item in ordered)):
            raise ServiceError("order must be a list of connection ids")
        try:
            self._store.reorder_connections(instance_id, ordered)
        except TranslatorStoreError as error:
            raise ServiceError(str(error)) from error
        return [self.connection_view(instance, row) for row in self._store.list_connections(instance_id)]

    def _connection(self, connection_id: int):
        row = self._store.get_connection(connection_id)
        if row is None:
            raise NotFoundError(f"no connection {connection_id}")
        return row

    # ------------------------------------------------------------ options
    def options(self, instance_id: int, backend_type: str, field_key: str,
                draft: Mapping[str, Any]) -> dict[str, Any]:
        """The live choices of an ``options`` field, or ``available: false`` so the page offers free text."""
        self._instance(instance_id)
        source = self._options_source
        answer = source(instance_id, backend_type, field_key, draft) if source is not None else None
        if not answer or not isinstance(answer.get("items"), list):
            return {"available": False, "items": [], "reason": "The model list could not be fetched."}
        return {"available": True, "items": answer["items"], "reason": ""}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _clear_list(payload: Mapping[str, Any]) -> list[str]:
        clear = payload.get("clear_secrets") or []
        if not isinstance(clear, list) or any(not isinstance(item, str) for item in clear):
            raise ServiceError("clear_secrets must be a list of field names")
        return clear

    @staticmethod
    def _split_host(values: Mapping[str, Any], keep=None) -> tuple[dict[str, Any], dict[str, Any]]:
        """Take the model and the prices out of a connection's values; the rest is the back end's settings."""
        host: dict[str, Any] = {
            MODEL_KEY: keep.model if keep is not None else "",
            PRICE_KEYS[0]: keep.price_input_per_million if keep is not None else 0,
            PRICE_KEYS[1]: keep.price_output_per_million if keep is not None else 0}
        plain = dict(values)
        for key in HOST_KEYS:
            if key in plain:
                value = plain.pop(key)
                if key == MODEL_KEY:
                    if not isinstance(value, str):
                        raise InvalidValuesError("The values are not valid.", field_errors={key: "must be text"})
                    host[key] = value
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value:
                    raise InvalidValuesError("The values are not valid.",
                                             field_errors={key: "must be a number from 0 up"})
                else:
                    host[key] = value
        return host, plain

    def _validate(self, schema: DestinationSchema, values: dict[str, Any], stored: frozenset[str], plugin_id: str,
                  scope: str, backend_type: str) -> ValidatedValues:
        validated = validate_values(schema.fields, values, stored_secrets=stored)
        errors = dict(validated.errors)
        if not errors and self._validator is not None:
            merged = {**validated.settings, **validated.secrets}
            try:
                errors.update(dict(self._validator(plugin_id, scope, backend_type, merged) or {}))
            except Exception:
                logger.exception("The plugin could not validate its settings")
                raise PluginUnavailableError("The plugin could not check these settings.")
        if errors:
            raise InvalidValuesError("The values are not valid.", field_errors=errors)
        return validated

    @staticmethod
    def _drop_cleared(validated: ValidatedValues, clear: list[str], schema: DestinationSchema) -> ValidatedValues:
        secret_fields = {item["key"] for item in schema.fields if is_secret_field(item)}
        unknown = [name for name in clear if name not in secret_fields]
        if unknown:
            raise InvalidValuesError("The values are not valid.",
                                     field_errors={name: "is not a secret of this engine" for name in unknown})
        for name in clear:
            validated.secrets.pop(name, None)
        return validated

    def _require_consent(self, schema: DestinationSchema, before: Mapping[str, Any], after: Mapping[str, Any],
                         payload: Mapping[str, Any], *, new_owner: bool) -> None:
        """A destination the user typed, that the owner did not have, waits for the user's say-so.

        The manifest's own defaults and fixed origins are not asked about here: the plugin's consent screen
        covered them when it was installed.
        """
        typed = DestinationSchema(fields=schema.fields)
        baseline = effective_destinations(typed, {})
        new = effective_destinations(typed, after) - effective_destinations(typed, {} if new_owner else before) - baseline
        accepted = payload.get("accept_destinations") or []
        if not isinstance(accepted, list):
            raise ServiceError("accept_destinations must be a list")
        missing = sorted(new - {normalize_destination(item) for item in accepted if isinstance(item, str)})
        if missing:
            raise ConsentRequiredError(
                "These addresses are new. Check that you trust them: the text to translate, and the key if you "
                "entered one, are sent there.", destinations=missing)

    def _write_secrets(self, owner, validated: ValidatedValues, clear: list[str], destinations) -> None:
        for name in clear:
            self._store.clear_secret(owner, name, destinations=destinations)
        for name, value in validated.secrets.items():
            self._store.set_secret(owner, name, _json_secret(value), destinations=destinations)
            if isinstance(value, list):
                _register_for_scrubbing(value)


__all__ = [
    "ConsentRequiredError", "InstalledPlugin", "InvalidValuesError", "NotFoundError", "PluginUnavailableError",
    "ServiceError", "StaleRevisionError", "TranslatorSettingsService", "field_visible", "installed_plugin"]

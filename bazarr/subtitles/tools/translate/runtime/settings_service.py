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
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from provider_hub.plugin_manifest import parse_plugin_manifest

from .destinations import DestinationError, DestinationSchema, effective_destinations, normalize_destination
from .field_schema import (ValidatedValues, field_visible, is_finite_number, is_secret_field, validate_values)
from .store import (CONNECTION, INSTANCE, OFFICIAL_ENGINE_PLUGIN_ID, StaleRevisionError as StoreStale,
                    TranslatorStore, TranslatorStoreError)

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


class RevisionRequiredError(ServiceError):
    status = 400
    code = "expected_revision_required"


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


# Hooks the translation worker binds once it exists. With no worker, a value that passes the schema is accepted as is and the
# model list is unavailable, which the page shows as a free-text model field.
PluginValidator = Callable[[str, str, str, Mapping[str, Any]], Mapping[str, str] | None]
# (engine instance id, back-end type or '' for the engine's own field, field key, draft values without secrets)
OptionsSource = Callable[[int, str, str, Mapping[str, Any]], Mapping[str, Any] | None]


def _json_secret(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _clean(value: Any) -> Any:
    """The value as JSON can carry it: a stored NaN or infinity becomes null instead of breaking the response."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _without_unsafe_addresses(fields, settings: Mapping[str, Any]) -> dict[str, Any]:
    """The settings as the page may see them: an address field holding something that is not a clean address
    (a stored password in a URL, say) is left out rather than echoed. Saving validates, so this only guards
    what was stored some other way."""
    shown = dict(settings)
    for item in fields:
        key = item.get("key")
        if item.get("destination") is not True or key not in shown:
            continue
        for entry in (shown[key] if isinstance(shown[key], list) else [shown[key]]):
            try:
                normalize_destination(entry if isinstance(entry, str) else "\x00")
            except DestinationError:
                del shown[key]
                break
    return shown


def _price(value: Any) -> float:
    return value if is_finite_number(value) else 0


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
            "schema": self._display_schema(plugin), "values": _clean(_without_unsafe_addresses(schema.fields, settings)),
            "secrets": self._secret_view(owner, schema.fields, destinations),
            "connections": [self.connection_view(instance, row) for row in store.list_connections(instance.id)],
        }

    def connection_view(self, instance, connection) -> dict[str, Any]:
        plugin = self._serving_plugin(instance, connection)
        schema = self._backend_schema(plugin, connection.backend_type) or DestinationSchema()
        settings = self._store.settings_of(connection)
        destinations = effective_destinations(schema, settings)
        values = _clean(_without_unsafe_addresses(schema.fields, settings))
        values[MODEL_KEY] = connection.model
        values[PRICE_KEYS[0]] = _price(connection.price_input_per_million)
        values[PRICE_KEYS[1]] = _price(connection.price_output_per_million)
        return {
            "id": connection.id, "instance_id": connection.instance_id, "position": connection.position,
            "backend_type": connection.backend_type, "plugin_id": connection.plugin_id, "model": connection.model,
            "enabled": bool(connection.enabled), "revision": connection.revision, "awaiting_secrets": bool(connection.awaiting_secrets),
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
        """Change the engine, the fallback or both. Both are checked before either is saved."""
        proposed: dict[str, Any] = {}
        for key, name in (("engine", "engine"), ("fallback_engine", "fallback")):
            if key in payload:
                value = payload[key]
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise ServiceError(f"{key} must be an engine instance id or null")
                proposed[name] = value
        try:
            change = self._store.set_pointer_pair(**proposed)
        except TranslatorStoreError as error:
            raise ServiceError(str(error)) from error
        return [self._notice(item) for item in change.notices]

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
        values = payload.get("values")
        if values is not None and not isinstance(values, dict):
            raise ServiceError("values must be an object")
        clear = self._clear_list(payload)
        enabled = payload.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ServiceError("enabled must be true or false")
        expected = self._expected(payload, required=values is not None or bool(clear))
        if expected is not None and instance.revision != expected:
            raise StaleRevisionError("The engine settings changed since they were loaded.", revision=instance.revision)
        if values is not None or clear:
            plugin = self._plugin(instance.plugin_id)
            if plugin is None or not plugin.active:
                raise PluginUnavailableError("The plugin for this engine is not installed.")
            self._save_engine_values(instance, plugin, values, clear, payload, expected)
        if enabled is not None and bool(instance.enabled) != enabled:
            notices += [self._notice(item) for item in self._store.set_instance_enabled(instance_id, enabled).notices]
        return {"engine": self.instance_view(self._instance(instance_id)), "notices": notices}

    def _save_engine_values(self, instance, plugin, values, clear, payload, expected) -> None:
        schema = self._engine_schema(plugin)
        owner = (INSTANCE, instance.id)
        before = self._store.settings_of(instance)
        stored = frozenset(self._store.secret_summary(owner, frozenset())) - frozenset(clear)
        self._check_clear(clear, schema)
        validated = ValidatedValues()
        if values is not None:
            validated = self._validate(schema, self._merged(before, values, schema), stored, plugin.plugin_id,
                                       "engine", "")
        after = validated.settings if values is not None else before
        self._require_consent(schema, before, after, payload, new_owner=False)
        parsed = self._parsed(plugin)
        # The revision claim, the settings and every secret write or clear commit together. A save that only
        # clears a secret still claims a revision, so a concurrent save of the same engine is never overwritten
        # behind its back.
        with self._store.transaction():
            try:
                self._store.save_instance_settings(
                    instance.id, after, schema=schema, settings_version=parsed.settings_version if parsed else None,
                    plugin_version=plugin.version, expected_revision=expected)
            except StoreStale as error:
                raise StaleRevisionError("The engine settings changed since they were loaded.",
                                         revision=error.revision) from error
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
        host, plain = self._split_host(values, schema=schema)
        validated = self._validate(self._without_host(schema), plain, frozenset(), plugin.plugin_id, "connection",
                                   backend_type)
        self._require_consent(schema, {}, validated.settings, payload, new_owner=True)
        parsed = self._parsed(plugin)
        with self._store.transaction():
            row = self._store.add_connection(
                instance_id, backend_type, plugin_id=plugin_id, model=host[MODEL_KEY], settings=validated.settings,
                settings_version=parsed.settings_version if parsed else None, plugin_version=plugin.version,
                price_input_per_million=host[PRICE_KEYS[0]], price_output_per_million=host[PRICE_KEYS[1]],
                enabled=self._flag(payload, "enabled", True))
            self._write_secrets((CONNECTION, row.id), validated, [],
                                effective_destinations(schema, validated.settings))
        return {"connection": self.connection_view(instance, self._store.get_connection(row.id)), "notices": []}

    def update_connection(self, connection_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Change a connection. ``values`` are merged onto what is stored: a key left out keeps its value,
        and one sent empty goes back to its default."""
        connection = self._connection(connection_id)
        instance = self._instance(connection.instance_id)
        plugin = self._serving_plugin(instance, connection)
        schema = self._backend_schema(plugin, connection.backend_type) if plugin and plugin.active else None
        enabled = self._flag(payload, "enabled", None)
        values = payload.get("values")
        if values is not None and not isinstance(values, dict):
            raise ServiceError("values must be an object")
        clear = self._clear_list(payload)
        expected = self._expected(payload, required=True)
        if schema is None:
            raise PluginUnavailableError("The plugin that serves this back end is not installed.")
        if connection.revision != expected:
            raise StaleRevisionError("The back end changed since it was loaded.", revision=connection.revision)
        if values is None and not clear and enabled is None:
            return {"connection": self.connection_view(instance, connection), "notices": []}
        owner = (CONNECTION, connection_id)
        before = self._store.settings_of(connection)
        stored = frozenset(self._store.secret_summary(owner, frozenset())) - frozenset(clear)
        self._check_clear(clear, schema)
        validated = ValidatedValues()
        host = {MODEL_KEY: connection.model, PRICE_KEYS[0]: connection.price_input_per_million,
                PRICE_KEYS[1]: connection.price_output_per_million}
        if values is not None:
            host, plain = self._split_host(values, keep=connection, schema=schema)
            validated = self._validate(self._without_host(schema), self._merged(before, plain, schema), stored,
                                       plugin.plugin_id, "connection", connection.backend_type)
        after = validated.settings if values is not None else before
        self._require_consent(schema, before, after, payload, new_owner=False)
        parsed = self._parsed(plugin)
        with self._store.transaction():
            try:
                self._store.update_connection(
                    connection_id, schema=schema, model=host[MODEL_KEY], settings=after,
                    settings_version=parsed.settings_version if parsed else None, plugin_version=plugin.version,
                    price_input_per_million=host[PRICE_KEYS[0]], price_output_per_million=host[PRICE_KEYS[1]],
                    enabled=enabled, expected_revision=expected)
            except StoreStale as error:
                raise StaleRevisionError("The back end changed since it was loaded.",
                                         revision=error.revision) from error
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
        """The live choices of an ``options`` field, or ``available: false`` so the page offers free text.

        The request is checked against the manifest before a source sees it: the back-end type and the
        field must exist, and only declared, non-secret draft values pass. The contract for a source
        that makes a network call: a draft address must not be sent a stored key unless
        ``TranslatorStore.secrets_for_request`` covers that address, and no redirect is followed.
        """
        instance = self._instance(instance_id)
        plugin = self._plugin(instance.plugin_id)
        display = self._display_schema(plugin)
        unavailable = {"available": False, "items": [], "reason": "The model list could not be fetched."}
        if display is None:
            return unavailable
        if backend_type:
            definition = display["backend_types"].get(backend_type)
            if definition is None:
                raise ServiceError(f"{plugin.name} has no back end of type {backend_type}")
            fields = definition["fields"]
        else:
            fields = display["fields"]
        field = next((item for item in fields if item["key"] == field_key and item.get("type") == "options"), None)
        if field is None:
            raise ServiceError(f"{field_key} is not a model list of this engine")
        clean = {item["key"]: draft[item["key"]] for item in fields
                 if item["key"] in draft and not is_secret_field(item)}
        # A draft address is checked like a saved one before any source sees it, so a source is never handed a
        # malformed address or one that carries a password, and the answer never repeats either.
        refused = {}
        for item in fields:
            value = clean.get(item["key"])
            if item.get("destination") is True and value not in (None, "", []):
                for entry in (value if isinstance(value, list) else [value]):
                    try:
                        if not isinstance(entry, str):
                            raise DestinationError(DestinationError.MALFORMED)
                        normalize_destination(entry)
                    except DestinationError as error:
                        refused[item["key"]] = error.message
        if refused:
            raise InvalidValuesError("The values are not valid.", field_errors=refused)
        source = self._options_source
        answer = source(instance_id, backend_type, field_key, clean) if source is not None else None
        if not answer or not isinstance(answer.get("items"), list):
            return unavailable
        return {"available": True, "items": answer["items"], "reason": ""}

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _clear_list(payload: Mapping[str, Any]) -> list[str]:
        clear = payload.get("clear_secrets") or []
        if not isinstance(clear, list) or any(not isinstance(item, str) for item in clear):
            raise ServiceError("clear_secrets must be a list of field names")
        return clear

    @staticmethod
    def _expected(payload: Mapping[str, Any], *, required: bool) -> int | None:
        expected = payload.get("expected_revision")
        if expected is None:
            if required:
                raise RevisionRequiredError("expected_revision is required: send the revision you loaded.")
            return None
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise ServiceError("expected_revision must be a number")
        return expected

    @staticmethod
    def _flag(payload: Mapping[str, Any], key: str, default: bool | None) -> bool | None:
        value = payload.get(key, default)
        if value is not None and not isinstance(value, bool):
            raise ServiceError(f"{key} must be true or false")
        return value

    @staticmethod
    def _merged(stored: Mapping[str, Any], values: Mapping[str, Any], schema: DestinationSchema) -> dict[str, Any]:
        """The stored settings with ``values`` laid over them. An empty value removes its key (the default applies)."""
        toggles = {item["key"] for item in schema.fields if item.get("type") == "toggle"}
        merged = dict(stored)
        for key, value in values.items():
            if (value is None or value == "" or value == []) and key not in toggles:
                merged.pop(key, None)
            else:
                merged[key] = value
        # A secret field is never in the stored settings; only a value typed now reaches validation.
        for item in schema.fields:
            if is_secret_field(item) and item["key"] not in values:
                merged.pop(item["key"], None)
        return merged

    @staticmethod
    def _without_host(schema: DestinationSchema) -> DestinationSchema:
        """The schema for the settings JSON: the model has its own column, so a field of that name is checked apart."""
        return DestinationSchema(fields=tuple(item for item in schema.fields if item["key"] not in HOST_KEYS),
                                 endpoints=schema.endpoints)

    @staticmethod
    def _split_host(values: Mapping[str, Any], keep=None, schema: DestinationSchema | None = None
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Take the model and the prices out of a connection's values; the rest is the back end's settings."""
        host: dict[str, Any] = {
            MODEL_KEY: keep.model if keep is not None else "",
            PRICE_KEYS[0]: keep.price_input_per_million if keep is not None else 0,
            PRICE_KEYS[1]: keep.price_output_per_million if keep is not None else 0}
        plain = dict(values)
        errors: dict[str, str] = {}
        for key in HOST_KEYS:
            if key not in plain:
                continue
            value = plain.pop(key)
            if key == MODEL_KEY:
                if not isinstance(value, str):
                    errors[key] = "must be text"
                else:
                    host[key] = value
            elif value is None:
                host[key] = 0  # an emptied price goes back to its default, like any other emptied field
            elif not is_finite_number(value) or value < 0:
                errors[key] = "must be a number from 0 up"
            else:
                host[key] = value
        # A manifest that declares its own model field keeps its limits and its required flag, and its default
        # stands in for a model that was left out or emptied, before the model is checked and stored.
        declared = next((item for item in (schema.fields if schema else ()) if item["key"] == MODEL_KEY), None)
        if (declared is not None and not errors.get(MODEL_KEY) and host[MODEL_KEY] == ""
                and isinstance(declared.get("default"), str)):
            host[MODEL_KEY] = declared["default"]
        if declared is not None and MODEL_KEY not in errors:
            checked = validate_values([declared], {MODEL_KEY: host[MODEL_KEY]})
            errors.update(checked.errors)
        if errors:
            raise InvalidValuesError("The values are not valid.", field_errors=errors)
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
    def _check_clear(clear: list[str], schema: DestinationSchema) -> None:
        secret_fields = {item["key"] for item in schema.fields if is_secret_field(item)}
        unknown = [name for name in clear if name not in secret_fields]
        if unknown:
            raise InvalidValuesError("The values are not valid.",
                                     field_errors={name: "is not a secret of this engine" for name in unknown})

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
        missing = sorted(new - self._clean_destinations(accepted))
        if missing:
            raise ConsentRequiredError(
                "These addresses are new. Check that you trust them: the text to translate, and the key if you "
                "entered one, are sent there.", destinations=missing)

    @staticmethod
    def _clean_destinations(items: Any) -> set[str]:
        """The addresses in a list the client sent, in one spelling. One that is not an address accepts nothing."""
        clean = set()
        for item in items:
            try:
                text = normalize_destination(item) if isinstance(item, str) else ""
            except DestinationError:
                continue
            if text:
                clean.add(text)
        return clean

    def _write_secrets(self, owner, validated: ValidatedValues, clear: list[str], destinations) -> None:
        for name in clear:
            # Clear, then type: a secret sent in the same request replaces the stored one instead of being dropped.
            if name not in validated.secrets:
                self._store.clear_secret(owner, name, destinations=destinations)
        for name, value in validated.secrets.items():
            self._store.set_secret(owner, name, _json_secret(value), destinations=destinations)


__all__ = [
    "ConsentRequiredError", "InstalledPlugin", "InvalidValuesError", "NotFoundError", "PluginUnavailableError",
    "ServiceError", "StaleRevisionError", "TranslatorSettingsService", "field_visible", "installed_plugin"]

# coding=utf-8
"""The typed settings service behind the Translation page.

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
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping
from uuid import uuid4

from provider_hub.plugin_manifest import parse_plugin_manifest
from secret_store.scrub import mask_credential_refusal

from . import catalog_view, legacy_engines
from .destinations import DestinationError, DestinationSchema, effective_destinations, normalize_destination
from .field_schema import (ValidatedValues, connection_constraint_errors, field_visible, is_finite_number,
                           is_secret_field, validate_draft)
from .host_fields import (HOST_FIELDS, HOST_KEYS, MODEL_KEY, PRICE_KEYS, host_default, host_definitions, host_of,
                          validate_owner)
from .store import (CONNECTION, INSTANCE, OFFICIAL_ENGINE_PLUGIN_ID, StaleRevisionError as StoreStale,
                    TranslatorStore, TranslatorStoreError)

if TYPE_CHECKING:
    from .usage import CostLimits, UsageLedger

logger = logging.getLogger(__name__)

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
    # The catalog source the installation came from; None when the hub state does not say.
    source_id: str | None = None


def installed_plugin(plugin_id: str) -> InstalledPlugin | None:
    """The plugin's active installation from the Provider Hub state, or None."""
    from provider_hub import state as hub_state

    item = (hub_state.load_state().get("installations") or {}).get(plugin_id)
    if not isinstance(item, dict) or not isinstance(item.get("manifest"), dict):
        return None
    manifest = item["manifest"]
    active = bool(item.get("active_version")) and item.get("state") != "removed" and not item.get("blocked")
    source = item.get("source_id")
    return InstalledPlugin(plugin_id, str(item.get("name") or manifest.get("name") or plugin_id),
                           str(item.get("active_version") or manifest.get("version") or ""), active, manifest,
                           source if isinstance(source, str) else None)


# Hooks the translation worker binds once it exists. With no worker, a value that passes the schema is accepted as is and the
# model list is unavailable, which the page shows as a free-text model field.
PluginValidator = Callable[[str, str, str, Mapping[str, Any]], Mapping[str, str] | None]
# (engine instance id, back-end type or '' for the engine's own field, field key, draft values without secrets)
OptionsSource = Callable[[int, str, str, Mapping[str, Any]], Mapping[str, Any] | None]

# The most a connection test may send and receive, so its worst-case charge is small and known before it runs.
TEST_INPUT_TOKENS = 64
TEST_OUTPUT_TOKENS = 16
TEST_MESSAGE_LIMIT = 300
# A probe is told to give up after this long, and is not waited for much longer: a hung worker must not hold a
# request thread, and its worst-case charge stays counted.
TEST_TIMEOUT_SECONDS = 20.0
# A test's worker has to be waited for and started before the request is sent (a permit when two others run, then the
# worker's start), and stopped after it, so the wait for the probe is longer than the request's own time by more than a
# second or two.
TEST_TIMEOUT_GRACE_SECONDS = 20.0
# What a probe may fail with and still be a failed test. A bug in our own code or in a binding (a wrong type, a
# missing attribute or key) is not turned into "the back end did not answer": it is raised.
PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, LookupError, AssertionError, ImportError,
                      NotImplementedError, SyntaxError)


def _is_bug(error: BaseException) -> bool:
    """A mistake in our own code or in a binding, as opposed to a failure of the back end or the provider. A
    failed lookup (a missing key or index) in what a provider answered is the provider's."""
    return isinstance(error, PROGRAMMING_ERRORS) and not isinstance(error, LookupError)


UNFINISHED_TEST = "The test could not be completed. The Bazarr+ log has the cause when debug logging is on."
# One test at a time per connection: a second request while one runs is told so instead of starting another.
_RUNNING_TESTS: set[int] = set()
_RUNNING_LOCK = threading.Lock()


class _Claim:
    """The right to run one test of a connection. Released once, by whoever finishes with it last: the request,
    or the probe's own thread when the request has stopped waiting for a probe that is still running."""

    def __init__(self, connection_id: int):
        self.connection_id = connection_id
        self.handed_to_probe = False
        self._released = False

    def release(self) -> None:
        with _RUNNING_LOCK:
            if not self._released:
                self._released = True
                _RUNNING_TESTS.discard(self.connection_id)


def _claim_test(connection_id: int) -> "_Claim | None":
    with _RUNNING_LOCK:
        if connection_id in _RUNNING_TESTS:
            return None
        _RUNNING_TESTS.add(connection_id)
        return _Claim(connection_id)


OPEN_TEST = "The translation engine is not running, so this back end cannot be tested yet."

# What a connection test came to, beside ``ok``: whether a request went out at all, and when none did, whether
# waiting is enough. A page shows a back end that failed differently from a test that never ran.
ANSWERED = "answered"    # the back end answered: ``ok`` is true
FAILED = "failed"        # a request went out, or may have, and the back end did not answer as it should
NOT_READY = "not_ready"  # nothing was sent: the test could not be started just now, and may be in a moment
REFUSED = "refused"      # nothing was sent: what is saved, or a cost cap, does not allow the test


@dataclass(frozen=True)
class ProbeRequest:
    """One small request to a saved connection, as the engine's worker is asked to make it.

    It is one snapshot, taken by the caller in one pass: the settings the destinations were worked out from and
    the secrets cut to them. A probe sends exactly this and never reads the connection again, so an address
    saved in the meantime cannot pair a new address with an old key.

    ``secrets`` holds what the engine's and the connection's stored secrets may carry to their own
    destinations, as for a real translation (the connection's win a name they share): a secret bound to other
    addresses is left out. The worker must send them nowhere else, follow no redirect, and put none of them in
    what it answers.
    """

    instance_id: int
    connection_id: int
    backend_type: str
    model: str
    values: Mapping[str, Any]
    destinations: frozenset[str]
    input_token_limit: int
    output_token_limit: int
    timeout_seconds: float = TEST_TIMEOUT_SECONDS
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)
    # The plugin whose worker answers: the connection's own when an extension serves it, else its engine's.
    plugin_id: str = ""
    # The engine's own saved settings, without secrets, and the connection's prices, as they were when read.
    engine_values: Mapping[str, Any] = field(default_factory=dict)
    price_input_per_million: float = 0
    price_output_per_million: float = 0


# A probe runs on a thread of its own, not the request's. It must not use the request's database session, must be
# safe to call from any thread, and must give up within ``ProbeRequest.timeout_seconds``: the caller stops waiting
# a few seconds after that and the test counts as failed.
#
# A probe answers ``{"ok": bool, "message": str, "input_tokens": int, "output_tokens": int, "sent": bool}``.
# The tokens are what the request used, settled against the cost caps. Without them the test keeps its
# worst-case charge, because the provider may have billed a request whose answer never came; only ``sent:
# false`` (the request never left) settles it at nothing.
ConnectionProbe = Callable[[ProbeRequest], Mapping[str, Any]]


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


def _shown_settings(fields, settings: Mapping[str, Any]) -> dict[str, Any]:
    """The stored settings as a response may carry them. A field the manifest declares secret *now* is never in
    them, whatever the JSON holds: a plugin update can turn a text field into a secret after a value was stored
    in the clear. An address field holding something unclean is left out too."""
    declared = {item.get("key") for item in fields if not is_secret_field(item)}
    return _without_unsafe_addresses(fields, {key: value for key, value in settings.items() if key in declared})


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


def _entries_of(secret: str) -> list[str]:
    """The entries of a list secret (a JSON array of strings), so each one is masked on its own."""
    if not secret.startswith("["):
        return []
    try:
        parsed = json.loads(secret)
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _rendered_forms(value: str) -> set[str]:
    """A secret as it is, and as formatting renders it: repr without its quotes (either quote style), repr of
    that again (a dict inside a message), JSON with and without ensure_ascii, and the unicode-escape form,
    each with the quote characters escaped as well."""
    escaped = value.encode("unicode_escape").decode("ascii")
    once = repr(value)[1:-1]
    base = {value, once, escaped, repr(once)[1:-1], repr(repr(value))[1:-1],
            json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    forms = (base | {form.replace("'", "\\'") for form in base} | {form.replace('"', '\\"') for form in base})
    return {form for form in forms if form}


def _configured_limits() -> "CostLimits":
    from .limits import current_cost_limits

    return current_cost_limits()


def _cap_message(reason: str | None, caps: "CostLimits", remainder: int | None) -> str:
    """Why a priced test was not sent, naming the cap that binds.

    With both caps set, the one that leaves less room is named: ``remainder`` is what is left of the month.
    """
    if reason == "wait":
        return "Other translations are holding this month's budget. Try the test again in a moment."
    month = ("This back end is priced and what is left of this month's cost limit cannot cover a test, so no "
             "test was sent. Raise the monthly limit, or wait for next month.")
    job = ("This back end is priced and the cost limit for one translation is too low to cover a test, so no "
           "test was sent. Raise that limit, or test a free back end.")
    if caps.month_cap is None:
        return job
    if caps.job_cap is None or reason == "month_spent":
        return month
    if reason == "job_cap_spent":
        return job
    return month if remainder is not None and remainder < caps.job_cap else job


class TranslatorSettingsService:
    def __init__(self, store: TranslatorStore, *, plugin_lookup: Callable[[str], InstalledPlugin | None] | None = None,
                 plugin_validator: PluginValidator | None = None, options_source: OptionsSource | None = None,
                 connection_probe: ConnectionProbe | None = None,
                 catalog_engines: Callable[[], list[dict[str, Any]]] | None = None,
                 update_lookup: Callable[[str], str | None] | None = None,
                 cost_limits: Callable[[], "CostLimits"] | None = None, legacy: bool = False,
                 lingarr: legacy_engines.ConfigLingarr | None = None,
                 test_ready: Callable[[], bool] | None = None):
        self._store = store
        self._plugin = plugin_lookup or installed_plugin
        self._validator = plugin_validator
        self._options_source = options_source
        self._probe = connection_probe
        # Whether something can answer a test right now (a worker is bound). None means always.
        self._test_ready = test_ready
        self._catalog_engines = catalog_engines or catalog_view.available_engines
        self._update_lookup = update_lookup or catalog_view.update_version
        self._cost_limits = cost_limits
        # Whether the engines served by code in Bazarr+ itself (Lingarr) are listed. Their settings stay in the
        # settings they always used, so nothing is migrated.
        self._legacy = legacy
        self._lingarr = lingarr or legacy_engines.ConfigLingarr()

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

    @staticmethod
    def _retained_secrets(plugin_id: str, backend_type: str | None = None) -> frozenset[str]:
        from provider_hub.service import retained_settings_manifest
        from .destinations import schema_for_backend, schema_for_engine

        manifest = retained_settings_manifest(plugin_id)
        if manifest is None:
            return frozenset()
        schema = schema_for_engine(manifest) if backend_type is None else schema_for_backend(manifest, backend_type)
        return schema.secret_fields

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
        if self._legacy:
            for legacy in legacy_engines.LEGACY_ENGINES.values():
                self._store.ensure_instance(legacy.plugin_id, name=legacy.name, settings_version=1)
        engine, fallback = self.pointers()
        return {"enabled": True, "engine": engine, "fallback_engine": fallback,
                "instances": [self.instance_view(row) for row in self._store.list_instances()],
                "capabilities": self.capabilities(), "available_engines": self._catalog_engines()}

    def can_test(self) -> bool:
        """Whether a connection test has anything to ask: a probe, or the model list the engine can fetch."""
        if self._probe is None and self._options_source is None:
            return False
        return self._test_ready() if self._test_ready is not None else True

    def capabilities(self) -> list[str]:
        """The routes of the page that work now. The page shows a control only for one listed here."""
        listed = ["summary", "limits"]
        if self.can_test():
            listed.append("test")
        return listed + ["queue"]

    def instance_view(self, instance) -> dict[str, Any]:
        legacy = legacy_engines.legacy_engine_for(instance.plugin_id)
        if legacy is not None:
            return self._legacy_view(instance, legacy)
        store = self._store
        plugin = self._plugin(instance.plugin_id)
        schema = self._engine_schema(plugin)
        owner = (INSTANCE, instance.id)
        settings = store.settings_of(instance)
        destinations = effective_destinations(schema, settings)
        description = self._parsed(plugin).raw.get("description") if plugin is not None and self._parsed(plugin) else None
        view = {
            "id": instance.id, "plugin_id": instance.plugin_id, "name": instance.name or (plugin.name if plugin else ""),
            "official": self._is_official(instance, plugin), "enabled": bool(instance.enabled),
            "revision": instance.revision, "awaiting_secrets": bool(instance.awaiting_secrets),
            "configured": store.instance_configured(instance), "settings_version": instance.settings_version,
            "plugin_version": instance.plugin_version,
            "plugin": None if plugin is None else {"name": plugin.name, "version": plugin.version,
                                                    "active": plugin.active,
                                                    "update_version": self._update_lookup(plugin.plugin_id)},
            "schema": self._display_schema(plugin), "values": _clean(_shown_settings(schema.fields, settings)),
            "secrets": self._secret_view(owner, schema.fields, destinations),
            "connections": [self.connection_view(instance, row) for row in store.list_connections(instance.id)],
        }
        if isinstance(description, str) and description.strip():
            view["description"] = description.strip()[:300]
        return view

    @staticmethod
    def _is_official(instance, plugin: InstalledPlugin | None) -> bool:
        """The official engine is the official plugin id installed from the official catalog source.

        The same rule the catalog side applies to an engine on offer: a plugin that borrows the id from
        another source is not the official engine.
        """
        from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID

        return (instance.plugin_id == OFFICIAL_ENGINE_PLUGIN_ID and plugin is not None
                and plugin.source_id == OFFICIAL_CATALOG_SOURCE_ID)

    def _legacy_view(self, instance, legacy) -> dict[str, Any]:
        """An engine Bazarr+ runs itself: its address, and whether a token is set, never the token."""
        fields = legacy_engines.LINGARR_FIELDS
        token = self._lingarr.token()
        secrets: dict[str, Any] = {}
        if token:
            secrets[legacy_engines.TOKEN_KEY] = {"configured": True}
            if len(token) >= legacy_engines.LAST_FOUR_MIN_LENGTH:
                secrets[legacy_engines.TOKEN_KEY]["last4"] = token[-4:]
        return {
            "id": instance.id, "plugin_id": instance.plugin_id, "name": instance.name or legacy.name,
            "official": False, "legacy": True, "description": legacy_engines.DESCRIPTION,
            "enabled": bool(instance.enabled), "revision": instance.revision, "awaiting_secrets": False,
            "configured": self._store.instance_configured(instance), "settings_version": 1, "plugin_version": None,
            "plugin": None,
            "schema": {"settings_version": 1, "endpoints": [], "backend_types": {},
                       "fields": [dict(item) for item in fields]},
            "values": _without_unsafe_addresses(fields, {legacy_engines.URL_KEY: self._lingarr.url()}),
            "secrets": secrets,
            "connections": [],
        }

    def connection_view(self, instance, connection) -> dict[str, Any]:
        plugin = self._serving_plugin(instance, connection)
        schema = self._backend_schema(plugin, connection.backend_type) or DestinationSchema()
        settings = self._store.settings_of(connection)
        destinations = effective_destinations(schema, settings)
        values = _clean(_shown_settings(schema.fields, settings))
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
        secret_fields = DestinationSchema(fields=tuple(fields)).secret_fields
        summary = self._store.secret_summary(owner, destinations, fields=secret_fields)
        lists = {item["key"] for item in fields if item.get("type") in ("key_list", "list")}
        # An empty destination set is covered by every binding. This reads only the active fields, then
        # counts the entries without including their values in the view.
        held = (self._store.secrets_for_request(owner, frozenset(), fields=secret_fields)
                if lists & set(summary) else {})
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
        row = self._store.get_instance(instance_id, fresh=True)
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
        expected = self._expected(payload, required=values is not None or bool(clear) or enabled is not None)
        if expected is not None and instance.revision != expected:
            raise StaleRevisionError("The engine settings changed since they were loaded.", revision=instance.revision)
        # Everything that may call the plugin (its validate hook) or read the manifest is done first, before any
        # lock is taken: a slow plugin must not hold the config lock. The revision check inside the transaction
        # still refuses a save whose state moved in the meantime.
        legacy = legacy_engines.legacy_engine_for(instance.plugin_id) is not None
        commit: Callable[[], None] | None = None
        if values is not None or clear:
            if not legacy:
                plugin = self._plugin(instance.plugin_id)
                if plugin is None or not plugin.active:
                    raise PluginUnavailableError("The plugin for this engine is not installed.")
                commit = self._prepare_engine_values(instance, plugin, values, clear, payload, expected)
        elif enabled is True:
            self._check_engine_before_turning_on(instance)
        # The config lock is taken before the write transaction, the one order every writer of both keeps, and it
        # is held to the end, the undo included, so nothing else writes config.yaml between the save and its undo.
        #
        # The settings and the switch are one save: if the switch loses a race, the settings are not kept either.
        # An engine that keeps its settings in config.yaml has already written them when the database part fails,
        # so it hands back an undo that puts them as they were.
        with self._lingarr.lock():
            undo: Callable[[], None] | None = None
            try:
                with self._store.transaction():
                    if legacy and (values is not None or clear):
                        undo = self._save_legacy_values(instance, values or {}, clear, payload, expected)
                    elif commit is not None:
                        commit()
                    if enabled is not None and bool(instance.enabled) != enabled:
                        # The switch is swapped against the revision the client sent. Only when this request's own
                        # save has just moved it on does the switch use the revision that save produced.
                        produced = expected + 1 if (values is not None or clear) else expected
                        try:
                            change = self._store.set_instance_enabled(instance_id, enabled,
                                                                      expected_revision=produced)
                        except StoreStale as error:
                            raise StaleRevisionError("The engine settings changed since they were loaded.",
                                                     revision=error.revision) from error
                        notices += [self._notice(item) for item in change.notices]
            except BaseException:
                if undo is not None:
                    undo()
                raise
        return {"engine": self.instance_view(self._instance(instance_id)), "notices": notices}

    def _check_engine_before_turning_on(self, instance) -> None:
        """An engine is turned on only while what it holds still satisfies its plugin's manifest."""
        plugin = self._plugin(instance.plugin_id)
        if plugin is None or not plugin.active:
            return
        schema = self._engine_schema(plugin)
        owner = (INSTANCE, instance.id)
        stored = frozenset(self._store.secret_summary(owner, frozenset(), fields=schema.secret_fields))
        self._validate(schema, self._merged(self._store.settings_of(instance), {}, schema), stored, plugin.plugin_id,
                       "engine", "", contents=self._store.secret_contents(owner, schema.fields))

    def _prepare_engine_values(self, instance, plugin, values, clear, payload, expected) -> Callable[[], None]:
        """Check an engine save against its manifest and its plugin, and return the step that commits it."""
        schema = self._engine_schema(plugin)
        owner = (INSTANCE, instance.id)
        before = self._store.settings_of(instance)
        stored = frozenset(self._store.secret_summary(owner, frozenset(), fields=schema.secret_fields)) - frozenset(clear)
        self._check_clear(clear, schema)
        # Every save is checked, one that only clears a secret included: the stored settings, with the values
        # sent laid over them and the secrets that remain after the clear, must still satisfy the manifest.
        # Only a non-empty replacement or an explicit clear takes a stored secret out of the content check; an
        # empty input keeps the stored one, which is then checked like any other that stays.
        replaced = {key for key, value in (values or {}).items() if value not in (None, "", [])} | set(clear)
        validated = self._validate(schema, self._merged(before, values or {}, schema), stored, plugin.plugin_id,
                                   "engine", "", contents=self._store.secret_contents(owner, schema.fields, replaced))
        after = validated.settings
        self._require_consent(schema, before, after, payload, new_owner=False)
        parsed = self._parsed(plugin)
        self._make_key_durable(validated)  # before the lock and the transaction, never inside them
        retained_secrets = self._retained_secrets(plugin.plugin_id)

        def commit() -> None:
            # The revision claim, the settings and every secret write or clear commit together. A save that only
            # clears a secret still claims a revision, so a concurrent save of the same engine is never
            # overwritten behind its back.
            with self._store.transaction():
                try:
                    self._store.save_instance_settings(
                        instance.id, after, schema=schema,
                        settings_version=parsed.settings_version if parsed else None,
                        plugin_version=plugin.version, expected_revision=expected, retained_secrets=retained_secrets)
                except StoreStale as error:
                    raise StaleRevisionError("The engine settings changed since they were loaded.",
                                             revision=error.revision) from error
                self._write_secrets(owner, validated, clear, effective_destinations(schema, after), schema.secret_fields)

        return commit

    def _save_legacy_values(self, instance, values: Mapping[str, Any], clear: list[str], payload,
                            expected) -> Callable[[], None]:
        """Save the address and token of an engine served by code in Bazarr+, in the settings it always used.

        The rules are those of any engine: the address is a destination, so a new one needs the person's
        consent in the same request, a changed address never keeps the stored token, a token goes in and never
        comes out, and the save claims the revision the caller loaded.
        """
        # From the read of the old address to the write of the new one no other writer of config.yaml gets in.
        with self._lingarr.lock():
            errors: dict[str, str] = {}
            for key in values:
                if key not in (legacy_engines.URL_KEY, legacy_engines.TOKEN_KEY):
                    errors[str(key)] = "is not a setting of this engine"
            for name in clear:
                if name != legacy_engines.TOKEN_KEY:
                    errors[name] = "is not a secret of this engine"
            before = self._lingarr.url()
            before_token = self._lingarr.token()
            url = before
            if legacy_engines.URL_KEY in values:
                raw = values[legacy_engines.URL_KEY]
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    errors[legacy_engines.URL_KEY] = "is required"
                elif not isinstance(raw, str):
                    errors[legacy_engines.URL_KEY] = "must be text"
                elif len(raw) > legacy_engines.URL_LIMIT:
                    errors[legacy_engines.URL_KEY] = f"must be at most {legacy_engines.URL_LIMIT} characters"
                else:
                    try:
                        url = normalize_destination(raw)
                    except DestinationError as error:
                        errors[legacy_engines.URL_KEY] = error.message
            token: str | None = None  # None keeps the stored token
            if legacy_engines.TOKEN_KEY in values:
                given = values[legacy_engines.TOKEN_KEY]
                if given is not None and not isinstance(given, str):
                    errors[legacy_engines.TOKEN_KEY] = "must be text"
                elif isinstance(given, str) and given.strip():
                    refusal = mask_credential_refusal(legacy_engines.TOKEN_KEY, given, before_token or None)
                    if refusal:
                        errors[legacy_engines.TOKEN_KEY] = refusal
                    else:
                        token = given.strip()
            if legacy_engines.TOKEN_KEY in clear and token is None:
                token = ""
            if errors:
                raise InvalidValuesError("The values are not valid.", field_errors=errors)
            schema = DestinationSchema(fields=legacy_engines.LINGARR_FIELDS)
            self._require_consent(schema, {legacy_engines.URL_KEY: before}, {legacy_engines.URL_KEY: url}, payload,
                                  new_owner=False)
            if (effective_destinations(schema, {legacy_engines.URL_KEY: before})
                    != effective_destinations(schema, {legacy_engines.URL_KEY: url}) and token is None):
                token = ""  # a changed address never reuses a stored token
            kept = dict(self._store.settings_of(instance))
            if legacy_engines.URL_KEY in values:
                kept[legacy_engines.SAVED_FLAG] = True  # the person has set the address, the default included
            try:
                self._store.save_instance_settings(instance.id, kept, schema=DestinationSchema(), settings_version=1,
                                                   expected_revision=expected)
            except StoreStale as error:
                raise StaleRevisionError("The engine settings changed since they were loaded.",
                                         revision=error.revision) from error
            if not self._lingarr.set(url, token):
                raise ServiceError("The Lingarr settings could not be saved.")

            wrote = (url, before_token if token is None else token)

            def undo() -> None:
                # Compare and restore: only the values this save wrote are put back. Anything that changed
                # them since belongs to a newer save and stays.
                with self._lingarr.lock():
                    if (self._lingarr.url(), self._lingarr.token()) != wrote:
                        return
                    if not self._lingarr.set(before, before_token, restoring=True):
                        logger.error("The Lingarr settings could not be put back after a failed save")

            return undo

    # -------------------------------------------------------- connections
    def create_connection(self, instance_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        instance = self._instance(instance_id)
        if legacy_engines.legacy_engine_for(instance.plugin_id) is not None:
            raise ServiceError("This engine has no back ends to add.")
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
        validated = self._validate(schema, plain, frozenset(), plugin.plugin_id, "connection", backend_type,
                                   host=host)
        self._require_consent(schema, {}, validated.settings, payload, new_owner=True)
        parsed = self._parsed(plugin)
        self._make_key_durable(validated)
        with self._store.transaction():
            row = self._store.add_connection(
                instance_id, backend_type, plugin_id=plugin_id, model=host[MODEL_KEY], settings=validated.settings,
                settings_version=parsed.settings_version if parsed else None, plugin_version=plugin.version,
                price_input_per_million=host[PRICE_KEYS[0]], price_output_per_million=host[PRICE_KEYS[1]],
                enabled=self._flag(payload, "enabled", True))
            self._write_secrets((CONNECTION, row.id), validated, [],
                                effective_destinations(schema, validated.settings), schema.secret_fields)
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
        stored = frozenset(self._store.secret_summary(owner, frozenset(), fields=schema.secret_fields)) - frozenset(clear)
        self._check_clear(clear, schema)
        # Every save that changes what the connection holds, or turns it on, is checked, a clear-only one
        # included: the stored settings, the values sent and the secrets that remain must satisfy the manifest.
        # A save that only turns it off needs nothing checked, so a broken connection can always be switched off.
        validated = ValidatedValues()
        checked = values is not None or bool(clear) or enabled is True
        host = host_of(connection)
        if checked:
            host, plain = self._split_host(values or {}, keep=connection, schema=schema)
            validated = self._validate(
                schema, self._merged(before, plain, schema), stored, plugin.plugin_id, "connection",
                connection.backend_type, host=host,
                contents=self._store.secret_contents(
                    owner, schema.fields,
                    {key for key, value in plain.items() if value not in (None, "", [])} | set(clear)))
        after = validated.settings if checked else before
        self._require_consent(schema, before, after, payload, new_owner=False)
        parsed = self._parsed(plugin)
        self._make_key_durable(validated)
        retained_secrets = self._retained_secrets(plugin.plugin_id, connection.backend_type)
        with self._store.transaction():
            try:
                self._store.update_connection(
                    connection_id, schema=schema, model=host[MODEL_KEY], settings=after,
                    settings_version=parsed.settings_version if parsed else None, plugin_version=plugin.version,
                    price_input_per_million=host[PRICE_KEYS[0]], price_output_per_million=host[PRICE_KEYS[1]],
                    enabled=enabled, expected_revision=expected, retained_secrets=retained_secrets)
            except StoreStale as error:
                raise StaleRevisionError("The back end changed since it was loaded.",
                                         revision=error.revision) from error
            self._write_secrets(owner, validated, clear, effective_destinations(schema, after), schema.secret_fields)
        return {"connection": self.connection_view(instance, self._connection(connection_id)), "notices": []}

    def delete_connection(self, connection_id: int, expected_revision: Any = None) -> None:
        """Remove a connection. Like a save, it carries the revision the caller loaded, and an older one is refused."""
        connection = self._connection(connection_id)
        expected = self._expected({"expected_revision": self._revision_arg(expected_revision)}, required=True)
        if connection.revision != expected:
            raise StaleRevisionError("The back end changed since it was loaded.", revision=connection.revision)
        try:
            self._store.delete_connection(connection_id, expected_revision=expected)
        except StoreStale as error:
            raise StaleRevisionError("The back end changed since it was loaded.",
                                     revision=error.revision) from error

    @staticmethod
    def _revision_arg(value: Any) -> Any:
        """A revision that came in as text, in a query string. Anything that is not a whole number stays as it is."""
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        return value

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
        row = self._store.get_connection(connection_id, fresh=True)
        if row is None:
            raise NotFoundError(f"no connection {connection_id}")
        return row

    # ------------------------------------------------------------ options
    def options(self, instance_id: int, backend_type: str, field_key: str,
                draft: Mapping[str, Any], *, plugin: InstalledPlugin | None = None) -> dict[str, Any]:
        """The live choices of an ``options`` field, or ``available: false`` so the page offers free text.

        The request is checked against the manifest before a source sees it: the back-end type and the
        field must exist, and only declared, non-secret draft values pass. The contract for a source
        that makes a network call: a draft address must not be sent a stored key unless
        ``TranslatorStore.secrets_for_request`` covers that address, and no redirect is followed.
        """
        instance = self._instance(instance_id)
        plugin = plugin or self._plugin(instance.plugin_id)  # a connection served by an extension names its own
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
        if not isinstance(draft, Mapping):
            raise ServiceError("values must be an object")
        clean = {item["key"]: draft[item["key"]] for item in fields
                 if item["key"] in draft and not is_secret_field(item)}
        # A draft is checked like a save is, minus what is merely missing: every value it carries must be of the
        # field's type and within its limits, and an address must be a clean one. A source is never handed
        # anything else, and the refusal never repeats a value.
        errors = validate_draft(fields, clean)
        if errors:
            raise InvalidValuesError("The values are not valid.", field_errors=errors)
        source = self._options_source
        answer = source(instance_id, backend_type, field_key, clean) if source is not None else None
        if not answer or not isinstance(answer.get("items"), list):
            return unavailable
        return {"available": True, "items": answer["items"], "reason": ""}

    # --------------------------------------------------------------- test
    def test_connection(self, connection_id: int, payload: Any = None) -> dict[str, Any]:
        """Send one small request to a saved connection and say whether it answered.

        The connection is tested as it is saved, never as a draft, and under the rules a save and a
        translation keep: its stored secrets reach only the addresses they were entered under, an
        address that is not a clean web address is refused before anything is sent, and a connection that
        waits for its secrets is not tested with stale ones. A refusal of this kind and a back end that
        does not answer are both ``ok: false`` with a sentence that says what to do; the answer never carries a
        key. A priced test is reserved against the cost caps and settled in the usage ledger like any call.

        ``outcome`` says what ``ok: false`` alone does not: ``failed`` when a request went out (or may have) and
        the back end did not answer as it should, ``not_ready`` when nothing was sent because the test could
        not be started just now (too many other tests running or the worker not started, a test of this back end
        still running, the test stopping before its request was handed over), and ``refused`` when nothing
        was sent because what is saved, or a cost cap, does not allow it. A test that worked is ``answered``.
        """
        if payload not in (None, {}):
            raise InvalidValuesError("A test uses the settings as they are saved. Save your changes first.")
        connection = self._connection(connection_id)
        instance = self._instance(connection.instance_id)
        plugin = self._serving_plugin(instance, connection)
        schema = self._backend_schema(plugin, connection.backend_type) if plugin is not None and plugin.active else None
        if schema is None:
            raise PluginUnavailableError("The plugin that serves this back end is not installed or not running.")
        if not self.can_test():
            raise PluginUnavailableError(OPEN_TEST)
        owner = (CONNECTION, connection.id)
        saved = self._store.settings_of(connection)
        # The addresses are read from what is stored, not from what a response may show: an address that is not
        # clean must stop the test, not be swapped for the manifest's default.
        destinations = effective_destinations(schema, saved)
        values = _shown_settings(schema.fields, saved)
        if any(item.startswith("invalid:") for item in destinations):
            return self._test_result(False, "The saved address is not a valid web address. Open this back end, "
                                            "correct the address and save it.", outcome=REFUSED)
        summary = self._store.secret_summary(owner, destinations, fields=schema.secret_fields)
        if connection.awaiting_secrets or any(entry.get("stale") for entry in summary.values()):
            return self._test_result(False, "The address changed after the key was saved, so the saved key is not "
                                            "sent to the new one. Enter the key again and save.", outcome=REFUSED)
        secrets = self._store.secrets_for_request(owner, destinations, fields=schema.secret_fields)
        engine_values = dict(self._store.settings_of(instance))
        engine_refusal, engine_secrets = self._engine_secrets(instance, engine_values)
        if engine_refusal:
            return self._test_result(False, engine_refusal, outcome=REFUSED)
        secrets = {**engine_secrets, **secrets}  # the connection's win a name they share
        # Every saved secret of the connection and of its engine, for masking only.
        held = {**{f"engine.{name}": value for name, value in
                   self._store.secrets_for_request((INSTANCE, instance.id), frozenset()).items()},
                **self._store.secrets_for_request(owner, frozenset())}
        where = self._typed_origin(schema, values)
        claim = _claim_test(connection.id)
        if claim is None:
            return self._test_result(False, "A test of this back end is already running. Wait for it to finish.",
                                     outcome=NOT_READY)
        bug: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            if self._probe is not None:
                result = self._paid_test(connection, instance, values, destinations, secrets, held, where, claim,
                                         engine_values)
            else:
                result = self._listing_test(connection, instance, values, where)
        except Exception as error:
            # Whatever a probe or a model list raised, a refusal of ours included, may carry an address or a key
            # in its text or its fields: only its type is said, and the scrubbed traceback goes to the debug log.
            # A bug in our own code is raised, as a copy with the secrets masked. The copy is built here and
            # raised after this block, so it carries no context and no traceback of the original.
            if _is_bug(error):
                if getattr(error, "_test_sanitized", False):
                    bug = error
                else:
                    self._log_failure(error, held)
                    bug = self._sanitized(error, held)
            else:
                self._log_failure(error, held)
                # A priced test that stopped before its probe was started (its budget could not be reserved)
                # sent nothing, so that is not a failed back end. Once the probe has the request, or when the
                # model list was asked instead, the request may have gone out.
                unsent = self._probe is not None and not claim.handed_to_probe
                result = self._test_result(False, UNFINISHED_TEST, outcome=NOT_READY if unsent else FAILED)
        finally:
            if not claim.handed_to_probe:
                claim.release()
        if bug is not None:
            raise bug
        return result

    @classmethod
    def _sanitized(cls, error: BaseException, held: Mapping[str, str]) -> BaseException:
        """A copy of the error of the same family whose text has every saved secret masked, with no traceback or
        cause attached: what reaches the framework's own logging cannot carry a key."""
        text = cls._mask(str(error), held)
        clean: BaseException | None = None
        try:
            clean = type(error)(text)
        except Exception:
            for base in PROGRAMMING_ERRORS:
                if isinstance(error, base):
                    clean = base(text)
                    break
        clean = clean if clean is not None else RuntimeError(text)
        clean._test_sanitized = True  # the outer handler does not clean it twice
        return clean

    @classmethod
    def _log_failure(cls, error: BaseException, held: Mapping[str, str]) -> None:
        logger.warning("A connection test failed with %s", type(error).__name__)
        if logger.isEnabledFor(logging.DEBUG):
            text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            logger.debug("The connection test failed:\n%s", cls._mask(text, held))

    def _listing_test(self, connection, instance, values, where) -> dict[str, Any]:
        """Ask for the model list, which the engine fetches with no charge. It answers when the back end does."""
        plugin = self._serving_plugin(instance, connection)
        definition = (self._display_schema(plugin) or {}).get("backend_types", {})
        fields = (definition.get(connection.backend_type) or {}).get("fields") or []
        listing = next((item["key"] for item in fields if item.get("type") == "options"), None)
        if listing is None:
            return self._test_result(False, "This back end has no check that can run without translating.",
                                     outcome=REFUSED)
        draft = {**values, **({MODEL_KEY: connection.model} if connection.model else {})}
        started = time.monotonic()
        try:
            answer = self.options(instance.id, connection.backend_type, listing, draft, plugin=plugin)
        except InvalidValuesError:
            return self._test_result(False, "The saved settings no longer fit this version of the plugin. Open "
                                            "this back end and save it again.", outcome=REFUSED)
        if answer.get("available") is True:
            return self._test_result(True, "", started)
        return self._test_result(False, self._unanswered(where))

    def _engine_secrets(self, instance, engine_values: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
        """The engine's stored secrets a test may send, cut to the engine's own destinations, or a refusal.

        The same rule a translation keeps: a key entered for other addresses is never sent to these.
        """
        owner = (INSTANCE, instance.id)
        schema = self._engine_schema(self._plugin(instance.plugin_id))
        if not self._store.secrets_for_request(owner, frozenset(), fields=schema.secret_fields):
            return "", {}
        destinations = effective_destinations(schema, engine_values)
        if (instance.awaiting_secrets or self._store.has_stale_secrets(owner, destinations, fields=schema.secret_fields)
                or any(item.startswith("invalid:") for item in destinations)):
            return ("The address of the engine changed after its key was saved, so the saved key is not sent to "
                    "the new one. Enter the key again and save."), {}
        return "", dict(self._store.secrets_for_request(owner, destinations, fields=schema.secret_fields))

    def _paid_test(self, connection, instance, values, destinations, secrets, held, where,
                   claim: _Claim, engine_values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # Imported here: the ledger reads the database tables at import, and a caller that only lists
        # engines or saves settings must not need them.
        from .usage import RESERVED, CostLimits, UsageLedger, worst_case_micros

        worst = worst_case_micros(TEST_INPUT_TOKENS, TEST_OUTPUT_TOKENS, _price(connection.price_input_per_million),
                                  _price(connection.price_output_per_million))
        ledger = UsageLedger(self._store.session)
        caps = self._cost_limits() if self._cost_limits is not None else _configured_limits()
        # The test holds what it may cost, not a whole job's share of the month.
        limits = CostLimits(caps.month_cap, worst if caps.job_cap is None else min(caps.job_cap, worst),
                            caps.translation_limit)
        job_id = f"connection-test-{connection.id}-{uuid4().hex[:12]}"
        call_id = "test"
        if worst > 0:
            # A priced test is a call like any other: it fits the month and job caps or it does not run.
            def refused(reason):
                remainder = (ledger.month_remainder(caps)
                             if caps.month_cap is not None and caps.job_cap is not None else None)
                return self._test_result(False, _cap_message(reason, caps, remainder), outcome=REFUSED)

            try:
                budget = ledger.reserve_job_budget(job_id, limits)
                if budget.status != RESERVED:
                    return refused(budget.status)
                reservation = ledger.reserve_call(job_id, call_id, worst_case_micros=worst, limits=limits,
                                                  connection_id=connection.id, backend_type=connection.backend_type,
                                                  model=connection.model)
                if not reservation.admitted:
                    ledger.release_job(job_id)
                    return refused(reservation.reason)
            except BaseException:
                # Whatever failed part way, the budget the first step committed is given back.
                try:
                    ledger.release_job(job_id)
                except Exception:
                    logger.warning("A connection test could not give back its budget")
                raise
        request = ProbeRequest(
            instance_id=instance.id, connection_id=connection.id, backend_type=connection.backend_type,
            model=connection.model, values=dict(values), destinations=destinations,
            input_token_limit=TEST_INPUT_TOKENS, output_token_limit=TEST_OUTPUT_TOKENS,
            timeout_seconds=TEST_TIMEOUT_SECONDS, secrets=dict(secrets),
            plugin_id=connection.plugin_id or instance.plugin_id, engine_values=dict(engine_values or {}),
            price_input_per_million=_price(connection.price_input_per_million),
            price_output_per_million=_price(connection.price_output_per_million))
        started = time.monotonic()
        answer: Mapping[str, Any] = {}
        failure: Exception | None = None
        try:
            answer = self._run_probe(request, claim)
        except Exception as error:  # settled below, then said as a failure without its text
            failure = error
        finally:
            if worst > 0:
                try:
                    self._settle_test(ledger, job_id, call_id, connection, answer, limits)
                finally:
                    ledger.release_job(job_id)
        if failure is not None:
            # A bug in our code is raised, with the secrets masked in what is raised. A lookup that failed on an
            # odd answer from a provider (a missing key, an index) is that provider's failure, not ours.
            if _is_bug(failure):
                self._log_failure(failure, held)
                raise self._sanitized(failure, held)
            self._log_failure(failure, held)
            return self._test_result(False, UNFINISHED_TEST)
        if answer.get("ok") is True:
            return self._test_result(True, "", started)
        # A probe that says the request never left Bazarr+ (too many other tests running, one that would not
        # start) is not a verdict on the back end: the same test may go through in a moment.
        return self._test_result(False, self._said(answer.get("message"), held) or self._unanswered(where),
                                 outcome=NOT_READY if answer.get("sent") is False else FAILED)

    def _run_probe(self, request: ProbeRequest, claim: _Claim) -> Mapping[str, Any]:
        """Call the probe, and stop waiting for it a little after the time it was given.

        The claim on the connection goes with the probe's thread: if this stops waiting, the connection stays
        claimed until the probe really returns, so a second test cannot start beside one that still runs.
        """
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["answer"] = self._probe(request)
            except Exception as error:  # handed to the caller, which says only its type
                box["error"] = error
            finally:
                claim.release()

        thread = threading.Thread(target=run, name="translation-connection-test", daemon=True)
        try:
            thread.start()
        except Exception as error:
            # Nothing was sent, so a priced test is settled at nothing and the request releases the claim.
            logger.warning("A connection test could not start its probe: %s", type(error).__name__)
            return {"ok": False, "sent": False, "message": "The test could not be started. Try again in a moment."}
        claim.handed_to_probe = True  # only once the thread exists: if it never started, the request releases
        thread.join(request.timeout_seconds + TEST_TIMEOUT_GRACE_SECONDS)
        if thread.is_alive():
            raise TimeoutError("the probe did not answer in time")
        if "error" in box:
            raise box["error"]
        return box.get("answer") or {}

    @staticmethod
    def _settle_test(ledger: "UsageLedger", job_id: str, call_id: str, connection, answer: Mapping[str, Any],
                     limits: "CostLimits") -> None:
        """Replace the test's worst-case charge with what it used, when that is known.

        The provider may bill a request whose answer never arrived, so a call that failed, raised or reported
        no usage keeps its worst-case charge. It is settled at nothing only when the probe says the request
        never left (``sent`` is false), which is the one definite free result besides reported usage.
        """
        from .usage import UsageError, worst_case_micros

        prices = (_price(connection.price_input_per_million), _price(connection.price_output_per_million))
        used_in, used_out = answer.get("input_tokens"), answer.get("output_tokens")
        if all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in (used_in, used_out)):
            actual = worst_case_micros(used_in, used_out, *prices)
        elif answer.get("sent") is False:
            used_in = used_out = actual = 0
        else:
            used_in, used_out = TEST_INPUT_TOKENS, TEST_OUTPUT_TOKENS
            actual = worst_case_micros(used_in, used_out, *prices)
        try:
            ledger.settle_call(job_id, call_id, input_tokens=used_in, output_tokens=used_out, actual_micros=actual,
                               limits=limits)
        except UsageError:
            logger.warning("A connection test could not be settled in the usage ledger")

    @staticmethod
    def _typed_origin(schema: DestinationSchema, values: Mapping[str, Any]) -> str:
        """The first address the person typed for this back end, as scheme and host, or an empty string."""
        typed = DestinationSchema(fields=schema.fields)
        baseline = effective_destinations(typed, {})
        for item in sorted(effective_destinations(typed, values) - baseline):
            try:
                return catalog_view._shown(normalize_destination(item))
            except DestinationError:
                continue
        return ""

    @staticmethod
    def _unanswered(where: str) -> str:
        at = f" at {where}" if where else ""
        return (f"No answer{at}. Check the address and the key, and that the server is running and reachable "
                "from the Bazarr+ container.")

    @staticmethod
    def _mask(text: str, held: Mapping[str, str]) -> str:
        """The text with every saved secret and credential shape masked, whitespace left as it was.

        ``held`` is every secret the connection stores, whatever its length. A secret is masked as it is and in
        the forms it takes when something renders it (repr, a dict or tuple inside a message, JSON, unicode
        escapes), so a key with a backslash, a quote or a newline in it does not get through escaped. A short
        value is masked only as a whole word, so it does not eat the letters of an ordinary word.
        """
        pieces = {piece for value in held.values() for piece in [value, *_entries_of(value)] if piece}
        forms: set[str] = set()
        for piece in pieces:
            forms |= _rendered_forms(piece)
        for form in sorted(forms, key=len, reverse=True):
            pattern = re.escape(form) if len(form) >= 4 else r"(?<!\w)" + re.escape(form) + r"(?!\w)"
            text = re.sub(pattern, "***", text)
        try:
            from secret_store.scrub import scrub_text

            return scrub_text(text)
        except Exception:
            return "[the text could not be scrubbed and is left out]"

    @classmethod
    def _said(cls, message: Any, held: Mapping[str, str]) -> str:
        """A probe's message as it may leave: one short line with every saved secret and credential masked.

        The masking runs on the text as it came, before whitespace is collapsed, so a value cannot hide across
        a line break.
        """
        if not isinstance(message, str):
            return ""
        text = cls._mask(message, held)
        if text.startswith("[the text could not"):
            return ""
        return " ".join(text.split())[:TEST_MESSAGE_LIMIT]

    @staticmethod
    def _test_result(ok: bool, message: str, started: float | None = None, *,
                     outcome: str | None = None) -> dict[str, Any]:
        """The answer of a test. ``outcome`` is ``answered`` for one that worked and ``failed`` for one that did
        not, unless the caller knows that nothing was sent and says which kind of refusal it was."""
        latency = int((time.monotonic() - started) * 1000) if ok and started is not None else None
        return {"ok": ok, "message": message, "latency_ms": latency,
                "outcome": outcome or (ANSWERED if ok else FAILED)}

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
        """The stored settings the manifest still declares, with ``values`` laid over them. An empty value removes
        its key (the default applies)."""
        toggles = {item["key"] for item in schema.fields if item.get("type") == "toggle"}
        # A stored key the manifest no longer declares (a release dropped the field) is not carried into a save:
        # the form cannot show it to remove it, and validation would refuse the whole save over it. Only keys
        # sent now are checked against the manifest.
        declared = {item["key"] for item in schema.fields}
        merged = {key: value for key, value in stored.items() if key in declared}
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
    def _split_host(values: Mapping[str, Any], keep=None, schema: DestinationSchema | None = None
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Take the model and the prices out of a connection's values; the rest is the back end's settings.

        This only reads them: the right type, and the field's default for one that was left out of a new
        connection or sent empty. Whether they are within the manifest's limits is decided with everything else
        by ``validate_connection``.
        """
        definitions = host_definitions(schema.fields if schema else ())
        host: dict[str, Any] = (host_of(keep) if keep is not None
                                else {key: host_default(definitions, key) for key in (MODEL_KEY, *PRICE_KEYS)})
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
                    host[key] = value if value.strip() else host_default(definitions, key)
            elif value is None or value == "":
                host[key] = host_default(definitions, key)  # an emptied price goes back to its default
            elif not is_finite_number(value):
                errors[key] = "must be a number"
            else:
                host[key] = value
        if errors:
            raise InvalidValuesError("The values are not valid.", field_errors=errors)
        return host, plain

    def _validate(self, schema: DestinationSchema, values: dict[str, Any], stored: frozenset[str], plugin_id: str,
                  scope: str, backend_type: str, host: Mapping[str, Any] | None = None,
                  contents: Mapping[str, Any] | None = None) -> ValidatedValues:
        # A connection is checked together with its host-kept model and prices; an engine is just its fields.
        # ``contents`` are the stored secrets that stay as they are, so their content is checked too.
        validated = validate_owner(schema.fields, values, stored, host, contents)
        errors = dict(validated.errors)
        if not errors and scope == "connection":
            errors.update(connection_constraint_errors(schema.fields, {**validated.settings, **(host or {})}))
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

    @staticmethod
    def _make_key_durable(validated: ValidatedValues) -> None:
        """Make sure the master encryption key is on disk before a secret under it is written.

        Persisting it writes config.yaml, which takes the config lock. That has to happen before the write
        transaction opens, never inside it: the config lock comes first.
        """
        if not validated.secrets:
            return
        from secret_store import persist_master_key

        try:
            persist_master_key()
        except ValueError:
            raise ServiceError("The encryption key could not be saved, so no secret was stored. Try again.") from None

    def _write_secrets(self, owner, validated: ValidatedValues, clear: list[str], destinations, fields) -> None:
        for name in clear:
            # Clear, then type: a secret sent in the same request replaces the stored one instead of being dropped.
            if name not in validated.secrets:
                self._store.clear_secret(owner, name, destinations=destinations, fields=fields)
        for name, value in validated.secrets.items():
            try:
                self._store.set_secret(owner, name, _json_secret(value), destinations=destinations, fields=fields)
            except TranslatorStoreError as error:
                raise InvalidValuesError(str(error), field_errors={name: str(error)}) from error


__all__ = [
    "ConsentRequiredError", "InstalledPlugin", "InvalidValuesError", "NotFoundError", "PluginUnavailableError",
    "ServiceError", "StaleRevisionError", "TranslatorSettingsService", "field_visible", "installed_plugin"]

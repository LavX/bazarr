# coding=utf-8
"""Engine instances, ordered back-end connections, their secrets and the two engine pointers.

Persistence only: nothing here talks to a worker or decides which connection serves
a job. The store takes an explicit session, like the other repositories.

Rules kept here:

* An engine instance has a ``revision`` that goes up on every save of its settings.
  A connection change (model, order, on or off) does not touch it.
* ``translator.engine`` and ``translator.fallback_engine`` never name the same
  instance. Turning an instance off, or removing it or its plugin, puts a pointer
  that named it back to its default and says so in a notice.
* A secret is encrypted with ``secret_store`` and bound to the destinations it was
  entered under. It is handed out only to a request whose destinations the binding
  covers. An owner with a secret that no longer applies is ``awaiting_secrets``
  and is not usable until the secret is entered again.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import delete, func, select

from app.database import (TableTranslatorConnection, TableTranslatorEngineInstance,
                          TableTranslatorSecret)
from media_servers.repository import atomic

from .destinations import DestinationSchema, added_destinations, covers, effective_destinations

logger = logging.getLogger(__name__)

OFFICIAL_ENGINE_PLUGIN_ID = "ai_translator"
INSTANCE = "instance"
CONNECTION = "connection"
# A secret shorter than this shows only that it is set; a longer one also shows its last four characters.
LAST_FOUR_MIN_LENGTH = 16

Owner = tuple[str, int]


class TranslatorStoreError(ValueError):
    """A request the store refuses, with a reason a person can read."""


@dataclass(frozen=True)
class Notice:
    code: str
    message: str
    instance_id: int | None = None


@dataclass(frozen=True)
class SaveResult:
    revision: int
    new_destinations: frozenset[str]
    awaiting_secrets: bool


@dataclass(frozen=True)
class OwnerChange:
    owner: Owner
    added: frozenset[str]
    has_secrets: bool


@dataclass
class PointerChange:
    notices: list[Notice] = field(default_factory=list)


class ConfigPointers:
    """The two pointers as they live in config.yaml, as instance ids ('' for none)."""

    def get(self) -> tuple[int | None, int | None]:
        from app.config import settings

        return _to_id(settings.translator.engine), _to_id(settings.translator.fallback_engine)

    def set(self, engine: int | None, fallback: int | None) -> bool:
        """Write both pointers. False, with the live values put back, when the file could not be written."""
        from app.config import settings, write_config
        from discover.metadata import CONFIG_LOCK

        with CONFIG_LOCK:
            previous = (settings.translator.engine, settings.translator.fallback_engine)
            try:
                settings.translator.engine = "" if engine is None else str(engine)
                settings.translator.fallback_engine = "" if fallback is None else str(fallback)
                saved = write_config()
            except Exception:
                logger.exception("Could not save the translator engine pointers")
                saved = False
            if saved is not True:
                settings.translator.engine, settings.translator.fallback_engine = previous
                return False
            return True


def _to_id(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _loads(text: str | None, default: Any) -> Any:
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def collect_secret_values() -> set[str]:
    """Every stored translator secret, decrypted, for the log scrubber.

    A secret that will not decrypt is skipped with a warning. A database error
    raises, so the scrubber keeps the set it already has.
    """
    from app.database import database
    from secret_store import decrypt_secret

    values: set[str] = set()
    for encrypted in database.execute(select(TableTranslatorSecret.encrypted_value)).scalars().all():
        try:
            plain = decrypt_secret(encrypted)
        except Exception as error:
            logger.warning("A stored translator secret could not be decrypted to register it for scrubbing: %s",
                           type(error).__name__)
            continue
        if isinstance(plain, str) and plain:
            values.add(plain)
    return values


def default_plugin_active(plugin_id: str) -> bool:
    """True when the plugin is installed with a running release and is switched on."""
    from provider_hub import state as hub_state
    from provider_hub.kinds import UnknownKindError, get_kind, kind_of

    item = (hub_state.load_state().get("installations") or {}).get(plugin_id)
    if not isinstance(item, dict) or not item.get("active_version") or item.get("state") == "removed":
        return False
    try:
        return bool(get_kind(kind_of(item)).is_enabled(item))
    except UnknownKindError:
        return False


class TranslatorStore:
    def __init__(self, session, *, pointers=None, plugin_active: Callable[[str], bool] | None = None):
        self._session = session
        self._pointers = pointers if pointers is not None else ConfigPointers()
        self._plugin_active = plugin_active if plugin_active is not None else default_plugin_active

    # ------------------------------------------------------------------ reads
    def get_instance(self, instance_id: int):
        return self._session.get(TableTranslatorEngineInstance, instance_id)

    def list_instances(self, plugin_id: str | None = None):
        stmt = select(TableTranslatorEngineInstance).order_by(TableTranslatorEngineInstance.id)
        if plugin_id is not None:
            stmt = stmt.where(TableTranslatorEngineInstance.plugin_id == plugin_id)
        return list(self._session.execute(stmt).scalars().all())

    def get_connection(self, connection_id: int):
        return self._session.get(TableTranslatorConnection, connection_id)

    def list_connections(self, instance_id: int):
        stmt = (select(TableTranslatorConnection).where(TableTranslatorConnection.instance_id == instance_id)
                .order_by(TableTranslatorConnection.position))
        return list(self._session.execute(stmt).scalars().all())

    def settings_of(self, row) -> dict[str, Any]:
        return _loads(row.settings_json, {})

    # ---------------------------------------------------------- usable rules
    def connection_usable(self, connection) -> bool:
        """On, served by an installed and enabled plugin, and not waiting for its secrets."""
        return bool(connection.enabled) and not connection.awaiting_secrets and self._serving_plugin_active(connection)

    def _serving_plugin_active(self, connection) -> bool:
        if connection.plugin_id:
            return self._plugin_active(connection.plugin_id)
        instance = self.get_instance(connection.instance_id)
        return instance is not None and self._plugin_active(instance.plugin_id)

    def instance_configured(self, instance) -> bool:
        """The plugin is installed and on, the instance is on and not waiting for its secrets.

        An engine that serves back-end connections (it has any) also needs one usable connection.
        """
        if instance is None or not instance.enabled or instance.awaiting_secrets:
            return False
        if not self._plugin_active(instance.plugin_id):
            return False
        connections = self.list_connections(instance.id)
        if instance.plugin_id == OFFICIAL_ENGINE_PLUGIN_ID or connections:
            return any(self.connection_usable(connection) for connection in connections)
        return True

    # -------------------------------------------------------------- instances
    def create_instance(self, plugin_id: str, *, name: str = "", settings: Mapping[str, Any] | None = None,
                        settings_version: int | None = None, plugin_version: str | None = None,
                        enabled: bool = True):
        if not plugin_id:
            raise TranslatorStoreError("an engine instance needs a plugin id")
        row = TableTranslatorEngineInstance(
            plugin_id=plugin_id, name=name, enabled=1 if enabled else 0,
            settings_json=json.dumps(dict(settings or {}), sort_keys=True),
            settings_version=settings_version, plugin_version=plugin_version, revision=1)
        with atomic(self._session, durable=True):
            self._session.add(row)
            self._session.flush()
        return row

    def ensure_instance(self, plugin_id: str, **kwargs):
        """The plugin's first instance, created when it has none."""
        existing = self.list_instances(plugin_id)
        return existing[0] if existing else self.create_instance(plugin_id, **kwargs)

    def save_instance_settings(self, instance_id: int, settings: Mapping[str, Any], *,
                               schema: DestinationSchema, settings_version: int | None = None,
                               plugin_version: str | None = None,
                               expected_revision: int | None = None) -> SaveResult:
        """Replace an instance's settings. Bumps the revision and checks the destinations."""
        instance = self._require_instance(instance_id)
        if expected_revision is not None and instance.revision != expected_revision:
            raise TranslatorStoreError("the engine settings changed since they were loaded")
        owner = (INSTANCE, instance_id)
        before = effective_destinations(schema, self.settings_of(instance))
        after = effective_destinations(schema, settings)
        with atomic(self._session, durable=True):
            instance.settings_json = json.dumps(dict(settings), sort_keys=True)
            instance.revision = int(instance.revision) + 1
            if settings_version is not None:
                instance.settings_version = settings_version
            if plugin_version is not None:
                instance.plugin_version = plugin_version
            instance.updated_at = datetime.now()
            instance.awaiting_secrets = 1 if self._stale_secrets(owner, after) else 0
        return SaveResult(instance.revision, added_destinations(before, after), bool(instance.awaiting_secrets))

    def set_instance_enabled(self, instance_id: int, enabled: bool) -> PointerChange:
        instance = self._require_instance(instance_id)
        with atomic(self._session, durable=True):
            instance.enabled = 1 if enabled else 0
            instance.updated_at = datetime.now()
        if enabled:
            return PointerChange()
        return self.release_pointers({instance_id}, why="turned off")

    def delete_instance(self, instance_id: int) -> PointerChange:
        """Remove an instance with its connections and every secret they own."""
        self._require_instance(instance_id)
        connection_ids = [row.id for row in self.list_connections(instance_id)]
        with atomic(self._session, durable=True):
            for connection_id in connection_ids:
                self._delete_secrets((CONNECTION, connection_id))
            self._delete_secrets((INSTANCE, instance_id))
            self._session.execute(delete(TableTranslatorConnection)
                                  .where(TableTranslatorConnection.instance_id == instance_id))
            self._session.execute(delete(TableTranslatorEngineInstance)
                                  .where(TableTranslatorEngineInstance.id == instance_id))
        self._session.expire_all()
        return self.release_pointers({instance_id}, why="removed")

    def remove_plugin(self, plugin_id: str) -> PointerChange:
        """The plugin was uninstalled: every instance of it goes, and the pointers that named one reset."""
        merged = PointerChange()
        for instance in self.list_instances(plugin_id):
            merged.notices.extend(self.delete_instance(instance.id).notices)
        return merged

    def plugin_disabled(self, plugin_id: str) -> PointerChange:
        """The plugin was switched off. Its instances keep their own switches; the pointers let go of them."""
        ids = {instance.id for instance in self.list_instances(plugin_id)}
        return self.release_pointers(ids, why="turned off with its plugin") if ids else PointerChange()

    # ------------------------------------------------------------ connections
    def add_connection(self, instance_id: int, backend_type: str, *, plugin_id: str = "", model: str = "",
                       settings: Mapping[str, Any] | None = None, settings_version: int | None = None,
                       plugin_version: str | None = None, price_input_per_million: float = 0,
                       price_output_per_million: float = 0, enabled: bool = True):
        """Append a connection at the end of the instance's list."""
        self._require_instance(instance_id)
        if not backend_type:
            raise TranslatorStoreError("a connection needs a back-end type")
        _check_price(price_input_per_million, price_output_per_million)
        with atomic(self._session, durable=True):
            last = self._session.execute(
                select(func.max(TableTranslatorConnection.position))
                .where(TableTranslatorConnection.instance_id == instance_id)).scalar()
            row = TableTranslatorConnection(
                instance_id=instance_id, position=0 if last is None else int(last) + 1, plugin_id=plugin_id,
                backend_type=backend_type, model=model, settings_json=json.dumps(dict(settings or {}), sort_keys=True),
                settings_version=settings_version, plugin_version=plugin_version,
                price_input_per_million=price_input_per_million, price_output_per_million=price_output_per_million,
                enabled=1 if enabled else 0)
            self._session.add(row)
            self._session.flush()
        return row

    def update_connection(self, connection_id: int, *, schema: DestinationSchema, model: str | None = None,
                          settings: Mapping[str, Any] | None = None, settings_version: int | None = None,
                          plugin_version: str | None = None, price_input_per_million: float | None = None,
                          price_output_per_million: float | None = None, enabled: bool | None = None) -> SaveResult:
        """Change a connection. Never touches the instance's revision."""
        connection = self._require_connection(connection_id)
        _check_price(price_input_per_million or 0, price_output_per_million or 0)
        before = effective_destinations(schema, self.settings_of(connection))
        new_settings = self.settings_of(connection) if settings is None else dict(settings)
        after = effective_destinations(schema, new_settings)
        with atomic(self._session, durable=True):
            if model is not None:
                connection.model = model
            if settings is not None:
                connection.settings_json = json.dumps(new_settings, sort_keys=True)
            if settings_version is not None:
                connection.settings_version = settings_version
            if plugin_version is not None:
                connection.plugin_version = plugin_version
            if price_input_per_million is not None:
                connection.price_input_per_million = price_input_per_million
            if price_output_per_million is not None:
                connection.price_output_per_million = price_output_per_million
            if enabled is not None:
                connection.enabled = 1 if enabled else 0
            connection.updated_at = datetime.now()
            connection.awaiting_secrets = 1 if self._stale_secrets((CONNECTION, connection_id), after) else 0
        return SaveResult(self._require_instance(connection.instance_id).revision,
                          added_destinations(before, after), bool(connection.awaiting_secrets))

    def delete_connection(self, connection_id: int) -> None:
        """Remove a connection and its secrets, then close the gap in the order."""
        connection = self._require_connection(connection_id)
        instance_id = connection.instance_id
        with atomic(self._session, durable=True):
            self._delete_secrets((CONNECTION, connection_id))
            self._session.execute(delete(TableTranslatorConnection)
                                  .where(TableTranslatorConnection.id == connection_id))
        self._session.expire_all()
        self._renumber(instance_id)

    def reorder_connections(self, instance_id: int, ordered_ids: Sequence[int]) -> None:
        """Set the order of an instance's connections. ``ordered_ids`` must name every one of them once."""
        rows = {row.id: row for row in self.list_connections(instance_id)}
        if sorted(ordered_ids) != sorted(rows):
            raise TranslatorStoreError("the new order must list every connection of the engine once")
        with atomic(self._session, durable=True):
            # Two passes: the unique (instance, position) index would refuse a swap in one.
            for index, connection_id in enumerate(ordered_ids):
                rows[connection_id].position = -(index + 1)
            self._session.flush()
            for index, connection_id in enumerate(ordered_ids):
                rows[connection_id].position = index
            self._session.flush()

    def _renumber(self, instance_id: int) -> None:
        rows = self.list_connections(instance_id)
        if [row.position for row in rows] == list(range(len(rows))):
            return
        self.reorder_connections(instance_id, [row.id for row in rows])

    # ---------------------------------------------------------------- secrets
    def set_secret(self, owner: Owner, field_name: str, value: str, *, destinations: frozenset[str]) -> None:
        """Store a secret, encrypted, bound to ``destinations``. An empty value keeps what is stored."""
        from secret_store import encrypt_secret, persist_master_key

        self._require_owner(owner)
        if not value:
            return
        persist_master_key()
        encrypted = encrypt_secret(value)
        with atomic(self._session, durable=True):
            row = self._secret_row(owner, field_name)
            payload = json.dumps(sorted(destinations))
            if row is None:
                self._session.add(TableTranslatorSecret(
                    owner_kind=owner[0], owner_id=owner[1], field=field_name, encrypted_value=encrypted,
                    destinations_json=payload))
            else:
                row.encrypted_value = encrypted
                row.destinations_json = payload
                row.updated_at = datetime.now()
            self._session.flush()
            self._refresh_awaiting(owner, destinations)
        self._refresh_scrubbing()

    def clear_secret(self, owner: Owner, field_name: str, *, destinations: frozenset[str]) -> None:
        with atomic(self._session, durable=True):
            self._session.execute(delete(TableTranslatorSecret).where(
                TableTranslatorSecret.owner_kind == owner[0], TableTranslatorSecret.owner_id == owner[1],
                TableTranslatorSecret.field == field_name))
            self._refresh_awaiting(owner, destinations)
        self._refresh_scrubbing()

    def secrets_for_request(self, owner: Owner, destinations: frozenset[str]) -> dict[str, str]:
        """The decrypted secrets a request with these effective destinations may carry.

        A secret whose binding does not cover every destination is left out, so a key
        never follows a new address. A secret that will not decrypt is left out too.
        """
        from secret_store import decrypt_secret

        granted: dict[str, str] = {}
        for row in self._secret_rows(owner):
            if not covers(_loads(row.destinations_json, []), destinations):
                continue
            try:
                granted[row.field] = decrypt_secret(row.encrypted_value)
            except ValueError:
                logger.error("Cannot decrypt a translator secret (%s %s, field %s); enter it again.",
                             owner[0], owner[1], row.field)
        return granted

    def secret_summary(self, owner: Owner, destinations: frozenset[str]) -> dict[str, dict[str, Any]]:
        """What the API may say about each stored secret: that it is set, and sometimes its last four characters."""
        from secret_store import decrypt_secret

        summary: dict[str, dict[str, Any]] = {}
        for row in self._secret_rows(owner):
            entry: dict[str, Any] = {"configured": True,
                                     "stale": not covers(_loads(row.destinations_json, []), destinations)}
            try:
                plain = decrypt_secret(row.encrypted_value)
            except ValueError:
                entry["decrypt_failed"] = True
            else:
                if len(plain) >= LAST_FOUR_MIN_LENGTH:
                    entry["last4"] = plain[-4:]
            summary[row.field] = entry
        return summary

    def refresh_awaiting(self, owner: Owner, destinations: frozenset[str]) -> bool:
        """Recompute and store whether the owner waits for its secrets. Returns the new flag."""
        with atomic(self._session, durable=True):
            return self._refresh_awaiting(owner, destinations)

    def _refresh_awaiting(self, owner: Owner, destinations: frozenset[str]) -> bool:
        waiting = self._stale_secrets(owner, destinations)
        row = self._require_owner(owner)
        row.awaiting_secrets = 1 if waiting else 0
        return waiting

    def _stale_secrets(self, owner: Owner, destinations: frozenset[str]) -> bool:
        return any(not covers(_loads(row.destinations_json, []), destinations) for row in self._secret_rows(owner))

    def _secret_rows(self, owner: Owner):
        return list(self._session.execute(select(TableTranslatorSecret).where(
            TableTranslatorSecret.owner_kind == owner[0], TableTranslatorSecret.owner_id == owner[1])
            .order_by(TableTranslatorSecret.field)).scalars().all())

    def _secret_row(self, owner: Owner, field_name: str):
        return self._session.execute(select(TableTranslatorSecret).where(
            TableTranslatorSecret.owner_kind == owner[0], TableTranslatorSecret.owner_id == owner[1],
            TableTranslatorSecret.field == field_name)).scalar_one_or_none()

    def _delete_secrets(self, owner: Owner) -> None:
        self._session.execute(delete(TableTranslatorSecret).where(
            TableTranslatorSecret.owner_kind == owner[0], TableTranslatorSecret.owner_id == owner[1]))

    @staticmethod
    def _refresh_scrubbing() -> None:
        """Rebuild the whole log-scrub set, which now includes the stored translator secrets.

        Never registers a value on its own: registering replaces the set, which would
        unmask every settings and Provider Hub credential.
        """
        try:
            from secret_store.scrub import refresh_secret_values
            refresh_secret_values()
        except Exception:
            logger.debug("Could not refresh the log scrubbing after a translator secret change", exc_info=True)

    # ----------------------------------------------------------- plugin update
    def destination_changes_for_update(
            self, plugin_id: str, old_schema: Callable[[str, Any], DestinationSchema],
            new_schema: Callable[[str, Any], DestinationSchema],
            new_settings: Callable[[str, Any], Mapping[str, Any] | None] | None = None) -> list[OwnerChange]:
        """Which owners of this plugin gain a destination when it updates, whether or not a migration runs.

        ``old_schema`` and ``new_schema`` return the schema of an owner (``"instance"`` or
        ``"connection"`` and its row) before and after. ``new_settings`` returns the settings an
        owner has after a migration, or None to keep what is stored. Nothing is written: the
        caller asks the user first. Call ``apply_plugin_update`` once the update is activated.
        """
        changes = []
        for kind, row in self._owners_of_plugin(plugin_id):
            stored = self.settings_of(row)
            after_settings = stored
            if new_settings is not None:
                migrated = new_settings(kind, row)
                after_settings = stored if migrated is None else migrated
            added = added_destinations(effective_destinations(old_schema(kind, row), stored),
                                       effective_destinations(new_schema(kind, row), after_settings))
            if added:
                changes.append(OwnerChange((kind, row.id), added, bool(self._secret_rows((kind, row.id)))))
        return changes

    def apply_plugin_update(self, plugin_id: str, new_schema: Callable[[str, Any], DestinationSchema],
                            new_settings: Callable[[str, Any], Mapping[str, Any] | None] | None = None,
                            plugin_version: str | None = None, settings_version: int | None = None) -> list[Owner]:
        """Record an activated update: store migrated settings and mark owners whose secrets no longer apply.

        Returns the owners now awaiting their secrets. Secrets stay stored under the destinations they
        were entered with, so a rollback finds them.
        """
        waiting: list[Owner] = []
        with atomic(self._session, durable=True):
            for kind, row in self._owners_of_plugin(plugin_id):
                if new_settings is not None:
                    migrated = new_settings(kind, row)
                    if migrated is not None:
                        row.settings_json = json.dumps(dict(migrated), sort_keys=True)
                        if kind == INSTANCE:
                            row.revision = int(row.revision) + 1
                if plugin_version is not None:
                    row.plugin_version = plugin_version
                if settings_version is not None:
                    row.settings_version = settings_version
                if self._refresh_awaiting((kind, row.id), effective_destinations(new_schema(kind, row),
                                                                                  self.settings_of(row))):
                    waiting.append((kind, row.id))
        return waiting

    def _owners_of_plugin(self, plugin_id: str):
        owners = [(INSTANCE, row) for row in self.list_instances(plugin_id)]
        stmt = select(TableTranslatorConnection).where(
            (TableTranslatorConnection.plugin_id == plugin_id)
            | ((TableTranslatorConnection.plugin_id == "") & TableTranslatorConnection.instance_id.in_(
                [row.id for _kind, row in owners] or [0])))
        owners += [(CONNECTION, row) for row in self._session.execute(stmt).scalars().all()]
        return owners

    # --------------------------------------------------------------- pointers
    def pointers(self) -> tuple[int | None, int | None]:
        return self._pointers.get()

    def default_fallback(self, engine: int | None) -> int | None:
        """The official AI engine's instance when it is configured and is not the selected engine."""
        for instance in self.list_instances(OFFICIAL_ENGINE_PLUGIN_ID):
            if instance.id != engine and self.instance_configured(instance):
                return instance.id
        return None

    def set_engine(self, instance_id: int | None) -> PointerChange:
        """Select the engine. A fallback that named the same instance goes back to its default."""
        if instance_id is not None:
            self._require_instance(instance_id)
        engine, fallback = self._pointers.get()
        change = PointerChange()
        if fallback is not None and fallback == instance_id:
            fallback = self.default_fallback(instance_id)
            change.notices.append(Notice(
                "fallback_reset", "The fallback engine was reset because it is now the selected engine.",
                instance_id))
        self._write_pointers(instance_id, fallback)
        return change

    def set_fallback(self, instance_id: int | None) -> PointerChange:
        """Choose the fallback engine. It must not be the selected engine."""
        engine, _fallback = self._pointers.get()
        if instance_id is not None:
            self._require_instance(instance_id)
            if instance_id == engine:
                raise TranslatorStoreError("the fallback engine cannot be the selected engine")
        self._write_pointers(engine, instance_id)
        return PointerChange()

    def release_pointers(self, instance_ids: set[int], *, why: str) -> PointerChange:
        """Reset any pointer that names one of these instances to its default, with a notice."""
        engine, fallback = self._pointers.get()
        change = PointerChange()
        new_engine, new_fallback = engine, fallback
        if engine in instance_ids:
            new_engine = None
            change.notices.append(Notice("engine_reset", f"The selected translation engine was {why}, "
                                                         "so no engine is selected.", engine))
        if fallback in instance_ids:
            new_fallback = self.default_fallback(new_engine)
            if new_fallback in instance_ids:
                new_fallback = None
            change.notices.append(Notice("fallback_reset", f"The fallback translation engine was {why}.", fallback))
        if (new_engine, new_fallback) != (engine, fallback):
            self._write_pointers(new_engine, new_fallback)
        return change

    def _write_pointers(self, engine: int | None, fallback: int | None) -> None:
        if engine is not None and engine == fallback:
            raise TranslatorStoreError("the fallback engine cannot be the selected engine")
        if not self._pointers.set(engine, fallback):
            raise TranslatorStoreError("the engine choice could not be saved")

    # ---------------------------------------------------------------- helpers
    def _require_instance(self, instance_id: int):
        row = self.get_instance(instance_id)
        if row is None:
            raise TranslatorStoreError(f"no engine instance {instance_id}")
        return row

    def _require_connection(self, connection_id: int):
        row = self.get_connection(connection_id)
        if row is None:
            raise TranslatorStoreError(f"no connection {connection_id}")
        return row

    def _require_owner(self, owner: Owner):
        if owner[0] == INSTANCE:
            return self._require_instance(owner[1])
        if owner[0] == CONNECTION:
            return self._require_connection(owner[1])
        raise TranslatorStoreError(f"unknown secret owner {owner[0]!r}")


def _check_price(*prices: float) -> None:
    for price in prices:
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0 or price != price:
            raise TranslatorStoreError("a price per million tokens must be a number from 0 up")

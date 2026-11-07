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
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import delete, func, select, update

from app.database import (TableTranslatorConnection, TableTranslatorEngineInstance,
                          TableTranslatorSecret)

from .field_schema import is_finite_number, is_secret_field
from .destinations import DestinationSchema, added_destinations, covers, effective_destinations
from .transaction import inside_write_transaction, write_transaction

logger = logging.getLogger(__name__)

OFFICIAL_ENGINE_PLUGIN_ID = "ai_translator"
INSTANCE = "instance"
CONNECTION = "connection"
# A secret shorter than this shows only that it is set; a longer one also shows its last four characters.
LAST_FOUR_MIN_LENGTH = 16

Owner = tuple[str, int]
# Stands in for a pointer store that has no lock of its own (a test double).
_POINTER_LOCK = threading.RLock()
_UNSET: Any = object()


class TranslatorStoreError(ValueError):
    """A request the store refuses, with a reason a person can read."""


class StaleRevisionError(TranslatorStoreError):
    """The row changed since the caller read it; nothing was written."""

    def __init__(self, message: str, revision: int | None = None):
        super().__init__(message)
        self.revision = revision


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

    def lock(self):
        """The lock every writer of config.yaml takes. Hold it across a whole read, check and write."""
        from discover.metadata import CONFIG_LOCK

        return CONFIG_LOCK

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
            values.update(_list_entries(plain))
    return values


def _list_entries(plain: str) -> list[str]:
    """The entries of a list secret (a JSON array of strings), so each key is scrubbed on its own."""
    if not plain.startswith("["):
        return []
    try:
        parsed = json.loads(plain)
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str) and item] if isinstance(parsed, list) else []


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
        with write_transaction(self._session):
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
            raise StaleRevisionError("the engine settings changed since they were loaded", instance.revision)
        owner = (INSTANCE, instance_id)
        before = effective_destinations(schema, self.settings_of(instance))
        after = effective_destinations(schema, settings)
        with write_transaction(self._session):
            current = int(instance.revision)
            # Compare and swap in the database, so two saves that both read this revision cannot both win.
            claimed = self._session.execute(
                update(TableTranslatorEngineInstance)
                .where(TableTranslatorEngineInstance.id == instance_id,
                       TableTranslatorEngineInstance.revision == current)
                .values(revision=current + 1).execution_options(synchronize_session=False)).rowcount
            if claimed != 1:
                raise StaleRevisionError("the engine settings changed since they were loaded")
            instance.settings_json = json.dumps(dict(settings), sort_keys=True)
            instance.revision = current + 1
            if settings_version is not None:
                instance.settings_version = settings_version
            if plugin_version is not None:
                instance.plugin_version = plugin_version
            instance.updated_at = datetime.now()
            instance.awaiting_secrets = 1 if self._stale_secrets(owner, after) else 0
        return SaveResult(instance.revision, added_destinations(before, after), bool(instance.awaiting_secrets))

    def set_instance_enabled(self, instance_id: int, enabled: bool, *,
                             expected_revision: int | None = None) -> PointerChange:
        """Turn an engine on or off. Bumps the revision, and with ``expected_revision`` swaps it in the database
        so a switch made from a stale view cannot overwrite a newer save.

        The revision is claimed first, inside the transaction; only then do the pointers let go of an engine
        that was turned off. If the pointers cannot be saved, the transaction rolls back and the engine stays as
        it was, and a switch that loses the race changes no pointer at all.
        """
        instance = self._require_instance(instance_id)
        if expected_revision is not None and instance.revision != expected_revision:
            raise StaleRevisionError("the engine settings changed since they were loaded", instance.revision)
        with write_transaction(self._session):
            current = int(instance.revision)
            claimed = self._session.execute(
                update(TableTranslatorEngineInstance)
                .where(TableTranslatorEngineInstance.id == instance_id,
                       TableTranslatorEngineInstance.revision == current)
                .values(revision=current + 1).execution_options(synchronize_session=False)).rowcount
            if claimed != 1:
                raise StaleRevisionError("the engine settings changed since they were loaded")
            instance.revision = current + 1
            instance.enabled = 1 if enabled else 0
            instance.updated_at = datetime.now()
            self._session.flush()
            return PointerChange() if enabled else self.release_pointers({instance_id}, why="turned off")

    def delete_instance(self, instance_id: int) -> PointerChange:
        """Remove an instance with its connections and every secret they own.

        The pointers that named it are saved first. If that write fails nothing has been deleted, and if
        the delete fails afterwards the only loss is a pointer that was reset anyway.
        """
        self._require_instance(instance_id)
        change = self.release_pointers({instance_id}, why="removed")
        self._delete_instance_rows(instance_id)
        return change

    def _delete_instance_rows(self, instance_id: int) -> None:
        connection_ids = [row.id for row in self.list_connections(instance_id)]
        with write_transaction(self._session):
            for connection_id in connection_ids:
                self._delete_secrets((CONNECTION, connection_id))
            self._delete_secrets((INSTANCE, instance_id))
            self._session.execute(delete(TableTranslatorConnection)
                                  .where(TableTranslatorConnection.instance_id == instance_id))
            self._session.execute(delete(TableTranslatorEngineInstance)
                                  .where(TableTranslatorEngineInstance.id == instance_id))
        self._session.expire_all()

    def release_plugin(self, plugin_id: str) -> PointerChange:
        """The plugin is being uninstalled: the pointers that name one of its instances let go. Deletes nothing."""
        ids = {instance.id for instance in self.list_instances(plugin_id)}
        return self.release_pointers(ids, why="removed") if ids else PointerChange()

    def delete_plugin_data(self, plugin_id: str) -> None:
        """Delete every instance of the plugin with its connections and secrets. Call it once the removal is saved."""
        for instance in self.list_instances(plugin_id):
            self._delete_instance_rows(instance.id)

    def remove_plugin(self, plugin_id: str) -> PointerChange:
        """The plugin was uninstalled: the pointers that named an instance reset, then every instance goes."""
        change = self.release_plugin(plugin_id)
        self.delete_plugin_data(plugin_id)
        return change

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
        with write_transaction(self._session):
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
                          price_output_per_million: float | None = None, enabled: bool | None = None,
                          expected_revision: int | None = None) -> SaveResult:
        """Change a connection and bump its own revision. Never touches the instance's revision."""
        connection = self._require_connection(connection_id)
        if expected_revision is not None and connection.revision != expected_revision:
            raise StaleRevisionError("the back end changed since it was loaded", connection.revision)
        _check_price(price_input_per_million or 0, price_output_per_million or 0)
        before = effective_destinations(schema, self.settings_of(connection))
        new_settings = self.settings_of(connection) if settings is None else dict(settings)
        after = effective_destinations(schema, new_settings)
        with write_transaction(self._session):
            current = int(connection.revision)
            claimed = self._session.execute(
                update(TableTranslatorConnection)
                .where(TableTranslatorConnection.id == connection_id,
                       TableTranslatorConnection.revision == current)
                .values(revision=current + 1).execution_options(synchronize_session=False)).rowcount
            if claimed != 1:
                raise StaleRevisionError("the back end changed since it was loaded")
            connection.revision = current + 1
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

    def delete_connection(self, connection_id: int, *, expected_revision: int | None = None) -> None:
        """Remove a connection and its secrets, then close the gap in the order.

        The delete is conditional on the revision, inside the transaction, so a save that landed after the
        caller loaded the connection is never deleted unseen; if the delete loses, the secrets stay.
        """
        connection = self._require_connection(connection_id)
        instance_id = connection.instance_id
        current = int(connection.revision if expected_revision is None else expected_revision)
        with write_transaction(self._session):
            removed = self._session.execute(delete(TableTranslatorConnection).where(
                TableTranslatorConnection.id == connection_id,
                TableTranslatorConnection.revision == current)).rowcount
            if removed != 1:
                raise StaleRevisionError("the back end changed since it was loaded")
            self._delete_secrets((CONNECTION, connection_id))
        self._session.expire_all()
        self._renumber(instance_id)

    def reorder_connections(self, instance_id: int, ordered_ids: Sequence[int]) -> None:
        """Set the order of an instance's connections. ``ordered_ids`` must name every one of them once."""
        rows = {row.id: row for row in self.list_connections(instance_id)}
        if sorted(ordered_ids) != sorted(rows):
            raise TranslatorStoreError("the new order must list every connection of the engine once")
        with write_transaction(self._session):
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
        with write_transaction(self._session):
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
        self._scrub_after_commit()

    def clear_secret(self, owner: Owner, field_name: str, *, destinations: frozenset[str]) -> None:
        with write_transaction(self._session):
            self._session.execute(delete(TableTranslatorSecret).where(
                TableTranslatorSecret.owner_kind == owner[0], TableTranslatorSecret.owner_id == owner[1],
                TableTranslatorSecret.field == field_name))
            self._refresh_awaiting(owner, destinations)
        self._scrub_after_commit()

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
        with write_transaction(self._session):
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

    @contextmanager
    def transaction(self):
        """Run several store calls as one transaction: all of them commit together, or none does.

        The log-scrub set is rebuilt once, after the commit, because it reads the secrets table through
        another connection that cannot see what is not committed yet.
        """
        outermost = not inside_write_transaction(self._session)
        with write_transaction(self._session):
            yield self
        if outermost:
            self._refresh_scrubbing()

    def _scrub_after_commit(self) -> None:
        if not inside_write_transaction(self._session):
            self._refresh_scrubbing()

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
                            plugin_version: str | None = None, settings_version: int | None = None,
                            skip: Iterable[Owner] = ()) -> list[Owner]:
        """Record an activated update: store migrated settings and mark owners whose secrets no longer apply.

        Owners in ``skip`` are left exactly as they were (the caller found their settings do not fit the new
        release and holds them back separately).

        Returns the owners now awaiting their secrets. Secrets stay stored under the destinations they
        were entered with, so a rollback finds them.
        """
        waiting: list[Owner] = []
        # self.transaction(), not a bare transaction: it rebuilds the log-scrub set once the changes have
        # committed, so a secret moved here is masked in the very next log line.
        with self.transaction():
            skipped = set(skip)
            for kind, row in self._owners_of_plugin(plugin_id):
                if (kind, row.id) in skipped:
                    continue
                stored_before = self.settings_of(row)
                if new_settings is not None:
                    migrated = new_settings(kind, row)
                    if migrated is not None:
                        row.settings_json = json.dumps(dict(migrated), sort_keys=True)
                self._move_reclassified_secrets((kind, row.id), row, new_schema(kind, row))
                if self.settings_of(row) != stored_before:
                    # Anything that changes what an owner stores moves its revision, so a draft loaded before
                    # the update can no longer be saved over it unseen.
                    row.revision = int(row.revision) + 1
                if plugin_version is not None:
                    row.plugin_version = plugin_version
                if settings_version is not None:
                    row.settings_version = settings_version
                if self._refresh_awaiting((kind, row.id), effective_destinations(new_schema(kind, row),
                                                                                  self.settings_of(row))):
                    waiting.append((kind, row.id))
        return waiting

    def hold_back_for_attention(self, owners: Iterable[Owner]) -> PointerChange:
        """Turn owners off and flag them for the person to look at, keeping everything they hold.

        Used when a plugin update leaves an owner's settings unfit for the new release. The flag is the one
        that already means "enter this again": the owner is not usable until it is saved again, and an engine
        that was turned off lets the pointers go.
        """
        engine_ids: set[int] = set()
        with self.transaction():
            for kind, owner_id in owners:
                row = self._require_owner((kind, owner_id))
                row.enabled = 0
                row.awaiting_secrets = 1
                row.revision = int(row.revision) + 1
                row.updated_at = datetime.now()
                if kind == INSTANCE:
                    engine_ids.add(owner_id)
            self._session.flush()
            return self.release_pointers(engine_ids, why="turned off by a plugin update") if engine_ids \
                else PointerChange()

    def _move_reclassified_secrets(self, owner: Owner, row, schema: DestinationSchema) -> None:
        """A field the plugin now declares secret may still hold a plaintext value from when it was not.

        The value moves into the secret store, bound to the owner's destinations, and leaves the settings
        JSON. A value that is empty just leaves it.
        """
        settings = self.settings_of(row)
        secret_keys = [item["key"] for item in schema.fields if is_secret_field(item) and item["key"] in settings]
        if not secret_keys:
            return
        moved = {key: settings.pop(key) for key in secret_keys}
        row.settings_json = json.dumps(settings, sort_keys=True)
        destinations = effective_destinations(schema, settings)
        for key, value in moved.items():
            if value in (None, "", []):
                continue
            self.set_secret(owner, key, value if isinstance(value, str) else json.dumps(value),
                            destinations=destinations)

    def owners_of_plugin(self, plugin_id: str):
        """Every engine instance and connection a plugin serves, as ``(kind, row)``."""
        return self._owners_of_plugin(plugin_id)

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
        return self.set_pointer_pair(engine=instance_id)

    def set_fallback(self, instance_id: int | None) -> PointerChange:
        """Choose the fallback engine. It must not be the selected engine."""
        return self.set_pointer_pair(fallback=instance_id)

    def set_pointer_pair(self, engine: Any = _UNSET, fallback: Any = _UNSET) -> PointerChange:
        """Change the engine, the fallback or both. The proposed pair is checked as a whole, then written once.

        An argument left out keeps its current value. A fallback that is left out and now names the selected
        engine goes back to its default, with a notice; a fallback that is asked for and names it is refused.
        Nothing is written when any part is refused.
        """
        for value in (engine, fallback):
            if value is not _UNSET and value is not None:
                self._require_instance(value)
        # The pair is read, checked and written under one lock: two requests that each change one pointer both
        # land, instead of the later write putting back the value the earlier one read.
        with self._pointer_cycle():
            return self._change_pointer_pair(engine, fallback)

    def _pointer_cycle(self):
        return getattr(self._pointers, "lock", lambda: _POINTER_LOCK)()

    def _change_pointer_pair(self, engine: Any, fallback: Any) -> PointerChange:
        current_engine, current_fallback = self._pointers.get()
        new_engine = current_engine if engine is _UNSET else engine
        change = PointerChange()
        if fallback is _UNSET:
            new_fallback = current_fallback
            if engine is not _UNSET and new_fallback is not None and new_fallback == new_engine:
                new_fallback = self.default_fallback(new_engine)
                change.notices.append(Notice(
                    "fallback_reset", "The fallback engine was reset because it is now the selected engine.",
                    new_engine))
        else:
            new_fallback = fallback
            if new_fallback is not None and new_fallback == new_engine:
                raise TranslatorStoreError("the fallback engine cannot be the selected engine")
        self._write_pointers(new_engine, new_fallback)
        return change

    def release_pointers(self, instance_ids: set[int], *, why: str) -> PointerChange:
        """Reset any pointer that names one of these instances to its default, with a notice."""
        with self._pointer_cycle():
            return self._release_pointers(instance_ids, why)

    def _release_pointers(self, instance_ids: set[int], why: str) -> PointerChange:
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
        if not is_finite_number(price) or price < 0:
            raise TranslatorStoreError("a price per million tokens must be a number from 0 up")

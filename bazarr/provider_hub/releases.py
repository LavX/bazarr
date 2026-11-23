# coding=utf-8
"""Plugin releases: the release model and the tables that hold it.

An installation is the plugin as the person sees it: its settings, whether it
is switched on, where it came from. A release is one version of that plugin's
code with the bundle, environment and manifest it runs from. An installation
has at most one active release and at most one staged one, and keeps the
release it replaced for a rollback.

States: ``staged`` (written, not running), ``active`` (the release the plugin
runs), ``draining`` (replaced or stopped, finishing the calls it already took),
``retired`` (no longer runs, kept as the previous release) and ``failed`` (a
staged release that did not activate, kept so the reason can be shown).

The rest of the Hub reads and writes installation rows as dicts, with the
release fields (``active_version``, ``staged_path``, ``python_path`` and so on)
inline. This module is the other half of that shape: ``split`` takes those
fields out of a row into release rows, ``compose`` puts them back, so the
database holds releases in tables of their own while every caller keeps one
dict API. The fields of an installation row that are not about a release
(``name``, ``config``, ``enabled``, the revocation and hold marks) stay in the
state document.

Everything here is pure except the functions that take a connection.

Two things to know before changing this module:

* On a real upgrade the store, not the migration, does the move. Settings load reads the state before the
  schema step runs, so ``ensure_ready`` creates the tables and the first write splits every row through
  ``split``. The migration moves the rows of a database the store has not written to yet, with its own frozen
  copy of the logic; the two are pinned together by a test. A column added here later has to be created by
  ``create_tables`` as well as by its revision, and ``read_rows`` must not select it before then.
* Release rows of a plugin the document does not list (what a damaged document lost) are never deleted by a
  write. They are logged at boot and their files stay protected. Ending them is an administrator's repair step,
  with no screen of its own: ``state_store.discard_orphan_releases(state_store.orphan_plugin_ids())`` run in a
  Python shell against the app's database, once whoever runs it has looked at what is kept.
* A release leaves while Bazarr+ runs in three ways, and ``detach`` does the consumer side of each. A live
  activation that replaces a release (a kind that attaches live) sends the old one to ``draining`` through
  ``DRAIN_PREVIOUS``; once the pools have let go of it and its calls are done, ``retire_release`` retires it, and
  until then it is not a rollback target. A removal marks the installation removed and deletes the row, with all
  its releases, once the calls are done, so those releases go through no other state. A revoked release is stopped
  the way the boot holds it back: the installation row is marked held back and the release record stays
  ``active``, so a restart finds the same state. An activation still takes no install lock, jobs do not bind a
  release, and only provider workers carry the release key.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

RELEASES_TABLE = "provider_hub_releases"
BINDINGS_TABLE = "provider_hub_release_bindings"

STAGED = "staged"
ACTIVE = "active"
DRAINING = "draining"
RETIRED = "retired"
FAILED = "failed"
RELEASE_STATES = (STAGED, ACTIVE, DRAINING, RETIRED, FAILED)
# States an installation holds one release in at most: the partial unique index
# enforces it, so two writers cannot both promote.
SLOT_STATES = (STAGED, ACTIVE)
# States whose code may still be running in this process.
RUNNING_STATES = (ACTIVE, DRAINING)

# What a release may become. A retired release can come back as the active one
# (a rollback); a failed one is history only.
_TRANSITIONS = {
    STAGED: {ACTIVE, FAILED, RETIRED},
    ACTIVE: {DRAINING, RETIRED},
    DRAINING: {RETIRED, ACTIVE},
    RETIRED: {ACTIVE, STAGED},
    FAILED: set(),
}

# At most this many of each are kept per installation besides the active, staged
# and draining ones: the previous release, and the last failure.
KEEP_RETIRED = 1
KEEP_FAILED = 1

# Set on an installation row by whatever replaces the active release while its
# calls are still being served. ``split`` consumes it: the replaced release goes
# to ``draining`` instead of straight to ``retired``.
DRAIN_PREVIOUS = "drain_previous"
# The rollback carries the exact released row approved by the user.
STAGED_ROLLBACK = "staged_rollback"
PROMOTE_ROLLBACK = "promote_rollback"
# Set on an installation row when a staged release is dropped because it failed to activate, with the reason.
# ``split`` consumes it: that release is kept as failed, or as the previous release again when it came from there.
# A staged release that leaves its slot for any other reason is forgotten, whatever error the row shows.
FAIL_STAGED = "fail_staged"
# The stage's old data key when activation publishes a fresh directory. None marks a keyless stage.
PROMOTE_STAGED_DATA = "promote_staged_data"

UNKNOWN_CATALOG_SOURCE_NOTE = (
    "No configured catalog source is known to have published this version, so it "
    "gets no updates. Reinstall it from a catalog source to get updates again."
)

metadata = sa.MetaData()

releases_table = sa.Table(
    RELEASES_TABLE,
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("plugin_id", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("bundle_path", sa.Text),
    sa.Column("python_path", sa.Text),
    # The content address of the bundle (its manifest's bundle_sha256) and the
    # key of the environment it runs from, when the hub recorded one.
    sa.Column("bundle_digest", sa.Text),
    sa.Column("env_key", sa.Text),
    sa.Column("manifest", sa.Text),
    sa.Column("error", sa.Text),
    # Where the release came from, recorded when it is created: a release of the same id from another source
    # is not the previous release of this one.
    sa.Column("source_id", sa.Text),
    sa.Column("origin", sa.Text),
    # Timestamps as the hub writes them everywhere else: ISO 8601 text.
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("activated_at", sa.Text),
    sa.Column("retired_at", sa.Text),
    # The order releases ended in: a number taken from a counter that only grows, written when a release
    # becomes retired or failed. Ids say when a release was created, and a rollback reuses an id.
    sa.Column("ended_seq", sa.Integer),
    # Which of the row's release fields the installation row carried (a JSON list), so the row reads back with
    # the keys it had.
    sa.Column("fields", sa.Text),
    sa.CheckConstraint("state IN ('staged', 'active', 'draining', 'retired', 'failed')",
                       name="ck_provider_hub_releases_state"),
    sa.Index("ix_provider_hub_releases_plugin_id", "plugin_id"),
    sa.Index("ux_provider_hub_releases_slot", "plugin_id", "state", unique=True,
             sqlite_where=sa.text("state IN ('staged', 'active')"),
             postgresql_where=sa.text("state IN ('staged', 'active')")),
)

bindings_table = sa.Table(
    BINDINGS_TABLE,
    metadata,
    sa.Column("release_id", sa.Integer, sa.ForeignKey(f"{RELEASES_TABLE}.id", ondelete="CASCADE"),
              primary_key=True),
    sa.Column("capability", sa.Text, primary_key=True),
    sa.Column("supplier_id", sa.Text, nullable=False),
)

# The release fields an installation row carries, per slot.
_ACTIVE_KEYS = ("active_version", "active_path", "python_path", "manifest", "supplied_by", "activated_at")
_STAGED_KEYS = ("staged_version", "staged_path", "staged_python_path", "staged_manifest", "staged_supplied_by")
# Recorded in ``fields`` when the row's map of bindings was null and not empty.
NULL_BINDINGS = "supplied_by:null"
# The existing per-release field metadata also carries the opaque data folder key.
_DATA_KEY = "data:"
# A release written now can belong to a catalog that was registered before IDs were recorded.
_CATALOG_REGISTRATION = "catalog_registration:"
_LEGACY_CATALOG_REGISTRATION = "legacy"
_UNVERIFIED_LEGACY_CATALOG_REGISTRATION = "legacy-unverified"
LEGACY_RESTAGED_REGISTRATION = "legacy-restaged"
# What each key after the version is, in order, for the ``fields`` column.
_FIELD_NAMES = ("path", "python_path", "manifest", "supplied_by", "activated_at")


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def can_move(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, ())


class ReleaseTransitionError(ValueError):
    """A release was asked to move to a state it cannot move to from where it is."""


@dataclass(frozen=True)
class Release:
    """One stored release, as the callers that are not installation rows read it."""

    id: int
    plugin_id: str
    version: str
    state: str
    bundle_path: str | None = None
    python_path: str | None = None
    bundle_digest: str | None = None
    env_key: str | None = None
    manifest: dict[str, Any] | None = None
    supplied_by: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    created_at: str | None = None
    activated_at: str | None = None
    retired_at: str | None = None
    ended_seq: int | None = None
    source_id: str | None = None
    origin: str | None = None
    data_key: str | None = None
    catalog_registration: str | None = None


def release_of(row: dict[str, Any]) -> Release:
    return Release(
        id=int(row["id"]), plugin_id=row["plugin_id"], version=row["version"], state=row["state"],
        bundle_path=row.get("bundle_path"), python_path=row.get("python_path"),
        bundle_digest=row.get("bundle_digest"), env_key=row.get("env_key"),
        manifest=_loads(row.get("manifest")), supplied_by=dict(row.get("supplied_by") or {}),
        error=row.get("error"), created_at=row.get("created_at"), activated_at=row.get("activated_at"),
        retired_at=row.get("retired_at"), ended_seq=row.get("ended_seq"), source_id=row.get("source_id"),
        origin=row.get("origin"), data_key=_data_key(row),
        catalog_registration=_field_value(row, _CATALOG_REGISTRATION),
    )


def _field_value(row: dict[str, Any], prefix: str) -> str | None:
    return next((marker[len(prefix):] for marker in json.loads(row.get("fields") or "[]")
                 if isinstance(marker, str) and marker.startswith(prefix)), None)


def _data_key(row: dict[str, Any]) -> str | None:
    return _field_value(row, _DATA_KEY)


def catalog_registration(data: dict[str, Any], source_id: str | None) -> str | None:
    from .state import OFFICIAL_CATALOG_SOURCE_ID

    if source_id == OFFICIAL_CATALOG_SOURCE_ID:
        return None
    source = (data.get("catalog_sources") or {}).get(source_id)
    return (source.get("registration") or _LEGACY_CATALOG_REGISTRATION) if isinstance(source, dict) else None


def matches_catalog_registration(data: dict[str, Any], source_id: str | None, recorded: str | None) -> bool:
    current = catalog_registration(data, source_id)
    return recorded == current or (recorded is None and current == _LEGACY_CATALOG_REGISTRATION)


def legacy_restage_sources_current(data: dict[str, Any], previous_source: str | None,
                                   selected_source: str | None) -> bool:
    """Both registrations used by the historical writer remain, including the official source."""
    from .state import OFFICIAL_CATALOG_SOURCE_ID

    return (catalog_registration(data, previous_source) == _LEGACY_CATALOG_REGISTRATION
            and (selected_source == OFFICIAL_CATALOG_SOURCE_ID
                 or catalog_registration(data, selected_source) == _LEGACY_CATALOG_REGISTRATION))


def _legacy_manifest_provenance_matches(data, source_id, source, plugin_id, version, manifest) -> bool:
    """Use the original URL or a matching entry, with no conflicting current digest."""
    entries = data.get("catalog_entries")
    commit = source.get("resolved_commit")
    declared_url = manifest["source"].get("catalog_url")
    direct = declared_url == source["url"]
    id_key = "id" if manifest.get("schema_version") == 2 else "provider_id"
    if (not isinstance(entries, dict) or not isinstance(commit, str) or not commit
            or manifest.get(id_key) != plugin_id or manifest.get("version") != version
            or manifest["source"].get("commit") != commit):
        return direct

    digest = manifest.get("bundle_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return False
    digest = digest.lower()
    expected_url = declared_url or source["url"]
    installed = {**manifest, "source": {key: value for key, value in manifest["source"].items()
                                        if key not in ("catalog_url", "trusted")}}
    # Schema 2 subtitle installs materialize the schema 1 provider ID for downstream callers.
    if id_key == "id" and installed.get("provider_id") == plugin_id:
        installed.pop("provider_id")
    matched = False
    for entry in entries.values():
        if not (isinstance(entry, dict) and entry.get("source") == source_id
                and entry.get("provider_id") == plugin_id and entry.get("version") == version
                and entry.get("resolved_commit") == commit):
            continue
        cached = entry.get("manifest")
        # Every record for this source and release must agree on the artifact.
        cached_digest = cached.get("bundle_sha256") if isinstance(cached, dict) else None
        if not isinstance(cached_digest, str) or cached_digest.lower() != digest:
            return False
        cached_source = cached.get("source")
        if not isinstance(cached_source, dict) or cached_source.get("catalog_url") != expected_url:
            continue
        comparable = {**cached, "source": {key: value for key, value in cached_source.items()
                                           if key not in ("catalog_url", "trusted")}}
        if id_key == "id" and comparable.get("provider_id") == plugin_id:
            comparable.pop("provider_id")
        if comparable == installed:
            if matched and not direct:
                return False
            matched = True
    return direct or matched


def _legacy_catalog_attested(data, source_id, plugin_id, version, manifest) -> bool:
    sources = data.get("catalog_sources")
    source = sources.get(source_id) if isinstance(sources, dict) else None
    url = source.get("url") if isinstance(source, dict) and not source.get("registration") else None
    manifest_source = manifest.get("source") if isinstance(manifest, dict) else None
    return (isinstance(url, str) and bool(url) and isinstance(manifest_source, dict)
            and _legacy_manifest_provenance_matches(data, source_id, source, plugin_id, version, manifest))


def _legacy_catalog_registrations(data: dict[str, Any], plugin_id: str, row: dict[str, Any],
                                  active: _Slot | None, staged: _Slot | None) -> dict[str, str | None] | None:
    """Resolve each inline community release's binding before its first store write."""
    from .state import OFFICIAL_CATALOG_SOURCE_ID

    source_id = row.get("source_id")
    if (row.get("origin") != "catalog" or not isinstance(source_id, str)
            or source_id == OFFICIAL_CATALOG_SOURCE_ID or (active is None and staged is None)):
        return None
    registrations = {}
    for state, slot in ((ACTIVE, active), (STAGED, staged)):
        if slot is not None:
            matches = row.get("source_bound", True) is True and _legacy_catalog_attested(
                data, source_id, plugin_id, slot.version, slot.manifest)
            registrations[state] = _LEGACY_CATALOG_REGISTRATION if matches else None
    row["source_bound"] = True
    if not registrations.get(STAGED, registrations.get(ACTIVE)):
        row.update(source_id=None, trusted=False, trust_note=UNKNOWN_CATALOG_SOURCE_NOTE)
    return registrations


def _loads(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dumps(manifest: dict[str, Any] | None) -> str | None:
    return None if manifest is None else json.dumps(manifest, sort_keys=True)


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _bindings_ok(value: Any) -> bool:
    return value is None or (isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()))


@dataclass
class _Slot:
    version: str
    bundle_path: str | None
    python_path: str | None
    manifest: dict[str, Any] | None
    supplied_by: dict[str, str]
    activated_at: str | None
    data_key: str | None
    # The names (``_FIELD_NAMES``) of the fields the installation row carried.
    present: list[str]


def _slot(row: dict[str, Any], keys: tuple[str, ...], with_activation: bool) -> _Slot | None:
    """The release an installation row describes in one slot, or None.

    A slot is a release only when it is well formed: a version, and values of the
    types the tables hold. Anything else is left in the document untouched, so a
    row an older build or a test wrote in an odd shape is kept as it is.
    """
    version = row.get(keys[0])
    if not isinstance(version, str) or not version:
        return None
    path, python, manifest, supplied = (row.get(key) for key in keys[1:5])
    activated = row.get(keys[5]) if with_activation else None
    data_key = row.get("active_data_key" if with_activation else "staged_data_key")
    if not (_optional_text(data_key) and _optional_text(path) and _optional_text(python) and _optional_text(activated)
            and (manifest is None or isinstance(manifest, dict)) and _bindings_ok(supplied)):
        return None
    names = _FIELD_NAMES if with_activation else _FIELD_NAMES[:4]
    present = [name for name, key in zip(names, keys[1:]) if key in row]
    if "supplied_by" in present and supplied is None:
        # A null map and an empty one are different values of the row: the null is remembered.
        present.append(NULL_BINDINGS)
    if data_key:
        present.append(_DATA_KEY + data_key)
    return _Slot(version, path, python, manifest, dict(supplied or {}), activated, data_key, present)


@functools.lru_cache(maxsize=1024)
def _resolved(path: str) -> str:
    from .env_registry import path_key
    return path_key(path)


def _env_key(data: dict[str, Any], python_path: str | None) -> str | None:
    """The fingerprint the environment of ``python_path`` is recorded under, when the registry knows it."""
    envs = data.get("plugin_envs")
    if not python_path or not isinstance(envs, dict) or not envs:
        return None
    try:
        from pathlib import Path
        wanted = _resolved(str(Path(python_path).parent.parent))
        for fingerprint, entry in envs.items():
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and _resolved(entry["path"]) == wanted:
                return str(fingerprint)
    except Exception:
        return None
    return None


def _digest(manifest: dict[str, Any] | None) -> str | None:
    value = manifest.get("bundle_sha256") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class SlotIdentity:
    id: int
    created_at: str
    source_id: str | None
    origin: str | None
    bundle_digest: str | None
    catalog_registration: str | None = None


class StateDocument(dict):
    """The composed state with release metadata that is never stored in the document or state.json."""

    retained_releases: tuple[dict[str, Any], ...] = ()
    adopting_staged_data: tuple[str, str | None] | None = None
    staging_install: str | None = None
    promoting_stage: str | None = None

    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.release_slots: dict[str, dict[str, SlotIdentity]] = {}
        self.staged_releases: dict[str, dict[str, Any]] = {}
        self.inline_releases: set[str] = set()


def compose(data: dict[str, Any], rows: list[dict[str, Any]]) -> "StateDocument":
    """Put the active and staged release of each installation back into its row, in place.

    ``rows`` are release rows as ``read_rows`` returns them (the retired and draining ones may come without
    their manifest). An installation whose releases are not in the tables yet (a document written before they
    existed) keeps the fields it has: they are moved out at its next write. Returns the document, with the
    releases kept besides the active and staged ones attached.
    """
    document = StateDocument(data)
    installations = data.get("installations")
    listed = installations if isinstance(installations, dict) else {}
    # The previous and draining releases, and every release of a plugin the document does not list (what a
    # damaged document lost): they are kept until discarded on purpose, and so are their files.
    document.retained_releases = tuple(
        {**{key: release.get(key) for key in (
            "id", "plugin_id", "version", "state", "bundle_path", "python_path", "bundle_digest", "env_key",
            "created_at", "ended_seq", "source_id", "origin", "fields")},
         "data_key": _data_key(release)}
        for release in rows
        if release["state"] in (RETIRED, DRAINING) or release["plugin_id"] not in listed)
    if not isinstance(installations, dict):
        return document
    by_plugin: dict[str, dict[str, dict[str, Any]]] = {}
    for release in rows:
        if release["state"] in SLOT_STATES:
            by_plugin.setdefault(release["plugin_id"], {})[release["state"]] = release
        if release["state"] == STAGED:
            document.staged_releases[release["plugin_id"]] = release
    for plugin_id, installation in installations.items():
        if not isinstance(installation, dict):
            continue
        slots = by_plugin.get(plugin_id)
        if not slots:
            if installation.get("active_version") or installation.get("staged_version"):
                document.inline_releases.add(plugin_id)
            continue
        document.release_slots[plugin_id] = {
            state: SlotIdentity(release["id"], release["created_at"], release.get("source_id"),
                                release.get("origin"), release.get("bundle_digest"),
                                _field_value(release, _CATALOG_REGISTRATION))
            for state, release in slots.items()
        }
        for state, keys in ((ACTIVE, _ACTIVE_KEYS), (STAGED, _STAGED_KEYS)):
            release = slots.get(state)
            if release is None:
                continue
            installation[keys[0]] = release["version"]
            data_field = "active_data_key" if state == ACTIVE else "staged_data_key"
            if data_key := _data_key(release):
                installation[data_field] = data_key
            else:
                installation.pop(data_field, None)
            recorded = release.get("fields")
            names = _FIELD_NAMES if state == ACTIVE else _FIELD_NAMES[:4]
            present = set(json.loads(recorded)) if recorded else set(names)
            for name, key in zip(names, keys[1:]):
                if name not in present:
                    installation.pop(key, None)
                elif name == "path":
                    installation[key] = release.get("bundle_path")
                elif name == "python_path":
                    installation[key] = release.get("python_path")
                elif name == "manifest":
                    installation[key] = _loads(release.get("manifest"))
                elif name == "supplied_by":
                    installation[key] = None if NULL_BINDINGS in present else dict(release.get("supplied_by") or {})
                elif name == "activated_at":
                    installation[key] = release.get("activated_at")
    return document


@dataclass
class Plan:
    """What a write does to the release tables, worked out from the document it stores."""

    deletes: list[int] = field(default_factory=list)
    # (id, changed columns, bindings or None when they did not change)
    updates: list[tuple[int, dict[str, Any], dict[str, str] | None]] = field(default_factory=list)
    inserts: list[tuple[dict[str, Any], dict[str, str]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.deletes or self.updates or self.inserts)


def backfill_legacy_registrations(data: dict[str, Any], rows: list[dict[str, Any]]) -> Plan:
    """Record ownership once for releases already stored by pre-registration development hosts."""
    from .state import OFFICIAL_CATALOG_SOURCE_ID

    plan = Plan()
    installations = data.get("installations")
    if not isinstance(installations, dict):
        return plan
    selected = {row["plugin_id"]: row for row in rows if row["state"] == ACTIVE}
    selected.update((row["plugin_id"], row) for row in rows if row["state"] == STAGED)
    for release in rows:
        plugin_id, source_id = release["plugin_id"], release.get("source_id")
        if (release.get("origin") != "catalog" or not source_id or source_id == OFFICIAL_CATALOG_SOURCE_ID
                or _field_value(release, _CATALOG_REGISTRATION) is not None):
            continue
        row = installations.get(plugin_id)
        if not isinstance(row, dict):
            continue
        is_selected = selected.get(plugin_id) is release
        bound = (not is_selected or (row.get("source_id") == source_id and row.get("source_bound", True) is True))
        attested = bound and _legacy_catalog_attested(
            data, source_id, plugin_id, release["version"], _loads(release.get("manifest")))
        # Keep the old source in the release history, including a stage that must now be refused.
        # An explicit nonmatching registration makes that decision stable across later refreshes.
        registration = (_LEGACY_CATALOG_REGISTRATION if attested else _UNVERIFIED_LEGACY_CATALOG_REGISTRATION)
        if (is_selected and release["state"] == STAGED and _data_key(release) is None
                and row.get("source_bound", True) is True and row.get("source_id") != source_id
                and legacy_restage_sources_current(data, source_id, row.get("source_id"))):
            # The old writer changed the selected source without changing the release's original source.
            # This stage can proceed only after the new catalog's entry and actual bundle are verified.
            registration = LEGACY_RESTAGED_REGISTRATION
        if is_selected and not attested and row.get("source_id") == source_id:
            row.update(source_bound=True, source_id=None, trusted=False, trust_note=UNKNOWN_CATALOG_SOURCE_NOTE)
        raw = release.get("fields")
        fields = json.loads(raw) if raw else list(_FIELD_NAMES if release["state"] != STAGED else _FIELD_NAMES[:4])
        fields.append(_CATALOG_REGISTRATION + registration)
        plan.updates.append((release["id"], {"fields": json.dumps(fields)}, None))
    return plan


def _matches(release: dict[str, Any], slot: _Slot) -> bool:
    return (release["version"] == slot.version and release.get("bundle_path") == slot.bundle_path
            and _data_key(release) == slot.data_key)


def split(data: dict[str, Any], rows: list[dict[str, Any]], *, now: str | None = None,
          previously_listed: "set[str] | None" = None) -> tuple[dict[str, Any], Plan]:
    """The document to store, and the release-table changes that go with it.

    ``data`` is the full dict a caller works with. The plan consumes transient marks
    from its rows and records any legacy source detachment for the state.json copy.
    The returned document is a copy without the release fields of the rows whose
    slots became releases. ``rows`` are the stored releases at the moment of the
    write, with their bindings, and with ``manifest`` for the ones in a slot.
    ``previously_listed`` is the set of plugin ids the document listed before this write. Only a plugin that was
    listed and no longer is has its releases deleted: a release whose plugin the document never listed (what a
    document rebuilt after damage lost) is an orphan, and stays until ``state_store.discard_orphan_releases``
    is asked for it. Without it every release of a plugin the document does not list is deleted.
    """
    stamp = now or now_text()
    stored = dict(data)
    plan = Plan()
    installations = data.get("installations")
    by_plugin: dict[str, list[dict[str, Any]]] = {}
    for release in rows:
        by_plugin.setdefault(release["plugin_id"], []).append(release)
    if not isinstance(installations, dict):
        # Not a document this module understands: leave the tables as they are.
        return stored, plan
    counter = [max((release.get("ended_seq") or 0 for release in rows), default=0)]

    def next_seq() -> int:
        counter[0] += 1
        return counter[0]

    stored["installations"] = stored_rows = {}
    for plugin_id, installation in installations.items():
        if not isinstance(installation, dict):
            stored_rows[plugin_id] = installation
            continue
        drain = bool(installation.pop(DRAIN_PREVIOUS, False))
        failure = installation.pop(FAIL_STAGED, None)
        promote_rollback = installation.pop(PROMOTE_ROLLBACK, None)
        promote_staged = PROMOTE_STAGED_DATA in installation
        staged_data = installation.pop(PROMOTE_STAGED_DATA, None)
        active = _slot(installation, _ACTIVE_KEYS, True)
        staged = _slot(installation, _STAGED_KEYS, False)
        inline_source = installation.get("source_id")
        legacy_registrations = None
        if plugin_id in getattr(data, "inline_releases", ()) and getattr(data, "staging_install", None) != plugin_id:
            legacy_registrations = _legacy_catalog_registrations(data, plugin_id, installation, active, staged)
        row = dict(installation)
        existing = by_plugin.get(plugin_id, [])
        claimed: set[int] = set()

        def find(slot: _Slot, states: tuple[str, ...], *,
                 for_install: bool = False, release_id: int | None = None) -> dict[str, Any] | None:
            for state in states:
                for release in existing:
                    if (release["id"] not in claimed and release["state"] == state and _matches(release, slot)
                            and (release_id is None or release["id"] == release_id)):
                        if for_install and (
                                (release.get("source_id"), release.get("origin"))
                                != (row.get("source_id"), row.get("origin"))
                                or _field_value(release, _CATALOG_REGISTRATION)
                                != catalog_registration(data, row.get("source_id"))):
                            continue
                        return release
            return None

        def settle(slot: _Slot, release: dict[str, Any] | None, state: str, *, source_id: str | None = None) -> None:
            unchanged_env = release is not None and release.get("python_path") == slot.python_path
            values = {
                "state": state, "bundle_path": slot.bundle_path, "python_path": slot.python_path,
                "manifest": _dumps(slot.manifest), "bundle_digest": _digest(slot.manifest),
                "env_key": (release.get("env_key") if unchanged_env and release.get("env_key")
                            else _env_key(data, slot.python_path)),
                "error": None,
            }
            registration = None
            if release is None and legacy_registrations is not None:
                registration = legacy_registrations.get(state)
            elif source_id is not None or (release is None and row.get("origin") == "catalog"):
                registration = catalog_registration(data, source_id if source_id is not None else row.get("source_id"))
            elif release is not None:
                registration = _field_value(release, _CATALOG_REGISTRATION)
            fields = slot.present + ([_CATALOG_REGISTRATION + registration] if registration else [])
            values["fields"] = json.dumps(fields)
            if state == ACTIVE:
                values["activated_at"] = slot.activated_at
                values["retired_at"] = None
            if source_id is not None:
                values["source_id"] = source_id
            if release is None:
                source, origin = row.get("source_id"), row.get("origin")
                if legacy_registrations is not None:
                    # Keep an unverified stage's former source so activation can discard it.
                    source = inline_source if registration or state == STAGED else None
                values.update(plugin_id=plugin_id, version=slot.version, created_at=stamp,
                              source_id=source if isinstance(source, str) else None,
                              origin=origin if isinstance(origin, str) else None)
                plan.inserts.append((values, dict(slot.supplied_by)))
                return
            claimed.add(release["id"])
            changed = {key: value for key, value in values.items() if release.get(key) != value}
            bindings = slot.supplied_by if (release.get("supplied_by") or {}) != slot.supplied_by else None
            if changed or bindings is not None:
                plan.updates.append((release["id"], changed, None if bindings is None else dict(bindings)))

        if staged is not None:
            adoption = getattr(data, "adopting_staged_data", None)
            rebound_source = None
            if adoption is not None and adoption[0] == plugin_id:
                rebound_source = adoption[1]
                # Adopt only into the existing staged slot. A retired release with the same code is unrelated.
                staged_release = next((release for release in existing if release["state"] == STAGED), None)
                source_id = row.get("source_id")
                if staged_release is not None and (
                        _data_key(staged_release) is not None or staged_release["version"] != staged.version
                        or staged_release.get("bundle_path") != staged.bundle_path
                        or staged_release.get("python_path") != staged.python_path
                        or _loads(staged_release.get("manifest")) != staged.manifest
                        or (staged_release.get("source_id") != source_id and rebound_source != source_id)
                        or staged_release.get("origin") != row.get("origin")):
                    raise ReleaseTransitionError("the staged release changed before its data key could be recorded")
            else:
                rollback = row.get(STAGED_ROLLBACK)
                rollback_id = rollback.get("release_id") if isinstance(rollback, dict) else None
                for_install = getattr(data, "staging_install", None) == plugin_id
                def same_contents(release):
                    return (release.get("python_path") == staged.python_path
                            and release.get("manifest") == _dumps(staged.manifest))

                staged_release = find(staged, (STAGED,), for_install=for_install)
                if (staged_release is not None and staged_release.get("retired_at")
                        and not same_contents(staged_release)):
                    # A replacement must leave the protected rollback row intact.
                    staged_release = None
                if staged_release is None:
                    retained = find(staged, (RETIRED,), for_install=for_install, release_id=rollback_id)
                    if retained is not None and (rollback_id is None or same_contents(retained)):
                        staged_release = retained
            settle(staged, staged_release, STAGED, source_id=rebound_source)
        if active is not None:
            if promote_rollback is not None:
                active_release = find(active, (STAGED,), release_id=promote_rollback)
                if active_release is None:
                    raise ReleaseTransitionError("the confirmed rollback release is no longer staged")
            elif promote_staged and staged is None:
                # Match the saved stage using its old key and write the active slot with the new key.
                active_release = find(replace(active, data_key=staged_data), (STAGED,))
            else:
                states = ((STAGED, ACTIVE, DRAINING, RETIRED) if getattr(data, "promoting_stage", None) == plugin_id
                          else (ACTIVE, STAGED, DRAINING, RETIRED))
                active_release = find(active, states)
            settle(active, active_release, ACTIVE)
        # What the slots no longer name.
        for release in existing:
            if release["id"] in claimed:
                continue
            state = release["state"]
            if state == ACTIVE:
                if drain:
                    plan.updates.append((release["id"], {"state": DRAINING}, None))
                else:
                    plan.updates.append((release["id"], {
                        "state": RETIRED, "retired_at": stamp, "ended_seq": next_seq()}, None))
            elif state == STAGED:
                if release.get("retired_at"):
                    # It was the previous release, staged again to go back to. It stays the previous release,
                    # with the reason it did not come back when there is one.
                    plan.updates.append((release["id"], {
                        "state": RETIRED, "error": failure, "ended_seq": next_seq()}, None))
                elif staged is None and failure:
                    plan.updates.append((release["id"], {
                        "state": FAILED, "error": failure, "ended_seq": next_seq()}, None))
                else:
                    plan.deletes.append(release["id"])
        # The slots that did not become releases keep their fields in the document.
        if active is not None:
            row.pop("active_data_key", None)
            for key in _ACTIVE_KEYS:
                row.pop(key, None)
        if staged is not None:
            row.pop("staged_data_key", None)
            for key in _STAGED_KEYS:
                row.pop(key, None)
        stored_rows[plugin_id] = row
    for plugin_id, existing in by_plugin.items():
        if plugin_id not in installations and (previously_listed is None or plugin_id in previously_listed):
            plan.deletes.extend(release["id"] for release in existing)
    _prune(plan, by_plugin, installations)
    return stored, plan


def _ended(release: dict[str, Any], changes: dict[str, Any] | None) -> tuple[int, int]:
    """Where a release stands in the order releases ended in: newest sorts last."""
    seq = (changes or {}).get("ended_seq", release.get("ended_seq")) or 0
    return seq, release["id"]


def _prune(plan: Plan, by_plugin: dict[str, list[dict[str, Any]]], installations: dict[str, Any]) -> None:
    """Drop the retired and failed releases past what is kept, the ones that ended first."""
    changes = {release_id: changed for release_id, changed, _bindings in plan.updates}
    deleted = set(plan.deletes)
    for plugin_id, existing in by_plugin.items():
        if plugin_id not in installations:
            continue
        for state, keep in ((RETIRED, KEEP_RETIRED), (FAILED, KEEP_FAILED)):
            held = [release for release in existing if release["id"] not in deleted
                    and changes.get(release["id"], {}).get("state", release["state"]) == state]
            for release in sorted(held, key=lambda item: _ended(item, changes.get(item["id"])), reverse=True)[keep:]:
                plan.deletes.append(release["id"])
                deleted.add(release["id"])


# ---- database access ------------------------------------------------------------------------------------------


def create_tables(bind) -> None:
    """Create the release tables when missing, tolerating a second process creating them first."""
    metadata.create_all(bind, checkfirst=True)


def read_rows(connection, *, states: tuple[str, ...] | None = None, manifests: tuple[str, ...] | None = None,
              plugin_id: str | None = None) -> list[dict[str, Any]]:
    """The stored releases, oldest first, each with its bindings.

    ``manifests`` names the states whose manifest text is loaded; the others come
    without the key, which is what keeps a read of the retired and failed history
    cheap.
    """
    columns = [column for column in releases_table.c if column.name != "manifest"]
    wanted = manifests if manifests is not None else (ACTIVE, STAGED)
    query = sa.select(*columns, sa.case(
        (releases_table.c.state.in_(wanted), releases_table.c.manifest), else_=None).label("manifest"))
    if states is not None:
        query = query.where(releases_table.c.state.in_(states))
    if plugin_id is not None:
        query = query.where(releases_table.c.plugin_id == plugin_id)
    rows = [dict(row._mapping) for row in connection.execute(query.order_by(releases_table.c.id))]
    for row in rows:
        row["supplied_by"] = {}
        if row["state"] not in wanted:
            row.pop("manifest", None)
    if rows:
        ids = [row["id"] for row in rows]
        by_id = {row["id"]: row for row in rows}
        for release_id, capability, supplier in connection.execute(
                sa.select(bindings_table.c.release_id, bindings_table.c.capability, bindings_table.c.supplier_id)
                .where(bindings_table.c.release_id.in_(ids))):
            by_id[release_id]["supplied_by"][capability] = supplier
    return rows


def apply_plan(connection, plan: Plan) -> None:
    """Run a plan inside the caller's transaction.

    Releases that leave a slot state go first, so a promotion never meets the
    release it replaces in the partial unique index.
    """
    if not plan:
        return
    if plan.deletes:
        connection.execute(sa.delete(bindings_table).where(bindings_table.c.release_id.in_(plan.deletes)))
        connection.execute(sa.delete(releases_table).where(releases_table.c.id.in_(plan.deletes)))

    def vacates(changes: dict[str, Any]) -> bool:
        return "state" in changes and changes["state"] not in SLOT_STATES

    ordered = [item for item in plan.updates if vacates(item[1])] + [
        item for item in plan.updates if not vacates(item[1])]
    for release_id, changes, bindings in ordered:
        if changes:
            connection.execute(sa.update(releases_table).where(releases_table.c.id == release_id).values(**changes))
        if bindings is not None:
            _write_bindings(connection, release_id, bindings)
    for values, bindings in plan.inserts:
        result = connection.execute(sa.insert(releases_table).values(**values))
        _write_bindings(connection, int(result.inserted_primary_key[0]), bindings)


def _write_bindings(connection, release_id: int, bindings: dict[str, str]) -> None:
    connection.execute(sa.delete(bindings_table).where(bindings_table.c.release_id == release_id))
    if bindings:
        connection.execute(sa.insert(bindings_table), [
            {"release_id": release_id, "capability": capability, "supplier_id": supplier}
            for capability, supplier in sorted(bindings.items())])


def _next_seq(connection) -> int:
    return (connection.execute(sa.select(sa.func.max(releases_table.c.ended_seq))).scalar() or 0) + 1


def set_state(connection, release_id: int, target: str, *, error: str | None = None) -> bool:
    """Move one release to ``target`` if its current state allows it. Returns whether a row changed.

    Raises ``ReleaseTransitionError`` for a move the model forbids. A release that is no longer there answers False.
    """
    current = connection.execute(
        sa.select(releases_table.c.plugin_id, releases_table.c.state).where(releases_table.c.id == release_id)).first()
    if current is None:
        return False
    if current.state == target:
        return False
    if not can_move(current.state, target):
        raise ReleaseTransitionError(f"a {current.state} release cannot become {target}")
    values: dict[str, Any] = {"state": target}
    if target == RETIRED:
        values.update(retired_at=now_text(), ended_seq=_next_seq(connection))
    if target == ACTIVE:
        values["retired_at"] = None
    if target == FAILED:
        values.update(error=error, ended_seq=_next_seq(connection))
    if target in SLOT_STATES:
        # The slot is taken by whatever holds it now: it leaves first.
        connection.execute(
            sa.update(releases_table)
            .where(releases_table.c.plugin_id == current.plugin_id, releases_table.c.state == target,
                   releases_table.c.id != release_id)
            .values(state=RETIRED, retired_at=now_text(), ended_seq=_next_seq(connection)))
    connection.execute(sa.update(releases_table).where(releases_table.c.id == release_id).values(**values))
    return True

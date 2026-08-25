# coding=utf-8
"""Resolve the owning arr_instance for runtime sync writes (#156).

During an instance-aware sync the orchestrators stamp every media row with its
owning ``arr_instance_id``. For the default (single-instance) path the owner is
the enabled default instance: a freshly-upgraded install has exactly one,
created by the backfill, and every pre-existing row already carries its id.

``default_instance_id`` returns ``None`` when no default exists yet (a
pre-backfill install). Callers MUST then leave ``arr_instance_id`` unset rather
than writing a literal ``None`` - a literal None masquerades as an explicit
"unowned" marker and would corrupt the eventual NOT NULL / instance-scoped-query
invariants. ``stamp_owner`` enforces that rule in one place.

The session is passed in by the caller (the sync orchestrators already hold the
scoped ``database`` session) so these helpers never reach for a global and stay
trivially testable against an in-memory session.
"""
import logging

from sqlalchemy import select

from app.database import TableShows

from .repository import ArrInstanceRepository


def default_instance_id(session, kind):
    """Return the enabled default instance id for ``kind``, or None.

    None signals a pre-backfill install with no default instance; the caller
    must leave ``arr_instance_id`` unset in that case (legacy behaviour).
    """
    row = ArrInstanceRepository(session).get_default(kind)
    return row.id if row is not None else None


def scoped(stmt, column, arr_instance_id):
    """Add ``column == arr_instance_id`` to a select/update/delete when the sync
    is instance-scoped; a no-op for the default path (``arr_instance_id`` None)
    so the statement stays byte-identical to the legacy unscoped query.

    This is the guard against the cross-instance hazard: in multi-instance the
    upstream ids (sonarrSeriesId/radarrId) are no longer globally unique, so an
    unscoped WHERE on the upstream id would update or delete a DIFFERENT
    instance's row that happens to share that id. Scoping confines every
    read/delete/update to the instance being synced.
    """
    return stmt if arr_instance_id is None else stmt.where(column == arr_instance_id)


def stamp_owner(row, instance_id):
    """Set ``arr_instance_id`` on a parser-output dict, guarded.

    Writes the key only when ``instance_id`` is not None, so a pre-backfill
    install leaves the column NULL (the exact legacy behaviour) instead of
    persisting a None that looks like an explicit owner. Mutates and returns
    ``row`` for call-site convenience.
    """
    if instance_id is not None:
        row["arr_instance_id"] = instance_id
    return row


def client_for_instance(session, instance_id, http_get=None, enabled_only=True):
    """Build an :class:`ArrClient` for a saved instance (key decrypted at the
    repository boundary), or None when the instance no longer exists. The INC7
    fan-out + webhook/signalr entry points use this to route HTTP at one
    specific instance.

    ``enabled_only`` (default True) returns None for a DISABLED instance so the
    sync entry points honour their "skip disabled" contract instead of silently
    syncing it. The pre-save connection test builds its client elsewhere
    (``from_params``) and is unaffected; pass ``enabled_only=False`` to reach a
    disabled instance deliberately.
    """
    from .client import ArrClientFactory
    from .repository import ArrInstanceRepository

    repo = ArrInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return None
    if enabled_only and not row.enabled:
        return None
    return ArrClientFactory(repo).from_row(row, http_get=http_get)


def sonarr_series_owner(session, sonarr_series_id, arr_instance_id=None):
    """Return ``(owner_instance_id, local_series_id)`` for a series.

    Episodes inherit their owner from their parent series rather than
    re-resolving the default, so a non-default series' episodes are owned by the
    right instance. ``owner_instance_id`` falls back to the default when the
    parent row exists but is not yet stamped (a transient pre-INC4 row).
    ``local_series_id`` is the parent's canonical ``id`` (None when the parent
    is not in the DB yet, e.g. an episode event arriving before its series).

    When ``arr_instance_id`` is given the parent lookup is scoped to that
    instance, so a colliding upstream id under another instance is never
    mistaken for this instance's parent series.
    """
    stmt = scoped(
        select(TableShows.arr_instance_id, TableShows.id)
        .where(TableShows.sonarrSeriesId == sonarr_series_id),
        TableShows.arr_instance_id, arr_instance_id)
    parent = session.execute(stmt).first()
    if parent is None:
        return default_instance_id(session, "sonarr"), None
    instance_id = parent.arr_instance_id
    if instance_id is None:
        instance_id = default_instance_id(session, "sonarr")
    return instance_id, parent.id


# --- Per-instance subtitle settings resolution (#227) ----------------------
# instance_id -> parsed subtitle_settings blob. Populated lazily and cleared by
# service.refresh_runtime on any instance create/update/delete (there is no
# repository update event to subscribe to), so an edited override never keeps
# serving a stale value until restart.
_subtitle_settings_cache = {}


def clear_subtitle_settings_cache(arr_instance_id=None):
    """Drop the cached subtitle_settings for one instance, or all of them."""
    if arr_instance_id is None:
        _subtitle_settings_cache.clear()
    else:
        _subtitle_settings_cache.pop(arr_instance_id, None)


def _instance_subtitle_settings(arr_instance_id, session=None):
    if arr_instance_id in _subtitle_settings_cache:
        return _subtitle_settings_cache[arr_instance_id]
    if session is None:
        from app.database import database
        session = database
    from app.database import TableArrInstances
    from .subtitle_settings import read_subtitle_settings
    row = session.execute(
        select(TableArrInstances.options).where(TableArrInstances.id == arr_instance_id)
    ).first()
    blob = read_subtitle_settings(row.options if row else None)
    _subtitle_settings_cache[arr_instance_id] = blob
    return blob


def resolve_subtitle_setting(arr_instance_id, dotted_key, global_default, session=None):
    """Return the per-instance override for ``dotted_key`` ("<section>.<key>"),
    else ``global_default``.

    A missing instance context (``arr_instance_id`` None) returns the global
    value unconditionally, so existing single-instance / default call sites are
    unaffected until an override is set. A real instance id (including the
    default) resolves its overrides. ``dotted_key`` maps directly to
    ``subtitle_settings[<section>][<key>]`` in the instance options blob.
    """
    if arr_instance_id is None:
        return global_default
    section, _, key = dotted_key.partition(".")
    blob = _instance_subtitle_settings(arr_instance_id, session=session)
    section_blob = blob.get(section)
    if isinstance(section_blob, dict) and key in section_blob:
        return section_blob[key]
    return global_default


# --- Per-instance default language profile resolution ----------------------
# instance_id -> parsed media_defaults blob. Same lifecycle as the
# subtitle_settings cache above: populated lazily, cleared by
# service.refresh_runtime on any instance create/update/delete so an edited
# override never keeps serving a stale value until restart.
_media_defaults_cache = {}


def clear_media_defaults_cache(arr_instance_id=None):
    """Drop the cached media_defaults for one instance, or all of them."""
    if arr_instance_id is None:
        _media_defaults_cache.clear()
    else:
        _media_defaults_cache.pop(arr_instance_id, None)


def _instance_media_defaults(arr_instance_id, session=None):
    if arr_instance_id in _media_defaults_cache:
        return _media_defaults_cache[arr_instance_id]
    if session is None:
        from app.database import database
        session = database
    from app.database import TableArrInstances
    from .media_defaults import read_media_defaults
    row = session.execute(
        select(TableArrInstances.options).where(TableArrInstances.id == arr_instance_id)
    ).first()
    blob = read_media_defaults(row.options if row else None)
    _media_defaults_cache[arr_instance_id] = blob
    return blob


def _language_profile_exists(profile_id, session=None):
    from app.database import TableLanguagesProfiles
    if session is None:
        from app.database import database
        session = database
    return session.execute(
        select(TableLanguagesProfiles.profileId)
        .where(TableLanguagesProfiles.profileId == profile_id)
    ).first() is not None


def resolve_default_profile(arr_instance_id, global_enabled, global_profile, session=None):
    """Return the language profile id to stamp on media a sync INSERTS, or None.

    ``global_enabled``/``global_profile`` are the raw
    ``settings.general.<noun>_default_enabled`` / ``_default_profile`` pair; the
    same reduction the sync sites did inline is applied here.

    The instance override is consulted ONLY when the instance actually has one.
    An instance with no ``media_defaults`` block, a missing instance context
    (``arr_instance_id`` None, i.e. a pre-backfill install), or an unparseable
    options blob all return the global value unchanged. That conditionality is
    the whole point: resolving an override where none is set would move profile
    assignment for every single-instance install on its next sync.

    A stored override naming a profile that no longer exists (the profile was
    deleted after the override was saved; the API rejects unknown ids at write
    time) falls back to the global default rather than writing a dangling
    ``profileId``. The existence check costs one indexed lookup and runs only
    when an override is actually set.
    """
    from .media_defaults import global_default_profile, instance_default_profile

    global_value = global_default_profile(global_enabled, global_profile)
    if arr_instance_id is None:
        return global_value

    has_override, profile = instance_default_profile(
        _instance_media_defaults(arr_instance_id, session=session))
    if not has_override:
        return global_value
    if profile is not None and not _language_profile_exists(profile, session=session):
        logging.warning(
            "BAZARR arr instance %s has a default language profile (%s) that no longer "
            "exists; falling back to the global default.", arr_instance_id, profile)
        return global_value
    return profile

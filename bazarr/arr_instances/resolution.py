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


def sonarr_series_owner(session, sonarr_series_id):
    """Return ``(owner_instance_id, local_series_id)`` for a series.

    Episodes inherit their owner from their parent series rather than
    re-resolving the default, so a non-default series' episodes are owned by the
    right instance. ``owner_instance_id`` falls back to the default when the
    parent row exists but is not yet stamped (a transient pre-INC4 row).
    ``local_series_id`` is the parent's canonical ``id`` (None when the parent
    is not in the DB yet, e.g. an episode event arriving before its series).
    """
    parent = session.execute(
        select(TableShows.arr_instance_id, TableShows.id)
        .where(TableShows.sonarrSeriesId == sonarr_series_id)).first()
    if parent is None:
        return default_instance_id(session, "sonarr"), None
    instance_id = parent.arr_instance_id
    if instance_id is None:
        instance_id = default_instance_id(session, "sonarr")
    return instance_id, parent.id

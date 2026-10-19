# coding=utf-8
"""Embedded subtitle tracks the user hid, one media row at a time.

Hiding never touches the media file. It records the track's language key
against the row, and the indexers skip a recorded key the next time they read
the file's tracks. The track then stops satisfying its language, so the
missing-subtitle computation, which reads only what the indexer stored, treats
the language as wanted again. Unhiding deletes the record and the next index
lists the track again.
"""

import logging
import re
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import aliased

from app.database import TableEmbeddedTrackHidden, TableEpisodes, TableMovies, TableSportsEvents
from subtitles.indexer.utils import normalize_subtitle_language_variant
from utilities.sql_limits import in_chunks

logger = logging.getLogger(__name__)

# Stored where a movie or episode row has no owning instance, instead of NULL:
# the unique index is what makes hiding idempotent, and NULL never equals NULL.
# 0 is not a real instance id, they are assigned from 1.
UNOWNED = 0
MEDIA_TYPES = ('movie', 'episode', 'sports')

_LANGUAGE_CODE = re.compile(r'^[a-z]{2,3}$')


def owner_key(arr_instance_id):
    return UNOWNED if arr_instance_id is None else int(arr_instance_id)


def applicable_owners(arr_instance_id):
    """The stored owners whose records apply to a row with this owner.

    Its own, and the sentinel: a row the startup backfill has not stamped yet
    has no owner, so its hide is stored under UNOWNED, and it must keep
    applying (and stay removable) once the backfill stamps the row. A record
    under any other owner belongs to a deleted row whose local id was reused.
    """
    return sorted({owner_key(arr_instance_id), UNOWNED})


def language_key(code2, hi=False, forced=False):
    """The key the indexer stores for an embedded track with these flags.

    Built with the indexer's own normaliser, so ``en`` with both flags is
    ``en:hi:forced`` here exactly as it is in the stored listing. None when
    ``code2`` is not a language code.
    """
    code2 = (code2 or '').strip().lower()
    if not _LANGUAGE_CODE.match(code2):
        return None
    return normalize_subtitle_language_variant(code2, forced=bool(forced), hi=bool(hi))


def hidden_languages(session, media_type, media_ids, arr_instance_id):
    """Language keys hidden on any of ``media_ids`` for this owner (see
    ``applicable_owners``).

    Several episode rows can share one file and the indexer writes one listing
    to all of them, so the caller passes every row sharing the file and a track
    hidden on any of them is skipped for the file. Never raises: a failed
    lookup lists the track, which is what the user saw before hiding it.
    """
    ids = [media_id for media_id in media_ids or [] if media_id is not None]
    if not ids:
        return set()
    try:
        found = set()
        for chunk in in_chunks(ids):
            found.update(session.execute(
                select(TableEmbeddedTrackHidden.language)
                .where(TableEmbeddedTrackHidden.media_type == media_type,
                       TableEmbeddedTrackHidden.media_id.in_(chunk),
                       TableEmbeddedTrackHidden.arr_instance_id.in_(applicable_owners(arr_instance_id)))
            ).scalars().all())
        return found
    except Exception:
        logger.exception('BAZARR could not read the hidden embedded tracks of %s %s', media_type, ids)
        return set()


def _insert_ignoring_duplicates(session):
    # Each dialect spells ON CONFLICT DO NOTHING through its own construct.
    # One statement, so it is atomic on the AUTOCOMMIT engine without a
    # transaction or a SAVEPOINT.
    if session.get_bind().dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(TableEmbeddedTrackHidden)


def hide_track(session, media_type, media_id, arr_instance_id, language, codec=None):
    """Record the track as hidden. True when this call recorded it, False when
    it was already hidden."""
    if media_type not in MEDIA_TYPES:
        raise ValueError(f'Unknown media type: {media_type}')
    statement = _insert_ignoring_duplicates(session).values(
        media_type=media_type,
        media_id=int(media_id),
        arr_instance_id=owner_key(arr_instance_id),
        language=language,
        codec=codec,
        hidden_at=datetime.now(),
    ).on_conflict_do_nothing()
    if session.get_bind().dialect.insert_returning:
        # Ask for the new id and count what comes back. The rowcount cannot
        # answer here: SQLAlchemy adds an implicit RETURNING to fetch the id,
        # and psycopg 3 then reports -1 whether the row went in or not.
        return session.execute(statement.returning(TableEmbeddedTrackHidden.id)).first() is not None
    # SQLite before 3.35 has no RETURNING, and its rowcount is exact.
    return bool(session.execute(statement).rowcount)


def unhide_track(session, media_type, media_ids, arr_instance_id, language):
    """Forget the record on every row in ``media_ids``, under this owner or
    the sentinel (see ``applicable_owners``). Returns how many went."""
    removed = 0
    for chunk in in_chunks([media_id for media_id in media_ids if media_id is not None]):
        removed += session.execute(
            delete(TableEmbeddedTrackHidden)
            .where(TableEmbeddedTrackHidden.media_type == media_type,
                   TableEmbeddedTrackHidden.media_id.in_(chunk),
                   TableEmbeddedTrackHidden.arr_instance_id.in_(applicable_owners(arr_instance_id)),
                   TableEmbeddedTrackHidden.language == language)).rowcount or 0
    return removed


def list_hidden(session, media_type, media_ids):
    """Every record on ``media_ids``, oldest first per row."""
    rows = []
    for chunk in in_chunks([media_id for media_id in media_ids if media_id is not None]):
        rows.extend(session.execute(
            select(TableEmbeddedTrackHidden)
            .where(TableEmbeddedTrackHidden.media_type == media_type,
                   TableEmbeddedTrackHidden.media_id.in_(chunk))
            .order_by(TableEmbeddedTrackHidden.media_id, TableEmbeddedTrackHidden.hidden_at,
                      TableEmbeddedTrackHidden.id)).scalars().all())
    return rows


def forget_hidden(session, media_type, media_ids):
    """Drop the records of media rows that no longer exist.

    The link is a plain integer, not a foreign key, because it points into
    three tables. SQLite reuses the highest deleted row id for the next insert,
    so an orphan left here would hide a track on an unrelated new item.
    """
    media_ids = [media_id for media_id in media_ids or [] if media_id is not None]
    for chunk in in_chunks(media_ids):
        session.execute(
            delete(TableEmbeddedTrackHidden)
            .where(TableEmbeddedTrackHidden.media_type == media_type,
                   TableEmbeddedTrackHidden.media_id.in_(chunk)))


def media_owners(session, media_type, media_ids):
    """The owner of each of ``media_ids`` that still has a row, by local id.

    The listing uses it to show only the records the indexer applies to each
    row (see ``applicable_owners``).
    """
    table = {'movie': TableMovies, 'episode': TableEpisodes, 'sports': TableSportsEvents}[media_type]
    owners = {}
    for chunk in in_chunks([media_id for media_id in media_ids if media_id is not None]):
        owners.update(session.execute(
            select(table.id, table.arr_instance_id).where(table.id.in_(chunk))).tuples().all())
    return owners


def episode_file_siblings(session, episode_ids):
    """Map each episode id to the ids of every episode row sharing its file.

    A multi-episode file is one path held by several rows of one owner. The
    indexer writes the same listing to all of them, so hiding, listing and
    unhiding work on the whole group. Each id maps to at least itself.
    """
    other = aliased(TableEpisodes)
    groups = {episode_id: {episode_id} for episode_id in episode_ids}
    for chunk in in_chunks(list(groups)):
        for episode_id, sibling_id in session.execute(
                select(TableEpisodes.id, other.id)
                .join(other, (other.path == TableEpisodes.path)
                      & (func.coalesce(other.arr_instance_id, UNOWNED)
                         == func.coalesce(TableEpisodes.arr_instance_id, UNOWNED)))
                .where(TableEpisodes.id.in_(chunk))).all():
            groups[episode_id].add(sibling_id)
    return groups

# coding=utf-8

import logging

from datetime import datetime

from app.database import TableBlacklistMovie, TableMovies, database, insert, delete, select, func
from app.event_handler import event_stream
from arr_instances.resolution import scoped, default_instance_id


def _resolve_movie_owner_row(radarr_id, arr_instance_id):
    """Resolve the owning movie row for a (possibly colliding) radarrId (#156).

    Scoped when arr_instance_id is given; on an unscoped collision prefer the
    DEFAULT instance's row over an arbitrary .first(). Single-instance installs
    behave identically.
    """
    rows = database.execute(scoped(
        select(TableMovies.arr_instance_id, TableMovies.id)
        .where(TableMovies.radarrId == radarr_id),
        TableMovies.arr_instance_id, arr_instance_id)).all()
    if not rows:
        return None
    if len(rows) == 1 or arr_instance_id is not None:
        return rows[0]

    default_id = default_instance_id(database, "radarr")
    preferred = next((r for r in rows if r.arr_instance_id == default_id), None)
    logging.warning(
        'BAZARR radarrId %s collides across %s instances and no arr_instance_id was given; '
        'stamping blacklist on the %s instance.', radarr_id, len(rows),
        'default' if preferred is not None else 'first-matching')
    return preferred if preferred is not None else rows[0]


def get_blacklist_movie():
    return [(item.provider, item.subs_id) for item in
            database.execute(
                select(TableBlacklistMovie.provider, TableBlacklistMovie.subs_id))
            .all()]


def blacklist_log_movie(radarr_id, provider, subs_id, language, arr_instance_id=None):
    values = dict(
        radarr_id=radarr_id,
        timestamp=datetime.now(),
        provider=provider,
        subs_id=subs_id,
        language=language,
    )

    # Stamp owner + local ref (movie_id -> the local PK) from the movie row
    # (the INC2b pattern). Guarded; NULL for an unresolved row / pre-backfill.
    if radarr_id is not None:
        mv = _resolve_movie_owner_row(radarr_id, arr_instance_id)
        if mv is not None:
            owner = arr_instance_id if arr_instance_id is not None else mv.arr_instance_id
            if owner is not None:
                values['arr_instance_id'] = owner
            if mv.id is not None:
                values['movie_id'] = mv.id

    database.execute(insert(TableBlacklistMovie).values(**values))
    event_stream(type='movie-blacklist')


def blacklist_delete_movie(provider, subs_id, arr_instance_id=None):
    # Refuse an unscoped cross-instance fan-out (#156): without an
    # arr_instance_id, deleting on (provider, subs_id) alone would wipe the
    # entry from every instance that shares it. Only proceed when the key
    # resolves to a single owning instance (legacy single-default install).
    if arr_instance_id is None:
        owners = database.execute(
            select(func.count(func.distinct(func.coalesce(TableBlacklistMovie.arr_instance_id, -1))))
            .where((TableBlacklistMovie.provider == provider) & (TableBlacklistMovie.subs_id == subs_id))
        ).scalar()
        if owners and owners > 1:
            logging.warning(
                'BAZARR refusing to delete movie blacklist entry (provider=%s, subs_id=%s) without '
                'an arr_instance_id: it is owned by %s instances and an unscoped delete would remove '
                'it from all of them. Pass arr_instance_id to target one.', provider, subs_id, owners)
            return

    database.execute(
        scoped(
            delete(TableBlacklistMovie)
            .where((TableBlacklistMovie.provider == provider) & (TableBlacklistMovie.subs_id == subs_id)),
            TableBlacklistMovie.arr_instance_id, arr_instance_id))
    event_stream(type='movie-blacklist', action='delete')


def blacklist_delete_all_movie():
    database.execute(
        delete(TableBlacklistMovie))
    event_stream(type='movie-blacklist', action='delete')

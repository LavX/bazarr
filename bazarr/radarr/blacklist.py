# coding=utf-8

from datetime import datetime

from app.database import TableBlacklistMovie, TableMovies, database, insert, delete, select
from app.event_handler import event_stream
from arr_instances.resolution import scoped


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
        mv = database.execute(scoped(
            select(TableMovies.arr_instance_id, TableMovies.id)
            .where(TableMovies.radarrId == radarr_id),
            TableMovies.arr_instance_id, arr_instance_id)).first()
        if mv is not None:
            owner = arr_instance_id if arr_instance_id is not None else mv.arr_instance_id
            if owner is not None:
                values['arr_instance_id'] = owner
            if mv.id is not None:
                values['movie_id'] = mv.id

    database.execute(insert(TableBlacklistMovie).values(**values))
    event_stream(type='movie-blacklist')


def blacklist_delete_movie(provider, subs_id):
    database.execute(
        delete(TableBlacklistMovie)
        .where((TableBlacklistMovie.provider == provider) & (TableBlacklistMovie.subs_id == subs_id)))
    event_stream(type='movie-blacklist', action='delete')


def blacklist_delete_all_movie():
    database.execute(
        delete(TableBlacklistMovie))
    event_stream(type='movie-blacklist', action='delete')

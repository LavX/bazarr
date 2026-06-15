# coding=utf-8

import logging

from datetime import datetime

from subliminal_patch.score import MAX_SCORES

from app.database import TableHistoryMovie, TableMovies, database, insert, select
from app.event_handler import event_stream
from arr_instances.resolution import scoped, default_instance_id


def _resolve_movie_owner_row(radarr_id, arr_instance_id):
    """Resolve the owning movie row for a (possibly colliding) radarrId (#156).

    Scoped lookup when arr_instance_id is given; on an unscoped collision prefer
    the DEFAULT instance's row instead of an arbitrary .first(). Single-instance
    installs behave identically.
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
        'stamping history on the %s instance.', radarr_id, len(rows),
        'default' if preferred is not None else 'first-matching')
    return preferred if preferred is not None else rows[0]


def history_log_movie(action, radarr_id, result, fake_provider=None, fake_score=None, upgraded_from_id=None,
                      arr_instance_id=None):
    description = result.message
    video_path = result.path
    language = result.language_code
    provider = fake_provider or result.provider
    score = fake_score or result.score
    subs_id = result.subs_id
    subtitles_path = result.subs_path
    matched = result.matched
    not_matched = result.not_matched

    values = dict(
        action=action,
        radarrId=radarr_id,
        timestamp=datetime.now(),
        description=description,
        video_path=video_path,
        language=language,
        provider=provider,
        score=score,
        score_out_of=MAX_SCORES['movie'] if score else None,
        subs_id=subs_id,
        subtitles_path=subtitles_path,
        matched=str(matched) if matched else None,
        not_matched=str(not_matched) if not_matched else None,
        upgradedFromId=upgraded_from_id,
    )

    # Stamp owner + local ref (movie_id -> the local PK) from the movie row so
    # history is instance-owned (the INC4 pattern for history). Guarded; NULL
    # for an unresolved row or pre-backfill install. No-op for the default
    # instance beyond populating movie_id.
    if radarr_id is not None:
        mv = _resolve_movie_owner_row(radarr_id, arr_instance_id)
        if mv is not None:
            owner = arr_instance_id if arr_instance_id is not None else mv.arr_instance_id
            if owner is not None:
                values['arr_instance_id'] = owner
            if mv.id is not None:
                values['movie_id'] = mv.id

    database.execute(insert(TableHistoryMovie).values(**values))
    event_stream(type='movie-history')

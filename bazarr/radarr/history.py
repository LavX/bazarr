# coding=utf-8

from datetime import datetime

from subliminal_patch.score import MAX_SCORES

from app.database import TableHistoryMovie, TableMovies, database, insert, select
from app.event_handler import event_stream
from arr_instances.resolution import scoped


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

    database.execute(insert(TableHistoryMovie).values(**values))
    event_stream(type='movie-history')

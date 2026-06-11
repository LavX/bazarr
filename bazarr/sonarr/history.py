# coding=utf-8

from datetime import datetime

from subliminal_patch.score import MAX_SCORES

from app.database import TableEpisodes, TableHistory, database, insert, select
from app.event_handler import event_stream
from arr_instances.resolution import scoped


def history_log(action, sonarr_series_id, sonarr_episode_id, result, fake_provider=None, fake_score=None,
                upgraded_from_id=None, arr_instance_id=None):
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
        sonarrSeriesId=sonarr_series_id,
        sonarrEpisodeId=sonarr_episode_id,
        timestamp=datetime.now(),
        description=description,
        video_path=video_path,
        language=language,
        provider=provider,
        score=score,
        score_out_of=MAX_SCORES['episode'] if score else None,
        subs_id=subs_id,
        subtitles_path=subtitles_path,
        matched=str(matched) if matched else None,
        not_matched=str(not_matched) if not_matched else None,
        upgradedFromId=upgraded_from_id,
    )

    # Stamp the owning instance + local refs (episode_id/series_id -> the local
    # PKs) from the episode row so history is instance-owned, the same way INC4
    # stamps media. Guarded: a row that can't be resolved (or a pre-backfill
    # install) leaves the columns NULL. No-op for the default single instance
    # beyond populating the local refs.
    if sonarr_episode_id is not None:
        ep = database.execute(scoped(
            select(TableEpisodes.arr_instance_id, TableEpisodes.id, TableEpisodes.series_id)
            .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
            TableEpisodes.arr_instance_id, arr_instance_id)).first()
        if ep is not None:
            owner = arr_instance_id if arr_instance_id is not None else ep.arr_instance_id
            if owner is not None:
                values['arr_instance_id'] = owner
            if ep.id is not None:
                values['episode_id'] = ep.id
            if ep.series_id is not None:
                values['series_id'] = ep.series_id

    database.execute(insert(TableHistory).values(**values))
    event_stream(type='episode-history')

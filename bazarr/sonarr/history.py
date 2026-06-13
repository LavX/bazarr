# coding=utf-8

import logging

from datetime import datetime

from subliminal_patch.score import MAX_SCORES

from app.database import TableEpisodes, TableHistory, database, insert, select
from app.event_handler import event_stream
from arr_instances.resolution import scoped, default_instance_id


def _resolve_episode_owner_row(sonarr_episode_id, arr_instance_id):
    """Resolve the owning episode row for a (possibly colliding) upstream id.

    When ``arr_instance_id`` is given the lookup is scoped to it (exact owner).
    When it is None and the upstream id collides across instances (#156), an
    unscoped ``.first()`` would pick an arbitrary instance's row; instead prefer
    the DEFAULT instance's row and log a warning. Single-instance installs (one
    matching row) behave identically to the old code.
    """
    rows = database.execute(scoped(
        select(TableEpisodes.arr_instance_id, TableEpisodes.id, TableEpisodes.series_id)
        .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
        TableEpisodes.arr_instance_id, arr_instance_id)).all()
    if not rows:
        return None
    if len(rows) == 1 or arr_instance_id is not None:
        return rows[0]

    default_id = default_instance_id(database, "sonarr")
    preferred = next((r for r in rows if r.arr_instance_id == default_id), None)
    logging.warning(
        'BAZARR sonarrEpisodeId %s collides across %s instances and no arr_instance_id was given; '
        'stamping history on the %s instance.', sonarr_episode_id, len(rows),
        'default' if preferred is not None else 'first-matching')
    return preferred if preferred is not None else rows[0]


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
        ep = _resolve_episode_owner_row(sonarr_episode_id, arr_instance_id)
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

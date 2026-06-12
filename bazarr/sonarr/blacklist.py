# coding=utf-8

from datetime import datetime

from app.database import TableBlacklist, TableEpisodes, database, insert, delete, select
from app.event_handler import event_stream
from arr_instances.resolution import scoped


def get_blacklist():
    return [(item.provider, item.subs_id) for item in
            database.execute(
                select(TableBlacklist.provider, TableBlacklist.subs_id))
            .all()]


def blacklist_log(sonarr_series_id, sonarr_episode_id, provider, subs_id, language, arr_instance_id=None):
    values = dict(
        sonarr_series_id=sonarr_series_id,
        sonarr_episode_id=sonarr_episode_id,
        timestamp=datetime.now(),
        provider=provider,
        subs_id=subs_id,
        language=language,
    )

    # Stamp owner + local refs (episode_id/series_id -> the local PKs) from the
    # episode row so blacklist rows are instance-owned (the INC2b pattern).
    # Guarded; NULL for an unresolved row or pre-backfill install.
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

    database.execute(insert(TableBlacklist).values(**values))
    event_stream(type='episode-blacklist')


def blacklist_delete(provider, subs_id, arr_instance_id=None):
    database.execute(
        scoped(
            delete(TableBlacklist)
            .where((TableBlacklist.provider == provider) & (TableBlacklist.subs_id == subs_id)),
            TableBlacklist.arr_instance_id, arr_instance_id))
    event_stream(type='episode-blacklist', action='delete')


def blacklist_delete_all():
    database.execute(delete(TableBlacklist))
    event_stream(type='episode-blacklist', action='delete')

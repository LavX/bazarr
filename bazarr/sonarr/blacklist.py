# coding=utf-8

import logging

from datetime import datetime

from app.database import TableBlacklist, TableEpisodes, database, insert, delete, select, func
from app.event_handler import event_stream
from arr_instances.resolution import scoped, default_instance_id


def _resolve_episode_owner_row(sonarr_episode_id, arr_instance_id):
    """Resolve the owning episode row for a (possibly colliding) upstream id.

    Scoped when arr_instance_id is given; on an unscoped multi-instance
    collision prefer the DEFAULT instance's row over an arbitrary .first()
    (#156). Single-instance installs behave identically.
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
        'stamping blacklist on the %s instance.', sonarr_episode_id, len(rows),
        'default' if preferred is not None else 'first-matching')
    return preferred if preferred is not None else rows[0]


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
        ep = _resolve_episode_owner_row(sonarr_episode_id, arr_instance_id)
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
    # When the caller can't tell us the owner (arr_instance_id None) the scoped
    # delete would be unscoped and wipe the (provider, subs_id) pair from EVERY
    # instance that shares it (#156). Refuse that cross-instance fan-out: only
    # proceed if the key resolves to a single owning instance (the legacy
    # single-default install, where every match shares one owner or NULL).
    if arr_instance_id is None:
        owners = database.execute(
            select(func.count(func.distinct(TableBlacklist.arr_instance_id)))
            .where((TableBlacklist.provider == provider) & (TableBlacklist.subs_id == subs_id))
        ).scalar()
        if owners and owners > 1:
            logging.warning(
                'BAZARR refusing to delete blacklist entry (provider=%s, subs_id=%s) without an '
                'arr_instance_id: it is owned by %s instances and an unscoped delete would remove '
                'it from all of them. Pass arr_instance_id to target one.', provider, subs_id, owners)
            return

    database.execute(
        scoped(
            delete(TableBlacklist)
            .where((TableBlacklist.provider == provider) & (TableBlacklist.subs_id == subs_id)),
            TableBlacklist.arr_instance_id, arr_instance_id))
    event_stream(type='episode-blacklist', action='delete')


def blacklist_delete_all():
    database.execute(delete(TableBlacklist))
    event_stream(type='episode-blacklist', action='delete')

"""Reconcile native Sportarr leagues without crossing instance boundaries."""
from datetime import datetime
import logging

from sqlalchemy import delete, select

from app.config import settings
from app.database import database, TableArrInstances, TableSportsLeagues
from arr_instances.client import ArrClientFactory
from arr_instances.resolution import resolve_default_profile
from sportarr.db import sports_transaction
from sportarr.connection import check_cancelled, connection_identity, owner_sync_lock
from sportarr.settings import get_sports_settings


def require_sportarr(session, arr_instance_id):
    if type(arr_instance_id) is not int or arr_instance_id <= 0:
        raise ValueError('A Sportarr instance owner is required')
    instance = session.execute(select(TableArrInstances).where(
        TableArrInstances.id == arr_instance_id,
        TableArrInstances.kind == 'sportarr', TableArrInstances.enabled == 1)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if instance is None:
        raise ValueError('Enabled Sportarr instance not found')
    return instance


def _get_list(client, path):
    try:
        response = client.get(path)
        if response.status_code != 200:
            raise ValueError('Sportarr request failed')
        data = response.json()
    except Exception:
        # Do not propagate credential-bearing transport exception strings.
        raise ValueError('Could not read the complete Sportarr response') from None
    if not isinstance(data, list):
        raise ValueError('Sportarr response must be a complete list')
    return data


def _parse_leagues(data, client):
    ids = set()
    for item in data:
        if (not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0
                or not isinstance(item.get('name'), str) or not item['name'].strip()
                or type(item.get('monitored')) is not bool
                or not isinstance(item.get('tags', []), list)
                or any(type(tag) is not int for tag in item.get('tags', []))
                or item.get('path') is not None and not isinstance(item['path'], str)):
            raise ValueError('Malformed Sportarr league')
        if item['id'] in ids:
            raise ValueError('Duplicate Sportarr league identity')
        ids.add(item['id'])
    tags = {}
    if any(item.get('tags') for item in data):
        for tag in _get_list(client, '/api/tag'):
            if not isinstance(tag, dict) or type(tag.get('id')) is not int or not isinstance(tag.get('label'), str):
                raise ValueError('Malformed Sportarr tag')
            tags[tag['id']] = tag['label']
        if any(tag not in tags for item in data for tag in item.get('tags', [])):
            raise ValueError('Incomplete Sportarr tags')
    return [(item, [tags[tag] for tag in item.get('tags', [])]) for item in data]


def _prune_leagues(session, owner, upstream_ids):
    session.execute(delete(TableSportsLeagues).where(
        TableSportsLeagues.arr_instance_id == owner,
        TableSportsLeagues.sportarrLeagueId.not_in(upstream_ids)))


def notify(local_ids):
    from app.event_handler import event_stream
    try:
        event_stream(type='sports')
    except Exception:
        logging.exception('Could not notify sports library listeners')


def sync_leagues(arr_instance_id, *, cancel=None, expected_connection=None, http_get=None,
                 lock_timeout=None):
    with owner_sync_lock(arr_instance_id, cancel, timeout=lock_timeout):
        return _sync_leagues(arr_instance_id, cancel, expected_connection, http_get)


def _sync_leagues(arr_instance_id, cancel, expected_connection, http_get):
    """Return synced local league IDs, after complete owner-scoped reconciliation."""
    instance = require_sportarr(database, arr_instance_id)
    expected = connection_identity(instance)
    if expected_connection is not None and expected != expected_connection:
        raise ValueError('Sportarr connection changed during synchronization')
    factory = ArrClientFactory()
    client = factory.from_row(instance, http_get=http_get) if http_get else factory.from_row(instance)
    parsed = _parse_leagues(_get_list(client, '/api/leagues'), client)
    local_ids = []
    with sports_transaction(database) as session:
        check_cancelled(cancel)
        # Revalidate after the network roundtrip: disable/delete must stop writes.
        instance = require_sportarr(session, arr_instance_id)
        if connection_identity(instance) != expected:
            raise ValueError('Sportarr connection changed during synchronization')
        options = get_sports_settings(instance)
        # The precedence Series and Movies already use: a Series/Movie tag, then
        # the owning instance's override when it has one, then the global
        # default. Sports resolved the override alone, so an install that never
        # set a per-instance override stamped no profile on any league and the
        # Languages page had nothing to point at.
        profile = resolve_default_profile(
            arr_instance_id,
            settings.general.sports_default_enabled,
            settings.general.sports_default_profile,
            session=session)
        existing = {row.sportarrLeagueId: row for row in session.execute(
            select(TableSportsLeagues).where(TableSportsLeagues.arr_instance_id == arr_instance_id)).scalars()}
        kept = []
        for item, tags in parsed:
            if options['sync_only_monitored_leagues'] and not item['monitored']:
                continue
            kept.append(item['id'])
            row = existing.get(item['id'])
            now = datetime.now()
            if row is None:
                row = TableSportsLeagues(arr_instance_id=arr_instance_id,
                                         sportarrLeagueId=item['id'], profileId=profile,
                                         created_at_timestamp=now)
                session.add(row)
            row.externalId = item.get('externalId')
            row.path = item.get('path')
            row.title = row.sortTitle = item['name']
            row.overview = item.get('description')
            row.poster = item.get('posterUrl') or item.get('logoUrl')
            row.fanart = item.get('bannerUrl')
            row.sport = item.get('sport')
            row.monitored = str(item['monitored'])
            row.tags = str(tags)
            row.audio_language = '[]'
            row.updated_at_timestamp = now
            session.flush()
            local_ids.append(row.id)
        _prune_leagues(session, arr_instance_id, kept)
    notify(local_ids)
    return local_ids


def update_sports_for_instance(arr_instance_id, job_id=None, *, cancel=None, expected_connection=None,
                               http_get=None, lock_timeout=None):
    """Startup and periodic repair of one complete owned sports library.

    ``lock_timeout`` is threaded through so a request-thread caller can bound
    its wait; background callers leave it None and wait as they always have.
    """
    from sportarr.rootfolder import sync_rootfolders
    from sportarr.sync.events import sync_events
    with owner_sync_lock(arr_instance_id, cancel, timeout=lock_timeout):
        expected = expected_connection or connection_identity(require_sportarr(database, arr_instance_id))
        # The lock is re-entrant and already held here, so the nested calls
        # cannot block on it; they take the same owner's RLock again.
        kwargs = dict(cancel=cancel, expected_connection=expected, http_get=http_get)
        sync_rootfolders(arr_instance_id, **kwargs)
        ids = sync_leagues(arr_instance_id, **kwargs)
        for league_id in ids:
            check_cancelled(cancel)
            sync_events(league_id, arr_instance_id, **kwargs)
        return ids

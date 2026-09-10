"""Atomic native pagination and file-first, owner-scoped reconciliation."""

import logging
from datetime import datetime

from sqlalchemy import delete, or_, select

from app.database import database, TableSportsEvents, TableSportsLeagues
from arr_instances.client import ArrClientFactory
from sportarr.connection import check_cancelled, connection_identity, owner_sync_lock, revalidate
from sportarr.db import sports_transaction
from sportarr.parser import parse_events, positive_id
from app.config import settings
from sportarr.settings import get_sports_settings
from sportarr.sync.leagues import notify, require_sportarr
from utilities.sql_limits import in_chunks
from sportarr.errors import SportsNotFound


def read_events(client, upstream_league_id, page_size=1000, cancel=None):
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError('Invalid Sportarr page size')
    records, total, pages = [], None, None
    page = 1
    while True:
        check_cancelled(cancel)
        try:
            response = client.get(f'/api/leagues/{upstream_league_id}/events'
                                  f'?showAll=true&page={page}&pageSize={page_size}')
            if response.status_code != 200:
                raise ValueError()
            payload = response.json()
        except Exception:
            raise ValueError('Could not read complete Sportarr events') from None
        if (not isinstance(payload, dict)
                or any(type(payload.get(field)) is not int for field in
                       ('page', 'pageSize', 'totalRecords', 'totalPages'))
                or payload['page'] != page or payload['pageSize'] != page_size
                or payload['totalRecords'] < 0
                or not isinstance(payload.get('records'), list)):
            raise ValueError('Malformed Sportarr event page')
        expected_pages = (payload['totalRecords'] + page_size - 1) // page_size
        if payload['totalPages'] != expected_pages:
            raise ValueError('Inconsistent Sportarr page count')
        if total is None:
            total, pages = payload['totalRecords'], payload['totalPages']
        if total != payload['totalRecords'] or pages != payload['totalPages']:
            raise ValueError('Sportarr event list changed during pagination')
        expected_count = min(page_size, max(0, total - (page - 1) * page_size))
        if len(payload['records']) != expected_count:
            raise ValueError('Incomplete Sportarr event page')
        records.extend(payload['records'])
        if page >= pages:
            return parse_events(records, upstream_league_id)
        page += 1


def _league(session, league_id, owner):
    if not positive_id(league_id):
        raise ValueError('An exact local sports league ID is required')
    row = session.execute(select(TableSportsLeagues).where(
        TableSportsLeagues.id == league_id, TableSportsLeagues.arr_instance_id == owner)
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        raise SportsNotFound('Sports league not found for this owner')
    return row


def _prune_events(session, league_id, owner, keep):
    existing = session.execute(select(TableSportsEvents.id).where(
        TableSportsEvents.league_id == league_id, TableSportsEvents.arr_instance_id == owner)).scalars()
    stale = set(existing) - set(keep)
    if not stale:
        return
    # Drop the mismatches of the events about to disappear, the way the sonarr
    # and radarr sync paths do. The link is a plain integer, not a foreign key,
    # so nothing removes these on its own, and SQLite reuses a deleted row id
    # for a later insert, which would badge an unrelated new event.
    try:
        from subtitles.mismatch import forget_media
        forget_media(session, 'sports', list(stale))
    except Exception:
        logging.exception('BAZARR could not forget the pruned sports events mismatches')
    for batch in in_chunks(stale):
        session.execute(delete(TableSportsEvents).where(
            TableSportsEvents.league_id == league_id, TableSportsEvents.arr_instance_id == owner,
            TableSportsEvents.id.in_(batch)))


def refresh_event_files(event_ids, arr_instance_id, cancel=None):
    """Post-commit hook for the subtitle indexer, using exact local file-row IDs."""
    from subtitles.indexer.sports import refresh_sports_files

    refresh_sports_files(event_ids, arr_instance_id, cancel=cancel)
    from sportarr.workflows import search_after_sync

    search_after_sync(database, event_ids, arr_instance_id, cancel=cancel)


def _match_events(existing, parsed):
    by_file = {row.file_id: row for row in existing}
    matches = [by_file.get(item['file_id']) for item in parsed]
    reserved = {row.id for row in matches if row is not None}
    by_path = {}
    for row in existing:
        if row.id not in reserved:
            by_path.setdefault((row.sportarrEventId, row.path), []).append(row)
    # Reserve every stable path before positional replacement can consume it.
    for index, item in enumerate(parsed):
        if matches[index] is not None:
            continue
        candidates = by_path.get((item['sportarrEventId'], item['path']), [])
        if len(candidates) > 1:
            raise ValueError('Ambiguous stored Sportarr event path')
        if candidates:
            matches[index] = candidates[0]
            reserved.add(candidates[0].id)
    by_part = {(row.sportarrEventId, row.partNumber): row for row in existing if row.id not in reserved}
    for index, item in enumerate(parsed):
        if matches[index] is None:
            matches[index] = by_part.get((item['sportarrEventId'], item['partNumber']))
    return matches


def sync_one_league(league_id, arr_instance_id, job_id=None, *, cancel=None):
    """Queue-callable wrapper for a single league's event sync.

    The jobs queue injects job_id into every job's kwargs, and sync_events does
    not take one, so queueing it directly raised TypeError the moment the job
    ran: the request returned 202 and the sync never happened. Mirrors
    update_sports_for_instance, which accepts and ignores job_id for the same
    reason.
    """
    return sync_events(league_id, arr_instance_id, cancel=cancel)


def sync_events(league_id, arr_instance_id, *, page_size=1000, cancel=None, expected_connection=None,
                http_get=None, lock_timeout=None):
    with owner_sync_lock(arr_instance_id, cancel, timeout=lock_timeout):
        instance = require_sportarr(database, arr_instance_id)
        expected = connection_identity(instance)
        if expected_connection is not None and expected_connection != expected:
            raise ValueError('Sportarr connection changed during synchronization')
        league = _league(database, league_id, arr_instance_id)
        upstream_id = league.sportarrLeagueId
        factory = ArrClientFactory()
        client = factory.from_row(instance, http_get=http_get) if http_get else factory.from_row(instance)
        parsed = read_events(client, upstream_id, page_size, cancel)
        ids = []
        with sports_transaction(database) as transaction:
            instance = revalidate(transaction, arr_instance_id, expected, cancel)
            if get_sports_settings(instance)['sync_only_monitored_events']:
                parsed = [item for item in parsed if item['monitored'] == 'True']
            if _league(transaction, league_id, arr_instance_id).sportarrLeagueId != upstream_id:
                raise ValueError('Sports league changed during synchronization')
            existing = transaction.execute(select(TableSportsEvents).where(
                TableSportsEvents.arr_instance_id == arr_instance_id,
                TableSportsEvents.league_id == league_id)).scalars().all()
            for batch in in_chunks(parsed, size=200):
                conflict = transaction.execute(select(TableSportsEvents.id).where(
                    TableSportsEvents.arr_instance_id == arr_instance_id,
                    TableSportsEvents.league_id != league_id,
                    or_(TableSportsEvents.file_id.in_([item['file_id'] for item in batch]),
                        TableSportsEvents.sportarrEventId.in_([item['sportarrEventId'] for item in batch]),
                        TableSportsEvents.path.in_([item['path'] for item in batch]))).limit(1)).first()
                if conflict:
                    raise ValueError('Sportarr file belongs to another local league')
            matches = _match_events(existing, parsed)
            # Vacate positional keys together before applying swaps. File IDs take
            # precedence, keeping subtitle history attached to the same physical file.
            for row in existing:
                row.partNumber = -row.id
            transaction.flush()
            # With embedded audio parsing on, audio_language belongs to the
            # indexer: Sportarr reports no audio metadata, so the parser can
            # only ever offer '[]'. Letting the sync write that back would both
            # erase what ffprobe found AND, because the column takes part in the
            # new_file comparison below, make the next sync judge the file to be
            # new and wipe the subtitle index with it.
            indexer_owns_audio = settings.general.parse_embedded_audio_track
            compared = ('file_id', 'path', 'file_size', 'format', 'resolution',
                        'video_codec', 'audio_codec', 'sceneName')
            if not indexer_owns_audio:
                compared += ('audio_language',)
            for item, row in zip(parsed, matches):
                # Per row, not just once before the loop. A league with
                # thousands of events flushes once per row here, and without
                # this a restart or a disabled instance had to wait for the
                # whole loop to drain before the thread could notice.
                check_cancelled(cancel)
                now = datetime.now()
                new_file = row is None or any(getattr(row, key) != item[key] for key in compared)
                if row is None:
                    row = TableSportsEvents(arr_instance_id=arr_instance_id, league_id=league_id,
                                            created_at_timestamp=now)
                    transaction.add(row)
                if new_file:
                    row.ffprobe_cache = None
                    row.subtitles = row.missing_subtitles = row.failedAttempts = '[]'
                for key, value in item.items():
                    if key == 'audio_language' and indexer_owns_audio and not new_file:
                        continue
                    setattr(row, key, value)
                row.updated_at_timestamp = now
                transaction.flush()
                ids.append(row.id)
            _prune_events(transaction, league_id, arr_instance_id, ids)
        if ids:
            refresh_event_files(ids, arr_instance_id, cancel=cancel)
        notify(ids)
        return ids

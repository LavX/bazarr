"""Atomic native pagination and file-first, owner-scoped reconciliation."""

import ast
import logging
from datetime import datetime

from sqlalchemy import delete, select

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


class EventSnapshot(list):
    """Playable rows plus complete upstream identity evidence, including no-file events."""

    def __init__(self, records, upstream_league_id):
        super().__init__(parse_events(records, upstream_league_id))
        self.records = records
        self.upstream_league_id = upstream_league_id
        self.event_ids = {item['id'] for item in records}
        self.file_claims = {item['id']: (event['id'], item['filePath'], item['exists'], item['size'])
                            for event in records for item in event['files']}


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
            return EventSnapshot(records, upstream_league_id)
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


def refresh_event_files(event_ids, arr_instance_id, cancel=None, *, search_event_ids=()):
    """Post-commit hook for the subtitle indexer, using exact local file-row IDs."""
    from sportarr.hash_index import refresh_recording_index
    refresh_recording_index(arr_instance_id, event_ids, session=database, cancel=cancel)
    from subtitles.indexer.sports import refresh_sports_files

    refresh_sports_files(event_ids, arr_instance_id, cancel=cancel)
    from sportarr.workflows import search_after_sync

    if search_event_ids:
        search_after_sync(database, search_event_ids, arr_instance_id, cancel=cancel)


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
    update_sports_for_instance.

    That job_id is also the only handle the Tasks page has on this sync, so it
    becomes the cancellation signal when the caller supplied none: without it
    stopping the task marked the job cancelled and the league went on syncing.
    """
    from sportarr.workflows import SportsJobSignal

    if cancel is None and job_id:
        cancel = SportsJobSignal(arr_instance_id, job_id)
    return sync_events(league_id, arr_instance_id, cancel=cancel)


def _validate_snapshots(snapshots):
    files, paths, events = set(), set(), set()
    for snapshot in snapshots.values():
        if not isinstance(snapshot, EventSnapshot):
            raise ValueError("A complete Sportarr event snapshot is required")
        if events & snapshot.event_ids or files & snapshot.file_claims.keys():
            raise ValueError('Contradictory Sportarr league snapshots')
        events.update(snapshot.event_ids)
        files.update(snapshot.file_claims)
        claimed_paths = {claim[1] for claim in snapshot.file_claims.values()}
        if paths & claimed_paths:
            raise ValueError('Contradictory Sportarr league snapshots')
        paths.update(claimed_paths)


def _same_snapshot(first, second):
    return (first.event_ids == second.event_ids and first.file_claims == second.file_claims
            and sorted(first, key=lambda item: item['file_id']) == sorted(second, key=lambda item: item['file_id']))


def _expanded_snapshots(client, league_ids, owner, page_size, cancel, complete):
    leagues = dict(database.execute(select(TableSportsLeagues.id, TableSportsLeagues.sportarrLeagueId).where(
        TableSportsLeagues.arr_instance_id == owner)).all())
    if not set(league_ids) <= set(leagues):
        raise SportsNotFound('Sports league not found for this owner')
    by_upstream = {upstream: local for local, upstream in leagues.items()}
    snapshots = {}
    def fetch(local):
        if local not in snapshots:
            snapshots[local] = read_events(client, leagues[local], page_size, cancel)
    for local in league_ids:
        fetch(local)
    stored = database.execute(select(TableSportsEvents).where(
        TableSportsEvents.arr_instance_id == owner)).scalars().all()
    located = set()
    while True:
        _validate_snapshots(snapshots)
        parsed = [item for items in snapshots.values() for item in items]
        file_ids = {item['file_id'] for item in parsed}
        event_ids = {item['sportarrEventId'] for item in parsed}
        paths = {item['path'] for item in parsed}
        conflicts = {row.league_id for row in stored if row.league_id not in snapshots
                     and (row.file_id in file_ids or row.sportarrEventId in event_ids or row.path in paths)}
        if conflicts:
            for local in sorted(conflicts):
                fetch(local)
            continue
        if complete:
            break
        affected = [row for row in stored if row.league_id in snapshots]
        matched = {row.id for row in _match_events(affected, parsed) if row is not None}
        missing = [row for row in affected if row.id not in matched]
        if not missing:
            break
        expanded = False
        for row in missing:
            check_cancelled(cancel)
            if row.sportarrEventId in located:
                continue
            located.add(row.sportarrEventId)
            try:
                response = client.get(f'/api/events/{row.sportarrEventId}')
                if response.status_code == 404:
                    continue
                if response.status_code != 200:
                    raise ValueError()
                payload = response.json()
                if (not isinstance(payload, dict) or payload.get('id') != row.sportarrEventId
                        or not positive_id(payload.get('leagueId'))):
                    raise ValueError()
                destination = by_upstream.get(payload['leagueId'])
                if destination is None:
                    raise ValueError()
                locator_items = EventSnapshot([payload], payload['leagueId'])
            except Exception:
                raise ValueError('Could not resolve the current Sportarr event owner') from None
            if destination not in snapshots:
                fetch(destination)
                expanded = True
            if not _same_snapshot(locator_items, EventSnapshot(
                    [item for item in snapshots[destination].records if item['id'] == row.sportarrEventId],
                    payload['leagueId'])):
                raise ValueError('Sportarr event changed during reconciliation')
        if expanded:
            continue
        # A vanished event does not prove its recording was deleted: a file can
        # be reassigned to another event. Only this exceptional disappearance
        # path needs the complete owner event list. Ordinary callbacks do not.
        from sportarr.sync.leagues import _get_list
        raw = _get_list(client, '/api/events')
        grouped = {local: [] for local in leagues}
        for item in raw:
            if not isinstance(item, dict) or not positive_id(item.get('leagueId')):
                if isinstance(item, dict) and item.get('leagueId') is None and not item.get('hasFile') and not item.get('files'):
                    continue
                raise ValueError('Incomplete Sportarr owner event snapshot')
            local = by_upstream.get(item['leagueId'])
            if local is None:
                # Unknown destinations require league reconciliation first.
                if item.get('hasFile') or item.get('files'):
                    raise ValueError('Sportarr destination league is not synchronized')
                continue
            grouped[local].append(item)
        all_snapshots = {local: EventSnapshot(items, leagues[local]) for local, items in grouped.items()}
        _validate_snapshots(all_snapshots)
        if any(not _same_snapshot(items, all_snapshots[local]) for local, items in snapshots.items()):
            raise ValueError('Sportarr owner snapshot contradicts the league snapshot')
        snapshots = all_snapshots
        break
    return snapshots, leagues


def _artifact_signatures(session, row, instance):
    from sportarr.identity import resolve_event_in_session
    from sportarr.subtitles import _signature
    try:
        return _signature(resolve_event_in_session(session, row.id, row.arr_instance_id), instance)
    except (OSError, ValueError):
        return None


def _rebind_artifacts(session, row, before, instance):
    import hashlib
    import json
    from app.database import TableHistorySports
    from sportarr.artifacts import validate_artifact_stat
    if before is None:
        return
    after = _artifact_signatures(session, row, instance)
    if after is None or before[1:] != after[1:]:
        return
    old_digest = hashlib.sha256(repr(before).encode()).hexdigest()
    new_digest = hashlib.sha256(repr(after).encode()).hexdigest()
    for history in session.execute(select(TableHistorySports).where(
            TableHistorySports.event_id == row.id, TableHistorySports.arr_instance_id == row.arr_instance_id)
            .execution_options(populate_existing=True)).scalars():
        try:
            proof = json.loads(history.artifact)
            if (not isinstance(proof, dict) or type(proof.get('version')) is not int or proof['version'] != 1
                    or proof.get('video') != old_digest or not isinstance(proof.get('path'), str)
                    or not isinstance(proof.get('stat'), list) or len(proof['stat']) != 5
                    or any(type(value) is not int for value in proof['stat'])
                    or not isinstance(proof.get('sha256'), str) or len(proof['sha256']) != 64
                    or any(character not in '0123456789abcdef' for character in proof['sha256'])):
                continue
            validate_artifact_stat(history.artifact, proof['path'])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        proof['video'] = new_digest
        history.artifact = json.dumps(proof, sort_keys=True)


def sync_event_leagues(league_ids, arr_instance_id, *, page_size=1000, cancel=None, expected_connection=None,
                      http_get=None, lock_timeout=None, is_signalr=False, complete=False):
    with owner_sync_lock(arr_instance_id, cancel, timeout=lock_timeout):
        instance = require_sportarr(database, arr_instance_id)
        expected = connection_identity(instance)
        if expected_connection is not None and expected_connection != expected:
            raise ValueError('Sportarr connection changed during synchronization')
        factory = ArrClientFactory()
        client = factory.from_row(instance, http_get=http_get) if http_get else factory.from_row(instance)
        snapshots, leagues = _expanded_snapshots(client, league_ids, arr_instance_id, page_size, cancel, complete)
        ids_by_league = {local: [] for local in snapshots}
        search_event_ids = []
        with sports_transaction(database) as transaction:
            instance = revalidate(transaction, arr_instance_id, expected, cancel)
            for local in snapshots:
                if _league(transaction, local, arr_instance_id).sportarrLeagueId != leagues[local]:
                    raise ValueError('Sports league changed during synchronization')
            options = get_sports_settings(instance)
            parsed = [(local, item) for local, items in snapshots.items() for item in items
                      if not options['sync_only_monitored_events'] or item['monitored'] == 'True']
            existing = transaction.execute(select(TableSportsEvents).where(
                TableSportsEvents.arr_instance_id == arr_instance_id,
                TableSportsEvents.league_id.in_(list(snapshots)))).scalars().all()
            matches = _match_events(existing, [item for _, item in parsed])
            indexer_owns_audio = settings.general.parse_embedded_audio_track
            compared = ('file_id', 'path', 'file_size', 'format', 'resolution', 'video_codec', 'audio_codec', 'sceneName')
            if not indexer_owns_audio:
                compared += ('audio_language',)
            old_values = {row.id: {key: getattr(row, key) for key in compared} for row in existing}
            moving = {row.id: _artifact_signatures(transaction, row, instance) for (local, item), row in zip(parsed, matches)
                      if row is not None and (row.league_id != local or row.sportarrEventId != item['sportarrEventId']
                                              or row.file_id != item['file_id'] or row.path != item['path'])}
            # Vacate every unique file and positional key before applying swaps.
            for row in existing:
                row.partNumber = -row.id
                row.file_id = -row.id
            transaction.flush()
            for (local, item), row in zip(parsed, matches):
                check_cancelled(cancel)
                now = datetime.now()
                new_file = row is None or any(old_values[row.id][key] != item[key] for key in compared)
                if row is None:
                    row = TableSportsEvents(arr_instance_id=arr_instance_id, league_id=local, created_at_timestamp=now)
                    transaction.add(row)
                if new_file:
                    row.ffprobe_cache = None
                    row.subtitles = row.missing_subtitles = row.failedAttempts = '[]'
                row.league_id = local
                for key, value in item.items():
                    if key == 'audio_language' and indexer_owns_audio and not new_file:
                        continue
                    setattr(row, key, value)
                row.updated_at_timestamp = now
                transaction.flush()
                if row.id in moving:
                    _rebind_artifacts(transaction, row, moving[row.id], instance)
                ids_by_league[local].append(row.id)
                if new_file:
                    search_event_ids.append(row.id)
            for local, ids in ids_by_league.items():
                _prune_events(transaction, local, arr_instance_id, ids)
            check_cancelled(cancel)
        all_ids = [local_id for ids in ids_by_league.values() for local_id in ids]
        if all_ids:
            refresh_event_files(all_ids, arr_instance_id, cancel=cancel, search_event_ids=search_event_ids)
        notify(all_ids)
        if is_signalr and settings.general.notify_if_nothing_is_missing_for_signalr_event:
            for local in snapshots:
                _notify_league_fully_subtitled(database, local, arr_instance_id)
        return ids_by_league


def sync_events(league_id, arr_instance_id, *, page_size=1000, cancel=None, expected_connection=None,
                http_get=None, lock_timeout=None, is_signalr=False):
    return sync_event_leagues([league_id], arr_instance_id, page_size=page_size, cancel=cancel,
                             expected_connection=expected_connection, http_get=http_get, lock_timeout=lock_timeout,
                             is_signalr=is_signalr)[league_id]


def _notify_league_fully_subtitled(session, league_id, owner):
    """A live sync found a league with no missing subtitles left to search.

    Mirrors the Sonarr and Radarr sync paths: the option only describes what a
    live sync found, an empty league is not what it describes, and a league
    that still has missing languages must not report the opposite.
    """
    rows = session.execute(select(TableSportsEvents.missing_subtitles).where(
        TableSportsEvents.arr_instance_id == owner,
        TableSportsEvents.league_id == league_id)).scalars().all()
    if not rows:
        return
    if any(ast.literal_eval(row or '[]') for row in rows):
        return
    from app.notifier import send_notifications_sports_league
    try:
        send_notifications_sports_league(
            league_id, "There are no missing subtitles in this league.", owner)
    except Exception:
        logging.exception('Could not send the sports sync-found-nothing notification')

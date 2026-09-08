"""Local-ID sports library operations shared by the HTTP boundary."""
import ast

from sqlalchemy import func, select, update

from app.database import TableArrInstances, TableSportsLeagues, TableSportsEvents, TableLanguagesProfiles
from arr_instances.media_defaults import instance_default_profile, read_media_defaults
from sportarr.db import sports_transaction
from sportarr.sync.leagues import require_sportarr
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings


def _query():
    counts = select(
        TableSportsEvents.league_id,
        TableSportsEvents.arr_instance_id,
        func.count(func.distinct(TableSportsEvents.sportarrEventId)).label('eventCount'),
        func.count(TableSportsEvents.id).label('eventFileCount'),
    ).group_by(TableSportsEvents.league_id, TableSportsEvents.arr_instance_id).subquery()
    return select(TableSportsLeagues, counts.c.eventCount, counts.c.eventFileCount).join(
        TableArrInstances, TableArrInstances.id == TableSportsLeagues.arr_instance_id).outerjoin(
        counts, (counts.c.league_id == TableSportsLeagues.id) &
        (counts.c.arr_instance_id == TableSportsLeagues.arr_instance_id)).where(
            TableArrInstances.kind == 'sportarr', TableArrInstances.enabled == 1)


def _serialize(row):
    league, event_count, file_count = row
    result = league.to_dict()
    for field in ('created_at_timestamp', 'updated_at_timestamp'):
        if result[field] is not None:
            result[field] = result[field].isoformat()
    for field in ('tags', 'audio_language'):
        try:
            value = ast.literal_eval(result[field] or '[]')
        except (SyntaxError, ValueError):
            value = []
        result[field] = value if isinstance(value, list) else []
    result['monitored'] = result['monitored'] == 'True'
    return result | {'eventCount': event_count or 0, 'eventFileCount': file_count or 0}


def list_leagues(session, arr_instance_id=None, start=0, length=100):
    query = _query()
    if arr_instance_id is not None:
        require_sportarr(session, arr_instance_id)
        query = query.where(TableSportsLeagues.arr_instance_id == arr_instance_id)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = session.execute(query.order_by(TableSportsLeagues.sortTitle, TableSportsLeagues.id)
                           .offset(start).limit(length)).all()
    return {'data': [_serialize(row) for row in rows], 'total': total}


def get_league(session, league_id, arr_instance_id=None):
    query = _query().where(TableSportsLeagues.id == league_id)
    if arr_instance_id is not None:
        require_sportarr(session, arr_instance_id)
        query = query.where(TableSportsLeagues.arr_instance_id == arr_instance_id)
    row = session.execute(query).first()
    return _serialize(row) if row is not None else None


def assign_profile(session, league_id, arr_instance_id, profile_id):
    if profile_id is not None and (type(profile_id) is not int or profile_id <= 0):
        raise ValueError('profileId must be a positive language profile ID or null')
    with sports_transaction(session) as transaction:
        require_sportarr(transaction, arr_instance_id)
        if profile_id is not None and transaction.get(TableLanguagesProfiles, profile_id) is None:
            raise ValueError('Language profile does not exist')
        result = transaction.execute(update(TableSportsLeagues).where(
            TableSportsLeagues.id == league_id,
            TableSportsLeagues.arr_instance_id == arr_instance_id).values(profileId=profile_id))
        return result.rowcount == 1


def apply_instance_default_profile(session, arr_instance_id):
    with sports_transaction(session) as transaction:
        instance = require_sportarr(transaction, arr_instance_id)
        _, profile = instance_default_profile(read_media_defaults(instance.options))
        if profile is None or transaction.get(TableLanguagesProfiles, profile) is None:
            raise ValueError('This instance has no valid default language profile to apply.')
        ids = transaction.execute(select(TableSportsLeagues.id).where(
            TableSportsLeagues.arr_instance_id == arr_instance_id,
            TableSportsLeagues.profileId.is_(None))).scalars().all()
        transaction.execute(update(TableSportsLeagues).where(
            TableSportsLeagues.arr_instance_id == arr_instance_id,
            TableSportsLeagues.profileId.is_(None)).values(profileId=profile))
    return {'updated': len(ids), 'profileId': profile, 'kind': 'sportarr', 'upstream_ids': ids}


def refresh_league_profiles(league_ids, arr_instance_id, job_id=None):
    from subtitles.indexer.sports import list_missing_subtitles_sports
    for league_id in league_ids:
        list_missing_subtitles_sports(league_id=league_id, arr_instance_id=arr_instance_id)


def _event_query(arr_instance_id=None, enabled_only=True):
    query = select(TableSportsEvents, TableSportsLeagues.profileId, TableArrInstances.path_mappings).join(
        TableSportsLeagues, (TableSportsEvents.league_id == TableSportsLeagues.id) &
        (TableSportsEvents.arr_instance_id == TableSportsLeagues.arr_instance_id)).join(
        TableArrInstances, TableSportsEvents.arr_instance_id == TableArrInstances.id).where(
        TableArrInstances.kind == 'sportarr')
    if enabled_only:
        query = query.where(TableArrInstances.enabled == 1)
    if arr_instance_id is not None:
        query = query.where(TableArrInstances.id == arr_instance_id)
    return query


def _serialize_event(row):
    event, profile, mappings = row
    result = event.to_dict()
    result.pop('ffprobe_cache')
    for name in ('created_at_timestamp', 'updated_at_timestamp'):
        if result[name] is not None:
            result[name] = result[name].isoformat()
    for name in ('audio_language', 'subtitles', 'missing_subtitles', 'failedAttempts'):
        try:
            value = ast.literal_eval(result[name] or '[]')
        except (SyntaxError, ValueError):
            value = []
        result[name] = value if isinstance(value, list) else []
    result['monitored'] = result['monitored'] == 'True'
    result['partNumber'] = result['partNumber'] or None
    return result | {'profileId': profile, 'hasFile': True,
                     'mapped_path': apply_sports_mapping(event.path, read_sports_mappings(mappings))}


def list_events(session, league_id, arr_instance_id=None, start=0, length=100):
    if start < 0 or not 1 <= length <= 1000:
        raise ValueError('Invalid pagination')
    league = get_league(session, league_id, arr_instance_id)
    if league is None:
        raise ValueError('League not found for this owner')
    query = _event_query(league['arr_instance_id']).where(TableSportsEvents.league_id == league_id)
    count = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = session.execute(query.order_by(TableSportsEvents.eventDate.desc(), TableSportsEvents.sportarrEventId,
                                         TableSportsEvents.partNumber, TableSportsEvents.id)
                           .offset(start).limit(length)).all()
    return {'data': [_serialize_event(row) for row in rows], 'total': count}


def get_event(session, event_id, arr_instance_id=None, enabled_only=True):
    row = session.execute(_event_query(arr_instance_id, enabled_only).where(
        TableSportsEvents.id == event_id)).first()
    return _serialize_event(row) if row else None

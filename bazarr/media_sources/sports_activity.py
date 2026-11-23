"""Page Wanted, History and Excluded records across the two sports origins."""

import sqlalchemy as sa

from app.config import settings
from app.database import TableArrInstances, TableBlacklistSports, TableHistorySports, TableSportsEvents, TableSportsLeagues
from app.event_handler import event_stream
from sportarr import history, workflows
from sportarr.pagination import validate_page

from . import repository
from .db import source_transaction
from .history import get_blacklist_source
from .union import includes_sources, union_page
from .wanted import wanted_list_items


_KIND = 'sports_event'
_METADATA = ('source_id', 'source_item_id', 'source_group_id', 'source_name', 'source_kind')


def _project(query, columns):
    if query is None:
        # A source-only list needs no Sportarr tables or guards.
        return sa.select(*(sa.cast(sa.null(), column.type).label(column.key) for column in columns)).where(sa.false())
    return query.with_only_columns(*columns, maintain_column_froms=True)


def _page_ids(page, origin):
    return [row.id for row in page if row.origin == origin]


def _mixed_response(page, total, arr, source):
    return {'total': total, 'data': [
        dict(item, origin=row.origin, source_key=row.source_key)
        for row in page if (item := (source if row.origin == 'source' else arr).get(row.id)) is not None]}


def _source_wanted(row):
    from .api_rows import postprocess_source

    return postprocess_source({
        'id': row.id, 'source_id': row.source_id, 'source_item_id': row.id, 'source_group_id': row.group_id,
        'source_name': row.source_name, 'source_kind': row.source_kind, 'identity_state': row.identity_state,
        'arr_instance_id': None, 'league_id': None, 'sportarrLeagueId': None, 'sportarrEventId': None, 'file_id': None,
        'leagueTitle': row.league_title, 'title': row.title,
        'season': row.season, 'episode': row.episode, 'eventDate': row.event_date, 'broadcastDate': None,
        'partNumber': row.part_number or None, 'partName': row.part_name,
        'path': row.path, 'mapped_path': row.path, 'hasFile': True, 'release_mismatch': False,
        'poster': row.poster, 'profileId': row.effective_profile, 'monitored': bool(row.monitored),
        'audio_language': row.audio_language, 'subtitles': row.subtitles,
        'missing_subtitles': row.missing_subtitles, 'shadowed_by': None,
    })


def list_wanted(session, args, scope, *, arr_instance_id, start, length):
    events = TableSportsEvents
    items, groups, libraries, sources = repository.Items, repository.Groups, repository.Libraries, repository.Sources
    if arr_instance_id is not None or not includes_sources(args, session, items, items.c.kind == _KIND):
        return workflows.list_wanted(session, arr_instance_id, start, length, **(
            {'owner_filter': scope.arr(events.arr_instance_id)} if scope.keys else {}))

    validate_page(start, length)
    arr_query = None
    if settings.general.use_sportarr and scope.includes('arr'):
        arr_query = workflows.wanted_query(session).where(scope.arr(events.arr_instance_id))
    arr = _project(arr_query, (
        events.id, events.arr_instance_id.label('_owner'), TableSportsLeagues.title.label('league_title'),
        sa.func.coalesce(sa.func.nullif(events.eventDate, ''), events.broadcastDate).label('event_date'),
        events.season, events.episode, events.partNumber.label('part_number'),
    ))
    source_query = wanted_list_items(
        _KIND, session, items.c.id, items.c.source_id.label('_owner'), groups.c.title.label('league_title'),
        items.c.event_date, items.c.season, items.c.episode, items.c.part_number,
    ).where(scope.source(items.c.source_id))
    page, total = union_page(
        session, arr, source_query, start=start, length=length, offset_unbounded=True,
        sort=lambda c: (sa.func.coalesce(c.league_title, ''), sa.func.coalesce(c.event_date, ''),
                        sa.func.coalesce(c.season, 0), sa.func.coalesce(c.episode, 0), c.part_number, c.origin, c.id))
    arr_ids, source_ids = _page_ids(page, 'arr'), _page_ids(page, 'source')
    arr_rows = workflows.list_wanted(
        session, length=-1, query=arr_query, owner_filter=events.id.in_(arr_ids))['data'] if arr_ids else []
    arr_data = {row['id']: dict(row, **dict.fromkeys(_METADATA)) for row in arr_rows}

    source_data = {}
    if source_ids:
        details = source_query.with_only_columns(
            *items.c, groups.c.title.label('league_title'), sources.c.name.label('source_name'),
            sources.c.kind.label('source_kind'),
            sa.func.coalesce(items.c.profile_id, groups.c.profile_id, libraries.c.profile_id).label('effective_profile'),
            maintain_column_froms=True,
        ).where(items.c.id.in_(source_ids))
        source_data = {row.id: _source_wanted(row) for row in session.execute(details)}
    return _mixed_response(page, total, arr_data, source_data)


def _source_activity_query(table, scope, *, language, provider, action, include_embedded):
    query = sa.select(table.c.id, table.c.source_id.label('_owner'), table.c.timestamp).where(
        table.c.item_kind == _KIND, scope.source(table.c.source_id))
    if language:
        query = query.where(table.c.language == language)
    if provider:
        query = query.where(table.c.provider == provider)
    if table is repository.History:
        if action is not None:
            query = query.where(table.c.action == action)
        if not include_embedded:
            query = query.where(table.c.action != 7)
    return query


def _source_activity(session, table, query, ids):
    items, sources = repository.Items, repository.Sources
    joined = table.outerjoin(items, table.c.item_id == items.c.id).outerjoin(sources, table.c.source_id == sources.c.id)
    details = query.with_only_columns(
        *table.c, items.c.group_id.label('source_group_id'), items.c.season, items.c.episode,
        items.c.part_number, items.c.part_name, sources.c.kind.label('source_kind'),
    ).select_from(joined).where(table.c.id.in_(ids))
    is_history = table is repository.History
    pairs = set(get_blacklist_source(_KIND, session)) if is_history else set()
    rows = {}
    for row in session.execute(details):
        data = dict(row._mapping)
        data.update(arr_instance_id=None, event_id=None, league_id=None, source_item_id=data.pop('item_id'),
                    title=data.pop('item_title'), leagueTitle=data.pop('group_title'),
                    partNumber=data.pop('part_number') or None, partName=data.pop('part_name'))
        data.pop('item_kind')
        if is_history:
            data.update(ai_translated=data['ai_translated'] == 1, upgradable=False,
                        blacklisted=(data['provider'], data['subs_id']) in pairs)
        rows[row.id] = history.format_activity(data, row.timestamp)
    return rows


def list_activity(session, kind, args, scope, **filters):
    table = repository.History if kind == 'history' else repository.Blacklist
    arr_table = TableHistorySports if kind == 'history' else TableBlacklistSports
    owner_filter = {'owner_filter': scope.arr(arr_table.arr_instance_id)} if scope.keys else {}
    if (any(filters[key] is not None for key in ('arr_instance_id', 'event_id', 'league_id'))
            or not includes_sources(args, session, table, table.c.item_kind == _KIND)):
        return history.list_records(session, kind, **filters, **owner_filter)

    start, length = filters.pop('start'), filters.pop('length')
    validate_page(start, length)
    arr_query = None
    if settings.general.use_sportarr and scope.includes('arr'):
        _, arr_query = history.records_query(session, kind, **filters, **owner_filter)
    arr = _project(arr_query, (arr_table.id, arr_table.arr_instance_id.label('_owner'), arr_table.timestamp))
    source_query = _source_activity_query(
        table, scope, language=filters['language'], provider=filters['provider'],
        action=filters['action'], include_embedded=filters['include_embedded'])
    page, total = union_page(
        session, arr, source_query, start=start, length=length, offset_unbounded=True,
        sort=lambda c: (c.timestamp.desc().nulls_last(), c.origin, c.id.desc()))
    arr_ids, source_ids = _page_ids(page, 'arr'), _page_ids(page, 'source')
    arr_rows = history.serialize_records_page(
        session, kind, arr_query.where(arr_table.id.in_(arr_ids))) if arr_ids else []
    arr_data = {row['id']: dict(row, **dict.fromkeys(_METADATA)) for row in arr_rows}
    source_data = _source_activity(session, table, source_query, source_ids) if source_ids else {}
    return _mixed_response(page, total, arr_data, source_data)


def clear_blacklist(session, scope):
    source = repository.Blacklist
    removed = 0
    with source_transaction(session) as connection:
        if scope.includes('source'):
            removed = connection.execute(sa.delete(source).where(
                source.c.item_kind == _KIND, scope.source(source.c.source_id),
            )).rowcount
        if not scope.keys and not removed:
            raise ValueError('arr_instance_id is required and must be a positive integer')
        if settings.general.use_sportarr and scope.includes('arr'):
            enabled = sa.select(TableArrInstances.id).where(
                TableArrInstances.kind == 'sportarr', TableArrInstances.enabled == 1)
            removed += connection.execute(sa.delete(TableBlacklistSports).where(
                TableBlacklistSports.arr_instance_id.in_(enabled), scope.arr(TableBlacklistSports.arr_instance_id),
            )).rowcount
    event_stream(type='sports')
    return removed


def remove_blacklist_entry(session, entry_id):
    table = repository.Blacklist
    removed = session.execute(sa.delete(table).where(table.c.item_kind == _KIND, table.c.id == entry_id)).rowcount
    if removed:
        event_stream(type='sports')
    return removed == 1

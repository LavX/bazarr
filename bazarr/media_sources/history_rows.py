# coding=utf-8
"""Page history across media origins while enriching only the selected rows."""

import ast

import sqlalchemy as sa
from flask_restx import fields

from api.swaggerui import subtitles_language_model
from api.utils import chunked
from utilities.pretty_date import pretty_date

from . import repository
from .api_rows import postprocess_source
from .history import get_upgradable_source_subtitles
from .union import SourceKeyFilter, union_page


original_history_fields = {
    'origin': fields.String(),
    'source_key': fields.String(),
    'history_id': fields.Integer(),
    'timestamp_iso': fields.String(),
    'action': fields.Integer(),
    'provider': fields.String(),
    'description': fields.String(),
}

history_origin_fields = {
    'origin': fields.String(),
    'source_key': fields.String(),
    'source_id': fields.Integer(),
    'source_name': fields.String(),
    'source_kind': fields.String(),
    'item_id': fields.Integer(),
    'item_kind': fields.String(),
    'identity_state': fields.String(),
    'shadowed_by': fields.String(),
    'item_title': fields.String(),
    'group_title': fields.String(),
    'video_path': fields.String(),
    'upgradedFromId': fields.Integer(),
    'original_id': fields.Integer(),
    'original': fields.Nested(original_history_fields, allow_null=True),
}

source_history_fields = {
    **history_origin_fields,
    'id': fields.Integer(),
    'history_id': fields.Integer(),
    'title': fields.String(),
    'series_id': fields.Integer(),
    'seriesTitle': fields.String(),
    'episodeTitle': fields.String(),
    'episode_number': fields.String(),
    'arr_instance_id': fields.Integer(),
    'radarrId': fields.Integer(),
    'sonarrSeriesId': fields.Integer(),
    'sonarrEpisodeId': fields.Integer(),
    'monitored': fields.Boolean(),
    'timestamp': fields.String(),
    'timestamp_iso': fields.String(),
    'parsed_timestamp': fields.String(),
    'action': fields.Integer(),
    'provider': fields.String(),
    'language': fields.Nested(subtitles_language_model, allow_null=True),
    'description': fields.String(),
    'subtitles_path': fields.String(),
    'ai_translated': fields.Boolean(),
    'subs_id': fields.String(),
    'score': fields.String(),
    'blacklisted': fields.Boolean(),
    'upgradable': fields.Boolean(),
    'matches': fields.List(fields.String),
    'dont_matches': fields.List(fields.String),
}


def source_history_conditions(kind, include_embedded=False):
    history = repository.History.c
    conditions = [history.item_kind == kind]
    if not include_embedded:
        conditions.append(history.action != 7)
    return conditions


def mixed_history_page(session, arr_keys, kind, args, *, arr_detail=False):
    """Fetch the timeline page in SQL, then hydrate each origin's selected IDs."""
    history = repository.History.c
    owner_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
    source = sa.select(history.id, history.source_id.label('_owner'),
                       sa.cast(history.timestamp, sa.Text).label('timestamp')).where(
        *source_history_conditions(kind, args.get('include_embedded')), owner_filter.source(history.source_id))
    if arr_detail:
        source = source.where(sa.false())
    return union_page(
        session, arr_keys, source, start=args.get('start'), length=args.get('length'),
        sort=lambda c: (c.timestamp.desc(), c.origin, c.id.desc()))


def arr_history_rows(session, stmt, id_column, refs):
    rows = []
    for batch in chunked([row.id for row in refs if row.origin == 'arr']):
        rows.extend(session.execute(stmt.where(id_column.in_(batch))).all())
    return rows


def merge_history_rows(session, refs, arr_rows, kind, *, arr_item_column, include_upgradable):
    _attach_history_originals(session, arr_rows, arr_item_column, origin='arr')
    arr_by_id = {row['history_id']: row for row in arr_rows}
    source_by_id = source_history_rows(
        session, [ref.id for ref in refs if ref.origin == 'source'], include_upgradable=include_upgradable)
    merged = []
    for ref in refs:
        data = (arr_by_id if ref.origin == 'arr' else source_by_id).get(ref.id)
        if data is not None:
            merged.append({**data, 'origin': ref.origin, 'source_key': ref.source_key})
    history = repository.History.c
    source_ids = session.scalars(sa.select(history.source_id).where(history.item_kind == kind).distinct())
    keys = sorted(f'source:{key if key is not None else "removed"}' for key in source_ids)
    return merged, keys


def _attach_history_originals(session, rows, item_column, *, origin):
    """Resolve explicit upgrade references within the same origin, recorded owner and item."""
    events = {row['history_id']: row for row in rows if row['upgradedFromId'] is not None}
    if not events:
        return

    table = item_column.table
    original_table = table.alias('original_history_event')
    event, original = table.c, original_table.c
    owner_key = 'source_id' if origin == 'source' else 'arr_instance_id'
    owner = event[owner_key]
    matches = [original.id == event.upgradedFromId, original.id != event.id,
               original[owner_key].is_not_distinct_from(owner), original[item_column.key] == item_column]
    if origin == 'source':
        # Removed sources share a filter key, which cannot establish common ownership.
        matches.extend((owner.is_not(None), original.item_kind == event.item_kind))
    stmt = sa.select(event.id.label('event_id'), owner.label('owner_id'), original.id, original.timestamp,
                     original.action, original.provider, original.description).select_from(
        table.join(original_table, sa.and_(*matches)))

    for batch in chunked(list(events)):
        counterparts = {row.event_id: row for row in session.execute(stmt.where(event.id.in_(batch)))}
        for event_id in batch:
            data = events[event_id]
            match = counterparts.get(event_id)
            if match is None:
                data.update(upgradedFromId=None, original_id=None)
                continue
            data['original'] = {
                'origin': origin, 'source_key': f'{origin}:{match.owner_id}' if match.owner_id is not None else None,
                'history_id': match.id, 'timestamp_iso': match.timestamp.isoformat(), 'action': match.action,
                'provider': match.provider, 'description': match.description,
            }


def _source_select(*conditions):
    history, items, sources, blacklist = (table.c for table in
                                           (repository.History, repository.Items, repository.Sources,
                                            repository.Blacklist))
    blacklisted = sa.exists().where(
        blacklist.item_kind == history.item_kind, blacklist.provider == history.provider,
        blacklist.subs_id == history.subs_id).correlate(repository.History)
    return sa.select(
        *history, items.id.label('live_item_id'), items.group_id, items.season, items.episode,
        items.monitored, items.identity_state,
        items.shadow_owner,
        sources.name.label('live_source_name'), sources.kind.label('source_kind'),
        blacklisted.label('blacklisted'),
    ).select_from(repository.History.outerjoin(
        repository.Items, sa.and_(history.item_id == items.id, history.source_id == items.source_id,
                                  history.item_kind == items.kind)
    ).outerjoin(repository.Sources, items.source_id == sources.id)).where(*conditions)


def source_history_rows(session, ids, *, include_upgradable=True):
    rows = []
    for batch in chunked(ids):
        rows.extend(session.execute(_source_select(repository.History.c.id.in_(batch))).all())
    return _serialize_source_history(session, rows, include_upgradable)


def _serialize_source_history(session, rows, include_upgradable):
    upgrades = {}
    if include_upgradable:
        for batch in chunked([row.id for row in rows if row.live_item_id is not None]):
            upgrades.update(get_upgradable_source_subtitles(history_id_list=batch, target=session))
    data = {row.id: _source_history_row(row, upgrades) for row in rows}
    _attach_history_originals(session, data.values(), repository.History.c.item_id, origin='source')
    return data


def item_history(session, item, *, include_embedded, include_upgradable, start, length):
    history, items = repository.History.c, repository.Items.c
    conditions = [history.item_id == item.id, history.source_id == item.source_id,
                  *source_history_conditions(item.kind, include_embedded)]
    stmt = _source_select(*conditions, items.id.is_not(None)).order_by(history.timestamp.desc(), history.id.desc())
    if length > 0:
        stmt = stmt.offset(start).limit(length)
    rows = session.execute(stmt).all()
    current_owner = sa.exists().where(items.id == item.id, items.source_id == item.source_id)
    total = session.scalar(sa.select(sa.func.count()).select_from(repository.History).where(*conditions, current_owner))
    return {'data': list(_serialize_source_history(session, rows, include_upgradable).values()), 'total': total}


def _source_history_row(row, upgrades):
    live_item_id = row.live_item_id
    episode_number = f'{row.season}x{row.episode}' if row.season is not None and row.episode is not None else None
    score = f'{round(row.score * 100 / row.score_out_of, 2)}%' if (
        row.score is not None and row.score_out_of) else row.score
    data = postprocess_source({
        'origin': 'source', 'source_key': f'source:{row.source_id or "removed"}', 'source_id': row.source_id,
        'source_name': row.live_source_name or row.source_name, 'source_kind': row.source_kind,
        'id': live_item_id, 'item_id': live_item_id, 'item_kind': row.item_kind,
        'identity_state': row.identity_state, 'shadowed_by': row.shadow_owner,
        'item_title': row.item_title, 'group_title': row.group_title, 'title': row.item_title,
        'series_id': row.group_id if live_item_id is not None else None,
        'seriesTitle': row.group_title, 'episodeTitle': row.item_title, 'episode_number': episode_number,
        'history_id': row.id, 'action': row.action, 'description': row.description, 'language': row.language,
        'provider': row.provider, 'ai_translated': bool(row.ai_translated), 'score': score, 'subs_id': row.subs_id,
        'subtitles_path': row.subtitles_path, 'video_path': row.video_path, 'monitored': row.monitored,
        'timestamp': pretty_date(row.timestamp), 'timestamp_iso': row.timestamp.isoformat(),
        'parsed_timestamp': row.timestamp.strftime('%x %X'), 'blacklisted': bool(live_item_id and row.blacklisted),
        'matches': ast.literal_eval(row.matched) if row.matched else [],
        'dont_matches': ast.literal_eval(row.not_matched) if row.not_matched else [],
        'upgradedFromId': row.upgradedFromId, 'original_id': upgrades.get(row.id),
        'upgradable': live_item_id is not None and row.id in upgrades,
    })
    if live_item_id is None:
        data['monitored'] = None
    return data

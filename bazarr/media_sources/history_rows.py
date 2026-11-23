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


def merge_history_rows(session, refs, arr_rows, kind, *, include_upgradable):
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


def _source_select(*conditions):
    history, items, sources, blacklist = (table.c for table in
                                           (repository.History, repository.Items, repository.Sources,
                                            repository.Blacklist))
    blacklisted = sa.exists().where(
        blacklist.item_kind == history.item_kind, blacklist.provider == history.provider,
        blacklist.subs_id == history.subs_id).correlate(repository.History)
    return sa.select(
        *history, items.group_id, items.season, items.episode, items.monitored, items.identity_state,
        items.shadow_owner,
        sources.name.label('live_source_name'), sources.kind.label('source_kind'),
        blacklisted.label('blacklisted'),
    ).select_from(repository.History.outerjoin(repository.Items, history.item_id == items.id)
                 .outerjoin(repository.Sources, history.source_id == sources.id)).where(*conditions)


def source_history_rows(session, ids, *, include_upgradable=True):
    rows, upgrades = [], {}
    for batch in chunked(ids):
        rows.extend(session.execute(_source_select(repository.History.c.id.in_(batch))).all())
        if include_upgradable:
            upgrades.update(get_upgradable_source_subtitles(history_id_list=batch, target=session))
    return {row.id: _source_history_row(row, upgrades) for row in rows}


def item_history(session, item, *, include_embedded, include_upgradable, start, length):
    history = repository.History.c
    conditions = [history.item_id == item.id, *source_history_conditions(item.kind, include_embedded)]
    stmt = sa.select(history.id).where(*conditions).order_by(history.timestamp.desc(), history.id.desc())
    if length > 0:
        stmt = stmt.offset(start).limit(length)
    ids = list(session.scalars(stmt))
    rows = source_history_rows(session, ids, include_upgradable=include_upgradable)
    total = session.scalar(sa.select(sa.func.count()).select_from(repository.History).where(*conditions))
    return {'data': [rows[history_id] for history_id in ids if history_id in rows], 'total': total}


def _source_history_row(row, upgrades):
    live_item_id = row.item_id if row.source_id is not None else None
    episode_number = f'{row.season}x{row.episode}' if row.season is not None and row.episode is not None else None
    score = f'{round(row.score * 100 / row.score_out_of, 2)}%' if (
        row.score is not None and row.score_out_of) else row.score
    return postprocess_source({
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
        'parsed_timestamp': row.timestamp.strftime('%x %X'), 'blacklisted': bool(row.blacklisted),
        'matches': ast.literal_eval(row.matched) if row.matched else [],
        'dont_matches': ast.literal_eval(row.not_matched) if row.not_matched else [],
        'upgradedFromId': row.upgradedFromId, 'original_id': upgrades.get(row.id),
        'upgradable': live_item_id is not None and row.id in upgrades,
    })

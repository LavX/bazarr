# coding=utf-8
"""Read and remove blacklist entries across the source and Arr tables."""

import sqlalchemy as sa
from flask_restx import marshal

from api.utils import postprocess
from app.event_handler import event_stream
from utilities.pretty_date import pretty_date

from . import repository
from .db import source_transaction
from .union import SourceKeyFilter, includes_sources, union_page


def list_blacklist(session, args, arr, arr_table, kind, model):
    blacklist = repository.Blacklist
    key_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
    arr = arr.where(key_filter.arr(arr_table.arr_instance_id))
    start, length = max(0, args['start']), args['length']
    kind_condition = blacklist.c.item_kind == kind
    mixed = includes_sources(args, session, blacklist, kind_condition)
    if mixed:
        items = repository.Items
        sources = repository.Sources
        arr = arr.add_columns(
            arr_table.id.label('_blacklist_id'), arr_table.arr_instance_id.label('_owner'),
            sa.cast(sa.null(), sa.Integer).label('source_item_id'),
            sa.cast(sa.null(), sa.Integer).label('source_group_id'),
            sa.cast(sa.null(), sa.Text).label('source_name'),
            sa.cast(sa.null(), sa.Text).label('source_kind'),
            sa.cast(sa.null(), sa.Text).label('video_path'))
        src = sa.select(
            blacklist.c.id, blacklist.c.id.label('_blacklist_id'), blacklist.c.source_id.label('_owner'),
            blacklist.c.item_id.label('source_item_id'), items.c.group_id.label('source_group_id'),
            sa.func.coalesce(sources.c.name, blacklist.c.source_name).label('source_name'),
            sources.c.kind.label('source_kind'), blacklist.c.video_path,
            blacklist.c.provider, blacklist.c.subs_id, blacklist.c.language, blacklist.c.timestamp,
        ).select_from(blacklist.outerjoin(items, blacklist.c.item_id == items.c.id)
                      .outerjoin(sources, blacklist.c.source_id == sources.c.id)
                      ).where(kind_condition, key_filter.source(blacklist.c.source_id))
        if kind == 'episode':
            src = src.add_columns(
                blacklist.c.group_title.label('seriesTitle'), blacklist.c.item_title.label('episodeTitle'),
                items.c.season.cast(sa.Text).concat('x').concat(items.c.episode.cast(sa.Text)).label('episode_number'))
        else:
            src = src.add_columns(blacklist.c.item_title.label('title'))
        rows, total = union_page(
            session, arr, src, start=start, length=length,
            sort=lambda c: (c.timestamp.desc().nulls_last(), c.origin, c._blacklist_id.desc()))
    else:
        arr = arr.order_by(arr_table.timestamp.desc())
        if length > 0:
            arr = arr.limit(length).offset(start)
        rows = session.execute(arr).all()
    data = []
    for row in rows:
        entry = dict(row._mapping)
        entry['timestamp'] = pretty_date(row.timestamp) if row.timestamp is not None else None
        entry['parsed_timestamp'] = row.timestamp.strftime('%x %X') if row.timestamp is not None else None
        parsed = marshal(postprocess(entry, source=entry.get('origin') == 'source'), model)
        if mixed:
            parsed.update(
                origin=row.origin, source_key=row.source_key, source_item_id=row.source_item_id,
                source_group_id=row.source_group_id, source_name=row.source_name, source_kind=row.source_kind,
                source_id=row._owner if row.origin == 'source' else None, video_path=row.video_path)
        data.append(parsed)
    return {'data': data, 'total': total} if mixed else {'data': data}


def add_blacklist_delete_arguments(parser):
    from .union import add_source_arguments

    add_source_arguments(parser)
    parser.add_argument('source_blacklist_id', type=int, location='args', help='The source blacklist entry ID')


def delete_blacklist(args, *, session, kind, arr_table, delete_one, delete_all):
    """Preserve legacy removal and apply a mixed bulk request in a single transaction."""
    blacklist = repository.Blacklist
    kind_condition = blacklist.c.item_kind == kind
    row_id = args.get('source_blacklist_id')
    all_rows = args.get('all') == 'true'
    mixed = args.get('sources') == '1'
    key_filter = SourceKeyFilter(tuple(args.get('source_key[]') or ()))
    if row_id is not None:
        if not mixed or all_rows or row_id <= 0:
            return {'message': 'A source blacklist entry requires sources=1 and a positive entry ID.'}, 400
        session.execute(sa.delete(blacklist).where(kind_condition, blacklist.c.id == row_id))
    elif all_rows:
        if not mixed and not key_filter.keys:
            delete_all()
            return None
        with source_transaction(session) as connection:
            connection.execute(sa.delete(arr_table).where(key_filter.arr(arr_table.arr_instance_id)))
            if mixed:
                connection.execute(sa.delete(blacklist).where(kind_condition, key_filter.source(blacklist.c.source_id)))
    else:
        delete_one(provider=args.get('provider'), subs_id=args.get('subs_id'),
                   arr_instance_id=args.get('arr_instance_id'))
        return None
    event_stream(type=f'{kind}-blacklist', action='delete')
    return None

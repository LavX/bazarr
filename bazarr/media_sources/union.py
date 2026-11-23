# coding=utf-8
"""Source opt-in, owner filters and SQL paging shared by media list endpoints."""

import re
from dataclasses import dataclass

import sqlalchemy as sa
from flask_restx import reqparse
from werkzeug.datastructures import MultiDict


def _source_key(value):
    match = re.fullmatch(r'(arr|source):([1-9][0-9]{0,9})', value)
    if match and int(match[2]) <= 2_147_483_647:
        return match[1], int(match[2])
    if value == 'source:removed':
        return 'source', None
    raise ValueError('Expected arr:<id>, source:<id> or source:removed')


class _OptedInSourceKey(reqparse.Argument):
    def source(self, request):
        if request.args.get('sources') != '1':
            return MultiDict()
        return super().source(request)


def add_source_arguments(parser, *, require_opt_in=False):
    parser.add_argument('sources', location='args', help='Set to 1 to include media sources')
    argument = _OptedInSourceKey if require_opt_in else parser.argument_class
    parser.add_argument(argument('source_key[]', type=_source_key, location='args', action='append',
                                 help='Filter by arr:<id>, source:<id> or source:removed'))


@dataclass(frozen=True)
class SourceKeyFilter:
    keys: tuple = ()

    def arr(self, owner_column):
        return self._predicate('arr', owner_column)

    def source(self, owner_column):
        return self._predicate('source', owner_column)

    def _predicate(self, origin, owner_column):
        if not self.keys:
            return sa.true()
        matches = {owner for kind, owner in self.keys if kind == origin}
        return sa.or_(sa.false(), owner_column.in_(matches - {None}),
                      owner_column.is_(None) if None in matches else sa.false())


def includes_sources(args, session, source_table, kind_condition):
    """Preserve the original response when this endpoint has no source rows of its kind."""
    return args.get('sources') == '1' and bool(session.scalar(
        sa.select(sa.exists().select_from(source_table).where(kind_condition))))


def union_page(session, arr, source, *, sort, start, length):
    """Page both origins together, counting in SQL. The Arr projection defines the common column types.

    Each select must expose its own local ID and source owner as `id` and `_owner`. Missing columns on
    the source side become typed NULLs. Sort expressions start with the endpoint's keys and may specify
    a descending ID tiebreak for newest-first endpoints.
    """
    columns = arr.selected_columns
    source_columns = source.selected_columns
    arr_rows = arr.add_columns(sa.literal('arr', sa.Text).label('origin'))
    source_rows = source.with_only_columns(
        *(sa.cast(source_columns.get(column.key, sa.null()), column.type).label(column.key) for column in columns),
        sa.literal('source', sa.Text).label('origin'), maintain_column_froms=True)
    rows = sa.union_all(arr_rows, source_rows).subquery()
    source_key = sa.case(
        (rows.c.origin == 'source',
         sa.literal('source:').concat(sa.func.coalesce(sa.cast(rows.c._owner, sa.Text), 'removed'))),
        else_=sa.literal('arr:').concat(sa.cast(rows.c._owner, sa.Text)),
    )
    page = sa.select(rows, source_key.label('source_key')).order_by(*sort(rows.c))
    if length > 0:
        page = page.offset(start).limit(length)
    return session.execute(page).all(), session.scalar(sa.select(sa.func.count()).select_from(rows))

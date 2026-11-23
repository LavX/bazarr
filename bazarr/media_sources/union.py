# coding=utf-8
"""Source opt-in, owner filters and SQL paging shared by media list endpoints."""

import re
from dataclasses import dataclass

import sqlalchemy as sa


def _source_key(value):
    match = re.fullmatch(r'(arr|source):([1-9][0-9]{0,9})', value)
    if match and int(match[2]) <= 2_147_483_647:
        return match[1], int(match[2])
    if value == 'source:removed':
        return 'source', None
    raise ValueError('Expected arr:<id>, source:<id> or source:removed')


def add_source_arguments(parser):
    parser.add_argument('sources', location='args', help='Set to 1 to include media sources')
    parser.add_argument('source_key[]', type=_source_key, location='args', action='append',
                        help='Filter by arr:<id>, source:<id> or source:removed')


@dataclass(frozen=True)
class SourceKeyFilter:
    keys: tuple = ()

    @classmethod
    def from_values(cls, values):
        return cls(tuple(_source_key(value) for value in values))

    def includes(self, origin):
        return not self.keys or any(kind == origin for kind, _ in self.keys)

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


def union_page(session, arr, source, *, sort, start, length, offset_unbounded=False):
    """Page both origins together, counting in SQL. The Arr projection defines the common column types.

    Each select must expose its own local ID and source owner as `id` and `_owner`. Missing columns on
    the source side become typed NULLs. Sort expressions start with the endpoint's keys and may specify
    a descending ID tiebreak for newest-first endpoints.
    """
    columns = arr.selected_columns
    source_columns = source.selected_columns
    def source_column(column):
        expression = source_columns.get(column.key, sa.null())
        # SQLite casts DATETIME text to its integer year. Matching datetime columns
        # already have the common type and should keep their stored value.
        if not (isinstance(column.type, sa.DateTime) and isinstance(expression.type, sa.DateTime)):
            expression = sa.cast(expression, column.type)
        return expression.label(column.key)

    arr_rows = arr.add_columns(sa.literal('arr', sa.Text).label('origin'))
    source_rows = source.with_only_columns(
        *(source_column(column) for column in columns),
        sa.literal('source', sa.Text).label('origin'), maintain_column_froms=True)
    rows = sa.union_all(arr_rows, source_rows).subquery()
    source_key = sa.case(
        (rows.c.origin == 'source',
         sa.literal('source:').concat(sa.func.coalesce(sa.cast(rows.c._owner, sa.Text), 'removed'))),
        else_=sa.literal('arr:').concat(sa.cast(rows.c._owner, sa.Text)),
    )
    page = sa.select(rows, source_key.label('source_key')).order_by(*sort(rows.c))
    if length > 0 or offset_unbounded:
        page = page.offset(start)
    if length > 0:
        page = page.limit(length)
    return session.execute(page).all(), session.scalar(sa.select(sa.func.count()).select_from(rows))

"""index the columns that point at series, episodes, movies and history

Revision ID: f8c3d1a7b926
Revises: b2c9e741a605
Create Date: 2026-09-28 12:00:00.000000

History and blacklist rows point at the series, episode or movie they are
about, and an upgrade's history row points at the one it replaced. None of
those columns had an index. Deleting a row the database has to find every row
pointing at it, on SQLite (which enforces the foreign keys) and on PostgreSQL
alike, so each deleted series, episode, movie or history row read the whole
history and blacklist tables. Deleting an instance together with its library
deletes all of them at once and took minutes on a real library, holding the
write lock throughout. One index per pointing column makes each lookup cheap.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f8c3d1a7b926'
down_revision = 'b2c9e741a605'
branch_labels = None
depends_on = None


# (index, table, column). The ORM declares the same set, so a fresh install
# and an upgraded one end up with the same indexes.
INDEXES = (
    ('ix_table_history_series_id', 'table_history', 'series_id'),
    ('ix_table_history_episode_id', 'table_history', 'episode_id'),
    ('ix_table_history_upgraded_from_id', 'table_history', 'upgradedFromId'),
    ('ix_table_history_movie_movie_id', 'table_history_movie', 'movie_id'),
    ('ix_table_history_movie_upgraded_from_id', 'table_history_movie', 'upgradedFromId'),
    ('ix_table_blacklist_series_id', 'table_blacklist', 'series_id'),
    ('ix_table_blacklist_episode_id', 'table_blacklist', 'episode_id'),
    ('ix_table_blacklist_movie_movie_id', 'table_blacklist_movie', 'movie_id'),
)


def create_library_link_indexes(bind):
    """Create the missing indexes. Returns the names it created.

    Written against a bind rather than through ``op`` so the same code runs on
    SQLite and PostgreSQL and the tests can call it directly. An index that
    already exists is left alone, so a re-run is a no-op. A table or column
    that is missing (a database adopted from upstream Bazarr before its columns
    are restored) is skipped rather than failing the upgrade: the startup index
    repair builds that index once the column is back.
    """
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    created = []
    for name, table, column in INDEXES:
        if table not in tables:
            continue
        if column not in {c['name'] for c in inspector.get_columns(table)}:
            continue
        if name in {index['name'] for index in inspector.get_indexes(table)}:
            continue
        reflected = sa.Table(table, sa.MetaData(), autoload_with=bind)
        sa.Index(name, reflected.c[column]).create(bind)
        created.append(name)
    return created


def drop_library_link_indexes(bind):
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for name, table, _column in INDEXES:
        if table not in tables:
            continue
        if name not in {index['name'] for index in inspector.get_indexes(table)}:
            continue
        reflected = sa.Table(table, sa.MetaData(), autoload_with=bind)
        for index in reflected.indexes:
            if index.name == name:
                index.drop(bind)


def upgrade():
    create_library_link_indexes(op.get_bind())


def downgrade():
    drop_library_link_indexes(op.get_bind())

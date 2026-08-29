"""history timestamp and wanted partial indexes

Revision ID: b8d2c5f1a604
Revises: a3f1c7d90b21
Create Date: 2026-08-29 22:00:00.000000

The history pages order by timestamp with no index behind it, so SQLite scans
and sorts the entire table before pagination; with the six-figure row counts
"Treat Embedded Subtitles as Downloaded" produces, one page costs seconds.
The wanted pages scan every media row (fat TEXT columns included) twice per
request, once for the page and once for the count. Four indexes fix both:
plain timestamp indexes on the two history tables, and partial indexes over
exactly the wanted predicate so both the page and the count walk the index
instead of the table.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8d2c5f1a604'
down_revision = 'a3f1c7d90b21'
branch_labels = None
depends_on = None


# The partial predicate must match the wanted queries verbatim, or neither
# planner will use the index.
WANTED_PREDICATE = "missing_subtitles IS NOT NULL AND missing_subtitles != '[]'"

# The default history view hides Embedded Source rows (action=7). Right after
# a library scan the newest rows are all embedded, so ordering by the plain
# timestamp index would walk the flood before finding 25 real events; these
# partial event indexes hold only the rows the default view shows.
EVENTS_PREDICATE = "action != 7"

INDEXES = (
    ('ix_table_history_timestamp', 'table_history', 'timestamp', None),
    ('ix_table_history_movie_timestamp', 'table_history_movie', 'timestamp', None),
    ('ix_table_history_events', 'table_history', 'timestamp', EVENTS_PREDICATE),
    ('ix_table_history_movie_events', 'table_history_movie', 'timestamp', EVENTS_PREDICATE),
    ('ix_table_episodes_wanted', 'table_episodes', 'sonarrEpisodeId', WANTED_PREDICATE),
    ('ix_table_movies_wanted', 'table_movies', 'radarrId', WANTED_PREDICATE),
)


def create_history_and_wanted_indexes(bind):
    """Create the missing indexes. Returns the names it created.

    Written against a bind rather than through ``op`` so the same code runs on
    SQLite and PostgreSQL and can be exercised directly by the tests. Re-running
    an already-applied upgrade is a no-op, and databases that acquired one of
    these indexes some other way (an adopted database, a manual fix) are left
    alone rather than crash-looping the boot migration.
    """
    inspector = sa.inspect(bind)
    created = []
    for name, table, column, predicate in INDEXES:
        if table not in inspector.get_table_names():
            continue
        existing = {index['name'] for index in inspector.get_indexes(table)}
        if name in existing:
            continue
        metadata = sa.MetaData()
        reflected = sa.Table(table, metadata, autoload_with=bind)
        kwargs = {}
        if predicate is not None:
            kwargs = {
                'sqlite_where': sa.text(predicate),
                'postgresql_where': sa.text(predicate),
            }
        sa.Index(name, reflected.c[column], **kwargs).create(bind)
        created.append(name)
    return created


def drop_history_and_wanted_indexes(bind):
    inspector = sa.inspect(bind)
    for name, table, _column, _predicate in INDEXES:
        if table not in inspector.get_table_names():
            continue
        existing = {index['name'] for index in inspector.get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)


def upgrade():
    create_history_and_wanted_indexes(op.get_bind())


def downgrade():
    drop_history_and_wanted_indexes(op.get_bind())

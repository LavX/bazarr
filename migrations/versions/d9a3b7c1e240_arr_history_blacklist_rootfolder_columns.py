"""multiple arr instances: additive history/blacklist/rootfolder columns

Revision ID: d9a3b7c1e240
Revises: c8f2e1a4b560
Create Date: 2026-06-10 14:00:00.000000

Adds nullable ownership/local-reference columns for the multiple Sonarr/Radarr
instances feature (#156):

- table_history:          arr_instance_id, series_id, episode_id
- table_history_movie:    arr_instance_id, movie_id
- table_blacklist:        arr_instance_id, series_id, episode_id
- table_blacklist_movie:  arr_instance_id, movie_id
- table_shows_rootfolder: arr_instance_id, upstream_rootfolder_id, local_rootfolder_id
- table_movies_rootfolder:arr_instance_id, upstream_rootfolder_id, local_rootfolder_id

Purely additive and nullable. No FK, NOT NULL, or backfill here. Each add is
guarded, so it no-ops on fresh installs that already have the columns from
create_all().
"""
from alembic import op
import sqlalchemy as sa


revision = 'd9a3b7c1e240'
down_revision = 'c8f2e1a4b560'
branch_labels = None
depends_on = None


_ADDITIONS = [
    ('table_history', 'arr_instance_id'),
    ('table_history', 'series_id'),
    ('table_history', 'episode_id'),
    ('table_history_movie', 'arr_instance_id'),
    ('table_history_movie', 'movie_id'),
    ('table_blacklist', 'arr_instance_id'),
    ('table_blacklist', 'series_id'),
    ('table_blacklist', 'episode_id'),
    ('table_blacklist_movie', 'arr_instance_id'),
    ('table_blacklist_movie', 'movie_id'),
    ('table_shows_rootfolder', 'arr_instance_id'),
    ('table_shows_rootfolder', 'upstream_rootfolder_id'),
    ('table_shows_rootfolder', 'local_rootfolder_id'),
    ('table_movies_rootfolder', 'arr_instance_id'),
    ('table_movies_rootfolder', 'upstream_rootfolder_id'),
    ('table_movies_rootfolder', 'local_rootfolder_id'),
]


def _columns(insp, table):
    return {c['name'] for c in insp.get_columns(table)}


def upgrade():
    insp = sa.inspect(op.get_context().bind)
    tables = set(insp.get_table_names())
    for table, column in _ADDITIONS:
        if table not in tables:
            continue
        if column not in _columns(insp, table):
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_context().bind)
    tables = set(insp.get_table_names())
    for table, column in reversed(_ADDITIONS):
        if table not in tables:
            continue
        if column in _columns(insp, table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)

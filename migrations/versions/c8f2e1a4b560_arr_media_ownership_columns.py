"""multiple arr instances: additive media ownership columns

Revision ID: c8f2e1a4b560
Revises: b7e1a9c4d230
Create Date: 2026-06-10 13:00:00.000000

Adds nullable ownership/local-identity columns to the media tables for the
multiple Sonarr/Radarr instances feature (#156):

- table_shows:    id, arr_instance_id
- table_episodes: id, arr_instance_id, series_id
- table_movies:   id, arr_instance_id

Purely additive and nullable. No FK, NOT NULL, or backfill here; those land in
later increments once the legacy writers populate the columns. Each add is
guarded by an existence check, so it no-ops on fresh installs that already have
the columns from create_all().
"""
from alembic import op
import sqlalchemy as sa


revision = 'c8f2e1a4b560'
down_revision = 'b7e1a9c4d230'
branch_labels = None
depends_on = None


_ADDITIONS = [
    ('table_shows', 'id'),
    ('table_shows', 'arr_instance_id'),
    ('table_episodes', 'id'),
    ('table_episodes', 'arr_instance_id'),
    ('table_episodes', 'series_id'),
    ('table_movies', 'id'),
    ('table_movies', 'arr_instance_id'),
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

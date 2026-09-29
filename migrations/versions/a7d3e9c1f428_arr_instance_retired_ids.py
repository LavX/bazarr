"""remember the ids of deleted Sonarr, Radarr and Sportarr instances

Revision ID: a7d3e9c1f428
Revises: f8c3d1a7b926
Create Date: 2026-09-29 12:00:00.000000

SQLite gives a new row the highest id plus one, so once the instance with the
highest id is deleted, the next one added takes its id. A write that was
already past its instance lookup when the delete committed can land after
that, naming the deleted instance, and the new one would own the row. The ids
of deleted instances are kept here, and a new instance's id is chosen above
them.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a7d3e9c1f428'
down_revision = 'f8c3d1a7b926'
branch_labels = None
depends_on = None


TABLE = 'arr_instance_retired_ids'


def create_retired_ids_table(bind):
    """Create the table unless it is there, and return whether it did.

    Written against a bind rather than through ``op`` so the tests can call it
    directly. The ORM declares the same table and startup creates the missing
    ones before the migrations run, so there it is usually a no-op.
    """
    if TABLE in sa.inspect(bind).get_table_names():
        return False
    sa.Table(TABLE, sa.MetaData(),
             sa.Column('id', sa.Integer(), primary_key=True, autoincrement=False)).create(bind)
    return True


def upgrade():
    create_retired_ids_table(op.get_bind())


def downgrade():
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)

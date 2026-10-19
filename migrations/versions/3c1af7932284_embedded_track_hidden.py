"""Embedded subtitle tracks the user hid, per media row.

Revision ID: 3c1af7932284
Revises: a7d3e9c1f428
"""

from alembic import op
import sqlalchemy as sa

revision = '3c1af7932284'
down_revision = 'a7d3e9c1f428'
branch_labels = None
depends_on = None

TABLE_NAME = 'table_embedded_track_hidden'
INDEX_NAME = 'ux_embedded_track_hidden_item'


def create_embedded_track_hidden(bind):
    """Create the table if it is not there yet. Returns True when it created it.

    Written against a bind rather than through ``op`` so the same code runs on
    SQLite and PostgreSQL and the tests can call it directly. A fresh install
    has already made the table through create_all, so this is a no-op there.
    """
    if TABLE_NAME in sa.inspect(bind).get_table_names():
        return False

    metadata = sa.MetaData()
    table = sa.Table(
        TABLE_NAME, metadata,
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('media_type', sa.Text(), nullable=False),
        sa.Column('media_id', sa.Integer(), nullable=False),
        sa.Column('arr_instance_id', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('language', sa.Text(), nullable=False),
        sa.Column('codec', sa.Text(), nullable=True),
        sa.Column('hidden_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("media_type IN ('movie', 'episode', 'sports')",
                           name='ck_embedded_track_hidden_media_type'),
        sa.Index(INDEX_NAME, 'media_type', 'media_id', 'arr_instance_id', 'language', unique=True),
    )
    table.create(bind)
    return True


def upgrade():
    create_embedded_track_hidden(op.get_bind())


def downgrade():
    bind = op.get_bind()
    if TABLE_NAME in sa.inspect(bind).get_table_names():
        op.drop_table(TABLE_NAME)

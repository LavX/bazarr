"""release type mismatches

Revision ID: a3f1c7d90b21
Revises: e7f4c9d80abc
Create Date: 2026-08-25 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a3f1c7d90b21'
down_revision = 'e7f4c9d80abc'
branch_labels = None
depends_on = None


TABLE_NAME = 'release_type_mismatches'
INDEX_NAME = 'ix_release_type_mismatches_item'


def create_release_type_mismatches(bind):
    """Create the table if it is not there yet. Returns True when it created it.

    Written against a bind rather than through ``op`` so the same code runs on
    SQLite and PostgreSQL and can be exercised directly by the tests. Re-running
    an already-applied upgrade is a no-op.
    """
    inspector = sa.inspect(bind)
    if TABLE_NAME in inspector.get_table_names():
        return False

    metadata = sa.MetaData()
    table = sa.Table(
        TABLE_NAME, metadata,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('media_type', sa.Text(), nullable=False),
        sa.Column('media_id', sa.Integer(), nullable=False),
        sa.Column('arr_instance_id', sa.Integer(), nullable=True),
        sa.Column('language', sa.Text(), nullable=False),
        sa.Column('video_release_type', sa.Text(), nullable=False),
        sa.Column('subtitle_release_type', sa.Text(), nullable=False),
        sa.Column('provider', sa.Text(), nullable=True),
        sa.Column('release_info', sa.Text(), nullable=True),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('detected_at', sa.DateTime(), nullable=False),
        # One recorded detection per item, owning instance, searched language
        # and the release type the item itself is. This is what keeps a repeated
        # scheduled pass from notifying again.
        sa.Index(INDEX_NAME, 'media_type', 'media_id', 'arr_instance_id', 'language',
                 'video_release_type', unique=True),
    )
    table.create(bind)
    return True


def upgrade():
    create_release_type_mismatches(op.get_bind())


def downgrade():
    pass

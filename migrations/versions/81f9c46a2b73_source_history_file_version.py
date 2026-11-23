"""Persist the exact file version published by a source provider download."""

from alembic import op
import sqlalchemy as sa

revision = '81f9c46a2b73'
down_revision = '2a7e4c8f109b'
branch_labels = None
depends_on = None


def upgrade():
    if 'published_file_version' not in {
            column['name'] for column in sa.inspect(op.get_bind()).get_columns('table_source_history')}:
        op.add_column('table_source_history', sa.Column('published_file_version', sa.JSON()))


def downgrade():
    op.drop_column('table_source_history', 'published_file_version')

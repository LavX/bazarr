"""empty message

Revision ID: 7e9a2b1c4d5f
Revises: f2f74f2d6d0a
Create Date: 2026-05-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7e9a2b1c4d5f'
down_revision = 'f2f74f2d6d0a'
branch_labels = None
depends_on = None

bind = op.get_context().bind
insp = sa.inspect(bind)


def column_exists(table_name, column_name):
    columns = insp.get_columns(table_name)
    return any(c["name"] == column_name for c in columns)


def upgrade():
    if not column_exists('table_shows', 'originalLanguage'):
        with op.batch_alter_table('table_shows', schema=None) as batch_op:
            batch_op.add_column(sa.Column('originalLanguage', sa.Text(), nullable=True))
    if not column_exists('table_movies', 'originalLanguage'):
        with op.batch_alter_table('table_movies', schema=None) as batch_op:
            batch_op.add_column(sa.Column('originalLanguage', sa.Text(), nullable=True))


def downgrade():
    pass

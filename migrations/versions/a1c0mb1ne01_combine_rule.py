"""language profile combine rule

Revision ID: a1c0mb1ne01
Revises: 6c9f1b8d2e3a
Create Date: 2026-06-02 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1c0mb1ne01'
down_revision = '6c9f1b8d2e3a'
branch_labels = None
depends_on = None


def _column_exists(inspector, table_name, column_name):
    return column_name in [c['name'] for c in inspector.get_columns(table_name)]


def upgrade():
    insp = sa.inspect(op.get_context().bind)

    if 'table_languages_profiles' in insp.get_table_names():
        if not _column_exists(insp, 'table_languages_profiles', 'combine'):
            op.add_column(
                'table_languages_profiles',
                sa.Column('combine', sa.Text(), nullable=True),
            )


def downgrade():
    pass

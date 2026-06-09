"""distribution hub: api keys + usage

Revision ID: d15706c70b01
Revises: a1c0mb1ne01
Create Date: 2026-06-02 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd15706c70b01'
down_revision = 'a1c0mb1ne01'
branch_labels = None
depends_on = None


def _table_exists(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = sa.inspect(op.get_context().bind)

    if not _table_exists(insp, 'compat_api_keys'):
        op.create_table(
            'compat_api_keys',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.Text(), nullable=False),
            sa.Column('key_prefix', sa.Text(), nullable=True),
            sa.Column('key_hash', sa.Text(), nullable=False),
            sa.Column('tier', sa.Text(), nullable=False, server_default='free'),
            sa.Column('custom_limits', sa.Text(), nullable=True),
            sa.Column('excluded_providers', sa.Text(), nullable=True),
            sa.Column('timeout_seconds', sa.Integer(), nullable=True),
            sa.Column('enabled', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('is_legacy', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
        )
        op.create_index('ix_compat_api_keys_key_hash', 'compat_api_keys',
                        ['key_hash'], unique=True)

    if not _table_exists(insp, 'compat_usage'):
        op.create_table(
            'compat_usage',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('key_id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.Text(), nullable=False),
            sa.Column('hour_start', sa.DateTime(), nullable=False),
            sa.Column('count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('blocked', sa.Integer(), nullable=False, server_default='0'),
        )
        op.create_index('ix_compat_usage_key_id', 'compat_usage', ['key_id'])
        op.create_index('ix_compat_usage_key_kind_hour', 'compat_usage',
                        ['key_id', 'kind', 'hour_start'], unique=True)


def downgrade():
    pass

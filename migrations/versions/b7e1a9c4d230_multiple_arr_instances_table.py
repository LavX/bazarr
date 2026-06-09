"""multiple arr instances: arr_instances table (additive)

Revision ID: b7e1a9c4d230
Revises: d15706c70b02
Create Date: 2026-06-10 12:00:00.000000

First migration of the multiple Sonarr/Radarr instances feature (#156). Adds
the ``arr_instances`` table only. Purely additive: existing single-instance
installs are untouched until later phases backfill a default instance and add
``arr_instance_id`` ownership columns.

Fresh installs already have the table from the ORM ``create_all()`` that runs
before migrations, so the create is guarded by an existence check and no-ops
there. Existing installs stamped at d15706c70b02 get the table created here.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7e1a9c4d230'
down_revision = 'd15706c70b02'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_context().bind)
    if 'arr_instances' in insp.get_table_names():
        return  # fresh installs already have it from create_all()

    op.create_table(
        'arr_instances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('stable_key', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('is_default', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('ip', sa.Text(), nullable=False, server_default='127.0.0.1'),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('base_url', sa.Text(), nullable=False, server_default='/'),
        sa.Column('ssl', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('verify_ssl', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('http_timeout', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('api_key', sa.Text(), nullable=False, server_default=''),
        sa.Column('options', sa.Text(), nullable=True),
        sa.Column('path_mappings', sa.Text(), nullable=True),
        sa.Column('schedule', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_sync_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("kind IN ('sonarr', 'radarr')", name='ck_arr_instances_kind'),
        sa.CheckConstraint("enabled IN (0, 1)", name='ck_arr_instances_enabled'),
        sa.CheckConstraint("is_default IN (0, 1)", name='ck_arr_instances_is_default'),
        sa.CheckConstraint("is_default = 0 OR enabled = 1", name='ck_arr_instances_default_enabled'),
    )
    op.create_index('ux_arr_instances_kind_stable_key', 'arr_instances',
                    ['kind', 'stable_key'], unique=True)
    op.create_index('ux_arr_instances_default_kind', 'arr_instances', ['kind'],
                    unique=True,
                    sqlite_where=sa.text('is_default = 1 AND enabled = 1'),
                    postgresql_where=sa.text('is_default = 1 AND enabled = 1'))


def downgrade():
    insp = sa.inspect(op.get_context().bind)
    if 'arr_instances' not in insp.get_table_names():
        return
    op.drop_index('ux_arr_instances_default_kind', table_name='arr_instances')
    op.drop_index('ux_arr_instances_kind_stable_key', table_name='arr_instances')
    op.drop_table('arr_instances')

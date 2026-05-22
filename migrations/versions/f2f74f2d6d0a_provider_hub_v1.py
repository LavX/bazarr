"""provider hub v1

Revision ID: f2f74f2d6d0a
Revises: 4bb94a033f93
Create Date: 2026-05-16 23:52:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f2f74f2d6d0a'
down_revision = '4bb94a033f93'
branch_labels = None
depends_on = None


def table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def upgrade():
    # Alembic imports every revision module up-front to build its graph; the
    # `op` context isn't bound yet at that point, so resolving the bind at
    # module top raises NameError and blocks migrate_db on a fresh database.
    # Defer the inspector to upgrade() where the operation context is live.
    insp = sa.inspect(op.get_context().bind)

    if not table_exists(insp, 'provider_hub_catalog_sources'):
        op.create_table(
            'provider_hub_catalog_sources',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.Text(), nullable=False, unique=True),
            sa.Column('type', sa.Text(), nullable=False),
            sa.Column('url', sa.Text(), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False, default=1),
            sa.Column('trust_key', sa.Text(), nullable=True),
            sa.Column('etag', sa.Text(), nullable=True),
            sa.Column('last_checked_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
        )

    if not table_exists(insp, 'provider_hub_catalog_entries'):
        op.create_table(
            'provider_hub_catalog_entries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('source_id', sa.Integer(), sa.ForeignKey('provider_hub_catalog_sources.id', ondelete='CASCADE')),
            sa.Column('provider_id', sa.Text(), nullable=False),
            sa.Column('version', sa.Text(), nullable=False),
            sa.Column('manifest_json', sa.Text(), nullable=False),
            sa.Column('bundle_url', sa.Text(), nullable=True),
            sa.Column('sha256', sa.Text(), nullable=True),
            sa.Column('signature', sa.Text(), nullable=True),
            sa.Column('compatibility', sa.Text(), nullable=True),
            sa.Column('published_at', sa.DateTime(), nullable=True),
            sa.Column('deprecated', sa.Integer(), nullable=False, default=0),
        )
        op.create_index('ix_provider_hub_catalog_entries_source_id', 'provider_hub_catalog_entries', ['source_id'])
        op.create_index('ix_provider_hub_catalog_entries_provider_id', 'provider_hub_catalog_entries', ['provider_id'])

    if not table_exists(insp, 'provider_hub_installations'):
        op.create_table(
            'provider_hub_installations',
            sa.Column('provider_id', sa.Text(), primary_key=True),
            sa.Column('active_version', sa.Text(), nullable=True),
            sa.Column('staged_version', sa.Text(), nullable=True),
            sa.Column('active_path', sa.Text(), nullable=True),
            sa.Column('staged_path', sa.Text(), nullable=True),
            sa.Column('state', sa.Text(), nullable=False, default='inactive'),
            sa.Column('pending_restart', sa.Integer(), nullable=False, default=0),
            sa.Column('installed_at', sa.DateTime(), nullable=True),
            sa.Column('activated_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('manifest_json', sa.Text(), nullable=True),
        )

    if not table_exists(insp, 'provider_hub_config'):
        op.create_table(
            'provider_hub_config',
            sa.Column('provider_id', sa.Text(), primary_key=True),
            sa.Column('enabled', sa.Integer(), nullable=False, default=0),
            sa.Column('priority', sa.Integer(), nullable=True),
            sa.Column('config_json', sa.Text(), nullable=False, default='{}'),
            sa.Column('schema_version', sa.Integer(), nullable=False, default=1),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )

    if not table_exists(insp, 'provider_hub_secrets'):
        op.create_table(
            'provider_hub_secrets',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('provider_id', sa.Text(), nullable=False),
            sa.Column('field', sa.Text(), nullable=False),
            sa.Column('encrypted_value', sa.Text(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_provider_hub_secrets_provider_id', 'provider_hub_secrets', ['provider_id'])

    if not table_exists(insp, 'provider_hub_jobs'):
        op.create_table(
            'provider_hub_jobs',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('provider_id', sa.Text(), nullable=True),
            sa.Column('action', sa.Text(), nullable=False),
            sa.Column('state', sa.Text(), nullable=False),
            sa.Column('message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_provider_hub_jobs_provider_id', 'provider_hub_jobs', ['provider_id'])

    if not table_exists(insp, 'provider_hub_install_events'):
        op.create_table(
            'provider_hub_install_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('provider_id', sa.Text(), nullable=False),
            sa.Column('job_id', sa.Text(), nullable=True),
            sa.Column('action', sa.Text(), nullable=False),
            sa.Column('state', sa.Text(), nullable=False),
            sa.Column('message', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        op.create_index('ix_provider_hub_install_events_provider_id', 'provider_hub_install_events', ['provider_id'])
        op.create_index('ix_provider_hub_install_events_job_id', 'provider_hub_install_events', ['job_id'])


def downgrade():
    pass

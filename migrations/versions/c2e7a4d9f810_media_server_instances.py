"""Independent native media server destinations.

Revision ID: c2e7a4d9f810
Revises: b8d2c5f1a604
"""

from alembic import op
import sqlalchemy as sa

revision = 'c2e7a4d9f810'
down_revision = 'b8d2c5f1a604'
branch_labels = None
depends_on = None


def create_media_server_tables(bind):
    metadata = sa.MetaData()
    sa.Table('media_server_instances', metadata,
             sa.Column('id', sa.Text, primary_key=True),
             sa.Column('kind', sa.Text, nullable=False),
             sa.Column('name', sa.Text, nullable=False),
             sa.Column('enabled', sa.Integer, nullable=False, server_default='0'),
             sa.Column('url', sa.Text, nullable=False),
             sa.Column('api_key', sa.Text, nullable=False, server_default=''),
             sa.Column('verify_ssl', sa.Integer, nullable=False, server_default='1'),
             sa.Column('path_mappings', sa.Text, nullable=False, server_default='[]'),
             sa.Column('revision', sa.Integer, nullable=False, server_default='1'),
             sa.CheckConstraint("kind IN ('emby', 'silo')", name='ck_media_server_kind'),
             sa.CheckConstraint('enabled IN (0, 1)', name='ck_media_server_enabled'),
             sa.CheckConstraint('verify_ssl IN (0, 1)', name='ck_media_server_verify_ssl'))
    sa.Table('media_server_imports', metadata,
             sa.Column('kind', sa.Text, primary_key=True),
             sa.CheckConstraint("kind IN ('emby', 'silo')", name='ck_media_server_import_kind'))
    metadata.create_all(bind)


def upgrade():
    create_media_server_tables(op.get_bind())


def downgrade():
    pass

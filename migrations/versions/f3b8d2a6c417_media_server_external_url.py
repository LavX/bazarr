"""The address a browser opens each media server destination at.

Revision ID: f3b8d2a6c417
Revises: a7d3e9c1f428
"""

from alembic import op
import sqlalchemy as sa

revision = "f3b8d2a6c417"
down_revision = "3c1af7932284"
branch_labels = None
depends_on = None

_TABLE = "media_server_instances"


def _has_external_url():
    inspector = sa.inspect(op.get_bind())
    return (_TABLE in inspector.get_table_names()
            and "external_url" in {column["name"] for column in inspector.get_columns(_TABLE)})


def upgrade():
    # A fresh install builds the table from the ORM before walking the chain,
    # so the column can already be there, and re-adding it would abort startup.
    if not _has_external_url():
        op.add_column(_TABLE, sa.Column("external_url", sa.Text(), nullable=True))


def downgrade():
    if _has_external_url():
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column("external_url")

"""distribution hub: per-key allowed_providers (allow-list)

Revision ID: d15706c70b02
Revises: d15706c70b01
Create Date: 2026-06-03 12:00:00.000000

Adds the compat_api_keys.allowed_providers column (the inverse of
excluded_providers: restrict a key to ONLY these providers). A separate,
append-only revision rather than an edit to d15706c70b01 because that revision
already shipped to RC deployments - alembic would never re-run an applied
revision, so the column has to come in via a new one.

Fresh installs already have the column from the ORM create_all() that runs
before migrations, so the add is guarded by an existence check and no-ops
there. Existing installs stamped at d15706c70b01 get the column added here.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd15706c70b02'
down_revision = 'd15706c70b01'
branch_labels = None
depends_on = None


def _column_exists(insp, table, column):
    return column in {c['name'] for c in insp.get_columns(table)}


def upgrade():
    insp = sa.inspect(op.get_context().bind)
    if 'compat_api_keys' not in insp.get_table_names():
        return  # down_revision creates it; defensive no-op if it somehow isn't there
    if not _column_exists(insp, 'compat_api_keys', 'allowed_providers'):
        op.add_column('compat_api_keys',
                      sa.Column('allowed_providers', sa.Text(), nullable=True))


def downgrade():
    pass

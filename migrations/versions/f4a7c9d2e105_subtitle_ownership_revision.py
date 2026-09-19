"""Track every native and sports ownership write transactionally.

Revision ID: f4a7c9d2e105
Revises: e7a9c2d4b601
"""

from alembic import op
from app.ownership_revision import install_ownership_revision, remove_ownership_revision

revision = "f4a7c9d2e105"
down_revision = "e7a9c2d4b601"
branch_labels = None
depends_on = None


def upgrade():
    install_ownership_revision(op.get_bind())


def downgrade():
    remove_ownership_revision(op.get_bind())

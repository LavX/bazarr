"""Persist sports subtitle publication identity.

Revision ID: e7a9c2d4b601
Revises: d3f6a8b1e902
"""

from alembic import op
import sqlalchemy as sa

revision = "e7a9c2d4b601"
down_revision = "d3f6a8b1e902"
branch_labels = None
depends_on = None


def upgrade():
    if "artifact" not in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("table_history_sports")
    }:
        op.add_column(
            "table_history_sports", sa.Column("artifact", sa.Text(), nullable=True)
        )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM table_history_sports WHERE artifact IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise ValueError(
            "Cannot remove sports artifact proof while publication records use it"
        )
    op.drop_column("table_history_sports", "artifact")

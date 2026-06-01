"""subsync engine failures

Revision ID: 6c9f1b8d2e3a
Revises: 7e9a2b1c4d5f
Create Date: 2026-05-27 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "6c9f1b8d2e3a"
down_revision = "7e9a2b1c4d5f"
branch_labels = None
depends_on = None


def table_exists(inspector, table_name):
    return table_name in inspector.get_table_names()


def upgrade():
    insp = sa.inspect(op.get_context().bind)

    if not table_exists(insp, "subsync_engine_failures"):
        op.create_table(
            "subsync_engine_failures",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("subtitle_path", sa.Text(), nullable=False),
            sa.Column("engine", sa.Text(), nullable=False),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, default=0),
            sa.Column("is_skipped", sa.Integer(), nullable=False, default=0),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_subsync_engine_failures_path_engine",
            "subsync_engine_failures",
            ["subtitle_path", "engine"],
            unique=True,
        )


def downgrade():
    pass

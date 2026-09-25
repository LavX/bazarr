"""Record provider AI translation provenance on history rows.

Revision ID: b2c9e741a605
Revises: e5b7d1c93a24
"""

from alembic import op
import sqlalchemy as sa

revision = "b2c9e741a605"
down_revision = "e5b7d1c93a24"
branch_labels = None
depends_on = None

_TABLES = ("table_history", "table_history_movie", "table_history_sports")


def _has_ai_translated_column(table):
    return "ai_translated" in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def upgrade():
    for table in _TABLES:
        if not _has_ai_translated_column(table):
            op.add_column(
                table, sa.Column("ai_translated", sa.Boolean(), nullable=True)
            )


def downgrade():
    for table in _TABLES:
        if _has_ai_translated_column(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("ai_translated")

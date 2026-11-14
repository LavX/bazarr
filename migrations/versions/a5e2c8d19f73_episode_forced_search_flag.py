"""A per-episode request for a forced duplicate subtitle search.

Revision ID: a5e2c8d19f73
Revises: f3b8d2a6c417
"""

from alembic import op
import sqlalchemy as sa

revision = "a5e2c8d19f73"
down_revision = "f3b8d2a6c417"
branch_labels = None
depends_on = None

_TABLE = "table_episodes"
_COLUMN = "forced_search_requested_at"
_INDEX = "ix_table_episodes_forced_search"
_PENDING = sa.text(f"{_COLUMN} IS NOT NULL")


def _has_table():
    return _TABLE in sa.inspect(op.get_bind()).get_table_names()


def _has_column():
    inspector = sa.inspect(op.get_bind())
    return (_TABLE in inspector.get_table_names()
            and _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)})


def _has_index():
    inspector = sa.inspect(op.get_bind())
    return (_TABLE in inspector.get_table_names()
            and _INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)})


def upgrade():
    # A fresh install builds the table from the ORM before walking the chain,
    # so the column and the index can already be there, and adding either again
    # would abort startup.
    if not _has_table():
        return
    if not _has_column():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(), nullable=True))
    if not _has_index():
        op.create_index(_INDEX, _TABLE, ["id"], sqlite_where=_PENDING, postgresql_where=_PENDING)


def downgrade():
    # The index goes first: SQLite refuses to drop an indexed column. The column
    # is a plain DROP COLUMN, not a batch rebuild: rebuilding the episode table
    # on SQLite would drop and recreate a table other tables cascade from.
    if _has_index():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column():
        op.drop_column(_TABLE, _COLUMN)

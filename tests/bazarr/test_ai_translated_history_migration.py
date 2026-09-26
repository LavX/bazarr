import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

TABLES = ("table_history", "table_history_movie", "table_history_sports")
REVISION = "b2c9e741a605"


def _migration():
    path = Path(__file__).parents[2] / (
        "migrations/versions/b2c9e741a605_ai_translated_history.py"
    )
    spec = importlib.util.spec_from_file_location("ai_translated_history_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exercise(connection):
    for table in TABLES:
        connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text(f"INSERT INTO {table} (id) VALUES (1)"))
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    migration = _migration()
    assert migration.revision == REVISION
    assert migration.down_revision == "e5b7d1c93a24"
    with Operations.context(context):
        migration.upgrade()
    for table in TABLES:
        column = next(c for c in sa.inspect(connection).get_columns(table)
                      if c["name"] == "ai_translated")
        assert column["nullable"] is True
        assert column["default"] is None
        assert connection.execute(sa.text(f"SELECT ai_translated FROM {table} WHERE id=1")).scalar() is None
        connection.execute(sa.text(
            f"INSERT INTO {table} (id, ai_translated) VALUES (2, true), (3, false)"
        ))
        assert connection.execute(sa.text(
            f"SELECT ai_translated FROM {table} WHERE id=2"
        )).scalar() in (True, 1)
    with Operations.context(context):
        migration.downgrade()
    for table in TABLES:
        assert "ai_translated" not in {
            c["name"] for c in sa.inspect(connection).get_columns(table)
        }
    with Operations.context(context):
        migration.upgrade()
    for table in TABLES:
        assert "ai_translated" in {
            c["name"] for c in sa.inspect(connection).get_columns(table)
        }
    # Fresh installations create the current ORM schema before walking the
    # migration chain. Re-running the additive migration against that schema
    # must be a no-op instead of aborting startup with a duplicate column.
    with Operations.context(context):
        migration.upgrade()
    for table in TABLES:
        assert [c["name"] for c in sa.inspect(connection).get_columns(table)].count("ai_translated") == 1


def test_sqlite_ai_history_migration(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    try:
        with engine.begin() as connection:
            _exercise(connection)
    finally:
        engine.dispose()


def test_postgres_ai_history_migration():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    engine = sa.create_engine(url)
    schema = "ai_history_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
            connection.execute(sa.text(f"SET search_path TO {schema}"))
            _exercise(connection)
            connection.execute(sa.text("SET search_path TO public"))
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
    finally:
        engine.dispose()


def test_fresh_history_schema_has_nullable_ai_flag():
    from app.database import TableHistory, TableHistoryMovie, TableHistorySports

    for model in (TableHistory, TableHistoryMovie, TableHistorySports):
        column = model.__table__.c.ai_translated
        assert isinstance(column.type, sa.Boolean)
        assert column.nullable is True
        assert column.server_default is None

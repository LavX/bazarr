"""The browser URL column: additive, idempotent, and reversible on both engines."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REVISION = "f3b8d2a6c417"
PREVIOUS = "a7d3e9c1f428"
# The table as the previous head left it, with the kind constraint the
# downgrade has to keep through SQLite's table rebuild.
BEFORE = """
CREATE TABLE media_server_instances (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0, url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '', verify_ssl INTEGER NOT NULL DEFAULT 1,
    path_mappings TEXT NOT NULL DEFAULT '[]', refresh_movies INTEGER NOT NULL DEFAULT 1,
    refresh_episodes INTEGER NOT NULL DEFAULT 1, options TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT ck_media_server_kind CHECK (kind IN ('emby', 'jellyfin', 'plex', 'silo')))
"""


def _migration():
    path = Path(__file__).parents[2] / "migrations/versions/f3b8d2a6c417_media_server_external_url.py"
    spec = importlib.util.spec_from_file_location("media_server_external_url_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection):
    return [column["name"] for column in sa.inspect(connection).get_columns("media_server_instances")]


def _exercise(connection):
    connection.execute(sa.text(BEFORE))
    connection.execute(sa.text("INSERT INTO media_server_instances (id, kind, name, url) "
                               "VALUES ('a', 'emby', 'A', 'http://emby.example')"))
    migration = _migration()
    assert (migration.revision, migration.down_revision) == (REVISION, PREVIOUS)
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    with Operations.context(context):
        migration.upgrade()
        migration.upgrade()
    assert _columns(connection).count("external_url") == 1
    assert connection.execute(sa.text("SELECT external_url FROM media_server_instances")).scalar() is None
    connection.execute(sa.text("UPDATE media_server_instances SET external_url = 'https://emby.example.com'"))
    with Operations.context(context):
        migration.downgrade()
        migration.downgrade()
    assert "external_url" not in _columns(connection)
    assert connection.execute(sa.text("SELECT name FROM media_server_instances")).scalar() == "A"
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(sa.text("INSERT INTO media_server_instances (id, kind, name, url) "
                                       "VALUES ('b', 'unknown', 'B', 'http://b.example')"))


def test_sqlite_external_url_migration(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'destinations.db'}")
    try:
        with engine.begin() as connection:
            _exercise(connection)
    finally:
        engine.dispose()


def test_postgres_external_url_migration():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    engine = sa.create_engine(url)
    schema = "external_url_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
            connection.execute(sa.text(f"SET search_path TO {schema}"))
            _exercise(connection)
            connection.execute(sa.text("SET search_path TO public"))
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
    finally:
        engine.dispose()


def test_a_fresh_schema_already_has_the_nullable_column():
    from app.database import TableMediaServerInstances
    column = TableMediaServerInstances.__table__.c.external_url
    assert isinstance(column.type, sa.Text) and column.nullable is True and column.server_default is None

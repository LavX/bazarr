"""The forced-search request column: additive, idempotent, reversible on both engines."""
import ast
import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REVISION = "a5e2c8d19f73"
PREVIOUS = "f3b8d2a6c417"
COLUMN = "forced_search_requested_at"
ROOT = Path(__file__).parents[2]


def _migration():
    path = ROOT / "migrations/versions/a5e2c8d19f73_episode_forced_search_flag.py"
    spec = importlib.util.spec_from_file_location("episode_forced_search_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(connection):
    return {c["name"]: c for c in sa.inspect(connection).get_columns("table_episodes")}


def _shape(connection):
    """What a schema comparison cares about: columns, indexes and foreign keys."""
    inspector = sa.inspect(connection)
    # Sorted by name: an added column lands last, a fresh table has its ORM order.
    columns = sorted((c["name"], str(c["type"]), c["nullable"], c["default"])
                     for c in inspector.get_columns("table_episodes"))
    indexes = sorted((i["name"], tuple(i["column_names"]), bool(i["unique"]))
                     for i in inspector.get_indexes("table_episodes"))
    foreign_keys = sorted((tuple(f["constrained_columns"]), f["referred_table"],
                           (f.get("options") or {}).get("ondelete"))
                          for f in inspector.get_foreign_keys("table_episodes"))
    return columns, indexes, foreign_keys


def _build(connection):
    from app.database import Base
    Base.metadata.create_all(connection)


def _drop_column(connection):
    """The table as the previous head left it: the ORM schema without the column."""
    connection.execute(sa.text(f"ALTER TABLE table_episodes DROP COLUMN {COLUMN}"))


def _exercise(connection, reference):
    _build(connection)
    _drop_column(connection)
    connection.execute(sa.text("INSERT INTO table_shows (id, title, path, \"sonarrSeriesId\") "
                               "VALUES (1, 'S', '/tv', 5)"))
    connection.execute(sa.text(
        "INSERT INTO table_episodes "
        "(id, series_id, \"sonarrSeriesId\", \"sonarrEpisodeId\", title, path, season, episode) "
        "VALUES (1, 1, 5, 51, 'E', '/tv/e.mkv', 1, 1)"))
    before = _shape(connection)
    assert COLUMN not in _columns(connection)

    migration = _migration()
    assert (migration.revision, migration.down_revision) == (REVISION, PREVIOUS)
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    with Operations.context(context):
        migration.upgrade()
        migration.upgrade()

    column = _columns(connection)[COLUMN]
    assert column["nullable"] is True and column["default"] is None
    assert isinstance(column["type"], sa.DateTime)
    # An upgraded database and a fresh install end up with the same table.
    assert _shape(connection) == reference
    row = connection.execute(sa.text(f"SELECT title, {COLUMN} FROM table_episodes WHERE id = 1")).one()
    assert tuple(row) == ("E", None)

    connection.execute(sa.text(f"UPDATE table_episodes SET {COLUMN} = '2026-11-07 10:00:00'"))
    with Operations.context(context):
        migration.downgrade()
        migration.downgrade()
    assert COLUMN not in _columns(connection)
    assert _shape(connection) == before
    assert connection.execute(sa.text("SELECT title FROM table_episodes")).scalar() == "E"
    # The cascade from the series is still in place after the downgrade.
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
    connection.execute(sa.text("DELETE FROM table_shows WHERE id = 1"))
    assert connection.execute(sa.text("SELECT COUNT(*) FROM table_episodes")).scalar() == 0


@contextmanager
def _sqlite(tmp_path, name):
    engine = sa.create_engine(f"sqlite:///{tmp_path / name}")
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


@contextmanager
def _postgres_schema(engine):
    schema = "forced_search_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"SET search_path TO {schema}"))
            yield connection
    finally:
        # Its own transaction: the test's may have been aborted by a failed statement.
        with engine.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))


def test_sqlite_forced_search_migration(tmp_path):
    with _sqlite(tmp_path, "reference.db") as fresh:
        _build(fresh)
        reference = _shape(fresh)
    with _sqlite(tmp_path, "upgraded.db") as connection:
        _exercise(connection, reference)


def test_postgres_forced_search_migration():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    engine = sa.create_engine(url)
    try:
        with _postgres_schema(engine) as fresh:
            _build(fresh)
            reference = _shape(fresh)
        with _postgres_schema(engine) as connection:
            _exercise(connection, reference)
    finally:
        engine.dispose()


def test_the_migration_is_the_only_head_of_the_chain():
    # Read the revision ids from the source: older migrations run database
    # calls when imported, so the chain cannot be loaded outside an upgrade.
    revisions, parents = set(), set()
    for path in (ROOT / "migrations/versions").glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == "revision":
                    revisions.add(ast.literal_eval(node.value))
                elif node.targets[0].id == "down_revision":
                    value = ast.literal_eval(node.value)
                    parents.update(value if isinstance(value, tuple) else (value,))
    assert revisions - parents == {REVISION}


def test_a_fresh_schema_already_has_the_nullable_column():
    from app.database import TableEpisodes
    column = TableEpisodes.__table__.c[COLUMN]
    assert isinstance(column.type, sa.DateTime) and column.nullable is True and column.server_default is None

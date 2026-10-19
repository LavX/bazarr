import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

TABLE = "table_embedded_track_hidden"
REVISION = "3c1af7932284"


def _migration():
    path = Path(__file__).parents[2] / "migrations/versions/3c1af7932284_embedded_track_hidden.py"
    spec = importlib.util.spec_from_file_location("embedded_track_hidden_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(connection, **values):
    base = {"media_type": "movie", "media_id": 1, "arr_instance_id": 1,
            "language": "en", "codec": "subrip", "hidden_at": "2026-09-26 00:00:00"}
    base.update(values)
    connection.execute(sa.text(
        f"INSERT INTO {TABLE} (media_type, media_id, arr_instance_id, language, codec, hidden_at) "
        "VALUES (:media_type, :media_id, :arr_instance_id, :language, :codec, :hidden_at)"), base)


def _exercise(connection):
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    migration = _migration()
    assert migration.revision == REVISION
    assert migration.down_revision == "a7d3e9c1f428"

    with Operations.context(context):
        migration.upgrade()
    inspector = sa.inspect(connection)
    assert TABLE in inspector.get_table_names()
    columns = {c["name"]: c for c in inspector.get_columns(TABLE)}
    assert set(columns) == {"id", "media_type", "media_id", "arr_instance_id",
                            "language", "codec", "hidden_at"}
    assert columns["codec"]["nullable"] is True
    assert columns["arr_instance_id"]["nullable"] is False
    unique = [i for i in inspector.get_indexes(TABLE) if i["name"] == "ux_embedded_track_hidden_item"]
    assert unique and unique[0]["unique"]
    assert unique[0]["column_names"] == ["media_type", "media_id", "arr_instance_id", "language"]

    _row(connection)
    # Same track on another owner, and another variant on the same owner, are different rows.
    _row(connection, arr_instance_id=2)
    _row(connection, language="en:hi")
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            _row(connection)
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            _row(connection, media_type="series")
    assert connection.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar() == 3

    # A second upgrade, which is what a fresh install runs after create_all,
    # must leave the table and its rows alone.
    with Operations.context(context):
        migration.upgrade()
    assert connection.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar() == 3

    with Operations.context(context):
        migration.downgrade()
    assert TABLE not in sa.inspect(connection).get_table_names()
    with Operations.context(context):
        migration.downgrade()
        migration.upgrade()
    assert TABLE in sa.inspect(connection).get_table_names()


def test_sqlite_embedded_track_hidden_migration(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'hidden.db'}")
    try:
        with engine.begin() as connection:
            _exercise(connection)
    finally:
        engine.dispose()


def test_postgres_embedded_track_hidden_migration():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    engine = sa.create_engine(url)
    schema = "hidden_tracks_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
            connection.execute(sa.text(f"SET search_path TO {schema}"))
            _exercise(connection)
            connection.execute(sa.text("SET search_path TO public"))
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
    finally:
        engine.dispose()


def test_fresh_schema_matches_the_migration(tmp_path):
    """create_all and the migration must build the same table, or a fresh and
    an upgraded install disagree about what the indexers read."""
    from app.database import TableEmbeddedTrackHidden

    fresh = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    migrated = sa.create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    try:
        TableEmbeddedTrackHidden.__table__.create(fresh)
        with migrated.begin() as connection:
            _migration().create_embedded_track_hidden(connection)

        def shape(engine):
            inspector = sa.inspect(engine)
            columns = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(TABLE)}
            indexes = {i["name"]: (tuple(i["column_names"]), bool(i["unique"]))
                       for i in inspector.get_indexes(TABLE)}
            return columns, indexes

        assert shape(fresh) == shape(migrated)
    finally:
        fresh.dispose()
        migrated.dispose()

"""Jellyfin and Plex convergence migration through the real Alembic environment, on both engines."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

_PREVIOUS = "a6d8f2b9c103"
_REVISION = "e5b7d1c93a24"
_LEGACY_KINDS = "kind IN ('emby', 'silo')"


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_engine(request, tmp_path):
    from app.database import configure_sqlite_connection

    if request.param == "sqlite":
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'convergence.db'}",
                                  poolclass=NullPool, isolation_level="AUTOCOMMIT")
        sa.event.listen(engine, "connect", configure_sqlite_connection)
        yield engine
        engine.dispose()
        return

    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("Set BAZARR_PG_TEST_URL to exercise PostgreSQL")
    schema = f"media_convergence_{uuid4().hex}"
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT",
                              connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _create_schema(engine, *, fresh=False):
    """The destination tables as the pre-convergence release left them.

    ``fresh`` builds them from the live metadata instead, which is what a new
    install gets: the migration has to be a no-op against that too.
    """
    if fresh:
        from app.database import Base, TableMediaServerImports, TableMediaServerInstances
        with engine.connect() as conn:
            Base.metadata.create_all(conn, tables=[TableMediaServerInstances.__table__,
                                                   TableMediaServerImports.__table__])
    else:
        path = Path(__file__).resolve().parents[2] / "migrations/versions/c2e7a4d9f810_media_server_instances.py"
        spec = importlib.util.spec_from_file_location("media_server_tables", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with engine.connect() as conn:
            module.create_media_server_tables(conn)
    with engine.connect() as conn:
        conn.execute(sa.text(
            "INSERT INTO media_server_instances (id, kind, name, enabled, url, api_key, verify_ssl, "
            "path_mappings, revision) VALUES ('emby-1', 'emby', 'Emby', 1, 'http://emby.example', "
            """'enc:v1:preserved', 1, '[{"local_path": "/movies", "remote_path": "/media"}]', 3)"""
            ))
        conn.execute(sa.text("INSERT INTO media_server_imports (kind) VALUES ('emby')"))


def _run(engine, action, revision):
    from flask import Flask

    app = Flask(__name__)
    app.extensions["migrate"] = SimpleNamespace(
        db=SimpleNamespace(get_engine=lambda: engine, metadata=sa.MetaData()),
        configure_args={"render_as_batch": True})
    directory = Path(__file__).resolve().parents[2] / "migrations"
    config = Config(str(directory / "alembic.ini"))
    config.set_main_option("script_location", str(directory))
    with app.app_context():
        getattr(command, action)(config, revision)


def _rows(engine):
    with engine.connect() as conn:
        return conn.execute(sa.text(
            "SELECT id, kind, name, enabled, url, api_key, verify_ssl, path_mappings, revision "
            "FROM media_server_instances ORDER BY id")).all()


def _insert(engine, **overrides):
    values = dict(id=uuid4().hex, kind="jellyfin", name="Jellyfin", enabled=1,
                  url="http://jellyfin.example", api_key="", verify_ssl=1) | overrides
    columns = ", ".join(values)
    placeholders = ", ".join(":" + name for name in values)
    with engine.connect() as conn:
        conn.execute(sa.text(f"INSERT INTO media_server_instances ({columns}) "
                             f"VALUES ({placeholders})"), values)


@pytest.mark.parametrize("fresh", [False, True], ids=["upgrade", "metadata-fresh"])
def test_upgrade_widens_kinds_adds_per_type_columns_and_reapplies(migration_engine, fresh):
    engine = migration_engine
    _create_schema(engine, fresh=fresh)
    before = _rows(engine)
    _run(engine, "stamp", _PREVIOUS)
    _run(engine, "upgrade", _REVISION)
    assert _rows(engine) == before
    with engine.connect() as conn:
        columns = {column["name"] for column in sa.inspect(conn).get_columns("media_server_instances")}
        assert {"refresh_movies", "refresh_episodes", "options"} <= columns
        # An existing destination keeps refreshing both libraries.
        assert conn.execute(sa.text("SELECT refresh_movies, refresh_episodes, options "
                                    "FROM media_server_instances")).all() == [(1, 1, "{}")]
    # Reapplying after metadata create_all or an interrupted stamp is safe.
    _run(engine, "stamp", _PREVIOUS)
    _run(engine, "upgrade", _REVISION)
    assert _rows(engine) == before
    _insert(engine)
    _insert(engine, kind="plex", name="Plex", refresh_episodes=0)
    with engine.connect() as conn:
        conn.execute(sa.text("INSERT INTO media_server_imports (kind) VALUES ('plex')"))
    for overrides in ({"kind": "unknown"}, {"refresh_movies": 2}, {"refresh_episodes": 2}):
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, **overrides)


def test_downgrade_refuses_converged_rows_and_markers_then_restores_schema(migration_engine):
    engine = migration_engine
    _create_schema(engine)
    _run(engine, "stamp", _PREVIOUS)
    _run(engine, "upgrade", _REVISION)
    _insert(engine)
    before = _rows(engine)
    with pytest.raises(ValueError, match="Jellyfin and Plex destinations"):
        _run(engine, "downgrade", _PREVIOUS)
    assert _rows(engine) == before
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == _REVISION
        conn.execute(sa.text("DELETE FROM media_server_instances WHERE kind='jellyfin'"))
        conn.execute(sa.text("INSERT INTO media_server_imports (kind) VALUES ('jellyfin')"))
    with pytest.raises(ValueError, match="import markers"):
        _run(engine, "downgrade", _PREVIOUS)
    with engine.connect() as conn:
        conn.execute(sa.text("DELETE FROM media_server_imports WHERE kind='jellyfin'"))
    remaining = _rows(engine)
    _run(engine, "downgrade", _PREVIOUS)
    assert _rows(engine) == remaining
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == _PREVIOUS
        columns = {column["name"] for column in sa.inspect(conn).get_columns("media_server_instances")}
        assert not {"refresh_movies", "refresh_episodes", "options"} & columns
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine)

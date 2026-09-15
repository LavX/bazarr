"""Sportarr kind migration through the real app Alembic environment on both engines."""
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

_PREVIOUS = "c2e7a4d9f810"
_REVISION = "c9e4a6b2d701"


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_engine(request, tmp_path):
    from app.database import configure_sqlite_connection

    if request.param == "sqlite":
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}",
                                  poolclass=NullPool, isolation_level="AUTOCOMMIT")
        sa.event.listen(engine, "connect", configure_sqlite_connection)
        yield engine
        engine.dispose()
        return

    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("Set BAZARR_PG_TEST_URL to exercise PostgreSQL")
    # Every test owns a separate schema, leaving other tests and services alone.
    schema = f"sportarr_kind_{uuid4().hex}"
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
    from app.database import TableArrInstances

    metadata = sa.MetaData()
    instances = TableArrInstances.__table__.to_metadata(metadata)
    if not fresh:
        kind_check = next(c for c in instances.constraints if c.name == "ck_arr_instances_kind")
        instances.constraints.remove(kind_check)
        instances.append_constraint(sa.CheckConstraint(
            "kind IN ('sonarr', 'radarr')", name="ck_arr_instances_kind"))
    # CASCADE makes an unsafe SQLite parent-table rebuild silently destructive.
    sa.Table("owned_rows", metadata,
             sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("arr_instance_id", sa.Integer,
                       sa.ForeignKey("arr_instances.id", ondelete="CASCADE"), nullable=False),
             sa.Column("payload", sa.Text))
    metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(instances.insert().values(
            kind="sonarr", name="Main", stable_key="main", port=8989,
            is_default=1, api_key="preserved-encrypted-payload", options='{"untouched": true}'))
        conn.execute(sa.text("INSERT INTO owned_rows VALUES (1, 1, 'preserved media')"))
    return instances


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


def _state(engine):
    with engine.connect() as conn:
        return (
            conn.execute(sa.text("SELECT * FROM arr_instances ORDER BY id")).all(),
            conn.execute(sa.text("SELECT * FROM owned_rows ORDER BY id")).all())


def _insert_sportarr(engine, instances, **overrides):
    with engine.connect() as conn:
        conn.execute(instances.insert().values(**(dict(
            kind="sportarr", name="Sports", stable_key="main", port=1867,
            is_default=1) | overrides)))


def _assert_integrity(engine, instances):
    for overrides in ({"is_default": 0}, {"stable_key": "another-default"},
                      {"stable_key": "disabled", "enabled": 0},
                      {"stable_key": "bad-enabled", "enabled": 2, "is_default": 0},
                      {"stable_key": "bad-default", "is_default": 2},
                      {"kind": "unknown", "stable_key": "invalid", "is_default": 0}):
        with pytest.raises(sa.exc.IntegrityError):
            _insert_sportarr(engine, instances, **overrides)
    with pytest.raises(sa.exc.IntegrityError):
        with engine.connect() as conn:
            conn.execute(sa.text("INSERT INTO owned_rows VALUES (2, 99999, 'orphan')"))
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            assert conn.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1
            assert conn.execute(sa.text("PRAGMA foreign_key_check")).all() == []
        # The parent link still cascades after migration.
        conn.execute(sa.text("DELETE FROM arr_instances WHERE id=1"))
        assert conn.execute(sa.text("SELECT * FROM owned_rows")).all() == []


@pytest.mark.parametrize("fresh", [False, True], ids=["upgrade", "metadata-fresh"])
def test_kind_upgrade_preserves_children_constraints_and_reapplication(migration_engine, fresh):
    engine = migration_engine
    instances = _create_schema(engine, fresh=fresh)
    before = _state(engine)
    _run(engine, "stamp", _PREVIOUS)
    _run(engine, "upgrade", _REVISION)
    assert _state(engine) == before
    # Reapplying after metadata create_all or an interrupted version stamp is safe.
    _run(engine, "stamp", _PREVIOUS)
    _run(engine, "upgrade", _REVISION)
    assert _state(engine) == before
    _insert_sportarr(engine, instances)
    _assert_integrity(engine, instances)


def test_kind_downgrade_refuses_sportarr_rows_then_restores_old_constraint(migration_engine):
    engine = migration_engine
    instances = _create_schema(engine)
    _run(engine, "stamp", _PREVIOUS)
    _run(engine, "upgrade", _REVISION)
    _insert_sportarr(engine, instances)
    before = _state(engine)
    with pytest.raises(ValueError, match="Sportarr"):
        _run(engine, "downgrade", _PREVIOUS)
    assert _state(engine) == before
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == _REVISION
        conn.execute(sa.text("DELETE FROM arr_instances WHERE kind='sportarr'"))
    remaining = _state(engine)
    _run(engine, "downgrade", _PREVIOUS)
    assert _state(engine) == remaining
    with pytest.raises(sa.exc.IntegrityError):
        _insert_sportarr(engine, instances)
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == _PREVIOUS
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO owned_rows VALUES (2, 99999, 'orphan')"))

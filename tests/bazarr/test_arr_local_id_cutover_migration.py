# coding=utf-8
"""Dry-run harness for the Phase 1e local-id PK cutover migration (#156).

Exercises the automatic existing-install upgrade path that the create_all-based
schema_session fixture cannot: it copies a REAL database to a tmp file, brings it
to the chain head (d9a3b7c1e240), then runs the cutover (e7f4c9d80abc) via
flask_migrate.upgrade - the exact call migrate_db() makes at startup.

Source DBs are local dev artifacts (skip-if-absent so CI stays green): the bench
DB (media volume, no history) and the operator's backup DB (full history/
blacklist volume). Never mutates the source - always works on a tmp copy.

Spec: docs/superpowers/specs/2026-06-11-phase1e-cutover-design.md.
"""
import os
import shutil

import pytest
from sqlalchemy import create_engine, inspect, text

_HEAD_BEFORE_CUTOVER = "d9a3b7c1e240"
_CUTOVER = "e7f4c9d80abc"

# Local dev DBs; absent on CI -> the parametrization skips.
_SOURCE_DBS = {
    "bench": "/home/lavx/bench/dev/config/db/bazarr.db",
    "backup": "/tmp/p1e_backup.db",
}


def _run_migration(db_path, revision):
    """Upgrade a sqlite file to `revision` exactly as migrate_db() would."""
    import flask_migrate
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

    from app.database import metadata, migrations_directory

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db = SQLAlchemy(app, metadata=metadata)
    with app.app_context():
        flask_migrate.Migrate(app, db, render_as_batch=True)
        flask_migrate.upgrade(directory=migrations_directory, revision=revision)
        db.engine.dispose()


def _alembic_version(db_path):
    eng = create_engine(f"sqlite:///{db_path}")
    try:
        with eng.connect() as conn:
            return conn.execute(text("select version_num from alembic_version")).scalar()
    finally:
        eng.dispose()


def _pk_columns(db_path, table):
    eng = create_engine(f"sqlite:///{db_path}")
    try:
        return inspect(eng).get_pk_constraint(table)["constrained_columns"]
    finally:
        eng.dispose()


@pytest.fixture(params=list(_SOURCE_DBS), ids=list(_SOURCE_DBS))
def legacy_db_at_head(request, tmp_path):
    """A tmp copy of a real DB brought up to the pre-cutover head."""
    src = _SOURCE_DBS[request.param]
    if not os.path.exists(src):
        pytest.skip(f"source DB not present: {src}")
    dst = tmp_path / "bazarr.db"
    shutil.copy(src, dst)
    _run_migration(str(dst), _HEAD_BEFORE_CUTOVER)
    assert _alembic_version(str(dst)) == _HEAD_BEFORE_CUTOVER
    # pre-cutover the media PK is still the upstream id
    assert _pk_columns(str(dst), "table_shows") == ["sonarrSeriesId"]
    return str(dst)


def test_cutover_stamps_revision(legacy_db_at_head):
    # Increment 0 (scaffold): the migration is reachable, chains correctly, and
    # stamps its revision. Schema transformation lands in later increments.
    _run_migration(legacy_db_at_head, _CUTOVER)
    assert _alembic_version(legacy_db_at_head) == _CUTOVER


def test_cutover_is_idempotent_noop_on_rerun(legacy_db_at_head):
    _run_migration(legacy_db_at_head, _CUTOVER)
    pk_after_first = _pk_columns(legacy_db_at_head, "table_shows")
    _run_migration(legacy_db_at_head, _CUTOVER)  # second run must no-op cleanly
    assert _alembic_version(legacy_db_at_head) == _CUTOVER
    assert _pk_columns(legacy_db_at_head, "table_shows") == pk_after_first


_OWNED_TABLES = (
    "table_shows", "table_episodes", "table_history", "table_blacklist",
    "table_shows_rootfolder", "table_movies", "table_history_movie",
    "table_blacklist_movie", "table_movies_rootfolder",
)


def test_cutover_bootstraps_default_instances_and_stamps_owners(legacy_db_at_head):
    # Step A+B: whether the source DB was already backfilled (backup) or not
    # (bench, simulating a fresh upgrade), after the cutover every owned row has
    # an owner and each kind has exactly one enabled default.
    _run_migration(legacy_db_at_head, _CUTOVER)
    eng = create_engine(f"sqlite:///{legacy_db_at_head}")
    try:
        with eng.connect() as conn:
            for kind in ("sonarr", "radarr"):
                n = conn.execute(text(
                    "select count(*) from arr_instances "
                    "where kind=:k and is_default=1 and enabled=1"), {"k": kind}).scalar()
                assert n == 1, f"{kind}: expected 1 enabled default, got {n}"
            for table in _OWNED_TABLES:
                nulls = conn.execute(text(
                    f"select count(*) from {table} where arr_instance_id is null")).scalar()
                assert nulls == 0, f"{table} has {nulls} unstamped rows after cutover"
    finally:
        eng.dispose()

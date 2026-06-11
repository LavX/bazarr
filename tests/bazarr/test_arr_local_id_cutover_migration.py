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
from sqlalchemy import create_engine, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

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


def test_cutover_backfills_local_ids_equal_to_upstream(legacy_db_at_head):
    # Step C+D: local id = upstream id everywhere; history/blacklist local refs
    # copied from their upstream id columns. The backup DB (9376 history /
    # 2052 history_movie / 8 blacklist) is the load-bearing case here.
    _run_migration(legacy_db_at_head, _CUTOVER)
    eng = create_engine(f"sqlite:///{legacy_db_at_head}")
    mismatches = [
        "select count(*) from table_shows where id != sonarrSeriesId",
        "select count(*) from table_episodes where id != sonarrEpisodeId",
        "select count(*) from table_episodes where series_id != sonarrSeriesId and sonarrSeriesId is not null",
        "select count(*) from table_movies where id != radarrId",
        "select count(*) from table_shows_rootfolder where local_rootfolder_id != id or upstream_rootfolder_id != id",
        "select count(*) from table_movies_rootfolder where local_rootfolder_id != id or upstream_rootfolder_id != id",
        "select count(*) from table_history where series_id != sonarrSeriesId and sonarrSeriesId is not null",
        "select count(*) from table_history where episode_id != sonarrEpisodeId and sonarrEpisodeId is not null",
        "select count(*) from table_history_movie where movie_id != radarrId and radarrId is not null",
        "select count(*) from table_blacklist where series_id != sonarr_series_id and sonarr_series_id is not null",
        "select count(*) from table_blacklist_movie where movie_id != radarr_id and radarr_id is not null",
    ]
    try:
        with eng.connect() as conn:
            for sql in mismatches:
                assert conn.execute(text(sql)).scalar() == 0, sql
    finally:
        eng.dispose()


def _counts(db):
    eng = create_engine(f"sqlite:///{db}")
    try:
        with eng.connect() as c:
            return {t: c.execute(text(f"select count(*) from {t}")).scalar()
                    for t in _OWNED_TABLES}
    finally:
        eng.dispose()


def test_cutover_rebuilds_tables_to_local_id_pk(legacy_db_at_head):
    # Steps F+G+H: PK cutover, FK repoint, scoped uniques, with row parity and
    # FK integrity. The backup DB (real history/blacklist) is load-bearing for
    # the FK-repoint path.
    pre = _counts(legacy_db_at_head)
    _run_migration(legacy_db_at_head, _CUTOVER)

    eng = create_engine(f"sqlite:///{legacy_db_at_head}")
    try:
        insp = inspect(eng)
        assert insp.get_pk_constraint("table_shows")["constrained_columns"] == ["id"]
        assert insp.get_pk_constraint("table_episodes")["constrained_columns"] == ["id"]
        assert insp.get_pk_constraint("table_movies")["constrained_columns"] == ["id"]
        assert insp.get_pk_constraint("table_shows_rootfolder")["constrained_columns"] == ["local_rootfolder_id"]
        assert insp.get_pk_constraint("table_movies_rootfolder")["constrained_columns"] == ["local_rootfolder_id"]

        with eng.connect() as c:
            for t in _OWNED_TABLES:
                assert c.execute(text(f"select count(*) from {t}")).scalar() == pre[t], t
            assert c.execute(text("PRAGMA foreign_key_check")).fetchall() == []

        # global uniques gone, scoped uniques present
        shows_idx = insp.get_indexes("table_shows")
        assert not any(i["unique"] and i["column_names"] == ["path"] for i in shows_idx)
        assert any(i["unique"] and i["column_names"] == ["arr_instance_id", "path"] for i in shows_idx)
        movies_idx = insp.get_indexes("table_movies")
        assert not any(i["unique"] and i["column_names"] == ["tmdbId"] for i in movies_idx)
        assert not any(i["unique"] and i["column_names"] == ["path"] for i in movies_idx)

        # FK repoint: episodes.series_id -> table_shows.id, old sonarrSeriesId FK gone
        ep_fks = insp.get_foreign_keys("table_episodes")
        assert any(fk["referred_table"] == "table_shows" and
                   fk["constrained_columns"] == ["series_id"] and
                   fk["referred_columns"] == ["id"] for fk in ep_fks)
        assert not any(fk["constrained_columns"] == ["sonarrSeriesId"] for fk in ep_fks)
    finally:
        eng.dispose()


def test_cutover_local_pk_autoincrements_and_scopes_uniqueness(legacy_db_at_head):
    _run_migration(legacy_db_at_head, _CUTOVER)
    eng = create_engine(f"sqlite:///{legacy_db_at_head}")
    ins = ('insert into table_shows (path, title, "sonarrSeriesId", arr_instance_id) '
           'values (:p, :t, :s, :i)')
    try:
        with eng.begin() as c:
            maxid = c.execute(text("select max(id) from table_shows")).scalar()
            inst = c.execute(text("select id from arr_instances where kind='sonarr'")).scalar()
            # bare insert (no id) -> rowid alias assigns MAX(id)+1
            c.execute(text(ins), {"p": "/p1e/x", "t": "X", "s": 999991, "i": inst})
            newid = c.execute(text("select id from table_shows where path='/p1e/x'")).scalar()
            assert newid == maxid + 1
            # same path under a DIFFERENT instance is allowed (scoped uniqueness)
            c.execute(text(ins), {"p": "/p1e/x", "t": "X2", "s": 999992, "i": inst + 1000})
        with eng.begin() as c:  # same path + same instance -> violation
            with pytest.raises(IntegrityError):
                c.execute(text(ins), {"p": "/p1e/x", "t": "X3", "s": 999993, "i": inst})
    finally:
        eng.dispose()


def test_migrated_db_works_with_current_orm_models(legacy_db_at_head):
    # The ORM PK flip is intentionally deferred (it would break single-instance
    # callers keyed by the upstream id). This proves the CURRENT models
    # (upstream-id PK) keep working against the migrated id-PK DB: get-by-radarrId,
    # select-by-path, and a legacy-style insert + fetch all succeed.
    from sqlalchemy.orm import Session

    from app.database import TableMovies

    _run_migration(legacy_db_at_head, _CUTOVER)
    eng = create_engine(f"sqlite:///{legacy_db_at_head}")
    try:
        with Session(eng) as s:
            existing = s.execute(text("select radarrId from table_movies limit 1")).scalar()
            if existing is not None:
                m = s.get(TableMovies, existing)            # model PK is radarrId
                assert m is not None and m.radarrId == existing
                by_path = s.execute(
                    select(TableMovies).where(TableMovies.path == m.path)).scalar_one()
                assert by_path.radarrId == existing
            inst = s.execute(text("select id from arr_instances where kind='radarr'")).scalar()
            s.execute(insert(TableMovies).values(
                radarrId=987654, path="/orm/new.mkv", title="ORM", tmdbId="orm987654",
                arr_instance_id=inst))
            s.commit()
            got = s.get(TableMovies, 987654)               # legacy get-by-radarrId
            assert got is not None and got.path == "/orm/new.mkv"
    finally:
        eng.dispose()

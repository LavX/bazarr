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
import importlib.util
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


_OWNED_FOR_PARITY = (
    "table_shows", "table_episodes", "table_movies",
    "table_shows_rootfolder", "table_movies_rootfolder",
    "table_history", "table_history_movie", "table_blacklist", "table_blacklist_movie",
)


def _schema_shape(db_path):
    """PK / FK / unique-index shape per owned table - the load-bearing schema
    facts that must agree between a fresh create_all and a migrated DB."""
    eng = create_engine(f"sqlite:///{db_path}")
    try:
        insp = inspect(eng)
        shape = {}
        for t in _OWNED_FOR_PARITY:
            pk = insp.get_pk_constraint(t)["constrained_columns"]
            fks = sorted((f["constrained_columns"][0], f["referred_table"], f["referred_columns"][0])
                         for f in insp.get_foreign_keys(t))
            uq = sorted(tuple(i["column_names"]) for i in insp.get_indexes(t) if i["unique"])
            shape[t] = {"pk": pk, "fks": fks, "uniques": uq}
        return shape
    finally:
        eng.dispose()


def test_fresh_create_all_matches_migrated_schema(legacy_db_at_head, tmp_path):
    # INC6 gate: the flipped ORM models' create_all must produce the SAME
    # PK/FK/unique shape that the cutover migration produces on an upgraded DB,
    # for all 9 owned tables - otherwise fresh installs diverge from upgrades.
    from app.database import Base

    _run_migration(legacy_db_at_head, _CUTOVER)
    migrated = _schema_shape(legacy_db_at_head)

    fresh_path = tmp_path / "fresh.db"
    fresh_eng = create_engine(f"sqlite:///{fresh_path}")
    Base.metadata.create_all(fresh_eng)
    fresh_eng.dispose()
    fresh = _schema_shape(str(fresh_path))

    for t in _OWNED_FOR_PARITY:
        assert fresh[t] == migrated[t], f"{t}: fresh create_all {fresh[t]} != migrated {migrated[t]}"


def test_migrated_db_works_with_flipped_orm_models(legacy_db_at_head):
    # Post-flip: the ORM PK is the local id. This proves the flipped models work
    # against the migrated DB: get-by-local-id, select/get-by-upstream-id (now a
    # scoped column query, no longer the PK), select-by-path, and a fresh insert
    # whose local id autoincrements independently of the upstream radarrId.
    from sqlalchemy.orm import Session

    from app.database import TableMovies

    _run_migration(legacy_db_at_head, _CUTOVER)
    eng = create_engine(f"sqlite:///{legacy_db_at_head}")
    try:
        with Session(eng) as s:
            existing = s.execute(text("select id, radarrId from table_movies limit 1")).first()
            if existing is not None:
                local_id, upstream = existing.id, existing.radarrId
                m = s.get(TableMovies, local_id)            # model PK is now id
                assert m is not None and m.id == local_id
                by_upstream = s.execute(
                    select(TableMovies).where(TableMovies.radarrId == upstream)).scalar_one()
                assert by_upstream.id == local_id
                by_path = s.execute(
                    select(TableMovies).where(TableMovies.path == m.path)).scalar_one()
                assert by_path.id == local_id
            inst = s.execute(text("select id from arr_instances where kind='radarr'")).scalar()
            s.execute(insert(TableMovies).values(
                radarrId=987654, path="/orm/new.mkv", title="ORM", tmdbId="orm987654",
                arr_instance_id=inst))
            s.commit()
            # local id autoincremented (a fresh rowid, not the upstream radarrId);
            # the row is reachable by its upstream id via a scoped column query.
            new_row = s.execute(
                select(TableMovies).where(TableMovies.radarrId == 987654)).scalar_one()
            assert new_row.path == "/orm/new.mkv"
            assert s.get(TableMovies, new_row.id) is new_row
    finally:
        eng.dispose()


# --------------------------------------------------------------------------- #
# Synthetic, ALWAYS-RUNS coverage. The real-DB params above skip on CI (the dev
# DBs are absent), so before this the file passed vacuously. These build the
# pre-cutover schema by hand and exercise the actual cutover transformation
# functions on a throwaway SQLite file, so the irreversible migration is gated
# on every CI run (#156 review H5).

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "migrations", "versions",
    "e7f4c9d80abc_arr_local_id_pk_cutover.py")

_SYNTH_OWNED = (
    "table_shows", "table_episodes", "table_movies", "table_shows_rootfolder",
    "table_movies_rootfolder", "table_history", "table_history_movie",
    "table_blacklist", "table_blacklist_movie")

_SYNTH_DDL = [
    'CREATE TABLE arr_instances (id INTEGER PRIMARY KEY, kind text, stable_key text, name text, enabled int default 1, is_default int default 0)',
    'CREATE TABLE table_languages_profiles ("profileId" INTEGER PRIMARY KEY, name text)',
    'CREATE TABLE table_shows ("sonarrSeriesId" INTEGER PRIMARY KEY, id int, arr_instance_id int, path text UNIQUE, "tmdbId" text, "profileId" int)',
    'CREATE TABLE table_episodes ("sonarrEpisodeId" INTEGER PRIMARY KEY, id int, arr_instance_id int, series_id int, "sonarrSeriesId" int, episode_file_id int)',
    'CREATE TABLE table_movies ("radarrId" INTEGER PRIMARY KEY, id int, arr_instance_id int, path text, "tmdbId" text, "profileId" int)',
    'CREATE TABLE table_shows_rootfolder (id INTEGER PRIMARY KEY, local_rootfolder_id int, upstream_rootfolder_id int, arr_instance_id int, path text, accessible int, error text)',
    'CREATE TABLE table_movies_rootfolder (id INTEGER PRIMARY KEY, local_rootfolder_id int, upstream_rootfolder_id int, arr_instance_id int, path text, accessible int, error text)',
    'CREATE TABLE table_history (id INTEGER PRIMARY KEY, arr_instance_id int, "sonarrSeriesId" int, "sonarrEpisodeId" int, series_id int, episode_id int, "upgradedFromId" int, video_path text, language text, "timestamp" int, action int, subs_id text)',
    'CREATE TABLE table_history_movie (id INTEGER PRIMARY KEY, arr_instance_id int, "radarrId" int, movie_id int, "upgradedFromId" int, video_path text, language text, "timestamp" int, action int, subs_id text)',
    'CREATE TABLE table_blacklist (id INTEGER PRIMARY KEY, arr_instance_id int, sonarr_series_id int, sonarr_episode_id int, series_id int, episode_id int, subs_id text)',
    'CREATE TABLE table_blacklist_movie (id INTEGER PRIMARY KEY, arr_instance_id int, radarr_id int, movie_id int, subs_id text)',
]


def _load_cutover_module():
    spec = importlib.util.spec_from_file_location("_p1e_cut", _MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def synthetic_sqlite(tmp_path):
    """A throwaway SQLite DB with the pre-cutover schema + single-instance data,
    owners stamped, local ids NULL."""
    db = tmp_path / "synth.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=OFF"))
        for ddl in _SYNTH_DDL:
            c.execute(text(ddl))
        c.execute(text("INSERT INTO arr_instances (id,kind,stable_key,name,is_default) "
                       "VALUES (1,'sonarr','sonarr','Sonarr',1),(2,'radarr','radarr','Radarr',1)"))
        c.execute(text('INSERT INTO table_shows ("sonarrSeriesId",arr_instance_id,path) VALUES (10,1,\'/tv/a\'),(11,1,\'/tv/b\')'))
        c.execute(text('INSERT INTO table_episodes ("sonarrEpisodeId",arr_instance_id,"sonarrSeriesId") VALUES (500,1,10),(501,1,11)'))
        c.execute(text('INSERT INTO table_movies ("radarrId",arr_instance_id,path) VALUES (20,2,\'/mov/a.mkv\')'))
        c.execute(text("INSERT INTO table_shows_rootfolder (id,arr_instance_id,path) VALUES (1,1,'/tv')"))
        c.execute(text('INSERT INTO table_history (arr_instance_id,"sonarrSeriesId","sonarrEpisodeId",video_path,language,"timestamp",action) VALUES (1,10,500,\'/tv/a\',\'en\',1,1)'))
    try:
        yield eng
    finally:
        eng.dispose()


def _run_sqlite_cutover(eng):
    mod = _load_cutover_module()
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=OFF"))
        mod._backfill_local_ids(c)
        mod._validate_pre_rebuild(c)
        pre = {t: c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in _SYNTH_OWNED}
        mod._rebuild_all(c)
        mod._validate_post_rebuild(c, pre)
    return pre


def test_synthetic_sqlite_cutover_flips_pks_and_preserves_rows(synthetic_sqlite):
    pre = _run_sqlite_cutover(synthetic_sqlite)
    eng = synthetic_sqlite
    assert _pk_columns_eng(eng, "table_shows") == ["id"]
    assert _pk_columns_eng(eng, "table_episodes") == ["id"]
    assert _pk_columns_eng(eng, "table_movies") == ["id"]
    assert _pk_columns_eng(eng, "table_shows_rootfolder") == ["local_rootfolder_id"]
    with eng.connect() as c:
        for t in _SYNTH_OWNED:
            assert c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() == pre[t]
        assert c.execute(text('SELECT id FROM table_shows WHERE "sonarrSeriesId"=10')).scalar() == 10


def test_synthetic_sqlite_cutover_allows_colliding_upstream_id(synthetic_sqlite):
    _run_sqlite_cutover(synthetic_sqlite)
    eng = synthetic_sqlite
    with eng.begin() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
        c.execute(text("INSERT INTO arr_instances (id,kind,stable_key,name) VALUES (8,'sonarr','sonarr-2','4K')"))
        c.execute(text('INSERT INTO table_shows ("sonarrSeriesId",arr_instance_id,path) VALUES (10,8,\'/tv4k/a\')'))
    with eng.connect() as c:
        new_id = c.execute(text("SELECT id FROM table_shows WHERE arr_instance_id=8")).scalar()
        assert new_id > 11  # autoincremented
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text('INSERT INTO table_shows ("sonarrSeriesId",arr_instance_id,path) VALUES (10,8,\'/tv4k/dup\')'))


def test_synthetic_sqlite_cutover_is_idempotent(synthetic_sqlite):
    _run_sqlite_cutover(synthetic_sqlite)
    mod = _load_cutover_module()
    with synthetic_sqlite.connect() as c:
        assert mod._already_cut_over(inspect(c)) is True


def _pk_columns_eng(eng, table):
    with eng.connect() as c:
        return inspect(c).get_pk_constraint(table)["constrained_columns"]

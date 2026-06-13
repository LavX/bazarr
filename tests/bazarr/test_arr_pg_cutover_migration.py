# coding=utf-8
"""Native PostgreSQL path of the local-id PK cutover migration (#156 review C2).

The cutover (e7f4c9d80abc) was SQLite-only (PRAGMA / rowid-alias) and bricked an
existing Postgres install on upgrade. This exercises the new native Postgres
branch against a REAL Postgres, building a faithful pre-cutover schema (upstream
ids as PKs, FKs between them) and asserting the transformation: PK flips to the
local id, the upstream id becomes unique only per instance, FKs repoint to the
local PK and still CASCADE, no rows are lost, and future bare inserts
autoincrement.

Skips when no Postgres is reachable (set BAZARR_PG_TEST_URL, default the
docker-compose/dev container on 55432). CI provides a postgres service so this
does NOT skip there.
"""
import importlib.util
import os

import pytest
import sqlalchemy as sa

_PG_URL = os.environ.get(
    "BAZARR_PG_TEST_URL",
    "postgresql+psycopg://postgres:test@127.0.0.1:55432/bazarr")

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "migrations", "versions",
    "e7f4c9d80abc_arr_local_id_pk_cutover.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("_p1e_cutover", _MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pg_engine():
    try:
        eng = sa.create_engine(_PG_URL)
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
        return eng
    except Exception:
        return None


@pytest.fixture
def pg_bind():
    eng = _pg_engine()
    if eng is None:
        pytest.skip(f"Postgres not reachable at {_PG_URL}")
    with eng.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    try:
        yield eng
    finally:
        eng.dispose()


# Pre-cutover schema: upstream ids are PKs (shows/episodes/movies/rootfolders),
# children FK to parents on upstream ids, history/blacklist already have an id
# PK. Mirrors what an existing single-instance Postgres install looks like.
_PRECUTOVER_DDL = [
    'CREATE TABLE arr_instances (id serial PRIMARY KEY, kind text, stable_key text, '
    'name text, enabled int DEFAULT 1, is_default int DEFAULT 0)',
    'CREATE TABLE table_languages_profiles ("profileId" int PRIMARY KEY, name text)',
    'CREATE TABLE table_shows ("sonarrSeriesId" int CONSTRAINT shows_pk PRIMARY KEY, '
    'id int, arr_instance_id int, path text CONSTRAINT shows_path_uq UNIQUE, '
    '"tmdbId" text, "profileId" int)',
    'CREATE TABLE table_episodes ("sonarrEpisodeId" int CONSTRAINT eps_pk PRIMARY KEY, '
    'id int, arr_instance_id int, series_id int, "sonarrSeriesId" int, episode_file_id int, '
    'CONSTRAINT eps_fk FOREIGN KEY ("sonarrSeriesId") REFERENCES table_shows("sonarrSeriesId") ON DELETE CASCADE)',
    'CREATE TABLE table_movies ("radarrId" int CONSTRAINT mov_pk PRIMARY KEY, '
    'id int, arr_instance_id int, path text, "tmdbId" text, "profileId" int)',
    'CREATE TABLE table_shows_rootfolder (id int CONSTRAINT srf_pk PRIMARY KEY, '
    'local_rootfolder_id int, upstream_rootfolder_id int, arr_instance_id int, path text, '
    'accessible int, error text)',
    'CREATE TABLE table_movies_rootfolder (id int CONSTRAINT mrf_pk PRIMARY KEY, '
    'local_rootfolder_id int, upstream_rootfolder_id int, arr_instance_id int, path text, '
    'accessible int, error text)',
    'CREATE TABLE table_history (id serial PRIMARY KEY, arr_instance_id int, '
    '"sonarrSeriesId" int, "sonarrEpisodeId" int, series_id int, episode_id int, '
    '"upgradedFromId" int, video_path text, language text, "timestamp" int, action int, subs_id text)',
    'CREATE TABLE table_history_movie (id serial PRIMARY KEY, arr_instance_id int, '
    '"radarrId" int, movie_id int, "upgradedFromId" int, video_path text, language text, '
    '"timestamp" int, action int, subs_id text)',
    'CREATE TABLE table_blacklist (id serial PRIMARY KEY, arr_instance_id int, '
    'sonarr_series_id int, sonarr_episode_id int, series_id int, episode_id int, subs_id text)',
    'CREATE TABLE table_blacklist_movie (id serial PRIMARY KEY, arr_instance_id int, '
    'radarr_id int, movie_id int, subs_id text)',
]


def _seed_precutover(bind):
    for ddl in _PRECUTOVER_DDL:
        bind.execute(sa.text(ddl))
    bind.execute(sa.text("INSERT INTO arr_instances (id,kind,stable_key,name,is_default) "
                         "VALUES (1,'sonarr','sonarr','Sonarr',1),(2,'radarr','radarr','Radarr',1)"))
    bind.execute(sa.text('INSERT INTO table_languages_profiles ("profileId",name) VALUES (1,\'English\')'))
    # single-instance data, owner stamped, local ids NULL (pre-backfill)
    bind.execute(sa.text('INSERT INTO table_shows ("sonarrSeriesId",arr_instance_id,path,"tmdbId","profileId") '
                         "VALUES (10,1,'/tv/a','100',1),(11,1,'/tv/b','101',1)"))
    bind.execute(sa.text('INSERT INTO table_episodes ("sonarrEpisodeId",arr_instance_id,"sonarrSeriesId",episode_file_id) '
                         "VALUES (500,1,10,9001),(501,1,11,9002)"))
    bind.execute(sa.text('INSERT INTO table_movies ("radarrId",arr_instance_id,path,"tmdbId","profileId") '
                         "VALUES (20,2,'/mov/a.mkv','200',1)"))
    bind.execute(sa.text("INSERT INTO table_shows_rootfolder (id,arr_instance_id,path) VALUES (1,1,'/tv')"))
    bind.execute(sa.text("INSERT INTO table_movies_rootfolder (id,arr_instance_id,path) VALUES (1,2,'/mov')"))
    bind.execute(sa.text('INSERT INTO table_history (arr_instance_id,"sonarrSeriesId","sonarrEpisodeId",video_path,language,"timestamp",action,subs_id) '
                         "VALUES (1,10,500,'/tv/a/ep','en',123,1,'h1')"))
    bind.execute(sa.text("INSERT INTO table_blacklist (arr_instance_id,sonarr_series_id,sonarr_episode_id,subs_id) "
                         "VALUES (1,10,500,'b1')"))


_OWNED = ('table_shows', 'table_episodes', 'table_movies', 'table_shows_rootfolder',
          'table_movies_rootfolder', 'table_history', 'table_history_movie',
          'table_blacklist', 'table_blacklist_movie')


def _run_pg_cutover(bind):
    mod = _load_migration()
    mod._backfill_local_ids(bind)
    mod._validate_pre_rebuild(bind)
    pre = {t: bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in _OWNED}
    mod._rebuild_all_postgres(bind)
    mod._validate_post_rebuild(bind, pre)
    return pre


def _pk_columns(bind, table):
    return bind.execute(sa.text(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
        "ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey) "
        f"WHERE i.indrelid='{table}'::regclass AND i.indisprimary")).scalars().all()


def test_pg_cutover_flips_pks_and_preserves_rows(pg_bind):
    with pg_bind.begin() as bind:
        _seed_precutover(bind)
    with pg_bind.begin() as bind:
        pre = _run_pg_cutover(bind)
    with pg_bind.connect() as bind:
        assert _pk_columns(bind, "table_shows") == ["id"]
        assert _pk_columns(bind, "table_episodes") == ["id"]
        assert _pk_columns(bind, "table_movies") == ["id"]
        assert _pk_columns(bind, "table_shows_rootfolder") == ["local_rootfolder_id"]
        # row parity across every owned table
        for t in _OWNED:
            assert bind.execute(sa.text(f'SELECT COUNT(*) FROM "{t}"')).scalar() == pre[t]
        # local id == upstream id for the migrated single instance
        assert bind.execute(sa.text('SELECT id FROM table_shows WHERE "sonarrSeriesId"=10')).scalar() == 10


def test_pg_cutover_allows_colliding_upstream_id_across_instances(pg_bind):
    with pg_bind.begin() as bind:
        _seed_precutover(bind)
    with pg_bind.begin() as bind:
        _run_pg_cutover(bind)
    # a second instance can now reuse sonarrSeriesId=10; local id autoincrements
    with pg_bind.begin() as bind:
        bind.execute(sa.text("INSERT INTO arr_instances (id,kind,stable_key,name) VALUES (8,'sonarr','sonarr-2','4K')"))
        bind.execute(sa.text('INSERT INTO table_shows (id,"sonarrSeriesId",arr_instance_id,path) '
                             "VALUES (DEFAULT,10,8,'/tv4k/a')"))
    with pg_bind.connect() as bind:
        new_id = bind.execute(sa.text("SELECT id FROM table_shows WHERE arr_instance_id=8")).scalar()
        assert new_id > 11  # autoincremented past the migrated ids
    # but a true duplicate (same instance + upstream id) is still rejected
    with pytest.raises(sa.exc.IntegrityError):
        with pg_bind.begin() as bind:
            bind.execute(sa.text('INSERT INTO table_shows (id,"sonarrSeriesId",arr_instance_id,path) '
                                 "VALUES (DEFAULT,10,8,'/tv4k/dup')"))


def test_pg_cutover_repoints_fk_to_local_pk_with_cascade(pg_bind):
    with pg_bind.begin() as bind:
        _seed_precutover(bind)
    with pg_bind.begin() as bind:
        _run_pg_cutover(bind)
    # deleting a show by its LOCAL id cascades to its episodes via the repointed FK
    with pg_bind.begin() as bind:
        bind.execute(sa.text("DELETE FROM table_shows WHERE id=10"))
    with pg_bind.connect() as bind:
        assert bind.execute(sa.text("SELECT COUNT(*) FROM table_episodes WHERE series_id=10")).scalar() == 0


def test_pg_cutover_is_idempotent_via_already_cut_over_guard(pg_bind):
    with pg_bind.begin() as bind:
        _seed_precutover(bind)
    with pg_bind.begin() as bind:
        _run_pg_cutover(bind)
    # after the flip, _already_cut_over reports True so the whole migration no-ops
    mod = _load_migration()
    with pg_bind.connect() as bind:
        assert mod._already_cut_over(sa.inspect(bind)) is True

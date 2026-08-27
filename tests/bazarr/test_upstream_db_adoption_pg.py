# coding=utf-8
"""Native PostgreSQL path of the upstream database adoption (inbound LavX/bazarr#302).

The second reporter on that issue hit it on PostgreSQL, after loading an
upstream SQLite database into Postgres with pgloader: the alembic_version row
comes across with everything else. PostgreSQL is a first-class backend here, so
the adoption is exercised against a real server rather than assumed to follow
from the SQLite run.

Skips when no Postgres is reachable (set BAZARR_PG_TEST_URL, default the
docker-compose/dev container on 55432). CI provides a postgres service so this
does NOT skip there.
"""
import os

import pytest
import sqlalchemy as sa

_PG_URL = os.environ.get(
    "BAZARR_PG_TEST_URL",
    "postgresql+psycopg://postgres:test@127.0.0.1:55432/bazarr")


@pytest.fixture
def pg_engine():
    try:
        engine = sa.create_engine(_PG_URL)
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as exc:  # driver missing or server unreachable
        pytest.skip(f"no PostgreSQL at {_PG_URL}: {exc}")

    with engine.begin() as connection:
        connection.execute(sa.text('DROP SCHEMA IF EXISTS upstream_adoption CASCADE'))
        connection.execute(sa.text('CREATE SCHEMA upstream_adoption'))
    engine.dispose()

    engine = sa.create_engine(_PG_URL,
                              connect_args={"options": "-csearch_path=upstream_adoption"})
    try:
        yield engine
    finally:
        engine.dispose()
        cleanup = sa.create_engine(_PG_URL)
        with cleanup.begin() as connection:
            connection.execute(sa.text('DROP SCHEMA IF EXISTS upstream_adoption CASCADE'))
        cleanup.dispose()


def _upstream_shape(engine, stamp='0124f9e278fb'):
    """A PostgreSQL database in the shape upstream leaves one at 0124f9e278fb."""
    with engine.begin() as connection:
        connection.execute(sa.text(
            'CREATE TABLE table_episodes ("sonarrEpisodeId" INTEGER PRIMARY KEY, '
            '"sonarrSeriesId" INTEGER, path TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_movies ("radarrId" INTEGER PRIMARY KEY, path TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_episodes_subtitles (id SERIAL PRIMARY KEY, '
            '"sonarrEpisodeId" INTEGER, "sonarrSeriesId" INTEGER, language TEXT, '
            'hi BOOLEAN, forced BOOLEAN, path TEXT, size INTEGER, embedded_track_id TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE table_movies_subtitles (id SERIAL PRIMARY KEY, '
            '"radarrId" INTEGER, language TEXT, hi BOOLEAN, forced BOOLEAN, '
            'path TEXT, size INTEGER, embedded_track_id TEXT)'))
        connection.execute(sa.text(
            'CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
        connection.execute(sa.text(
            'INSERT INTO alembic_version (version_num) VALUES (:stamp)'), {'stamp': stamp})


def test_an_unknown_revision_is_adopted_on_postgres_too(pg_engine):
    """The second reporter on the inbound issue was on PostgreSQL, after
    pgloader carried the alembic_version row across with everything else."""
    from app.upstream_adoption import adopt_upstream_database

    _upstream_shape(pg_engine, stamp='537e9b4d10e3')
    with pg_engine.begin() as connection:
        assert adopt_upstream_database(connection) == '309dc062d2e4'


def test_a_postgres_database_from_upstream_is_adopted(pg_engine):
    from app.upstream_adoption import adopt_upstream_database

    _upstream_shape(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            "VALUES (41, 4, '/m/e.mkv')"))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            "VALUES (41, 4, 'en', true, false, '/m/e.en.hi.srt', 500)"))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, '
            'size, embedded_track_id) '
            "VALUES (41, 4, 'fr', false, true, NULL, NULL, '2')"))
        connection.execute(sa.text(
            'INSERT INTO table_movies ("radarrId", path) VALUES (7, \'/m/m.mkv\')'))
        connection.execute(sa.text(
            'INSERT INTO table_movies_subtitles '
            '("radarrId", language, hi, forced, path, size) '
            "VALUES (7, 'es', false, true, '/m/m.es.forced.srt', 12)"))

    with pg_engine.begin() as connection:
        assert adopt_upstream_database(connection) == '309dc062d2e4'

    with pg_engine.connect() as connection:
        assert connection.execute(
            sa.text('SELECT version_num FROM alembic_version')).scalar() == '309dc062d2e4'
        # The boolean columns are real booleans here, not the 0/1 SQLite stores,
        # and the embedded track survives the conversion alongside the file.
        assert connection.execute(sa.text(
            'SELECT subtitles FROM table_episodes WHERE "sonarrEpisodeId" = 41')).scalar() == \
            str([['en:hi', '/m/e.en.hi.srt', 500], ['fr:forced', None, None]])
        assert connection.execute(sa.text(
            'SELECT subtitles FROM table_movies WHERE "radarrId" = 7')).scalar() == \
            str([['es:forced', '/m/m.es.forced.srt', 12]])

    # The split tables go once their rows are across: on PostgreSQL their
    # foreign keys would block the local-id PK cutover outright.
    tables = set(sa.inspect(pg_engine).get_table_names())
    assert 'table_episodes_subtitles' not in tables
    assert 'table_movies_subtitles' not in tables


def test_adopting_a_postgres_database_twice_changes_nothing(pg_engine):
    from app.upstream_adoption import adopt_upstream_database

    _upstream_shape(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO table_episodes ("sonarrEpisodeId", "sonarrSeriesId", path) '
            "VALUES (42, 4, '/m/f.mkv')"))
        connection.execute(sa.text(
            'INSERT INTO table_episodes_subtitles '
            '("sonarrEpisodeId", "sonarrSeriesId", language, hi, forced, path, size) '
            "VALUES (42, 4, 'de', false, false, '/m/f.de.srt', 3)"))

    with pg_engine.begin() as connection:
        assert adopt_upstream_database(connection) == '309dc062d2e4'
    with pg_engine.begin() as connection:
        assert adopt_upstream_database(connection) is None

    with pg_engine.connect() as connection:
        assert connection.execute(sa.text(
            'SELECT subtitles FROM table_episodes WHERE "sonarrEpisodeId" = 42')).scalar() == \
            str([['de', '/m/f.de.srt', 3]])


# --- the cutover's own index creation, on PostgreSQL ----------------------
#
# Found while adopting an upstream database on PostgreSQL, but it is not an
# adoption bug. The local-id PK cutover short-circuits on a fresh install,
# where create_all has already built the final shape. Any database whose tables
# predate the cutover runs the body instead, and by then 4bb94a033f93 has
# created ix_table_episodes_episode_file_id, which the cutover's PostgreSQL
# branch then creates again. The SQLite branch never noticed: it rebuilds each
# table, so the old indexes go with it.

def _cutover_module():
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), '..', '..', 'migrations', 'versions',
                        'e7f4c9d80abc_arr_local_id_pk_cutover.py')
    spec = importlib.util.spec_from_file_location('cutover_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_cutover_index_can_be_created_when_it_is_already_there():
    """A shape assertion covering all six tables at once. Executing every DDL
    against a real server would mean rebuilding six pre-cutover schemas; this
    catches the same class of failure for the ones the test below does not."""
    module = _cutover_module()

    for table, _pk, _nn, _du, index_ddls, _fks in module._PG_SPECS:
        for ddl in index_ddls:
            assert 'IF NOT EXISTS' in ddl, f'{table}: {ddl}'


def test_the_episodes_cutover_indexes_survive_one_already_existing(pg_engine):
    """The collision that actually happens: 4bb94a033f93 creates
    ix_table_episodes_episode_file_id, and the cutover then creates it again."""
    module = _cutover_module()

    with pg_engine.begin() as connection:
        connection.execute(sa.text(
            'CREATE TABLE table_episodes (id INTEGER, arr_instance_id INTEGER, '
            '"sonarrEpisodeId" INTEGER, "sonarrSeriesId" INTEGER, series_id INTEGER, '
            'episode_file_id INTEGER)'))
        # exactly what the hot-path index migration leaves behind
        connection.execute(sa.text(
            'CREATE INDEX ix_table_episodes_episode_file_id ON table_episodes (episode_file_id)'))

    episodes = next(spec for spec in module._PG_SPECS if spec[0] == 'table_episodes')

    with pg_engine.begin() as connection:
        for ddl in episodes[4]:
            connection.execute(sa.text(ddl))

    indexes = {index['name'] for index in sa.inspect(pg_engine).get_indexes('table_episodes')}
    assert 'ix_table_episodes_episode_file_id' in indexes
    assert 'ix_table_episodes_series_id' in indexes


def test_an_index_that_cannot_be_built_does_not_undo_the_column_repair(pg_engine):
    """On PostgreSQL a failed statement aborts the transaction it ran in.

    The column repair and the index repair used to share one, so a single index
    that cannot be built (a unique one meeting duplicate rows) poisoned the
    connection: the commit that was going to persist the freshly restored
    columns failed instead, and the repair silently undid itself. Everything the
    adoption path had just fixed went with it.

    A savepoint would fix that here and break the app, whose engine runs with
    isolation_level=AUTOCOMMIT where PostgreSQL rejects SAVEPOINT outright. One
    transaction per index is correct under both.
    """
    from sqlalchemy import Column, Index, Integer, MetaData, Table, Text

    from app.upstream_adoption import (restore_missing_model_columns,
                                       restore_missing_model_indexes)

    with pg_engine.begin() as connection:
        connection.execute(sa.text('DROP TABLE IF EXISTS probe_repair'))
        connection.execute(sa.text('CREATE TABLE probe_repair (id INTEGER)'))
        connection.execute(sa.text('INSERT INTO probe_repair VALUES (1), (1)'))

    # A UNIQUE INDEX (not a unique constraint: only an Index reaches the index
    # repair at all) over a column that already holds duplicates. It cannot be
    # built, which is the failure the repair has to survive.
    indexed = MetaData()
    probe = Table('probe_repair', indexed,
                  Column('id', Integer),
                  Column('restored', Text))
    Index('ix_probe_repair_id', probe.c.id, unique=True)

    with pg_engine.begin() as connection:
        restored = restore_missing_model_columns(connection, metadata=indexed)
    assert ('probe_repair', 'restored') in restored

    created = restore_missing_model_indexes(pg_engine, metadata=indexed)
    assert created == [], (
        'the unique index cannot exist over duplicate rows, so nothing should '
        f'have been reported as created; got {created!r}')

    columns = {c['name'] for c in sa.inspect(pg_engine).get_columns('probe_repair')}
    assert 'restored' in columns, (
        'the failed index rolled back the column the repair had just restored, '
        f'leaving {sorted(columns)}')

    with pg_engine.begin() as connection:
        connection.execute(sa.text('DROP TABLE IF EXISTS probe_repair'))

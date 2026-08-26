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


def _upstream_shape(engine):
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
            "INSERT INTO alembic_version (version_num) VALUES ('0124f9e278fb')"))


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

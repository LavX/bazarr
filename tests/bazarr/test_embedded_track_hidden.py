# coding=utf-8
"""The hidden-track records on the engine the application really runs.

The application engine is AUTOCOMMIT on both backends, so session.rollback()
undoes nothing and a bare SAVEPOINT fails on PostgreSQL. Every write here is a
single statement for that reason, and these tests run the writes on an
AUTOCOMMIT engine of each kind rather than on a transactional test session.
"""
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool


@pytest.fixture(params=["sqlite", "postgresql"])
def hidden_db(request, tmp_path):
    """A session on an AUTOCOMMIT engine of each kind, the way the app runs.

    The indexer and API tests import this fixture too, so every database case
    of this feature runs on both backends.
    """
    from app.database import Base, configure_sqlite_connection

    admin = schema = None
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.fail("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
        schema = "embedded_hidden_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT",
                                  connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'hidden.db'}", poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")
        # The app's own connect hook, so foreign keys are enforced as they are live.
        sa.event.listen(engine, "connect", configure_sqlite_connection)
    Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    try:
        yield session
    finally:
        session.remove()
        engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


def test_language_key_is_the_key_the_indexer_stores():
    from subtitles.embedded_hidden import language_key
    from subtitles.indexer.utils import normalize_subtitle_language_variant

    for hi in (False, True):
        for forced in (False, True):
            assert language_key("en", hi=hi, forced=forced) == \
                normalize_subtitle_language_variant("en", forced=forced, hi=hi)
    assert language_key("en", hi=True, forced=True) == "en:hi:forced"
    assert language_key(" EN ") == "en"
    for bad in ("", None, "e", "english", "en:hi", "e1", "../x"):
        assert language_key(bad) is None


def test_hiding_is_idempotent_and_scoped_to_the_owner(hidden_db):
    from subtitles.embedded_hidden import hidden_languages, hide_track

    assert hide_track(hidden_db, "movie", 5, 1, "en", "subrip") is True
    assert hide_track(hidden_db, "movie", 5, 1, "en", "subrip") is False
    assert hide_track(hidden_db, "movie", 5, 1, "en:hi") is True
    assert hidden_languages(hidden_db, "movie", [5], 1) == {"en", "en:hi"}
    # Same local id under another owner is another row (SQLite reuses ids).
    assert hidden_languages(hidden_db, "movie", [5], 2) == set()
    # Same local id as another media type is another row.
    assert hidden_languages(hidden_db, "episode", [5], 1) == set()


def test_hiding_reports_a_duplicate_on_a_sqlite_without_returning(tmp_path):
    """SQLite before 3.35 has no RETURNING. SQLAlchemy then turns it off, and
    hide_track reads the rowcount, which is exact there."""
    from app.database import TableEmbeddedTrackHidden
    from subtitles.embedded_hidden import hide_track

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}", poolclass=NullPool,
                              isolation_level="AUTOCOMMIT")
    engine.dialect.insert_returning = False
    TableEmbeddedTrackHidden.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        assert hide_track(session, "movie", 5, 1, "en") is True
        assert hide_track(session, "movie", 5, 1, "en") is False
    finally:
        session.close()
        engine.dispose()


def test_a_row_without_an_owner_is_stored_under_the_sentinel(hidden_db):
    from app.database import TableEmbeddedTrackHidden
    from subtitles.embedded_hidden import UNOWNED, hidden_languages, hide_track

    assert hide_track(hidden_db, "episode", 9, None, "fr") is True
    # The sentinel is what makes the unique index hold: NULL never equals NULL.
    assert hide_track(hidden_db, "episode", 9, None, "fr") is False
    assert hidden_db.execute(sa.select(TableEmbeddedTrackHidden.arr_instance_id)).scalar_one() == UNOWNED
    assert hidden_languages(hidden_db, "episode", [9], None) == {"fr"}


def test_unhide_removes_only_that_language_on_those_rows(hidden_db):
    from subtitles.embedded_hidden import hidden_languages, hide_track, unhide_track

    for media_id in (1, 2, 3):
        hide_track(hidden_db, "episode", media_id, 1, "en")
    hide_track(hidden_db, "episode", 1, 1, "de")
    assert unhide_track(hidden_db, "episode", [1, 2], 1, "en") == 2
    assert hidden_languages(hidden_db, "episode", [1], 1) == {"de"}
    assert hidden_languages(hidden_db, "episode", [3], 1) == {"en"}
    assert unhide_track(hidden_db, "episode", [1], 1, "en") == 0


def test_list_and_forget(hidden_db):
    from subtitles.embedded_hidden import forget_hidden, hide_track, list_hidden

    hide_track(hidden_db, "sports", 61, 1, "fr", "subrip")
    hide_track(hidden_db, "sports", 62, 2, "fr")
    rows = list_hidden(hidden_db, "sports", [61, 62, 63])
    assert [(r.media_id, r.arr_instance_id, r.language, r.codec) for r in rows] == \
        [(61, 1, "fr", "subrip"), (62, 2, "fr", None)]
    assert rows[0].hidden_at is not None
    forget_hidden(hidden_db, "sports", [61])
    assert [r.media_id for r in list_hidden(hidden_db, "sports", [61, 62])] == [62]


def test_forget_media_also_forgets_hidden_tracks(hidden_db):
    """Every sync path that deletes a media row calls forget_media, so the
    hidden records go with the row instead of waiting for SQLite to hand the
    id to a new movie."""
    from subtitles.embedded_hidden import hide_track, list_hidden
    from subtitles.mismatch import forget_media

    hide_track(hidden_db, "movie", 7, 1, "en")
    hide_track(hidden_db, "episode", 7, 1, "en")
    forget_media(hidden_db, "series", [7])
    assert list_hidden(hidden_db, "episode", [7]) == []
    assert len(list_hidden(hidden_db, "movie", [7])) == 1
    forget_media(hidden_db, "movie", [7])
    assert list_hidden(hidden_db, "movie", [7]) == []


def test_episode_file_siblings_group_one_file_per_owner(hidden_db):
    from app.database import TableEpisodes, TableShows
    from subtitles.embedded_hidden import episode_file_siblings

    hidden_db.execute(sa.insert(TableShows).values(id=1, sonarrSeriesId=1, title="S", path="/tv/s"))
    for episode_id, owner, path in ((1, 1, "/tv/s/e01-e02.mkv"), (2, 1, "/tv/s/e01-e02.mkv"),
                                    (3, 2, "/tv/s/e01-e02.mkv"), (4, 1, "/tv/s/e03.mkv"),
                                    (5, None, "/tv/s/e05.mkv"), (6, None, "/tv/s/e05.mkv")):
        hidden_db.execute(sa.insert(TableEpisodes).values(
            id=episode_id, arr_instance_id=owner, series_id=1, sonarrSeriesId=1,
            sonarrEpisodeId=100 + episode_id, title="E", path=path, season=1, episode=episode_id))
    groups = episode_file_siblings(hidden_db, [1, 2, 3, 4, 5, 404])
    assert groups == {1: {1, 2}, 2: {1, 2}, 3: {3}, 4: {4}, 5: {5, 6}, 404: {404}}


def test_a_failed_lookup_lists_the_track_instead_of_breaking_indexing():
    from subtitles.embedded_hidden import hidden_languages

    class Broken:
        def execute(self, *_args, **_kwargs):
            raise sa.exc.OperationalError("SELECT", {}, Exception("database is locked"))

    assert hidden_languages(Broken(), "movie", [1], 1) == set()
    assert hidden_languages(Broken(), "movie", [], 1) == set()

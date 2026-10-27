# coding=utf-8

from datetime import datetime

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import scoped_session, sessionmaker

from app.database import Base, TableEpisodes, TableHistory, TableHistoryMovie, TableMovies, TableShows


@pytest.fixture
def history_db(monkeypatch):
    from subtitles.indexer import movies, series
    import radarr.history as radarr_history
    import sonarr.history as sonarr_history

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))

    monkeypatch.setattr(series, "database", session)
    monkeypatch.setattr(movies, "database", session)
    monkeypatch.setattr(sonarr_history, "database", session)
    monkeypatch.setattr(radarr_history, "database", session)
    monkeypatch.setattr(sonarr_history, "event_stream", lambda **_: None)
    monkeypatch.setattr(radarr_history, "event_stream", lambda **_: None)

    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


def _embedded_row(table, language, path, **ids):
    return dict(action=7, description=f"{language} embedded subtitles detected.", language=language,
                provider="embedded", score=100, score_out_of=100, timestamp=datetime.now(), video_path=path, **ids)


EPISODE = "/series/show/s01e01.mkv"
MOVIE = "/movies/movie.mkv"


def _episode_languages(db, path=None):
    query = select(TableHistory.language, TableHistory.video_path).where(TableHistory.action == 7)
    return sorted((row.language, row.video_path) for row in db.execute(query).all()
                  if path is None or row.video_path == path)


def _movie_languages(db):
    query = select(TableHistoryMovie.language, TableHistoryMovie.video_path).where(TableHistoryMovie.action == 7)
    return sorted((row.language, row.video_path) for row in db.execute(query).all())


def test_series_embedded_history_dedup_normalizes_combined_forced_hi(history_db):
    from subtitles.indexer.series import _log_embedded_history

    _insert_episode(history_db)
    history_db.execute(insert(TableHistory).values(**_embedded_row(
        TableHistory, "en:forced", EPISODE, sonarrEpisodeId=20, sonarrSeriesId=10)))

    _log_embedded_history(10, 20, ["en:forced:hi", "en:hi:forced"], EPISODE)

    assert _episode_languages(history_db) == [("en:forced", EPISODE)]


def test_movie_embedded_history_dedup_normalizes_combined_forced_hi(history_db):
    from subtitles.indexer.movies import _log_embedded_history_movie

    _insert_movie(history_db)
    history_db.execute(insert(TableHistoryMovie).values(**_embedded_row(
        TableHistoryMovie, "en:forced", MOVIE, radarrId=30)))

    _log_embedded_history_movie(30, ["en:forced:hi", "en:hi:forced"], MOVIE)

    assert _movie_languages(history_db) == [("en:forced", MOVIE)]


def test_a_forced_and_hi_track_is_recorded_as_forced_not_as_hi(history_db):
    """Recorded as plain HI it would end the upgrades of a plain English
    download, which the forced track does not cover."""
    from subtitles.indexer.movies import _log_embedded_history_movie
    from subtitles.indexer.series import _log_embedded_history

    _insert_episode(history_db)
    _insert_movie(history_db)

    _log_embedded_history(10, 20, ["en:hi:forced", "fr:hi"], EPISODE, arr_instance_id=1)
    _log_embedded_history_movie(30, ["en:hi:forced", "fr:hi"], MOVIE, arr_instance_id=1)

    assert _episode_languages(history_db) == [("en:forced", EPISODE), ("fr:hi", EPISODE)]
    assert _movie_languages(history_db) == [("en:forced", MOVIE), ("fr:hi", MOVIE)]


def test_a_video_at_a_new_path_gets_a_row_of_its_own(history_db):
    """The upgrade check matches the row to a download by video path, so the
    row of the old file says nothing about a file that replaced it."""
    from subtitles.indexer.movies import _log_embedded_history_movie
    from subtitles.indexer.series import _log_embedded_history

    _insert_episode(history_db)
    _insert_movie(history_db)
    replaced = "/series/show/s01e01.v2.mkv"
    moved = "/movies/movie.v2.mkv"

    for _ in range(2):
        _log_embedded_history(10, 20, ["en"], EPISODE, arr_instance_id=1)
        _log_embedded_history_movie(30, ["en"], MOVIE, arr_instance_id=1)
    _log_embedded_history(10, 20, ["en"], replaced, arr_instance_id=1)
    _log_embedded_history(10, 20, ["en"], replaced, arr_instance_id=1)
    _log_embedded_history_movie(30, ["en"], moved, arr_instance_id=1)
    _log_embedded_history_movie(30, ["en"], moved, arr_instance_id=1)

    assert _episode_languages(history_db) == [("en", EPISODE), ("en", replaced)]
    assert _movie_languages(history_db) == [("en", MOVIE), ("en", moved)]


def test_every_episode_of_a_multi_episode_file_gets_its_row(history_db):
    from subtitles.indexer.series import _log_embedded_history

    _insert_episode(history_db)
    history_db.execute(insert(TableEpisodes).values(
        arr_instance_id=1, episode=2, monitored="True", path=EPISODE, season=1, sonarrEpisodeId=21, sonarrSeriesId=10, title="Two"))

    _log_embedded_history(10, 20, ["en"], EPISODE, arr_instance_id=1)
    _log_embedded_history(10, 21, ["en"], EPISODE, arr_instance_id=1)
    _log_embedded_history(10, 21, ["en"], EPISODE, arr_instance_id=1)

    rows = history_db.execute(select(TableHistory.sonarrEpisodeId).where(TableHistory.action == 7)).all()
    assert sorted(row.sonarrEpisodeId for row in rows) == [20, 21]


def _insert_episode(db):
    db.execute(insert(TableShows).values(
        arr_instance_id=1,
        sonarrSeriesId=10,
        path="/series/show",
        title="Show",
    ))
    db.execute(insert(TableEpisodes).values(
        arr_instance_id=1,
        episode=1,
        monitored="True",
        path="/series/show/s01e01.mkv",
        season=1,
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        title="Pilot",
    ))


def _insert_movie(db):
    db.execute(insert(TableMovies).values(
        arr_instance_id=1,
        path="/movies/movie.mkv",
        radarrId=30,
        title="Movie",
        tmdbId="100",
    ))

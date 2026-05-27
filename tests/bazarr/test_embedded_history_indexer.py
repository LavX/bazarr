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


def test_series_embedded_history_dedup_normalizes_combined_forced_hi(history_db):
    from subtitles.indexer.series import _log_embedded_history

    _insert_episode(history_db)
    history_db.execute(insert(TableHistory).values(
        action=7,
        description="en:hi embedded subtitles detected.",
        language="en:hi",
        provider="embedded",
        score=100,
        score_out_of=100,
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        timestamp=datetime.now(),
        video_path="/series/show/s01e01.mkv",
    ))

    _log_embedded_history(10, 20, ["en:forced:hi"], "/series/show/s01e01.mkv")

    rows = history_db.execute(
        select(TableHistory.language)
        .where(TableHistory.sonarrEpisodeId == 20)
        .where(TableHistory.action == 7)
    ).all()
    assert [row.language for row in rows] == ["en:hi"]


def test_movie_embedded_history_dedup_normalizes_combined_forced_hi(history_db):
    from subtitles.indexer.movies import _log_embedded_history_movie

    _insert_movie(history_db)
    history_db.execute(insert(TableHistoryMovie).values(
        action=7,
        description="en:hi embedded subtitles detected.",
        language="en:hi",
        provider="embedded",
        score=100,
        score_out_of=100,
        radarrId=30,
        timestamp=datetime.now(),
        video_path="/movies/movie.mkv",
    ))

    _log_embedded_history_movie(30, ["en:forced:hi"], "/movies/movie.mkv")

    rows = history_db.execute(
        select(TableHistoryMovie.language)
        .where(TableHistoryMovie.radarrId == 30)
        .where(TableHistoryMovie.action == 7)
    ).all()
    assert [row.language for row in rows] == ["en:hi"]


def _insert_episode(db):
    db.execute(insert(TableShows).values(
        sonarrSeriesId=10,
        path="/series/show",
        title="Show",
    ))
    db.execute(insert(TableEpisodes).values(
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
        path="/movies/movie.mkv",
        radarrId=30,
        title="Movie",
        tmdbId="100",
    ))

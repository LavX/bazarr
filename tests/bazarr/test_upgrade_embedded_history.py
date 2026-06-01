# coding=utf-8

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import scoped_session, sessionmaker

from app.database import (
    Base,
    TableEpisodes,
    TableHistory,
    TableHistoryMovie,
    TableMovies,
    TableShows,
)


@pytest.fixture
def upgrade_db(monkeypatch):
    from api.subtitles import batch
    import app.database as database_module
    from subtitles import upgrade

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))

    settings = SimpleNamespace(
        general=SimpleNamespace(
            days_to_upgrade_subs=365,
            upgrade_manual=False,
            upgrade_subs=True,
        )
    )
    monkeypatch.setattr(upgrade, "database", session)
    monkeypatch.setattr(batch, "database", session)
    monkeypatch.setattr(upgrade, "settings", settings)
    monkeypatch.setattr(batch, "settings", settings)
    monkeypatch.setattr(
        database_module,
        "settings",
        SimpleNamespace(
            radarr=SimpleNamespace(excluded_tags=[], only_monitored=False),
            sonarr=SimpleNamespace(
                excluded_series_types=[],
                excluded_tags=[],
                exclude_season_zero=False,
                only_monitored=False,
            ),
        ),
    )

    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


def test_episode_upgrade_keeps_download_candidate_when_embedded_history_is_newer(
    upgrade_db,
):
    from subtitles.upgrade import get_upgradable_episode_subtitles

    now = datetime.now()
    _insert_episode(upgrade_db)
    upgrade_db.execute(
        insert(TableHistory).values(
            id=101,
            action=1,
            description="English subtitles downloaded.",
            language="en",
            provider="opensubtitlescom",
            score=90,
            score_out_of=100,
            sonarrEpisodeId=20,
            sonarrSeriesId=10,
            timestamp=now - timedelta(minutes=5),
            video_path="/series/show/s01e01.mkv",
        )
    )
    upgrade_db.execute(
        insert(TableHistory).values(
            id=102,
            action=7,
            description="en embedded subtitles detected.",
            language="en",
            provider="embedded",
            score=100,
            score_out_of=100,
            sonarrEpisodeId=20,
            sonarrSeriesId=10,
            timestamp=now,
            video_path="/series/show/s01e01.mkv",
        )
    )

    assert get_upgradable_episode_subtitles() == {101: None}


def test_movie_upgrade_keeps_download_candidate_when_embedded_history_is_newer(
    upgrade_db,
):
    from subtitles.upgrade import get_upgradable_movies_subtitles

    now = datetime.now()
    _insert_movie(upgrade_db)
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            id=201,
            action=1,
            description="English subtitles downloaded.",
            language="en",
            provider="opensubtitlescom",
            score=90,
            score_out_of=100,
            radarrId=30,
            timestamp=now - timedelta(minutes=5),
            video_path="/movies/movie.mkv",
        )
    )
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            id=202,
            action=7,
            description="en embedded subtitles detected.",
            language="en",
            provider="embedded",
            score=100,
            score_out_of=100,
            radarrId=30,
            timestamp=now,
            video_path="/movies/movie.mkv",
        )
    )

    assert get_upgradable_movies_subtitles() == {201: None}


def test_batch_upgrade_ids_ignore_newer_embedded_history(upgrade_db):
    from api.subtitles.batch import get_upgradable_media_ids

    now = datetime.now()
    _insert_episode(upgrade_db)
    _insert_movie(upgrade_db)
    upgrade_db.execute(
        insert(TableHistory).values(
            action=1,
            description="English subtitles downloaded.",
            language="en",
            provider="opensubtitlescom",
            score=90,
            score_out_of=100,
            sonarrEpisodeId=20,
            sonarrSeriesId=10,
            timestamp=now - timedelta(minutes=5),
            video_path="/series/show/s01e01.mkv",
        )
    )
    upgrade_db.execute(
        insert(TableHistory).values(
            action=7,
            description="en embedded subtitles detected.",
            language="en",
            provider="embedded",
            score=100,
            score_out_of=100,
            sonarrEpisodeId=20,
            sonarrSeriesId=10,
            timestamp=now,
            video_path="/series/show/s01e01.mkv",
        )
    )
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            action=1,
            description="English subtitles downloaded.",
            language="en",
            provider="opensubtitlescom",
            score=90,
            score_out_of=100,
            radarrId=30,
            timestamp=now - timedelta(minutes=5),
            video_path="/movies/movie.mkv",
        )
    )
    upgrade_db.execute(
        insert(TableHistoryMovie).values(
            action=7,
            description="en embedded subtitles detected.",
            language="en",
            provider="embedded",
            score=100,
            score_out_of=100,
            radarrId=30,
            timestamp=now,
            video_path="/movies/movie.mkv",
        )
    )

    assert get_upgradable_media_ids() == {"movies": [30], "series": [10]}


def _insert_episode(db):
    db.execute(
        insert(TableShows).values(
            sonarrSeriesId=10,
            path="/series/show",
            title="Show",
        )
    )
    db.execute(
        insert(TableEpisodes).values(
            episode=1,
            monitored="True",
            path="/series/show/s01e01.mkv",
            season=1,
            sonarrEpisodeId=20,
            sonarrSeriesId=10,
            title="Pilot",
        )
    )


def _insert_movie(db):
    db.execute(
        insert(TableMovies).values(
            path="/movies/movie.mkv",
            radarrId=30,
            title="Movie",
            tmdbId="100",
        )
    )

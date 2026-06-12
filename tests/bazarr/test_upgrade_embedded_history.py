# coding=utf-8

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import scoped_session, sessionmaker

from app.database import Base, TableEpisodes, TableHistory, TableHistoryMovie, TableMovies, TableShows


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


def test_episode_upgrade_keeps_download_candidate_when_embedded_history_is_newer(upgrade_db):
    from subtitles.upgrade import get_upgradable_episode_subtitles

    now = datetime.now()
    _insert_episode(upgrade_db)
    upgrade_db.execute(insert(TableHistory).values(
        id=101,
        arr_instance_id=1,
        series_id=10,
        episode_id=20,
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
    ))
    upgrade_db.execute(insert(TableHistory).values(
        id=102,
        arr_instance_id=1,
        series_id=10,
        episode_id=20,
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
    ))

    assert get_upgradable_episode_subtitles() == {101: None}


def test_movie_upgrade_keeps_download_candidate_when_embedded_history_is_newer(upgrade_db):
    from subtitles.upgrade import get_upgradable_movies_subtitles

    now = datetime.now()
    _insert_movie(upgrade_db)
    upgrade_db.execute(insert(TableHistoryMovie).values(
        id=201,
        arr_instance_id=2,
        movie_id=30,
        action=1,
        description="English subtitles downloaded.",
        language="en",
        provider="opensubtitlescom",
        score=90,
        score_out_of=100,
        radarrId=30,
        timestamp=now - timedelta(minutes=5),
        video_path="/movies/movie.mkv",
    ))
    upgrade_db.execute(insert(TableHistoryMovie).values(
        id=202,
        arr_instance_id=2,
        movie_id=30,
        action=7,
        description="en embedded subtitles detected.",
        language="en",
        provider="embedded",
        score=100,
        score_out_of=100,
        radarrId=30,
        timestamp=now,
        video_path="/movies/movie.mkv",
    ))

    assert get_upgradable_movies_subtitles() == {201: None}


def test_batch_upgrade_ids_ignore_newer_embedded_history(upgrade_db):
    from api.subtitles.batch import get_upgradable_media_ids

    now = datetime.now()
    _insert_episode(upgrade_db)
    _insert_movie(upgrade_db)
    upgrade_db.execute(insert(TableHistory).values(
        arr_instance_id=1,
        series_id=10,
        episode_id=20,
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
    ))
    upgrade_db.execute(insert(TableHistory).values(
        arr_instance_id=1,
        series_id=10,
        episode_id=20,
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
    ))
    upgrade_db.execute(insert(TableHistoryMovie).values(
        arr_instance_id=2,
        movie_id=30,
        action=1,
        description="English subtitles downloaded.",
        language="en",
        provider="opensubtitlescom",
        score=90,
        score_out_of=100,
        radarrId=30,
        timestamp=now - timedelta(minutes=5),
        video_path="/movies/movie.mkv",
    ))
    upgrade_db.execute(insert(TableHistoryMovie).values(
        arr_instance_id=2,
        movie_id=30,
        action=7,
        description="en embedded subtitles detected.",
        language="en",
        provider="embedded",
        score=100,
        score_out_of=100,
        radarrId=30,
        timestamp=now,
        video_path="/movies/movie.mkv",
    ))

    assert get_upgradable_media_ids() == {
        "movies": [30],
        "series": [10],
        "movieKeys": [{"radarrId": 30, "arr_instance_id": 2}],
        "seriesKeys": [{"sonarrSeriesId": 10, "arr_instance_id": 1}],
    }


def test_batch_upgrade_ids_keep_duplicate_upstream_ids_by_instance(upgrade_db):
    from api.subtitles.batch import get_upgradable_media_ids

    now = datetime.now()
    _insert_episode(upgrade_db)
    _insert_movie(upgrade_db)
    upgrade_db.execute(insert(TableShows).values(
        id=11,
        arr_instance_id=3,
        sonarrSeriesId=10,
        path="/series/show-b",
        title="Show B",
    ))
    upgrade_db.execute(insert(TableEpisodes).values(
        id=21,
        series_id=11,
        arr_instance_id=3,
        episode=1,
        monitored="True",
        path="/series/show-b/s01e01.mkv",
        season=1,
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        title="Pilot",
    ))
    upgrade_db.execute(insert(TableMovies).values(
        id=31,
        arr_instance_id=4,
        path="/movies/movie-b.mkv",
        radarrId=30,
        title="Movie B",
        tmdbId="101",
    ))

    for history_id, owner, video_path in [
        (101, 1, "/series/show/s01e01.mkv"),
        (102, 3, "/series/show-b/s01e01.mkv"),
    ]:
        upgrade_db.execute(insert(TableHistory).values(
            id=history_id,
            arr_instance_id=owner,
            series_id=10,
            episode_id=20,
            action=1,
            description="English subtitles downloaded.",
            language="en",
            provider="opensubtitlescom",
            score=90,
            score_out_of=100,
            sonarrEpisodeId=20,
            sonarrSeriesId=10,
            timestamp=now - timedelta(minutes=5),
            video_path=video_path,
        ))

    for history_id, owner, video_path in [
        (201, 2, "/movies/movie.mkv"),
        (202, 4, "/movies/movie-b.mkv"),
    ]:
        upgrade_db.execute(insert(TableHistoryMovie).values(
            id=history_id,
            arr_instance_id=owner,
            movie_id=30,
            action=1,
            description="English subtitles downloaded.",
            language="en",
            provider="opensubtitlescom",
            score=90,
            score_out_of=100,
            radarrId=30,
            timestamp=now - timedelta(minutes=5),
            video_path=video_path,
        ))

    result = get_upgradable_media_ids()

    assert sorted(result["movies"]) == [30, 30]
    assert sorted(result["series"]) == [10, 10]
    assert sorted(result["movieKeys"], key=lambda item: item["arr_instance_id"]) == [
        {"radarrId": 30, "arr_instance_id": 2},
        {"radarrId": 30, "arr_instance_id": 4},
    ]
    assert sorted(result["seriesKeys"], key=lambda item: item["arr_instance_id"]) == [
        {"sonarrSeriesId": 10, "arr_instance_id": 1},
        {"sonarrSeriesId": 10, "arr_instance_id": 3},
    ]


def _insert_episode(db):
    db.execute(insert(TableShows).values(
        id=10,
        arr_instance_id=1,
        sonarrSeriesId=10,
        path="/series/show",
        title="Show",
    ))
    db.execute(insert(TableEpisodes).values(
        id=20,
        series_id=10,
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
        id=30,
        arr_instance_id=2,
        path="/movies/movie.mkv",
        radarrId=30,
        title="Movie",
        tmdbId="100",
    ))

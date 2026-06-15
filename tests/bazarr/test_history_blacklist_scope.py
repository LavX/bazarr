# coding=utf-8

from datetime import datetime

from flask import Flask


def _stub_history_dependencies(monkeypatch):
    from api import utils
    from api.episodes import history as episode_history
    from api.movies import history as movie_history

    monkeypatch.setattr(movie_history, "get_upgradable_movies_subtitles", lambda history_id_list: {})
    monkeypatch.setattr(movie_history, "_language_still_desired", lambda language, profile_id: True)
    monkeypatch.setattr(movie_history, "pretty_date", lambda value: "pretty")
    monkeypatch.setattr(episode_history, "get_upgradable_episode_subtitles", lambda history_id_list: {})
    monkeypatch.setattr(episode_history, "_language_still_desired", lambda language, profile_id: True)
    monkeypatch.setattr(episode_history, "pretty_date", lambda value: "pretty")
    monkeypatch.setattr(utils, "language_from_alpha2", lambda value: {"en": "English"}[value])
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda value: {"en": "eng"}[value])


def test_movie_history_blacklist_flag_is_scoped_by_instance(schema_session, monkeypatch):
    from api.movies import history
    from app.database import TableBlacklistMovie, TableHistoryMovie, TableMovies

    _stub_history_dependencies(monkeypatch)
    monkeypatch.setattr(history, "database", schema_session)

    schema_session.add(TableMovies(
        id=100,
        radarrId=1,
        arr_instance_id=2,
        path="/movies/alpha.mkv",
        title="Alpha",
        tmdbId="100",
        monitored="True",
        subtitles="[]",
        tags="[]",
    ))
    schema_session.flush()
    schema_session.add(TableHistoryMovie(
        id=1,
        movie_id=100,
        radarrId=1,
        arr_instance_id=2,
        action=1,
        description="alpha history",
        language="en",
        provider="provider-a",
        subs_id="shared-sub",
        subtitles_path="/movies/alpha.en.srt",
        video_path="/movies/alpha.mkv",
        timestamp=datetime(2026, 6, 12, 12, 0, 1),
    ))
    schema_session.add(TableBlacklistMovie(
        id=2,
        movie_id=100,
        radarr_id=1,
        arr_instance_id=3,
        provider="provider-a",
        subs_id="shared-sub",
        language="en",
        timestamp=datetime(2026, 6, 12, 12, 0, 2),
    ))

    app = Flask(__name__)
    with app.test_request_context("/api/movies/history?id=100"):
        result = history.MoviesHistory.get.__wrapped__(history.MoviesHistory())

    assert result["data"][0]["blacklisted"] is False


def test_episode_history_blacklist_flag_is_scoped_by_instance(schema_session, monkeypatch):
    from api.episodes import history
    from app.database import TableBlacklist, TableEpisodes, TableHistory, TableShows

    _stub_history_dependencies(monkeypatch)
    monkeypatch.setattr(history, "database", schema_session)

    schema_session.add(TableShows(
        id=501,
        sonarrSeriesId=1,
        arr_instance_id=8,
        path="/series/alpha",
        title="Alpha",
        tags="[]",
    ))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=601,
        series_id=501,
        sonarrEpisodeId=11,
        sonarrSeriesId=1,
        arr_instance_id=8,
        path="/series/alpha/s01e01.mkv",
        title="Pilot",
        season=1,
        episode=1,
        monitored="True",
        subtitles="[]",
    ))
    schema_session.flush()
    schema_session.add(TableHistory(
        id=701,
        series_id=501,
        episode_id=601,
        sonarrEpisodeId=11,
        sonarrSeriesId=1,
        arr_instance_id=8,
        action=1,
        description="alpha history",
        language="en",
        provider="provider-a",
        subs_id="shared-sub",
        subtitles_path="/series/alpha/s01e01.en.srt",
        video_path="/series/alpha/s01e01.mkv",
        timestamp=datetime(2026, 6, 12, 12, 0, 1),
    ))
    schema_session.add(TableBlacklist(
        id=702,
        series_id=501,
        episode_id=601,
        sonarr_series_id=1,
        sonarr_episode_id=11,
        arr_instance_id=9,
        provider="provider-a",
        subs_id="shared-sub",
        language="en",
        timestamp=datetime(2026, 6, 12, 12, 0, 2),
    ))

    app = Flask(__name__)
    with app.test_request_context("/api/episodes/history?id=601"):
        result = history.EpisodesHistory.get.__wrapped__(history.EpisodesHistory())

    assert result["data"][0]["blacklisted"] is False

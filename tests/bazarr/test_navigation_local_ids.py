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


def _stub_language_dependencies(monkeypatch):
    from api import utils

    monkeypatch.setattr(utils, "language_from_alpha2", lambda value: {"en": "English"}[value])
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda value: {"en": "eng"}[value])


def test_episode_history_returns_local_episode_and_series_ids(schema_session, monkeypatch):
    from api.episodes import history
    from app.database import TableEpisodes, TableHistory, TableShows

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
        provider="provider",
        subtitles_path="/series/alpha/s01e01.en.srt",
        video_path="/series/alpha/s01e01.mkv",
        timestamp=datetime(2026, 6, 12, 12, 0, 1),
    ))
    schema_session.add(TableShows(
        id=502,
        sonarrSeriesId=1,
        arr_instance_id=9,
        path="/series/beta",
        title="Beta",
        tags="[]",
    ))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=602,
        series_id=502,
        sonarrEpisodeId=11,
        sonarrSeriesId=1,
        arr_instance_id=9,
        path="/series/beta/s01e01.mkv",
        title="Pilot",
        season=1,
        episode=1,
        monitored="True",
        subtitles="[]",
    ))
    schema_session.flush()
    schema_session.add(TableHistory(
        id=702,
        series_id=502,
        episode_id=602,
        sonarrEpisodeId=11,
        sonarrSeriesId=1,
        arr_instance_id=9,
        action=1,
        description="beta history",
        language="en",
        provider="provider",
        subtitles_path="/series/beta/s01e01.en.srt",
        video_path="/series/beta/s01e01.mkv",
        timestamp=datetime(2026, 6, 12, 12, 0, 2),
    ))

    app = Flask(__name__)
    with app.test_request_context("/api/episodes/history?id=601"):
        result = history.EpisodesHistory.get.__wrapped__(history.EpisodesHistory())

    assert [item["description"] for item in result["data"]] == ["alpha history"]
    assert result["data"][0]["id"] == 601
    assert result["data"][0]["series_id"] == 501
    assert result["data"][0]["arr_instance_id"] == 8
    assert result["total"] == 1


def test_movie_blacklist_returns_local_movie_id(schema_session, monkeypatch):
    from api.movies import blacklist
    from app.database import TableBlacklistMovie, TableMovies

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "pretty_date", lambda value: "pretty")
    _stub_language_dependencies(monkeypatch)

    schema_session.add(TableMovies(
        id=801,
        radarrId=1,
        arr_instance_id=3,
        path="/movies/alpha.mkv",
        title="Alpha",
        tmdbId="801",
    ))
    schema_session.flush()
    schema_session.add(TableBlacklistMovie(
        id=901,
        movie_id=801,
        radarr_id=1,
        arr_instance_id=3,
        provider="provider",
        subs_id="sub-1",
        language="en",
        timestamp=datetime(2026, 6, 12, 12, 0, 1),
    ))

    app = Flask(__name__)
    with app.test_request_context("/api/movies/blacklist"):
        result = blacklist.MoviesBlacklist.get.__wrapped__(blacklist.MoviesBlacklist())

    assert result["data"][0]["id"] == 801
    assert result["data"][0]["arr_instance_id"] == 3


def test_episode_blacklist_returns_local_series_id(schema_session, monkeypatch):
    from api.episodes import blacklist
    from app.database import TableBlacklist, TableEpisodes, TableShows

    monkeypatch.setattr(blacklist, "database", schema_session)
    monkeypatch.setattr(blacklist, "pretty_date", lambda value: "pretty")
    _stub_language_dependencies(monkeypatch)

    schema_session.add(TableShows(
        id=1001,
        sonarrSeriesId=1,
        arr_instance_id=9,
        path="/series/beta",
        title="Beta",
    ))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=1002,
        series_id=1001,
        sonarrEpisodeId=11,
        sonarrSeriesId=1,
        arr_instance_id=9,
        path="/series/beta/s01e01.mkv",
        title="Pilot",
        season=1,
        episode=1,
    ))
    schema_session.flush()
    schema_session.add(TableBlacklist(
        id=1003,
        series_id=1001,
        episode_id=1002,
        sonarr_series_id=1,
        sonarr_episode_id=11,
        arr_instance_id=9,
        provider="provider",
        subs_id="sub-2",
        language="en",
        timestamp=datetime(2026, 6, 12, 12, 0, 1),
    ))

    app = Flask(__name__)
    with app.test_request_context("/api/episodes/blacklist"):
        result = blacklist.EpisodesBlacklist.get.__wrapped__(blacklist.EpisodesBlacklist())

    assert result["data"][0]["id"] == 1001
    assert result["data"][0]["arr_instance_id"] == 9

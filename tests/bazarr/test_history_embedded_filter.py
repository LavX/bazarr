# coding=utf-8

"""General history must not drown real events in Embedded Source records.

"Treat Embedded Subtitles as Downloaded" writes one action=7 history row per
episode/language combination. A large library produces six figures of them in
a single indexing pass, and the History pages become unusable: meaningful
events (downloads, uploads, upgrades, translations) are buried, and every
page load pays to sort the whole flood. The endpoints therefore exclude
action=7 from both the rows and the total by default, and only include them
when the caller asks with include_embedded=true.
"""

from datetime import datetime

from flask import Flask


EMBEDDED_ACTION = 7


def _series_fixture(schema_session):
    from app.database import TableEpisodes, TableHistory, TableShows

    show = TableShows(
        id=1,
        sonarrSeriesId=10,
        arr_instance_id=1,
        title="alpha show",
        path="/tv/alpha",
        profileId=None,
        tags="[]",
        seriesType="standard",
        monitored="True",
    )
    episode = TableEpisodes(
        id=1,
        series_id=1,
        sonarrSeriesId=10,
        sonarrEpisodeId=100,
        arr_instance_id=1,
        title="pilot",
        path="/tv/alpha/s01e01.mkv",
        season=1,
        episode=1,
        subtitles="[]",
        missing_subtitles="[]",
        monitored="True",
    )
    # Two flushes on purpose: the FK from episodes to shows must be satisfied
    # before the episode row goes in (matching test_history_blacklist_scope).
    schema_session.add(show)
    schema_session.flush()
    schema_session.add(episode)
    schema_session.flush()

    def history_row(history_id, action, description):
        return TableHistory(
            id=history_id,
            series_id=1,
            episode_id=1,
            sonarrSeriesId=10,
            sonarrEpisodeId=100,
            arr_instance_id=1,
            action=action,
            description=description,
            language="en",
            provider="embedded" if action == EMBEDDED_ACTION else "provider",
            subtitles_path="/tv/alpha/s01e01.en.srt",
            video_path="/tv/alpha/s01e01.mkv",
            timestamp=datetime(2026, 8, 29, 12, 0, history_id),
        )

    schema_session.add_all([
        history_row(1, 1, "downloaded"),
        history_row(2, EMBEDDED_ACTION, "embedded detected"),
        history_row(3, EMBEDDED_ACTION, "embedded detected again"),
    ])
    schema_session.flush()


def _movie_fixture(schema_session):
    from app.database import TableHistoryMovie, TableMovies

    movie = TableMovies(
        id=1,
        radarrId=1,
        arr_instance_id=1,
        title="alpha movie",
        path="/movies/alpha.mkv",
        tmdbId="1",
        profileId=None,
        tags="[]",
        monitored="True",
        subtitles="[]",
        missing_subtitles="[]",
    )
    schema_session.add(movie)
    schema_session.flush()

    def history_row(history_id, action, description):
        return TableHistoryMovie(
            id=history_id,
            movie_id=1,
            radarrId=1,
            arr_instance_id=1,
            action=action,
            description=description,
            language="en",
            provider="embedded" if action == EMBEDDED_ACTION else "provider",
            subtitles_path="/movies/alpha.en.srt",
            video_path="/movies/alpha.mkv",
            timestamp=datetime(2026, 8, 29, 12, 0, history_id),
        )

    schema_session.add_all([
        history_row(1, 1, "downloaded"),
        history_row(2, EMBEDDED_ACTION, "embedded detected"),
    ])
    schema_session.flush()


def _patch_episode_endpoint(monkeypatch, schema_session):
    from api import utils
    from api.episodes import history

    monkeypatch.setattr(history, "database", schema_session)
    monkeypatch.setattr(history, "get_upgradable_episode_subtitles",
                        lambda history_id_list: {})
    monkeypatch.setattr(history, "_language_still_desired",
                        lambda language, profile_id: True)
    monkeypatch.setattr(history, "pretty_date", lambda value: "pretty")
    monkeypatch.setattr(utils, "language_from_alpha2", lambda value: "English")
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda value: "eng")


def _patch_movie_endpoint(monkeypatch, schema_session):
    from api import utils
    from api.movies import history

    monkeypatch.setattr(history, "database", schema_session)
    monkeypatch.setattr(history, "get_upgradable_movies_subtitles",
                        lambda history_id_list: {})
    monkeypatch.setattr(history, "_language_still_desired",
                        lambda language, profile_id: True)
    monkeypatch.setattr(history, "pretty_date", lambda value: "pretty")
    monkeypatch.setattr(utils, "language_from_alpha2", lambda value: "English")
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda value: "eng")


def _get_episode_history(query_string):
    from api.episodes import history

    app = Flask(__name__)
    with app.test_request_context(f"/api/episodes/history{query_string}"):
        return history.EpisodesHistory.get.__wrapped__(history.EpisodesHistory())


def _get_movie_history(query_string):
    from api.movies import history

    app = Flask(__name__)
    with app.test_request_context(f"/api/movies/history{query_string}"):
        return history.MoviesHistory.get.__wrapped__(history.MoviesHistory())


def test_episode_history_excludes_embedded_by_default(schema_session, monkeypatch):
    _series_fixture(schema_session)
    _patch_episode_endpoint(monkeypatch, schema_session)

    result = _get_episode_history("?start=0&length=25")

    assert [item["description"] for item in result["data"]] == ["downloaded"]
    # The total must agree with the rows: a count that still includes the
    # flood makes the pager claim pages that render empty.
    assert result["total"] == 1


def test_episode_history_includes_embedded_on_request(schema_session, monkeypatch):
    _series_fixture(schema_session)
    _patch_episode_endpoint(monkeypatch, schema_session)

    result = _get_episode_history("?start=0&length=25&include_embedded=true")

    assert sorted(item["action"] for item in result["data"]) == [1, 7, 7]
    assert result["total"] == 3


def test_episode_scoped_history_applies_the_same_default(schema_session, monkeypatch):
    """The per-episode listing uses the same default: embedded track state is
    media state, not an event, so it stays out unless asked for."""
    _series_fixture(schema_session)
    _patch_episode_endpoint(monkeypatch, schema_session)

    result = _get_episode_history("?episodeid=100")

    assert [item["action"] for item in result["data"]] == [1]
    assert result["total"] == 1


def test_movie_history_excludes_embedded_by_default(schema_session, monkeypatch):
    _movie_fixture(schema_session)
    _patch_movie_endpoint(monkeypatch, schema_session)

    result = _get_movie_history("?start=0&length=25")

    assert [item["description"] for item in result["data"]] == ["downloaded"]
    assert result["total"] == 1


def test_movie_history_includes_embedded_on_request(schema_session, monkeypatch):
    _movie_fixture(schema_session)
    _patch_movie_endpoint(monkeypatch, schema_session)

    result = _get_movie_history("?start=0&length=25&include_embedded=true")

    assert sorted(item["action"] for item in result["data"]) == [1, 7]
    assert result["total"] == 2

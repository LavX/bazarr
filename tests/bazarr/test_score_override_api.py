# coding=utf-8
"""POST /api/subtitles/score-override: the arr instance that owns the item is
the one the lookup reads, and an empty subtitles path is refused."""

import pytest
from flask import Flask
from sqlalchemy import select

from app.database import TableEpisodes, TableHistory, TableHistoryMovie, TableMovies, TableShows
from subtitles.score_override import MANUAL_SCORE_ACTION

HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
ROOT = "/api/subtitles/score-override"


@pytest.fixture
def score_api(schema_session, monkeypatch):
    import api.subtitles.subtitles as api_mod
    from api import api_bp
    from app.config import settings as app_settings

    monkeypatch.setitem(app_settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(api_mod, "database", schema_session)
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client(), schema_session


def _manual_rows(session):
    return session.execute(
        select(TableHistoryMovie.movie_id, TableHistoryMovie.arr_instance_id).where(
            TableHistoryMovie.action == MANUAL_SCORE_ACTION
        )
    ).all()


def test_score_override_reads_the_item_of_the_instance_named(score_api):
    """Two instances each hold a movie with radarr id 7. The request names
    instance 2, so the row must be the one of instance 2 and not the first
    match on the upstream id."""
    client, session = score_api
    session.add_all(
        [
            TableMovies(
                id=1,
                arr_instance_id=1,
                radarrId=7,
                title="A",
                path="/movies/a.mkv",
                tmdbId="1",
                subtitles=str([["en", "/movies/a.en.srt", 100]]),
            ),
            TableMovies(
                id=2,
                arr_instance_id=2,
                radarrId=7,
                title="B",
                path="/movies/b.mkv",
                tmdbId="2",
                subtitles=str([["en", "/movies/b.en.srt", 100]]),
            ),
        ]
    )
    session.commit()

    response = client.post(
        ROOT,
        json={
            "radarrMovieId": 7,
            "arr_instance_id": 2,
            "subtitlesPath": "/movies/b.en.srt",
            "score": 40,
        },
        headers=HEADERS,
    )

    assert response.status_code == 204
    assert _manual_rows(session) == [(2, 2)]


def test_score_override_refuses_a_path_only_the_other_instance_lists(score_api):
    client, session = score_api
    session.add_all(
        [
            TableMovies(
                id=1,
                arr_instance_id=1,
                radarrId=7,
                title="A",
                path="/movies/a.mkv",
                tmdbId="1",
                subtitles=str([["en", "/movies/a.en.srt", 100]]),
            ),
            TableMovies(
                id=2,
                arr_instance_id=2,
                radarrId=7,
                title="B",
                path="/movies/b.mkv",
                tmdbId="2",
                subtitles=str([["en", "/movies/b.en.srt", 100]]),
            ),
        ]
    )
    session.commit()

    response = client.post(
        ROOT,
        json={
            "radarrMovieId": 7,
            "arr_instance_id": 1,
            "subtitlesPath": "/movies/b.en.srt",
            "score": 40,
        },
        headers=HEADERS,
    )

    assert response.status_code == 404
    assert _manual_rows(session) == []


@pytest.mark.parametrize("score", [None, 150, -1])
def test_score_override_refuses_a_score_that_is_null_or_out_of_range(score_api, score):
    """A null score must be refused with 400 like an out-of-range one. A null
    reached the range check and raised a TypeError, which returned 500."""
    client, session = score_api
    session.add(
        TableMovies(
            id=1,
            arr_instance_id=1,
            radarrId=7,
            title="A",
            path="/movies/a.mkv",
            tmdbId="1",
            subtitles=str([["en", "/movies/a.en.srt", 100]]),
        )
    )
    session.commit()

    response = client.post(
        ROOT,
        json={"radarrMovieId": 7, "subtitlesPath": "/movies/a.en.srt", "score": score},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert _manual_rows(session) == []


@pytest.mark.parametrize("path", [None, ""])
def test_score_override_refuses_an_empty_subtitles_path(score_api, path):
    """An embedded track is listed as [lang, None, None, codec]. A null path
    must not match it and write a row: the request is refused with 400."""
    client, session = score_api
    session.add(
        TableMovies(
            id=1,
            arr_instance_id=1,
            radarrId=7,
            title="A",
            path="/movies/a.mkv",
            tmdbId="1",
            subtitles=str([["en", None, None, "subrip"]]),
        )
    )
    session.commit()

    response = client.post(
        ROOT,
        json={"radarrMovieId": 7, "subtitlesPath": path, "score": 40},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert _manual_rows(session) == []


def _add_movie(session, subtitles=None):
    session.add(
        TableMovies(
            id=1,
            arr_instance_id=1,
            radarrId=7,
            title="A",
            path="/movies/a.mkv",
            tmdbId="1",
            subtitles=str(subtitles or [["en", "/movies/a.en.srt", 100]]),
        )
    )
    session.commit()


def _add_episode(session):
    session.add(TableShows(id=10, arr_instance_id=1, sonarrSeriesId=10, path="/series/show", title="Show"))
    session.add(
        TableEpisodes(
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
            subtitles=str([["en", "/series/show/s01e01.en.srt", 100]]),
        )
    )
    session.commit()


def _manual_history_rows(session):
    return session.execute(
        select(
            TableHistory.action,
            TableHistory.score,
            TableHistory.score_out_of,
            TableHistory.episode_id,
            TableHistory.sonarrEpisodeId,
            TableHistory.language,
            TableHistory.subtitles_path,
        ).where(TableHistory.action == MANUAL_SCORE_ACTION)
    ).all()


def test_score_override_stores_the_episode_row_through_sonarr_episode_id(score_api):
    client, session = score_api
    _add_episode(session)

    response = client.post(
        ROOT,
        json={"sonarrEpisodeId": 20, "subtitlesPath": "/series/show/s01e01.en.srt", "score": 40},
        headers=HEADERS,
    )

    assert response.status_code == 204
    assert _manual_history_rows(session) == [
        (
            MANUAL_SCORE_ACTION,
            round(360 * 40 / 100),
            360,
            20,
            20,
            "en",
            "/series/show/s01e01.en.srt",
        )
    ]


def test_score_override_stores_the_movie_row_on_the_movie_scale(score_api):
    client, session = score_api
    _add_movie(session)

    response = client.post(
        ROOT,
        json={"radarrMovieId": 7, "subtitlesPath": "/movies/a.en.srt", "score": 40},
        headers=HEADERS,
    )

    assert response.status_code == 204
    assert session.execute(
        select(
            TableHistoryMovie.action,
            TableHistoryMovie.score,
            TableHistoryMovie.score_out_of,
            TableHistoryMovie.movie_id,
            TableHistoryMovie.language,
            TableHistoryMovie.subtitles_path,
        ).where(TableHistoryMovie.action == MANUAL_SCORE_ACTION)
    ).all() == [(MANUAL_SCORE_ACTION, round(180 * 40 / 100), 180, 1, "en", "/movies/a.en.srt")]


def test_score_override_refuses_a_request_with_neither_id(score_api):
    client, session = score_api
    _add_movie(session)
    _add_episode(session)

    response = client.post(
        ROOT,
        json={"subtitlesPath": "/movies/a.en.srt", "score": 40},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert _manual_rows(session) == []
    assert _manual_history_rows(session) == []


def test_score_override_refuses_a_request_with_both_ids(score_api):
    client, session = score_api
    _add_movie(session)
    _add_episode(session)

    response = client.post(
        ROOT,
        json={
            "radarrMovieId": 7,
            "sonarrEpisodeId": 20,
            "subtitlesPath": "/movies/a.en.srt",
            "score": 40,
        },
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert _manual_rows(session) == []
    assert _manual_history_rows(session) == []


def test_score_override_emits_a_movie_history_event(score_api, monkeypatch):
    import subtitles.score_override as score_override

    events = []
    monkeypatch.setattr(score_override, "event_stream", lambda **kwargs: events.append(kwargs))
    client, session = score_api
    _add_movie(session)

    response = client.post(
        ROOT,
        json={"radarrMovieId": 7, "subtitlesPath": "/movies/a.en.srt", "score": 40},
        headers=HEADERS,
    )

    assert response.status_code == 204
    assert events == [{"type": "movie-history"}]


def test_score_override_emits_an_episode_history_event(score_api, monkeypatch):
    import subtitles.score_override as score_override

    events = []
    monkeypatch.setattr(score_override, "event_stream", lambda **kwargs: events.append(kwargs))
    client, session = score_api
    _add_episode(session)

    response = client.post(
        ROOT,
        json={"sonarrEpisodeId": 20, "subtitlesPath": "/series/show/s01e01.en.srt", "score": 40},
        headers=HEADERS,
    )

    assert response.status_code == 204
    assert events == [{"type": "episode-history"}]

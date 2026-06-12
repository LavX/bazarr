# coding=utf-8

from datetime import datetime

from flask import Flask


def _history_item(movie_id, arr_instance_id, history_id, title):
    from app.database import TableHistoryMovie, TableMovies

    movie = TableMovies(
        id=movie_id,
        radarrId=1,
        arr_instance_id=arr_instance_id,
        path=f"/movies/{title}.mkv",
        title=title,
        tmdbId=str(movie_id),
        monitored="True",
        subtitles="[]",
        tags="[]",
    )
    history = TableHistoryMovie(
        id=history_id,
        movie_id=movie_id,
        radarrId=1,
        arr_instance_id=arr_instance_id,
        action=1,
        description=f"{title} history",
        language="en",
        provider="provider",
        subtitles_path=f"/movies/{title}.en.srt",
        video_path=f"/movies/{title}.mkv",
        timestamp=datetime(2026, 6, 12, 12, 0, history_id),
    )
    return movie, history


def test_movie_history_filters_by_local_movie_id(schema_session, monkeypatch):
    from api import utils
    from api.movies import history

    monkeypatch.setattr(history, "database", schema_session)
    monkeypatch.setattr(history, "get_upgradable_movies_subtitles", lambda history_id_list: {})
    monkeypatch.setattr(history, "_language_still_desired", lambda language, profile_id: True)
    monkeypatch.setattr(history, "pretty_date", lambda value: "pretty")
    monkeypatch.setattr(utils, "language_from_alpha2", lambda value: {"en": "English"}[value])
    monkeypatch.setattr(utils, "alpha3_from_alpha2", lambda value: {"en": "eng"}[value])

    movie_a, history_a = _history_item(100, 2, 1, "alpha")
    movie_b, history_b = _history_item(200, 3, 2, "beta")
    schema_session.add_all([movie_a, movie_b])
    schema_session.flush()
    schema_session.add_all([history_a, history_b])

    app = Flask(__name__)
    with app.test_request_context("/api/movies/history?id=100"):
        result = history.MoviesHistory.get.__wrapped__(history.MoviesHistory())

    assert [item["description"] for item in result["data"]] == ["alpha history"]
    assert result["data"][0]["id"] == 100
    assert result["data"][0]["arr_instance_id"] == 2
    assert result["total"] == 1

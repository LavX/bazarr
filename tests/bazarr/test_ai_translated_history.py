from types import SimpleNamespace

import pytest


def _result(ai_translated):
    from subtitles.processing import ProcessSubtitlesResult

    return ProcessSubtitlesResult(
        message="downloaded", reversed_path="/media/movie.mkv",
        downloaded_language_code2="en", downloaded_provider="examplehub",
        score=200, forced=False, subtitle_id="sub-1",
        reversed_subtitles_path="/media/movie.en.srt", hearing_impaired=False,
        ai_translated=ai_translated,
    )


@pytest.mark.parametrize("ai_translated", [True, False])
def test_provider_history_writers_store_explicit_flag(monkeypatch, ai_translated):
    from sonarr import history as series
    from radarr import history as movies
    from sportarr import subtitles as sports

    result = _result(ai_translated)
    captured = []
    writer = SimpleNamespace(
        execute=lambda stmt: captured.append(stmt.compile().params),
        commit=lambda: None,
    )
    monkeypatch.setattr(series, "database", writer)
    monkeypatch.setattr(movies, "database", writer)
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(movies, "event_stream", lambda **kwargs: None)
    series.history_log(1, None, None, result, arr_instance_id=1)
    movies.history_log_movie(2, None, result, arr_instance_id=1)
    monkeypatch.setattr(sports, "validate_context", lambda context, session: None)
    rows = []
    sports.sports_history(
        SimpleNamespace(add=rows.append),
        SimpleNamespace(event_id=1, league_id=1, arr_instance_id=1),
        result, action=3,
    )
    assert [item["ai_translated"] for item in captured] == [ai_translated] * 2
    assert rows[0].ai_translated is ai_translated


def test_non_provider_history_action_keeps_unknown_provenance(monkeypatch):
    from sonarr import history as series

    captured = []
    monkeypatch.setattr(series, "database", SimpleNamespace(
        execute=lambda stmt: captured.append(stmt.compile().params), commit=lambda: None
    ))
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)
    series.history_log(6, None, None, _result(True), arr_instance_id=1)
    assert captured[0]["ai_translated"] is None


def test_zero_penalized_provider_score_keeps_upgrade_denominator(monkeypatch):
    from sonarr import history as series
    from radarr import history as movies
    from subliminal_patch.score import MAX_SCORES

    captured = []
    writer = SimpleNamespace(
        execute=lambda stmt: captured.append(stmt.compile().params),
        commit=lambda: None,
    )
    monkeypatch.setattr(series, "database", writer)
    monkeypatch.setattr(movies, "database", writer)
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(movies, "event_stream", lambda **kwargs: None)
    result = _result(True)
    result.score = 0
    series.history_log(1, None, None, result, arr_instance_id=1)
    movies.history_log_movie(1, None, result, arr_instance_id=1)
    assert [row["score"] for row in captured] == [0, 0]
    assert [row["score_out_of"] for row in captured] == [
        MAX_SCORES["episode"], MAX_SCORES["movie"]
    ]
    assert all(row["score"] < row["score_out_of"] - 3 for row in captured)


def test_zero_unflagged_provider_score_keeps_previous_history_shape(monkeypatch):
    from sonarr import history as series
    from radarr import history as movies

    captured = []
    writer = SimpleNamespace(
        execute=lambda stmt: captured.append(stmt.compile().params),
        commit=lambda: None,
    )
    monkeypatch.setattr(series, "database", writer)
    monkeypatch.setattr(movies, "database", writer)
    monkeypatch.setattr(series, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(movies, "event_stream", lambda **kwargs: None)
    result = _result(False)
    result.score = 0
    series.history_log(1, None, None, result, arr_instance_id=1)
    movies.history_log_movie(1, None, result, arr_instance_id=1)
    assert [row["score"] for row in captured] == [0, 0]
    assert [row["score_out_of"] for row in captured] == [None, None]

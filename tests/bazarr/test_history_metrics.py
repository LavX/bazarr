# coding=utf-8

from datetime import datetime, timedelta

from flask import Flask


def _call(metrics, query=""):
    app = Flask(__name__)
    with app.test_request_context(f"/api/history/metrics{query}"):
        return metrics.HistoryMetrics.get.__wrapped__(metrics.HistoryMetrics())


def _episode_row(**over):
    from app.database import TableHistory

    base = dict(
        action=1,
        description="downloaded",
        language="en",
        provider="provider-a",
        score=360,
        score_out_of=360,
        timestamp=datetime.now() - timedelta(days=1),
        video_path="/tv/a.mkv",
    )
    base.update(over)
    return TableHistory(**base)


def _movie_row(**over):
    from app.database import TableHistoryMovie

    base = dict(
        action=1,
        description="downloaded",
        language="en",
        provider="provider-a",
        score=180,
        score_out_of=180,
        timestamp=datetime.now() - timedelta(days=1),
        video_path="/movies/a.mkv",
    )
    base.update(over)
    return TableHistoryMovie(**base)


def test_totals_sum_across_series_movies_and_sports(schema_session, monkeypatch):
    from api.history import metrics
    from app.database import (TableArrInstances, TableHistorySports,
                              TableSportsEvents, TableSportsLeagues)

    monkeypatch.setattr(metrics, "database", schema_session)
    # Sports history carries a composite FK to the owning event and league.
    schema_session.add(TableArrInstances(
        id=1, kind="sportarr", stable_key="s1", name="Sportarr", port=1867,
    ))
    schema_session.flush()
    schema_session.add(TableSportsLeagues(
        id=1, arr_instance_id=1, sportarrLeagueId=1, title="League",
    ))
    schema_session.flush()
    schema_session.add(TableSportsEvents(
        id=1, arr_instance_id=1, league_id=1, sportarrEventId=1, file_id=1,
        path="/sports/a.mkv", title="Event",
    ))
    schema_session.flush()

    schema_session.add(_episode_row())
    schema_session.add(_episode_row())
    schema_session.add(_movie_row())
    schema_session.add(TableHistorySports(
        arr_instance_id=1, league_id=1, event_id=1, action=1,
        description="downloaded", language="en", provider="provider-a",
        score=180, score_out_of=180,
        timestamp=datetime.now() - timedelta(days=1),
    ))
    schema_session.flush()

    body = _call(metrics)

    assert body["totals"]["series"] == 2
    assert body["totals"]["movies"] == 1
    assert body["totals"]["sports"] == 1
    assert body["totals"]["downloads"] == 4


def test_provider_leaderboard_normalises_score_across_tables(schema_session, monkeypatch):
    """A perfect episode (360/360) and a perfect movie (180/180) are both 100%.

    Averaging the raw score would report 270, which is meaningless.
    """
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="alpha", score=360, score_out_of=360))
    schema_session.add(_movie_row(provider="alpha", score=180, score_out_of=180))
    schema_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["count"] == 2
    assert alpha["avgScorePct"] == 100.0


def test_provider_leaderboard_is_ordered_by_downloads(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="busy"))
    schema_session.add(_episode_row(provider="busy"))
    schema_session.add(_episode_row(provider="quiet"))
    schema_session.flush()

    body = _call(metrics)

    assert [p["provider"] for p in body["byProvider"]][:2] == ["busy", "quiet"]


def test_a_zero_score_download_still_counts_towards_quality(schema_session, monkeypatch):
    """score_out_of is NULL whenever score is falsy, which includes a real 0.

    Filtering those rows out would silently drop the worst matches and bias
    every quality average upward, so the denominator is filled in per table.
    """
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="alpha", score=0, score_out_of=None))
    schema_session.add(_episode_row(provider="alpha", score=360, score_out_of=360))
    schema_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["count"] == 2
    assert alpha["avgScorePct"] == 50.0


def test_score_histogram_buckets_by_ten_percent(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(score=36, score_out_of=360))    # 10% -> bucket 1
    schema_session.add(_episode_row(score=180, score_out_of=360))   # 50% -> bucket 5
    schema_session.add(_episode_row(score=360, score_out_of=360))   # 100% -> bucket 10
    schema_session.flush()

    body = _call(metrics)

    counts = {b["bucket"]: b["count"] for b in body["scoreHistogram"]}
    assert counts[1] == 1
    assert counts[5] == 1
    assert counts[10] == 1


def test_a_hash_match_lands_in_the_overflow_bucket(schema_session, monkeypatch):
    """A hash match scores 359 plus other matches against a 360 denominator,
    so percentages above 100 are legitimate and must not be clipped away."""
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(score=700, score_out_of=360))  # ~194%
    schema_session.flush()

    body = _call(metrics)

    counts = {b["bucket"]: b["count"] for b in body["scoreHistogram"]}
    assert counts[10] == 1
    assert max(counts) == 10


def test_language_breakdown_groups_by_language(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(language="en"))
    schema_session.add(_episode_row(language="en"))
    schema_session.add(_movie_row(language="hu"))
    schema_session.flush()

    body = _call(metrics)

    counts = {row["language"]: row["count"] for row in body["byLanguage"]}
    assert counts == {"en": 2, "hu": 1}


def test_action_breakdown_separates_automatic_manual_and_upgrade(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(action=1))
    schema_session.add(_episode_row(action=2))
    schema_session.add(_episode_row(action=3))
    schema_session.add(_episode_row(action=3))
    schema_session.flush()

    body = _call(metrics)

    counts = {row["action"]: row["count"] for row in body["byAction"]}
    assert counts == {1: 1, 2: 1, 3: 2}


def test_provider_reliability_reports_blacklist_rate(schema_session, monkeypatch):
    from api.history import metrics
    from app.database import TableBlacklist

    monkeypatch.setattr(metrics, "database", schema_session)
    for _ in range(4):
        schema_session.add(_episode_row(provider="flaky"))
    schema_session.add(TableBlacklist(
        language="en", provider="flaky", subs_id="x",
        timestamp=datetime.now() - timedelta(days=1),
    ))
    schema_session.flush()

    body = _call(metrics)

    flaky = next(p for p in body["providerReliability"] if p["provider"] == "flaky")
    assert flaky["downloads"] == 4
    assert flaky["blacklisted"] == 1
    assert flaky["ratePct"] == 25.0


def test_filters_narrow_the_result(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="alpha", language="en"))
    schema_session.add(_episode_row(provider="beta", language="hu"))
    schema_session.flush()

    body = _call(metrics, "?provider=alpha")

    assert body["totals"]["downloads"] == 1
    assert [p["provider"] for p in body["byProvider"]] == ["alpha"]


def test_embedded_scans_are_excluded_by_default(schema_session, monkeypatch):
    """Action 7 rows are written at library-scan time with score == score_out_of,
    so including them spikes a day and pins quality at 100%."""
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(action=1))
    schema_session.add(_episode_row(action=7, provider="embedded"))
    schema_session.flush()

    body = _call(metrics)

    assert body["totals"]["downloads"] == 1
    assert [p["provider"] for p in body["byProvider"]] == ["provider-a"]


def test_an_unknown_timeframe_is_rejected_rather_than_crashing(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)

    result = _call(metrics, "?timeFrame=fortnight")

    assert result[1] == 400


def test_a_non_numeric_action_is_rejected_rather_than_crashing(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)

    result = _call(metrics, "?action=upgraded")

    assert result[1] == 400


def test_daily_average_and_peak_day_are_reported(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    busy = datetime.now() - timedelta(days=2)
    schema_session.add(_episode_row(timestamp=busy))
    schema_session.add(_episode_row(timestamp=busy))
    schema_session.add(_episode_row(timestamp=datetime.now() - timedelta(days=1)))
    schema_session.flush()

    body = _call(metrics, "?timeFrame=week")

    assert body["totals"]["peakCount"] == 2
    assert body["totals"]["peakDate"] == busy.strftime("%Y-%m-%d")
    # 3 downloads over a 7 day window.
    assert body["totals"]["dailyAverage"] == 0.43

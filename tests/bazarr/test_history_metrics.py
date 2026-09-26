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


def _sports_event(session):
    """The owner, league and event a sports history or blacklist row needs."""
    from app.database import TableArrInstances, TableSportsEvents, TableSportsLeagues

    session.add(TableArrInstances(
        id=1, kind="sportarr", stable_key="s1", name="Sportarr", port=1867,
    ))
    session.flush()
    session.add(TableSportsLeagues(
        id=1, arr_instance_id=1, sportarrLeagueId=1, title="League",
    ))
    session.flush()
    session.add(TableSportsEvents(
        id=1, arr_instance_id=1, league_id=1, sportarrEventId=1, file_id=1,
        path="/sports/a.mkv", title="Event",
    ))
    session.flush()


def test_a_language_filter_includes_its_variants(schema_session, monkeypatch):
    """The selector offers base codes only, while rows store en:hi or en:forced.

    Exact equality dropped every variant row from the English figures.
    """
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(language="en"))
    schema_session.add(_episode_row(language="en:hi"))
    schema_session.add(_movie_row(language="en:forced"))
    schema_session.add(_episode_row(language="hu"))
    schema_session.flush()

    body = _call(metrics, "?language=en")

    assert body["totals"]["downloads"] == 3


def test_the_activity_chart_counts_the_same_language_variants(schema_session, monkeypatch):
    """The chart sits under the metrics tiles, so both must read one population."""
    from api.history import stats

    monkeypatch.setattr(stats, "database", schema_session)
    schema_session.add(_episode_row(language="en"))
    schema_session.add(_episode_row(language="en:hi"))
    schema_session.add(_movie_row(language="en:forced"))
    schema_session.add(_episode_row(language="hu"))
    schema_session.flush()

    app = Flask(__name__)
    with app.test_request_context("/api/history/stats?language=en"):
        body = stats.HistoryStats.get.__wrapped__(stats.HistoryStats())

    assert sum(day["count"] for day in body["series"]) == 2
    assert sum(day["count"] for day in body["movies"]) == 1


def test_sports_exclusions_count_towards_provider_reliability(schema_session, monkeypatch):
    """Sports downloads are in the denominator, so their exclusions belong in
    the numerator. Without them a sports provider always read 0%."""
    from api.history import metrics
    from app.database import TableBlacklistSports, TableHistorySports

    monkeypatch.setattr(metrics, "database", schema_session)
    _sports_event(schema_session)
    for _ in range(2):
        schema_session.add(TableHistorySports(
            arr_instance_id=1, league_id=1, event_id=1, action=1,
            description="downloaded", language="en", provider="sporty",
            score=180, score_out_of=180,
            timestamp=datetime.now() - timedelta(days=1),
        ))
    schema_session.add(TableBlacklistSports(
        arr_instance_id=1, league_id=1, event_id=1, language="en",
        provider="sporty", subs_id="x",
        timestamp=datetime.now() - timedelta(days=1),
    ))
    schema_session.flush()

    body = _call(metrics)

    sporty = next(p for p in body["providerReliability"] if p["provider"] == "sporty")
    assert sporty["blacklisted"] == 1
    assert sporty["ratePct"] == 50.0


def test_blacklist_rate_is_narrowed_to_the_selected_language(schema_session, monkeypatch):
    """One English download against several Hungarian exclusions read as a
    rate far above 100% while viewing English."""
    from api.history import metrics
    from app.database import TableBlacklist

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="flaky", language="en"))
    schema_session.add(_episode_row(provider="flaky", language="en:hi"))
    for language in ("en:hi", "hu", "hu", "hu"):
        schema_session.add(TableBlacklist(
            language=language, provider="flaky", subs_id=f"x-{language}",
            timestamp=datetime.now() - timedelta(days=1),
        ))
    schema_session.flush()

    body = _call(metrics, "?language=en")

    flaky = next(p for p in body["providerReliability"] if p["provider"] == "flaky")
    assert flaky["downloads"] == 2
    assert flaky["blacklisted"] == 1
    assert flaky["ratePct"] == 50.0


def test_reliability_is_unavailable_for_a_single_action(schema_session, monkeypatch):
    """An exclusion does not record which kind of download it undid, so one
    action's downloads cannot be the denominator for every action's exclusions."""
    from api.history import metrics
    from app.database import TableBlacklist

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="flaky", action=1))
    schema_session.add(_episode_row(provider="flaky", action=2))
    schema_session.add(TableBlacklist(
        language="en", provider="flaky", subs_id="x",
        timestamp=datetime.now() - timedelta(days=1),
    ))
    schema_session.flush()

    body = _call(metrics, "?action=1")

    assert body["providerReliability"] is None
    assert body["totals"]["downloads"] == 1


def test_unscored_downloads_do_not_weigh_on_a_provider_average(schema_session, monkeypatch):
    """AVG skips a NULL score but COUNT(*) does not, so weighting each table's
    average by its row count let unscored rows outvote the scored ones."""
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="alpha", score=360, score_out_of=360))  # 100%
    for _ in range(9):
        schema_session.add(_episode_row(provider="alpha", score=None, score_out_of=None))
    schema_session.add(_movie_row(provider="alpha", score=90, score_out_of=180))  # 50%
    schema_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["count"] == 11
    assert alpha["avgScorePct"] == 75.0


def test_a_provider_with_only_unscored_downloads_has_no_average(schema_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(provider="alpha", score=None, score_out_of=None))
    schema_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["count"] == 1
    assert alpha["avgScorePct"] is None


def test_unscored_downloads_are_left_out_of_the_histogram(schema_session, monkeypatch):
    """A NULL score fell through the CASE into the 0-9% bucket, reporting an
    unmeasured download as the worst match. A real zero still counts."""
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", schema_session)
    schema_session.add(_episode_row(score=None, score_out_of=None))
    schema_session.add(_episode_row(score=0, score_out_of=None))
    schema_session.flush()

    body = _call(metrics)

    counts = {b["bucket"]: b["count"] for b in body["scoreHistogram"]}
    assert counts[0] == 1
    assert sum(counts.values()) == 1


def test_sports_only_providers_are_offered_to_the_statistics_filter(schema_session, monkeypatch):
    """The provider selector read only the episode and movie history, so a
    provider that only ever delivered sports subtitles showed on the
    leaderboard but could not be picked as a filter."""
    from api.providers import providers
    from app.database import TableHistorySports

    monkeypatch.setattr(providers, "database", schema_session)
    _sports_event(schema_session)
    schema_session.add(_movie_row(provider="moviesonly"))
    for provider in ("sportsonly", "manual"):
        schema_session.add(TableHistorySports(
            arr_instance_id=1, league_id=1, event_id=1, action=1,
            description="downloaded", language="en", provider=provider,
            timestamp=datetime.now() - timedelta(days=1),
        ))
    schema_session.flush()

    app = Flask(__name__)
    with app.test_request_context("/api/providers?history=true"):
        answer = providers.Providers.get.__wrapped__(providers.Providers())

    assert [item["name"] for item in answer["data"]] == ["moviesonly", "sportsonly"]

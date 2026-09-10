"""Digital feeds require selected-region release records, never primary dates."""
from datetime import date, datetime, timedelta, timezone

import pytest
import test_discover_metadata as fixtures

upstream = fixtures.upstream
authenticated_client = fixtures.authenticated_client
retrieval_database = fixtures.retrieval_database


def get(client, query="region=US"):
    return client.get("/api/discover/feeds/digital?" + query,
                      headers={"X-API-KEY": "metadata-test-key"})


def release(day, kind=4, region="US"):
    return {"iso_3166_1": region, "release_dates": [{"type": kind, "release_date": day}]}


def configure(upstream, monkeypatch, records=None):
    from discover import feeds
    monkeypatch.setattr(feeds, "_now", lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    records = records if records is not None else {
        1: [release("2026-09-08T00:00:00.000Z")],
        2: [release("2026-08-10T23:00:00-07:00")],
        3: [release("2026-08-09T00:00:00.000Z")],
        4: [release("2026-09-09T00:00:00.000Z")],
        5: [release("2026-09-08", kind=3)],
        6: [release("2026-09-08", kind=5)],
        7: [release("2026-09-08", region="GB")],
        8: [],
    }
    def payload(url):
        if url.endswith("/configuration"):
            return {"images": {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w342"]}}
        if url.endswith("/discover/movie"):
            return {"results": [
                {"id": identity, "title": f"Film {identity}", "release_date": "2026-09-08", "poster_path": "/poster.jpg"}
                for identity in [*records, 1]], "total_pages": 1}
        identity = int(url.split("/")[-2])
        return {"id": identity, "results": records[identity]}
    upstream.payload = payload


def test_only_verified_regional_digital_dates_enter_inclusive_window(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch)
    response = get(authenticated_client)
    assert response.status_code == 200
    feed = response.json
    assert [row["release_date"] for row in feed["items"]] == ["2026-09-08", "2026-08-10"]
    assert all(row["region"] == "US" and row["release_type"] == "digital" for row in feed["items"])
    assert feed["window"] == {"start": "2026-08-10", "end": "2026-09-08"}
    assert feed["coverage"]["missing_region"] == 2
    assert feed["coverage"]["complete"] is False
    assert feed["items"][1]["provenance"]["release_date"] == "2026-08-10T23:00:00-07:00"
    params = next(kwargs["params"] for url, kwargs in upstream.calls if url.endswith("/discover/movie"))
    assert params["region"] == "US" and params["with_release_type"] == 4
    assert params["release_date.gte"] == "2026-08-10" and params["release_date.lte"] == "2026-09-08"
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("today,start", [(date(2026, 1, 1), "2025-12-03"), (date(2024, 3, 1), "2024-02-01"), (date(2026, 3, 1), "2026-01-31")])
def test_calendar_boundaries_are_date_only(authenticated_client, upstream, monkeypatch, today, start):
    from discover.feeds import digital_releases
    configure(upstream, monkeypatch, {1: [release(start)], 2: [release(today.isoformat())]})
    feed = digital_releases(today=today)
    assert feed["window"] == {"start": start, "end": today.isoformat()}
    assert [item["release_date"] for item in feed["items"]] == [today.isoformat(), start]


@pytest.mark.parametrize("query", ["region=us", "region=USA", "region=US&region=GB", "region=US&language=hun", "today=2026-09-08", "with_release_type=3", "region=ZZ"])
def test_invalid_parameters_are_rejected_before_requests(authenticated_client, upstream, query):
    assert get(authenticated_client, query).status_code == 400
    assert upstream.calls == []


def test_authentication_empty_missing_and_unconfigured_truth(authenticated_client, upstream, monkeypatch):
    from app import tmdb
    from app.config import settings
    assert authenticated_client.get("/api/discover/feeds/digital").status_code == 401
    configure(upstream, monkeypatch, {1: []})
    missing = get(authenticated_client).json
    assert missing["status"] == "empty" and missing["coverage"]["missing_region"] == 1
    assert missing["coverage"]["complete"] is False
    # Unconfigured now means no credential anywhere: an empty setting falls back
    # to the key the application ships.
    monkeypatch.setattr(tmdb, "builtin_api_key", lambda: "")
    settings.discover.tmdb_access_token = ""
    assert get(authenticated_client).json["status"] == "unconfigured"
    monkeypatch.setattr(tmdb, "builtin_api_key", lambda: "5ecafe88cafe88cafe88cafe88cafe88")
    upstream.payload = {"results": [], "total_pages": 0}
    empty = get(authenticated_client, "").json
    assert empty["status"] == "empty" and empty["coverage"]["complete"] is True
    assert empty["region"] == "US"


def test_cache_separates_region_window_locale_and_effective_configuration(authenticated_client, upstream, monkeypatch):
    from app.config import settings
    from discover import feeds
    configure(upstream, monkeypatch, {1: [release("2026-09-08"), release("2026-09-07", region="GB")]})
    us = feeds.digital_releases("US", date(2026, 9, 8))
    gb = feeds.digital_releases("GB", date(2026, 9, 8))
    assert us["items"][0]["release_date"] == "2026-09-08"
    assert gb["items"][0]["release_date"] == "2026-09-07"
    assert feeds.digital_releases("US", date(2026, 9, 8))["status"] == "cached"
    feeds.digital_releases("US", date(2026, 9, 9))
    settings.discover.locale = "hu-HU"
    localized = feeds.digital_releases("US", date(2026, 9, 8))
    assert localized["revision"] != us["revision"]
    settings.discover.tmdb_access_token = "rotation-synthetic"
    assert feeds.digital_releases("US", date(2026, 9, 8))["revision"] != localized["revision"]
    admission = [kwargs["params"] for url, kwargs in upstream.calls if url.endswith("/discover/movie")]
    assert len(admission) == 5
    assert [row["region"] for row in admission] == ["US", "GB", "US", "US", "US"]
    assert settings.discover.locale == "hu-HU"
    assert authenticated_client.provider_searches == []


def test_failed_refresh_keeps_original_age_and_auth_failure_hides_items(authenticated_client, upstream, monkeypatch):
    from discover import feeds
    clock = [100.0]
    monkeypatch.setattr(feeds.time, "monotonic", lambda: clock[0])
    configure(upstream, monkeypatch, {1: [release("2026-09-08")]})
    first = get(authenticated_client).json
    clock[0] += 301
    upstream.status = 503
    failed = get(authenticated_client).json
    assert failed["status"] == "cached" and failed["service_status"] == "unavailable"
    assert all(first[key] == failed[key] for key in ("items", "fetched_at", "expires_at", "stale_until", "last_success"))
    count = len(upstream.calls)
    get(authenticated_client)
    assert len(upstream.calls) == count
    clock[0] += 31
    upstream.status = 401
    rejected = get(authenticated_client).json
    assert rejected["status"] == "authentication_failed" and rejected["items"] == []
    clock[0] += 3600
    upstream.status = 503
    assert get(authenticated_client).json["items"] == []


@pytest.mark.parametrize("bad", [None, {}, "bad", [{"iso_3166_1": "US", "release_dates": "bad"}], [release("2026-99-99")], [release("2026-09-08", kind=True)]])
def test_bad_hydration_retains_unaffected_verified_films(authenticated_client, upstream, monkeypatch, bad):
    configure(upstream, monkeypatch, {1: [release("2026-09-08")], 2: bad})
    result = get(authenticated_client).json
    assert [row["id"] for row in result["items"]] == [1]
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["failed"] == 1


def test_duplicates_keep_latest_qualifying_provenance_and_candidate_coverage_is_bounded(authenticated_client, upstream, monkeypatch):
    configure(upstream, monkeypatch, {i: [release("2026-09-05"), release("2026-09-08"), release("2026-09-10")] for i in range(1, 30)})
    result = get(authenticated_client).json
    assert len(result["items"]) == 20
    assert all(item["release_date"] == "2026-09-08" for item in result["items"])
    assert result["coverage"]["truncated"] is True and result["coverage"]["complete"] is False
    assert len(upstream.calls) <= 22
    assert len({item["source_id"] for item in result["items"]}) == 20


@pytest.mark.parametrize("copies", [0, 2])
def test_region_browsing_has_no_database_or_provider_side_effects(authenticated_client, upstream, monkeypatch, retrieval_database, copies):
    import sqlalchemy as sa
    from app import database as db
    engine, session = retrieval_database
    for copy in range(1, copies + 1):
        session.add(db.TableMovies(id=copy, arr_instance_id=copy, radarrId=42, tmdbId="1", imdbId="tt0080274", path=f"/synthetic/{copy}/film.mkv", title="Local copy"))
    session.commit()
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    configure(upstream, monkeypatch)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        for region in ("US", "GB", "HU"):
            assert get(authenticated_client, "region=" + region).status_code == 200
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert statements == []
    assert authenticated_client.provider_searches == []
    assert {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables} == before


def test_hydration_deadline_keeps_completed_provenance_and_late_work_cannot_publish(authenticated_client, upstream, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from discover import feeds, metadata
    configure(upstream, monkeypatch, {1: [release("2026-09-08")], 2: [release("2026-09-07")]})
    monkeypatch.setattr(feeds, "CALL_SECONDS", .15)
    release_worker = threading.Event()
    started = threading.Event()
    original = metadata._request
    def request(config, path, params=None):
        if path == "/movie/2/release_dates":
            started.set()
            assert release_worker.wait(3)
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            before = time.monotonic()
            future = pool.submit(feeds.digital_releases)
            assert started.wait(1)
            result = future.result(timeout=1)
            assert time.monotonic() - before < .8
        assert [row["id"] for row in result["items"]] == [1]
        assert result["coverage"]["failed"] == 1
        assert result["service_status"] == "unavailable"
    finally:
        jobs = list(feeds._jobs.values())
        release_worker.set()
        for job in jobs:
            assert job.done.wait(2)
    assert [row["id"] for row in feeds.digital_releases()["items"]] == [1]


def test_configuration_rotation_during_hydration_cannot_publish(authenticated_client, upstream, monkeypatch):
    from app.config import settings
    from discover import feeds, metadata
    configure(upstream, monkeypatch, {1: [release("2026-09-08")]})
    revision = metadata.configuration().revision
    original = metadata._request
    def request(config, path, params=None):
        value = original(config, path, params)
        if path.endswith("/release_dates"):
            settings.discover.tmdb_access_token = "rotated-during-request"
        return value
    monkeypatch.setattr(metadata, "_request", request)
    response = feeds.digital_releases()
    assert response["items"] == [] and response["revision"] != revision


def test_shared_job_cap_coalesces_regional_work_and_rate_limit_is_global(authenticated_client, upstream, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from discover import feeds, metadata
    configure(upstream, monkeypatch, {1: [release("2026-09-08")]})
    monkeypatch.setattr(feeds, "CALL_SECONDS", .1)
    release_worker = threading.Event()
    started = threading.Event()
    original = metadata._request
    admissions = []
    def request(config, path, params=None):
        admissions.append(params["region"])
        started.set()
        assert release_worker.wait(3)
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(feeds.digital_releases, "US")
            assert started.wait(1)
            duplicate = pool.submit(feeds.digital_releases, "US")
            second = pool.submit(feeds.digital_releases, "GB")
            assert all(future.result(timeout=1)["status"] == "unavailable" for future in (first, duplicate, second))
        assert sorted(admissions) == ["GB", "US"]
        assert len(feeds._jobs) == 2
        assert feeds.digital_releases("HU")["status"] == "unavailable"
        assert feeds.trending()["status"] == "unavailable"
    finally:
        jobs = list(feeds._jobs.values())
        release_worker.set()
        for job in jobs:
            assert job.done.wait(2)
    assert feeds._jobs == {}
    assert feeds.digital_releases("US")["items"] == []
    monkeypatch.setattr(metadata, "_request", original)
    upstream.status = 429
    assert feeds.digital_releases("HU")["status"] == "unavailable"
    calls = len(upstream.calls)
    assert feeds.digital_releases("FR")["status"] == "unavailable"
    assert len(upstream.calls) == calls


@pytest.mark.parametrize("raw", [{}, {"results": [None]}, {"results": [{"id": True, "title": "Bad"}]}, {"results": [{"id": 1, "title": "5ecafe00cafe00cafe00cafe00cafe00"}]}])
def test_malformed_admission_and_credential_echo_never_become_feed_data(authenticated_client, upstream, raw):
    upstream.payload = raw
    response = get(authenticated_client)
    assert response.json["status"] == "unavailable" and response.json["items"] == []
    assert "5ecafe00cafe00cafe00cafe00cafe00" not in response.get_data(as_text=True)


def configure_admission_rows(upstream, monkeypatch, rows):
    configure(upstream, monkeypatch, {1: [release("2026-09-08T00:00:00.000Z")]})
    original = upstream.payload
    upstream.payload = lambda url: {"results": rows, "total_pages": 1} if url.endswith("/discover/movie") else original(url)


@pytest.mark.parametrize("rows,candidates,failed", [
    ([{"id": 1, "title": "Verified film"}], 1, 0),
    ([{"id": 1, "title": "Verified film"}, {"id": 2, "title": None}], 2, 1),
    ([{"id": 2, "title": None}, {"id": 1, "title": "Verified film"}], 2, 1),
    ([{"id": 1, "title": None}, {"id": 1, "title": "Verified film"}], 1, 0),
    ([{"id": 1, "title": "Verified film"}, {"id": 1, "title": None}], 1, 0),
    ([{"id": 1, "title": None}, {"id": 2, "title": ""}, {"id": 1, "title": "Verified film"}, {"id": 2, "title": None}], 2, 1),
    ([{"id": 1, "title": "Verified film"}, {"id": 2, "title": None}, {"id": 2, "title": ""}], 2, 1),
])
def test_admission_isolates_malformed_siblings_and_recovers_duplicate_identity(authenticated_client, upstream, monkeypatch, rows, candidates, failed):
    configure_admission_rows(upstream, monkeypatch, rows)
    response = get(authenticated_client)
    assert response.status_code == 200
    feed = response.json
    assert [item["id"] for item in feed["items"]] == [1]
    assert feed["items"][0]["title"] == "Verified film"
    assert feed["items"][0]["provenance"] == {
        "source": "tmdb", "path": "/movie/1/release_dates", "region": "US", "type": 4,
        "release_date": "2026-09-08T00:00:00.000Z",
    }
    assert feed["coverage"] == {"candidate_limit": 20, "candidates": candidates, "checked": 1,
                                "missing_region": 0, "failed": failed, "truncated": False, "complete": failed == 0}
    assert feed["service_status"] == ("unavailable" if failed else None)
    assert [url.rsplit("/3", 1)[1] for url, _ in upstream.calls if "/release_dates" in url] == ["/movie/1/release_dates"]
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("bad", [None, [], {}, {"title": "Missing identity"},
                                  {"id": True, "title": "Boolean"}, {"id": "1", "title": "String"},
                                  {"id": 0, "title": "Zero"}, {"id": -1, "title": "Negative"},
                                  {"id": 2**53, "title": "Unsafe integer"}])
def test_unidentified_admission_rows_each_remain_one_failed_unit(authenticated_client, upstream, monkeypatch, bad):
    rows = [bad, {"id": 1, "title": "Verified film"}, bad]
    configure_admission_rows(upstream, monkeypatch, rows)
    feed = get(authenticated_client).json
    assert [item["id"] for item in feed["items"]] == [1]
    assert feed["coverage"] == {"candidate_limit": 20, "candidates": 3, "checked": 1,
                                "missing_region": 0, "failed": 2, "truncated": False, "complete": False}
    assert len([url for url, _ in upstream.calls if "/release_dates" in url]) == 1


def test_admission_failure_accounting_cannot_exceed_bounded_raw_rows(authenticated_client, upstream, monkeypatch):
    rows = [{"id": 1, "title": "Verified film"}] + [{"id": 2, "title": None}] * 18 + [None, {"id": 3, "title": "Beyond page"}]
    configure_admission_rows(upstream, monkeypatch, rows)
    feed = get(authenticated_client).json
    assert [item["id"] for item in feed["items"]] == [1]
    assert feed["coverage"] == {"candidate_limit": 20, "candidates": 3, "checked": 1,
                                "missing_region": 0, "failed": 2, "truncated": True, "complete": False}
    assert feed["coverage"]["candidates"] <= min(len(rows), feed["coverage"]["candidate_limit"])
    assert len(upstream.calls) == 3


@pytest.mark.parametrize("rows,candidates", [
    ([None, None], 2),
    ([{"id": 2, "title": None}, {"id": 2, "title": ""}], 1),
    ([{"id": 2, "title": None}, {"id": 3, "title": ""}, {"id": True, "title": "Bad identity"}], 3),
])
def test_wholly_malformed_candidate_page_is_unavailable_with_failed_coverage(authenticated_client, upstream, monkeypatch, rows, candidates):
    configure_admission_rows(upstream, monkeypatch, rows)
    feed = get(authenticated_client).json
    assert feed["status"] == "unavailable" and feed["items"] == []
    assert feed["coverage"] == {"candidate_limit": 20, "candidates": candidates, "checked": 0,
                                "missing_region": 0, "failed": candidates, "truncated": False, "complete": False}
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("payload", [{}, {"results": None}, {"results": "malformed"},
                                     {"results": [{"id": 1, "title": "Verified film"}, {"id": 2, "title": "5ecafe00cafe00cafe00cafe00cafe00"}], "total_pages": 1}])
def test_invalid_whole_envelope_or_credential_echo_rejects_all_candidates(authenticated_client, upstream, payload):
    upstream.payload = payload
    feed = get(authenticated_client).json
    assert feed["status"] == "unavailable" and feed["items"] == []
    assert feed["coverage"]["candidates"] == feed["coverage"]["failed"] == 0
    assert len(upstream.calls) == 1
    assert "5ecafe00cafe00cafe00cafe00cafe00" not in str(feed)



def control_digital_time(monkeypatch):
    from discover import feeds
    elapsed = [0]
    monkeypatch.setattr(feeds.time, "monotonic", lambda: 100.0 + elapsed[0])
    monkeypatch.setattr(feeds, "_now", lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc) + timedelta(seconds=elapsed[0]))
    def advance(seconds):
        elapsed[0] += seconds
    return advance


@pytest.mark.parametrize("initial_rows", [
    [{"id": 1, "title": "Verified film"}],
    [{"id": 1, "title": "Verified film"}, None, None],
    [],
], ids=["verified", "partial", "empty"])
def test_malformed_refresh_retains_success_payload_coverage_age_and_retry_then_recovers(authenticated_client, upstream, monkeypatch, initial_rows):
    configure_admission_rows(upstream, monkeypatch, initial_rows)
    advance = control_digital_time(monkeypatch)
    original_payload = upstream.payload
    first = get(authenticated_client).json
    retained = {name: first[name] for name in ("items", "coverage", "fetched_at", "expires_at", "stale_until", "last_success")}
    assert first["fetched_at"] == first["last_success"] == "2026-09-08T12:00:00+00:00"
    assert first["expires_at"] == "2026-09-08T12:05:00+00:00"
    assert first["stale_until"] == "2026-09-08T13:00:00+00:00"
    if initial_rows:
        assert [item["id"] for item in first["items"]] == [1]
        assert first["items"][0]["provenance"]["release_date"] == "2026-09-08T00:00:00.000Z"
    advance(301)
    upstream.payload = {"results": [{"id": 2, "title": None}], "total_pages": 1}
    failed = get(authenticated_client).json
    assert {name: failed[name] for name in retained} == retained
    assert failed["status"] == ("cached" if initial_rows else "empty")
    assert failed["service_status"] == "unavailable"
    assert failed["attempted_at"] == "2026-09-08T12:05:01+00:00"
    calls = len(upstream.calls)
    advance(29)
    throttled = get(authenticated_client).json
    assert len(upstream.calls) == calls
    assert {name: throttled[name] for name in retained} == retained
    assert throttled["service_status"] == "unavailable"
    advance(1)
    repeated = get(authenticated_client).json
    assert len(upstream.calls) == calls + 1
    assert {name: repeated[name] for name in retained} == retained
    assert repeated["service_status"] == "unavailable"
    advance(30)
    upstream.payload = original_payload
    recovered = get(authenticated_client).json
    assert recovered["items"] == first["items"] and recovered["coverage"] == first["coverage"]
    assert recovered["status"] == ("live" if initial_rows else "empty")
    assert recovered["service_status"] == first["service_status"]
    assert recovered["fetched_at"] == recovered["last_success"] == "2026-09-08T12:06:01+00:00"
    assert recovered["expires_at"] == "2026-09-08T12:11:01+00:00"
    assert recovered["stale_until"] == "2026-09-08T13:06:01+00:00"


def test_cold_repeated_malformed_attempts_have_failure_coverage_without_success_age(authenticated_client, upstream, monkeypatch):
    configure_admission_rows(upstream, monkeypatch, [{"id": 2, "title": None}])
    advance = control_digital_time(monkeypatch)
    first = get(authenticated_client).json
    assert first["status"] == first["service_status"] == "unavailable"
    assert first["items"] == []
    assert all(first[name] is None for name in ("fetched_at", "expires_at", "stale_until", "last_success"))
    assert first["coverage"] == {"candidate_limit": 20, "candidates": 1, "checked": 0,
                                 "missing_region": 0, "failed": 1, "truncated": False, "complete": False}
    advance(29)
    throttled = get(authenticated_client).json
    assert len(upstream.calls) == 1
    assert throttled["coverage"] == first["coverage"]
    upstream.payload = {"results": [None, None], "total_pages": 1}
    advance(1)
    repeated = get(authenticated_client).json
    assert len(upstream.calls) == 2
    assert repeated["coverage"]["candidates"] == repeated["coverage"]["failed"] == 2
    assert repeated["status"] == repeated["service_status"] == "unavailable"
    assert all(repeated[name] is None for name in ("fetched_at", "expires_at", "stale_until", "last_success"))
    assert repeated["attempted_at"] == "2026-09-08T12:00:30+00:00"
    advance(30)
    upstream.payload = {"results": [], "total_pages": 0}
    empty = get(authenticated_client).json
    assert len(upstream.calls) == 3
    assert empty["status"] == "empty" and empty["service_status"] is None
    assert empty["coverage"]["candidates"] == empty["coverage"]["failed"] == 0
    assert empty["coverage"]["complete"] is True
    assert empty["fetched_at"] == empty["last_success"] == "2026-09-08T12:01:00+00:00"
    assert empty["expires_at"] == "2026-09-08T12:06:00+00:00"
    assert empty["stale_until"] == "2026-09-08T13:01:00+00:00"
    advance(31)
    assert get(authenticated_client).json["coverage"] == empty["coverage"]
    assert len(upstream.calls) == 3


def test_malformed_retry_cannot_extend_or_revive_expired_success(authenticated_client, upstream, monkeypatch):
    configure_admission_rows(upstream, monkeypatch, [{"id": 1, "title": "Verified film"}])
    advance = control_digital_time(monkeypatch)
    original_payload = upstream.payload
    first = get(authenticated_client).json
    assert [item["id"] for item in first["items"]] == [1]
    upstream.payload = {"results": [{"id": 2, "title": None}, None], "total_pages": 1}
    advance(3599)
    retained = get(authenticated_client).json
    assert retained["items"] == first["items"] and retained["coverage"] == first["coverage"]
    assert retained["stale_until"] == first["stale_until"]
    calls = len(upstream.calls)
    advance(1)
    expired = get(authenticated_client).json
    assert len(upstream.calls) == calls
    assert expired["items"] == [] and expired["status"] == "unavailable"
    assert expired["service_status"] == "unavailable"
    assert expired["coverage"] == {"candidate_limit": 20, "candidates": 2, "checked": 0,
                                   "missing_region": 0, "failed": 2, "truncated": False, "complete": False}
    assert all(expired[name] is None for name in ("fetched_at", "expires_at", "stale_until"))
    assert expired["last_success"] == first["last_success"]
    advance(29)
    repeated = get(authenticated_client).json
    assert len(upstream.calls) == calls + 1
    assert repeated["items"] == [] and repeated["coverage"] == expired["coverage"]
    assert repeated["last_success"] == first["last_success"]
    assert all(repeated[name] is None for name in ("fetched_at", "expires_at", "stale_until"))
    advance(30)
    upstream.payload = original_payload
    recovered = get(authenticated_client).json
    assert recovered["status"] == "live" and recovered["items"] == first["items"]
    assert recovered["service_status"] is None
    assert recovered["fetched_at"] == recovered["last_success"] == "2026-09-08T13:00:59+00:00"


def test_new_transport_failure_does_not_relabel_earlier_malformed_coverage(authenticated_client, upstream, monkeypatch):
    configure_admission_rows(upstream, monkeypatch, [None, None])
    advance = control_digital_time(monkeypatch)
    assert get(authenticated_client).json["coverage"]["failed"] == 2
    advance(30)
    upstream.status = 503
    failed = get(authenticated_client).json
    assert failed["status"] == "unavailable" and failed["items"] == []
    assert failed["coverage"]["candidates"] == failed["coverage"]["failed"] == 0
    assert all(failed[name] is None for name in ("fetched_at", "expires_at", "stale_until", "last_success"))

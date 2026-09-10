"""Global feed behavior through the real authenticated API and safe transport."""
import pytest
import test_discover_metadata as metadata_fixtures

upstream = metadata_fixtures.upstream
authenticated_client = metadata_fixtures.authenticated_client
retrieval_database = metadata_fixtures.retrieval_database


def get(client, query="media_type=all"):
    return client.get("/api/discover/feeds/trending?" + query, headers={"X-API-KEY": "metadata-test-key"})


def payload(url):
    if url.endswith("/configuration"):
        return {"images": {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w342"], "backdrop_sizes": ["w780"]}}
    rows = [
        {"id": 42, "media_type": "movie", "title": "Northern Light", "release_date": "2008-09-08", "poster_path": "/poster.jpg", "backdrop_path": "/landscape.jpg", "overview": "A journey through the north."},
        {"id": 17, "media_type": "person", "name": "A person"},
        {"id": 42, "media_type": "tv", "name": "The Long Winter", "first_air_date": "2018-01-01"},
    ]
    if "/trending/movie/" in url:
        rows = [rows[0]]
    if "/trending/tv/" in url:
        rows = [rows[2]]
    return {"results": rows}


@pytest.mark.parametrize("copies", [0, 2])
def test_weekly_global_ranking_is_independent_of_empty_library(authenticated_client, upstream, retrieval_database, copies):
    import sqlalchemy as sa
    from app import database as db
    engine, session = retrieval_database
    for copy in range(1, copies + 1):
        session.add(db.TableMovies(id=copy, arr_instance_id=copy, radarrId=7, tmdbId="42", imdbId="tt0080274",
                                   path=f"/synthetic/library-{copy}/film.mkv", title="Wrong local ranking"))
    session.commit()
    tables = db.Base.metadata.sorted_tables
    before = {table.name: session.execute(sa.select(table)).all() for table in tables}
    local_library_count = session.execute(sa.select(sa.func.count()).select_from(db.TableMovies)).scalar()
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    upstream.payload = payload
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        response = get(authenticated_client)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert response.json["source"] == "tmdb"
    assert response.json["period"] == "week"
    assert {r["media_type"] for r in response.json["items"]} == {"movie", "series"}
    assert [r["source_id"] for r in response.json["items"]] == ["tmdb:movie:42", "tmdb:show:42"]
    assert [r["rank"] for r in response.json["items"]] == [1, 3]
    assert authenticated_client.provider_searches == []
    assert local_library_count == copies
    assert statements == []
    assert {table.name: session.execute(sa.select(table)).all() for table in tables} == before


@pytest.mark.parametrize("query", ["media_type=all", "media_type=movie", "media_type=series"])
def test_feed_is_authenticated_and_parameters_are_source_scoped(authenticated_client, upstream, query):
    upstream.payload = payload
    assert authenticated_client.get("/api/discover/feeds/trending").status_code == 401
    response = get(authenticated_client, query)
    assert response.status_code == 200
    assert response.json["status"] == "live"
    assert response.json["fetched_at"] == response.json["last_success"]
    assert response.json["expires_at"] > response.json["fetched_at"]
    cached = get(authenticated_client, query).json
    assert cached["status"] == "cached"
    assert cached["fetched_at"] == response.json["fetched_at"]
    assert len(upstream.calls) <= 2


@pytest.mark.parametrize("query", ["media_type=tv", "media_type=all&media_type=movie", "period=day", "page=2", "media_type=all&language=hun"])
def test_unknown_or_repeated_parameters_never_reach_upstream(authenticated_client, upstream, query):
    assert get(authenticated_client, query).status_code == 400
    assert upstream.calls == []


def test_unconfigured_empty_and_authentication_failure_remain_distinct(authenticated_client, upstream,
                                                                       monkeypatch):
    from app import tmdb
    from app.config import settings
    # Unconfigured now means no credential anywhere, because the application
    # ships its own key and an empty setting simply falls back to it.
    monkeypatch.setattr(tmdb, "builtin_api_key", lambda: "")
    settings.discover.tmdb_access_token = ""
    assert get(authenticated_client).json["status"] == "unconfigured"
    assert upstream.calls == []
    # Restore the built-in key alone, with no stored token: that is the ordinary
    # installation, and the feed has to work in it.
    monkeypatch.setattr(tmdb, "builtin_api_key", lambda: "5ecafe99cafe99cafe99cafe99cafe99")
    upstream.payload = {"results": []}
    result = get(authenticated_client).json
    assert result["status"] == "empty" and result["last_success"]
    assert len(upstream.calls) == 1
    upstream.status = 401
    # A different usable credential, so the cached empty result is not reused
    # and the rejection is what the feed actually reports.
    settings.discover.tmdb_access_token = "5ecafeddcafeddcafeddcafeddcafedd"
    assert get(authenticated_client).json["status"] == "authentication_failed"


def test_cache_scope_includes_filter_locale_and_configuration(authenticated_client, upstream):
    from app.config import settings
    upstream.payload = payload
    responses = [get(authenticated_client, "media_type=" + kind).json for kind in ("all", "movie", "series")]
    assert [{r["media_type"] for r in response["items"]} for response in responses] == [{"movie", "series"}, {"movie"}, {"series"}]
    assert [url.rsplit("/", 2)[-2] for url, _ in upstream.calls if "/trending/" in url] == ["all", "movie", "tv"]
    settings.discover.locale = "hu-HU"
    locale = get(authenticated_client).json
    assert locale["revision"] != responses[0]["revision"]
    assert locale["locale"] == "hu-HU"
    settings.discover.tmdb_access_token = "rotated-private-synthetic"
    rotated = get(authenticated_client).json
    assert rotated["revision"] != locale["revision"]
    assert "rotated-private-synthetic" not in str(rotated)
    assert authenticated_client.provider_searches == []


def test_failed_refresh_retains_original_times_then_expires(authenticated_client, upstream, monkeypatch):
    from discover import feeds
    clock = [100.0]
    monkeypatch.setattr(feeds.time, "monotonic", lambda: clock[0])
    upstream.payload = payload
    first = get(authenticated_client).json
    clock[0] += 301
    upstream.status = 503
    cached = get(authenticated_client).json
    assert cached["status"] == "cached" and cached["service_status"] == "unavailable"
    assert all(cached[k] == first[k] for k in ("items", "fetched_at", "expires_at", "stale_until", "last_success"))
    calls = len(upstream.calls)
    assert get(authenticated_client).json["status"] == "cached"
    assert len(upstream.calls) == calls
    clock[0] += 3600
    expired = get(authenticated_client).json
    assert expired["status"] == "unavailable" and expired["items"] == []
    assert expired["last_success"] == first["fetched_at"]


@pytest.mark.parametrize("raw", [{}, {"results": "bad"}, {"results": [None]}, {"results": [{"id": True, "media_type": "movie", "title": "bad"}]}, {"results": [{"id": 1, "media_type": "movie", "title": "5ecafe00cafe00cafe00cafe00cafe00"}]}])
def test_malformed_or_credential_echo_feed_fails_safely(authenticated_client, upstream, raw):
    upstream.payload = raw
    response = get(authenticated_client)
    assert response.json["status"] == "unavailable"
    assert response.json["items"] == []
    assert "5ecafe00cafe00cafe00cafe00cafe00" not in response.get_data(as_text=True)


def test_feed_bounds_page_size_and_uses_only_validated_image_urls(authenticated_client, upstream):
    upstream.payload = lambda url: payload(url) if url.endswith("/configuration") else {"results": [
        {"id": i + 1, "media_type": "movie", "title": "A film", "poster_path": "/poster.jpg", "backdrop_path": "https://invalid.example/art.jpg"} for i in range(100)]}
    result = get(authenticated_client).json
    assert len(result["items"]) == 20
    assert all(row["poster_url"] == "https://image.tmdb.org/t/p/w342/poster.jpg" and row["backdrop_url"] is None for row in result["items"])
    assert len(upstream.calls) == 2


def test_obsolete_response_cannot_publish_after_configuration_rotation(authenticated_client, upstream, monkeypatch):
    from discover import metadata
    from app.config import settings
    upstream.payload = payload
    old_revision = metadata.configuration().revision
    original = metadata._request
    def request(config, path, params=None):
        result = original(config, path, params)
        settings.discover.tmdb_access_token = "replacement-token"
        metadata.configuration()
        return result
    monkeypatch.setattr(metadata, "_request", request)
    response = get(authenticated_client).json
    assert response["items"] == []
    assert response["revision"] != old_revision


def test_total_caller_deadline_coalesces_and_bounds_uncancelled_workers(authenticated_client, upstream, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from discover import feeds, metadata
    upstream.payload = payload
    monkeypatch.setattr(feeds, "CALL_SECONDS", 0.1)
    original = metadata._request
    release = threading.Event()
    started = threading.Event()
    calls = []
    def request(config, path, params=None):
        calls.append(path)
        started.set()
        assert release.wait(3)
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            before = time.monotonic()
            first = pool.submit(feeds.trending, "all")
            assert started.wait(1)
            duplicate = pool.submit(feeds.trending, "all")
            second = pool.submit(feeds.trending, "movie")
            results = [first.result(timeout=1), duplicate.result(timeout=1), second.result(timeout=1)]
            assert time.monotonic() - before < 0.8
        assert all(row["status"] == "unavailable" for row in results)
        assert len(feeds._jobs) == 2
        assert feeds.trending("series")["status"] == "unavailable"
        assert sorted(calls) == ["/trending/all/week", "/trending/movie/week"]
        jobs = list(feeds._jobs.values())
    finally:
        release.set()
        for job in list(feeds._jobs.values()):
            assert job.done.wait(2)
    assert feeds._jobs == {}
    assert all(job.status == "unavailable" for job in jobs)
    assert feeds.trending("all")["items"] == []
    assert len(calls) == 2


def test_rate_limit_is_honored_across_feed_filters(authenticated_client, upstream):
    upstream.status = 429
    assert get(authenticated_client).json["status"] == "unavailable"
    assert get(authenticated_client, "media_type=series").json["status"] == "unavailable"
    assert len(upstream.calls) == 1


def test_feed_cache_has_fixed_storage_bound(authenticated_client, upstream):
    from discover import feeds
    from app.config import settings
    upstream.payload = {"results": []}
    for index in range(67):
        settings.discover.tmdb_access_token = f"cache-bound-synthetic-{index}"
        assert get(authenticated_client).json["status"] == "empty"
    assert feeds._cache.currsize <= 64

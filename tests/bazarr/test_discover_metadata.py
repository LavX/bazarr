"""Authenticated metadata browsing with deterministic TMDB transport fixtures."""
import json
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

import test_discover_search as search_fixtures
import test_config_save as config_fixtures

retrieval_database = search_fixtures.retrieval_database
metadata_save_environment = config_fixtures.metadata_save_environment


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    fixture = SimpleNamespace(status=200, error=None, calls=calls, payload=None,
                              encode=lambda payload: json.dumps(payload).encode())

    def request(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = fixture.status
        if url.endswith("announcements.json"):
            response.status_code = 200
            response._content = b"[]"
            return response
        assert url.startswith("https://api.themoviedb.org/3/")
        calls.append((url, kwargs))
        if fixture.error:
            raise fixture.error
        payload = fixture.payload(url) if callable(fixture.payload) else fixture.payload
        if payload is None:
            if url.endswith("/search/movie"):
                payload = {"results": [{"id": 42, "title": "Shōgun", "release_date": "1980-01-01",
                                        "overview": "A film outside the library.", "poster_path": None}]}
            elif url.endswith("/movie/42"):
                payload = {"id": 42, "title": "Shōgun", "release_date": "1980-01-01",
                           "overview": "A film outside the library.", "imdb_id": "tt0080274",
                           "poster_path": None}
            else:
                payload = {"success": True}
        response._content = fixture.encode(payload)
        response._content_consumed = True
        response.iter_content = lambda chunk_size: iter([response.content])
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return fixture


@pytest.fixture
def authenticated_client(upstream, monkeypatch):
    from api import api_bp
    from app.config import settings
    from compat import service
    from discover import metadata
    monkeypatch.setattr(metadata, "_current", None)

    previous = settings.get("discover", {}).copy()
    settings.set("discover", {"tmdb_access_token": "synthetic-metadata-token", "locale": "en-US"})
    monkeypatch.setattr(settings.auth, "apikey", "metadata-test-key")
    monkeypatch.setattr(settings.compat_endpoint, "enabled", False)
    provider_searches = []
    monkeypatch.setattr(service, "search", lambda *a, **kw: provider_searches.append(a))
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    client = app.test_client()
    client.provider_searches = provider_searches
    yield client
    settings.set("discover", previous)


def get(client, path):
    return client.get("/api/discover/metadata/" + path, headers={"X-API-KEY": "metadata-test-key"})


def test_search_is_authenticated_cached_and_never_submits_providers(authenticated_client, upstream):
    first = get(authenticated_client, "search?q=Shogun&type=movie")
    second = get(authenticated_client, "search?q=Shogun&type=movie")
    assert first.status_code == second.status_code == 200
    assert len(upstream.calls) == 1
    assert first.json["data"]["items"][0]["source_id"] == "tmdb:movie:42"
    assert second.json["data"]["status"] == "cached"
    assert first.json["data"]["fetched_at"] == second.json["data"]["fetched_at"]
    assert authenticated_client.provider_searches == []
    assert "synthetic-metadata-token" not in first.get_data(as_text=True)


def test_metadata_requires_bazarr_authentication(authenticated_client):
    assert authenticated_client.get("/api/discover/metadata/status").status_code == 401
    assert authenticated_client.get("/api/discover/metadata/status", headers={"X-API-KEY": "wrong"}).status_code == 401


def test_real_settings_serializer_omits_write_only_value(authenticated_client):
    from app.config import get_settings, settings
    result = get_settings()
    assert "tmdb_access_token" not in result["discover"]
    assert result["discover"]["tmdb_configured"] is True
    assert result["discover"]["metadata_revision"]
    assert result["auth"]["apikey"] == settings.auth.apikey
    assert result["general"]["enabled_providers"] == list(settings.general.enabled_providers)


@pytest.mark.parametrize("query", ["", "   ", "...!!!", "---"])
def test_empty_normalized_query_cannot_reuse_results(authenticated_client, upstream, query):
    response = get(authenticated_client, "search?type=movie&q=" + requests.utils.quote(query))
    assert response.status_code == 400
    assert upstream.calls == []


def test_details_resolve_identity_without_subtitle_lookup(authenticated_client):
    response = get(authenticated_client, "movies/42")
    assert response.status_code == 200
    movie = response.json["data"]["item"]
    assert (movie["source_id"], movie["imdb_id"], movie["year"], movie["media_type"]) == (
        "tmdb:movie:42", "tt0080274", 1980, "movie")
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("status,expected", [(401, "authentication_failed"), (403, "authentication_failed"),
                                             (429, "unavailable"), (500, "unavailable")])
def test_upstream_failures_are_safe_domain_outcomes(authenticated_client, upstream, status, expected):
    upstream.status = status
    response = authenticated_client.post("/api/discover/metadata/test", json={"token": "candidate-private"},
                                         headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 200
    assert response.json["data"]["status"] == expected
    assert "candidate-private" not in response.get_data(as_text=True)


def test_connection_check_does_not_save_candidate(authenticated_client):
    from app.config import settings
    before = settings.discover.tmdb_access_token
    response = authenticated_client.post("/api/discover/metadata/test", json={"token": "draft-token"},
                                         headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 200
    assert response.json["data"]["status"] == "available"
    assert settings.discover.tmdb_access_token == before


@pytest.mark.parametrize("query", ["Shōgun", "SHOGUN", "...Shogun!!!", " Shogun "])
def test_normalization_reuses_same_source_query(authenticated_client, upstream, query):
    get(authenticated_client, "search?q=Shogun&type=movie")
    result = get(authenticated_client, "search?q=" + requests.utils.quote(query))
    assert result.json["data"]["status"] == "cached"
    assert len(upstream.calls) == 1
    assert upstream.calls[0][1]["params"]["query"] == "shogun"
    assert upstream.calls[0][1]["allow_redirects"] is False
    assert upstream.calls[0][1]["stream"] is True
    assert upstream.calls[0][1]["timeout"] == (3.05, 8)


@pytest.mark.parametrize("value", [None, "", "not-imdb", "tt123", True])
def test_unresolved_mapping_is_never_guessed(authenticated_client, upstream, value):
    upstream.payload = {"id": 42, "title": "Unknown film", "imdb_id": value}
    movie = get(authenticated_client, "movies/42").json["data"]["item"]
    assert movie["imdb_id"] is None
    assert movie["mapping_status"] == "unresolved"
    assert authenticated_client.provider_searches == []


@pytest.mark.parametrize("failure", [requests.Timeout("synthetic-sensitive-exception"), requests.ConnectionError("synthetic-sensitive-exception")])
def test_timeouts_and_transport_errors_do_not_leak(authenticated_client, upstream, failure):
    upstream.error = failure
    response = get(authenticated_client, "search?q=Shogun")
    assert response.status_code == 200
    assert response.json["data"]["status"] == "unavailable"
    assert "synthetic-sensitive-exception" not in response.get_data(as_text=True)


def test_missing_key_never_calls_upstream(authenticated_client, upstream):
    from app.config import settings
    settings.discover.tmdb_access_token = ""
    for path in ["status", "search?q=Shogun", "movies/42"]:
        result = get(authenticated_client, path)
        assert result.status_code == 200
        assert result.json["data"]["status"] == "unconfigured"
    assert upstream.calls == []


@pytest.mark.parametrize("payload", [{}, {"results": "bad"}, {"results": [{"id": True, "title": "Invalid"}]},
                                     {"results": [{"id": 42, "title": "synthetic-metadata-token"}]},
                                     {"results": [{"id": 42, "title": "x" * (1024 * 1024)}]}])
def test_invalid_or_oversized_responses_fail_closed(authenticated_client, upstream, payload):
    upstream.payload = payload
    response = get(authenticated_client, "search?q=Shogun")
    assert response.json["data"]["status"] == "unavailable"
    assert response.json["data"]["items"] == []
    assert "synthetic-metadata-token" not in response.get_data(as_text=True)


def test_cache_has_bounded_stale_fallback_and_no_cross_rotation_reuse(authenticated_client, upstream, monkeypatch):
    from discover import metadata
    from app.config import settings
    now = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: now[0])
    first = get(authenticated_client, "search?q=Shogun").json["data"]
    now[0] += 301
    upstream.status = 503
    stale = get(authenticated_client, "search?q=Shogun").json["data"]
    assert stale["status"] == "cached" and stale["service_status"] == "unavailable"
    assert stale["fetched_at"] == first["fetched_at"]
    now[0] += 3600
    assert get(authenticated_client, "search?q=Shogun").json["data"]["status"] == "unavailable"
    settings.discover.tmdb_access_token = "rotated-synthetic-token"
    rotated = get(authenticated_client, "search?q=Shogun").json["data"]
    assert rotated["revision"] != first["revision"]
    assert rotated["items"] == []


def test_obsolete_inflight_response_cannot_repopulate_cache(authenticated_client, upstream, monkeypatch):
    from discover import metadata
    from app.config import settings
    original = metadata._request
    first_revision = metadata.configuration().revision
    def rotate(config, path, params=None):
        result = original(config, path, params)
        settings.discover.tmdb_access_token = "new-token-after-request"
        metadata.invalidate_metadata()
        return result
    monkeypatch.setattr(metadata, "_request", rotate)
    response = get(authenticated_client, "search?q=Shogun").json["data"]
    assert response["items"] == []
    assert response["revision"] != first_revision
    monkeypatch.setattr(metadata, "_request", original)
    assert get(authenticated_client, "search?q=Shogun").json["data"]["status"] == "available"
    assert len(upstream.calls) == 2


def test_metadata_locale_is_separate_from_language_and_has_its_own_cache(authenticated_client, upstream):
    from app.config import settings
    first = get(authenticated_client, "search?q=Shogun").json["data"]
    settings.discover.locale = "hu-HU"
    second = get(authenticated_client, "search?q=Shogun").json["data"]
    assert second["revision"] != first["revision"]
    assert second["locale"] == "hu-HU"
    assert [kwargs["params"]["language"] for _, kwargs in upstream.calls] == ["en-US", "hu-HU"]
    assert authenticated_client.provider_searches == []


def test_real_authenticated_settings_get_omits_token_and_ciphertext(authenticated_client, monkeypatch):
    from api.system import settings as endpoint
    monkeypatch.setattr(endpoint, "database", SimpleNamespace(execute=lambda statement: SimpleNamespace(all=lambda: [])))
    result = authenticated_client.get("/api/system/settings", headers={"X-API-KEY": "metadata-test-key"})
    assert result.status_code == 200
    assert "tmdb_access_token" not in result.json["discover"]
    assert result.json["discover"]["tmdb_configured"] is True
    assert "synthetic-metadata-token" not in result.get_data(as_text=True)


def test_repeated_token_form_is_rejected_before_language_mutation(authenticated_client, monkeypatch):
    from api.system import settings as endpoint
    from werkzeug.datastructures import MultiDict
    calls = []
    monkeypatch.setattr(endpoint, "database", SimpleNamespace(execute=lambda statement: calls.append(statement)))
    response = authenticated_client.post("/api/system/settings", data=MultiDict([
        ("settings-discover-tmdb_access_token", "one"), ("settings-discover-tmdb_access_token", "two"),
        ("languages-enabled", "en")]), headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 406
    assert calls == []




@pytest.mark.parametrize("copies", [0, 2])
def test_browsing_never_reads_or_mutates_local_libraries(authenticated_client, retrieval_database, copies):
    import sqlalchemy as sa
    from app import database as db
    engine, session = retrieval_database
    for copy in range(1, copies + 1):
        session.add(db.TableMovies(id=copy, arr_instance_id=copy, radarrId=7, tmdbId="42", imdbId="tt0080274",
                                   path=f"/synthetic/library-{copy}/film.mkv", title="Wrong local title"))
    session.commit()
    tables = db.Base.metadata.sorted_tables
    before = {table.name: session.execute(sa.select(table)).all() for table in tables}
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        first = get(authenticated_client, "search?q=Shogun")
        detail = get(authenticated_client, "movies/42")
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)
    assert first.status_code == detail.status_code == 200
    assert detail.json["data"]["item"]["title"] == "Shōgun"
    assert statements == []
    assert {table.name: session.execute(sa.select(table)).all() for table in tables} == before
    assert authenticated_client.provider_searches == []


def test_tmdb_rate_limit_is_honored_without_automatic_retry(authenticated_client, upstream):
    upstream.status = 429
    for _ in range(2):
        assert get(authenticated_client, "search?q=Shogun").json["data"]["status"] == "unavailable"
    assert len(upstream.calls) == 1


def test_image_urls_use_validated_source_configuration(authenticated_client, upstream):
    def payload(url):
        if url.endswith("/configuration"):
            return {"images": {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w342"], "backdrop_sizes": ["w780"]}}
        return {"id": 42, "title": "Shōgun", "poster_path": "/abc123.jpg", "backdrop_path": "https://untrusted.example/secret.jpg"}
    upstream.payload = payload
    movie = get(authenticated_client, "movies/42").json["data"]["item"]
    assert movie["poster_url"] == "https://image.tmdb.org/t/p/w342/abc123.jpg"
    assert movie["backdrop_url"] is None
    assert all("synthetic-metadata-token" not in url for url, _ in upstream.calls)
    assert len(upstream.calls) == 2


def test_search_and_image_caches_remain_bounded(authenticated_client, upstream):
    from discover import metadata
    for index in range(260):
        assert get(authenticated_client, f"search?q=movie{index}").status_code == 200
    assert len(upstream.calls) == 260
    assert get(authenticated_client, "search?q=movie0").json["data"]["status"] == "available"
    assert len(upstream.calls) == 261
    assert metadata._cache.backend._cache.currsize <= 256


def test_concurrent_identical_queries_share_one_upstream_request(authenticated_client, upstream, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from discover import metadata
    original = metadata._request
    started = threading.Event()
    finish = threading.Event()
    def request(config, path, params=None):
        started.set()
        assert finish.wait(3)
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(metadata.search_movies, "Shogun")
        assert started.wait(3)
        second = pool.submit(metadata.search_movies, "Shōgun")
        finish.set()
        results = [first.result(), second.result()]
    assert {result["data"]["status"] for result in results} == {"available", "cached"}
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("payload", [{}, {"success": False}])
def test_malformed_connection_check_cannot_claim_available(authenticated_client, upstream, payload):
    upstream.payload = payload
    response = get(authenticated_client, "status")
    assert response.json["data"]["status"] == "unavailable"


@pytest.mark.parametrize("field,value", [("tmdb_access_token", "never-saved"), ("locale", "hu-HU")])
def test_real_system_endpoint_reports_metadata_io_failure(authenticated_client, metadata_save_environment,
                                                        monkeypatch, field, value):
    env = metadata_save_environment
    before = env.config.get_settings()["discover"]
    def fail(*args, **kwargs):
        raise OSError("synthetic-private-error")
    monkeypatch.setattr(env.config, "write", fail)
    response = authenticated_client.post("/api/system/settings", data={f"settings-discover-{field}": value},
                                         headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 503
    assert "synthetic-private-error" not in response.get_data(as_text=True)
    assert env.config.get_settings()["discover"] == before
    assert env.executed == []


def test_real_settings_followup_failure_preserves_saved_metadata_and_retires_old_cache(
    authenticated_client, metadata_save_environment, upstream, monkeypatch,
):
    import sys
    env = metadata_save_environment
    old = get(authenticated_client, "search?q=Shogun").json["data"]
    assert get(authenticated_client, "search?q=Shogun").json["data"]["status"] == "cached"
    def fail(statement):
        raise RuntimeError("synthetic-private-followup-error")
    monkeypatch.setattr(sys.modules["app.database"].database, "execute", fail)
    response = authenticated_client.post("/api/system/settings", data={
        "settings-discover-tmdb_access_token": "replacement-synthetic-token", "settings-discover-locale": "hu-HU",
    }, headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 503
    assert response.json == {"code": "discover_settings_refresh_failed",
                             "message": "Discover settings were saved, but application refresh failed. Reload settings before retrying."}
    status = get(authenticated_client, "status").json["data"]
    assert status["status"] == "available"
    assert status["revision"] != old["revision"]
    assert status["locale"] == "hu-HU"
    refreshed = get(authenticated_client, "search?q=Shogun").json["data"]
    assert refreshed["status"] == "available"
    assert refreshed["revision"] == status["revision"] == env.config.get_settings()["discover"]["metadata_revision"]
    assert upstream.calls[-1][1]["headers"]["Authorization"] == "Bearer replacement-synthetic-token"


@pytest.mark.parametrize("field", ["title", "overview", "poster_path", "backdrop_path"])
@pytest.mark.parametrize("path", ["search?q=Shogun", "movies/42"])
@pytest.mark.parametrize("primed", [False, True])
def test_escaped_credential_echo_never_reaches_metadata_or_cache(authenticated_client, upstream, monkeypatch, field, path, primed):
    from discover import metadata
    token = "synthetic-metadata-token"
    now = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: now[0])
    original = get(authenticated_client, path).json["data"] if primed else None
    now[0] += 301
    raw = {"id": 42, "title": "Safe film", "imdb_id": "tt0080274", field: f"/{token}.jpg" if field.endswith("_path") else token}
    upstream.payload = lambda url: {"results": [raw]} if url.endswith("/search/movie") else raw
    escaped = "".join(f"\\u{ord(char):04x}" for char in token)
    upstream.encode = lambda payload: json.dumps(payload).replace(token, escaped).encode()
    assert token.encode() not in upstream.encode(raw)
    response = get(authenticated_client, path)
    assert response.status_code == 200
    assert token not in response.get_data(as_text=True)
    data = response.json["data"]
    assert data["status"] == ("cached" if primed else "unavailable")
    if primed:
        assert data["fetched_at"] == original["fetched_at"]
        assert data["service_status"] == "unavailable"
    upstream.payload = None
    upstream.encode = lambda payload: json.dumps(payload).encode()
    assert get(authenticated_client, path).json["data"]["status"] == "available"


@pytest.mark.parametrize("kind", ["poster", "backdrop"])
@pytest.mark.parametrize("sizes", [None, 7, "w342", {"w342": True}, ["w342", None]])
def test_malformed_image_sizes_are_omitted_safely(authenticated_client, upstream, kind, sizes):
    def payload(url):
        if url.endswith("/configuration"):
            return {"images": {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w342"],
                               "backdrop_sizes": ["w780"], kind + "_sizes": sizes}}
        return {"id": 42, "title": "Safe film", "poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg"}
    upstream.payload = payload
    response = get(authenticated_client, "movies/42")
    assert response.status_code == 200
    data = response.json["data"]
    assert data["status"] == "available"
    assert data["item"]["title"] == "Safe film"
    assert data["item"]["poster_url"] is data["item"]["backdrop_url"] is None
    assert get(authenticated_client, "movies/42").json["data"]["status"] == "cached"


def test_malformed_image_refresh_keeps_metadata_and_its_stale_fallback_safe(authenticated_client, upstream, monkeypatch):
    from discover import metadata
    now = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: now[0])
    images = {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w342"], "backdrop_sizes": ["w780"]}
    upstream.payload = lambda url: {"images": images} if url.endswith("/configuration") else {
        "id": 42, "title": "Safe film", "poster_path": "/poster.jpg"}
    first = get(authenticated_client, "movies/42").json["data"]
    assert first["item"]["poster_url"]
    metadata._cache.delete((metadata.SOURCE, first["revision"], "images"))
    images["poster_sizes"] = None
    now[0] += 301
    refresh = get(authenticated_client, "movies/42")
    assert refresh.status_code == 200
    fresh = refresh.json["data"]
    assert fresh["status"] == "available"
    assert fresh["item"]["poster_url"] is None
    upstream.status = 503
    now[0] += 301
    stale = get(authenticated_client, "movies/42").json["data"]
    assert stale["status"] == "cached"
    assert stale["service_status"] == "unavailable"
    assert stale["item"] == fresh["item"]
    assert stale["fetched_at"] == fresh["fetched_at"]

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
    settings.set("discover", {"tmdb_access_token": "5ecafe00cafe00cafe00cafe00cafe00", "locale": "en-US"})
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
    assert "5ecafe00cafe00cafe00cafe00cafe00" not in first.get_data(as_text=True)


def test_metadata_requires_bazarr_authentication(authenticated_client):
    assert authenticated_client.get("/api/discover/metadata/status").status_code == 401
    assert authenticated_client.get("/api/discover/metadata/status", headers={"X-API-KEY": "wrong"}).status_code == 401


def test_stored_key_flag_is_false_while_metadata_stays_configured(authenticated_client, monkeypatch):
    """A reader with no key of their own still browses, and is told nothing is saved.

    These are two different facts. Before the built-in key they moved together,
    so the client could read one off the other; now only this flag answers
    "is there something here to remove?".
    """
    from app.config import get_settings, settings
    for stored in ("", "   ", "enc:v1:opaque"):
        settings.set("discover", {"tmdb_access_token": stored, "locale": "en-US"})
        result = get_settings()["discover"]
        assert result["tmdb_configured"] is True
        assert result["tmdb_token_stored"] is False
    settings.set("discover", {"tmdb_access_token": "5ecafe00cafe00cafe00cafe00cafe00", "locale": "en-US"})
    assert get_settings()["discover"]["tmdb_token_stored"] is True


def test_real_settings_serializer_omits_write_only_value(authenticated_client):
    from app.config import get_settings, settings
    result = get_settings()
    assert "tmdb_access_token" not in result["discover"]
    assert result["discover"]["tmdb_configured"] is True
    # The reader's own key is present here, and the flag says so without
    # carrying any part of it.
    assert result["discover"]["tmdb_token_stored"] is True
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
    response = authenticated_client.post("/api/discover/metadata/test",
                                         json={"token": "5ecafe44cafe44cafe44cafe44cafe44"},
                                         headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 200
    assert response.json["data"]["status"] == expected
    assert "5ecafe44cafe44cafe44cafe44cafe44" not in response.get_data(as_text=True)


def test_connection_check_does_not_save_candidate(authenticated_client):
    from app.config import settings
    before = settings.discover.tmdb_access_token
    response = authenticated_client.post("/api/discover/metadata/test",
                                         json={"token": "5ecafe55cafe55cafe55cafe55cafe55"},
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


@pytest.mark.parametrize("value", ["2026-02-31", "2026-13-01", "2026-00-10", "0000-00-00", "1980-1-1", "1980"])
def test_date_shaped_but_invalid_release_dates_stay_unknown(authenticated_client, upstream, value):
    """A shape match is not a date. An impossible calendar value must not
    project a confident year on either the movie or the show path."""
    upstream.payload = {"id": 42, "title": "Unknown film", "release_date": value}
    assert get(authenticated_client, "movies/42").json["data"]["item"]["year"] is None
    upstream.payload = {"id": 42, "name": "Unknown show", "first_air_date": value, "seasons": []}
    assert get(authenticated_client, "shows/42").json["data"]["item"]["year"] is None


@pytest.mark.parametrize("value,expected", [("2024-02-29", 2024), ("1980-01-01", 1980)])
def test_real_calendar_dates_still_project_their_year(authenticated_client, upstream, value, expected):
    upstream.payload = {"id": 42, "title": "Known film", "release_date": value}
    assert get(authenticated_client, "movies/42").json["data"]["item"]["year"] == expected
    upstream.payload = {"id": 42, "name": "Known show", "first_air_date": value, "seasons": []}
    assert get(authenticated_client, "shows/42").json["data"]["item"]["year"] == expected


@pytest.mark.parametrize("failure", [requests.Timeout("synthetic-sensitive-exception"), requests.ConnectionError("synthetic-sensitive-exception")])
def test_timeouts_and_transport_errors_do_not_leak(authenticated_client, upstream, failure):
    upstream.error = failure
    response = get(authenticated_client, "search?q=Shogun")
    assert response.status_code == 200
    assert response.json["data"]["status"] == "unavailable"
    assert "synthetic-sensitive-exception" not in response.get_data(as_text=True)


def test_missing_key_never_calls_upstream(authenticated_client, upstream, monkeypatch):
    """With no key anywhere, not even the built-in one, nothing is requested.

    A build with no credential at all is the only way this state is reachable
    now that the application ships its own key, so it is asserted against that
    condition rather than against an empty setting, which means "use the
    built-in one".
    """
    from app import tmdb
    from app.config import settings
    monkeypatch.setattr(tmdb, "builtin_api_key", lambda: "")
    settings.discover.tmdb_access_token = ""
    for path in ["status", "search?q=Shogun", "movies/42"]:
        result = get(authenticated_client, path)
        assert result.status_code == 200
        assert result.json["data"]["status"] == "unconfigured"
    assert upstream.calls == []


def test_no_stored_token_still_reaches_tmdb_with_the_built_in_key(authenticated_client, upstream):
    """The point of the change: Discover works with zero reader configuration."""
    from app import tmdb
    from app.config import settings
    settings.discover.tmdb_access_token = ""
    result = get(authenticated_client, "search?q=Shogun")
    assert result.status_code == 200
    assert result.json["data"]["status"] == "available"
    assert result.json["data"]["configured"] is True
    assert upstream.calls, "no request was made with the built-in key"
    # v3 authentication, and the key the application ships.
    assert upstream.calls[0][1]["params"]["api_key"] == tmdb.builtin_api_key()
    assert "Authorization" not in upstream.calls[0][1]["headers"]


@pytest.mark.parametrize("stored", ["eyJhbGciOiJIUzI1NiJ9.payload.signature", "not-a-key",
                                    "5ecafe00cafe00cafe00cafe00cafe0", "  "])
def test_a_stored_value_that_is_not_a_v3_key_falls_back_rather_than_failing(authenticated_client,
                                                                            upstream, stored):
    """A token stored during the v4 era must not break startup or a request."""
    from app import tmdb
    from app.config import settings
    settings.discover.tmdb_access_token = stored
    result = get(authenticated_client, "search?q=Shogun")
    assert result.status_code == 200
    assert result.json["data"]["status"] == "available"
    assert upstream.calls[0][1]["params"]["api_key"] == tmdb.builtin_api_key()
    if stored.strip():
        assert stored.strip() not in result.get_data(as_text=True)


def test_a_stored_v3_key_overrides_the_built_in_one(authenticated_client, upstream):
    from app import tmdb
    from app.config import settings
    settings.discover.tmdb_access_token = "5ecafe66cafe66cafe66cafe66cafe66"
    result = get(authenticated_client, "search?q=Shogun")
    assert result.status_code == 200
    assert upstream.calls[0][1]["params"]["api_key"] == "5ecafe66cafe66cafe66cafe66cafe66"
    assert upstream.calls[0][1]["params"]["api_key"] != tmdb.builtin_api_key()
    assert "5ecafe66cafe66cafe66cafe66cafe66" not in result.get_data(as_text=True)


def test_the_built_in_key_never_leaves_the_server(authenticated_client, upstream):
    """It travels as an outbound query parameter and nowhere else.

    Not in the settings payload, not in a DTO, not in the revision, not in a
    cache key. The revision must stay a random identifier: deriving it from the
    credential would put the key in every envelope the reader can read.
    """
    from app import tmdb
    from app.config import get_settings, settings
    from discover import metadata
    # The ordinary installation: nothing stored, so the built-in key is the one
    # in use and therefore the one that could leak.
    settings.discover.tmdb_access_token = ""
    key = tmdb.builtin_api_key()
    settings_payload = get_settings()
    assert "tmdb_access_token" not in settings_payload["discover"]
    assert key not in json.dumps(settings_payload)

    config = metadata.configuration()
    assert key not in config.revision
    assert key not in repr(config), repr(config)
    for path in ["status", "search?q=Shogun", "movies/42"]:
        body = get(authenticated_client, path).get_data(as_text=True)
        assert key not in body, path
    # It did reach TMDB, as a v3 query parameter.
    assert any(call[1]["params"].get("api_key") == key for call in upstream.calls)
    assert all("Authorization" not in call[1]["headers"] for call in upstream.calls)
    # And it is not the cache key either.
    from discover.metadata import _cache
    assert key not in repr(getattr(_cache, "backend", _cache).__dict__)


def test_the_built_in_default_does_not_make_a_process_see_a_configuration_change(authenticated_client):
    """A default must not look like a change on every call, or every read would
    hard-invalidate the metadata cache."""
    from app.config import settings
    from discover import metadata
    settings.discover.tmdb_access_token = ""
    first = metadata.configuration().revision
    for _ in range(5):
        assert metadata.configuration().revision == first
    # An ignored stored value is not a change either: the effective credential
    # is the same built-in key before and after.
    settings.discover.tmdb_access_token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    assert metadata.configuration().revision == first
    # A usable key is a real change, and rotates it.
    settings.discover.tmdb_access_token = "5ecafe99cafe99cafe99cafe99cafe99"
    assert metadata.configuration().revision != first


def test_a_draft_that_is_not_a_v3_key_is_refused_without_a_request(authenticated_client, upstream):
    """Checking a v4 era token must not quietly report the built-in key's health."""
    response = authenticated_client.post("/api/discover/metadata/test",
                                         json={"token": "eyJhbGciOiJIUzI1NiJ9.payload.signature"},
                                         headers={"X-API-KEY": "metadata-test-key"})
    assert response.status_code == 200
    assert response.json["data"]["status"] == "authentication_failed"
    assert "not a TMDB v3 API key" in response.json["data"]["message"]
    assert upstream.calls == []


@pytest.mark.parametrize("payload", [{}, {"results": "bad"}, {"results": [{"id": True, "title": "Invalid"}]},
                                     {"results": [{"id": 42, "title": "5ecafe00cafe00cafe00cafe00cafe00"}]},
                                     {"results": [{"id": 42, "title": "x" * (1024 * 1024)}]}])
def test_invalid_or_oversized_responses_fail_closed(authenticated_client, upstream, payload):
    upstream.payload = payload
    response = get(authenticated_client, "search?q=Shogun")
    assert response.json["data"]["status"] == "unavailable"
    assert response.json["data"]["items"] == []
    assert "5ecafe00cafe00cafe00cafe00cafe00" not in response.get_data(as_text=True)


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
    settings.discover.tmdb_access_token = "5ecafe11cafe11cafe11cafe11cafe11"
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
        settings.discover.tmdb_access_token = "5ecafe22cafe22cafe22cafe22cafe22"
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
    assert result.json["discover"]["tmdb_token_stored"] is True
    assert "5ecafe00cafe00cafe00cafe00cafe00" not in result.get_data(as_text=True)


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
    assert all("5ecafe00cafe00cafe00cafe00cafe00" not in url for url, _ in upstream.calls)
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
        "settings-discover-tmdb_access_token": "5ecafe77cafe77cafe77cafe77cafe77", "settings-discover-locale": "hu-HU",
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
    # v3 authentication: the replacement key travels as a query parameter and
    # no authorization header is sent at all.
    assert upstream.calls[-1][1]["params"]["api_key"] == "5ecafe77cafe77cafe77cafe77cafe77"
    assert "Authorization" not in upstream.calls[-1][1]["headers"]


@pytest.mark.parametrize("field", ["title", "overview", "poster_path", "backdrop_path"])
@pytest.mark.parametrize("path", ["search?q=Shogun", "movies/42"])
@pytest.mark.parametrize("primed", [False, True])
def test_escaped_credential_echo_never_reaches_metadata_or_cache(authenticated_client, upstream, monkeypatch, field, path, primed):
    from discover import metadata
    token = "5ecafe00cafe00cafe00cafe00cafe00"
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

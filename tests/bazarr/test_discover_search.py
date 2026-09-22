"""Discover uses the authenticated UI API and real pool with fixture upstreams."""
from types import SimpleNamespace
import os
import uuid

import pytest
from flask import Flask

from compat.service import _get_compat_pool as real_compat_pool


@pytest.fixture
def authenticated_client(monkeypatch):
    import requests

    def upstream_fixture(session, method, url, **kwargs):
        if method.lower() == "get" and url.endswith("announcements.json"):
            response = requests.Response()
            response.status_code = 200
            response._content = b"[]"
            return response
        raise AssertionError("Unexpected network access during deterministic API tests")

    monkeypatch.setattr(requests.Session, "request", upstream_fixture)
    monkeypatch.setattr("app.check_update._fetch_repo_releases", lambda *args, **kwargs: [])
    from api import api_bp
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", "discover-test-key")
    for key, value in {"enabled": False, "token": "", "jwt_secret": "", "file_id_secret": ""}.items():
        monkeypatch.setattr(settings.compat_endpoint, key, value)
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


@pytest.fixture
def providers(monkeypatch):
    from app import get_providers
    from app.config import settings
    from compat import service, cache
    from provider_hub import registry
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.core import SZAsyncProviderPool
    from subliminal_patch.provider_health import get_tracker
    from subzero.language import Language

    names = []
    videos = []
    installations = []
    original = {}

    def add(name, result=None, error=None, languages=None, video_types=None, trusted=True, delay=None):
        def listing(self, video, requested):
            videos.append((name, video, requested))
            if delay:
                # A provider that keeps the fanout running after a faster one
                # has already answered. Some behaviour only exists while the
                # search is still in flight and cannot be observed otherwise.
                import time as _time
                _time.sleep(delay)
            if error:
                raise error
            return result or []
        attrs = {"provider_name": name, "languages": languages or {Language("eng")},
                 "list_subtitles": listing}
        if video_types is not None:
            attrs["video_types"] = video_types
        cls = type(f"{name}FixtureProvider", (registry.HubProxyProvider,), attrs)
        if name in provider_registry.names():
            original[name] = provider_registry[name]
        provider_registry.register(name, cls)
        names.append(name)
        installations.append(SimpleNamespace(provider_id=name, trusted=trusted))
        return name

    monkeypatch.setattr(settings.general, "enabled_providers", names)
    monkeypatch.setattr(get_providers, "get_providers_sorted", lambda: list(names))
    monkeypatch.setattr(registry, "active_installations", lambda: installations)
    monkeypatch.setattr(get_providers, "tp", {})
    pool = SZAsyncProviderPool(providers=[], provider_configs={})
    pool.providers = names
    monkeypatch.setattr(service, "_get_compat_pool", lambda **kwargs: pool)
    monkeypatch.setattr(get_tracker(), "currently_discarded", lambda: set())
    cache.invalidate_all()
    yield SimpleNamespace(add=add, videos=videos, pool=pool, names=names)
    cache.invalidate_all()
    for name in names:
        if name in original:
            provider_registry.register(name, original[name])
        elif name in provider_registry.names():
            del provider_registry[name]


def post(client, payload):
    return client.post("/api/discover/search", json=payload, headers={"X-API-KEY": "discover-test-key"})


def test_auth_required_even_when_hub_disabled(authenticated_client):
    response = authenticated_client.post("/api/discover/search", json={})
    assert response.status_code == 401


@pytest.mark.parametrize("payload", [
    {}, {"media_type": "movie", "imdb_id": "The Matrix", "language": "eng"},
    {"media_type": "movie", "imdb_id": "tt0133093", "language": "not-a-language"},
    {"media_type": "movie", "imdb_id": "tt0133093", "language": "zzz"},
    {"media_type": "episode", "imdb_id": "tt0133093", "language": "eng", "season": 1},
    {"media_type": "episode", "imdb_id": "tt0133093", "language": "eng", "season": True, "episode": 1},
    {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng", "moviehash": "abc"},
])
def test_invalid_context_rejected_before_provider_fanout(authenticated_client, providers, payload):
    providers.add("discover_good")
    response = post(authenticated_client, payload)
    assert response.status_code == 400
    assert providers.videos == []


def test_real_pool_nonlibrary_search_without_hub_or_media_access(authenticated_client, providers, monkeypatch):
    from compat import service, meter
    from provider_hub.protocol import candidate_from_worker

    media_file_accesses = []
    hub_usage_writes = []
    def media(*args, **kwargs):
        media_file_accesses.append(args)
        raise AssertionError("media access")
    monkeypatch.setattr(service, "_lookup_library_metadata", media)
    monkeypatch.setattr(service, "search_local", media)
    monkeypatch.setattr(service, "_parse_video_from_library", media)
    monkeypatch.setattr(meter, "record", lambda *a, **kw: hub_usage_writes.append(a), raising=False)
    candidate = candidate_from_worker("discover_good", {
        "id": "candidate-1", "language": {"alpha3": "eng"},
        "release_info": "The.Matrix.1999.1080p", "matches": ["imdb_id"],
        "provider_payload": {"private": "never serialize"},
    })
    providers.add("discover_good", [candidate])
    payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}
    response = post(authenticated_client, payload)
    assert response.status_code == 200
    assert response.json["context"]["imdb_id"] == "tt0133093"
    assert response.json["context"]["matching_mode"] == "title"
    assert len(response.json["results"]) == 1
    assert response.json["coverage"]["providers"][0]["status"] == "success"
    assert response.json["results"][0]["provider"] == "discover_good"
    assert "never serialize" not in response.get_data(as_text=True)
    assert media_file_accesses == []
    assert hub_usage_writes == []


def test_failed_provider_does_not_hide_working_rows(authenticated_client, providers):
    from subliminal.exceptions import AuthenticationError
    from provider_hub.protocol import candidate_from_worker
    providers.add("discover_good", [candidate_from_worker("discover_good", {
        "id": "1", "language": {"alpha3": "eng"}, "release_info": "The Matrix", "provider_payload": {}})])
    providers.add("discover_auth", error=AuthenticationError("private credential detail"))
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"})
    assert response.status_code == 200
    assert response.json["status"] == "partial"
    assert len(response.json["results"]) == 1
    assert {p["provider"]: p["status"] for p in response.json["coverage"]["providers"]} == {
        "discover_good": "success", "discover_auth": "authentication_required"}
    assert "private credential detail" not in response.get_data(as_text=True)


def test_successful_empty_is_complete_and_cached_with_original_checked_time(authenticated_client, providers):
    providers.add("discover_empty")
    payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}
    first = post(authenticated_client, payload).json
    second = post(authenticated_client, payload).json
    assert first["status"] == "complete"
    assert first["results"] == []
    assert first["coverage"]["providers"][0]["status"] == "empty"
    assert second["checked_at"] == first["checked_at"]
    assert second["search_id"] == first["search_id"]
    assert second["cache_status"] == "cached"
    assert len(providers.videos) == 1


def test_no_configured_providers_is_failed_setup_not_successful_empty(authenticated_client, providers):
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert response["status"] == "failed"
    assert response["coverage"] == {"providers": [], "complete": False, "configured_count": 0, "completed_count": 0}
    assert providers.videos == []


def test_persisted_auth_cooldown_retains_known_cause(authenticated_client, providers, monkeypatch):
    import datetime as dt
    from app import get_providers

    providers.add("discover_auth")
    monkeypatch.setattr(get_providers, "get_providers_sorted", lambda: [])
    get_providers.tp["discover_auth"] = ("AuthenticationError", dt.datetime.now() + dt.timedelta(hours=1), "authentication")
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert response["coverage"]["providers"][0]["status"] == "authentication_required"
    assert response["coverage"]["providers"][0]["retry_at"] is not None
    assert providers.videos == []


@pytest.mark.parametrize("language,alpha3,country", [("pob", "por", "BR"), ("zht", "zho", "TW"), ("hun", "hun", None)])
def test_explicit_language_variants_reach_real_pool(authenticated_client, providers, language, alpha3, country):
    from subzero.language import Language
    providers.add("discover_variant", languages={Language(alpha3, country)})
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": language})
    assert response.status_code == 200
    requested = next(iter(providers.videos[0][2]))
    assert requested.alpha3 == alpha3
    assert (str(requested.country) if requested.country else None) == country


def test_exact_episode_and_language_do_not_cross_cache(authenticated_client, providers):
    from subzero.language import Language
    providers.add("discover_episode", languages={Language("eng"), Language("hun")})
    for episode, language in [(3, "eng"), (4, "eng"), (4, "hun")]:
        payload = {"media_type": "episode", "imdb_id": "tt0903747", "season": 0, "episode": episode, "language": language, "manual_confirmed": True}
        response = post(authenticated_client, payload)
        assert response.status_code == 200
        assert response.json["context"]["episode"] == episode
    assert [(v.season, v.episode, v.series_imdb_id, next(iter(languages)).alpha3)
            for _, v, languages in providers.videos] == [
        (0, 3, "tt0903747", "eng"), (0, 4, "tt0903747", "eng"), (0, 4, "tt0903747", "hun")]


def test_only_actual_trusted_catalog_proxies_can_search(authenticated_client, providers, monkeypatch):
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.providers import Provider
    providers.add("discover_good")
    providers.add("discover_untrusted", trusted=False)
    providers.add("discover_legacy")
    monkeypatch.setitem(provider_registry.providers, "discover_legacy", Provider)
    providers.names.append("discover_missing")
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert [name for name, _, _ in providers.videos] == ["discover_good"]
    assert {p["provider"]: p["reason"] for p in response["coverage"]["providers"]} == {
        "discover_good": None, "discover_untrusted": "not_catalog_provider",
        "discover_legacy": "not_catalog_provider", "discover_missing": "provider_unavailable"}


def test_failed_refresh_retains_rows_checked_time_and_honors_cooldown(authenticated_client, providers, monkeypatch):
    from provider_hub.protocol import candidate_from_worker
    from subliminal_patch.exceptions import APIThrottled
    providers.add("discover_good", [candidate_from_worker("discover_good", {
        "id": "1", "language": {"alpha3": "eng"}, "release_info": "Original release", "provider_payload": {}})])
    payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}
    first = post(authenticated_client, payload).json
    attempts = []

    def failed(*args):
        attempts.append(1)
        raise APIThrottled(retry_after=600)

    monkeypatch.setattr(providers.pool["discover_good"], "list_subtitles", failed)
    second = post(authenticated_client, {**payload, "refresh": True}).json
    third = post(authenticated_client, {**payload, "refresh": True}).json
    assert second["results"][0]["id"] == first["results"][0]["id"]
    assert second["results"][0]["stale"] is True
    assert second["checked_at"] == first["checked_at"]
    assert third["coverage"]["providers"][0]["status"] == "cooldown"
    assert third["cache_status"] == "stale"
    assert attempts == [1]


def test_handle_cannot_resolve_reused_integer_after_store_reset(authenticated_client, providers):
    from compat.file_id_store import reset_store, get_store
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker
    providers.add("discover_good", [candidate_from_worker("discover_good", {
        "id": "1", "language": {"alpha3": "eng"}, "provider_payload": {}})])
    reset_store()
    payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}
    first = post(authenticated_client, payload).json
    old_id = first["results"][0]["id"]
    assert resolve_result(old_id)["context"]["imdb_id"] == "tt0133093"
    assert resolve_result(old_id, search_id="other-search") is None
    reset_store()
    get_store().put({"subtitle": "different"}, 300)
    assert resolve_result(old_id) is None
    second = post(authenticated_client, payload).json
    assert second["results"][0]["id"] != old_id
    assert resolve_result(second["results"][0]["id"]) is not None


@pytest.mark.parametrize("scripts", [("sr-Latn", "sr-Cyrl"), ("sr-Cyrl", "sr-Latn")])
def test_script_variants_have_separate_populated_cache_and_handles(authenticated_client, providers, monkeypatch, scripts):
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    name = providers.add("discover_script", languages={Language.fromietf(code) for code in scripts})
    calls = []

    def listing(self, video, languages):
        language = next(iter(languages))
        calls.append(str(language))
        candidate = candidate_from_worker(name, {
            "id": str(language), "language": {"alpha3": "srp"}, "provider_payload": {},
        })
        candidate.language = language
        return [candidate]

    monkeypatch.setattr(provider_registry[name], "list_subtitles", listing)
    ids = []
    for language in scripts:
        payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": language}
        result = post(authenticated_client, payload).json
        assert result["context"]["language"] == language
        assert result["results"][0]["language"] == language
        handle = result["results"][0]["id"]
        stored = resolve_result(handle, search_id=result["search_id"])
        assert stored["context"]["language"] == language
        assert str(stored["subtitle"].language) == language
        ids.append(handle)
        cached = post(authenticated_client, payload).json
        assert cached["cache_status"] == "cached"
        assert cached["results"][0]["id"] == handle
    assert calls == list(scripts)
    assert len(set(ids)) == 2


def test_cold_pool_recovers_available_provider_without_retrying_cooldown(authenticated_client, providers, monkeypatch):
    import datetime as dt
    from app import get_providers
    from compat import service

    cold = providers.add("discover_cold")
    healthy = providers.add("discover_healthy")
    available = [healthy]
    configs = {cold: {"fixture_option": "before", "removed_option": True},
               healthy: {"fixture_option": "healthy"}}
    monkeypatch.setattr(service, "_get_compat_pool", real_compat_pool)
    monkeypatch.setattr(service, "_compat_pool", None)
    monkeypatch.setattr(service, "get_providers_sorted", lambda: list(available))
    monkeypatch.setattr(service, "get_providers_auth", lambda: configs)
    monkeypatch.setattr(service, "get_provider_language_hook", lambda: None)
    monkeypatch.setattr(get_providers, "get_providers_sorted", lambda: list(available))
    get_providers.tp[cold] = ("APIThrottled", dt.datetime.now() + dt.timedelta(minutes=1), "cooldown")
    payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}
    first = post(authenticated_client, payload).json
    pool = real_compat_pool()
    healthy_instance = pool.initialized_providers[healthy]
    # An unrelated discard is intentional and must not be reset by recovery.
    pool.discarded_providers.add("unrelated_discarded")
    first_state = pool._discover_state
    assert {row["provider"]: row["status"] for row in first["coverage"]["providers"]} == {
        cold: "cooldown", healthy: "empty"}
    post(authenticated_client, {**payload, "refresh": True})
    assert [name for name, _, _ in providers.videos] == [healthy, healthy]
    assert cold not in pool.providers
    # Availability reader has removed the elapsed throttle before acquisition.
    configs[cold] = {"fixture_option": "current"}
    available.append(cold)
    get_providers.tp.clear()
    restored = post(authenticated_client, {**payload, "refresh": True}).json
    assert {row["provider"]: row["status"] for row in restored["coverage"]["providers"]} == {
        cold: "empty", healthy: "empty"}
    assert [name for name, _, _ in providers.videos].count(cold) == 1
    assert real_compat_pool() is pool
    assert pool._discover_state is first_state
    assert pool.initialized_providers[healthy] is healthy_instance
    assert pool.discarded_providers == {"unrelated_discarded"}
    assert pool.provider_configs[cold] == configs[cold]


@pytest.mark.parametrize("remote_class,expected", [
    ("ConnectionError", "unreachable"), ("ReadTimeout", "timeout"),
    ("ConnectTimeout", "timeout"), ("Timeout", "timeout"), ("UnexpectedProviderError", "error"),
])
def test_worker_envelope_outcomes_through_catalog_proxy(authenticated_client, providers, monkeypatch,
                                                       remote_class, expected):
    from provider_hub.registry import HubProxyProvider
    from provider_hub.worker import _raise_worker_error
    from subliminal_patch.extensions import provider_registry

    name = providers.add("discover_worker_failure")
    monkeypatch.setattr(provider_registry[name], "list_subtitles", HubProxyProvider.list_subtitles)

    def fail(*args, **kwargs):
        _raise_worker_error({"code": "provider", "class_name": remote_class,
                             "message": "ConnectionError ReadTimeout must not be guessed from text"})

    providers.pool[name].worker_client = SimpleNamespace(request=fail)
    result = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert result["coverage"]["providers"][0]["status"] == expected
    assert result["coverage"]["providers"][0]["reason"] == expected


@pytest.mark.parametrize("invalidate", ["reset", "evict"])
def test_failed_refresh_omits_invalid_handles_but_retains_usable_rows(authenticated_client, providers, monkeypatch,
                                                                  invalidate):
    from compat import file_id_store
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker
    from subliminal.exceptions import AuthenticationError

    store = file_id_store.FileIdStore(max_entries=2)
    monkeypatch.setattr(file_id_store, "_singleton", store)
    name = providers.add("discover_invalidated", [candidate_from_worker("discover_invalidated", {
        "id": str(index), "language": {"alpha3": "eng"}, "provider_payload": {},
    }) for index in range(2)])
    payload = {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}
    first = post(authenticated_client, payload).json
    if invalidate == "reset":
        file_id_store.reset_store()
        retained = []
    else:
        store.put({"unrelated": True}, 300)
        retained = [first["results"][1]]
    assert resolve_result(first["results"][0]["id"]) is None

    def fail(*args):
        raise AuthenticationError("fixture failure")

    monkeypatch.setattr(providers.pool[name], "list_subtitles", fail)
    # Invalid handles force a real refresh even without an explicit refresh flag.
    second = post(authenticated_client, payload).json
    assert second["results"] == [{**row, "stale": True} for row in retained]
    assert all(resolve_result(row["id"]) is not None for row in second["results"])
    assert second["status"] == ("partial" if retained else "failed")
    assert second["coverage"]["completed_count"] == 0
    assert second["coverage"]["providers"][0]["result_count"] == 0
    if retained:
        assert second["checked_at"] == first["checked_at"]


def test_unknown_flags_stay_unknown_and_forced_and_hi_are_independent(authenticated_client, providers):
    from provider_hub.protocol import candidate_from_worker
    providers.add("discover_good", [candidate_from_worker("discover_good", {
        "id": "unknown", "language": {"alpha3": "eng"}, "provider_payload": {}}),
        candidate_from_worker("discover_good", {
            "id": "both", "language": {"alpha3": "eng", "forced": True, "hi": True}, "provider_payload": {}})])
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    unknown, both = response["results"]
    assert unknown["scope"] == "unknown"
    assert unknown["hearing_impaired"] is None
    assert unknown["rating"] is None
    assert both["scope"] == "forced"
    assert both["hearing_impaired"] is True


@pytest.mark.parametrize("forced,hi", [(None, None), (False, False), (True, False), (False, True), (True, True)])
def test_candidate_explicit_modifier_evidence_preserves_existing_flags(forced, hi):
    from provider_hub.protocol import candidate_from_worker
    payload = {"id": "flags", "language": {"alpha3": "eng"}, "provider_payload": {}}
    if forced is not None:
        payload["language"]["forced"] = forced
    if hi is not None:
        payload["hearing_impaired"] = hi
    subtitle = candidate_from_worker("discover_good", payload)
    assert getattr(subtitle, "_reported_forced", "missing") is forced
    assert getattr(subtitle, "_reported_hearing_impaired", "missing") is hi
    assert bool(subtitle.language.forced) is bool(forced)
    assert bool(subtitle.hearing_impaired) is bool(hi)


@pytest.fixture(params=["sqlite", "postgresql"])
def retrieval_database(request, monkeypatch):
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from app import database as db

    cleanup = None
    schema = "discover_" + uuid.uuid4().hex
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL acceptance lane")
        cleanup = sa.create_engine(url)
        with cleanup.begin() as connection:
            connection.execute(sa.schema.CreateSchema(schema))
        engine = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = sa.create_engine("sqlite:///:memory:")
    try:
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            monkeypatch.setattr(db, "database", session)
            yield engine, session
    finally:
        engine.dispose()
        if cleanup is not None:
            with cleanup.begin() as connection:
                connection.execute(sa.schema.DropSchema(schema, cascade=True))
            cleanup.dispose()


@pytest.mark.parametrize("copies", [0, 2])
@pytest.mark.parametrize("media_type", ["movie", "episode"])
def test_title_search_never_reads_or_mutates_empty_or_duplicate_arr_libraries(
    authenticated_client, providers, retrieval_database, copies, media_type, monkeypatch,
):
    import sqlalchemy as sa
    from app import database as db
    from compat import service
    from provider_hub.protocol import candidate_from_worker

    engine, session = retrieval_database
    for copy_id in range(1, copies + 1):
        session.add(db.TableMovies(
            id=copy_id, arr_instance_id=copy_id, radarrId=7, tmdbId="11", imdbId="tt0133093",
            path=f"/private/library-{copy_id}/local.mkv", title="Wrong local title", sceneName="Local.2160p.Group"))
        session.add(db.TableShows(
            id=copy_id, arr_instance_id=copy_id, sonarrSeriesId=7, imdbId="tt0133093",
            path=f"/private/library-{copy_id}", title="Wrong local series"))
        session.flush()
        session.add(db.TableEpisodes(
            id=copy_id, series_id=copy_id, arr_instance_id=copy_id, sonarrSeriesId=7, sonarrEpisodeId=9,
            path=f"/private/library-{copy_id}/ep.mkv", season=0, episode=3, title="Wrong local episode"))
    session.commit()
    tables = db.Base.metadata.sorted_tables
    before = {table.name: session.execute(sa.select(table)).all() for table in tables}
    media_file_accesses = []

    def media(*args, **kwargs):
        media_file_accesses.append(args)
        raise AssertionError("Discover attempted media refinement")

    monkeypatch.setattr(service, "_parse_video_from_library", media)
    providers.add("discover_database", [candidate_from_worker("discover_database", {
        "id": "1", "language": {"alpha3": "eng"}, "provider_payload": {}})])
    sql = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record_sql)
    payload = {"media_type": media_type, "imdb_id": "tt0133093", "language": "eng", "title": "1917", "year": 2019}
    if media_type == "episode":
        payload.update(season=0, episode=3, manual_confirmed=True)
    try:
        response = post(authenticated_client, payload)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record_sql)
    after = {table.name: session.execute(sa.select(table)).all() for table in tables}
    assert response.status_code == 200
    assert len(response.json["results"]) == 1
    assert sql == []
    assert after == before
    assert media_file_accesses == []
    video = providers.videos[0][1]
    assert (video.title if media_type == "movie" else video.series) == "1917"
    assert video.name == "" and video.hashes == {} and video.release_group is None


@pytest.fixture
def resolved_episode(authenticated_client, monkeypatch):
    from copy import deepcopy
    from app.config import settings
    from discover import metadata
    from subliminal_patch.refiners import tvdb_v4
    previous = settings.get("discover", {}).copy()
    settings.set("discover", {"tmdb_access_token": "synthetic-episode-source", "locale": "en-US"})
    monkeypatch.setattr(metadata, "_current", None)
    raw = {"id": 401, "season_number": 2, "episode_number": 1, "name": "Home", "air_date": "2026-09-01"}
    target = {"id": 501, "seriesId": 300, "seasonNumber": 2, "number": 1, "lastUpdated": "2026-09-02"}
    data = {
        "/tv/100": {"id": 100, "name": "Northern Light", "first_air_date": "2020-03-01",
                    "seasons": [{"id": 201, "season_number": 2, "name": "Season 2", "episode_count": 2}]},
        "/tv/100/external_ids": {"id": 100, "imdb_id": "tt1234567", "tvdb_id": 300},
        "/tv/100/season/2/episode/1": raw,
        "/tv/100/season/2/episode/1/external_ids": {"id": 401, "imdb_id": "tt7654321", "tvdb_id": 501},
    }
    monkeypatch.setattr(metadata, "_request", lambda config, path, params=None: deepcopy(data[path]))
    monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lambda episode_id: deepcopy(target)))
    identity = metadata.episode_details("100", "2", "1")["data"]["episode"]
    context = {"media_type": "episode", "imdb_id": "tt1234567", "title": "Northern Light", "year": 2020,
               "language": "eng", "season": 2, "episode": 1, "show_id": 100, "episode_identity": identity}
    yield SimpleNamespace(context=context, target=target, data=data, raw=raw)
    settings.set("discover", previous)


def test_server_reconciles_episode_and_preserves_all_worker_supported_identity(authenticated_client, providers, resolved_episode):
    from provider_hub.protocol import video_to_payload
    providers.add("discover_episode")
    response = post(authenticated_client, resolved_episode.context)
    assert response.status_code == 200
    assert response.json["context"]["episode_identity"] == resolved_episode.context["episode_identity"]
    video = providers.videos[0][1]
    worker = video_to_payload(video)
    assert worker["series_imdb_id"] == "tt1234567" and worker["imdb_id"] == "tt7654321"
    assert worker["series_tvdb_id"] == 300 and worker["tvdb_id"] == 501 and worker["tmdb_id"] == 401
    assert worker["series"] == "Northern Light" and worker["title"] == "Home" and worker["year"] == 2020
    assert worker["absolute_episode"] is None


@pytest.mark.parametrize("field,value", [("id", 999), ("show_imdb_id", "tt9999999"),
    ("imdb_id", "tt9999999"), ("target_season", 1), ("numbering", "aired"),
    ("identity_status", "manual_confirmed"), ("air_date", "2025-01-01"), ("absolute_episode", 42)])
def test_forged_episode_identity_is_rejected_before_fanout(authenticated_client, providers, resolved_episode, field, value):
    from copy import deepcopy
    providers.add("discover_episode")
    payload = deepcopy(resolved_episode.context)
    payload["episode_identity"][field] = value
    assert post(authenticated_client, payload).status_code == 400
    assert providers.videos == []


@pytest.mark.parametrize("field,value", [("imdb_id", "tt7654321"), ("title", "Home"), ("year", 2026),
                                        ("show_id", 101), ("season", 1), ("episode", 2)])
def test_episode_imdb_name_year_and_target_cannot_replace_series_fields(authenticated_client, providers, resolved_episode, field, value):
    providers.add("discover_episode")
    assert post(authenticated_client, {**resolved_episode.context, field: value}).status_code == 400
    assert providers.videos == []


def test_missing_episode_confirmation_is_not_a_provider_search(authenticated_client, providers):
    providers.add("discover_episode")
    response = post(authenticated_client, {"media_type": "episode", "imdb_id": "tt1234567", "language": "eng", "season": 2, "episode": 1})
    assert response.status_code == 400 and providers.videos == []


def test_manual_recovery_preserves_source_but_clears_effective_episode_ids(authenticated_client, providers, resolved_episode):
    providers.add("discover_episode")
    response = post(authenticated_client, {**resolved_episode.context, "manual_confirmed": True, "season": 0, "episode": 3})
    assert response.status_code == 200
    assert response.json["context"]["episode_identity"] == resolved_episode.context["episode_identity"]
    assert response.json["context"]["manual_confirmed"] is True
    video = providers.videos[0][1]
    assert video.series_imdb_id == "tt1234567" and (video.season, video.episode) == (0, 3)
    assert video.imdb_id is None and video.tvdb_id is None and video.tmdb_id is None and video.absolute_episode is None


def test_changed_episode_source_cannot_reuse_cache_or_silently_adopt_new_identity(authenticated_client, providers, resolved_episode):
    from discover import metadata
    providers.add("discover_episode")
    first = post(authenticated_client, resolved_episode.context).json
    resolved_episode.raw["name"] = "Home, revised"
    metadata._cache.invalidate(hard=True)
    assert post(authenticated_client, resolved_episode.context).status_code == 400
    identity = metadata.episode_details("100", "2", "1")["data"]["episode"]
    second = post(authenticated_client, {**resolved_episode.context, "episode_identity": identity}).json
    assert second["search_id"] != first["search_id"] and len(providers.videos) == 2


def test_expired_mapping_during_source_outage_never_accepts_forged_resolved_status(authenticated_client, providers, resolved_episode, monkeypatch):
    from discover import metadata
    providers.add("discover_episode")
    metadata._cache.invalidate(hard=True)
    def unavailable(*args, **kwargs):
        raise metadata.UpstreamFailure()
    monkeypatch.setattr(metadata, "_request", unavailable)
    for manual in (False, True):
        response = post(authenticated_client, {**resolved_episode.context, "manual_confirmed": manual})
        assert response.status_code == 400
    assert providers.videos == []


def test_known_conflict_stays_blocked_even_with_manual_confirmation(authenticated_client, providers, resolved_episode):
    from discover import metadata
    providers.add("discover_episode")
    resolved_episode.target["seriesId"] = 999
    metadata._cache.invalidate(hard=True)
    identity = metadata.episode_details("100", "2", "1")["data"]["episode"]
    assert identity["identity_status"] == "conflict"
    for manual in (False, True):
        assert post(authenticated_client, {**resolved_episode.context, "episode_identity": identity, "manual_confirmed": manual}).status_code == 400
    assert providers.videos == []


@pytest.mark.parametrize("dependency", ["show_external_ids", "episode_external_ids", "tvdb_episode"])
@pytest.mark.parametrize("manual", [False, True])
def test_partial_mapping_outage_allows_only_bounded_prior_context(authenticated_client, providers, resolved_episode, monkeypatch, dependency, manual):
    from discover import metadata
    from subliminal_patch.refiners import tvdb_v4
    providers.add("discover_episode")
    context = {**resolved_episode.context, "manual_confirmed": manual}
    if manual:
        context.update(season=0, episode=3)
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 0)
    if dependency == "tvdb_episode":
        monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lambda value: None))
    else:
        original = metadata._request
        path = "/tv/100/external_ids" if dependency == "show_external_ids" else "/tv/100/season/2/episode/1/external_ids"
        def request(config, requested, params=None):
            if requested == path:
                raise metadata.UpstreamFailure()
            return original(config, requested, params)
        monkeypatch.setattr(metadata, "_request", request)
    accepted = post(authenticated_client, context)
    assert accepted.status_code == 200
    assert accepted.json["context"]["episode_identity"] == context["episode_identity"]
    assert len(providers.videos) == 1
    if manual:
        assert accepted.json["context"]["manual_confirmed"] is True
        assert providers.videos[0][1].tvdb_id is None
    monkeypatch.setattr(metadata, "STALE_SECONDS", 0)
    assert post(authenticated_client, context).status_code == 400
    assert len(providers.videos) == 1


@pytest.mark.parametrize("manual", [False, True])
def test_changed_parent_with_partial_child_outage_rejects_previous_context(authenticated_client, providers, resolved_episode, monkeypatch, manual):
    from discover import metadata
    providers.add("discover_episode")
    resolved_episode.data["/tv/100/external_ids"]["tvdb_id"] = 999
    monkeypatch.setattr(metadata, "FRESH_SECONDS", 0)
    original = metadata._request
    def request(config, path, params=None):
        if path == "/tv/100/season/2/episode/1/external_ids":
            raise metadata.UpstreamFailure()
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    assert post(authenticated_client, {**resolved_episode.context, "manual_confirmed": manual}).status_code == 400
    assert providers.videos == []


@pytest.mark.parametrize("dependency", ["episode_external_ids", "tvdb_episode"])
def test_fresh_source_with_unavailable_mapping_requires_new_manual_confirmation(authenticated_client, providers, resolved_episode, monkeypatch, dependency):
    from discover import metadata
    from subliminal_patch.refiners import tvdb_v4
    providers.add("discover_episode")
    metadata._cache.invalidate(hard=True)
    if dependency == "tvdb_episode":
        monkeypatch.setattr(tvdb_v4, "get_client", lambda: SimpleNamespace(get_episode=lambda value: None))
    else:
        original = metadata._request
        def request(config, path, params=None):
            if path.endswith("/episode/1/external_ids"):
                raise metadata.UpstreamFailure()
            return original(config, path, params)
        monkeypatch.setattr(metadata, "_request", request)
    source = metadata.episode_details("100", "2", "1")["data"]
    assert source["status"] == "unavailable"
    assert source["episode"]["identity_status"] == "unverified"
    context = {**resolved_episode.context, "episode_identity": source["episode"], "season": 0, "episode": 3}
    assert post(authenticated_client, context).status_code == 400
    assert providers.videos == []
    accepted = post(authenticated_client, {**context, "manual_confirmed": True})
    assert accepted.status_code == 200
    assert accepted.json["context"]["episode_identity"] == source["episode"]
    assert accepted.json["context"]["episode_identity"]["identity_status"] == "unverified"
    video = providers.videos[0][1]
    assert video.series_imdb_id == "tt1234567" and (video.season, video.episode) == (0, 3)
    assert video.imdb_id is None and video.tvdb_id is None and video.tmdb_id is None


@pytest.mark.parametrize("decision_time", [3599.5, 3600, 3601])
@pytest.mark.parametrize("manual", [False, True])
def test_staggered_parent_expiry_bounds_source_linked_provider_search(authenticated_client, providers, resolved_episode, monkeypatch, decision_time, manual):
    from discover import metadata
    clock = [0]
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    metadata._cache.invalidate(hard=True)
    assert metadata.FRESH_SECONDS == 300 and metadata.STALE_SECONDS == 3600
    assert metadata.show_details("100")["data"]["status"] == "available"
    clock[0] = 100
    identity = metadata.episode_details("100", "2", "1")["data"]["episode"]
    assert identity == resolved_episode.context["episode_identity"]
    original = metadata._request
    def request(config, path, params=None):
        if path == "/tv/100/external_ids":
            raise metadata.UpstreamFailure()
        if path == "/tv/100/season/2/episode/1":
            clock[0] = decision_time
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    clock[0] = 3599
    providers.add("discover_parent_expiry")
    context = {**resolved_episode.context, "manual_confirmed": manual}
    if manual:
        context.update(season=0, episode=3)
    response = post(authenticated_client, context)
    accepted = decision_time < 3600
    assert response.status_code == (200 if accepted else 400)
    assert len(providers.videos) == (1 if accepted else 0)
    if accepted:
        assert response.json["context"]["episode_identity"] == identity
        if manual:
            assert response.json["context"]["manual_confirmed"] is True
            video = providers.videos[0][1]
            assert (video.season, video.episode) == (0, 3)
            assert video.imdb_id is None and video.tvdb_id is None and video.tmdb_id is None


@pytest.mark.parametrize("decision_time", [3599.5, 3601])
@pytest.mark.parametrize("manual", [False, True])
def test_new_unverified_source_manual_recovery_requires_unexpired_parent(authenticated_client, providers, resolved_episode, monkeypatch, decision_time, manual):
    from discover import metadata
    clock = [0]
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    metadata._cache.invalidate(hard=True)
    assert metadata.show_details("100")["data"]["status"] == "available"
    original = metadata._request
    child_finished_at = [3599]
    def request(config, path, params=None):
        if path == "/tv/100/external_ids":
            raise metadata.UpstreamFailure()
        if path == "/tv/100/season/2/episode/1":
            clock[0] = child_finished_at[0]
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    clock[0] = 3598
    source = metadata.episode_details("100", "2", "1")["data"]
    assert source["status"] == "unavailable" and source["episode"]["identity_status"] == "unverified"
    assert source["episode"]["show_imdb_id"] == "tt1234567"
    providers.add("discover_parent_expiry")
    child_finished_at[0] = decision_time
    context = {**resolved_episode.context, "episode_identity": source["episode"],
               "manual_confirmed": manual, "season": 0, "episode": 3}
    response = post(authenticated_client, context)
    accepted = manual and decision_time < 3600
    assert response.status_code == (200 if accepted else 400)
    assert len(providers.videos) == (1 if accepted else 0)
    if accepted:
        assert response.json["context"]["episode_identity"] == source["episode"]
        assert providers.videos[0][1].tvdb_id is None


@pytest.fixture
def admission_clock(resolved_episode, monkeypatch):
    from discover import metadata
    clock = [0]
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    metadata._cache.invalidate(hard=True)
    assert metadata.show_details("100")["data"]["status"] == "available"
    clock[0] = 100
    assert metadata.episode_details("100", "2", "1")["data"]["episode"] == resolved_episode.context["episode_identity"]
    original = metadata._request
    reads = []
    def request(config, path, params=None):
        reads.append(path)
        if path == "/tv/100/external_ids":
            raise metadata.UpstreamFailure()
        if path == "/tv/100/season/2/episode/1":
            clock[0] = 3599.5
        return original(config, path, params)
    monkeypatch.setattr(metadata, "_request", request)
    clock[0] = 3599
    return SimpleNamespace(now=clock, original_request=original, reads=reads)


@pytest.mark.parametrize("stage", ["pool", "creator_coverage", "video"])
@pytest.mark.parametrize("decision_time", [3599.75, 3600, 3601])
@pytest.mark.parametrize("manual", [False, True])
def test_source_cutoff_is_rechecked_at_provider_admission(authenticated_client, providers, resolved_episode, admission_clock, monkeypatch, stage, decision_time, manual):
    from compat import service
    from discover import search as discover_search
    preparation = []
    def advance():
        preparation.append(admission_clock.now[0])
        admission_clock.now[0] = decision_time
    if stage == "pool":
        original = discover_search._pool_state
        def pool_state(pool):
            advance()
            return original(pool)
        monkeypatch.setattr(discover_search, "_pool_state", pool_state)
    elif stage == "creator_coverage":
        original = discover_search._coverage
        calls = []
        def coverage(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                advance()
            return original(*args, **kwargs)
        monkeypatch.setattr(discover_search, "_coverage", coverage)
    else:
        original = service._build_video
        def build_video(*args, **kwargs):
            result = original(*args, **kwargs)
            advance()
            return result
        monkeypatch.setattr(service, "_build_video", build_video)
    providers.add("discover_admission")
    context = {**resolved_episode.context, "manual_confirmed": manual}
    if manual:
        context.update(season=0, episode=3)
    response = post(authenticated_client, context)
    assert preparation == [3599.5]
    assert admission_clock.reads.count("/tv/100/season/2/episode/1") == 1
    accepted = decision_time < 3600
    assert response.status_code == (200 if accepted else 400)
    assert len(providers.videos) == (1 if accepted else 0)
    if accepted:
        assert response.json["context"]["episode_identity"] == context["episode_identity"]
        if manual:
            assert response.json["context"]["manual_confirmed"] is True
            assert providers.videos[0][1].tvdb_id is None


@pytest.mark.parametrize("decision_time", [3599.75, 3600, 3601])
@pytest.mark.parametrize("manual", [False, True])
def test_unadmitted_cache_creator_wait_cannot_outlive_source_cutoff(authenticated_client, providers, resolved_episode, admission_clock, monkeypatch, decision_time, manual):
    import threading
    from dogpile.cache.api import NO_VALUE
    from compat import cache
    from discover import metadata
    original_get = cache.compat_region.get_or_create
    original_mutex = cache.compat_region._mutex
    contention = []
    owners = []
    errors = []
    cache_keys = []
    def get_or_create(key, creator, **kwargs):
        cache_keys.append(key)
        lock = original_mutex(key)
        held, waiting = threading.Event(), threading.Event()
        def owner():
            lock.acquire()
            try:
                held.set()
                if not waiting.wait(3):
                    errors.append("Cache creator never reached the held mutex")
                contention.append(admission_clock.now[0])
                admission_clock.now[0] = decision_time
            finally:
                lock.release()
        worker = threading.Thread(target=owner)
        owners.append(worker)
        worker.start()
        assert held.wait(3)
        class ObservedMutex:
            def acquire(self, *args, **kw):
                waiting.set()
                return lock.acquire(*args, **kw)
            def release(self):
                lock.release()
        with monkeypatch.context() as patch:
            patch.setattr(cache.compat_region, "_mutex", lambda requested: ObservedMutex() if requested == key else original_mutex(requested))
            try:
                return original_get(key, creator, **kwargs)
            finally:
                waiting.set()
                worker.join(3)
                assert not worker.is_alive()
    monkeypatch.setattr(cache.compat_region, "get_or_create", get_or_create)
    providers.add("discover_creator_wait")
    context = {**resolved_episode.context, "manual_confirmed": manual}
    if manual:
        context.update(season=0, episode=3)
    response = post(authenticated_client, context)
    assert contention == [3599.5] and errors == []
    assert all(not worker.is_alive() for worker in owners)
    accepted = decision_time < 3600
    assert response.status_code == (200 if accepted else 400)
    assert len(providers.videos) == (1 if accepted else 0)
    if not accepted:
        assert cache.compat_region.get(cache_keys[0]) is NO_VALUE
        monkeypatch.setattr(cache.compat_region, "get_or_create", original_get)
        monkeypatch.setattr(metadata, "_request", admission_clock.original_request)
        admission_clock.now[0] = 3602
        retried = post(authenticated_client, context)
        assert retried.status_code == 200 and retried.json["cache_status"] == "fresh"
        assert len(providers.videos) == 1


@pytest.mark.parametrize("manual", [False, True])
def test_admitted_provider_completion_keeps_results_and_handles_after_source_expiry(authenticated_client, providers, resolved_episode, admission_clock, monkeypatch, manual):
    from compat import cache, service
    from discover import metadata
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker
    candidate = candidate_from_worker("discover_admitted", {"id": "1", "language": {"alpha3": "eng"},
        "release_info": "Northern.Light.S02E01", "provider_payload": {}})
    providers.add("discover_admitted", [candidate])
    original_search = service.search_title
    admitted = []
    def search_title(*args):
        admitted.append(admission_clock.now[0])
        admission_clock.now[0] = 3601
        return original_search(*args)
    monkeypatch.setattr(service, "search_title", search_title)
    keys = []
    original_get = cache.compat_region.get_or_create
    def get_or_create(key, *args, **kwargs):
        keys.append(key)
        return original_get(key, *args, **kwargs)
    monkeypatch.setattr(cache.compat_region, "get_or_create", get_or_create)
    context = {**resolved_episode.context, "manual_confirmed": manual}
    if manual:
        context.update(season=0, episode=3)
    first = post(authenticated_client, context)
    assert first.status_code == 200 and admitted == [3599.5]
    assert admission_clock.now[0] == 3601 and len(providers.videos) == 1
    row = first.json["results"][0]
    stored = resolve_result(row["id"], row["search_id"])
    assert stored["context"] == first.json["context"]
    expected = {**resolved_episode.context, "language": "en", "matching_mode": "title"}
    if manual:
        expected.update(manual_confirmed=True, season=0, episode=3)
    assert first.json["context"] == expected
    # New, unchanged parent evidence has a later deadline. It must reuse the
    # accepted result identity and handle while their own lifetime is valid.
    monkeypatch.setattr(metadata, "_request", admission_clock.original_request)
    admission_clock.now[0] = 3602
    second = post(authenticated_client, context)
    assert second.status_code == 200 and second.json["cache_status"] == "cached"
    assert second.json["results"][0]["id"] == row["id"]
    assert second.json["search_id"] == first.json["search_id"]
    assert keys[0] == keys[1] and len(providers.videos) == 1
    assert resolve_result(row["id"], row["search_id"])["context"] == expected


@pytest.mark.parametrize("field", ["valid_until", "metadata_valid_until", "admission", "dependency_valid_until"])
def test_client_cannot_supply_a_provider_admission_deadline(authenticated_client, providers, resolved_episode, field):
    from copy import deepcopy
    providers.add("discover_forged_admission")
    payload = deepcopy(resolved_episode.context)
    payload[field] = 10**99
    assert post(authenticated_client, payload).status_code == 400
    payload = deepcopy(resolved_episode.context)
    payload["episode_identity"][field] = 10**99
    assert post(authenticated_client, payload).status_code == 400
    assert providers.videos == []


@pytest.mark.parametrize("manual", [False, True])
def test_cached_results_remain_available_when_no_new_provider_operation_is_admitted(authenticated_client, providers, resolved_episode, admission_clock, monkeypatch, manual):
    from compat import cache
    providers.add("discover_cached_admission")
    context = {**resolved_episode.context, "manual_confirmed": manual}
    first = post(authenticated_client, context)
    assert first.status_code == 200 and len(providers.videos) == 1
    original = cache.compat_region.get_or_create
    observed = []
    def delayed_get(*args, **kwargs):
        observed.append(admission_clock.now[0])
        admission_clock.now[0] = 3601
        return original(*args, **kwargs)
    monkeypatch.setattr(cache.compat_region, "get_or_create", delayed_get)
    second = post(authenticated_client, context)
    assert observed == [3599.5]
    assert second.status_code == 200 and second.json["cache_status"] == "cached"
    assert second.json["search_id"] == first.json["search_id"]
    assert len(providers.videos) == 1


@pytest.mark.parametrize("decision_time", [3599.75, 3600, 3601])
def test_manual_show_linkage_retains_its_parent_admission_bound(authenticated_client, providers, resolved_episode, admission_clock, monkeypatch, decision_time):
    from discover import search as discover_search
    original = discover_search._pool_state
    prepared = []
    def pool_state(pool):
        prepared.append(admission_clock.now[0])
        admission_clock.now[0] = decision_time
        return original(pool)
    monkeypatch.setattr(discover_search, "_pool_state", pool_state)
    providers.add("discover_manual_show")
    context = {key: value for key, value in resolved_episode.context.items() if key != "episode_identity"}
    response = post(authenticated_client, {**context, "manual_confirmed": True})
    assert prepared == [3599]
    assert response.status_code == (200 if decision_time < 3600 else 400)
    assert len(providers.videos) == (1 if decision_time < 3600 else 0)


@pytest.mark.parametrize("decision_time", [3599.75, 3600, 3601])
def test_unverified_source_manual_admission_keeps_the_parent_cutoff(authenticated_client, providers, resolved_episode, admission_clock, monkeypatch, decision_time):
    from discover import metadata
    from discover import search as discover_search
    resolved_episode.raw["name"] = "Home, revised"
    source = metadata.episode_details("100", "2", "1")["data"]
    assert source["status"] == "unavailable" and source["episode"]["identity_status"] == "unverified"
    original = discover_search._pool_state
    prepared = []
    def pool_state(pool):
        prepared.append(admission_clock.now[0])
        admission_clock.now[0] = decision_time
        return original(pool)
    monkeypatch.setattr(discover_search, "_pool_state", pool_state)
    providers.add("discover_unverified_admission")
    context = {**resolved_episode.context, "episode_identity": source["episode"], "manual_confirmed": True, "season": 0, "episode": 3}
    response = post(authenticated_client, context)
    assert prepared == [3599.5]
    assert response.status_code == (200 if decision_time < 3600 else 400)
    assert len(providers.videos) == (1 if decision_time < 3600 else 0)
    if decision_time < 3600:
        assert response.json["context"]["episode_identity"] == source["episode"]
        assert providers.videos[0][1].tvdb_id is None


@pytest.mark.parametrize("payload", [
    {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"},
    {"media_type": "episode", "imdb_id": "tt0133093", "language": "eng", "season": 0, "episode": 3, "manual_confirmed": True},
    {"mode": "release", "query": "Example.Movie.2024", "language": "eng"},
])
def test_modes_without_source_linkage_do_not_acquire_metadata_deadlines(authenticated_client, providers, monkeypatch, payload):
    from discover import metadata
    monkeypatch.setattr(metadata, "time", SimpleNamespace(monotonic=lambda: 10**12))
    def forbidden(*args, **kwargs):
        pytest.fail("This explicitly selected mode must not acquire metadata evidence")
    monkeypatch.setattr(metadata, "_request", forbidden)
    providers.add("discover_no_source")
    response = post(authenticated_client, payload)
    assert response.status_code == 200 and len(providers.videos) == 1


def _progress_post(client, payload, identity):
    return client.post("/api/discover/search", json=payload,
                       headers={"X-API-KEY": "discover-test-key", "X-Discover-Progress": identity})


def test_rows_are_offered_while_other_providers_are_still_pending(authenticated_client, providers, monkeypatch):
    """A finished provider's rows travel with the observation, not after the search."""
    import copy as copy_module
    from discover import progress
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker

    published = []
    original = progress.publish

    def record(identity, observation):
        published.append(copy_module.deepcopy(observation))
        original(identity, observation)

    monkeypatch.setattr(progress, "publish", record)
    for name in ("discover_one", "discover_two"):
        providers.add(name, [candidate_from_worker(name, {
            "id": f"{name}-1", "language": {"alpha3": "eng"},
            "release_info": f"The.Matrix.1999.{name}", "matches": ["imdb_id"],
            "provider_payload": {}})])
    identity = "11111111-2222-3333-4444-555555555555"
    response = _progress_post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}, identity)
    assert response.status_code == 200
    final = {row["id"]: row for row in response.json["results"]}
    assert len(final) == 2

    early = [observation for observation in published
             if observation.get("results")
             and any(item["status"] == "pending" for item in observation["providers"])]
    assert early, "no observation carried a result while a provider was still pending"
    offered = [row["id"] for row in early[0]["results"]]
    assert offered
    # Actionable at once: the handle a reader is offered mid-search resolves,
    # and it is still the same handle in the finished snapshot.
    for result_id in offered:
        assert resolve_result(result_id, early[0]["search_id"]) is not None
        assert result_id in final
    assert early[0]["context"]["imdb_id"] == "tt0133093"
    # The finished observation hands the result list back to the snapshot.
    assert published[-1]["phase"] == "finished"
    assert not published[-1].get("results")


def test_publishing_rows_early_does_not_mint_a_second_handle(authenticated_client, providers):
    from compat.file_id_store import get_store
    from provider_hub.protocol import candidate_from_worker

    providers.add("discover_one", [candidate_from_worker("discover_one", {
        "id": "1", "language": {"alpha3": "eng"}, "release_info": "The Matrix",
        "matches": ["imdb_id"], "provider_payload": {}})])
    before = len(get_store())
    response = _progress_post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"},
        "22222222-3333-4444-5555-666666666666")
    assert response.status_code == 200
    assert len(response.json["results"]) == 1
    assert len(get_store()) - before == 1


def test_search_without_an_observer_is_unchanged(authenticated_client, providers):
    """No progress identity means no early rows and no early handles."""
    from compat.file_id_store import get_store
    from provider_hub.protocol import candidate_from_worker

    providers.add("discover_one", [candidate_from_worker("discover_one", {
        "id": "1", "language": {"alpha3": "eng"}, "release_info": "The Matrix",
        "matches": ["imdb_id"], "provider_payload": {}})])
    before = len(get_store())
    response = post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"})
    assert response.status_code == 200
    assert len(response.json["results"]) == 1
    assert len(get_store()) - before == 1


def test_a_search_that_only_skipped_providers_is_not_reported_as_failed(authenticated_client, providers):
    """A skip is a correct answer about this target, not a failure. Reporting
    the whole search as failed put "No provider completed this search" in front
    of a reader whose providers had all behaved exactly as intended."""
    from subliminal.video import Episode

    providers.add("discover_episodes_only", video_types=(Episode,))
    providers.add("discover_other_episodes_only", video_types=(Episode,))
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert response["status"] == "skipped"
    assert {item["status"] for item in response["coverage"]["providers"]} == {"skipped"}
    assert response["coverage"]["complete"] is False


def test_one_real_failure_beside_a_correct_skip_is_still_a_failed_search(authenticated_client, providers):
    """The distinct state is only for a search where nothing was asked. One
    provider asked and failing is a failed search however many others were
    correctly skipped, and the frontend must not have to work that out itself."""
    from subliminal.video import Episode
    from subliminal.exceptions import AuthenticationError

    providers.add("discover_episodes_only", video_types=(Episode,))
    providers.add("discover_refuses", error=AuthenticationError("fixture failure"))
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert response["status"] == "failed"
    assert {item["provider"]: item["status"] for item in response["coverage"]["providers"]} == {
        "discover_episodes_only": "skipped", "discover_refuses": "authentication_required"}


def test_one_success_beside_a_skip_still_completes(authenticated_client, providers):
    from provider_hub.protocol import candidate_from_worker
    from subliminal.video import Episode

    providers.add("discover_episodes_only", video_types=(Episode,))
    providers.add("discover_found", [candidate_from_worker("discover_found", {
        "id": "one", "language": {"alpha3": "eng"}, "provider_payload": {},
    })])
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert response["status"] == "complete" and len(response["results"]) == 1


def _seconds_from_now(stamp):
    import datetime as dt

    moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (moment - dt.datetime.now(dt.timezone.utc)).total_seconds()


def _retry_seconds(outcome):
    return _seconds_from_now(outcome["retry_at"])


@pytest.mark.parametrize("error,status,floor,ceiling", [
    # A site answering 500 to every request used to be re-asked in 60 seconds,
    # on the same schedule as a provider that had merely been slow.
    (RuntimeError("upstream returned 500"), "error", 240, 320),
    (None, "authentication_required", 840, 920),
])
def test_the_wait_before_asking_again_comes_from_the_cause(authenticated_client, providers,
                                                           error, status, floor, ceiling):
    from subliminal.exceptions import AuthenticationError

    providers.add("discover_backoff", error=error or AuthenticationError("fixture failure"))
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    outcome = response["coverage"]["providers"][0]
    assert outcome["status"] == status
    assert floor < _retry_seconds(outcome) < ceiling


def test_a_provider_retry_after_outranks_the_per_cause_wait(authenticated_client, providers):
    from subliminal_patch.exceptions import APIThrottled

    providers.add("discover_retry_after", error=APIThrottled(retry_after=42))
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    outcome = response["coverage"]["providers"][0]
    assert outcome["status"] == "cooldown"
    assert 20 < _retry_seconds(outcome) < 60


def test_a_rate_limit_is_named_rather_than_folded_into_cooling_down(authenticated_client, providers, monkeypatch):
    """The throttle table records the exception class, so a rate limit and a
    download quota arrive here distinguishable. The status map had no entry for
    either, so both reached the reader as a generic cooldown."""
    import datetime as dt
    from app import get_providers

    providers.add("discover_limited")
    monkeypatch.setattr(get_providers, "get_providers_sorted", lambda: [])
    get_providers.tp["discover_limited"] = (
        "DownloadLimitExceeded", dt.datetime.now() + dt.timedelta(hours=3), "3 hours")
    response = post(authenticated_client, {"media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    outcome = response["coverage"]["providers"][0]
    assert (outcome["status"], outcome["reason"]) == ("cooldown", "download_limit_reached")
    assert providers.videos == []


def test_the_longer_of_the_two_running_deadlines_decides_the_retry(authenticated_client, providers):
    """A Retry-After the provider sent and a throttle-table entry can both be
    running. Publishing the shorter one offers a retry that searches nothing:
    get_providers_sorted() still excludes the provider until the table's
    deadline, so the retry only replaces the deadline on screen."""
    import datetime as dt
    from app import get_providers
    from subliminal_patch.exceptions import APIThrottled

    providers.add("discover_two_clocks", error=APIThrottled(retry_after=42))
    get_providers.tp["discover_two_clocks"] = (
        "TooManyRequests", dt.datetime.now() + dt.timedelta(hours=1), "1 hour")
    response = post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    outcome = response["coverage"]["providers"][0]
    assert outcome["status"] == "cooldown"
    assert 3400 < _retry_seconds(outcome) < 3700


def test_a_retry_after_still_wins_when_no_table_entry_outlasts_it(authenticated_client, providers):
    """The longer of the two, not the table unconditionally."""
    import datetime as dt
    from app import get_providers
    from subliminal_patch.exceptions import APIThrottled

    providers.add("discover_one_clock", error=APIThrottled(retry_after=600))
    get_providers.tp["discover_one_clock"] = (
        "APIThrottled", dt.datetime.now() + dt.timedelta(seconds=30), "30 seconds")
    response = post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert 560 < _retry_seconds(response["coverage"]["providers"][0]) < 620


def test_a_provider_that_never_searched_for_want_of_setup_is_not_complete_coverage(
        authenticated_client, providers):
    """A skip is the provider declining this target and has no answer to give.
    A provider missing its configuration has answers it was never able to look
    for, so a search beside one is partial, however well the others did. It
    also must not buy the long complete-search cache lifetime."""
    from provider_hub.protocol import candidate_from_worker
    from subliminal.exceptions import ConfigurationError

    providers.add("discover_found", [candidate_from_worker("discover_found", {
        "id": "one", "language": {"alpha3": "eng"}, "provider_payload": {}})])
    providers.add("discover_unconfigured", error=ConfigurationError("no credentials"))
    response = post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"}).json
    assert {item["provider"]: item["status"] for item in response["coverage"]["providers"]} == {
        "discover_found": "success", "discover_unconfigured": "setup_required"}
    assert response["status"] == "partial"
    assert response["coverage"]["complete"] is False
    assert len(response["results"]) == 1


def _fake_clock(monkeypatch, module, start=0.0):
    """Replace a module's time source with one that only moves when told.

    Budget accounting is what is under test, so the clock has to be driven by
    the work being charged rather than by how busy the machine is.
    """
    import time as real_time
    import types

    clock = types.SimpleNamespace(now=start)
    monkeypatch.setattr(module, "time",
                        types.SimpleNamespace(time=real_time.time, monotonic=lambda: clock.now))
    return clock


def _charge_row_building(monkeypatch, module, clock, seconds):
    real_result = module._result

    def costly(*args, **kwargs):
        row = real_result(*args, **kwargs)
        clock.now += seconds
        return row

    monkeypatch.setattr(module, "_result", costly)


def test_the_live_row_budget_is_spent_per_row_not_per_response(authenticated_client, providers,
                                                               monkeypatch):
    """One provider answering with a large batch used to overrun the budget by
    as much as it liked: the check only ran on the way into the batch. Every
    millisecond of that scoring is charged to the shared wall the remaining
    providers are still being judged against, so the overrun is paid for by
    reporting working providers as abandoned."""
    import copy as copy_module
    from discover import progress, search as search_module
    from provider_hub.protocol import candidate_from_worker

    # Building one row costs 0.6 of a second on a clock that moves only when a
    # row is built, so the 1.5 second budget stops the loop after three of them
    # and nothing else in the search is charged. Real sleeping would make the
    # same point in five seconds instead of none.
    clock = _fake_clock(monkeypatch, search_module)
    _charge_row_building(monkeypatch, search_module, clock, 0.6)
    published = []
    original = progress.publish

    def record(identity, observation):
        published.append(copy_module.deepcopy(observation))
        return original(identity, observation)

    monkeypatch.setattr(progress, "publish", record)

    batch = [candidate_from_worker("discover_batch", {
        "id": f"batch-{index}", "language": {"alpha3": "eng"},
        "release_info": f"The.Matrix.1999.copy{index}", "matches": ["imdb_id"],
        "provider_payload": {}}) for index in range(8)]
    providers.add("discover_batch", batch)
    response = _progress_post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"},
        "33333333-4444-5555-6666-777777777777")
    assert response.status_code == 200
    offered = [observation["results"] for observation in published if observation.get("results")]
    assert offered, "no observation carried a result"
    assert len(offered[-1]) == 3, "the budget did not stop the batch part way through"
    # The rest is built after the fanout, which is where all of it was built
    # before, so the reader still ends up with every row.
    assert len(response.json["results"]) == 8
    assert len({row["id"] for row in response.json["results"]}) == 8


def test_a_row_offered_early_still_resolves_when_the_search_finishes(authenticated_client,
                                                                     providers, monkeypatch):
    """The early handle's lifetime starts when it is offered, the snapshot's
    when the fanout returns. With a result TTL shorter than the search, the
    rows filed into a perfectly live cache entry named handles the store had
    already dropped, so the first download attempt failed."""
    from app.config import settings
    from discover.handles import resolve_result
    from provider_hub.protocol import candidate_from_worker

    for key in ("cache_ttl_seconds", "file_id_ttl_seconds"):
        monkeypatch.setattr(settings.compat_endpoint, key, 2)
    providers.add("discover_fast", [candidate_from_worker("discover_fast", {
        "id": "fast-1", "language": {"alpha3": "eng"}, "release_info": "The Matrix",
        "matches": ["imdb_id"], "provider_payload": {}})])
    providers.add("discover_slow", delay=2.6)
    response = _progress_post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"},
        "44444444-5555-6666-7777-888888888888")
    assert response.status_code == 200
    rows = response.json["results"]
    assert len(rows) == 1
    assert resolve_result(rows[0]["id"], response.json["search_id"]) is not None
    # And with its whole lifetime ahead of it, not the remainder of one that
    # started before the slow provider had even answered.
    assert _seconds_from_now(rows[0]["expires_at"]) > 1.0


def test_publishing_the_growing_row_list_is_charged_to_the_same_budget(authenticated_client,
                                                                       providers, monkeypatch):
    """Every outcome republishes the whole accumulated list and the observer
    copies it, so with enough rows the copying costs more than the building the
    budget was written to bound, and it comes out of the same wall. Charging it
    is what stops the list growing once that is where the budget is going."""
    import copy as copy_module
    from discover import progress, search as search_module
    from provider_hub.protocol import candidate_from_worker

    clock = _fake_clock(monkeypatch, search_module)
    published = []
    original = progress.publish

    def costly_publish(identity, observation):
        published.append(copy_module.deepcopy(observation))
        # 0.8 a call: the first two exhaust the 1.5 second budget between them.
        clock.now += 0.8
        return original(identity, observation)

    monkeypatch.setattr(progress, "publish", costly_publish)

    for name in ("discover_first", "discover_second"):
        providers.add(name, [candidate_from_worker(name, {
            "id": f"{name}-{index}", "language": {"alpha3": "eng"},
            "release_info": f"The.Matrix.1999.{name}.{index}", "matches": ["imdb_id"],
            "provider_payload": {}}) for index in range(4)])
    response = _progress_post(authenticated_client, {
        "media_type": "movie", "imdb_id": "tt0133093", "language": "eng"},
        "55555555-6666-7777-8888-999999999999")
    assert response.status_code == 200

    offered = [observation["results"] for observation in published if observation.get("results")]
    assert offered, "no observation carried a result"
    assert len(offered[-1]) == 4, "publishing was not charged, so the list kept growing"
    assert len(response.json["results"]) == 8

"""Manual-search GETs keep their rows and explain each considered provider."""

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from flask import Flask
from flask_restx import Api


PROVIDER_ORDER = ["zeta_search", "alpha_search", "middle_search"]


@pytest.fixture(params=["episode", "movie"])
def manual_endpoint(request, monkeypatch):
    # Only the subtitles package is stubbed.
    # The real bazarr/api package loads before Api(app) runs.
    root = Path(__file__).resolve().parents[2] / "bazarr"
    for name, directory in (("subtitles", "subtitles"),):
        if name not in sys.modules:
            package = ModuleType(name)
            package.__path__ = [str(root / directory)]
            monkeypatch.setitem(sys.modules, name, package)

    import app.database
    import subliminal
    from app import get_providers
    from app.config import settings
    from provider_hub.registry import HubProxyProvider
    from provider_hub.worker import ProviderWorkerClient
    from subliminal.video import Episode, Movie
    from subliminal_patch import core
    from subliminal_patch.subtitle import Subtitle
    from subtitles import manual, pool
    from subzero.language import Language

    kind = request.param
    media_type = "series" if kind == "episode" else "movie"
    language = Language("eng")
    path = f"/library/{kind}.mkv"
    video = (Episode(path, "Example", 1, 2) if kind == "episode"
             else Movie(path, "Example", year=2001))
    matches = ({"series", "season", "episode"} if kind == "episode"
               else {"title", "year"})
    state = {name: "empty" for name in PROVIDER_ORDER}
    state["alpha_search"] = "found"
    calls = []
    raw_returns = []
    candidates = {}
    worker = ProviderWorkerClient([])
    # A real held outcome latch refuses a turn before starting a subprocess.
    worker._outcome_latch.clear()
    busy_proxy = HubProxyProvider(worker_client=worker)
    busy_proxy.admit = lambda: True

    class Candidate(Subtitle):
        def __init__(self, provider_name, candidate_id):
            super().__init__(language, candidate_id,
                             page_link=f"https://example.invalid/{provider_name}/{candidate_id}")
            self.provider_name = provider_name
            self.release_info = "Example.Release"

        def get_matches(self, searched_video):
            return set(matches)

    class FakeProvider:
        languages = {language}

        @classmethod
        def check(cls, searched_video):
            return isinstance(searched_video, (Episode, Movie))

        def initialize(self):
            pass

        def terminate(self):
            pass

        def list_subtitles(self, searched_video, searched_languages):
            calls.append(self.provider_name)
            outcome = state[self.provider_name]
            if outcome == "failed":
                raise RuntimeError("fixture search failure")
            if outcome == "throttled":
                raise core.APIThrottled("fixture search quota")
            if outcome == "busy":
                return busy_proxy._gated_request("search", {}, timeout=0.001)
            return candidates[self.provider_name] if outcome == "found" else []

    registry = {
        name: type(f"Provider{index}", (FakeProvider,), {"provider_name": name})
        for index, name in enumerate(PROVIDER_ORDER)
    }
    for name, count in (("zeta_search", 2), ("alpha_search", 1), ("middle_search", 0)):
        candidates[name] = [Candidate(name, f"candidate-{index}") for index in range(count)]
    monkeypatch.setattr(core.provider_registry, "providers", registry)
    monkeypatch.setattr(get_providers, "_ensure_provider_hub_registered", lambda: None)
    monkeypatch.setattr(get_providers, "_decrypt_blocked_provider_ids", set)
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(get_providers, "set_throttled_providers", lambda data: None)
    monkeypatch.setattr(settings.general, "enabled_providers",
                        ["middle_search", "alpha_search", "zeta_search"])
    monkeypatch.setattr(settings.general, "provider_priorities",
                        {"zeta_search": 10, "alpha_search": 20, "middle_search": 30})
    monkeypatch.setattr(settings.general, "provider_score_modifiers", {})
    monkeypatch.setattr(settings.general, "minimum_score", 0)
    monkeypatch.setattr(settings.general, "minimum_score_movie", 0)

    def throttle(name, error, **context):
        return get_providers.provider_throttle(name, error, wait=False, **context)

    provider_pool = core.SZProviderPool(
        get_providers.get_providers_sorted(), {}, throttle_callback=throttle,
        adoption_gate=get_providers.provider_is_usable,
    )
    monkeypatch.setattr(pool, "_update_pool", lambda *args, **kwargs: False)
    monkeypatch.setattr(manual, "_get_pool", lambda *args, **kwargs: provider_pool)
    monkeypatch.setattr(manual, "_get_language_obj", lambda profile_id: ({language}, False))
    monkeypatch.setattr(manual, "get_video", lambda *args, **kwargs: video)
    suffix = "episodes" if kind == "episode" else "movies"
    routes = importlib.import_module(f"api.providers.providers_{suffix}")
    monkeypatch.setattr(subliminal, "region", SimpleNamespace(backend=SimpleNamespace(sync=lambda: None)))
    original_search = manual._manual_search

    def record_return(*args, **kwargs):
        result = original_search(*args, **kwargs)
        raw_returns.append(result)
        return result

    monkeypatch.setattr(manual, "_manual_search", record_return)
    target = {"path": path, "title": "Example", "sceneName": "None", "profileId": 1,
              "arr_instance_id": 7, "season": 1, "episode": 2, "year": "2001"}
    target_function = "_episode_search_target" if kind == "episode" else "_movie_search_target"
    monkeypatch.setattr(routes, target_function, lambda item_id: (target, None))
    app = Flask(__name__)
    app.config["TESTING"] = True
    namespace = getattr(routes, f"api_ns_providers_{suffix}")
    Api(app).add_namespace(namespace, path="/api/")
    client = app.test_client()

    def search(outcome):
        state["zeta_search"] = outcome
        if outcome == "excluded":
            provider_pool.discarded_providers.add("zeta_search")
        parameter = "episodeid" if kind == "episode" else "radarrid"
        return client.get(f"/api/providers/{suffix}", query_string={parameter: 42},
                          headers={"X-API-KEY": settings.auth.apikey})

    yield SimpleNamespace(
        search=search, pool=provider_pool, calls=calls, raw_returns=raw_returns,
        candidates=candidates, state=state, kind=kind, matches=matches, manual=manual,
        get_providers=get_providers, path=path, media_type=media_type,
    )
    provider_pool.terminate()


def assert_legacy_rows(endpoint, response, *, focus_found=False):
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    rows = body.get("data")
    assert type(rows) is list
    assert len(endpoint.raw_returns) == 1
    raw = endpoint.raw_returns[0]
    assert isinstance(raw, list)
    expected_providers = (["zeta_search", "zeta_search"] if focus_found else []) + ["alpha_search"]
    assert [row["provider"] for row in rows] == expected_providers
    assert [row["provider"] for row in raw] == expected_providers
    expected_score = 221 if endpoint.kind == "episode" else 101
    expected_percent = 61 if endpoint.kind == "episode" else 56
    expected_missing = ({"hash", "year", "source", "release_group", "audio_codec", "resolution",
                         "video_codec", "streaming_service"} if endpoint.kind == "episode"
                        else {"hash", "source", "edition", "release_group", "audio_codec", "resolution",
                              "video_codec", "streaming_service"})
    for row, original in zip(rows, raw, strict=True):
        assert set(row) == {
            "score", "orig_score", "score_without_hash", "forced", "language", "hearing_impaired",
            "provider", "subtitle", "url", "original_format", "matches", "dont_matches",
            "release_info", "uploader",
        }
        assert row["score"] == expected_percent
        assert row["orig_score"] == row["score_without_hash"] == expected_score
        assert row["forced"] == row["hearing_impaired"] == row["original_format"] == "False"
        assert row["language"] == "en"
        assert set(row["matches"]) == endpoint.matches | {"hearing_impaired"}
        assert set(row["dont_matches"]) == expected_missing
        assert row["release_info"] == ["Example.Release"]
        assert row["uploader"] is None
        assert isinstance(row["subtitle"], str) and row["subtitle"]
        cached = endpoint.manual.subtitle_cache.get(row["subtitle"])
        assert cached in endpoint.candidates[row["provider"]]
        assert row["url"] == cached.page_link
        assert original["subtitle"] == row["subtitle"]
        assert original["original_format"] is False
    return body


def assert_provider_summary(body, status, results=0):
    providers = body.get("providers")
    assert isinstance(providers, list)
    assert providers == [
        {"name": "zeta_search", "status": status, "results": results},
        {"name": "alpha_search", "status": "found", "results": 1},
        {"name": "middle_search", "status": "empty", "results": 0},
    ]
    for provider in providers:
        assert type(provider["results"]) is int


def test_found_provider_reports_count_in_configured_order(manual_endpoint):
    response = manual_endpoint.search("found")
    body = assert_legacy_rows(manual_endpoint, response, focus_found=True)
    assert manual_endpoint.calls == PROVIDER_ORDER
    assert_provider_summary(body, "found", 2)


def test_unrecorded_result_provider_reports_found(manual_endpoint):
    candidate = manual_endpoint.candidates["alpha_search"][0]
    candidate.provider_name = "unrecorded_search"

    response = manual_endpoint.search("empty")

    assert response.status_code == 200
    body = response.get_json()
    assert [row["provider"] for row in body["data"]] == ["unrecorded_search"]
    assert {"name": "unrecorded_search", "status": "found", "results": 1} in body["providers"]


def test_empty_provider_remains_visible_without_a_result_row(manual_endpoint):
    response = manual_endpoint.search("empty")
    body = assert_legacy_rows(manual_endpoint, response)
    assert manual_endpoint.calls == PROVIDER_ORDER
    assert_provider_summary(body, "empty")


def test_failed_provider_does_not_erase_other_providers_results(manual_endpoint):
    response = manual_endpoint.search("failed")
    body = assert_legacy_rows(manual_endpoint, response)
    assert manual_endpoint.calls == PROVIDER_ORDER
    assert manual_endpoint.get_providers.tp["zeta_search"][0] == "RuntimeError"
    assert_provider_summary(body, "failed")


def test_throttled_provider_is_distinct_from_an_empty_provider(manual_endpoint):
    response = manual_endpoint.search("throttled")
    body = assert_legacy_rows(manual_endpoint, response)
    assert manual_endpoint.calls == PROVIDER_ORDER
    assert manual_endpoint.get_providers.tp["zeta_search"][0] == "APIThrottled"
    assert not manual_endpoint.get_providers.provider_is_usable("zeta_search")
    assert_provider_summary(body, "throttled")


def test_backed_off_provider_is_reported_as_throttled_without_a_call(manual_endpoint):
    backoff = ("APIThrottled", datetime.now() + timedelta(minutes=10), "10 minutes")
    manual_endpoint.get_providers.tp["zeta_search"] = backoff

    response = manual_endpoint.search("empty")

    body = assert_legacy_rows(manual_endpoint, response)
    assert manual_endpoint.calls == ["alpha_search", "middle_search"]
    assert manual_endpoint.get_providers.tp["zeta_search"] == backoff
    assert_provider_summary(body, "throttled")


def test_busy_provider_is_visible_without_recording_a_failure(manual_endpoint):
    response = manual_endpoint.search("busy")
    body = assert_legacy_rows(manual_endpoint, response)
    assert manual_endpoint.calls == PROVIDER_ORDER
    assert "zeta_search" not in manual_endpoint.pool.discarded_providers
    assert manual_endpoint.get_providers.tp == {}
    assert_provider_summary(body, "busy")


def test_excluded_provider_is_visible_without_being_called(manual_endpoint):
    response = manual_endpoint.search("excluded")
    body = assert_legacy_rows(manual_endpoint, response)
    assert manual_endpoint.calls == ["alpha_search", "middle_search"]
    assert "zeta_search" in manual_endpoint.pool.discarded_providers
    assert manual_endpoint.get_providers.tp == {}
    assert_provider_summary(body, "excluded")


def test_no_results_keeps_the_empty_data_list(manual_endpoint):
    manual_endpoint.state["alpha_search"] = "empty"

    response = manual_endpoint.search("empty")

    assert response.status_code == 200
    body = response.get_json()
    assert body.get("data") == []
    assert type(body["data"]) is list
    assert manual_endpoint.raw_returns == [[]]
    assert manual_endpoint.calls == PROVIDER_ORDER


def test_no_results_reports_every_provider_as_empty(manual_endpoint):
    manual_endpoint.state["alpha_search"] = "empty"

    response = manual_endpoint.search("empty")

    body = response.get_json()
    assert body.get("providers") == [
        {"name": "zeta_search", "status": "empty", "results": 0},
        {"name": "alpha_search", "status": "empty", "results": 0},
        {"name": "middle_search", "status": "empty", "results": 0},
    ]
    for provider in body["providers"]:
        assert type(provider["results"]) is int


def test_manual_search_job_publishes_providers_beside_rows(manual_endpoint, monkeypatch):
    manual_endpoint.state["zeta_search"] = "found"
    manual = manual_endpoint.manual
    published = []
    monkeypatch.setattr(manual, "jobs_queue", SimpleNamespace(
        publish_job_result=lambda job_id, value: published.append((job_id, value)),
        update_job_progress=lambda **kwargs: None,
    ))
    monkeypatch.setattr(manual.activity, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(manual, "_JobStop", lambda job_id: SimpleNamespace(is_set=lambda: False))

    manual.run_manual_search_job(
        manual_endpoint.path, 1, "None", "Example", manual_endpoint.media_type, job_id=77,
    )

    assert manual_endpoint.calls == PROVIDER_ORDER
    assert len(published) == 1
    job_id, body = published[0]
    assert job_id == 77
    assert isinstance(body, dict)
    assert type(body["data"]) is list
    assert [row["provider"] for row in body["data"]] == ["zeta_search", "zeta_search", "alpha_search"]
    assert_provider_summary(body, "found", 2)


def test_all_throttled_preserves_the_aggregate_error(manual_endpoint):
    providers = manual_endpoint.get_providers
    from subliminal_patch.exceptions import APIThrottled

    for name in PROVIDER_ORDER:
        providers.provider_throttle(name, APIThrottled("fixture quota"), wait=False)
    assert providers.get_providers_sorted() is None

    response = manual_endpoint.search("empty")

    assert response.status_code == 500
    assert response.get_json() == "All providers are throttled"
    assert manual_endpoint.raw_returns == ["All providers are throttled"]
    assert manual_endpoint.calls == []

import time
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize("code,remote_class,expected", [
    ("provider", "ConnectionError", "unreachable"),
    ("provider", "ReadTimeout", "timeout"),
    ("provider", "UnknownError", "error"),
    ("internal", "ReadTimeout", "error"),
])
def test_worker_outcome_classification_does_not_change_legacy_exception(code, remote_class, expected):
    from provider_hub.worker import WorkerError, _raise_worker_error
    from subliminal_patch.core import provider_search_failure

    with pytest.raises(WorkerError) as raised:
        _raise_worker_error({"code": code, "class_name": remote_class, "message": "ReadTimeout ConnectionError"})
    assert raised.value.code == code
    assert raised.value.remote_class_name == remote_class
    assert provider_search_failure("example", raised.value).status == expected


@pytest.mark.parametrize("kind,expected", [
    ("empty", "empty"), ("auth", "authentication_required"),
    ("throttle", "cooldown"), ("connection", "unreachable"),
    ("timeout", "timeout"), ("error", "error"), ("language", "skipped"),
])
def test_detailed_real_pool_preserves_provider_outcome(monkeypatch, kind, expected):
    from provider_hub.registry import HubProxyProvider
    from subliminal.exceptions import AuthenticationError
    from subliminal_patch.core import SZAsyncProviderPool
    from subliminal_patch.core_persistent import list_all_subtitles_parallel
    from subliminal_patch.extensions import provider_registry
    from subliminal_patch.exceptions import APIThrottled
    from subliminal.video import Movie
    from subzero.language import Language
    from requests.exceptions import ConnectionError, Timeout

    failures = {"auth": AuthenticationError(), "throttle": APIThrottled(retry_after=120),
                "connection": ConnectionError(), "timeout": Timeout(), "error": RuntimeError()}

    class FixtureProvider(HubProxyProvider):
        languages = {Language("fra") if kind == "language" else Language("eng")}

        def list_subtitles(self, video, languages):
            if kind in failures:
                raise failures[kind]
            return []

    monkeypatch.setitem(provider_registry.providers, "discover_outcome", FixtureProvider)
    pool = SZAsyncProviderPool(providers=["discover_outcome"], provider_configs={})
    outcomes = []
    list_all_subtitles_parallel(
        [Movie("", "The Matrix", imdb_id="tt0133093")], {Language("eng")}, pool,
        on_outcome=lambda outcome, elapsed: outcomes.append(outcome),
    )
    assert len(outcomes) == 1
    assert outcomes[0].status == expected
    if kind == "throttle":
        assert outcomes[0].retry_after == 120


def test_list_all_subtitles_parallel_as_completed_short_circuits_slow():
    """A slow provider must not starve the wall-timeout."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    fast_subs = [MagicMock(provider_name="fast", language=MagicMock())]
    slow_subs = [MagicMock(provider_name="slow", language=MagicMock())]

    def list_fn(provider, video, languages):
        if provider == "slow":
            time.sleep(2.5)  # exceeds per-provider timeout and wall timeout
        return slow_subs if provider == "slow" else fast_subs

    pool.providers = ["fast", "slow"]
    pool.discarded_providers = set()
    pool.list_subtitles_provider.side_effect = list_fn

    video = MagicMock()
    t0 = time.time()
    results = list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1,
        wall_timeout=2,
    )
    elapsed = time.time() - t0
    assert elapsed < 3, "wall timeout must kick in"
    # "fast" provider's subtitles present
    assert any(getattr(s, "provider_name", "") == "fast" for s in results[video])


def test_wall_timeout_strictly_honored_even_with_many_slow():
    """With 5 providers all sleeping 5s, wall=1 must return in ~1s."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["p1", "p2", "p3", "p4", "p5"]
    pool.discarded_providers = set()

    def list_fn(provider, video, languages):
        time.sleep(5)
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    video = MagicMock()
    t0 = time.time()
    list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1, wall_timeout=1,
    )
    elapsed = time.time() - t0
    # Must be well under the 5s-per-provider wait; tolerate a bit of
    # shutdown overhead.
    assert elapsed < 2.5, f"fanout overran wall: elapsed={elapsed:.2f}s"


def test_on_result_callback_fired_for_every_provider():
    """All three outcomes (ok/slow/abandoned) reach the callback."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["fast", "slow_success", "abandoned"]
    pool.discarded_providers = set()

    fast_sub = MagicMock(provider_name="fast", language=MagicMock())
    slow_sub = MagicMock(provider_name="slow_success", language=MagicMock())

    def list_fn(provider, video, languages):
        if provider == "fast":
            return [fast_sub]
        if provider == "slow_success":
            time.sleep(1.2)  # > per_provider_timeout=1 but < wall=3
            return [slow_sub]
        time.sleep(10)  # abandoned
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    calls = []
    def on_result(name, outcome, latency_ms):
        calls.append((name, outcome, latency_ms))

    video = MagicMock()
    list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1, wall_timeout=3,
        on_result=on_result,
    )
    outcomes = {name: outcome for name, outcome, _ in calls}
    assert outcomes.get("fast") == "ok"
    assert outcomes.get("slow_success") == "slow"
    assert outcomes.get("abandoned") == "abandoned"
    # Latency is non-negative and plausible.
    for _, _, latency_ms in calls:
        assert latency_ms >= 0


def test_provider_exception_recorded_as_exception():
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["boom", "ok"]
    pool.discarded_providers = set()
    ok_sub = MagicMock(provider_name="ok", language=MagicMock())

    def list_fn(provider, video, languages):
        if provider == "boom":
            raise RuntimeError("provider exploded")
        return [ok_sub]

    pool.list_subtitles_provider.side_effect = list_fn

    calls = {}
    def on_result(name, outcome, latency_ms):
        calls[name] = outcome

    video = MagicMock()
    results = list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=5, wall_timeout=10,
        on_result=on_result,
    )
    assert calls["boom"] == "exception"
    assert calls["ok"] == "ok"
    assert any(getattr(s, "provider_name", "") == "ok" for s in results[video])


def test_excluded_providers_are_skipped_entirely():
    """Excluded providers must not be submitted or reported."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["keep", "skip"]
    pool.discarded_providers = set()

    invocations = []

    def list_fn(provider, video, languages):
        invocations.append(provider)
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    calls = {}
    def on_result(name, outcome, latency_ms):
        calls[name] = outcome

    video = MagicMock()
    list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=2, wall_timeout=3,
        exclude_providers={"skip"},
        on_result=on_result,
    )
    assert invocations == ["keep"]
    assert "skip" not in calls
    assert calls["keep"] == "ok"


def test_async_pool_tuple_shape_handled():
    """SZAsyncProviderPool returns (name, subs). Unpack without error."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["p"]
    pool.discarded_providers = set()
    sub = MagicMock(provider_name="p", language=MagicMock())
    pool.list_subtitles_provider.return_value = ("p", [sub])

    video = MagicMock()
    results = list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1, wall_timeout=2,
    )
    assert results[video] == [sub]


def test_pool_discarded_providers_respected():
    """Providers in pool.discarded_providers are skipped before submission."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["discarded", "live"]
    pool.discarded_providers = {"discarded"}
    live_sub = MagicMock(provider_name="live", language=MagicMock())

    invocations = []

    def list_fn(provider, video, languages):
        invocations.append(provider)
        return [live_sub]

    pool.list_subtitles_provider.side_effect = list_fn

    video = MagicMock()
    list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1, wall_timeout=2,
    )
    assert invocations == ["live"]


def test_empty_providers_returns_empty_result():
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = []
    pool.discarded_providers = set()

    video = MagicMock()
    results = list_all_subtitles_parallel(
        [video], set(), pool,
        per_provider_timeout=1, wall_timeout=1,
    )
    assert dict(results) == {}


def test_empty_videos_returns_empty_result():
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    pool.providers = ["a", "b"]
    pool.discarded_providers = set()

    results = list_all_subtitles_parallel(
        [], set(), pool,
        per_provider_timeout=1, wall_timeout=1,
    )
    assert dict(results) == {}
    pool.list_subtitles_provider.assert_not_called()


def test_detailed_saturation_reports_each_provider_without_search(monkeypatch):
    from types import SimpleNamespace
    from subliminal.video import Movie
    from subliminal_patch import core_persistent as fanout

    class Saturated:
        def acquire(self, timeout):
            return False

    monkeypatch.setattr(fanout, "_get_pool", lambda: (None, Saturated()))
    pool = SimpleNamespace(providers=["first", "second"], discarded_providers=set())
    outcomes = []
    result = fanout.list_all_subtitles_parallel(
        [Movie("", "A title")], set(), pool, wall_timeout=0.01,
        on_outcome=lambda outcome, elapsed: outcomes.append(outcome),
    )
    assert dict(result) == {}
    assert [(outcome.provider, outcome.status) for outcome in outcomes] == [
        ("first", "saturated"), ("second", "saturated")]


def test_detailed_wall_timeout_does_not_accept_late_provider_rows(monkeypatch):
    from threading import Event
    from provider_hub.registry import HubProxyProvider
    from subliminal.video import Movie
    from subliminal_patch.core import SZAsyncProviderPool
    from subliminal_patch.core_persistent import list_all_subtitles_parallel
    from subliminal_patch.extensions import provider_registry
    from subzero.language import Language

    release, finished = Event(), Event()

    class SlowProvider(HubProxyProvider):
        languages = {Language("eng")}

        def list_subtitles(self, video, languages):
            release.wait(2)
            finished.set()
            return []

    monkeypatch.setitem(provider_registry.providers, "discover_slow", SlowProvider)
    pool = SZAsyncProviderPool(providers=["discover_slow"], provider_configs={})
    outcomes = []
    try:
        result = list_all_subtitles_parallel(
            [Movie("", "The Matrix")], {Language("eng")}, pool, wall_timeout=0.05,
            on_outcome=lambda outcome, elapsed: outcomes.append(outcome),
        )
        assert [(outcome.provider, outcome.status) for outcome in outcomes] == [("discover_slow", "timeout")]
        release.set()
        assert finished.wait(2)
        assert dict(result) == {}
        assert len(outcomes) == 1
    finally:
        release.set()

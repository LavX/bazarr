"""What the fanout is allowed to call a provider when the wall fires.

The compat wall is one budget for the whole fanout, not a per-provider one, and
the provider count routinely exceeds the worker count. Every future left over
when the wall fired used to be reported as ``status="timeout"``, which put
"Provider search timed out" on screen for providers that had never been sent a
single request, and fed the health tracker a failure for each of them. A
provider measured completing its whole search in a few seconds was reported as
having timed out, because the search it was queued behind had not.

So the three outcomes are kept apart here:

    timeout      the provider itself blew a deadline it owns
    abandoned    the provider was working when the shared wall fired
    not_started  the call never reached a worker; no request was made
"""
import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_fanout_pool():
    from subliminal_patch.core_persistent import reset_pool
    reset_pool()
    yield
    reset_pool()


def _starved_fanout(monkeypatch, workers=4, providers=8):
    """Run one fanout with more providers than worker slots and collect
    everything it reported. Returns (outcomes, health_calls, started)."""
    from app.config import settings
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel, reset_pool,
    )

    monkeypatch.setattr(settings.compat_endpoint, "fanout_max_workers", workers, raising=False)
    monkeypatch.setattr(settings.compat_endpoint, "max_concurrent_fanouts", 1, raising=False)
    reset_pool()

    pool = MagicMock()
    pool.providers = [f"p{index}" for index in range(providers)]
    pool.discarded_providers = set()

    started, started_lock = [], threading.Lock()

    def list_fn(provider, video, languages, **kwargs):
        with started_lock:
            started.append(provider)
        time.sleep(2.0)  # still running when the wall fires
        return []

    pool.list_subtitles_provider.side_effect = list_fn
    outcomes, health_calls = [], []
    list_all_subtitles_parallel(
        [MagicMock()], set(), pool,
        per_provider_timeout=1, wall_timeout=1,
        on_result=lambda name, outcome, latency_ms: health_calls.append((name, outcome)),
        on_outcome=lambda outcome, elapsed: outcomes.append(outcome),
    )
    with started_lock:
        return outcomes, health_calls, list(started)


def test_queued_provider_that_never_ran_is_not_reported_as_a_timeout(monkeypatch):
    outcomes, health_calls, started = _starved_fanout(monkeypatch)
    by_name = {outcome.provider: outcome for outcome in outcomes}
    # The pool floor is four workers, so eight providers starve exactly four.
    assert len(started) == 4, f"expected the worker cap to starve four providers, started={started}"
    queued = [name for name in by_name if name not in started]
    assert len(queued) == 4

    for name in queued:
        assert (by_name[name].status, by_name[name].reason) == ("not_started", "fanout_capacity")
    # The claim that produced the original bug report: a provider that was
    # never asked anything must never be described as having timed out.
    assert not [outcome for outcome in outcomes if outcome.status == "timeout"]
    # And it must not be counted against the provider's health, or the
    # providers that happen to sort last get discarded for our scheduling.
    assert {outcome for name, outcome in health_calls if name in queued} == {"not_started"}


def test_provider_still_working_at_the_wall_is_abandoned_not_timed_out(monkeypatch):
    outcomes, health_calls, started = _starved_fanout(monkeypatch)
    by_name = {outcome.provider: outcome for outcome in outcomes}
    assert started, "no provider got a worker slot"
    for name in started:
        assert (by_name[name].status, by_name[name].reason) == ("abandoned", "wall_timeout")
    # Abandonment is still a failure for health purposes: the caller got no
    # data. It is the vocabulary that changes, not that judgement.
    assert {outcome for name, outcome in health_calls if name in started} == {"abandoned"}


def test_wall_outcomes_reach_the_log_provider_failures_use(monkeypatch, caplog):
    """The wall path emitted nothing to any log, so an operator comparing the
    screen against the backend log found timeouts that had never been recorded.
    """
    with caplog.at_level("INFO", logger="provider_search"):
        outcomes, _, _ = _starved_fanout(monkeypatch)
    logged = {
        record.getMessage().split("provider=")[1].split(" ")[0]: record.getMessage()
        for record in caplog.records if record.name == "provider_search"
    }
    assert set(logged) == {outcome.provider for outcome in outcomes}
    for outcome in outcomes:
        assert f"status={outcome.status}" in logged[outcome.provider]


@pytest.mark.parametrize("outcome,discarded", [
    ("abandoned", True), ("timeout", True), ("exception", True), ("not_started", False),
])
def test_health_tracker_only_discards_for_calls_that_were_actually_made(outcome, discarded):
    from subliminal_patch.provider_health import (
        FAILURES_TO_DISCARD, get_tracker, reset_tracker,
    )

    reset_tracker()
    try:
        tracker = get_tracker()
        for _ in range(FAILURES_TO_DISCARD * 3):
            tracker.record("example", outcome)
        assert tracker.is_discarded("example") is discarded
    finally:
        reset_tracker()

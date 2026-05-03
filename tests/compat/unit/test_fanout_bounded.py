"""Regression tests for the bounded compat fanout executor.

The previous implementation created a fresh ThreadPoolExecutor per
request and on wall timeout called shutdown(wait=False, cancel_futures=
True). cancel_futures only cancels QUEUED tasks, leaving running
provider threads alive in an abandoned executor. Under repeated timed-
out searches this would accumulate background threads without bound.

These tests pin the new contract:
  - one process-wide executor, sized by config (with a defensive default)
  - a concurrency semaphore bounds simultaneous fanouts
  - abandoned futures are observable via fanout_stats() and reaped on
    completion
  - thread count returns to baseline after timed-out fanouts complete
"""
import threading
import time
from unittest.mock import MagicMock

import pytest


def _baseline_compat_threads() -> int:
    """Count live threads whose name starts with the compat-fanout prefix."""
    return sum(1 for t in threading.enumerate()
               if t.is_alive() and t.name.startswith("compat-fanout"))


@pytest.fixture(autouse=True)
def _reset_fanout_pool():
    """Each test starts and ends with a fresh pool so other tests are
    isolated and the abandoned counter is accurate per-test."""
    from subliminal_patch.core_persistent import reset_pool
    reset_pool()
    yield
    reset_pool()


def test_max_workers_cap_respected_under_repeated_timeouts():
    """Even with many fanouts that all abandon their workers, the live
    compat-fanout thread count must not exceed max_workers."""
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel, fanout_stats,
    )

    pool = MagicMock()
    pool.providers = ["p1", "p2", "p3", "p4"]
    pool.discarded_providers = set()

    def list_fn(provider, video, languages):
        time.sleep(2.5)  # exceeds wall, will be abandoned
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    # Drive 10 fanouts against the same shared pool. With 4 providers
    # each, that's 40 logical submissions. The shared pool's max_workers
    # default is 32, but the concurrency semaphore caps simultaneous
    # fanouts at 4 by default - so at most 4 * 4 = 16 threads alive at
    # peak, well under the 32 cap.
    def _drive():
        v = MagicMock()
        list_all_subtitles_parallel(
            [v], set(), pool,
            per_provider_timeout=1, wall_timeout=1,
        )

    drivers = [threading.Thread(target=_drive) for _ in range(10)]
    for d in drivers:
        d.start()
    for d in drivers:
        d.join(timeout=15)

    stats = fanout_stats()
    live = _baseline_compat_threads()
    # Hard ceiling: never more than max_workers compat-fanout threads,
    # regardless of how many fanouts were issued.
    assert live <= stats["max_workers"], (
        f"compat-fanout threads={live} exceeded max_workers="
        f"{stats['max_workers']}"
    )


def test_threads_return_to_baseline_after_workers_finish():
    """Once provider workers return, all abandoned threads should be
    reusable / reaped, not leaking forever."""
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel, fanout_stats,
    )

    pool = MagicMock()
    pool.providers = ["a", "b", "c"]
    pool.discarded_providers = set()

    def list_fn(provider, video, languages):
        # Long enough to be abandoned by wall=1, short enough that we
        # can wait it out and observe completion within the test.
        time.sleep(2.0)
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    v = MagicMock()
    list_all_subtitles_parallel(
        [v], set(), pool,
        per_provider_timeout=1, wall_timeout=1,
    )

    # Right after the wall fires, abandoned futures should still be in
    # the registry (workers still sleeping).
    immediate = fanout_stats()
    assert immediate["abandoned_pending"] >= 1, (
        f"expected at least one abandoned future, got {immediate}"
    )

    # Wait long enough for the sleeping provider workers to return.
    time.sleep(2.5)

    # Trigger a reap by calling fanout_stats (which calls _reap_abandoned).
    after = fanout_stats()
    assert after["abandoned_pending"] == 0, (
        f"abandoned futures did not drain after workers returned: {after}"
    )
    assert after["abandoned_total"] >= immediate["abandoned_pending"], (
        "all-time abandoned counter must be monotonic"
    )


def test_concurrent_fanout_cap_blocks_extra_callers():
    """When more fanouts arrive than max_concurrent_fanouts, extras
    must wait or degrade rather than spawn unbounded background work."""
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel,
    )
    from app.config import settings

    cap = int(settings.compat_endpoint.max_concurrent_fanouts)

    enter = threading.Event()  # noqa: F841
    enter_count = threading.Semaphore(0)
    release = threading.Event()
    in_flight = []
    in_flight_lock = threading.Lock()

    def list_fn(provider, video, languages):
        with in_flight_lock:
            in_flight.append(1)
        enter_count.release()
        # Block until the test releases us.
        release.wait(timeout=10)
        with in_flight_lock:
            in_flight.pop()
        return []

    pool = MagicMock()
    pool.providers = ["only"]
    pool.discarded_providers = set()
    pool.list_subtitles_provider.side_effect = list_fn

    def _drive():
        v = MagicMock()
        list_all_subtitles_parallel(
            [v], set(), pool,
            per_provider_timeout=10, wall_timeout=10,
        )

    drivers = [threading.Thread(target=_drive) for _ in range(cap + 3)]
    for d in drivers:
        d.start()

    # Wait until cap workers have entered list_fn.
    deadline = time.monotonic() + 5.0
    entered = 0
    while entered < cap and time.monotonic() < deadline:
        if enter_count.acquire(timeout=0.2):
            entered += 1
    assert entered == cap, (
        f"expected {cap} concurrent workers, got {entered}"
    )

    # The remaining drivers must still be blocked on the semaphore.
    # No more workers should have entered list_fn.
    time.sleep(0.5)
    with in_flight_lock:
        live_workers = len(in_flight)
    assert live_workers == cap, (
        f"concurrency cap broken: in_flight={live_workers} cap={cap}"
    )

    # Release everything and let the threads drain.
    release.set()
    for d in drivers:
        d.join(timeout=15)


def test_queued_futures_cancelled_after_wall_timeout(monkeypatch):
    """When the shared pool is saturated, providers that never got
    picked up by a worker must be cancelled, not left to run after the
    caller returned. This guards against unbounded queue growth under
    repeated timed-out searches when provider count exceeds workers."""
    from app.config import settings
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel, reset_pool,
    )

    # Force a tiny pool so queueing is guaranteed.
    monkeypatch.setattr(
        settings.compat_endpoint, "fanout_max_workers", 4,
        raising=False,
    )
    monkeypatch.setattr(
        settings.compat_endpoint, "max_concurrent_fanouts", 1,
        raising=False,
    )
    reset_pool()

    pool = MagicMock()
    # 8 providers but only 4 worker slots -> 4 queued.
    pool.providers = [f"p{i}" for i in range(8)]
    pool.discarded_providers = set()

    started_count = [0]
    started_lock = threading.Lock()

    def list_fn(provider, video, languages):
        with started_lock:
            started_count[0] += 1
        time.sleep(2.0)  # exceeds wall, will be abandoned
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    v = MagicMock()
    list_all_subtitles_parallel(
        [v], set(), pool,
        per_provider_timeout=1, wall_timeout=1,
    )

    # Right after the wall fires, the 4 running providers haven't
    # returned (they sleep 2s) but the 4 queued ones must have been
    # cancelled and never invoked.
    assert started_count[0] == 4, (
        f"expected exactly 4 providers to start (max_workers cap), "
        f"got {started_count[0]}"
    )

    # Wait for the running providers to finish.
    time.sleep(2.5)

    # Even after the running providers complete, the cancelled queued
    # providers must NOT have run. Total invocations should still be 4.
    assert started_count[0] == 4, (
        f"queued providers ran after the caller returned: "
        f"started={started_count[0]} (expected 4)"
    )


def test_wall_timeout_includes_semaphore_wait(monkeypatch):
    """The wall timeout is a hard total budget. Time spent waiting for
    the concurrency semaphore must count toward it, so a saturated cap
    cannot cause the request to spend nearly 2 * wall_timeout."""
    from app.config import settings
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel, reset_pool,
    )

    monkeypatch.setattr(
        settings.compat_endpoint, "max_concurrent_fanouts", 1,
        raising=False,
    )
    reset_pool()

    pool = MagicMock()
    pool.providers = ["only"]
    pool.discarded_providers = set()

    holder_release = threading.Event()

    def list_fn(provider, video, languages):
        holder_release.wait(timeout=10)
        return []

    pool.list_subtitles_provider.side_effect = list_fn

    # Holder fanout: takes the only slot for the whole wall budget.
    def _holder():
        v = MagicMock()
        list_all_subtitles_parallel(
            [v], set(), pool,
            per_provider_timeout=10, wall_timeout=2,
        )

    holder = threading.Thread(target=_holder)
    holder.start()
    # Give the holder time to acquire the semaphore.
    time.sleep(0.3)

    # Second caller arrives while holder owns the slot. Its wall is 2s.
    # Total elapsed must NOT approach 4s (sem wait + new wall).
    pool2 = MagicMock()
    pool2.providers = ["fast"]
    pool2.discarded_providers = set()
    fast_sub = MagicMock(provider_name="fast", language=MagicMock())

    def fast_fn(provider, video, languages):
        return [fast_sub]

    pool2.list_subtitles_provider.side_effect = fast_fn

    t0 = time.monotonic()
    v2 = MagicMock()
    list_all_subtitles_parallel(
        [v2], set(), pool2,
        per_provider_timeout=1, wall_timeout=2,
    )
    elapsed = time.monotonic() - t0

    # Cleanup: release holder.
    holder_release.set()
    holder.join(timeout=10)

    # Must respect wall_timeout as a total budget, with a small grace
    # for thread-pool overhead. Pre-fix this would be ~4s.
    assert elapsed < 3.0, (
        f"wall_timeout contract broken: elapsed={elapsed:.2f}s "
        f"(expected < 3s for wall=2s including sem wait)"
    )


def test_concurrent_reset_does_not_break_in_flight_fanout(monkeypatch):
    """A reset_pool() call (e.g. config save) racing with an in-flight
    fanout's submit loop must not turn the search into a 500.
    _safe_submit transparently swaps to a fresh pool on RuntimeError."""
    from subliminal_patch.core_persistent import (
        list_all_subtitles_parallel, reset_pool, _get_pool,
    )

    pool = MagicMock()
    pool.providers = ["a", "b", "c", "d"]
    pool.discarded_providers = set()
    fast_sub = MagicMock(provider_name="ok", language=MagicMock())

    submit_count = [0]
    submit_lock = threading.Lock()
    reset_done = threading.Event()

    def list_fn(provider, video, languages):
        with submit_lock:
            submit_count[0] += 1
        return [fast_sub]

    pool.list_subtitles_provider.side_effect = list_fn

    # Force the in-flight fanout to observe a reset midway through
    # submitting providers by patching submit on the captured executor
    # to call reset_pool() the first time it's invoked.
    real_executor, _ = _get_pool()
    original_submit = real_executor.submit
    submit_calls = [0]

    def racy_submit(*args, **kwargs):
        submit_calls[0] += 1
        if submit_calls[0] == 2:
            # Second submit: simulate a config save interleaving here.
            reset_pool()
            reset_done.set()
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(real_executor, "submit", racy_submit)

    v = MagicMock()
    # Should NOT raise. Some providers may finish on the old executor,
    # the rest on the fresh one. Either way, the fanout must complete
    # and return collected results, not a RuntimeError.
    results = list_all_subtitles_parallel(
        [v], set(), pool,
        per_provider_timeout=2, wall_timeout=3,
    )
    assert reset_done.is_set(), "reset_pool was supposed to fire mid-fanout"
    flat = [s for subs in results.values() for s in subs]
    assert any(getattr(s, "provider_name", "") == "ok" for s in flat), (
        "expected at least one provider's results despite mid-fanout reset"
    )


def test_existing_short_circuit_behavior_preserved():
    """The original test_fanout.py's contract still holds with the
    shared-pool implementation: a slow provider must not starve the
    wall, and the fast provider's results come back."""
    from subliminal_patch.core_persistent import list_all_subtitles_parallel

    pool = MagicMock()
    fast_subs = [MagicMock(provider_name="fast", language=MagicMock())]
    slow_subs = [MagicMock(provider_name="slow", language=MagicMock())]

    def list_fn(provider, video, languages):
        if provider == "slow":
            time.sleep(2.5)
        return slow_subs if provider == "slow" else fast_subs

    pool.providers = ["fast", "slow"]
    pool.discarded_providers = set()
    pool.list_subtitles_provider.side_effect = list_fn

    v = MagicMock()
    t0 = time.time()
    results = list_all_subtitles_parallel(
        [v], set(), pool,
        per_provider_timeout=1, wall_timeout=2,
    )
    elapsed = time.time() - t0
    assert elapsed < 3
    assert any(getattr(s, "provider_name", "") == "fast" for s in results[v])

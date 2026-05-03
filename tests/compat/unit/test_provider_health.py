"""Provider-health auto-discard behavior.

Covers the timeout+retry discard policy in subliminal_patch.provider_health.
"""
import time  # noqa: F401

import pytest


@pytest.fixture(autouse=True)
def _fresh_tracker():
    from subliminal_patch import provider_health
    provider_health.reset_tracker()
    yield
    provider_health.reset_tracker()


def test_ok_leaves_provider_active():
    from subliminal_patch.provider_health import get_tracker
    t = get_tracker()
    for _ in range(10):
        t.record("os", "ok", 100)
    assert not t.is_discarded("os")
    assert "os" not in t.currently_discarded()


def test_exception_below_threshold_does_not_discard():
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD - 1):
        t.record("os", "exception")
    assert not t.is_discarded("os")


def test_exception_at_threshold_discards():
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD):
        t.record("os", "exception")
    assert t.is_discarded("os")
    assert "os" in t.currently_discarded()


def test_abandoned_counts_same_as_exception():
    """Wall-timeout abandonment is a user-visible failure; policy treats
    it the same as a raised exception to avoid one stuck provider dragging
    every request to the wall."""
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD):
        t.record("slow_provider", "abandoned")
    assert t.is_discarded("slow_provider")


def test_timeout_and_abandoned_mix():
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    outcomes = ["timeout", "abandoned", "exception"][:FAILURES_TO_DISCARD]
    for o in outcomes:
        t.record("flaky", o)
    assert t.is_discarded("flaky")


def test_ok_resets_failure_counter():
    """A single success fully rehabilitates the provider."""
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD - 1):
        t.record("os", "exception")
    t.record("os", "ok", 300)
    for _ in range(FAILURES_TO_DISCARD - 1):
        t.record("os", "exception")
    assert not t.is_discarded("os")


def test_slow_outcome_also_resets():
    """A slow-but-successful response is still a success for health."""
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD - 1):
        t.record("addic7ed", "timeout")
    t.record("addic7ed", "slow", 6000)
    t.record("addic7ed", "timeout")
    assert not t.is_discarded("addic7ed")


def test_cooldown_expires_allows_retry(monkeypatch):
    """After the cooldown window, the provider is given another chance."""
    from subliminal_patch import provider_health
    from subliminal_patch.provider_health import get_tracker
    t = get_tracker()

    now = [1_000_000.0]
    monkeypatch.setattr(provider_health.time, "monotonic", lambda: now[0])

    for _ in range(provider_health.FAILURES_TO_DISCARD):
        t.record("flaky", "exception")
    assert t.is_discarded("flaky")

    # Jump past the base cooldown.
    now[0] += provider_health.COOLDOWN_BASE_SECONDS + 1
    assert not t.is_discarded("flaky")


def test_exponential_backoff(monkeypatch):
    """Re-discards apply doubled cooldowns until the cap."""
    from subliminal_patch import provider_health
    from subliminal_patch.provider_health import get_tracker
    t = get_tracker()
    now = [1_000_000.0]
    monkeypatch.setattr(provider_health.time, "monotonic", lambda: now[0])

    # First discard -> base cooldown
    for _ in range(provider_health.FAILURES_TO_DISCARD):
        t.record("p", "exception")
    snap = t.snapshot()["p"]
    assert snap["discarded"]
    assert snap["discarded_for_seconds"] == provider_health.COOLDOWN_BASE_SECONDS

    # Advance past cooldown and fail again -> level 2 (2x base)
    now[0] += provider_health.COOLDOWN_BASE_SECONDS + 1
    for _ in range(provider_health.FAILURES_TO_DISCARD):
        t.record("p", "exception")
    snap = t.snapshot()["p"]
    assert snap["discarded_for_seconds"] == 2 * provider_health.COOLDOWN_BASE_SECONDS

    # Advance again -> level 3 (4x base)
    now[0] += 2 * provider_health.COOLDOWN_BASE_SECONDS + 1
    for _ in range(provider_health.FAILURES_TO_DISCARD):
        t.record("p", "exception")
    snap = t.snapshot()["p"]
    assert snap["discarded_for_seconds"] == min(
        4 * provider_health.COOLDOWN_BASE_SECONDS,
        provider_health.COOLDOWN_MAX_SECONDS,
    )


def test_backoff_capped_at_max(monkeypatch):
    """Backoff never exceeds COOLDOWN_MAX_SECONDS even after many cycles."""
    from subliminal_patch import provider_health
    from subliminal_patch.provider_health import get_tracker
    t = get_tracker()
    now = [1_000_000.0]
    monkeypatch.setattr(provider_health.time, "monotonic", lambda: now[0])

    for _ in range(20):
        for _ in range(provider_health.FAILURES_TO_DISCARD):
            t.record("p", "exception")
        snap = t.snapshot()["p"]
        assert snap["discarded_for_seconds"] <= provider_health.COOLDOWN_MAX_SECONDS
        now[0] += provider_health.COOLDOWN_MAX_SECONDS + 1


def test_unknown_outcome_ignored():
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD):
        t.record("p", "weird_unknown_outcome")
    assert not t.is_discarded("p")


def test_currently_discarded_snapshot():
    from subliminal_patch.provider_health import get_tracker, FAILURES_TO_DISCARD
    t = get_tracker()
    for _ in range(FAILURES_TO_DISCARD):
        t.record("a", "exception")
    for _ in range(FAILURES_TO_DISCARD):
        t.record("b", "timeout")
    t.record("c", "ok")

    active = t.currently_discarded()
    assert active == {"a", "b"}

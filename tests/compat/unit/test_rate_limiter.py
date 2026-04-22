import time
from bazarr.compat import rate_limiter as R


def test_first_consume_returns_allowed():
    R.reset()
    allowed, remaining, reset = R.try_consume("user-1", limit=3, window_seconds=60)
    assert allowed is True
    assert remaining == 2
    assert reset > int(time.time())


def test_exhausts_limit_and_reports_remaining():
    R.reset()
    for _ in range(3):
        allowed, _, _ = R.try_consume("u", 3, 60)
        assert allowed is True
    allowed, remaining, _ = R.try_consume("u", 3, 60)
    assert allowed is False
    assert remaining == 0


def test_inspect_does_not_consume():
    R.reset()
    R.try_consume("u", 5, 60)
    remaining, reset = R.inspect("u", 5, 60)
    assert remaining == 4
    # Calling inspect twice leaves count unchanged.
    remaining2, _ = R.inspect("u", 5, 60)
    assert remaining2 == 4


def test_window_resets_after_expiry(monkeypatch):
    R.reset()
    # Exhaust the window.
    for _ in range(2):
        R.try_consume("u", 2, 60)
    allowed, _, _ = R.try_consume("u", 2, 60)
    assert allowed is False
    # Advance past the window.
    real_time = time.time
    monkeypatch.setattr(R.time, "time", lambda: real_time() + 61)
    allowed, remaining, _ = R.try_consume("u", 2, 60)
    assert allowed is True
    assert remaining == 1


def test_distinct_keys_have_independent_quotas():
    R.reset()
    R.try_consume("a", 1, 60)
    allowed_a, _, _ = R.try_consume("a", 1, 60)
    allowed_b, _, _ = R.try_consume("b", 1, 60)
    assert allowed_a is False
    assert allowed_b is True


def test_inspect_on_unknown_key_returns_full_limit():
    R.reset()
    remaining, reset = R.inspect("new", 10, 60)
    assert remaining == 10
    assert reset > int(time.time())

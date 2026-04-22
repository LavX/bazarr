import time
from bazarr.compat import jwt_denylist as D


def test_fresh_jti_is_not_revoked():
    D.reset()
    assert D.is_revoked("jti-a") is False


def test_revoked_jti_reports_revoked():
    D.reset()
    D.revoke("jti-a", exp=int(time.time()) + 60)
    assert D.is_revoked("jti-a") is True


def test_expired_entries_are_pruned_on_add():
    D.reset()
    # Insert an already-expired jti via the underlying map.
    past = int(time.time()) - 10
    D.revoke("stale", exp=past)
    # Adding a new entry triggers pruning.
    D.revoke("fresh", exp=int(time.time()) + 60)
    assert D.is_revoked("stale") is False
    assert D.is_revoked("fresh") is True


def test_is_revoked_returns_false_for_expired_even_before_prune():
    D.reset()
    past = int(time.time()) - 10
    D.revoke("stale", exp=past)
    assert D.is_revoked("stale") is False


def test_reset_clears_all_entries():
    D.revoke("a", int(time.time()) + 60)
    D.reset()
    assert D.is_revoked("a") is False

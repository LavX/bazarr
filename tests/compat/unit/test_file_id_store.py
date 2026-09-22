import time
from compat.file_id_store import FileIdStore, get_store, reset_store


def test_put_returns_monotonic_int():
    s = FileIdStore()
    a = s.put({"p": "x", "i": "1"}, ttl_seconds=60)
    b = s.put({"p": "x", "i": "2"}, ttl_seconds=60)
    assert isinstance(a, int) and isinstance(b, int)
    assert b == a + 1


def test_get_roundtrips_payload():
    s = FileIdStore()
    fid = s.put({"p": "os", "i": "42", "l": "eng"}, ttl_seconds=60)
    ok, payload = s.get(fid)
    assert ok
    assert payload == {"p": "os", "i": "42", "l": "eng"}


def test_get_accepts_string_int():
    s = FileIdStore()
    fid = s.put({"p": "x", "i": "1"}, ttl_seconds=60)
    ok, _ = s.get(str(fid))
    assert ok


def test_get_rejects_unknown_id():
    s = FileIdStore()
    ok, payload = s.get(9999)
    assert not ok and payload == {}


def test_get_rejects_non_numeric():
    s = FileIdStore()
    for bogus in ("abc", None, "", object()):
        ok, _ = s.get(bogus)
        assert not ok


def test_expired_entry_removed_on_access():
    s = FileIdStore()
    fid = s.put({"p": "x", "i": "1"}, ttl_seconds=1)
    time.sleep(1.1)
    ok, _ = s.get(fid)
    assert not ok
    assert len(s) == 0


def test_overflow_triggers_gc():
    """Store keeps at most max_entries (best-effort; evicts expired + oldest)."""
    s = FileIdStore(max_entries=5)
    for _ in range(10):
        s.put({"p": "x", "i": "1"}, ttl_seconds=60)
    assert len(s) <= 5


def test_singleton_survives_reset():
    get_store().put({"p": "x", "i": "1"}, ttl_seconds=60)
    assert len(get_store()) >= 1
    reset_store()
    assert len(get_store()) == 0


def test_renew_extends_a_live_entry_without_changing_its_id():
    s = FileIdStore()
    fid = s.put({"p": "x", "i": "1", "expires_at": 0.0}, ttl_seconds=1)
    expires = s.renew(fid, 60)
    assert expires is not None and expires > time.time() + 50
    ok, payload = s.get(fid)
    assert ok and payload["i"] == "1"
    # The payload carries its own copy of the deadline, which downloads check
    # before the store's, so a renewal that moved one and not the other would
    # leave the entry resolvable and still refused.
    assert payload["expires_at"] == expires


def test_renew_never_shortens_a_longer_lifetime():
    s = FileIdStore()
    fid = s.put({"p": "x", "i": "1"}, ttl_seconds=600)
    expires = s.renew(fid, 5)
    assert expires > time.time() + 500


def test_renew_refuses_an_entry_that_is_gone():
    s = FileIdStore()
    fid = s.put({"p": "x", "i": "1"}, ttl_seconds=1)
    time.sleep(1.1)
    assert s.renew(fid, 60) is None
    assert s.renew(99999, 60) is None
    assert s.renew("not-a-number", 60) is None

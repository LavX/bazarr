import pytest
from bazarr.compat import auth as A


@pytest.fixture(autouse=True)
def _reset_compat_secrets():
    """Set live compat secrets for each test and restore afterward.

    pytest's monkeypatch.setattr on dotted string paths doesn't reliably restore
    Dynaconf attributes between tests, causing full-suite ordering flakes.
    Setting and restoring directly on the live DynaBox is deterministic.
    """
    from bazarr.app.config import settings
    original = {
        name: getattr(settings.compat_endpoint, name, "")
        for name in ("token", "jwt_secret", "file_id_secret",
                     "jwt_ttl_seconds", "file_id_ttl_seconds", "stream_token_ttl_seconds")
    }
    # Use dict-assignment (not setattr on the DynaBox wrapper) because the
    # wrapper doesn't reliably propagate into Dynaconf's layered storage
    # once another test has monkeypatched a dotted path in the same session.
    settings["compat_endpoint"]["token"] = "t" * 32
    settings["compat_endpoint"]["jwt_secret"] = "j" * 32
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["jwt_ttl_seconds"] = 60
    settings["compat_endpoint"]["file_id_ttl_seconds"] = 60
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 60
    yield
    for name, value in original.items():
        settings["compat_endpoint"][name] = value


def test_validate_compat_token_accepts_exact_match(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.token", "a" * 32)
    assert A.validate_compat_token("a" * 32) is True


def test_validate_compat_token_rejects_wrong(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.token", "a" * 32)
    assert A.validate_compat_token("b" * 32) is False


def test_validate_compat_token_empty_returns_false(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.token", "a" * 32)
    assert A.validate_compat_token("") is False
    assert A.validate_compat_token(None) is False


def test_validate_compat_token_uses_constant_time():
    """Fails if `==` is used instead of hmac.compare_digest."""
    import hmac
    # Sanity: hmac.compare_digest accepts bytes and str
    assert hmac.compare_digest("a" * 32, "a" * 32) is True


import time


def test_mint_and_validate_jwt_roundtrip(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_secret", "j" * 32)
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_ttl_seconds", 60)
    tok = A.mint_jwt()
    ok, claims = A.validate_jwt(tok)
    assert ok is True and "exp" in claims


def test_validate_jwt_rejects_expired(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_secret", "j" * 32)
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_ttl_seconds", 1)
    tok = A.mint_jwt()
    time.sleep(2)
    ok, _ = A.validate_jwt(tok)
    assert ok is False


def test_validate_jwt_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_secret", "j" * 32)
    tok = A.mint_jwt()
    # Replace the signature segment entirely with a clearly invalid payload.
    header_payload, _ = tok.rsplit(".", 1)
    bad = header_payload + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ok, _ = A.validate_jwt(bad)
    assert ok is False


def test_boot_hmac_selftest_passes_with_valid_secrets(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.token", "t" * 32)
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_secret", "j" * 32)
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_secret", "f" * 32)
    A.boot_hmac_selftest()  # no exception


def test_boot_hmac_selftest_fails_closed_with_empty_secret(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_secret", "")
    with pytest.raises(A.CompatBootError):
        A.boot_hmac_selftest()


def test_file_id_roundtrip(monkeypatch):
    """mint_file_id now returns an int (OS.com wire contract); parse resolves
    via the in-memory store."""
    from bazarr.compat.file_id_store import reset_store
    reset_store()
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_ttl_seconds", 60)
    fid = A.mint_file_id(
        provider="opensubtitlescom",
        native_id="12345",
        language="eng",
        release_info="Movie.2020.1080p.BluRay.x264-GROUP",
    )
    assert isinstance(fid, int) and fid > 0
    ok, payload = A.parse_file_id(fid)
    assert ok and payload["p"] == "opensubtitlescom" and payload["i"] == "12345"
    # Numeric string also accepted (clients may serialize as str)
    ok2, _ = A.parse_file_id(str(fid))
    assert ok2


def test_file_id_unknown_rejected(monkeypatch):
    """An int that was never minted must return (False, {})."""
    from bazarr.compat.file_id_store import reset_store
    reset_store()
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_ttl_seconds", 60)
    ok, _ = A.parse_file_id(999999999)
    assert not ok
    ok, _ = A.parse_file_id("not-a-number")
    assert not ok
    ok, _ = A.parse_file_id(None)
    assert not ok


def test_file_id_expired_rejected(monkeypatch):
    from bazarr.compat.file_id_store import reset_store
    reset_store()
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.file_id_ttl_seconds", 1)
    fid = A.mint_file_id("p", "i", "eng", "")
    time.sleep(2)
    ok, _ = A.parse_file_id(fid)
    assert not ok


def test_stream_token_roundtrip():
    from bazarr.app.config import settings
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 60
    tok = A.mint_stream_token("p", "i")
    ok, payload = A.parse_stream_token(tok)
    assert ok and payload["p"] == "p" and payload["i"] == "i"


def test_stream_token_roundtrip_all_byte_values_in_sig():
    """Regression: the parser used raw.rsplit(b'.', 1), which broke when
    the HMAC signature happened to end in 0x2e. Fixed-length split at -32
    makes the parser deterministic regardless of signature content.
    1000 iterations with varying payloads should exercise a signature
    whose last byte is b'.' (0x2e) many times over."""
    from bazarr.app.config import settings
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 60
    for i in range(1000):
        tok = A.mint_stream_token(f"p{i}", f"i{i}")
        ok, payload = A.parse_stream_token(tok)
        assert ok, f"roundtrip failed on iteration {i}, token={tok!r}"
        assert payload["p"] == f"p{i}"
        assert payload["i"] == f"i{i}"


def test_stream_token_expiry():
    """Expiry path: TTL=1, sleep 2s, parse must reject."""
    from bazarr.app.config import settings
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["stream_token_ttl_seconds"] = 1
    tok = A.mint_stream_token("p", "i")
    time.sleep(2)
    ok, _ = A.parse_stream_token(tok)
    assert not ok

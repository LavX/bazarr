"""Distribution Hub search protection: keyed auth, search rate limiting (429),
and per-request provider exclusion + timeout pass-through.
"""
import pytest
from flask import Flask

LEGACY_KEY = "t" * 32


@pytest.fixture(autouse=True)
def _secrets():
    from app.config import settings
    settings["compat_endpoint"]["token"] = LEGACY_KEY
    settings["compat_endpoint"]["jwt_secret"] = "j" * 32
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["search_rate_limit_enabled"] = True
    yield


def _app():
    from compat.routes import compat_bp
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    return app


# ---- cache key ----

def test_exclusion_and_timeout_change_cache_key():
    from compat.cache import build_key
    from subzero.language import Language
    langs = [Language.fromietf("en")]
    base = build_key("movie", "tt1", None, None, langs, ["p1", "p2"])
    excl = build_key("movie", "tt1", None, None, langs, ["p1", "p2"],
                     exclude_providers=["p2"])
    to = build_key("movie", "tt1", None, None, langs, ["p1", "p2"],
                   timeout_seconds=7)
    assert base != excl
    assert base != to
    assert excl != to


# ---- keyed auth ----

def test_unknown_key_is_forbidden():
    c = _app().test_client()
    r = c.get("/api/v1/infos/user", headers={"Api-Key": "bzr_nope"})
    assert r.status_code == 403
    assert r.headers["x-reason"] == "auth"


def test_named_key_authorizes_search(compat_db, monkeypatch):
    from compat import keyring
    monkeypatch.setattr("compat.routes.service.search",
                        lambda *a, **k: {"data": []})
    _, token = keyring.create("site", tier="free")
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/subtitles?imdb_id=tt0111161&languages=en",
              headers={"Api-Key": token})
    assert r.status_code == 200


# ---- search rate limit ----

def test_search_throttles_with_429(compat_db, monkeypatch):
    from compat import keyring
    monkeypatch.setattr("compat.routes.service.search",
                        lambda *a, **k: {"data": []})
    _, token = keyring.create("site", tier="free",
                              custom_limits={"search": {"hour": 1, "day": 0,
                                                        "week": 0, "month": 0}})
    keyring.invalidate_cache()
    c = _app().test_client()
    h = {"Api-Key": token}
    assert c.get("/api/v1/subtitles?imdb_id=tt1&languages=en", headers=h).status_code == 200
    r = c.get("/api/v1/subtitles?imdb_id=tt1&languages=en", headers=h)
    assert r.status_code == 429
    assert r.headers.get("x-reason") == "throttled"
    assert r.headers.get("Retry-After")
    assert r.headers.get("X-RateLimit-Limit") == "1"


def test_legacy_key_search_never_throttles(compat_db, monkeypatch):
    monkeypatch.setattr("compat.routes.service.search",
                        lambda *a, **k: {"data": []})
    c = _app().test_client()
    h = {"Api-Key": LEGACY_KEY}
    for _ in range(5):
        assert c.get("/api/v1/subtitles?imdb_id=tt1&languages=en",
                     headers=h).status_code == 200


# ---- exclusion + timeout pass-through ----

def test_request_exclusion_and_timeout_reach_service(compat_db, monkeypatch):
    captured = {}

    def _fake_search(*a, **k):
        captured.update(k)
        return {"data": []}

    monkeypatch.setattr("compat.routes.service.search", _fake_search)
    from compat import keyring
    _, token = keyring.create("site", tier="free")
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/subtitles?imdb_id=tt1&languages=en"
              "&exclude_providers=opensubtitles,subscene&timeout_seconds=10",
              headers={"Api-Key": token})
    assert r.status_code == 200
    assert captured["exclude_providers"] == ["opensubtitles", "subscene"]
    assert captured["timeout_seconds"] == 10


def test_jwt_kid_binds_to_key(compat_db, monkeypatch):
    """A JWT minted for key A must not be replayed with key B's Api-Key."""
    monkeypatch.setattr("compat.routes.service.download",
                        lambda *a, **k: {"link": "x", "remaining_downloads": 1,
                                         "remaining": 1, "reset_time_utc": "z"})
    from compat import keyring, auth
    from unittest.mock import MagicMock
    id_a, token_a = keyring.create("key-a", tier="unlimited")
    _, token_b = keyring.create("key-b", tier="unlimited")
    keyring.invalidate_cache()
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=MagicMock(provider_name="os", id="1"))
    jwt_a = auth.mint_jwt({"kid": id_a})
    c = _app().test_client()
    # Correct pairing (A's key + A's JWT) works.
    ok = c.post("/api/v1/download", headers={"Api-Key": token_a,
                "Authorization": f"Bearer {jwt_a}"}, json={"file_id": fid})
    assert ok.status_code == 200
    # A's JWT presented with B's Api-Key is rejected (kid mismatch -> 401).
    bad = c.post("/api/v1/download", headers={"Api-Key": token_b,
                 "Authorization": f"Bearer {jwt_a}"}, json={"file_id": fid})
    assert bad.status_code == 401


def test_per_key_exclusion_default_applies(compat_db, monkeypatch):
    captured = {}

    def _fake_search(*a, **k):
        captured.update(k)
        return {"data": []}

    monkeypatch.setattr("compat.routes.service.search", _fake_search)
    from compat import keyring
    _, token = keyring.create("site", tier="free",
                              excluded_providers=["embeddedsubtitles"])
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/subtitles?imdb_id=tt1&languages=en",
              headers={"Api-Key": token})
    assert r.status_code == 200
    assert captured["exclude_providers"] == ["embeddedsubtitles"]

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


def test_only_providers_changes_cache_key():
    """An allow-listed search must not collide with the full-provider envelope,
    nor with the same-named exclusion (incl and excl are distinct dimensions)."""
    from compat.cache import build_key
    from subzero.language import Language
    langs = [Language.fromietf("en")]
    base = build_key("movie", "tt1", None, None, langs, ["p1", "p2"])
    incl = build_key("movie", "tt1", None, None, langs, ["p1", "p2"],
                     only_providers=["p1"])
    excl = build_key("movie", "tt1", None, None, langs, ["p1", "p2"],
                     exclude_providers=["p1"])
    assert base != incl
    assert incl != excl


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


def test_disabled_legacy_key_revokes_shared_token(compat_db):
    """Disabling the seeded Default key must actually revoke the shared token,
    not silently fall back to the unmetered Unlimited legacy record."""
    from app.config import settings
    from compat import keyring, auth
    settings["compat_endpoint"]["token"] = "L" * 40
    keyring.seed_legacy_key()
    keyring.invalidate_cache()
    assert auth.resolve_compat_key("L" * 40) is not None  # enabled -> works
    legacy = next(k for k in keyring.list_keys() if k["is_legacy"])
    keyring.update(legacy["id"], enabled=0)
    keyring.invalidate_cache()
    assert auth.resolve_compat_key("L" * 40) is None       # disabled -> revoked


def test_legacy_token_works_before_seed(compat_db):
    """Bootstrap window: token matches config but no legacy row yet -> serve the
    synthesized id=0 record so the endpoint works before the seed runs."""
    from app.config import settings
    from compat import keyring, auth
    settings["compat_endpoint"]["token"] = "M" * 40
    keyring.invalidate_cache()  # no seed called -> no is_legacy row
    rec = auth.resolve_compat_key("M" * 40)
    assert rec is not None and rec["id"] == 0 and rec["is_legacy"] == 1


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


def test_request_only_providers_reach_service(compat_db, monkeypatch):
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
              "&only_providers=opensubtitles,subscene",
              headers={"Api-Key": token})
    assert r.status_code == 200
    assert captured["only_providers"] == ["opensubtitles", "subscene"]


def test_per_key_allow_default_applies(compat_db, monkeypatch):
    captured = {}

    def _fake_search(*a, **k):
        captured.update(k)
        return {"data": []}

    monkeypatch.setattr("compat.routes.service.search", _fake_search)
    from compat import keyring
    _, token = keyring.create("site", tier="free",
                              allowed_providers=["opensubtitles"])
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/subtitles?imdb_id=tt1&languages=en",
              headers={"Api-Key": token})
    assert r.status_code == 200
    assert captured["only_providers"] == ["opensubtitles"]


def test_request_only_cannot_escape_key_grant(compat_db, monkeypatch):
    """A per-request only_providers cannot reach a provider outside the key's
    allowed_providers grant. The intersection is empty, so the search is scoped
    to nothing rather than silently widening to the requested provider (the P1
    authorization bypass)."""
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "search_rate_limit_enabled", False)
    captured = {}

    def _fake_search(*a, **k):
        captured.update(k)
        return {"data": []}

    monkeypatch.setattr("compat.routes.service.search", _fake_search)
    from compat import keyring
    _, token = keyring.create("site", tier="free",
                              allowed_providers=["opensubtitles"])
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/subtitles?imdb_id=tt1&languages=en"
              "&only_providers=subscene",
              headers={"Api-Key": token})
    assert r.status_code == 200
    assert captured["only_providers"] == []


# ---- provider discovery endpoint ----

def test_providers_endpoint_lists_allowed_names(compat_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    monkeypatch.setattr("compat.routes.service.available_providers",
                        lambda: ["opensubtitles", "subscene", "embeddedsubtitles"])
    from compat import keyring
    _, token = keyring.create("site", tier="free")
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/providers", headers={"Api-Key": token})
    assert r.status_code == 200
    names = [p["name"] for p in r.get_json()["data"]]
    assert names == ["opensubtitles", "subscene", "embeddedsubtitles"]


def test_providers_endpoint_hides_key_excluded(compat_db, monkeypatch):
    """A provider walled off for the key must not appear in its discovery list."""
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    monkeypatch.setattr("compat.routes.service.available_providers",
                        lambda: ["opensubtitles", "subscene", "embeddedsubtitles"])
    from compat import keyring
    _, token = keyring.create("site", tier="free",
                              excluded_providers=["embeddedsubtitles"])
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/providers", headers={"Api-Key": token})
    names = [p["name"] for p in r.get_json()["data"]]
    assert "embeddedsubtitles" not in names
    assert set(names) == {"opensubtitles", "subscene"}


def test_providers_endpoint_intersects_key_allow_default(compat_db, monkeypatch):
    monkeypatch.setattr("compat.routes.service.available_providers",
                        lambda: ["opensubtitles", "subscene", "embeddedsubtitles"])
    from compat import keyring
    _, token = keyring.create("site", tier="free",
                              allowed_providers=["subscene"])
    keyring.invalidate_cache()
    c = _app().test_client()
    r = c.get("/api/v1/providers", headers={"Api-Key": token})
    names = [p["name"] for p in r.get_json()["data"]]
    assert names == ["subscene"]


def test_providers_endpoint_rejects_unknown_key(monkeypatch):
    monkeypatch.setattr("compat.routes.service.available_providers",
                        lambda: ["opensubtitles"])
    c = _app().test_client()
    r = c.get("/api/v1/providers", headers={"Api-Key": "bzr_nope"})
    assert r.status_code == 403
    assert r.headers["x-reason"] == "auth"


def test_providers_endpoint_lists_local_when_serve_on(compat_db, monkeypatch):
    """`local` is a selectable name when serve_local_subs is on, and is hidden
    when it is off."""
    from app.config import settings
    monkeypatch.setattr("compat.routes.service.available_providers",
                        lambda: ["opensubtitles"])
    from compat import keyring
    _, token = keyring.create("site", tier="free")
    keyring.invalidate_cache()
    c = _app().test_client()
    h = {"Api-Key": token}

    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", True)
    names = [p["name"] for p in c.get("/api/v1/providers", headers=h).get_json()["data"]]
    assert "local" in names

    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    names = [p["name"] for p in c.get("/api/v1/providers", headers=h).get_json()["data"]]
    assert "local" not in names


# ---- only_providers cache tri-state + per-key intersection ----

def test_only_providers_none_vs_empty_are_distinct():
    """None (no allow-list, all providers) must not share a cache key with []
    (allow-list active but matching nothing -> data: [])."""
    from compat.cache import build_key
    from subzero.language import Language
    langs = [Language.fromietf("en")]
    none_key = build_key("movie", "tt1", None, None, langs, ["p1", "p2"])
    empty_key = build_key("movie", "tt1", None, None, langs, ["p1", "p2"],
                          only_providers=[])
    scoped_key = build_key("movie", "tt1", None, None, langs, ["p1", "p2"],
                           only_providers=["p1"])
    assert none_key != empty_key
    assert empty_key != scoped_key


def test_request_only_providers_intersects_key_grant(compat_db, monkeypatch):
    """A per-request only_providers narrows within the key's allowed_providers
    grant (intersection); it can never reach a provider outside the grant."""
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "search_rate_limit_enabled", False)
    captured = {}

    def _fake_search(*a, **k):
        captured.clear()
        captured.update(k)
        return {"data": []}

    monkeypatch.setattr("compat.routes.service.search", _fake_search)
    from compat import keyring
    _, scoped = keyring.create("scoped", tier="free", allowed_providers=["p1"])
    _, open_key = keyring.create("open", tier="free")
    keyring.invalidate_cache()
    c = _app().test_client()
    base = "/api/v1/subtitles?imdb_id=tt1&languages=en"

    # request narrows within the grant: {p1,p2} ∩ {p1} == [p1]
    c.get(f"{base}&only_providers=p1,p2", headers={"Api-Key": scoped})
    assert captured["only_providers"] == ["p1"]

    # request outside the grant: {p2} ∩ {p1} == [] (active-empty, not None) so
    # the search yields nothing rather than silently widening to all providers
    c.get(f"{base}&only_providers=p2", headers={"Api-Key": scoped})
    assert captured["only_providers"] == []

    # no request allow-list: the key's grant applies
    c.get(base, headers={"Api-Key": scoped})
    assert captured["only_providers"] == ["p1"]

    # no grant, no request: no allow-list at all (None, every provider in play)
    c.get(base, headers={"Api-Key": open_key})
    assert captured["only_providers"] is None

    # no grant, with request: the request stands alone
    c.get(f"{base}&only_providers=p2", headers={"Api-Key": open_key})
    assert captured["only_providers"] == ["p2"]

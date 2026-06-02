"""Per-key download rate limiting (Distribution Hub).

Replaces the legacy global jti download quota. A named key with a finite
download limit throttles with the OS-contract 406; the legacy/Unlimited
shared token is never throttled (back-compat for existing integrations).
"""
from unittest.mock import MagicMock
import pytest
from flask import Flask

LEGACY_KEY = "t" * 32


@pytest.fixture(autouse=True)
def _secrets():
    from app.config import settings
    settings["compat_endpoint"]["token"] = LEGACY_KEY
    settings["compat_endpoint"]["jwt_secret"] = "j" * 32
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["jwt_ttl_seconds"] = 3600
    settings["compat_endpoint"]["downloads_per_window"] = 2
    settings["compat_endpoint"]["downloads_window_seconds"] = 60
    yield


def _app():
    from compat.routes import compat_bp
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    return app


def _limited_key(compat_db, hour_limit=2):
    from compat import keyring
    kid, token = keyring.create("metered-site", tier="free",
                                custom_limits={"download": {"hour": hour_limit,
                                                            "day": 0, "week": 0,
                                                            "month": 0}})
    keyring.invalidate_cache()
    return kid, token


def _download(client, token, jwt_tok, fid):
    return client.post("/api/v1/download",
                       headers={"Api-Key": token,
                                "Authorization": f"Bearer {jwt_tok}"},
                       json={"file_id": fid})


def test_named_key_emits_406_after_quota_exhausted(compat_db):
    from compat import auth
    _, token = _limited_key(compat_db, hour_limit=2)
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    c = _app().test_client()

    for _ in range(2):
        assert _download(c, token, jwt_tok, fid).status_code == 200

    r = _download(c, token, jwt_tok, fid)
    assert r.status_code == 406
    assert r.headers.get("x-reason") == "throttled"
    assert "reset_time_utc" in r.get_json()


def test_named_key_remaining_decrements(compat_db):
    from compat import auth
    _, token = _limited_key(compat_db, hour_limit=2)
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    c = _app().test_client()

    r1 = _download(c, token, jwt_tok, fid)
    assert r1.get_json()["remaining_downloads"] == 1
    r2 = _download(c, token, jwt_tok, fid)
    assert r2.get_json()["remaining_downloads"] == 0


def test_infos_user_reports_named_key_quota(compat_db):
    from compat import auth
    _, token = _limited_key(compat_db, hour_limit=2)
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    c = _app().test_client()
    _download(c, token, jwt_tok, fid)

    r = c.get("/api/v1/infos/user",
              headers={"Api-Key": token, "Authorization": f"Bearer {jwt_tok}"})
    assert r.status_code == 200
    body = r.get_json()
    # Inspect-only; must not consume an extra unit.
    assert body["data"]["remaining_downloads"] == 1
    assert body["data"]["allowed_downloads"] == 2


def test_legacy_unlimited_token_never_throttles(compat_db):
    from compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    c = _app().test_client()
    # Far exceed the legacy downloads_per_window cosmetic ceiling (2).
    for _ in range(5):
        assert _download(c, LEGACY_KEY, jwt_tok, fid).status_code == 200


def test_infos_user_without_bearer_reports_full_quota():
    c = _app().test_client()
    r = c.get("/api/v1/infos/user", headers={"Api-Key": LEGACY_KEY})
    assert r.status_code == 200
    # Legacy/Unlimited key reports the cosmetic downloads_per_window ceiling.
    assert r.get_json()["data"]["allowed_downloads"] == 2

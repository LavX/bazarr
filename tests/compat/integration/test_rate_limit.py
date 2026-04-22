from unittest.mock import MagicMock, patch
import pytest
from flask import Flask

API_KEY = "t" * 32


@pytest.fixture(autouse=True)
def _secrets():
    from bazarr.app.config import settings
    from bazarr.compat import rate_limiter, jwt_denylist
    settings["compat_endpoint"]["token"] = API_KEY
    settings["compat_endpoint"]["jwt_secret"] = "j" * 32
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["jwt_ttl_seconds"] = 3600
    settings["compat_endpoint"]["downloads_per_window"] = 2
    settings["compat_endpoint"]["downloads_window_seconds"] = 60
    rate_limiter.reset()
    jwt_denylist.reset()
    yield
    rate_limiter.reset()


def _app():
    from bazarr.compat.routes import compat_bp
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    return app


def test_download_emits_406_after_quota_exhausted():
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()

    c = _app().test_client()
    # Limit = 2 per window.
    for _ in range(2):
        r = c.post("/api/v1/download",
                   headers={"Api-Key": API_KEY,
                            "Authorization": f"Bearer {jwt_tok}"},
                   json={"file_id": fid})
        assert r.status_code == 200

    r = c.post("/api/v1/download",
               headers={"Api-Key": API_KEY,
                        "Authorization": f"Bearer {jwt_tok}"},
               json={"file_id": fid})
    assert r.status_code == 406
    assert r.headers.get("x-reason") == "throttled"
    body = r.get_json()
    assert "reset_time_utc" in body


def test_download_remaining_decrements():
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    c = _app().test_client()
    r = c.post("/api/v1/download",
               headers={"Api-Key": API_KEY,
                        "Authorization": f"Bearer {jwt_tok}"},
               json={"file_id": fid})
    assert r.status_code == 200
    assert r.get_json()["remaining_downloads"] == 1
    r = c.post("/api/v1/download",
               headers={"Api-Key": API_KEY,
                        "Authorization": f"Bearer {jwt_tok}"},
               json={"file_id": fid})
    assert r.get_json()["remaining_downloads"] == 0


def test_infos_user_reports_real_remaining():
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    c = _app().test_client()
    c.post("/api/v1/download",
           headers={"Api-Key": API_KEY,
                    "Authorization": f"Bearer {jwt_tok}"},
           json={"file_id": fid})

    r = c.get("/api/v1/infos/user",
              headers={"Api-Key": API_KEY,
                       "Authorization": f"Bearer {jwt_tok}"})
    assert r.status_code == 200
    body = r.get_json()
    # Inspect-only; must not consume an extra unit.
    assert body["data"]["remaining_downloads"] == 1
    assert body["data"]["allowed_downloads"] == 2


def test_infos_user_without_bearer_reports_full_quota():
    """Api-Key-only callers (no jti) aren't rate-limited individually.
    Report the configured ceiling so the UI shows something sensible."""
    c = _app().test_client()
    r = c.get("/api/v1/infos/user", headers={"Api-Key": API_KEY})
    assert r.status_code == 200
    assert r.get_json()["data"]["allowed_downloads"] == 2

from unittest.mock import MagicMock
import pytest
from flask import Flask

API_KEY = "t" * 32


@pytest.fixture(autouse=True)
def _secrets():
    from bazarr.app.config import settings
    from bazarr.compat import rate_limiter
    settings["compat_endpoint"]["token"] = API_KEY
    settings["compat_endpoint"]["jwt_secret"] = "j" * 32
    settings["compat_endpoint"]["file_id_secret"] = "f" * 32
    settings["compat_endpoint"]["jwt_ttl_seconds"] = 3600
    settings["compat_endpoint"]["downloads_per_window"] = 1000
    settings["compat_endpoint"]["downloads_window_seconds"] = 86400
    rate_limiter.reset()
    yield


def _app():
    from bazarr.compat.routes import compat_bp
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    return app


def test_download_accepts_srt():
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    r = _app().test_client().post(
        "/api/v1/download",
        headers={"Api-Key": API_KEY,
                 "Authorization": f"Bearer {jwt_tok}"},
        json={"file_id": fid, "sub_format": "srt"},
    )
    assert r.status_code == 200


def test_download_rejects_non_srt_sub_format():
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    r = _app().test_client().post(
        "/api/v1/download",
        headers={"Api-Key": API_KEY,
                 "Authorization": f"Bearer {jwt_tok}"},
        json={"file_id": fid, "sub_format": "vtt"},
    )
    assert r.status_code == 400
    assert r.headers.get("x-reason") == "bad-request"


def test_download_defaults_sub_format_to_srt():
    """sub_format missing entirely is fine (plugin may omit)."""
    from bazarr.compat import auth
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    jwt_tok = auth.mint_jwt()
    r = _app().test_client().post(
        "/api/v1/download",
        headers={"Api-Key": API_KEY,
                 "Authorization": f"Bearer {jwt_tok}"},
        json={"file_id": fid},
    )
    assert r.status_code == 200

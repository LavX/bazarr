import pytest
from flask import Flask


@pytest.fixture(autouse=True)
def _reset_compat_secrets():
    from bazarr.app.config import settings
    original = {
        name: getattr(settings.compat_endpoint, name, "")
        for name in ("token", "jwt_secret", "file_id_secret",
                     "jwt_ttl_seconds", "file_id_ttl_seconds",
                     "stream_token_ttl_seconds")
    }
    settings.compat_endpoint.token = "t" * 32
    settings.compat_endpoint.jwt_secret = "j" * 32
    settings.compat_endpoint.file_id_secret = "f" * 32
    settings.compat_endpoint.jwt_ttl_seconds = 60
    settings.compat_endpoint.file_id_ttl_seconds = 60
    settings.compat_endpoint.stream_token_ttl_seconds = 60
    yield
    for name, value in original.items():
        setattr(settings.compat_endpoint, name, value)


def _make_app():
    from bazarr.compat.routes import compat_bp
    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")
    return app


def test_login_response_includes_base_url():
    """OS.com wire contract: login returns a top-level base_url. Some OS-compat
    clients use it to auto-correct misconfigured base URLs."""
    app = _make_app()
    r = app.test_client().post(
        "/api/v1/login",
        headers={"Api-Key": "t" * 32},
        json={"username": "u", "password": "p"},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "base_url" in body
    assert isinstance(body["base_url"], str) and body["base_url"]
    assert "token" in body


def test_login_base_url_honors_x_forwarded_host():
    """Bazarr's supervisor proxies 6767 -> 6768, so request.host is the inner
    backend. base_url must come from X-Forwarded-Host when present."""
    app = _make_app()
    r = app.test_client().post(
        "/api/v1/login",
        headers={"Api-Key": "t" * 32,
                 "X-Forwarded-Host": "bazarr.example.com:6767"},
        json={},
    )
    assert r.status_code == 200
    assert r.get_json()["base_url"] == "bazarr.example.com:6767"


def test_download_link_uses_forwarded_host_for_fqdn():
    """When X-Forwarded-Host/Proto are set, the download link is absolute so
    OS-compat clients can hand it to an HTTP client without computing a base."""
    from bazarr.compat import auth
    from bazarr.compat.file_id_store import reset_store
    from unittest.mock import MagicMock
    reset_store()
    fake_sub = MagicMock(provider_name="os", id="1", language=MagicMock(),
                          release_info="r", download_count=0)
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)

    # Mint a JWT the route can accept
    app = _make_app()
    jwt_tok = auth.mint_jwt()
    r = app.test_client().post(
        "/api/v1/download",
        headers={"Api-Key": "t" * 32,
                 "Authorization": f"Bearer {jwt_tok}",
                 "X-Forwarded-Host": "bazarr.example.com",
                 "X-Forwarded-Proto": "https"},
        json={"file_id": fid},
    )
    assert r.status_code == 200
    link = r.get_json()["link"]
    assert link.startswith("https://bazarr.example.com/api/v1/download/stream/"), link


def test_download_link_is_always_absolute():
    """Plugin contract: `link` MUST be an absolute URL. Relative paths
    crash HttpClient.GetAsync when the plugin has no BaseAddress set.
    With only loopback visible we fall back to request.host_url, which
    is at least a valid absolute URL the client actually hit."""
    from bazarr.compat import auth
    from bazarr.compat.file_id_store import reset_store
    from unittest.mock import MagicMock
    reset_store()
    fake_sub = MagicMock(provider_name="os", id="1")
    fid = auth.mint_file_id("os", "1", "eng", "", subtitle=fake_sub)
    app = _make_app()
    jwt_tok = auth.mint_jwt()
    r = app.test_client().post(
        "/api/v1/download",
        headers={"Api-Key": "t" * 32,
                 "Authorization": f"Bearer {jwt_tok}"},
        json={"file_id": fid},
    )
    assert r.status_code == 200
    link = r.get_json()["link"]
    assert link.startswith(("http://", "https://")), link
    assert "/api/v1/download/stream/" in link


def test_infos_user_accepts_api_key_alone():
    """/infos/user is Api-Key-only (no Bearer JWT). Jellyfin polls this route
    for remaining-downloads updates without re-login."""
    app = _make_app()
    r = app.test_client().get(
        "/api/v1/infos/user",
        headers={"Api-Key": "t" * 32},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "data" in body
    assert "remaining_downloads" in body["data"]


def test_infos_user_rejects_missing_api_key():
    """Api-Key failures are 403 so the plugin doesn't misread them as
    a JWT-expiry signal and retry in a loop."""
    app = _make_app()
    r = app.test_client().get("/api/v1/infos/user")
    assert r.status_code == 403
    assert r.headers["x-reason"] == "auth"

import pytest
from flask import Flask
from bazarr.compat.auth import compat_auth, compat_error


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.token", "a" * 32)
    app = Flask(__name__)

    @app.route("/protected")
    @compat_auth(require_jwt=False)
    def protected():
        return "ok", 200

    return app


def test_missing_api_key_returns_403(app):
    """Contract: Api-Key failures MUST NOT use 401, which the plugin
    reads as 'JWT expired, clear and retry'. Forbidden = 403."""
    r = app.test_client().get("/protected")
    assert r.status_code == 403
    assert r.headers.get("x-reason") == "auth"
    assert r.json == {"message": "Missing API key"}


def test_wrong_api_key_returns_403(app):
    r = app.test_client().get("/protected", headers={"Api-Key": "x" * 32})
    assert r.status_code == 403 and r.headers["x-reason"] == "auth"


def test_jwt_failures_still_use_401(monkeypatch):
    """401 is reserved for JWT-specific failures so the plugin can
    retry with a fresh login."""
    from flask import Flask
    from bazarr.compat.auth import compat_auth
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.token",
                        "a" * 32)
    monkeypatch.setattr("bazarr.compat.auth.settings.compat_endpoint.jwt_secret",
                        "b" * 32)
    app = Flask(__name__)

    @app.route("/protected-jwt")
    @compat_auth(require_jwt=True)
    def _protected():
        return "ok", 200

    # Valid Api-Key but missing Bearer -> 401 (JWT-expiry signal)
    r = app.test_client().get("/protected-jwt",
                              headers={"Api-Key": "a" * 32})
    assert r.status_code == 401 and r.headers["x-reason"] == "auth"

    # Valid Api-Key but invalid Bearer -> 401
    r = app.test_client().get("/protected-jwt",
                              headers={"Api-Key": "a" * 32,
                                       "Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401 and r.headers["x-reason"] == "auth"


def test_valid_api_key_passes(app):
    r = app.test_client().get("/protected", headers={"Api-Key": "a" * 32})
    assert r.status_code == 200


def test_compat_error_unknown_x_reason_raises():
    """compat_error must reject unknown x-reason values even without -O optimizations."""
    app = Flask(__name__)
    with app.app_context():
        with pytest.raises(ValueError, match="x-reason"):
            compat_error("boom", 500, "totally-made-up")


def test_compat_error_sets_both_headers():
    app = Flask(__name__)
    with app.app_context():
        resp = compat_error("oops", 400, "bad-request")
        assert resp.headers["x-reason"] == "bad-request"
        assert resp.headers["Content-Type"] == "application/json"
        assert resp.status_code == 400

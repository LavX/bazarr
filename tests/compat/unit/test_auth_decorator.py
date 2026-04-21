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


def test_missing_api_key_returns_401(app):
    r = app.test_client().get("/protected")
    assert r.status_code == 401
    assert r.headers.get("x-reason") == "auth"
    assert r.json == {"message": "Missing API key"}


def test_wrong_api_key_returns_401(app):
    r = app.test_client().get("/protected", headers={"Api-Key": "x" * 32})
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

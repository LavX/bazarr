"""
Tests for the form-login session gate.

The gate used to ask whether the session carried a `logged_in` key at all,
while a failed login wrote that key with the value False. One wrong password
therefore handed the visitor a cookie that satisfied the gate, which opened the
log download, the backup download (config.yaml plus the database) and both
cover-image proxies. These tests pin the value check and the login endpoint's
half of the contract so the pair cannot drift apart again.
"""
from types import SimpleNamespace

import pytest
from flask import Flask, session

from literals import FILE_LOG


# The routes that carry @check_login and have no second gate of their own. The
# two /test/* proxies are deliberately absent: they are additionally gated by
# _require_proxy_api_key(), so the session bug never reached them.
PROTECTED_ROUTES = [
    "/" + FILE_LOG,
    "/images/series/MediaCover/1/poster.jpg",
    "/images/movies/MediaCover/1/poster.jpg",
    "/system/backup/download/bazarr_backup_v1.6.0_20260915.zip",
]


@pytest.fixture
def form_auth_app(monkeypatch):
    """A real app with the real ui blueprint, configured for form login."""
    from app import ui

    monkeypatch.setattr(
        ui, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    flask_app.register_blueprint(ui.ui_bp)
    return flask_app


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
def test_failed_login_session_does_not_unlock_protected_routes(form_auth_app, route):
    """
    The session a failed login leaves behind must not pass the gate.

    Asserting 401 rather than "not 200" is deliberate: it proves the gate fired
    before the view body ran, instead of the body happening to fail.
    """
    client = form_auth_app.test_client()
    with client.session_transaction() as sess:
        # Exactly what a failed login used to write.
        sess["logged_in"] = False

    assert client.get(route).status_code == 401


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
def test_anonymous_session_does_not_unlock_protected_routes(form_auth_app, route):
    client = form_auth_app.test_client()

    assert client.get(route).status_code == 401


def test_check_login_rejects_a_falsy_session_value():
    """The decorator must read the value, never merely the key."""
    from app import ui

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"

    @app.route("/probe")
    @ui.check_login
    def probe():  # pragma: no cover - only reached if the gate wrongly passes
        return "reached"

    with patch_form_auth(ui):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = False
        assert client.get("/probe").status_code == 401


def test_check_login_accepts_a_real_session():
    from app import ui

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"

    @app.route("/probe")
    @ui.check_login
    def probe():
        return "reached"

    with patch_form_auth(ui):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        response = client.get("/probe")
        assert response.status_code == 200
        assert response.get_data(as_text=True) == "reached"


def patch_form_auth(ui_module):
    """Context manager swapping the ui module's settings for form auth."""
    from unittest.mock import patch

    return patch.object(
        ui_module, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )


def test_failed_login_leaves_no_session_flag(monkeypatch):
    """
    The login endpoint's half of the contract: a rejected attempt must not put
    a `logged_in` key into the session at all.
    """
    from api.system import account

    monkeypatch.setattr(
        account, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )
    monkeypatch.setattr(account, "check_credentials", lambda *a, **k: False)
    account._login_attempts.clear()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"

    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "someone", "password": "wrong"},
    ):
        body, status = account.SystemAccount().post()
        assert status == 403
        assert "logged_in" not in session


def test_successful_login_sets_the_session_flag_to_true(monkeypatch):
    from api.system import account

    monkeypatch.setattr(
        account, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )
    monkeypatch.setattr(account, "check_credentials", lambda *a, **k: True)
    monkeypatch.setattr(account, "needs_password_upgrade", lambda: False)
    account._login_attempts.clear()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"

    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "someone", "password": "right"},
    ):
        _, status = account.SystemAccount().post()
        assert status == 204
        assert session["logged_in"] is True


def _login_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret-key"
    return app


def test_legacy_hash_is_upgraded_during_the_login_itself(monkeypatch):
    """
    The plaintext password must never be parked in the session.

    Flask's session is a signed but unencrypted cookie, so anything stored in
    it is handed to the browser and rides in every later request. The legacy
    MD5 upgrade used to stash the submitted password there and re-read it on a
    follow-up call. It is re-hashed inline instead, where the plaintext already
    is.
    """
    import hashlib

    from api.system import account
    from utilities import helper

    stored = SimpleNamespace(
        auth=SimpleNamespace(
            type="form",
            username="admin",
            password=hashlib.md5(b"hunter2").hexdigest(),
        )
    )
    monkeypatch.setattr(account, "settings", stored)
    monkeypatch.setattr(helper, "settings", stored)
    monkeypatch.setattr("app.config.write_config", lambda: None)
    account._login_attempts.clear()

    app = _login_app()
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": "hunter2"},
    ):
        body, status = account.SystemAccount().post()

        assert status == 204
        assert stored.auth.password.startswith("pbkdf2:")
        assert "hunter2" not in repr(body)
        assert not any("hunter2" in str(v) for v in session.values())
        assert "_pw_for_upgrade" not in session
        assert "_upgrade_token" not in session


def test_login_response_and_session_never_carry_the_password(monkeypatch):
    from api.system import account

    monkeypatch.setattr(
        account, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )
    monkeypatch.setattr(account, "check_credentials", lambda *a, **k: True)
    monkeypatch.setattr(account, "needs_password_upgrade", lambda: True)
    monkeypatch.setattr(account, "upgrade_password_hash", lambda pw: None)
    account._login_attempts.clear()

    secret = "correct-horse-battery-staple"
    app = _login_app()
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": secret},
    ):
        body, status = account.SystemAccount().post()

        assert status == 204
        assert secret not in repr(body)
        assert not any(secret in str(v) for v in session.values())


def test_a_failed_hash_upgrade_does_not_fail_a_valid_login(monkeypatch):
    """A config write problem must not reject credentials that were correct."""
    from api.system import account

    def boom(_pw):
        raise OSError("config.yaml is read-only")

    monkeypatch.setattr(
        account, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )
    monkeypatch.setattr(account, "check_credentials", lambda *a, **k: True)
    monkeypatch.setattr(account, "needs_password_upgrade", lambda: True)
    monkeypatch.setattr(account, "upgrade_password_hash", boom)
    account._login_attempts.clear()

    app = _login_app()
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": "pw"},
    ):
        _, status = account.SystemAccount().post()

        assert status == 204
        assert session["logged_in"] is True


def test_the_deferred_upgrade_action_is_gone(monkeypatch):
    """The second half of the old flow must not linger as a live endpoint."""
    from api.system import account

    monkeypatch.setattr(
        account, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )

    app = _login_app()
    with app.test_request_context(
        "/api/system/account?action=upgrade_hash",
        method="POST",
        data={"password": "any-token"},
    ):
        body, status = account.SystemAccount().post()

        assert status == 400
        assert body == "Unknown action"


def test_an_upstream_bazarr_user_can_log_in_twice(monkeypatch):
    """
    Bazarr+ adopts installs coming from upstream Bazarr, which stores MD5.

    Those users must be able to sign in with the password they already have,
    and must still be able to sign in with that same password after the
    silent upgrade has rewritten the stored hash. The second login is the one
    that breaks if the upgrade writes something the verifier cannot read.
    """
    import hashlib

    from api.system import account
    from utilities import helper

    stored = SimpleNamespace(
        auth=SimpleNamespace(
            type="form",
            username="admin",
            password=hashlib.md5(b"from-upstream").hexdigest(),
        )
    )
    monkeypatch.setattr(account, "settings", stored)
    monkeypatch.setattr(helper, "settings", stored)
    monkeypatch.setattr("app.config.write_config", lambda: None)
    account._login_attempts.clear()

    app = _login_app()

    # First login: the MD5 hash still verifies, and is upgraded in place.
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": "from-upstream"},
    ):
        _, status = account.SystemAccount().post()
        assert status == 204

    upgraded = stored.auth.password
    assert upgraded.startswith("pbkdf2:")

    # Second login against the rewritten hash.
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": "from-upstream"},
    ):
        _, status = account.SystemAccount().post()
        assert status == 204

    # The upgrade runs once and leaves the hash alone thereafter.
    assert stored.auth.password == upgraded

    # And the wrong password is still rejected against the new hash.
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": "not-the-password"},
    ):
        _, status = account.SystemAccount().post()
        assert status == 403


def test_login_drops_a_stale_password_left_by_an_older_release(monkeypatch):
    """
    Releases before this one parked the plaintext password in the session under
    _pw_for_upgrade. That cookie is still in browsers, so a login that only
    added its own key would re-sign the old payload and hand it back with a
    fresh expiry, outliving the fix.
    """
    from api.system import account

    monkeypatch.setattr(
        account, "settings", SimpleNamespace(auth=SimpleNamespace(type="form"))
    )
    monkeypatch.setattr(account, "check_credentials", lambda *a, **k: True)
    monkeypatch.setattr(account, "needs_password_upgrade", lambda: False)
    account._login_attempts.clear()

    app = _login_app()
    with app.test_request_context(
        "/api/system/account?action=login",
        method="POST",
        data={"username": "admin", "password": "pw"},
    ):
        session["_pw_for_upgrade"] = "hunter2"
        session["_upgrade_token"] = "stale-token"

        _, status = account.SystemAccount().post()

        assert status == 204
        assert "_pw_for_upgrade" not in session
        assert "_upgrade_token" not in session
        assert session["logged_in"] is True

"""
Tests for the session cookie's attributes and lifetime.

The cookie carried no SameSite, no Secure policy and no expiry: it was a
browser-session cookie that lived until the tab closed, or indefinitely in a
browser that restores tabs, and on an HTTPS deployment it was still sent over
plain HTTP if anything ever reached the instance that way.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, session


def _app(cookie_secure="auto", lifetime_days=30):
    from app.app import configure_session_cookie

    config = SimpleNamespace(
        auth=SimpleNamespace(
            cookie_secure=cookie_secure, session_lifetime_days=lifetime_days
        )
    )
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    configure_session_cookie(flask_app, config)

    @flask_app.route("/sign-in")
    def sign_in():
        from app.auth import establish_session
        from app.auth.generation import current_generation

        establish_session(current_generation())
        return "ok"

    return flask_app


def _set_cookie_header(response):
    return response.headers.get("Set-Cookie", "")


def test_cookie_is_httponly_and_samesite_lax():
    response = _app().test_client().get("/sign-in")

    header = _set_cookie_header(response)
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header


def test_cookie_carries_an_expiry_from_the_configured_lifetime():
    """The old cookie had no expiry at all, so it lived as long as the browser."""
    from email.utils import parsedate_to_datetime

    response = _app(lifetime_days=7).test_client().get("/sign-in")

    header = _set_cookie_header(response)
    assert "Expires=" in header

    expires = parsedate_to_datetime(header.split("Expires=")[1].split(";")[0])
    now = datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < expires - now < timedelta(days=7, hours=1)


@pytest.mark.parametrize(
    ("policy", "https", "expected"),
    [
        ("auto", False, False),
        ("auto", True, True),
        ("always", False, True),
        ("always", True, True),
        ("never", False, False),
        ("never", True, False),
    ],
)
def test_secure_flag_follows_the_policy(policy, https, expected):
    """
    'auto' has to answer per request, not once at startup: the same instance is
    reached over plain http on the LAN and over https through a proxy, and a
    single answer breaks one of them.
    """
    client = _app(cookie_secure=policy).test_client()

    base = "https://bazarr.example" if https else "http://bazarr.local:6767"
    response = client.get("/sign-in", base_url=base)

    assert ("Secure" in _set_cookie_header(response)) is expected


def test_the_session_survives_a_round_trip():
    """The policy must not stop the cookie from actually signing anyone in."""
    from app.auth import is_session_authenticated

    app = _app()

    @app.route("/whoami")
    def whoami():
        return "yes" if is_session_authenticated() else "no"

    client = app.test_client()
    assert client.get("/whoami").get_data(as_text=True) == "no"
    client.get("/sign-in")
    assert client.get("/whoami").get_data(as_text=True) == "yes"


def test_establish_session_marks_the_session_permanent():
    """Without this the configured lifetime is never applied."""
    from app.auth import establish_session
    from app.auth.generation import current_generation

    app = _app()
    with app.test_request_context("/"):
        establish_session(current_generation())
        assert session.permanent is True


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("  10.0.0.2  ", "10.0.0.2"),
        ("", None),
        (None, None),
    ],
)
def test_trusted_proxy_value(configured, expected):
    from app.app import trusted_proxy_value

    config = SimpleNamespace(general=SimpleNamespace(trusted_proxy=configured))

    assert trusted_proxy_value(config) == expected


def test_the_forwarded_scheme_is_believed_only_from_the_configured_peer(monkeypatch):
    """The trust rule lives in the middleware create_app wraps around the
    application, so it has to be pinned there: only general.trusted_proxy's
    peer has its X-Forwarded-Proto believed, and any other peer's forwarded
    headers are stripped before a route ever sees them."""
    from app import app as app_module
    from app.app import create_app

    socketio = app_module.socketio
    for name in ("server", "sockio_mw", "async_mode"):
        monkeypatch.setattr(socketio, name, getattr(socketio, name, None), raising=False)
    monkeypatch.setattr(socketio, "server_options", dict(socketio.server_options))

    flask_app = create_app()

    # An api-package import earlier in the same process leaves a transaction
    # open on this thread's session, and the request's own begin() would
    # refuse it otherwise.
    from app.database import database

    database.close()

    @flask_app.route("/scheme-probe")
    def scheme_probe():
        from flask import request

        return request.scheme

    client = flask_app.test_client()
    client.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    assert client.get("/scheme-probe",
                     headers={"X-Forwarded-Proto": "https"}).get_data(as_text=True) == "https"
    client.environ_base["REMOTE_ADDR"] = "10.0.0.9"
    assert client.get("/scheme-probe",
                     headers={"X-Forwarded-Proto": "https"}).get_data(as_text=True) == "http"


def test_the_server_options_leave_the_forwarded_headers_to_the_app():
    """The server passes no proxy options of its own: with waitress's default
    clear_untrusted_proxy_headers it would strip a trusted proxy's headers in
    front of the app, and the app's own parsing would never see them. The
    options helper pins that, and it must still build a server an empty
    trusted_proxy setting can start."""
    from waitress.adjustments import Adjustments

    from app.app import trusted_proxy_value, waitress_server_options

    proxy = trusted_proxy_value(SimpleNamespace(general=SimpleNamespace(trusted_proxy="")))
    assert proxy is None

    options = waitress_server_options()
    assert options["clear_untrusted_proxy_headers"] is False
    assert "trusted_proxy" not in options

    # Must not raise.
    Adjustments(host="127.0.0.1", port=6767, **options)

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

        establish_session()
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

    app = _app()
    with app.test_request_context("/"):
        establish_session()
        assert session.permanent is True


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (["127.0.0.1"], "127.0.0.1"),
        (["127.0.0.1", "172.18.0.5"], "127.0.0.1,172.18.0.5"),
        ([" 10.0.0.2 ", ""], "10.0.0.2"),
        ([], None),
        (None, None),
    ],
)
def test_trusted_proxy_value(configured, expected):
    from app.app import trusted_proxy_value

    config = SimpleNamespace(general=SimpleNamespace(trusted_proxies=configured))

    assert trusted_proxy_value(config) == expected

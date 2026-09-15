"""
Tests for the event socket's origin policy and connect gate.

The socket was configured with a wildcard origin and had no connect handler at
all. Engineio reflects the caller's origin and adds the credentials header for
a wildcard, so any page in any tab could open a long poll and read the whole
live event stream, with no cookie and no API key. The rest of the application
was gated; this channel was not.
"""
from types import SimpleNamespace

import pytest
from flask import Flask


def _config(cors=False, auth_type=None):
    return SimpleNamespace(
        cors=SimpleNamespace(enabled=cors),
        auth=SimpleNamespace(type=auth_type),
    )


def test_socket_is_same_origin_unless_cors_is_turned_on():
    """
    None is engineio's same-origin mode: it builds the allowed origin from the
    request's own scheme and host. The wildcard must be an explicit choice.
    """
    from app.app import socket_allowed_origins

    assert socket_allowed_origins(_config(cors=False)) is None
    assert socket_allowed_origins(_config(cors=True)) == "*"


def test_same_origin_mode_follows_forwarded_headers():
    """
    A reverse-proxied install has to keep working, so the same-origin value
    engineio computes must honour the forwarded scheme and host rather than the
    internal one.
    """
    from engineio import Server

    server = Server(cors_allowed_origins=None)
    allowed = server._cors_allowed_origins(
        {
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "127.0.0.1:6767",
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_FORWARDED_HOST": "bazarr.example",
        }
    )

    assert "https://bazarr.example" in allowed


@pytest.mark.parametrize(
    ("auth_type", "signed_in", "expected"),
    [
        (None, False, True),   # no login mode: the socket is as open as everything else
        ("form", False, False),
        ("form", True, True),
    ],
)
def test_connect_is_refused_without_a_session_under_form_auth(
    monkeypatch, auth_type, signed_in, expected
):
    from app import app as app_module

    monkeypatch.setattr(app_module, "settings", _config(auth_type=auth_type))

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    with flask_app.test_request_context("/api/socket.io/"):
        from flask import session

        if signed_in:
            session["logged_in"] = True

        assert app_module._authorize_socket_connection() is expected


def test_connect_under_basic_auth_checks_the_header(monkeypatch):
    from app import app as app_module
    from utilities import helper

    monkeypatch.setattr(app_module, "settings", _config(auth_type="basic"))
    monkeypatch.setattr(
        helper,
        "settings",
        SimpleNamespace(auth=SimpleNamespace(username="admin", password="")),
    )
    monkeypatch.setattr(helper, "_verify_password", lambda pw, _stored: pw == "right")

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    import base64

    def _connect_with(user, password):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        with flask_app.test_request_context(
            "/api/socket.io/", headers={"Authorization": f"Basic {token}"}
        ):
            return app_module._authorize_socket_connection()

    assert _connect_with("admin", "right") is True
    assert _connect_with("admin", "wrong") is False

    with flask_app.test_request_context("/api/socket.io/"):
        assert app_module._authorize_socket_connection() is False


def test_the_handler_is_registered_on_the_socket():
    """A handler that is never wired up gates nothing."""
    from app.app import socketio

    # flask-socketio stores (event, handler, namespace).
    events = [event for event, _handler, _namespace in socketio.handlers]

    assert "connect" in events

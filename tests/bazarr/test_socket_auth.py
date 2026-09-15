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


API_KEY = "testapikeytestapikeytestapikey12"


def _config(cors=False, auth_type=None, apikey=API_KEY):
    return SimpleNamespace(
        cors=SimpleNamespace(enabled=cors),
        auth=SimpleNamespace(type=auth_type, apikey=apikey),
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
    ("auth_type", "signed_in", "payload", "expected"),
    [
        # No credential at all is refused in every mode, including the one with
        # no login configured, which /api has always refused too.
        (None, False, None, False),
        ("form", False, None, False),
        ("basic", False, None, False),
        # The session is enough under form login.
        ("form", True, None, True),
        # The API key is enough in any mode; it is what the client sends and
        # the only credential available with basic auth or no login mode.
        (None, False, {"apiKey": API_KEY}, True),
        ("form", False, {"apiKey": API_KEY}, True),
        ("basic", False, {"apiKey": API_KEY}, True),
        # A wrong key is not.
        ("form", False, {"apiKey": "wrong"}, False),
        (None, False, {"apiKey": ""}, False),
    ],
)
def test_connect_requires_a_session_or_the_api_key(
    monkeypatch, auth_type, signed_in, payload, expected
):
    from app import app as app_module

    monkeypatch.setattr(app_module, "settings", _config(auth_type=auth_type))

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    with flask_app.test_request_context("/api/socket.io/"):
        from flask import session

        if signed_in:
            session["logged_in"] = True

        assert app_module._authorize_socket_connection(payload) is expected


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


def test_an_error_inside_the_gate_refuses_rather_than_admits(monkeypatch):
    """
    python-socketio joins the client to the namespace before calling this
    handler and only catches ConnectionRefusedError around it, so any other
    exception escaping would leave the client in the room receiving events. An
    error here has to mean refuse.
    """
    from app import app as app_module

    monkeypatch.setattr(app_module, "settings", _config(auth_type="form"))

    def boom():
        raise RuntimeError("session backend exploded")

    monkeypatch.setattr(app_module, "is_session_authenticated", boom)

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key"

    with flask_app.test_request_context("/api/socket.io/"):
        assert app_module._authorize_socket_connection() is False

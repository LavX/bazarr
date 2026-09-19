"""
Tests for the CORS switch in create_app.

`settings.get('cors', 'enabled')` reads as "the cors.enabled flag" but is
dynaconf's get(key, default): it returns the whole `cors` section, and a
non-empty dict is truthy. CORS was therefore enabled on every install
regardless of the setting, so every route answered with
Access-Control-Allow-Origin: *, the unauthenticated login endpoint included.
"""
from types import SimpleNamespace

import pytest
from flask import Flask
from flask_cors import CORS


def _app_with_cors_setting(enabled):
    """Reproduce create_app's decision in isolation.

    The real create_app opens a database and a Socket.IO server, neither of
    which this question needs. What matters is the predicate and the resulting
    header, so the test drives the same expression against a bare app.
    """
    from app import app as app_module

    settings = SimpleNamespace(cors=SimpleNamespace(enabled=enabled))
    flask_app = Flask(__name__)

    if app_module.cors_is_enabled(settings):
        CORS(flask_app)

    @flask_app.route("/probe")
    def probe():
        return "ok"

    return flask_app


@pytest.mark.parametrize("enabled", [False, True])
def test_cors_header_follows_the_setting(enabled):
    client = _app_with_cors_setting(enabled).test_client()

    response = client.get("/probe", headers={"Origin": "https://evil.example"})

    has_header = "Access-Control-Allow-Origin" in response.headers
    assert has_header is enabled


def test_the_predicate_reads_the_flag_not_the_section():
    """A truthy section must not be mistaken for a truthy flag."""
    from app.app import cors_is_enabled

    assert cors_is_enabled(SimpleNamespace(cors=SimpleNamespace(enabled=False))) is False
    assert cors_is_enabled(SimpleNamespace(cors=SimpleNamespace(enabled=True))) is True

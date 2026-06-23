"""
Tests for the unauthenticated login-backdrop endpoint (cinematic login screen).

The login page renders pre-auth, so it can't use the auth-gated /images/...
library proxy. Instead it shows TMDB trending backdrops (public catalog art,
never the user's library), like Overseerr/Jellyseerr. The TMDB key stays
server-side; the browser only ever receives public image.tmdb.org URLs.
"""
from types import SimpleNamespace

import pytest
from flask import Flask


@pytest.fixture
def app(monkeypatch):
    from app import ui

    # No auth configured: the endpoint must be reachable regardless.
    monkeypatch.setattr(
        ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None))
    )
    # Start every test from a cold cache so results don't leak between tests.
    monkeypatch.setattr(ui, "_backdrop_cache", {"at": 0.0, "urls": []})
    flask_app = Flask(__name__)
    flask_app.register_blueprint(ui.ui_bp)
    return flask_app


def _tmdb_response(results):
    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": results}

    return Resp()


def test_backdrops_empty_without_key(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_tmdb_api_key", lambda: "")

    def boom(*a, **k):
        raise AssertionError("TMDB must not be called without a key")

    monkeypatch.setattr(ui.requests, "get", boom)

    response = app.test_client().get("/system/backdrops")

    assert response.status_code == 200
    assert response.get_json() == {"backdrops": []}


def test_backdrops_returns_capped_tmdb_urls(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_tmdb_api_key", lambda: "testkey")
    results = [{"backdrop_path": f"/img{i}.jpg"} for i in range(30)]
    monkeypatch.setattr(ui.requests, "get", lambda *a, **k: _tmdb_response(results))

    response = app.test_client().get("/system/backdrops")

    assert response.status_code == 200
    urls = response.get_json()["backdrops"]
    # Capped, and every entry is a public TMDB image URL (no key leaked).
    assert 0 < len(urls) <= 12
    for url in urls:
        assert url.startswith("https://image.tmdb.org/t/p/")
        assert "testkey" not in url
        assert "api_key" not in url


def test_backdrops_skip_results_without_backdrop(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_tmdb_api_key", lambda: "testkey")
    results = [
        {"backdrop_path": "/good.jpg"},
        {"title": "no backdrop here"},
        {"backdrop_path": None},
    ]
    monkeypatch.setattr(ui.requests, "get", lambda *a, **k: _tmdb_response(results))

    urls = app.test_client().get("/system/backdrops").get_json()["backdrops"]

    assert urls == ["https://image.tmdb.org/t/p/original/good.jpg"]


def test_backdrops_cached_between_requests(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_tmdb_api_key", lambda: "testkey")
    calls = {"n": 0}

    def counting_get(*a, **k):
        calls["n"] += 1
        return _tmdb_response([{"backdrop_path": "/a.jpg"}])

    monkeypatch.setattr(ui.requests, "get", counting_get)

    client = app.test_client()
    first = client.get("/system/backdrops").get_json()["backdrops"]
    second = client.get("/system/backdrops").get_json()["backdrops"]

    assert first == second == ["https://image.tmdb.org/t/p/original/a.jpg"]
    # Cached: TMDB is hit only once across both requests.
    assert calls["n"] == 1


def test_backdrops_tmdb_error_returns_empty(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_tmdb_api_key", lambda: "testkey")

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ui.requests, "get", boom)

    response = app.test_client().get("/system/backdrops")

    assert response.status_code == 200
    assert response.get_json() == {"backdrops": []}

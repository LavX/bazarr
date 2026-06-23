"""
Tests for the unauthenticated login-backdrop endpoints (cinematic login screen).

The login page is rendered pre-auth, so the normal auth-gated /images/... proxy
cannot supply library art. These endpoints expose a small, random sample of
library backdrops WITHOUT auth, while remaining safe: only opaque tokens that map
to a known library item resolve to an image; everything else is a 404, and the
browser never sees an arr API key (the bytes are proxied server-side).
"""
from types import SimpleNamespace

import pytest
from flask import Flask


@pytest.fixture
def app(monkeypatch):
    from app import ui

    # No auth configured: the endpoints must be reachable regardless.
    monkeypatch.setattr(
        ui,
        "settings",
        SimpleNamespace(auth=SimpleNamespace(type=None)),
    )
    flask_app = Flask(__name__)
    flask_app.register_blueprint(ui.ui_bp)
    return flask_app


def _row(kind, item_id, fanart):
    """Mimic a SQLAlchemy Row for a backdrop candidate."""
    return SimpleNamespace(id=item_id, fanart=fanart, arr_instance_id=None, kind=kind)


def test_backdrops_list_is_reachable_without_auth_and_capped(app, monkeypatch):
    from app import ui

    # 20 candidate shows/movies with fanart; endpoint must cap the sample.
    rows = [_row("series", i, f"/MediaCover/{i}/fanart.jpg") for i in range(20)]
    monkeypatch.setattr(ui, "_backdrop_candidates", lambda: rows)

    response = app.test_client().get("/system/backdrops")

    assert response.status_code == 200
    payload = response.get_json()
    assert "backdrops" in payload
    assert isinstance(payload["backdrops"], list)
    # Capped to at most 8 entries.
    assert 0 < len(payload["backdrops"]) <= 8
    # Each entry is an opaque backdrop URL (no raw arr URL / api key leaked).
    for url in payload["backdrops"]:
        assert url.startswith("/system/backdrop/")
        assert "apikey" not in url
        assert "http" not in url


def test_backdrops_list_empty_library_returns_empty(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_backdrop_candidates", lambda: [])

    response = app.test_client().get("/system/backdrops")

    assert response.status_code == 200
    assert response.get_json() == {"backdrops": []}


def test_unknown_backdrop_token_returns_404(app, monkeypatch):
    from app import ui

    # No library items at all -> any token is unknown.
    monkeypatch.setattr(ui, "_backdrop_candidates", lambda: [])

    response = app.test_client().get("/system/backdrop/series-999999")

    assert response.status_code == 404


def test_malformed_backdrop_token_returns_404(app, monkeypatch):
    from app import ui

    monkeypatch.setattr(ui, "_backdrop_candidates", lambda: [])

    # Garbage / malformed single-segment tokens must never resolve. The route
    # uses a single-segment <token> converter, so a slash-bearing traversal
    # attempt ("../../etc/passwd") cannot even reach this handler.
    for token in ["garbage", "series-", "-5", "series-abc", "movies-1.5", "evil-1"]:
        response = app.test_client().get(f"/system/backdrop/{token}")
        assert response.status_code == 404, token


def test_known_backdrop_token_streams_image(app, monkeypatch):
    from app import ui

    captured = {}

    rows = [_row("series", 42, "/MediaCover/42/fanart.jpg")]
    monkeypatch.setattr(ui, "_backdrop_candidates", lambda: rows)

    class UpstreamResponse:
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size):
            captured["chunk_size"] = chunk_size
            yield b"fanart-bytes"

    def fake_get(url, stream, timeout, verify, headers):
        captured["url"] = url
        return UpstreamResponse()

    monkeypatch.setattr(ui.requests, "get", fake_get)
    # The token resolves to a sonarr image; the instance-aware url builder keeps
    # the api key server-side.
    monkeypatch.setattr(
        ui,
        "_backdrop_image_url",
        lambda kind, url, arr_instance_id: (
            "https://sonarr.example/api/v3/MediaCover/42/fanart.jpg?apikey=secret",
            True,
        ),
    )

    response = app.test_client().get("/system/backdrop/series-42")

    assert response.status_code == 200
    assert response.data == b"fanart-bytes"
    assert response.content_type == "image/jpeg"
    # The api key stayed server-side.
    assert "secret" in captured["url"]
    assert captured["chunk_size"] == 2048

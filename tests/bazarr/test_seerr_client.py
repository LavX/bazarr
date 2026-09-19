# coding=utf-8
"""Raw Seerr calls: paths, the key header, and status passthrough. Synthetic keys only."""

import json

import pytest

from test_media_server_http import http_fixture as http_fixture


def test_status_and_public_settings_are_plain_gets(http_fixture):
    from seerr.client import SeerrClient
    base, records = http_fixture([
        (200, {"version": "3.3.0"}, {}),
        (200, {"initialized": True, "movie4kEnabled": False}, {}),
    ])
    with SeerrClient(base, "synthetic-key") as client:
        assert client.status() == {"version": "3.3.0"}
        assert client.public_settings()["initialized"] is True
    assert [r["path"] for r in records] == ["/api/v1/status", "/api/v1/settings/public"]
    assert records[0]["headers"]["X-Api-Key"] == "synthetic-key"


def test_me_returns_status_and_body_on_403(http_fixture):
    from seerr.client import SeerrClient
    base, _records = http_fixture([(403, {"status": 403, "error": "denied"}, {})])
    with SeerrClient(base, "synthetic-key") as client:
        assert client.me() == (403, {"status": 403, "error": "denied"})


def test_media_paths_by_type(http_fixture):
    from seerr.client import SeerrClient
    base, records = http_fixture([(200, {"id": 550}, {}), (200, {"id": 1399}, {})])
    with SeerrClient(base, "synthetic-key") as client:
        assert client.media("movie", 550) == (200, {"id": 550})
        assert client.media("tv", 1399) == (200, {"id": 1399})
    assert [r["path"] for r in records] == ["/api/v1/movie/550", "/api/v1/tv/1399"]


def test_media_rejects_unknown_type_without_a_request(http_fixture):
    from seerr.client import SeerrClient
    from media_servers.http import MediaServerError
    base, records = http_fixture([])
    with SeerrClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="internal_error"):
        client.media("show", 1)
    assert records == []


def test_create_request_posts_json_and_returns_any_status(http_fixture):
    from seerr.client import SeerrClient
    base, records = http_fixture([(409, {"message": "Request for this media already exists."}, {})])
    with SeerrClient(base, "synthetic-key") as client:
        status, body = client.create_request({"mediaType": "movie", "mediaId": 550})
    assert status == 409 and body["message"].startswith("Request for")
    assert records[0]["method"] == "POST" and records[0]["path"] == "/api/v1/request"
    assert json.loads(records[0]["body"]) == {"mediaType": "movie", "mediaId": 550}


def test_non_json_body_yields_none(http_fixture):
    from seerr.client import SeerrClient
    base, _records = http_fixture([(403, b"<html>invalid csrf token</html>", {"Content-Type": "text/html"})])
    with SeerrClient(base, "synthetic-key") as client:
        assert client.create_request({"mediaType": "movie", "mediaId": 1}) == (403, None)


def test_missing_key_is_refused_before_any_request():
    from seerr.client import SeerrClient
    from media_servers.http import MediaServerError
    with pytest.raises(MediaServerError, match="missing_credentials"):
        SeerrClient("http://seerr.example", "")

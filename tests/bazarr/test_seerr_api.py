# coding=utf-8
"""Session-authenticated Seerr routes, with Seerr faked at the HTTP layer."""

import pytest
from flask import Flask

from test_media_server_http import http_fixture as http_fixture

HEADERS = {'X-API-KEY': 'synthetic-bazarr-key'}
PUBLIC = {"initialized": True, "hideBlocklisted": False, "movie4kEnabled": False, "series4kEnabled": False,
          "partialRequestsEnabled": True, "enableSpecialEpisodes": False, "applicationUrl": ""}


@pytest.fixture
def seerr_api(monkeypatch, http_fixture):
    from api import api_bp
    from app.config import settings

    def configure(replies):
        base, records = http_fixture(replies)
        monkeypatch.setitem(settings.auth, 'apikey', 'synthetic-bazarr-key')
        monkeypatch.setitem(settings.general, 'use_seerr', True)
        monkeypatch.setitem(settings.seerr, 'url', base)
        monkeypatch.setitem(settings.seerr, 'apikey', 'synthetic-seerr-key')
        monkeypatch.setitem(settings.seerr, 'verify_ssl', True)
        monkeypatch.setitem(settings.seerr, 'external_url', '')
        app = Flask(__name__)
        app.register_blueprint(api_bp)
        return app.test_client(), records
    return configure


def test_routes_require_the_session(seerr_api):
    client, _records = seerr_api([])
    assert client.get('/api/seerr/media/movie/550').status_code == 401
    assert client.post('/api/seerr/request', json={}).status_code == 401


def test_apikey_is_never_read_from_the_url():
    from api.seerr.endpoints import SeerrTestConnection
    for argument in SeerrTestConnection.post_request_parser.args:
        if argument.name == 'apikey':
            location = {argument.location} if isinstance(argument.location, str) else set(argument.location)
            assert not (location & {"values", "args", "query_string"})
            assert {"json", "form"} <= location


def test_test_connection_refuses_the_masked_sentinel(seerr_api):
    client, records = seerr_api([])
    response = client.post('/api/seerr/test-connection', json={"url": "http://seerr:5055", "apikey": "***"},
                           headers=HEADERS)
    assert response.status_code == 200
    assert response.json == {"success": False, "error_code": "configuration"}
    assert records == []


def test_media_status_for_a_movie(seerr_api):
    client, records = seerr_api([(200, PUBLIC, {}), (200, {"id": 550}, {})])
    response = client.get('/api/seerr/media/movie/550', headers=HEADERS)
    assert response.status_code == 200
    assert response.json["configured"] is True and response.json["requestable"] is True
    assert response.json["link"].endswith("/movie/550")
    assert [r["path"] for r in records] == ["/api/v1/settings/public", "/api/v1/movie/550"]


def test_media_status_reports_rejected_key(seerr_api):
    client, _records = seerr_api([(200, PUBLIC, {}), (403, {"status": 403, "error": "denied"}, {})])
    response = client.get('/api/seerr/media/movie/550', headers=HEADERS)
    assert response.status_code == 200 and response.json["error_code"] == "rejected_key"


def test_media_status_reports_unreachable(seerr_api, monkeypatch):
    from app.config import settings
    client, _records = seerr_api([])
    monkeypatch.setitem(settings.seerr, 'url', 'http://127.0.0.1:9')
    response = client.get('/api/seerr/media/movie/550', headers=HEADERS)
    assert response.json["error_code"] == "unreachable"


def test_media_status_not_configured(seerr_api, monkeypatch):
    from app.config import settings
    client, _records = seerr_api([])
    monkeypatch.setitem(settings.general, 'use_seerr', False)
    assert client.get('/api/seerr/media/movie/550', headers=HEADERS).json == {"configured": False,
                                                                              "error_code": "not_configured"}


def test_media_status_by_tvdb(seerr_api, monkeypatch):
    from seerr import operations
    monkeypatch.setattr(operations, 'tmdb_id_for_tvdb', lambda tvdb: 1399 if tvdb == 121361 else None)
    client, records = seerr_api([(200, PUBLIC, {}), (200, {"id": 1399}, {})])
    assert client.get('/api/seerr/media/tv/by-tvdb/121361', headers=HEADERS).json["link"].endswith("/tv/1399")
    assert records[-1]["path"] == "/api/v1/tv/1399"
    client, _records = seerr_api([])
    assert client.get('/api/seerr/media/tv/by-tvdb/5', headers=HEADERS).json["error_code"] == "unresolved"


def test_request_movie_posts_and_maps_outcome(seerr_api):
    client, records = seerr_api([(200, PUBLIC, {}),
                                 (201, {"id": 12, "status": 2, "is4k": False, "seasons": []}, {})])
    response = client.post('/api/seerr/request', json={"media_type": "movie", "tmdb_id": 550}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json["outcome"] == "requested" and response.json["link"].endswith("/movie/550")
    import json
    assert json.loads(records[-1]["body"]) == {"mediaType": "movie", "mediaId": 550, "is4k": False}


def test_request_show_sends_seasons_and_tvdb(seerr_api):
    import json
    client, records = seerr_api([(200, PUBLIC, {}), (202, {"message": "No seasons available to request"}, {})])
    body = {"media_type": "tv", "tmdb_id": 1399, "tvdb_id": 121361, "seasons": [1, 2]}
    response = client.post('/api/seerr/request', json=body, headers=HEADERS)
    assert response.json["outcome"] == "nothing_to_request"
    assert json.loads(records[-1]["body"]) == {"mediaType": "tv", "mediaId": 1399, "tvdbId": 121361,
                                               "seasons": [1, 2], "is4k": False}


@pytest.mark.parametrize("body", [
    {}, {"media_type": "show", "tmdb_id": 1}, {"media_type": "movie", "tmdb_id": "550"},
    {"media_type": "tv", "tmdb_id": 1, "seasons": "some"}, {"media_type": "tv", "tmdb_id": 1, "seasons": [-1]},
])
def test_request_validation(seerr_api, body):
    client, _records = seerr_api([])
    assert client.post('/api/seerr/request', json=body, headers=HEADERS).status_code == 400


def test_request_error_bodies_never_echo_the_key(seerr_api):
    client, _records = seerr_api([(200, PUBLIC, {}), (500, b"synthetic-seerr-key", {})])
    response = client.post('/api/seerr/request', json={"media_type": "movie", "tmdb_id": 1}, headers=HEADERS)
    assert response.json == {"error_code": "upstream_error"}
    assert "synthetic-seerr-key" not in response.get_data(as_text=True)

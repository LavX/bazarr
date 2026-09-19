import pytest
from flask import Flask

from test_media_server_http import http_fixture as http_fixture


@pytest.fixture
def api_client(monkeypatch):
    from api import api_bp
    from app.config import settings
    monkeypatch.setitem(settings.auth, "apikey", "synthetic-bazarr-key")
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


@pytest.mark.parametrize("encoding", ["json", "data"])
def test_current_unsaved_body_values_reach_server(api_client, http_fixture, encoding):
    base, records = http_fixture([(200, {"ServerName": "Unsaved", "Version": "4.9.5.0"}, {})])
    body = {"url": base + "/unsaved", "apikey": "synthetic-unsaved-key", "verify_ssl": "false"}
    response = api_client.post("/api/emby/test-connection?url=http://wrong&apikey=wrong&verify_ssl=wrong",
                               headers={"X-API-KEY": "synthetic-bazarr-key"}, **{encoding: body})
    assert response.status_code == 200
    assert response.json["success"] is True
    assert records[0]["path"] == "/unsaved/System/Info"
    assert records[0]["headers"]["X-Emby-Token"] == "synthetic-unsaved-key"


def test_bazarr_authentication_required(api_client):
    response = api_client.post("/api/emby/test-connection", json={"url": "http://server", "apikey": "synthetic-key"})
    assert response.status_code == 401


@pytest.mark.parametrize('server', ['emby', 'silo'])
def test_ambiguous_singleton_status_and_retry_routes_are_retired(api_client, server):
    headers = {'X-API-KEY': 'synthetic-bazarr-key'}
    assert api_client.get(f'/api/{server}/status', headers=headers).status_code == 404
    assert api_client.post(f'/api/{server}/retry-pending', headers=headers).status_code == 404


@pytest.mark.parametrize("body", [{}, {"url": "http://server"}, {"url": "", "apikey": "synthetic-key"},
                                 {"url": "http://server", "apikey": ""}, {"url": "http://server", "apikey": 7},
                                 {"url": "http://server", "apikey": "synthetic-key", "verify_ssl": "maybe"},
                                 {"url": "http://server", "apikey": "synthetic-key", "verify_ssl": 0}])
def test_invalid_body_never_uses_query_or_saved_key(api_client, body):
    response = api_client.post("/api/emby/test-connection?url=http://server&apikey=synthetic-key",
                               headers={"X-API-KEY": "synthetic-bazarr-key"}, json=body)
    assert response.status_code == 400
    assert response.json["success"] is False
    assert response.json["error_code"] in {"missing_credentials", "invalid_verify_ssl"}

import pytest

from test_emby_api import api_client as api_client
from test_media_server_http import http_fixture as http_fixture
from test_silo_client import HEALTH, LIBRARIES


@pytest.mark.parametrize("route", ["test-connection", "libraries"])
@pytest.mark.parametrize("encoding", ["json", "data"])
def test_silo_endpoints_use_unsaved_body_only(api_client, http_fixture, route, encoding):
    replies = [(200, HEALTH, {}), (200, LIBRARIES, {})] if route == "test-connection" else [(200, LIBRARIES, {})]
    base, records = http_fixture(replies)
    response = api_client.post(f"/api/silo/{route}?url=http://wrong&apikey=wrong&verify_ssl=wrong",
                               headers={"X-API-KEY": "synthetic-bazarr-key"}, **{encoding: {
                                   "url": base + "/unsaved", "apikey": "synthetic-unsaved-key", "verify_ssl": "false"}})
    assert response.status_code == 200
    assert response.json.get("success", True) is True
    assert response.json.get("error_code") is None
    assert records[-1]["path"] == "/unsaved/api/v1/libraries"
    assert records[-1]["headers"]["Authorization"] == "Bearer synthetic-unsaved-key"


@pytest.mark.parametrize("route", ["test-connection", "libraries"])
def test_bazarr_authentication_required_for_both_native_routes(api_client, route):
    response = api_client.post(f"/api/silo/{route}", json={"url": "http://server", "apikey": "synthetic-key"})
    assert response.status_code == 401


@pytest.mark.parametrize("route", ["test-connection", "libraries"])
@pytest.mark.parametrize("body", [{}, [], {"url": "http://server"}, {"url": "", "apikey": "synthetic-key"},
                                 {"url": "http://server", "apikey": 7},
                                 {"url": "http://server", "apikey": "synthetic-key", "verify_ssl": "maybe"},
                                 {"url": "http://server", "apikey": "synthetic-key", "verify_ssl": 0}])
def test_invalid_body_cannot_fall_back_to_query_or_saved_values(api_client, route, body):
    response = api_client.post(f"/api/silo/{route}?url=http://server&apikey=synthetic-key",
                               headers={"X-API-KEY": "synthetic-bazarr-key"}, json=body)
    assert response.status_code == 400
    assert response.json["error_code"] in {"missing_credentials", "invalid_verify_ssl"}
    if route == "libraries":
        assert response.json["data"] == []
    else:
        assert response.json["success"] is False

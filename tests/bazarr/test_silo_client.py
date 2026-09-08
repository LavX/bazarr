"""Native Silo connection contracts with synthetic credentials."""

import pytest

from test_media_server_http import http_fixture as http_fixture


HEALTH = {"server_id": "synthetic-server", "server_name": "Native Silo", "status": "ok"}
LIBRARIES = [
    {"id": 7, "name": "Movies", "type": "movies", "paths": ["/media/movies"], "enabled": True},
    {"id": 8, "name": "Shows", "type": "series", "paths": ["/media/shows"], "enabled": True},
    {"id": 9, "name": "Music", "type": "music", "paths": ["/media/music"], "enabled": True},
    {"id": 10, "name": "Offline", "type": "movies", "paths": ["/offline"], "enabled": False},
]


def test_native_test_requires_authenticated_libraries_and_omits_absent_version(http_fixture, monkeypatch):
    from silo.operations import silo_test_connection
    base, records = http_fixture([(200, HEALTH, {}), (200, LIBRARIES, {})])
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    result = silo_test_connection(base + "/reverse/silo/", "synthetic-key")
    assert result == {"success": True, "server_id": "synthetic-server", "server_name": "Native Silo"}
    assert [record["path"] for record in records] == [
        "/reverse/silo/api/v1/health", "/reverse/silo/api/v1/libraries"]
    assert records[1]["headers"]["Authorization"] == "Bearer synthetic-key"


@pytest.mark.parametrize(("status", "code"), [(401, "unauthorized"), (403, "forbidden")])
def test_public_health_does_not_prove_key_or_admin_scope(http_fixture, status, code):
    from silo.operations import silo_test_connection
    base, records = http_fixture([(200, HEALTH, {}), (status, b"synthetic-sensitive-detail", {})])
    assert silo_test_connection(base, "synthetic-key") == {"success": False, "error_code": code}
    assert len(records) == 2


def test_library_selector_uses_native_shapes_string_ids_and_enabled_media_types(http_fixture):
    from silo.operations import silo_get_libraries
    base, _records = http_fixture([(200, LIBRARIES, {})])
    assert silo_get_libraries(base, "synthetic-key") == {"data": [
        {"id": "7", "name": "Movies", "type": "movies", "paths": ["/media/movies"]},
        {"id": "8", "name": "Shows", "type": "series", "paths": ["/media/shows"]},
    ], "error_code": None}


@pytest.mark.parametrize("body", [[], {}, {**HEALTH, "status": "unhealthy"},
                                 {**HEALTH, "server_id": 7}, {"status": "ok", "server_name": "Name"}])
def test_invalid_health_is_rejected(http_fixture, body):
    from silo.operations import silo_test_connection
    base, _records = http_fixture([(200, body, {})])
    assert silo_test_connection(base, "synthetic-key") == {"success": False, "error_code": "invalid_response"}


@pytest.mark.parametrize("body", [{"libraries": LIBRARIES}, None, [None],
                                 [{**LIBRARIES[0], "id": "7"}], [{**LIBRARIES[0], "id": True}],
                                 [{**LIBRARIES[0], "paths": "/media"}],
                                 [{**LIBRARIES[0], "paths": [3]}], [{**LIBRARIES[0], "enabled": "true"}],
                                 [{**LIBRARIES[0], "type": None}], [LIBRARIES[0], LIBRARIES[0]]])
def test_invalid_native_libraries_are_not_success_or_partial_data(http_fixture, body):
    from silo.operations import silo_get_libraries, silo_test_connection
    body = b"null" if body is None else body
    base, _records = http_fixture([(200, body, {}), (200, HEALTH, {}), (200, body, {})])
    assert silo_get_libraries(base, "synthetic-key") == {"data": [], "error_code": "invalid_response"}
    assert silo_test_connection(base, "synthetic-key") == {"success": False, "error_code": "invalid_response"}


@pytest.mark.parametrize("apikey", [None, 123, "", " ", "synthetic\nkey"])
def test_invalid_credentials_are_sanitized(apikey):
    from silo.operations import silo_test_connection
    assert silo_test_connection("http://server", apikey) == {"success": False, "error_code": "missing_credentials"}


def test_library_errors_and_redirects_are_sanitized_without_following(http_fixture):
    from silo.operations import silo_get_libraries
    target, target_records = http_fixture([])
    base, _records = http_fixture([(307, b"", {"Location": target + "/capture"}),
                                  (500, b"synthetic-private-response", {})])
    assert silo_get_libraries(base, "synthetic-key") == {"data": [], "error_code": "redirect_denied"}
    assert silo_get_libraries(base, "synthetic-key") == {"data": [], "error_code": "server_error"}
    assert target_records == []

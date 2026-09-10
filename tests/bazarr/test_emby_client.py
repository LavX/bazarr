import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_media_server_http import http_fixture as http_fixture

# Emby names the two libraries Bazarr publishes into. Both resolve by exact
# file path and refresh identically; only the queried type differs.
ITEM_TYPES = {"movie": "Movie", "episode": "Episode"}
MEDIA_TYPES = list(ITEM_TYPES)


@pytest.mark.parametrize('media_type', MEDIA_TYPES)
@pytest.mark.parametrize('phase', ['lookup', 'post', 'completion'])
def test_revision_guard_prevents_later_authenticated_requests(monkeypatch, phase, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    requests = []
    valid = phase != 'lookup'

    def guard():
        if not valid:
            raise MediaServerError('configuration_changed')

    with EmbyClient('http://emby.example', 'synthetic-key') as client:
        def lookup(*args, **kwargs):
            nonlocal valid
            requests.append('lookup')
            if phase == 'post':
                valid = False
            return {'Items': [{'Id': '1', 'Type': ITEM_TYPES[media_type], 'Path': '/media/A.mkv'}]}

        def post(*args, **kwargs):
            nonlocal valid
            requests.append('post')
            valid = False

        monkeypatch.setattr(client.http, 'request_json', lookup)
        monkeypatch.setattr(client.http, 'request_empty', post)
        with pytest.raises(MediaServerError, match='configuration_changed'):
            client.refresh_item(media_type, '/media/A.mkv', ensure_current=guard)
    assert requests == {'lookup': [], 'post': ['lookup'], 'completion': ['lookup', 'post']}[phase]


def test_authenticated_connection_uses_system_info(http_fixture):
    from emby.operations import emby_test_connection
    base, records = http_fixture([(200, {"ServerName": "Fixture", "Version": "4.9.5.0"}, {})])
    assert emby_test_connection(base + "/emby", "synthetic-key") == {
        "success": True, "server_name": "Fixture", "version": "4.9.5.0"}
    assert records[0]["path"] == "/emby/System/Info"
    assert records[0]["headers"]["X-Emby-Token"] == "synthetic-key"


def test_invalid_key_returns_only_a_sanitized_code(http_fixture):
    from emby.operations import emby_test_connection
    base, _records = http_fixture([(401, b"synthetic-sensitive-response", {})])
    assert emby_test_connection(base, "synthetic-invalid-key") == {"success": False, "error_code": "unauthorized"}


@pytest.mark.parametrize("body", [[], {}, {"ServerName": 1, "Version": "4"}, {"ServerName": "Fixture"}])
def test_bad_system_info_is_not_connection_success(http_fixture, body):
    from emby.operations import emby_test_connection
    base, _records = http_fixture([(200, body, {})])
    assert emby_test_connection(base, "synthetic-key") == {"success": False, "error_code": "invalid_response"}


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize("status", [200, 204])
def test_refresh_resolves_exact_path_and_encodes_opaque_item_id(http_fixture, status, media_type):
    from emby.client import EmbyClient
    base, records = http_fixture([
        (200, {"Items": [{"Id": "edition/a?x=#", "Type": ITEM_TYPES[media_type], "Path": "/media/A.mkv"}],
               "TotalRecordCount": 1}, {}),
        (status, b"", {}),
    ])
    with EmbyClient(base + "/emby", "synthetic-key") as client:
        assert client.refresh_item(media_type, "/media/A.mkv") == {"status": "requested"}
    lookup = urlsplit(records[0]["path"])
    assert lookup.path == "/emby/Items"
    query = parse_qs(lookup.query)
    assert query["Path"] == ["/media/A.mkv"]
    assert query["IncludeItemTypes"] == [ITEM_TYPES[media_type]]
    assert query["Recursive"] == ["true"]
    assert "MediaSources" in query["Fields"][0].split(",")
    refresh = urlsplit(records[1]["path"])
    assert records[1]["method"] == "POST"
    assert refresh.path == "/emby/Items/edition%2Fa%3Fx%3D%23/Refresh"
    assert parse_qs(refresh.query) == {
        "Recursive": ["false"], "MetadataRefreshMode": ["ValidationOnly"],
        "ImageRefreshMode": ["ValidationOnly"], "ReplaceAllMetadata": ["false"], "ReplaceAllImages": ["false"],
    }
    assert json.loads(records[1]["body"]) == {"ReplaceThumbnailImages": False}
    assert records[1]["headers"]["X-Emby-Token"] == "synthetic-key"


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_media_source_exact_path_can_identify_item(http_fixture, media_type):
    from emby.client import EmbyClient
    base, records = http_fixture([
        (200, {"Items": [{"Id": "opaque", "Type": ITEM_TYPES[media_type], "Path": "/media/other.mkv",
                          "MediaSources": [{"Path": "/media/A.mkv"}]}], "TotalRecordCount": 1}, {}),
        (204, b"", {}),
    ])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_item(media_type, "/media/A.mkv")["status"] == "requested"
    assert len(records) == 2


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize(("items", "code"), [
    ([], "item_missing"),
    ([{"Id": "1", "Path": "/media/other.mkv", "ProviderIds": {"Tmdb": "1"}}], "item_missing"),
    ([{"Id": "1", "Path": "/media/A.mkv"}, {"Id": "2", "Path": "/media/A.mkv"}], "item_ambiguous"),
    ([{"Id": 1, "Path": "/media/A.mkv"}], "invalid_response"),
])
def test_non_unique_item_never_triggers_refresh(http_fixture, items, code, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    items = [{**item, "Type": ITEM_TYPES[media_type]} for item in items]
    base, records = http_fixture([(200, {"Items": items, "TotalRecordCount": len(items)}, {})])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match=code):
        client.refresh_item(media_type, "/media/A.mkv")
    assert len(records) == 1


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_other_library_type_at_the_same_path_is_never_refreshed(http_fixture, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    other = ITEM_TYPES["episode" if media_type == "movie" else "movie"]
    base, records = http_fixture([
        (200, {"Items": [{"Id": "1", "Type": other, "Path": "/media/A.mkv"}], "TotalRecordCount": 1}, {})])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="item_missing"):
        client.refresh_item(media_type, "/media/A.mkv")
    assert len(records) == 1


@pytest.mark.parametrize("media_type", ["", "series", "movies", "Movie", "episodes", None])
def test_unknown_media_type_never_reaches_the_server(http_fixture, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    base, records = http_fixture([])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="internal_error"):
        client.refresh_item(media_type, "/media/A.mkv")
    assert records == []


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize(("status", "body", "code"), [(202, b"", "request_rejected"), (200, b"{}", "invalid_response"),
                                                       (403, b"", "forbidden")])
def test_refresh_acceptance_is_strict_and_has_no_broader_fallback(http_fixture, status, body, code, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    base, records = http_fixture([
        (200, {"Items": [{"Id": "1", "Type": ITEM_TYPES[media_type], "Path": "/media/A.mkv"}],
               "TotalRecordCount": 1}, {}),
        (status, body, {}),
    ])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match=code):
        client.refresh_item(media_type, "/media/A.mkv")
    assert len(records) == 2


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize("result", [
    {"Items": {}},
    {"Items": [{"Id": "1", "Path": "/media/A.mkv"}], "TotalRecordCount": 2},
    {"Items": [{"Id": "1", "Path": "/media/A.mkv", "MediaSources": {}}]},
    {"Items": [{"Id": "\ud800", "Path": "/media/A.mkv"}]},
], ids=["items-shape", "partial-result", "sources-shape", "invalid-id-encoding"])
def test_malformed_lookup_cannot_refresh_or_leak_raw_errors(http_fixture, result, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    if isinstance(result["Items"], list):
        result = {**result, "Items": [{**item, "Type": ITEM_TYPES[media_type]} for item in result["Items"]]}
    base, records = http_fixture([(200, result, {})])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="invalid_response"):
        client.refresh_item(media_type, "/media/A.mkv")
    assert len(records) == 1


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize("path", ["/movies/D:/A.mkv", "/movies/C:/A.mkv", "/movies/D:A.mkv", "/movies/C:A.mkv"],
                         ids=["same-drive", "other-drive", "same-drive-filename", "other-drive-filename"])
def test_reinterpreted_mapping_cannot_refresh_a_different_item(http_fixture, path, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    from media_servers.paths import map_media_path
    base, records = http_fixture([
        (200, {"Items": [{"Id": "wrong-file", "Type": ITEM_TYPES[media_type], "Path": "D:\\Library\\A.mkv"}],
               "TotalRecordCount": 1}, {}),
        (204, b"", {}),
    ])
    rows = [{"local_path": "/movies", "remote_path": "D:\\Library"}]
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="path_invalid"):
        mapped = map_media_path(path, rows)
        client.refresh_item(media_type, mapped["path"])
    assert records == []

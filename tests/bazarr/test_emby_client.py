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


# --------------------------------------------------- identifier and title rungs

PARENT_TYPES = {"movie": "Movie", "episode": "Series"}
SERIES = {"Id": "35", "Type": "Series", "Name": "Firefly", "Path": "/media/Firefly"}
EPISODES = {"Items": [
    {"Id": "37", "Type": "Episode", "Name": "Serenity", "IndexNumber": 1, "ParentIndexNumber": 1},
    {"Id": "38", "Type": "Episode", "Name": "The Train Job", "IndexNumber": 2, "ParentIndexNumber": 1},
], "TotalRecordCount": 2}


def metadata(**overrides):
    from media_servers.resolution import MediaMetadata
    values = dict(imdb_id="tt0303461", tmdb_id="19", tvdb_id=78874, title="Firefly",
                  year=2002, season=1, episode=2)
    return MediaMetadata(**{**values, **overrides})


def query(record):
    return parse_qs(urlsplit(record["path"]).query)


def test_declared_rungs_are_the_whole_jellyfin_ladder():
    from emby.client import EmbyClient
    from media_servers import resolution
    assert EmbyClient.REFRESH_STEPS == resolution.CHAIN


@pytest.mark.parametrize(("media_type", "provider"), [("movie", "tmdb.19"), ("episode", "tvdb.78874")])
def test_provider_lookup_tries_imdb_first_then_the_type_specific_id(http_fixture, media_type, provider):
    from emby.client import EmbyClient
    found = {"Id": "opaque", "Type": PARENT_TYPES[media_type], "Name": "Firefly"}
    replies = [(200, {"Items": [], "TotalRecordCount": 0}, {}),
               (200, {"Items": [found], "TotalRecordCount": 1}, {})]
    if media_type == "episode":
        replies.append((200, EPISODES, {}))
    replies.append((204, b"", {}))
    base, records = http_fixture(replies)
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_provider_id(media_type, metadata()) == {"status": "requested"}
    assert query(records[0])["AnyProviderIdEquals"] == ["imdb.tt0303461"]
    assert query(records[0])["IncludeItemTypes"] == [PARENT_TYPES[media_type]]
    assert query(records[0])["Recursive"] == ["true"]
    assert "ProviderIds" in query(records[0])["Fields"][0].split(",")
    assert query(records[1])["AnyProviderIdEquals"] == [provider]
    assert urlsplit(records[-1]["path"]).path == (
        "/Items/38/Refresh" if media_type == "episode" else "/Items/opaque/Refresh")
    assert parse_qs(urlsplit(records[-1]["path"]).query)["Recursive"] == ["false"]


def test_provider_lookup_stops_at_the_first_identifier_that_resolves(http_fixture):
    from emby.client import EmbyClient
    base, records = http_fixture([
        (200, {"Items": [{"Id": "1", "Type": "Movie", "Name": "Metropolis"}], "TotalRecordCount": 1}, {}),
        (204, b"", {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_provider_id("movie", metadata()) == {"status": "requested"}
    assert len(records) == 2
    assert query(records[0])["AnyProviderIdEquals"] == ["imdb.tt0303461"]


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_provider_lookup_without_usable_identifiers_never_reaches_the_server(http_fixture, media_type):
    from emby.client import EmbyClient
    base, records = http_fixture([])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_provider_id(media_type, metadata(imdb_id=None, tmdb_id=None, tvdb_id=None)) is None
        # An episode with no numbers cannot be reached even through its series.
        assert client.refresh_by_provider_id("episode", metadata(season=None, episode=None)) is None
    assert records == []


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize(("items", "reason"), [
    ([], "no-match"),
    ([{"Id": "1", "Name": "Firefly"}, {"Id": "2", "Name": "Firefly"}], "ambiguous"),
], ids=["no-match", "ambiguous"])
def test_a_non_unique_identifier_match_misses_instead_of_guessing(http_fixture, items, reason, media_type):
    from emby.client import EmbyClient
    items = [{**item, "Type": PARENT_TYPES[media_type]} for item in items]
    replies = [(200, {"Items": items, "TotalRecordCount": len(items)}, {})] * 2
    base, records = http_fixture(replies)
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_provider_id(media_type, metadata()) is None
    assert len(records) == 2


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_title_lookup_narrows_by_name_and_year_then_matches_the_name_exactly(http_fixture, media_type):
    from emby.client import EmbyClient
    items = [{"Id": "wrong", "Type": PARENT_TYPES[media_type], "Name": "Firefly Lane"},
             {"Id": "right", "Type": PARENT_TYPES[media_type], "Name": "FIREFLY"}]
    replies = [(200, {"Items": items, "TotalRecordCount": 2}, {})]
    if media_type == "episode":
        replies.append((200, EPISODES, {}))
    replies.append((204, b"", {}))
    base, records = http_fixture(replies)
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_title_year(media_type, metadata()) == {"status": "requested"}
    assert query(records[0])["NameStartsWith"] == ["Firefly"]
    assert query(records[0])["Years"] == ["2002"]
    assert query(records[0])["IncludeItemTypes"] == [PARENT_TYPES[media_type]]
    assert urlsplit(records[-1]["path"]).path == (
        "/Items/38/Refresh" if media_type == "episode" else "/Items/right/Refresh")


def test_title_lookup_omits_an_unknown_year_and_misses_without_a_title(http_fixture):
    from emby.client import EmbyClient
    base, records = http_fixture([
        (200, {"Items": [{"Id": "1", "Type": "Movie", "Name": "Firefly"}], "TotalRecordCount": 1}, {}),
        (204, b"", {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_title_year("movie", metadata(title=None)) is None
        assert client.refresh_by_title_year("movie", metadata(year=None)) == {"status": "requested"}
    assert "Years" not in query(records[0])
    assert len(records) == 2


def test_a_series_without_the_published_episode_misses(http_fixture):
    from emby.client import EmbyClient
    base, records = http_fixture([
        (200, {"Items": [SERIES], "TotalRecordCount": 1}, {}),
        (200, {"Items": [], "TotalRecordCount": 0}, {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_title_year("episode", metadata(episode=9)) is None
    assert urlsplit(records[1]["path"]).path == "/Shows/35/Episodes"
    assert query(records[1])["Season"] == ["1"]


def test_an_episode_of_another_season_is_never_refreshed(http_fixture):
    from emby.client import EmbyClient
    other = {"Items": [{"Id": "99", "Type": "Episode", "IndexNumber": 2, "ParentIndexNumber": 4}],
             "TotalRecordCount": 1}
    base, records = http_fixture([(200, {"Items": [SERIES], "TotalRecordCount": 1}, {}), (200, other, {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_by_title_year("episode", metadata()) is None
    assert len(records) == 2


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
def test_identifier_rungs_reject_a_partial_or_malformed_response(http_fixture, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    items = [{"Id": "1", "Type": PARENT_TYPES[media_type], "Name": "Firefly"}]
    base, records = http_fixture([(200, {"Items": items, "TotalRecordCount": 4}, {})])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="invalid_response"):
        client.refresh_by_provider_id(media_type, metadata())
    assert len(records) == 1


@pytest.mark.parametrize("media_type", MEDIA_TYPES)
@pytest.mark.parametrize("phase", ["lookup", "post"])
def test_identifier_rungs_honour_the_revision_guard(monkeypatch, media_type, phase):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    requests = []
    valid = phase != "lookup"

    def guard():
        if not valid:
            raise MediaServerError("configuration_changed")

    with EmbyClient("http://emby.example", "synthetic-key") as client:
        def lookup(_method, path, **_kwargs):
            nonlocal valid
            requests.append(path)
            if phase == "post":
                valid = False
            if path.startswith("/Shows/"):
                return EPISODES
            return {"Items": [{"Id": "35", "Type": PARENT_TYPES[media_type], "Name": "Firefly"}],
                    "TotalRecordCount": 1}

        monkeypatch.setattr(client.http, "request_json", lookup)
        monkeypatch.setattr(client.http, "request_empty", lambda *args, **kwargs: requests.append("post"))
        with pytest.raises(MediaServerError, match="configuration_changed"):
            client.refresh_by_provider_id(media_type, metadata(), ensure_current=guard)
    assert requests == [] if phase == "lookup" else requests[0] == "/Items"


# ------------------------------------------------------------- library rung

FOLDERS = [
    {"Name": "Movies A1", "ItemId": "3", "CollectionType": "movies", "Locations": ["/media/movies"]},
    {"Name": "Movies A2", "ItemId": "5", "CollectionType": "movies", "Locations": ["/other"]},
    {"Name": "TV A", "ItemId": "19", "CollectionType": "tvshows", "Locations": ["/media/series"]},
    {"Name": "Music", "ItemId": "21", "CollectionType": "music", "Locations": ["/media"]},
]


@pytest.mark.parametrize(("media_type", "path", "item_id"), [
    ("movie", "/media/movies/A.mkv", "3"),
    ("episode", "/media/series/Firefly/S01E02.mkv", "19"),
])
def test_library_rung_refreshes_only_the_library_that_holds_the_file(http_fixture, media_type, path, item_id):
    from emby.client import EmbyClient
    base, records = http_fixture([(200, FOLDERS, {}), (204, b"", {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_library(media_type, path) == {"status": "requested"}
    assert urlsplit(records[0]["path"]).path == "/Library/VirtualFolders"
    assert urlsplit(records[1]["path"]).path == f"/Items/{item_id}/Refresh"
    refresh = query(records[1])
    assert refresh["Recursive"] == ["true"]
    assert refresh["MetadataRefreshMode"] == ["ValidationOnly"]
    assert refresh["ReplaceAllMetadata"] == ["false"]


@pytest.mark.parametrize(("media_type", "path"), [
    ("movie", "/nowhere/A.mkv"),
    ("movie", "/media/series/Firefly/S01E02.mkv"),
    ("episode", "/media/movies/A.mkv"),
])
def test_library_rung_misses_when_no_library_of_that_type_holds_the_file(http_fixture, media_type, path):
    from emby.client import EmbyClient
    base, records = http_fixture([(200, FOLDERS, {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_library(media_type, path) is None
    assert len(records) == 1


@pytest.mark.parametrize("folders", [
    {"Items": FOLDERS}, [None], [{**FOLDERS[0], "ItemId": 3}], [{**FOLDERS[0], "Locations": "/media/movies"}],
], ids=["not-a-list", "not-a-folder", "non-string-id", "locations-shape"])
def test_library_rung_rejects_a_malformed_folder_listing(http_fixture, folders):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    base, records = http_fixture([(200, folders, {})])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="invalid_response"):
        client.refresh_library("movie", "/media/movies/A.mkv")
    assert len(records) == 1


@pytest.mark.parametrize("media_type", ["", "series", None])
def test_library_rung_rejects_an_unknown_media_type_before_any_request(http_fixture, media_type):
    from emby.client import EmbyClient
    from media_servers.http import MediaServerError
    base, records = http_fixture([])
    with EmbyClient(base, "synthetic-key") as client, pytest.raises(MediaServerError, match="internal_error"):
        client.refresh_library(media_type, "/media/movies/A.mkv")
    assert records == []


def test_a_windows_library_root_never_swallows_a_posix_path(http_fixture):
    from emby.client import EmbyClient
    folders = [{"Name": "Movies", "ItemId": "3", "CollectionType": "movies", "Locations": ["D:\\Library"]}]
    base, records = http_fixture([(200, folders, {})])
    with EmbyClient(base, "synthetic-key") as client:
        assert client.refresh_library("movie", "/media/movies/A.mkv") is None
    assert len(records) == 1

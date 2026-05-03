"""Tests for Jellyfin operations using FakeJellyfinClient.

All test data is validated against Jellyfin's OpenAPI spec via the fake client.
"""

from unittest.mock import patch, MagicMock

import pytest

# Operations module-level binds `from app.config import settings`, which holds
# real bazarr config in batched test runs. The fixtures below patch
# `bazarr.jellyfin.operations.settings` per-test so each test sees a fresh
# MagicMock without leaking into compat/notifier/other tests that depend on
# the real settings module. Avoid sys.modules-level mocks here - those cross
# test files and produce spectacular order-dependent failures (the original
# upstream design used `sys.modules.setdefault("app.config", ...)` which
# becomes a no-op once any other test imports app.config and so the mock
# never won when run with the broader suite).
import jellyfin.operations as _ops_module  # noqa: E402, RUF100

from jellyfin.operations import (  # noqa: E402, RUF100
    jellyfin_test_connection,
    jellyfin_get_libraries,
    jellyfin_refresh_item,
    jellyfin_refresh_all_libraries,
    jellyfin_update_library,
)
from fake_jellyfin import FakeJellyfinClient, make_movie, make_series, make_episode  # noqa: E402, RUF100



@pytest.fixture(autouse=True)
def _isolated_settings():
    """Replace `bazarr.jellyfin.operations.settings` with a fresh MagicMock for
    every test, then restore. Keeps test pollution scoped to this file."""
    original = _ops_module.settings
    fresh = MagicMock()
    _ops_module.settings = fresh
    try:
        yield fresh
    finally:
        _ops_module.settings = original


@pytest.fixture
def fake():
    """Provide a FakeJellyfinClient and patch get_jellyfin_client to return it."""
    client = FakeJellyfinClient()
    with patch("jellyfin.operations.get_jellyfin_client", return_value=client):
        yield client


@pytest.fixture
def settings(_isolated_settings):
    return _isolated_settings



def test_test_connection_success():
    with patch("jellyfin.operations.get_jellyfin_client") as mock_get:
        client = FakeJellyfinClient()
        mock_get.return_value = client
        result = jellyfin_test_connection()
    assert result["success"] is True
    assert result["server_name"] == "TestServer"
    assert result["version"] == "10.12.0"


def test_test_connection_failure():
    """Connection failures surface a coarse error_code, not the raw exception
    text. Server banners, URLs, and any echoed Authorization data must stay
    server-side (logged after redaction). The api_key in particular must NEVER
    appear in the response payload."""
    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=Exception("refused at https://jf:8096 SECRET")):
        result = jellyfin_test_connection()
    assert result["success"] is False
    assert result.get("error_code") == "connection_failed"
    # Raw exception text never leaks
    assert "refused" not in str(result)
    assert "SECRET" not in str(result)
    # Old error key removed
    assert "error" not in result


def test_test_connection_configuration_error():
    """Missing url / apikey are configuration errors (raised by our own
    get_jellyfin_client). Surface a distinct error_code so the UI can prompt
    correctly."""
    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=ValueError("Jellyfin URL not configured.")):
        result = jellyfin_test_connection()
    assert result["success"] is False
    assert result.get("error_code") == "configuration"


def test_test_connection_with_explicit_params():
    with patch("jellyfin.operations.get_jellyfin_client") as mock_get:
        mock_get.return_value = FakeJellyfinClient()
        jellyfin_test_connection(url="http://custom:8096", apikey="key")
        mock_get.assert_called_once_with("http://custom:8096", "key",
                                          verify_ssl=None)


def test_test_connection_passes_verify_ssl_override():
    """When the UI toggle is honored without saving first, the override must
    flow all the way to JellyfinClient via get_jellyfin_client."""
    with patch("jellyfin.operations.get_jellyfin_client") as mock_get:
        mock_get.return_value = FakeJellyfinClient()
        jellyfin_test_connection(url="https://j", apikey="k", verify_ssl=False)
        mock_get.assert_called_once_with("https://j", "k", verify_ssl=False)


def test_get_libraries_passes_verify_ssl_override():
    with patch("jellyfin.operations.get_jellyfin_client") as mock_get:
        mock_get.return_value = FakeJellyfinClient()
        jellyfin_get_libraries(url="https://j", apikey="k", verify_ssl=False)
        mock_get.assert_called_once_with("https://j", "k", verify_ssl=False)



def test_get_libraries_filters_by_collection_type():
    with patch("jellyfin.operations.get_jellyfin_client") as mock_get:
        client = FakeJellyfinClient()
        client.libraries.append({
            "Name": "Music", "Locations": [], "CollectionType": "music", "ItemId": "lib-music",
        })
        mock_get.return_value = client
        result = jellyfin_get_libraries()
    libs = result["libraries"]
    assert result["error_code"] is None
    assert len(libs) == 2
    assert libs[0]["name"] == "Movies"
    assert libs[1]["name"] == "Shows"



def test_get_libraries_handles_null_collection_type():
    """Real Jellyfin can serve libraries with CollectionType=null (e.g. mixed-
    content libraries before classification). The filter must drop them, not
    crash. Note: bypasses the fake's OpenAPI validator (jsonschema rejects
    null where OpenAPI 3.0 marks `nullable: true`)."""
    with patch("jellyfin.operations.get_jellyfin_client") as mock_get:
        client = FakeJellyfinClient()
        client.get_libraries = lambda: [
            {"Name": "Misc", "Locations": [], "CollectionType": None, "ItemId": "x"},
        ]
        mock_get.return_value = client
        result = jellyfin_get_libraries()
    assert result["error_code"] is None
    assert result["libraries"] == []


def test_get_libraries_returns_connection_failed_on_error():
    """Connection/auth/TLS failures must surface as `connection_failed`, NOT
    silently as an empty list. Otherwise the UI cannot distinguish 'server
    unreachable' from 'no libraries exist' and renders the wrong guidance."""
    with patch("jellyfin.operations.get_jellyfin_client", side_effect=Exception("fail")):
        result = jellyfin_get_libraries()
    assert result == {"libraries": [], "error_code": "connection_failed"}


def test_test_connection_rejects_empty_override_apikey(settings):
    """If the user clears the API key field on the Settings page and clicks
    Test, the override apikey arrives as an empty string. The previous
    `apikey or settings.jellyfin.apikey` semantics would silently fall back
    to the saved key and the test would 'succeed' against the old one - so
    the user could save a broken empty config thinking it had been
    validated. Empty string is invalid input, NOT a 'use saved' signal."""
    settings.jellyfin.url = "https://saved.example"
    settings.jellyfin.apikey = "saved-key-must-not-be-used"
    result = jellyfin_test_connection(url="https://override", apikey="",
                                      verify_ssl=False)
    assert result == {"success": False, "error_code": "configuration"}


def test_test_connection_rejects_empty_override_url(settings):
    """Same protection for the URL field."""
    settings.jellyfin.url = "https://saved.example"
    settings.jellyfin.apikey = "saved-key"
    result = jellyfin_test_connection(url="", apikey="override-key",
                                      verify_ssl=False)
    assert result == {"success": False, "error_code": "configuration"}


def test_test_connection_falls_back_when_overrides_are_None(settings):
    """A None override (caller passed nothing) DOES still use saved values -
    that's the legitimate 'use saved config' signal from non-Settings
    callers like jellyfin_refresh_item()."""
    settings.jellyfin.url = "https://saved.example"
    settings.jellyfin.apikey = "saved-key"
    with patch("jellyfin.operations.JellyfinClient") as mock_client:
        mock_client.return_value.get_system_info.return_value = {
            "ServerName": "S", "Version": "1.0",
        }
        jellyfin_test_connection()  # no overrides at all
        mock_client.assert_called_once_with("https://saved.example",
                                            "saved-key", verify_ssl=None)


def test_get_libraries_returns_configuration_when_url_or_key_missing():
    """ValueErrors from get_jellyfin_client (missing URL/apikey) get a
    distinct error_code so the UI guides 'configure URL/key first'
    instead of conflating with connectivity failures."""
    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=ValueError("Jellyfin URL not configured.")):
        result = jellyfin_get_libraries()
    assert result == {"libraries": [], "error_code": "configuration"}



def test_refresh_movie_immediate(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_movie(id="m1", imdb_id="tt123")]

    jellyfin_refresh_item(imdb_id="tt123", is_movie=True)

    assert fake.refresh_item_calls == ["m1"]
    assert fake.report_media_updated_calls == []


def test_refresh_movie_async(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "async"
    fake.items = [make_movie(id="m1", imdb_id="tt123", path="/media/movie.mkv")]

    jellyfin_refresh_item(imdb_id="tt123", is_movie=True)

    assert fake.report_media_updated_calls == ["/media/movie.mkv"]
    assert fake.refresh_item_calls == []


def test_refresh_movie_by_tmdb(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_movie(id="m1", imdb_id=None, tmdb_id="999")]

    jellyfin_refresh_item(imdb_id=None, tmdb_id="999", is_movie=True)

    assert fake.refresh_item_calls == ["m1"]


def test_refresh_movie_by_title(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_movie(id="m1", imdb_id=None, tmdb_id=None)]

    jellyfin_refresh_item(imdb_id=None, is_movie=True, title="Test Movie")

    assert fake.refresh_item_calls == ["m1"]


def test_refresh_movie_title_case_insensitive(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_movie(id="m1", name="The Matrix", imdb_id=None, tmdb_id=None)]

    jellyfin_refresh_item(imdb_id=None, is_movie=True, title="the matrix")

    assert fake.refresh_item_calls == ["m1"]


def test_refresh_episode_immediate(fake, settings):
    settings.jellyfin.series_library_ids = ["lib-shows"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_series(id="s1", imdb_id="tt456")]
    fake.episodes = {"s1": {2: [make_episode(id="ep5", index=5), make_episode(id="ep6", index=6)]}}

    jellyfin_refresh_item(imdb_id="tt456", is_movie=False, season=2, episode=6)

    assert fake.refresh_item_calls == ["ep6"]


def test_refresh_episode_async(fake, settings):
    settings.jellyfin.series_library_ids = ["lib-shows"]
    settings.jellyfin.get.return_value = "async"
    fake.items = [make_series(id="s1", imdb_id="tt456", path="/media/shows/Show")]

    jellyfin_refresh_item(imdb_id="tt456", is_movie=False, season=1, episode=1)

    assert fake.report_media_updated_calls == ["/media/shows/Show"]


def test_refresh_episode_by_tvdb(fake, settings):
    settings.jellyfin.series_library_ids = ["lib-shows"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_series(id="s1", imdb_id=None, tvdb_id="789")]
    fake.episodes = {"s1": {1: [make_episode(id="ep1", index=1)]}}

    jellyfin_refresh_item(imdb_id=None, tvdb_id=789, is_movie=False, season=1, episode=1)

    assert fake.refresh_item_calls == ["ep1"]


def test_refresh_falls_back_to_library_update(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = []  # nothing to find

    jellyfin_refresh_item(imdb_id="tt999", is_movie=True)

    # Falls back to refreshing the library ID
    assert fake.refresh_item_calls == ["lib-movies"]


def test_refresh_skips_when_no_libraries(fake, settings):
    settings.jellyfin.movie_library_ids = []

    jellyfin_refresh_item(imdb_id="tt123", is_movie=True)

    assert fake.refresh_item_calls == []
    assert fake.report_media_updated_calls == []



def test_refresh_with_no_ids_and_no_title_falls_back(fake, settings):
    """No IDs, no title — should fall back to library update."""
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_movie()]

    jellyfin_refresh_item(imdb_id=None, tmdb_id=None, tvdb_id=None, title=None, is_movie=True)

    # Falls back to library refresh
    assert fake.refresh_item_calls == ["lib-movies"]


def test_refresh_movie_not_found_falls_back(fake, settings):
    """Movie exists in library but with different IDs — falls back to library update."""
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_movie(imdb_id="tt999", tmdb_id="999")]

    jellyfin_refresh_item(imdb_id="tt000", tmdb_id="000", is_movie=True)

    assert fake.refresh_item_calls == ["lib-movies"]


def test_refresh_episode_not_found_in_season(fake, settings):
    """Series found but episode not in that season — falls back to library update."""
    settings.jellyfin.series_library_ids = ["lib-shows"]
    settings.jellyfin.get.return_value = "immediate"
    fake.items = [make_series(id="s1", imdb_id="tt456")]
    fake.episodes = {"s1": {1: [make_episode(id="ep1", index=1)]}}

    jellyfin_refresh_item(imdb_id="tt456", is_movie=False, season=1, episode=99)

    # Episode 99 not found, falls back
    assert fake.refresh_item_calls == ["lib-shows"]


def test_update_library_refreshes_each_configured(fake, settings):
    settings.jellyfin.movie_library_ids = ["lib1", "lib2"]
    jellyfin_update_library(fake, is_movie_library=True, library_ids=["lib1", "lib2"])
    assert fake.refresh_item_calls == ["lib1", "lib2"]


def test_update_library_skips_empty_ids(fake, settings):
    settings.jellyfin.series_library_ids = ["", "lib1"]
    jellyfin_update_library(fake, is_movie_library=False, library_ids=["", "lib1"])
    assert fake.refresh_item_calls == ["lib1"]


# --- jellyfin_refresh_all_libraries (Maintenance card) ---


def test_refresh_all_success_counts(fake, settings):
    settings.jellyfin.movie_library_ids = ["m1", "m2"]
    settings.jellyfin.series_library_ids = ["s1"]
    result = jellyfin_refresh_all_libraries()
    assert result["success"] is True
    assert result["movies_total"] == 2
    assert result["movies_refreshed"] == 2
    assert result["series_total"] == 1
    assert result["series_refreshed"] == 1
    assert "error_code" not in result
    assert fake.refresh_item_calls == ["m1", "m2", "s1"]


def test_refresh_all_no_libraries_configured(settings):
    settings.jellyfin.movie_library_ids = []
    settings.jellyfin.series_library_ids = []
    with patch("jellyfin.operations.get_jellyfin_client",
               return_value=FakeJellyfinClient()):
        result = jellyfin_refresh_all_libraries()
    assert result["success"] is False
    assert result["error_code"] == "no_libraries_configured"


def test_refresh_all_configuration_error():
    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=ValueError("Jellyfin URL not configured.")):
        result = jellyfin_refresh_all_libraries()
    assert result["success"] is False
    assert result["error_code"] == "configuration"


def test_refresh_all_connection_failure():
    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=Exception("connection refused at https://x SECRET")):
        result = jellyfin_refresh_all_libraries()
    assert result["success"] is False
    assert result["error_code"] == "connection_failed"
    # No raw exception text leaks through (api_key wouldn't either)
    assert "SECRET" not in str(result)
    assert "refused" not in str(result)


def test_refresh_all_partial_success(fake, settings):
    """Some libraries succeed, others fail. success=False but counts reflect
    the partial result so the UI can render 'Refreshed 1 of 2'."""
    settings.jellyfin.movie_library_ids = ["good", "bad"]
    settings.jellyfin.series_library_ids = []

    real_refresh = fake.refresh_item

    def flaky(item_id):
        if item_id == "bad":
            raise RuntimeError("upstream Jellyfin returned 500")
        real_refresh(item_id)

    fake.refresh_item = flaky

    result = jellyfin_refresh_all_libraries()
    assert result["success"] is False
    assert result["movies_total"] == 2
    assert result["movies_refreshed"] == 1
    assert "error_code" not in result


def test_refresh_episode_async_falls_back_to_item_when_series_path_missing(fake, settings):
    """If Jellyfin returns a series with no Path (some libraries / API-key
    permissions strip it), the async branch must NOT silently no-op. It
    falls back to a series-level metadata refresh so subtitles still
    trigger SOME jellyfin update."""
    settings.jellyfin.series_library_ids = ["lib-shows"]
    settings.jellyfin.get.return_value = "async"
    # Drop the Path key entirely - that's the wire shape we're guarding
    # against (Jellyfin omits Path for some library / API-key permissions).
    series = make_series(id="s1", imdb_id="tt456")
    series.pop("Path", None)
    fake.items = [series]

    jellyfin_refresh_item(imdb_id="tt456", is_movie=False, season=1, episode=1)

    # No filesystem-change report (no Path), but the series id was refreshed
    # so Jellyfin re-reads metadata for that series.
    assert fake.report_media_updated_calls == []
    assert fake.refresh_item_calls == ["s1"]


def test_redact_strips_override_apikey_when_test_connection_fails(settings):
    """jellyfin_test_connection accepts an override apikey for the form
    preview. If the call fails and Jellyfin reflects that key into the
    error string, _redact must scrub it - even though the key is not the
    saved one in settings.jellyfin.apikey."""
    import logging  # noqa: F401

    settings.jellyfin.apikey = "saved-key-DIFFERENT"
    override = "override-key-LEAK-CANDIDATE"
    err = f"401 Unauthorized for /System/Info?api_key={override}"

    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=Exception(err)):
        with patch.object(_ops_module.logger, "error") as mock_error:
            result = jellyfin_test_connection(url="https://j", apikey=override,
                                              verify_ssl=False)

    assert result["success"] is False
    assert result["error_code"] == "connection_failed"
    logged = mock_error.call_args[0][0]
    assert override not in logged
    assert "saved-key-DIFFERENT" not in logged
    assert "***" in logged


def test_find_item_handles_null_providerids(fake, settings):
    """Jellyfin DTOs allow ProviderIds to be null. `item.get('ProviderIds', {})`
    only returns {} when the key is missing - if it's present with value None,
    the default is skipped and `.get()` would AttributeError, abort scanning
    the library, and force a full-library refresh fallback instead of finding
    the targeted item. Confirm the targeted refresh still wins when one item
    has ProviderIds=null among other items in the same library.

    Note: the FakeJellyfinClient's strict OpenAPI validator rejects
    ProviderIds=null (jsonschema doesn't honor OpenAPI 3.0's `nullable`
    extension), but real Jellyfin DOES serve null values - and
    requests.json() in the live client doesn't validate. So we mock
    get_items directly to deliver the wire shape we're guarding against.
    """
    settings.jellyfin.movie_library_ids = ["lib-movies"]
    settings.jellyfin.get.return_value = "immediate"
    poisoned = make_movie(id="bad", imdb_id="tt000")
    poisoned["ProviderIds"] = None  # the wire shape Codex flagged
    target = make_movie(id="good", imdb_id="tt999", path="/media/movies/Found")
    fake.get_items = lambda params: [poisoned, target]

    jellyfin_refresh_item(imdb_id="tt999", is_movie=True)

    # Targeted refresh of the good item, NOT the library-wide fallback.
    assert fake.refresh_item_calls == ["good"]


def test_redact_strips_override_apikey_when_get_libraries_fails(settings):
    """Same protection on the libraries probe path."""
    settings.jellyfin.apikey = "saved-key-DIFFERENT"
    override = "override-libs-LEAK-CANDIDATE"
    err = f"timeout fetching /Library/VirtualFolders for token {override}"

    with patch("jellyfin.operations.get_jellyfin_client",
               side_effect=Exception(err)):
        with patch.object(_ops_module.logger, "error") as mock_error:
            result = jellyfin_get_libraries(url="https://j", apikey=override,
                                            verify_ssl=False)

    assert result == {"libraries": [], "error_code": "connection_failed"}
    logged = mock_error.call_args[0][0]
    assert override not in logged
    assert "saved-key-DIFFERENT" not in logged

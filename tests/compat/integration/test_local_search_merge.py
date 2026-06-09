from unittest.mock import patch, MagicMock
from babelfish import Language

import pytest


@pytest.fixture(autouse=True)
def _bypass_compat_cache():
    from compat import cache as C
    C.invalidate_all()
    yield
    C.invalidate_all()


def test_search_merges_locals_above_provider_results():
    from compat import service
    fake_provider_sub = MagicMock(
        provider_name="opensubtitlescom", id="123", language=Language("eng"),
        release_info="P.2020.1080p", download_count=100, hearing_impaired=False,
        matches=set(),
    )
    fake_local_entry = {
        "id": "subtitle-9999",
        "type": "subtitle",
        "attributes": {
            "subtitle_id": "local-movie-1-en",
            "language": "en",
            "release": "Movie.en.srt",
            "hearing_impaired": False,
            "foreign_parts_only": False,
            "from_trusted": True,
            "ratings": 10.0,
            "download_count": 999_999,
            "upload_date": "2024-01-01T00:00:00Z",
            "files": [{"file_id": 9999, "file_name": "Movie.en.srt"}],
        },
    }
    with patch("compat.service._get_compat_pool") as gp, \
         patch("compat.service.list_all_subtitles_parallel") as lf, \
         patch("compat.service.search_local") as sl:
        lf.return_value = {MagicMock(): [fake_provider_sub]}
        gp.return_value.providers = ["opensubtitlescom"]
        gp.return_value.discarded_providers = set()
        sl.return_value = [fake_local_entry]
        result = service.search(imdb_id="tt1", season=None, episode=None,
                                 languages=[Language("eng")],
                                 media_type="movie",
                                 requested_languages=["en"])
    assert result["data"]
    assert result["data"][0]["attributes"]["from_trusted"] is True
    assert result["data"][0]["attributes"]["download_count"] == 999_999


def test_search_skips_locals_when_setting_disabled(monkeypatch):
    from compat import service
    from app.config import settings
    monkeypatch.setattr(settings.compat_endpoint, "serve_local_subs", False)
    with patch("compat.service._get_compat_pool") as gp, \
         patch("compat.service.list_all_subtitles_parallel") as lf, \
         patch("compat.service.search_local") as sl:
        lf.return_value = {MagicMock(): []}
        gp.return_value.providers = []
        gp.return_value.discarded_providers = set()
        sl.return_value = [{"id": "subtitle-9999",
                            "attributes": {"download_count": 999_999}}]
        service.search(imdb_id="tt1", season=None, episode=None,
                       languages=[Language("eng")], media_type="movie",
                       requested_languages=["en"])
    sl.assert_not_called()


def _provider_sub():
    return MagicMock(
        provider_name="opensubtitlescom", id="123", language=Language("eng"),
        release_info="P.2020.1080p", download_count=100, hearing_impaired=False,
        matches=set(),
    )


def _local_entry():
    return {
        "id": "subtitle-9999", "type": "subtitle",
        "attributes": {
            "subtitle_id": "local-movie-1-en", "language": "en",
            "release": "Movie.en.srt", "hearing_impaired": False,
            "foreign_parts_only": False, "from_trusted": True, "ratings": 10.0,
            "download_count": 999_999, "upload_date": "2024-01-01T00:00:00Z",
            "files": [{"file_id": 9999, "file_name": "Movie.en.srt"}],
        },
    }


def _run(only_providers=None, exclude_providers=None):
    """Run a compat search with mocked pool/fanout/local, returning
    (result, search_local_mock, fanout_mock)."""
    from compat import service
    with patch("compat.service._get_compat_pool") as gp, \
         patch("compat.service.list_all_subtitles_parallel") as lf, \
         patch("compat.service.search_local") as sl:
        lf.return_value = {MagicMock(): [_provider_sub()]}
        gp.return_value.providers = ["opensubtitlescom"]
        gp.return_value.discarded_providers = set()
        sl.return_value = [_local_entry()]
        result = service.search(
            imdb_id="tt1", season=None, episode=None,
            languages=[Language("eng")], media_type="movie",
            requested_languages=["en"],
            only_providers=only_providers, exclude_providers=exclude_providers,
        )
    return result, sl, lf


def test_allow_list_without_local_suppresses_locals():
    """only_providers that omits `local` must not merge on-disk subtitles."""
    _result, sl, lf = _run(only_providers=["opensubtitlescom"])
    sl.assert_not_called()
    # the named provider is still reachable (not excluded)
    assert "opensubtitlescom" not in lf.call_args.kwargs["exclude_providers"]


def test_allow_list_with_local_includes_locals():
    """only_providers naming `local` serves on-disk subtitles alongside it."""
    result, sl, _lf = _run(only_providers=["opensubtitlescom", "local"])
    sl.assert_called_once()
    assert result["data"][0]["attributes"]["download_count"] == 999_999


def test_only_local_excludes_remote_providers():
    """only_providers=['local'] excludes every pool provider but still serves
    locals."""
    _result, sl, lf = _run(only_providers=["local"])
    sl.assert_called_once()
    assert "opensubtitlescom" in lf.call_args.kwargs["exclude_providers"]


def test_active_empty_allow_list_excludes_everything():
    """An active-but-empty allow-list ([]) excludes all remote providers and
    serves no locals -> data: []."""
    _result, sl, lf = _run(only_providers=[])
    sl.assert_not_called()
    assert "opensubtitlescom" in lf.call_args.kwargs["exclude_providers"]


def test_exclude_local_suppresses_locals_without_allow_list():
    """exclude_providers naming `local` turns off on-disk subtitles even with
    no allow-list active."""
    _result, sl, _lf = _run(exclude_providers=["local"])
    sl.assert_not_called()

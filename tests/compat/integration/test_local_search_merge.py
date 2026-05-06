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

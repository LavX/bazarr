"""Tests for the TVDB v4 client and its compat-endpoint integration."""
from unittest.mock import MagicMock, patch

import pytest

from subliminal_patch.refiners import tvdb_v4


@pytest.fixture(autouse=True)
def _reset_client():
    tvdb_v4.reset_client()
    yield
    tvdb_v4.reset_client()


def test_apikey_is_hardcoded():
    """The api key is a maintainer-managed detail; not user-configurable."""
    assert isinstance(tvdb_v4._APIKEY, str) and tvdb_v4._APIKEY


def test_client_login_caches_token():
    """First search forces login; second reuses cached JWT."""
    c = tvdb_v4.TVDBv4Client()
    fake_login = MagicMock(status_code=200)
    fake_login.json.return_value = {"data": {"token": "jwt-token"}}
    fake_search = MagicMock(status_code=200)
    fake_search.json.return_value = {"data": []}

    with patch.object(c._session, "post", return_value=fake_login) as post, \
         patch.object(c._session, "get", return_value=fake_search) as get:
        c.search_by_imdb_id("tt111161")
        c.search_by_imdb_id("tt944947")
        assert post.call_count == 1  # login only once
        assert get.call_count == 2


def test_client_returns_none_on_missing_imdb():
    c = tvdb_v4.TVDBv4Client()
    assert c.search_by_imdb_id(None) is None
    assert c.search_by_imdb_id("") is None


def test_client_prepends_tt_prefix():
    c = tvdb_v4.TVDBv4Client()
    fake_login = MagicMock(status_code=200)
    fake_login.json.return_value = {"data": {"token": "jwt"}}
    fake_search = MagicMock(status_code=200)
    fake_search.json.return_value = {"data": []}
    with patch.object(c._session, "post", return_value=fake_login), \
         patch.object(c._session, "get", return_value=fake_search) as get:
        c.search_by_imdb_id("111161")
        c.search_by_imdb_id("tt111161")
        for call in get.call_args_list:
            url = call[0][0]
            assert "tt111161" in url


def test_client_survives_login_failure():
    c = tvdb_v4.TVDBv4Client()
    fake_login = MagicMock(status_code=401)
    fake_login.json.return_value = {"message": "InvalidAPIKey"}
    with patch.object(c._session, "post", return_value=fake_login):
        assert c.search_by_imdb_id("tt111161") is None


def test_v4_episode_lookup_populates_series_and_episode_fields():
    """Given a v4 match with episode+seriesId, populate tvdb_id, series name,
    year, and hydrate anything missing from the v4 series record (not the
    retired v1 client)."""
    from compat.service import _tvdb_v4_episode_lookup
    video = MagicMock(spec=["series_imdb_id", "tvdb_id", "title",
                            "series", "year", "series_tvdb_id"])
    video.series_imdb_id = "tt1480055"
    video.tvdb_id = None
    video.title = None
    video.series = None
    video.year = None
    video.series_tvdb_id = None
    v4_match = {
        "episode": {"id": 3254641, "name": "Winter Is Coming",
                    "seriesId": 121361, "aired": "2011-04-17"}
    }
    series_v4 = {"name": "Game of Thrones", "firstAired": "2011-04-17"}
    with patch("subliminal_patch.refiners.tvdb_v4.get_client") as gc, \
         patch("subliminal.refiners.tvdb.tvdb_client") as v1_client:
        gc.return_value.search_by_imdb_id.return_value = v4_match
        gc.return_value.get_series.return_value = series_v4
        assert _tvdb_v4_episode_lookup(video) is True
        # Hydration is v4-only now; the v1 client must never be touched.
        assert not v1_client.mock_calls
    assert video.series_tvdb_id == 121361
    assert video.tvdb_id == 3254641
    assert video.title == "Winter Is Coming"
    assert video.series == "Game of Thrones"
    assert video.year == 2011


def test_v4_episode_lookup_partial_ids_without_name():
    """A v4 match that only yields ids (series record unavailable) must
    still return True: the caller decides whether the series name is
    usable and continues the chain when it isn't."""
    from compat.service import _tvdb_v4_episode_lookup
    video = MagicMock(spec=["series_imdb_id", "tvdb_id", "title",
                            "series", "year", "series_tvdb_id"])
    video.series_imdb_id = "tt1480055"
    video.tvdb_id = None
    video.title = None
    video.series = None
    video.year = None
    video.series_tvdb_id = None
    v4_match = {"episode": {"id": 3254641, "seriesId": 121361}}
    with patch("subliminal_patch.refiners.tvdb_v4.get_client") as gc:
        gc.return_value.search_by_imdb_id.return_value = v4_match
        gc.return_value.get_series.return_value = None
        assert _tvdb_v4_episode_lookup(video) is True
    assert video.series_tvdb_id == 121361
    assert video.series is None


def test_client_get_series_returns_record_and_url():
    c = tvdb_v4.TVDBv4Client()
    fake_login = MagicMock(status_code=200)
    fake_login.json.return_value = {"data": {"token": "jwt"}}
    fake_series = MagicMock(status_code=200)
    fake_series.json.return_value = {"data": {"name": "Game of Thrones",
                                              "firstAired": "2011-04-17"}}
    with patch.object(c._session, "post", return_value=fake_login), \
         patch.object(c._session, "get", return_value=fake_series) as get:
        assert c.get_series(121361) == {"name": "Game of Thrones",
                                        "firstAired": "2011-04-17"}
        assert "/series/121361" in get.call_args_list[0][0][0]


def test_client_get_series_none_on_missing_or_failure():
    c = tvdb_v4.TVDBv4Client()
    assert c.get_series(None) is None
    fake_login = MagicMock(status_code=200)
    fake_login.json.return_value = {"data": {"token": "jwt"}}
    fake_404 = MagicMock(status_code=404)
    with patch.object(c._session, "post", return_value=fake_login), \
         patch.object(c._session, "get", return_value=fake_404):
        assert c.get_series(999999) is None
    # A cached token must not carry a later login failure: reset it so
    # the next call has to log in again, and this time fail.
    c._token = None
    c._token_exp = 0.0
    fake_bad_login = MagicMock(status_code=401)
    fake_bad_login.json.return_value = {"message": "InvalidAPIKey"}
    with patch.object(c._session, "post", return_value=fake_bad_login), \
         patch.object(c._session, "get") as get:
        assert c.get_series(121361) is None
        assert not get.mock_calls


def test_v4_episode_lookup_returns_false_on_miss():
    from compat.service import _tvdb_v4_episode_lookup
    video = MagicMock(spec=["series_imdb_id"])
    video.series_imdb_id = "tt0000000"
    with patch("subliminal_patch.refiners.tvdb_v4.get_client") as gc:
        gc.return_value.search_by_imdb_id.return_value = None
        assert _tvdb_v4_episode_lookup(video) is False

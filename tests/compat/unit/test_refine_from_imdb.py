"""Resolver-level tests for the compat _refine_from_imdb chain.

The chain must resolve an imdb id keylessly: TVDB v4 and TMDB carry
app-shipped keys, and OMDB is extra coverage for configured installs only.
HTTP is stubbed below the clients, so nothing here touches the network.
"""
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from subliminal.video import Episode, Movie
from subliminal_patch.refiners import tvdb_v4


def _response(status=200, data=None):
    r = MagicMock(status_code=status)
    r.json.return_value = data or {}
    return r


def _episode(series_imdb_id="tt7366338", season=1, episode=1):
    return Episode(name="chernobyl.s01e01.21600.mkv", series="",
                   season=season, episode=episode,
                   series_imdb_id=series_imdb_id)


def _movie(imdb_id="tt0111161"):
    return Movie(name="movie.mkv", title="", imdb_id=imdb_id)


def _v4_client(search_results=None, series_record=None):
    """A spec-set client stub: real signatures, no unknown attributes, no
    network. search_results may be a single value or a per-call sequence."""
    client = create_autospec(tvdb_v4.TVDBv4Client(), spec_set=True)
    if isinstance(search_results, list):
        client.search_by_imdb_id.side_effect = search_results
    else:
        client.search_by_imdb_id.return_value = search_results
    client.get_series.return_value = series_record
    return client


def _http(by_url=None):
    """requests.get stub keyed by URL substring; records every call."""
    by_url = by_url or {}
    calls = []

    def fake_get(url, params=None, timeout=None, **kwargs):
        calls.append((url, params))
        for fragment, response in by_url.items():
            if fragment in url:
                return response
        return _response(404, {})

    return fake_get, calls


TMDB_FIND_GOT = _response(200, {
    "tv_episode_results": [{
        "id": 63056, "show_id": 1399, "season_number": 1,
        "episode_number": 1, "name": "Winter Is Coming",
    }],
    "tv_results": [], "movie_results": [],
})
TMDB_TV_GOT = _response(200, {"name": "Game of Thrones",
                              "first_air_date": "2011-04-17"})
TMDB_FIND_SERIES = _response(200, {
    "tv_results": [{"id": 1399, "name": "Game of Thrones",
                    "first_air_date": "2011-04-17"}],
    "tv_episode_results": [], "movie_results": [],
})
TMDB_FIND_EMPTY = _response(200, {"tv_results": [], "tv_episode_results": [],
                                  "movie_results": []})
TMDB_FIND_MOVIE = _response(200, {
    "movie_results": [{"id": 278, "title": "The Shawshank Redemption",
                       "release_date": "1994-09-23"}],
    "tv_results": [], "tv_episode_results": [],
})
OMDB_EPISODE = _response(200, {
    "Response": "True", "Type": "episode", "Title": "Winter Is Coming",
    "Season": "1", "Episode": "1", "Year": "2011",
    "seriesID": "tt0944947",
})
OMDB_SERIES_ROOT = _response(200, {
    "Response": "True", "Type": "series", "Title": "Game of Thrones",
    "Year": "2011", "seriesID": "",
})
OMDB_MOVIE = _response(200, {"Response": "True", "Type": "movie",
                             "Title": "The Shawshank Redemption",
                             "Year": "1994"})
OMDB_EMPTY = _response(200, {"Response": "False"})


@pytest.fixture
def keyless():
    """No OMDB key configured: the keyless install baseline."""
    with patch("subliminal_patch.refiners.omdb._resolve_omdb_apikey",
               return_value=""):
        yield


@pytest.fixture
def omdb_key():
    with patch("subliminal_patch.refiners.omdb._resolve_omdb_apikey",
               return_value="omdb-key-for-tests"):
        yield


@pytest.fixture
def no_v1_client():
    """The retired v1 client must never be called anywhere in the chain."""
    with patch("subliminal.refiners.tvdb.tvdb_client") as v1:
        yield v1


def test_episode_v4_hit_stops_chain(keyless, no_v1_client):
    """A v4 episode match that names the series ends the chain: no TMDB,
    no OMDB, keyless."""
    from compat.service import _refine_from_imdb

    video = _episode()
    client = _v4_client(search_results={"episode": {
        "id": 7037111, "name": "1:23:45", "seriesId": 360893,
        "aired": "2019-05-06"}},
        series_record={"name": "Chernobyl", "firstAired": "2019-05-06"})
    get, calls = _http()
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == ["v4=ok"]
    assert video.series == "Chernobyl"
    assert video.year == 2019
    assert (video.series_tvdb_id, video.tvdb_id) == (360893, 7037111)
    assert calls == []
    assert not no_v1_client.mock_calls


def test_episode_v4_partial_ids_fall_through_to_tmdb(keyless, no_v1_client):
    """v4 returns ids but no usable series name: the chain continues to
    TMDB, which resolves the episode through its show record."""
    from compat.service import _refine_from_imdb

    video = _episode()
    client = _v4_client(search_results={"episode": {"id": 7037111,
                                                    "seriesId": 360893}},
                        series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_GOT,
                      "themoviedb.org/3/tv/": TMDB_TV_GOT})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == ["v4=partial",
                                                       "tmdb=ok"]
    assert video.series == "Game of Thrones"
    assert video.series_tmdb_id == 1399
    assert (video.season, video.episode) == (1, 1)
    urls = [c[0] for c in calls]
    assert any("themoviedb.org/3/find/tt7366338" in u for u in urls)
    assert any("themoviedb.org/3/tv/1399" in u for u in urls)
    assert not any("omdbapi.com" in u for u in urls)
    assert not no_v1_client.mock_calls


def test_episode_series_id_resolves_via_tmdb_tv_results(keyless,
                                                         no_v1_client):
    """The client sent the series' own imdb id: v4 misses, TMDB's find
    names the series through tv_results directly."""
    from compat.service import _refine_from_imdb

    video = _episode(series_imdb_id="tt0944947")
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_SERIES})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == ["v4=no_match",
                                                       "tmdb=ok"]
    assert video.series == "Game of Thrones"
    assert video.year == 2011
    assert not no_v1_client.mock_calls


def test_episode_keyless_unresolved_reports_every_source(keyless,
                                                          no_v1_client):
    """Nothing resolves an unknown id: every source reports why, OMDB is
    skipped without a key (zero omdbapi.com requests), and no request is
    ever made to the retired v1 client."""
    from compat.service import _refine_from_imdb

    video = _episode(series_imdb_id="tt0000000")
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_EMPTY})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == [
            "v4=no_match", "tmdb=no_match", "omdb=not_configured"]
    assert video.series in (None, "")
    urls = [c[0] for c in calls]
    assert not any("omdbapi.com" in u for u in urls)
    assert not no_v1_client.mock_calls


def test_episode_omdb_parent_retries_v4_then_tmdb(omdb_key, no_v1_client):
    """A configured OMDB bridges the episode id to the series id, and the
    chain retries v4 with the parent id before falling back to TMDB."""
    from compat.service import _refine_from_imdb

    video = _episode(series_imdb_id="tt1480055")
    client = _v4_client(
        search_results=[
            None,  # initial episode id miss
            {"series": {"id": 121361, "name": "Game of Thrones"}},
        ],
        series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_EMPTY,
                      "omdbapi.com": OMDB_EPISODE})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == [
            "v4=no_match", "tmdb=no_match", "omdb=parent", "v4=parent_ok"]
    assert video.series == "Game of Thrones"
    assert video.series_tvdb_id == 121361
    assert video.series_imdb_id == "tt0944947"
    urls = [c[0] for c in calls]
    assert any("omdbapi.com" in u for u in urls)
    assert not no_v1_client.mock_calls


def test_episode_omdb_series_root_names_series(omdb_key, no_v1_client):
    """The id already was a series id: OMDB's series-root record names the
    series itself and no parent retry is attempted."""
    from compat.service import _refine_from_imdb

    video = _episode(series_imdb_id="tt0944947")
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_EMPTY,
                      "omdbapi.com": OMDB_SERIES_ROOT})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == [
            "v4=no_match", "tmdb=no_match", "omdb=ok"]
    assert video.series == "Game of Thrones"
    # The parent-retry path never ran: exactly one OMDB request, no
    # second v4 search.
    client.search_by_imdb_id.assert_called_once()
    assert len([c for c in calls if "omdbapi.com" in c[0]]) == 1
    assert not no_v1_client.mock_calls


def test_movie_tmdb_resolves_keyless(keyless, no_v1_client):
    from compat.service import _refine_from_imdb

    video = _movie()
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_MOVIE})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "movie") == ["tmdb=ok"]
    assert video.title == "The Shawshank Redemption"
    assert video.year == 1994
    urls = [c[0] for c in calls]
    assert not any("omdbapi.com" in u for u in urls)
    assert not no_v1_client.mock_calls


def test_movie_tmdb_miss_falls_back_to_omdb(omdb_key, no_v1_client):
    from compat.service import _refine_from_imdb

    video = _movie(imdb_id="tt12850167")
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_EMPTY,
                      "omdbapi.com": OMDB_MOVIE})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "movie") == ["tmdb=no_match",
                                                     "omdb=ok"]
    assert video.title == "The Shawshank Redemption"
    assert video.year == 1994
    assert not no_v1_client.mock_calls


def test_movie_keyless_unresolved_reports_both_sources(keyless,
                                                        no_v1_client):
    from compat.service import _refine_from_imdb

    video = _movie(imdb_id="tt0000000")
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_EMPTY})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "movie") == ["tmdb=no_match",
                                                     "omdb=not_configured"]
    assert video.title in (None, "")
    urls = [c[0] for c in calls]
    assert not any("omdbapi.com" in u for u in urls)
    assert not no_v1_client.mock_calls


def test_omdb_key_failure_never_breaks_the_chain(no_v1_client):
    """A resolver exception inside the key helper must not raise: the
    chain still runs and reports OMDB as not configured."""
    from compat.service import _refine_from_imdb

    video = _episode(series_imdb_id="tt0000000")
    client = _v4_client(search_results=None, series_record=None)
    get, calls = _http({"themoviedb.org/3/find": TMDB_FIND_EMPTY})
    with patch("subliminal_patch.refiners.tvdb_v4.get_client",
               return_value=client), \
         patch("subliminal_patch.refiners.omdb._resolve_omdb_apikey",
               side_effect=RuntimeError("settings unavailable")), \
         patch("requests.get", side_effect=get):
        assert _refine_from_imdb(video, "episode") == [
            "v4=no_match", "tmdb=no_match", "omdb=not_configured"]
    urls = [c[0] for c in calls]
    assert not any("omdbapi.com" in u for u in urls)
    assert not no_v1_client.mock_calls

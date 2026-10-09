# coding=utf-8
"""The AniList refiner must cache an index, not the whole anime list.

`get_series_mappings` cached the parsed Fribb anime-list, roughly 43,000 dicts,
in the file-backed dogpile region. That backend unpickles its value on every
read, so each jimaku-refined search inflated a 2.6 MB pickle into about 30 MB of
short-lived Python objects, linear-scanned it with a `str()` conversion per
entry, took the first match and threw the rest away.

Only two fields of each entry are ever consulted, so the cache should hold the
lookup rather than the corpus. A dict keyed by the two id types is a fraction of
the size, survives the same round trip, and turns the scan into a hash lookup.
"""
import logging
from types import SimpleNamespace

import pytest
import requests


ANIME_LIST = [
    {"anidb_id": 1, "imdb_id": "tt0001", "anilist_id": 101, "title": "First",
     "mal_id": 9001, "themoviedb_id": 5001},
    {"anidb_id": 2, "anilist_id": 102, "title": "No imdb", "mal_id": 9002},
    {"imdb_id": "tt0003", "anilist_id": 103, "title": "No anidb"},
    {"anidb_id": 4, "imdb_id": "tt0004", "title": "No anilist id at all"},
    {"anidb_id": 1, "anilist_id": 999, "title": "Duplicate anidb, must not win"},
]


def test_the_index_resolves_both_id_types():
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert index["anidb_id"]["1"] == 101
    assert index["anidb_id"]["2"] == 102
    assert index["imdb_id"]["tt0001"] == 101
    assert index["imdb_id"]["tt0003"] == 103


def test_an_entry_without_an_anilist_id_still_claims_its_key():
    """It claims the key with a None value. The old scan matched that entry and
    returned nothing, so dropping it would let a later duplicate answer in its
    place, which is a different result for the same input."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert index["anidb_id"]["4"] is None
    assert index["imdb_id"]["tt0004"] is None


def test_a_later_duplicate_cannot_displace_an_id_less_first_entry():
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index([
        {"anidb_id": 42, "title": "First, no anilist id"},
        {"anidb_id": 42, "anilist_id": 999, "title": "Later duplicate"},
    ])

    assert index["anidb_id"]["42"] is None, (
        'the later duplicate answered for a key the first entry already claimed'
    )


def test_a_non_mapping_element_does_not_break_the_build():
    """The build runs inside the cached call, so raising here would mean nothing
    is ever cached and the 3 MB list is refetched on every search."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(["junk", None, 7, {"anidb_id": 1, "anilist_id": 101}])

    assert index["anidb_id"]["1"] == 101


def test_the_first_entry_wins_as_it_did_before():
    """The old code took obj[0] of the matches, so a later duplicate never won."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert index["anidb_id"]["1"] == 101, 'a later duplicate overwrote the first match'


def test_the_index_keeps_only_what_is_looked_up():
    """The point of the change. Anything else retained puts the corpus back."""
    from subtitles.refiners.anilist import build_series_index

    index = build_series_index(ANIME_LIST)

    assert set(index) == {"anidb_id", "imdb_id"}
    flattened = repr(index)
    for noise in ("title", "mal_id", "themoviedb_id", "9001", "5001"):
        assert noise not in flattened, (
            f'{noise!r} survived into the cached index; only the two id maps '
            'should be cached')


@pytest.mark.parametrize("candidate,value,expected", [
    ("series_anidb_id", 1, 101),
    ("series_anidb_id", "2", 102),
    ("imdb_id", "tt0003", 103),
    ("series_anidb_id", 4, None),      # present, but carries no AniList id
    ("imdb_id", "tt9999", None),       # not in the list at all
])
def test_lookup_matches_the_old_behaviour(monkeypatch, candidate, value, expected):
    from subtitles.refiners import anilist

    client = anilist.AniListClient.__new__(anilist.AniListClient)
    monkeypatch.setattr(anilist.AniListClient, 'get_series_index',
                        lambda self: anilist.build_series_index(ANIME_LIST))

    assert client.get_series_id(candidate, value) == expected


# Which refiners run is gated on settings.general.enabled_providers, and Provider
# Hub providers join that list under their catalog ids. The catalog anime
# providers read these identities from the worker payload: TsukiHime reads the
# AniDB series and episode ids for episodes and the AniList id for movies,
# AnimeTosho and AnimeTosho.xyz read the AniDB episode id, and Jimaku searches by
# AniList id first.

def _enabled(*providers, api_client=""):
    return SimpleNamespace(
        general=SimpleNamespace(enabled_providers=list(providers)),
        anidb=SimpleNamespace(api_client=api_client, api_client_ver=1),
    )


@pytest.mark.parametrize("provider,expected", [
    ("jimaku", True),
    ("tsukihime", True),
    ("animetosho", False),
    ("animetosho_xyz", False),
    ("unrelated", False),
])
@pytest.mark.parametrize("kind", ["episode", "movie"])
def test_anilist_refinement_runs_for_providers_that_read_the_anilist_id(monkeypatch, provider, expected, kind):
    from subliminal import Episode, Movie
    from subtitles.refiners import anilist

    if kind == "episode":
        video = Episode("/media/anime.mkv", "Example", 1, 2, series_anidb_id=101)
    else:
        video = Movie("/media/anime-movie.mkv", "Example Movie", imdb_id="tt0001")
    calls = []
    monkeypatch.setattr(anilist, "settings", _enabled(provider))
    monkeypatch.setattr(anilist, "refine_anilist_ids", calls.append)

    anilist.refine_from_anilist(video.name, video)

    assert calls == ([video] if expected else [])


@pytest.mark.parametrize("provider,expected", [
    ("animetosho", True),
    ("animetosho_xyz", True),
    ("jimaku", True),
    ("tsukihime", True),
    ("unrelated", False),
])
def test_anidb_refinement_runs_for_providers_that_read_anidb_ids(monkeypatch, provider, expected):
    from subliminal import Episode
    from subtitles.refiners import anidb

    video = Episode("/media/anime.mkv", "Example", 1, 2, series_tvdb_id=101)
    calls = []
    monkeypatch.setattr(anidb, "settings", _enabled(provider))
    monkeypatch.setattr(anidb, "refine_anidb_ids", calls.append)

    anidb.refine_from_anidb(video.name, video)

    assert calls == ([video] if expected else [])


class _AniDBClientWithoutCredentials:
    has_api_credentials = False
    is_throttled = False

    def __init__(self, *args, **kwargs):
        pass

    def get_show_information(self, tvdb_series_id, season, episode):
        return 17495, episode, 0

    def get_episode_ids(self, series_id, episode_no):
        raise AssertionError("the AniDB API must not be called without client credentials")


@pytest.mark.parametrize("provider,needs_api", [
    ("animetosho", True),
    ("animetosho_xyz", True),
    ("tsukihime", True),
    ("jimaku", False),
])
@pytest.mark.filterwarnings("error::DeprecationWarning")
def test_missing_anidb_api_credentials_are_reported_for_providers_that_need_episode_ids(
        monkeypatch, caplog, provider, needs_api):
    """Without AniDB API client credentials the refiner still finds the series
    but cannot resolve the episode id, so a provider that searches by episode id
    returns nothing. That has to be said in the log, naming the provider."""
    from subliminal import Episode
    from subtitles.refiners import anidb

    video = Episode("/media/anime.mkv", "Example", 1, 12, series_tvdb_id=101)
    monkeypatch.setattr(anidb, "settings", _enabled(provider))
    monkeypatch.setattr(anidb, "AniDBClient", _AniDBClientWithoutCredentials)

    with caplog.at_level(logging.WARNING, logger=anidb.logger.name):
        anidb.refine_anidb_ids(video)

    assert video.series_anidb_id == 17495
    assert video.series_anidb_episode_no == 12
    assert video.series_anidb_episode_id is None
    warnings = [record.getMessage() for record in caplog.records
                if record.name == anidb.logger.name and record.levelno == logging.WARNING]
    if needs_api:
        assert len(warnings) == 1 and provider in warnings[0]
    else:
        assert warnings == []


def test_every_provider_that_needs_the_anidb_api_is_also_refined():
    """A provider in the API set but not the refined set would never trigger the
    refiner, so the credential warning meant for it could never fire."""
    from subtitles.refiners import anidb

    assert anidb.providers_requiring_anidb_api <= anidb.refined_providers


@pytest.mark.parametrize("episode_ids,expected", [
    ((17495, 277518), 277518),
    ((17495, None), None),
])
def test_the_anidb_episode_id_reaches_the_worker_payload_as_a_single_id(monkeypatch, episode_ids, expected):
    """The AniDB client answers with the series id and the episode id together.
    Only the episode id belongs on the video. Storing the pair sends a two item
    list to workers, and one that puts the value straight into a query string
    then searches for the list instead of the episode."""
    from subliminal import Episode
    from provider_hub.protocol import video_to_payload
    from subtitles.refiners import anidb

    class _AniDBClientWithCredentials(_AniDBClientWithoutCredentials):
        has_api_credentials = True

        def get_episode_ids(self, series_id, episode_no):
            assert (series_id, episode_no) == (17495, 12)
            return episode_ids

    video = Episode("/media/anime.mkv", "Example", 1, 12, series_tvdb_id=101)
    monkeypatch.setattr(anidb, "settings", _enabled("animetosho", api_client="client"))
    monkeypatch.setattr(anidb, "AniDBClient", _AniDBClientWithCredentials)

    anidb.refine_anidb_ids(video)

    assert video.series_anidb_id == 17495
    assert video.series_anidb_episode_id == expected
    assert video_to_payload(video)["series_anidb_episode_id"] == expected


def _anidb_client_failing_in(method, error):
    class _FailingAniDBClient(_AniDBClientWithoutCredentials):
        has_api_credentials = method == "get_episode_ids"

    def fail(self, *args):
        raise error

    setattr(_FailingAniDBClient, method, fail)
    return _FailingAniDBClient


ANIDB_FAILURES = [
    pytest.param("get_show_information", requests.exceptions.ConnectTimeout("anime-list.xml timed out"), None,
                 id="mapping-download-timeout"),
    pytest.param("get_episode_ids", requests.exceptions.HTTPError("AniDB API Client error"), 17495,
                 id="api-client-rejected"),
    pytest.param("get_episode_ids", ValueError(), 17495, id="api-answer-without-episodes"),
]


@pytest.mark.parametrize("method,error,series_id", ANIDB_FAILURES)
def test_a_failed_anidb_lookup_leaves_the_episode_unrefined_instead_of_raising(
        monkeypatch, caplog, method, error, series_id):
    """AniDB refinement is optional metadata for a few anime providers. When a
    lookup fails the episode goes on without the ids it could not get, and the
    failure is logged, instead of the exception escaping into the search. A
    failed episode lookup keeps the series id, which the AniList refiner and
    Jimaku can still use, the same as when there are no API credentials."""
    from subliminal import Episode
    from subtitles.refiners import anidb

    video = Episode("/media/anime.mkv", "Example", 1, 12, series_tvdb_id=101)
    monkeypatch.setattr(anidb, "settings", _enabled("tsukihime", api_client="client"))
    monkeypatch.setattr(anidb, "AniDBClient", _anidb_client_failing_in(method, error))

    with caplog.at_level(logging.WARNING, logger=anidb.logger.name):
        anidb.refine_from_anidb(video.name, video)

    assert video.series_anidb_id == series_id
    assert video.series_anidb_episode_id is None
    warnings = [record for record in caplog.records
                if record.name == anidb.logger.name and record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Example" in warnings[0].getMessage()


class _AniListClientFailing:
    def get_series_id(self, candidate_id_name, candidate_id_value):
        raise requests.exceptions.ConnectTimeout("anime-list-mini.json timed out")


@pytest.mark.parametrize("kind", ["episode", "movie"])
def test_a_failed_anilist_lookup_leaves_the_video_unrefined_instead_of_raising(monkeypatch, caplog, kind):
    from subliminal import Episode, Movie
    from subtitles.refiners import anilist

    if kind == "episode":
        video = Episode("/media/anime.mkv", "Example", 1, 2, series_anidb_id=101)
    else:
        video = Movie("/media/anime-movie.mkv", "Example Movie", imdb_id="tt0001")
    monkeypatch.setattr(anilist, "settings", _enabled("tsukihime"))
    monkeypatch.setattr(anilist, "AniListClient", _AniListClientFailing)

    with caplog.at_level(logging.WARNING, logger=anilist.logger.name):
        anilist.refine_from_anilist(video.name, video)

    assert video.anilist_id is None
    warnings = [record for record in caplog.records
                if record.name == anilist.logger.name and record.levelno == logging.WARNING]
    assert len(warnings) == 1 and video.name in warnings[0].getMessage()


def test_a_failed_anime_lookup_does_not_skip_the_search(monkeypatch):
    """get_video runs every refiner in one try block and gives up on the whole
    item when one raises. Enabling an anime provider must not put every other
    provider's search for the episode at the mercy of the anime list downloads."""
    from subliminal import Episode
    import subtitles.utils as utils
    from subtitles.refiners import anidb, anilist

    video = Episode("/media/anime.mkv", "Example", 1, 12, series_tvdb_id=101)
    failure = requests.exceptions.ConnectTimeout("anime-list.xml timed out")
    monkeypatch.setattr(utils, "parse_video", lambda *args, **kwargs: video)
    monkeypatch.setattr(utils, "registered_refiners",
                        {"anidb": anidb.refine_from_anidb, "anilist": anilist.refine_from_anilist})
    monkeypatch.setattr(anidb, "settings", _enabled("tsukihime", "opensubtitlescom"))
    monkeypatch.setattr(anilist, "settings", _enabled("tsukihime", "opensubtitlescom"))
    monkeypatch.setattr(anidb, "AniDBClient", _anidb_client_failing_in("get_show_information", failure))

    assert utils.get_video(video.name, "Example", "None", media_type="series") is video


def test_a_movie_without_an_imdb_id_is_not_logged_as_an_error(monkeypatch, caplog):
    """With TsukiHime enabled the AniList refiner sees every movie, and most
    libraries have some without an IMDb id. That is expected, not an error."""
    from subliminal import Movie
    from subtitles.refiners import anilist

    class _AniListClientUnused:
        def get_series_id(self, candidate_id_name, candidate_id_value):
            raise AssertionError("there is no id to look up")

    video = Movie("/media/home-video.mkv", "Home Video")
    monkeypatch.setattr(anilist, "settings", _enabled("tsukihime"))
    monkeypatch.setattr(anilist, "AniListClient", _AniListClientUnused)

    with caplog.at_level(logging.DEBUG, logger=anilist.logger.name):
        anilist.refine_from_anilist(video.name, video)

    assert video.anilist_id is None
    assert [record for record in caplog.records
            if record.name == anilist.logger.name and record.levelno >= logging.WARNING] == []

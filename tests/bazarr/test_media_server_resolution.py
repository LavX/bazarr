# coding=utf-8
"""The shared resolution ladder and the metadata it reads for itself."""

from contextlib import nullcontext

import pytest

import app.database  # noqa: F401


def rung(name, result, *, expected="requested", log=None):
    def call():
        if log is not None:
            log.append(name)
        if isinstance(result, Exception):
            raise result
        return result
    return (name, call, expected)


def test_first_resolving_rung_wins_and_no_later_rung_is_reached():
    from media_servers import resolution
    log = []
    assert resolution.walk([
        rung(resolution.PROVIDER_ID, {"status": "requested"}, log=log),
        rung(resolution.TITLE_YEAR, {"status": "requested"}, log=log),
        rung(resolution.PATH, {"status": "requested"}, log=log),
        rung(resolution.LIBRARY, {"status": "requested"}, log=log),
    ]) == (resolution.PROVIDER_ID, {"status": "requested"}, "requested")
    assert log == [resolution.PROVIDER_ID]


def test_returned_none_is_a_miss_that_falls_through_in_order():
    from media_servers import resolution
    log = []
    name, _result, _expected = resolution.walk([
        rung(resolution.PROVIDER_ID, None, log=log),
        rung(resolution.TITLE_YEAR, None, log=log),
        rung(resolution.PATH, {"status": "requested"}, log=log),
        rung(resolution.LIBRARY, {"status": "requested"}, log=log),
    ])
    assert name == resolution.PATH
    assert log == [resolution.PROVIDER_ID, resolution.TITLE_YEAR, resolution.PATH]


def test_a_failure_stops_the_walk_instead_of_reaching_for_something_broader():
    from media_servers import resolution
    from media_servers.http import MediaServerError
    log = []
    with pytest.raises(MediaServerError, match="item_ambiguous"):
        resolution.walk([
            rung(resolution.PATH, MediaServerError("item_ambiguous"), log=log),
            rung(resolution.LIBRARY, {"status": "requested"}, log=log),
        ])
    assert log == [resolution.PATH]


def test_a_refusal_the_adapter_calls_a_miss_falls_through_but_no_other_does():
    from media_servers.dispatcher import _misses_on
    from media_servers.http import MediaServerError

    def refuse(code):
        def call():
            raise MediaServerError(code)
        return call

    assert _misses_on(refuse("item_missing"), ("item_missing",))() is None
    assert _misses_on(refuse("request_rejected"), ("request_rejected",))() is None
    with pytest.raises(MediaServerError, match="item_ambiguous"):
        _misses_on(refuse("item_ambiguous"), ("item_missing",))()
    assert _misses_on(lambda: {"status": "confirmed"}, ("item_missing",))() == {"status": "confirmed"}


def test_a_ladder_with_nothing_left_to_try_resolves_to_nothing():
    from media_servers import resolution
    assert resolution.walk([rung(resolution.PATH, None), rung(resolution.LIBRARY, None)]) is None
    assert resolution.walk([]) is None


def test_a_rung_that_resolves_hands_back_what_it_promised_for_checking():
    from media_servers import resolution
    log = []
    assert resolution.walk([
        rung(resolution.PATH, {"status": "confirmed"}, expected="confirmed", log=log)]) == (
        resolution.PATH, {"status": "confirmed"}, "confirmed")
    # A rung that answered at all ends the walk, right or wrong; judging the
    # answer is the caller's, so a later rung is still never reached.
    assert resolution.walk([
        rung(resolution.PATH, {"status": "requested"}, expected="confirmed", log=log),
        rung(resolution.LIBRARY, {"status": "requested"}, log=log),
    ]) == (resolution.PATH, {"status": "requested"}, "confirmed")
    assert log == [resolution.PATH, resolution.PATH]


def test_the_exact_path_outranks_the_weak_title_rung():
    """A provider id identifies the item on its own, so it stays first.

    A title and a year do not: they are a guess narrowed by two fields, and a
    guess must never pre-empt the one rung that proves which file this is.
    """
    from media_servers import resolution
    assert resolution.CHAIN == (resolution.PROVIDER_ID, resolution.PATH,
                                resolution.TITLE_YEAR, resolution.LIBRARY)


@pytest.mark.parametrize(("media_type", "expected"), [
    ("movie", [("imdb", "tt0017136"), ("tmdb", "19")]),
    ("episode", [("imdb", "tt0303461"), ("tvdb", "78874")]),
])
def test_provider_pairs_keep_jellyfins_precedence_per_media_type(media_type, expected):
    from media_servers.resolution import MediaMetadata
    metadata = MediaMetadata(imdb_id=expected[0][1], tmdb_id="19", tvdb_id=78874, season=1, episode=1)
    assert metadata.provider_ids(media_type) == expected


@pytest.mark.parametrize("metadata", [
    {}, {"imdb_id": ""}, {"tmdb_id": None},
])
def test_absent_identifiers_produce_no_pairs(metadata):
    from media_servers.resolution import MediaMetadata
    assert MediaMetadata(**metadata).provider_ids("movie") == []


def test_an_episode_is_only_locatable_once_the_numbers_are_known():
    from media_servers.resolution import MediaMetadata
    assert MediaMetadata(imdb_id="tt1").locatable("movie") is True
    assert MediaMetadata(imdb_id="tt1").locatable("episode") is False
    assert MediaMetadata(imdb_id="tt1", season=0, episode=0).locatable("episode") is True


# ----------------------------------------------------------- metadata lookup


@pytest.fixture
def stored(schema_session, monkeypatch):
    from app.database import TableEpisodes, TableMovies, TableShows
    from media_servers import resolution
    from utilities.path_mappings import path_mappings

    monkeypatch.setattr(resolution, "_reader", lambda: nullcontext(schema_session))
    monkeypatch.setattr(path_mappings, "path_mapping_series", [["/remote/shows", "/movies"]])
    monkeypatch.setattr(path_mappings, "path_mapping_movies", [["/remote/movies", "/movies"]])
    schema_session.add(TableMovies(
        id=1, arr_instance_id=2, radarrId=11, path="/remote/movies/A.mkv", title="Metropolis",
        year="1927", imdbId="tt0017136", tmdbId="19", subtitles="[]", tags="[]"))
    schema_session.add(TableShows(
        id=5, arr_instance_id=2, sonarrSeriesId=21, path="/remote/shows/Firefly", title="Firefly",
        year="2002", imdbId="tt0303461", tvdbId=78874, tags="[]"))
    schema_session.flush()
    schema_session.add(TableEpisodes(
        id=9, series_id=5, arr_instance_id=2, sonarrSeriesId=21, sonarrEpisodeId=31,
        path="/remote/shows/Firefly/S01E02.mkv", title="The Train Job", season=1, episode=2,
        monitored="True", subtitles="[]"))
    schema_session.flush()
    return schema_session


def test_movie_identifiers_are_read_from_the_row_bazarr_already_wrote(stored):
    from media_servers.resolution import media_metadata
    metadata = media_metadata("movie", "/movies/A.mkv", 2)
    assert (metadata.imdb_id, metadata.tmdb_id, metadata.title, metadata.year) == (
        "tt0017136", "19", "Metropolis", 1927)


def test_episode_identifiers_come_from_the_show_with_the_episode_numbers(stored):
    from media_servers.resolution import media_metadata
    metadata = media_metadata("episode", "/movies/Firefly/S01E02.mkv", 2)
    assert (metadata.imdb_id, metadata.tvdb_id, metadata.title, metadata.year) == (
        "tt0303461", 78874, "Firefly", 2002)
    assert (metadata.season, metadata.episode) == (1, 2)


def test_lookup_is_scoped_to_the_owning_arr_instance(stored):
    from media_servers.resolution import media_metadata
    assert media_metadata("movie", "/movies/A.mkv", 3) is None
    assert media_metadata("episode", "/movies/Firefly/S01E02.mkv", 3) is None


def test_unknown_path_and_colliding_rows_resolve_to_nothing(stored):
    from app.database import TableMovies
    from media_servers.resolution import media_metadata
    assert media_metadata("movie", "/movies/Missing.mkv", 2) is None
    # Two owners sharing one path cannot identify a single item, so an
    # unscoped publication must not guess between them.
    stored.add(TableMovies(id=2, arr_instance_id=4, radarrId=12, path="/remote/movies/A.mkv",
                           title="Other", year="1927", imdbId="tt9", tmdbId="20",
                           subtitles="[]", tags="[]"))
    stored.flush()
    assert media_metadata("movie", "/movies/A.mkv", None) is None


def test_a_broken_database_read_is_never_fatal_to_a_refresh(monkeypatch):
    from media_servers import resolution
    monkeypatch.setattr(resolution, "_reader", lambda: (_ for _ in ()).throw(RuntimeError("no database")))
    assert resolution.media_metadata("movie", "/movies/A.mkv", 2) is None

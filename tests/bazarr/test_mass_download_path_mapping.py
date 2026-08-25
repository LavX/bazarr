# coding=utf-8
"""Mass download must map each media kind with its own path mapping.

Episode paths come from Sonarr and belong to the series mapping
(``settings.general.path_mappings``); movie paths come from Radarr and belong
to the movie mapping (``settings.general.path_mappings_movie``). Reindexing an
episode whose ``subtitles`` column is NULL used the movie mapping, so with the
two mappings configured differently the indexer was handed a path that does not
exist on disk and the reindex silently did nothing.

These tests pin the mapping used per media kind at both mass-download reindex
call sites, using a real ``PathMappings`` instance rather than stubs so the
mapping semantics themselves are exercised.
"""

from types import SimpleNamespace
from unittest.mock import Mock, call

from sqlalchemy import insert

SERIES_MAPPING = [["/media/tv", "/tv"]]
MOVIES_MAPPING = [["/media/movies", "/movies"]]

EPISODE_REMOTE_PATH = "/media/tv/Show/Season 01/Show.S01E01.mkv"
EPISODE_LOCAL_PATH = "/tv/Show/Season 01/Show.S01E01.mkv"

MOVIE_REMOTE_PATH = "/media/movies/Film (2020)/Film.mkv"
MOVIE_LOCAL_PATH = "/movies/Film (2020)/Film.mkv"


def _path_mappings(monkeypatch):
    """A real PathMappings with distinct series and movie mappings.

    The two mappings do not overlap, so applying the wrong one leaves the
    remote prefix in place and the difference is observable.
    """
    import utilities.path_mappings as pm_mod

    monkeypatch.setattr(pm_mod, "settings", SimpleNamespace(
        general=SimpleNamespace(path_mappings=SERIES_MAPPING,
                                path_mappings_movie=MOVIES_MAPPING)))
    pm = pm_mod.PathMappings()
    pm.update()
    return pm


def _seed_episode(session):
    from app.database import TableEpisodes, TableShows

    session.execute(insert(TableShows).values(
        id=100,
        sonarrSeriesId=10,
        arr_instance_id=1,
        path="/media/tv/Show",
        title="Show",
        imdbId="tt-show",
        tvdbId=100,
        monitored="True",
        seriesType="standard",
        tags="[]",
    ))
    session.execute(insert(TableEpisodes).values(
        id=101,
        series_id=100,
        sonarrSeriesId=10,
        sonarrEpisodeId=20,
        arr_instance_id=1,
        path=EPISODE_REMOTE_PATH,
        title="Pilot",
        season=1,
        episode=1,
        monitored="True",
        audio_language="English",
        sceneName="Show.S01E01",
        missing_subtitles="['en']",
        failedAttempts="[]",
        # NULL subtitles: indexing for this episode is incomplete, which is the
        # branch that reindexes the file.
        subtitles=None,
    ))


def _seed_movie(session):
    from app.database import TableMovies

    session.execute(insert(TableMovies).values(
        id=301,
        radarrId=30,
        arr_instance_id=1,
        path=MOVIE_REMOTE_PATH,
        title="Film",
        year="2020",
        tmdbId="300",
        imdbId="tt-film",
        monitored="True",
        tags="[]",
        audio_language="English",
        sceneName="Film.2020",
        missing_subtitles="['en']",
        failedAttempts="[]",
        subtitles=None,
    ))


def test_episode_reindex_uses_the_series_path_mapping(schema_session, monkeypatch):
    import subtitles.mass_download.series as mass_download_series

    _seed_episode(schema_session)

    store_subtitles = Mock()
    monkeypatch.setattr(mass_download_series, "database", schema_session)
    monkeypatch.setattr(mass_download_series, "store_subtitles", store_subtitles)
    monkeypatch.setattr(mass_download_series, "path_mappings", _path_mappings(monkeypatch))
    monkeypatch.setattr(mass_download_series, "jobs_queue", Mock())
    monkeypatch.setattr(mass_download_series, "get_exclusion_clause", lambda _: [])
    monkeypatch.setattr(mass_download_series, "get_providers", lambda: [])
    monkeypatch.setattr(mass_download_series.os.path, "exists", lambda path: True)

    mass_download_series.episode_download_subtitles(20, job_id="job", job_sub_function=True)

    assert store_subtitles.call_args_list == [call(EPISODE_REMOTE_PATH, EPISODE_LOCAL_PATH)], (
        "an episode path must be mapped with the series mapping, not the movie mapping")


def test_movie_reindex_uses_the_movie_path_mapping(schema_session, monkeypatch):
    import subtitles.mass_download.movies as mass_download_movies

    _seed_movie(schema_session)

    store_subtitles_movie = Mock()
    monkeypatch.setattr(mass_download_movies, "database", schema_session)
    monkeypatch.setattr(mass_download_movies, "store_subtitles_movie", store_subtitles_movie)
    monkeypatch.setattr(mass_download_movies, "path_mappings", _path_mappings(monkeypatch))
    monkeypatch.setattr(mass_download_movies, "jobs_queue", Mock())
    monkeypatch.setattr(mass_download_movies, "get_exclusion_clause", lambda _: [])
    monkeypatch.setattr(mass_download_movies, "get_providers", lambda: [])
    monkeypatch.setattr(mass_download_movies, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(mass_download_movies.os.path, "exists", lambda path: True)

    mass_download_movies.movies_download_subtitles(30, job_id="job", job_sub_function=True)

    assert store_subtitles_movie.call_args_list == [call(MOVIE_REMOTE_PATH, MOVIE_LOCAL_PATH)], (
        "a movie path must be mapped with the movie mapping")

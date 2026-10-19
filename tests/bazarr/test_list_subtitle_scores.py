# coding=utf-8

import os
from datetime import datetime, timedelta

import pytest
from flask import Flask

_PG_URL = os.environ.get(
    "BAZARR_PG_TEST_URL",
    "postgresql+psycopg2://postgres:test@127.0.0.1:55432/bazarr")


def _pg_session():
    import sqlalchemy as sa
    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import Base

    try:
        engine = sa.create_engine(_PG_URL)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        if os.environ.get("BAZARR_PG_TEST_URL"):
            pytest.fail(f"BAZARR_PG_TEST_URL is set, but PostgreSQL is not usable: {exc}")
        pytest.skip(f"PostgreSQL not reachable at the default URL: {exc}")

    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    return engine, scoped_session(sessionmaker(bind=engine))


@pytest.fixture(params=["sqlite", "postgresql"])
def schema_session(request, schema_session):
    """Run every scored-list case on both back ends: the history read is SQL
    whose ordering and reduction must agree on SQLite and PostgreSQL."""
    if request.param == "sqlite":
        yield schema_session
        return
    engine, session = _pg_session()
    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


def _history_timestamp(history_id):
    return datetime(2026, 6, 12, 12, 0) + timedelta(seconds=history_id)


# --------------------------------------------------------------------------- #
# Movies                                                                       #
# --------------------------------------------------------------------------- #

def _prepare_movies(monkeypatch, schema_session):
    from api import utils
    from api.movies import movies

    monkeypatch.setattr(movies, "database", schema_session)
    monkeypatch.setattr(utils, "language_from_alpha2",
                        lambda value: {"en": "English", "es": "Spanish"}.get(value, value))
    monkeypatch.setattr(utils, "alpha3_from_alpha2",
                        lambda value: {"en": "eng", "es": "spa"}.get(value, value))
    return movies


def _add_movie(schema_session, movie_id, subtitles):
    from app.database import TableMovies

    schema_session.add(TableMovies(
        id=movie_id,
        radarrId=movie_id,
        arr_instance_id=1,
        path=f"/movies/movie{movie_id}.mkv",
        title=f"Movie {movie_id}",
        sortTitle=f"movie {movie_id}",
        tmdbId=str(movie_id),
        monitored="True",
        # profileId left unset so postprocess skips the desired-language filter.
        subtitles=subtitles,
        tags="[]",
    ))
    schema_session.flush()


def _add_movie_history(schema_session, history_id, movie_id, action, subtitles_path,
                       language, score, score_out_of, video_path=None):
    from app.database import TableHistoryMovie, TableMovies

    if video_path is None:
        # History writers record the movie's own video path.
        video_path = schema_session.get(TableMovies, movie_id).path
    schema_session.add(TableHistoryMovie(
        id=history_id,
        movie_id=movie_id,
        radarrId=movie_id,
        arr_instance_id=1,
        action=action,
        description=f"history {history_id}",
        language=language,
        provider="provider",
        subtitles_path=subtitles_path,
        video_path=video_path,
        score=score,
        score_out_of=score_out_of,
        timestamp=_history_timestamp(history_id),
    ))
    schema_session.flush()


def _get_movies(movies, query):
    app = Flask(__name__)
    with app.test_request_context(query):
        return movies.Movies.get.__wrapped__(movies.Movies())


def test_movies_scores_absent_leaves_response_unchanged(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100, "[['en', '/movies/movie100.en.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, "/movies/movie100.en.srt", "en", 90, 100)

    # The scoring helper must not run at all when the param is absent.
    def _boom(*args, **kwargs):
        raise AssertionError("lowest_subtitle_scores must not be called when scores is off")

    monkeypatch.setattr(movies, "lowest_subtitle_scores", _boom)

    result = _get_movies(movies, "/api/movies/movies?id[]=100")

    assert "lowest_subtitle_score" not in result["data"][0]


def test_movies_lowest_score_is_min_of_current_subtitles(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100,
               "[['en', '/movies/movie100.en.srt', 100], ['es', '/movies/movie100.es.srt', 200]]")
    _add_movie_history(schema_session, 1, 100, 1, "/movies/movie100.en.srt", "en", 90, 100)
    _add_movie_history(schema_session, 2, 100, 2, "/movies/movie100.es.srt", "es", 70, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] == 70.0


def test_movies_scores_match_a_mapped_translation_of_the_same_file(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    # The indexer records the instance-mapped path; the translation history row
    # carries the globally mapped path for the same file. The translation
    # replaced the download, so the old provider score must not survive.
    _add_movie(schema_session, 100, "[['de', '/instance/tv/movie100.de.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, "/instance/tv/movie100.de.srt", "de", 90, 100)
    _add_movie_history(schema_session, 2, 100, 6, "/global/tv/movie100.de.srt", "de", 50, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_movies_scores_match_windows_paths_under_different_roots(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    # A Linux host stores a Windows instance's paths unchanged. Windows names
    # resolve case-insensitively, so two roots and casings name the same file
    # and the newer translation shadows the download's score.
    _add_movie(schema_session, 100, r"[['de', 'X:\\TV\\Movie.DE.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, r"X:\TV\movie.de.srt", "de", 90, 100)
    _add_movie_history(schema_session, 2, 100, 6, r"Y:\tv\movie.de.srt", "de", 50, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_subtitle_file_name_splits_both_separator_styles():
    from api.utils import _subtitle_file_name

    assert _subtitle_file_name(r"X:\TV\Show\episode.de.srt") == ("episode.de.srt", True)
    assert _subtitle_file_name(r"\\server\share\tv\episode.de.srt") == ("episode.de.srt", True)
    assert _subtitle_file_name("X:/TV/Show/episode.de.srt") == ("episode.de.srt", True)
    assert _subtitle_file_name("/tv/Show/episode.de.srt") == ("episode.de.srt", False)


def test_movies_scores_match_mixed_windows_and_linux_paths(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    # The index holds the Windows remote path, the translation history the
    # mapped Linux path for the same file, with different casing. The Windows
    # side resolves the name case-insensitively, so the newer translation
    # shadows the download's score.
    _add_movie(schema_session, 100, r"[['de', 'X:\\TV\\Movie.DE.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, r"X:\TV\Movie.DE.srt", "de", 90, 100)
    _add_movie_history(schema_session, 2, 100, 6, "/media/tv/movie.de.srt", "de", 50, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_movies_case_distinct_unix_subtitles_stay_distinct(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    # Two different files on a case-sensitive filesystem. Each keeps its own
    # history row, so the lower score counts.
    _add_movie(schema_session, 100,
               "[['en', '/tv/Movie.EN.srt', 100], ['en', '/tv/movie.en.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, "/tv/Movie.EN.srt", "en", 40, 100)
    _add_movie_history(schema_session, 2, 100, 1, "/tv/movie.en.srt", "en", 90, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] == 40.0


@pytest.mark.parametrize("current, history", [
    # Drive-letter Windows paths can use forward slashes, and the mapping can
    # turn either side into a Linux path with different casing.
    ("X:/TV/Movie.DE.srt", "/media/tv/movie.de.srt"),
    ("/media/tv/Movie.DE.srt", "X:/TV/movie.de.srt"),
])
def test_movies_scores_match_forward_slash_windows_paths(schema_session, monkeypatch, current, history):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100, f"[['de', '{current}', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, current, "de", 90, 100)
    _add_movie_history(schema_session, 2, 100, 6, history, "de", 50, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_movies_ignores_history_for_deleted_subtitle_path(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100, "[['en', '/movies/movie100.en.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, "/movies/movie100.en.srt", "en", 90, 100)
    # A worse score for a subtitle that is no longer among the movie's files.
    _add_movie_history(schema_session, 2, 100, 1, "/movies/movie100.deleted.srt", "en", 40, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] == 90.0


def test_movies_embedded_track_is_scored_and_membership_enforced(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    # Only an embedded English track (empty path) is current.
    _add_movie(schema_session, 100, "[['en', None, None]]")
    _add_movie_history(schema_session, 1, 100, 7, None, "en", 100, 100)
    # An embedded Spanish record whose track the movie no longer has: ignored.
    _add_movie_history(schema_session, 2, 100, 7, None, "es", 50, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] == 100.0


def test_movies_skips_records_without_score_out_of(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100, "[['en', '/movies/movie100.en.srt', 100]]")
    _add_movie_history(schema_session, 1, 100, 1, "/movies/movie100.en.srt", "en", 88, None)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_movies_without_scored_subtitles_returns_null(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100, "[['en', '/movies/movie100.en.srt', 100]]")
    # No history at all.

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_movies_newer_unscored_record_shadows_older_scored(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    _add_movie(schema_session, 100, "[['en', '/movies/movie100.en.srt', 100]]")
    # An older scored record for the current file...
    _add_movie_history(schema_session, 1, 100, 1, "/movies/movie100.en.srt", "en", 90, 100)
    # ...shadowed by a newer record for the SAME path that carries no score (a
    # re-download whose score was not recorded). The seen-set keeps only the
    # newest record per subtitle, so the older score never counts and the movie
    # ends up with no known score.
    _add_movie_history(schema_session, 2, 100, 1, "/movies/movie100.en.srt", "en", None, None)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


def test_movies_embedded_multivariant_language_matches_canonical_record(schema_session, monkeypatch):
    movies = _prepare_movies(monkeypatch, schema_session)
    # The current embedded track carries a combined code ("en:hi:forced"), while
    # the action-7 record stores the canonical hi-priority variant ("en:hi").
    # The two must still be recognised as the same track.
    _add_movie(schema_session, 100, "[['en:hi:forced', None, None]]")
    _add_movie_history(schema_session, 1, 100, 7, None, "en:hi", 100, 100)

    result = _get_movies(movies, "/api/movies/movies?id[]=100&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] == 100.0


# --------------------------------------------------------------------------- #
# Series                                                                       #
# --------------------------------------------------------------------------- #

def _prepare_series(monkeypatch, schema_session):
    from api.series import series

    monkeypatch.setattr(series, "database", schema_session)
    monkeypatch.setattr(series, "get_exclusion_clause", lambda media_type: [])
    return series


def _add_series(schema_session, series_id):
    from app.database import TableShows

    schema_session.add(TableShows(
        id=series_id,
        sonarrSeriesId=series_id,
        arr_instance_id=1,
        path=f"/series/show{series_id}",
        title=f"Show {series_id}",
        sortTitle=f"show {series_id}",
        tags="[]",
    ))
    schema_session.flush()


def _add_episode(schema_session, episode_id, series_id, season, episode, subtitles):
    from app.database import TableEpisodes

    schema_session.add(TableEpisodes(
        id=episode_id,
        series_id=series_id,
        sonarrEpisodeId=episode_id,
        sonarrSeriesId=series_id,
        arr_instance_id=1,
        path=f"/series/show{series_id}/s{season:02d}e{episode:02d}.mkv",
        title=f"Episode {episode_id}",
        season=season,
        episode=episode,
        subtitles=subtitles,
    ))
    schema_session.flush()


def _add_episode_history(schema_session, history_id, series_id, episode_id, action,
                         subtitles_path, language, score, score_out_of, video_path=None):
    from app.database import TableEpisodes, TableHistory

    if video_path is None:
        # History writers record the episode's own video path.
        video_path = schema_session.get(TableEpisodes, episode_id).path
    schema_session.add(TableHistory(
        id=history_id,
        series_id=series_id,
        episode_id=episode_id,
        sonarrSeriesId=series_id,
        sonarrEpisodeId=episode_id,
        arr_instance_id=1,
        action=action,
        description=f"history {history_id}",
        language=language,
        provider="provider",
        subtitles_path=subtitles_path,
        video_path=video_path,
        score=score,
        score_out_of=score_out_of,
        timestamp=_history_timestamp(history_id),
    ))
    schema_session.flush()


def _get_series(series, query):
    app = Flask(__name__)
    with app.test_request_context(query):
        return series.Series.get.__wrapped__(series.Series())


def test_series_scores_absent_leaves_response_unchanged(schema_session, monkeypatch):
    series = _prepare_series(monkeypatch, schema_session)
    _add_series(schema_session, 501)
    _add_episode(schema_session, 601, 501, 1, 1, "[['en', '/series/show501/s01e01.en.srt', 100]]")
    _add_episode_history(schema_session, 1, 501, 601, 1, "/series/show501/s01e01.en.srt", "en", 90, 100)

    result = _get_series(series, "/api/series?id[]=501")

    assert "lowest_subtitle_score" not in result["data"][0]


def test_series_aggregate_is_global_minimum_across_episodes(schema_session, monkeypatch):
    series = _prepare_series(monkeypatch, schema_session)
    _add_series(schema_session, 501)
    _add_episode(schema_session, 601, 501, 1, 1, "[['en', '/series/show501/s01e01.en.srt', 100]]")
    _add_episode(schema_session, 602, 501, 1, 2, "[['en', '/series/show501/s01e02.en.srt', 100]]")
    _add_episode_history(schema_session, 1, 501, 601, 1, "/series/show501/s01e01.en.srt", "en", 90, 100)
    _add_episode_history(schema_session, 2, 501, 602, 1, "/series/show501/s01e02.en.srt", "en", 60, 100)

    result = _get_series(series, "/api/series?id[]=501&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] == 60.0


def test_series_without_scored_subtitles_returns_null(schema_session, monkeypatch):
    series = _prepare_series(monkeypatch, schema_session)
    _add_series(schema_session, 501)
    _add_episode(schema_session, 601, 501, 1, 1, "[['en', '/series/show501/s01e01.en.srt', 100]]")
    # Episode exists with a current subtitle but no scored history.

    result = _get_series(series, "/api/series?id[]=501&scores=1")

    assert result["data"][0]["lowest_subtitle_score"] is None


@pytest.fixture(params=['movie', 'series'])
def scored_media(request, monkeypatch, schema_session):
    """Exercise the same history changes through both public list endpoints."""
    if request.param == 'movie':
        api = _prepare_movies(monkeypatch, schema_session)
        path = '/movies/movie100.en.srt'
        _add_movie(schema_session, 100, repr([['en', path, 100]]))

        def add(history_id, action, score, score_out_of, language='en', subtitles_path=path):
            _add_movie_history(schema_session, history_id, 100, action,
                               subtitles_path, language, score, score_out_of)

        def score():
            return _get_movies(api, '/api/movies?id[]=100&scores=1')['data'][0]['lowest_subtitle_score']

        def add_full_other_subtitle():
            from app.database import TableMovies
            item = schema_session.get(TableMovies, 100)
            item.subtitles = repr([['en', path, 100], ['es', '/movies/movie100.es.srt', 100]])
            schema_session.flush()
            add(3, 1, 100, 100, 'es', '/movies/movie100.es.srt')
    else:
        api = _prepare_series(monkeypatch, schema_session)
        path = '/series/show501/s01e01.en.srt'
        _add_series(schema_session, 501)
        _add_episode(schema_session, 601, 501, 1, 1, repr([['en', path, 100]]))

        def add(history_id, action, score, score_out_of, language='en', subtitles_path=path):
            _add_episode_history(schema_session, history_id, 501, 601, action,
                                 subtitles_path, language, score, score_out_of)

        def score():
            return _get_series(api, '/api/series?id[]=501&scores=1')['data'][0]['lowest_subtitle_score']

        def add_full_other_subtitle():
            _add_episode(schema_session, 602, 501, 1, 2,
                         repr([['en', '/series/show501/s01e02.en.srt', 100]]))
            _add_episode_history(schema_session, 3, 501, 602, 1,
                                 '/series/show501/s01e02.en.srt', 'en', 100, 100)

    return add, score, add_full_other_subtitle


@pytest.mark.parametrize('action', [0, 4, 6], ids=['delete-then-reindex', 'upload', 'translation'])
def test_latest_replacement_shadows_download_score(scored_media, action):
    add, score, _ = scored_media
    add(1, 1, 90, 100)
    # Upload/translation can store a synthetic 100, but the detail page treats
    # these replacements as having no provider-match score.
    add(2, action, 100 if action in (4, 6) else None, 100 if action in (4, 6) else None)
    assert score() is None


@pytest.mark.parametrize('action', [0, 4, 6], ids=['deletion', 'upload', 'translation'])
def test_later_download_restores_score(scored_media, action):
    add, score, _ = scored_media
    add(1, 1, 90, 100)
    add(2, action, None, None)
    add(3, 2, 95, 100)
    assert score() == 95.0


@pytest.mark.parametrize('score_out_of', [100, None], ids=['with-denominator', 'legacy-zero'])
@pytest.mark.parametrize('with_full_other', [False, True], ids=['only-zero', 'zero-and-full'])
def test_zero_score_is_preserved(scored_media, score_out_of, with_full_other):
    add, score, add_full_other = scored_media
    add(1, 1, 90, 100)
    add(2, 1, 0, score_out_of)
    if with_full_other:
        add_full_other()
    assert score() == 0.0


def test_sync_preserves_download_score(scored_media):
    add, score, _ = scored_media
    add(1, 1, 90, 100)
    add(2, 5, None, None)
    assert score() == 90.0


@pytest.fixture(params=['movie', 'series'])
def placed_media(request, monkeypatch, schema_session):
    """One movie or one episode at a chosen video and subtitle path, owned by
    instance 1, with optional instance and global path mappings."""
    import json

    from app.database import TableArrInstances, TableEpisodes, TableMovies
    from utilities.path_mappings import path_mappings

    movie = request.param == 'movie'
    # The instance mapping is read through the application session.
    monkeypatch.setattr('app.database.database', schema_session)

    def place(video_path, subtitles_path, instance_mapping=None, global_mapping=None):
        if instance_mapping is not None:
            schema_session.add(TableArrInstances(
                id=1, kind='radarr' if movie else 'sonarr', stable_key='k1', name='i1',
                enabled=1, is_default=1, ip='127.0.0.1', port=7878, base_url='/', ssl=0,
                verify_ssl=0, http_timeout=60, api_key='',
                path_mappings=json.dumps({'movies' if movie else 'series': instance_mapping})))
            schema_session.flush()
        monkeypatch.setattr(path_mappings, 'path_mapping_movies' if movie else 'path_mapping_series',
                            global_mapping or [])
        subtitles = repr([['de', subtitles_path, 100]])
        if movie:
            api = _prepare_movies(monkeypatch, schema_session)
            _add_movie(schema_session, 100, subtitles)
            schema_session.get(TableMovies, 100).path = video_path
        else:
            api = _prepare_series(monkeypatch, schema_session)
            _add_series(schema_session, 501)
            _add_episode(schema_session, 601, 501, 1, 1, subtitles)
            schema_session.get(TableEpisodes, 601).path = video_path
        schema_session.flush()

        def add(history_id, action, score, subtitles_path, video_path):
            score_out_of = 100 if score is not None else None
            if movie:
                _add_movie_history(schema_session, history_id, 100, action, subtitles_path, 'de',
                                   score, score_out_of, video_path=video_path)
            else:
                _add_episode_history(schema_session, history_id, 501, 601, action, subtitles_path,
                                     'de', score, score_out_of, video_path=video_path)

        def score():
            if movie:
                result = _get_movies(api, '/api/movies?id[]=100&scores=1')
            else:
                result = _get_series(api, '/api/series?id[]=501&scores=1')
            return result['data'][0]['lowest_subtitle_score']

        return add, score

    return place


def test_moved_item_does_not_inherit_the_old_folder_score(placed_media):
    # The item moved and kept its subtitle file name. The detail view matches
    # the full subtitle path, so it shows no score for the old record.
    add, score = placed_media('/media/new/Title.mkv', '/media/new/Title.de.srt')
    add(1, 1, 40, '/media/old/Title.de.srt', '/media/old/Title.mkv')
    assert score() is None


def test_moved_item_scores_a_download_in_its_new_folder(placed_media):
    add, score = placed_media('/media/new/Title.mkv', '/media/new/Title.de.srt')
    add(1, 1, 40, '/media/old/Title.de.srt', '/media/old/Title.mkv')
    add(2, 1, 90, '/media/new/Title.de.srt', '/media/new/Title.mkv')
    assert score() == 90.0


@pytest.mark.parametrize('item, instance_mapping, global_mapping, translation', [
    # The translation logs the globally mapped root, the item the instance's.
    (('/arr/media/Title.mkv', '/arr/media/Title.de.srt'),
     [['/arr/media', '/local/media']], [['/global/media', '/local/media']],
     ('/global/media/Title.de.srt', '/global/media/Title.mkv')),
    # The translation logs the local path the mapping turns the item into.
    (('/arr/media/Title.mkv', '/arr/media/Title.de.srt'),
     [['/arr/media', '/local/media']], None,
     ('/local/media/Title.de.srt', '/local/media/Title.mkv')),
    # A Windows instance mapped to a Linux root, with different casing.
    ((r'X:\Media\Title.mkv', r'X:\Media\Title.de.srt'),
     [['X:\\Media', '/local/media']], None,
     ('/local/media/title.de.srt', '/local/media/title.mkv')),
    # Windows paths resolve case-insensitively without any mapping.
    ((r'X:\Media\Title.mkv', r'X:\Media\Title.de.srt'), None, None,
     (r'x:\media\title.de.srt', r'x:\media\title.mkv')),
], ids=['global-root', 'local-root', 'windows-to-linux', 'windows-case'])
def test_translation_under_a_mapped_root_shadows_the_download(
        placed_media, item, instance_mapping, global_mapping, translation):
    add, score = placed_media(*item, instance_mapping=instance_mapping, global_mapping=global_mapping)
    add(1, 1, 90, item[1], item[0])
    add(2, 6, 100, *translation)
    assert score() is None


def test_translation_for_another_folder_under_a_mapped_root_does_not_shadow(placed_media):
    add, score = placed_media('/arr/media/Title.mkv', '/arr/media/Title.de.srt',
                              instance_mapping=[['/arr/media', '/local/media']],
                              global_mapping=[['/global/media', '/local/media']])
    add(1, 1, 90, '/arr/media/Title.de.srt', '/arr/media/Title.mkv')
    add(2, 6, 100, '/global/media/Other/Title.de.srt', '/global/media/Other/Title.mkv')
    assert score() == 90.0


def test_unix_video_paths_differing_only_by_case_are_two_files(placed_media):
    add, score = placed_media('/media/Title.mkv', '/media/Title.de.srt')
    add(1, 1, 40, '/media/Title.de.srt', '/media/title.mkv')
    assert score() is None


class _CountingSession:
    """Count the history rows the score query hands back to Python."""

    def __init__(self, session):
        self.session = session
        self.rows = 0

    def execute(self, statement):
        from types import SimpleNamespace

        rows = self.session.execute(statement).all()
        self.rows += len(rows)
        return SimpleNamespace(all=lambda: rows)


@pytest.mark.parametrize('media', ['movie', 'series'])
def test_deep_history_returns_one_row_per_path_and_track(schema_session, monkeypatch, media):
    from api.utils import lowest_subtitle_scores
    from app.database import TableHistory, TableHistoryMovie

    subtitles = repr([['en', '/media/a.en.srt', 100], ['es', '/media/a.es.srt', 100],
                      ['fr', None, None]])
    other = repr([['en', '/media/b.en.srt', 100]])
    if media == 'movie':
        _prepare_movies(monkeypatch, schema_session)
        _add_movie(schema_session, 100, subtitles)
        _add_movie(schema_session, 101, other)

        def add(history_id, item_id, action, path, language, score):
            _add_movie_history(schema_session, history_id, item_id, action, path, language,
                               score, 100 if score is not None else None)
        id_column, table = TableHistoryMovie.movie_id, TableHistoryMovie
        items = (100, 101)
    else:
        _prepare_series(monkeypatch, schema_session)
        _add_series(schema_session, 501)
        _add_episode(schema_session, 601, 501, 1, 1, subtitles)
        _add_episode(schema_session, 602, 501, 1, 2, other)

        def add(history_id, item_id, action, path, language, score):
            _add_episode_history(schema_session, history_id, 501, item_id, action, path, language,
                                 score, 100 if score is not None else None)
        id_column, table = TableHistory.episode_id, TableHistory
        items = (601, 602)
    first, second = items

    history_id = 0
    for depth in range(30):
        history_id += 1
        add(history_id, first, 1 if depth % 2 else 3, '/media/a.en.srt', 'en', 50 + depth)
    # A newer deletion with no score still suppresses every older download.
    history_id += 1
    add(history_id, first, 0, '/media/a.en.srt', 'en', None)
    for depth in range(15):
        history_id += 1
        add(history_id, first, 1, '/media/a.es.srt', 'es', 66 + depth)
    for _ in range(10):
        history_id += 1
        add(history_id, first, 7, None, 'fr', 100)
    for depth in range(20):
        history_id += 1
        add(history_id, second, 2, '/media/b.en.srt', 'en', 70 + depth)
    # Syncs never count and stay out of the query.
    for _ in range(5):
        history_id += 1
        add(history_id, first, 5, '/media/a.es.srt', 'es', None)

    from app.database import TableEpisodes, TableMovies
    model = TableMovies if media == 'movie' else TableEpisodes
    records = {item: schema_session.get(model, item) for item in items}
    counting = _CountingSession(schema_session)
    scores = lowest_subtitle_scores(
        counting, {item: records[item].subtitles for item in items}, id_column, table,
        videos_by_id={item: (records[item].path, records[item].arr_instance_id) for item in items},
        media_type='movie' if media == 'movie' else 'series')

    assert scores == {first: 80.0, second: 89.0}
    # Three files and one embedded track, whatever the history depth.
    assert counting.rows == 4


@pytest.mark.parametrize('video_path, subtitles_path', [
    ('X:/Media/Title.mkv', 'X:/Media/Title.de.srt'),
    (r'x:\media\Title.mkv', r'x:\media\Title.de.srt'),
], ids=['forward-slashes', 'lower-case-root'])
def test_windows_root_maps_whatever_its_separators_and_case(placed_media, video_path, subtitles_path):
    # The mapping names the root as X:\Media; the indexed video spells it with
    # other separators or casing, and Windows resolves both to the same folder.
    add, score = placed_media(video_path, subtitles_path,
                              instance_mapping=[['X:\\Media', '/local/media']])
    add(1, 1, 90, subtitles_path, '/local/media/Title.mkv')
    assert score() == 90.0


def test_move_between_overlapping_mapped_roots_does_not_inherit_the_old_score(placed_media):
    # /local is both where /remote maps to and a remote root of its own, so the
    # old record's /local/Title.mkv is the file now at /archive, not the item's.
    add, score = placed_media('/remote/Title.mkv', '/remote/Title.de.srt',
                              instance_mapping=[['/remote', '/local'], ['/local', '/archive']])
    add(1, 1, 40, '/local/Title.de.srt', '/local/Title.mkv')
    assert score() is None

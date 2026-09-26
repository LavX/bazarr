# coding=utf-8

from datetime import datetime

from flask import Flask


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
                       language, score, score_out_of):
    from app.database import TableHistoryMovie

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
        video_path=f"/movies/movie{movie_id}.mkv",
        score=score,
        score_out_of=score_out_of,
        timestamp=datetime(2026, 6, 12, 12, 0, history_id),
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
                         subtitles_path, language, score, score_out_of):
    from app.database import TableHistory

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
        video_path=f"/series/ep{episode_id}.mkv",
        score=score,
        score_out_of=score_out_of,
        timestamp=datetime(2026, 6, 12, 12, 0, history_id),
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

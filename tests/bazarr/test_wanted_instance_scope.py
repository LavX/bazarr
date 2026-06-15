# coding=utf-8

from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import insert, select


def _seed_colliding_episodes(session):
    from app.database import TableEpisodes, TableShows

    session.execute(insert(TableShows).values(
        id=100,
        sonarrSeriesId=10,
        arr_instance_id=1,
        path="/series/default",
        title="Default",
        imdbId="tt-default",
        tvdbId=100,
    ))
    session.execute(insert(TableShows).values(
        id=200,
        sonarrSeriesId=10,
        arr_instance_id=2,
        path="/series/anime",
        title="Anime",
        imdbId="tt-anime",
        tvdbId=200,
    ))
    session.execute(insert(TableEpisodes).values(
        id=101,
        series_id=100,
        sonarrSeriesId=10,
        sonarrEpisodeId=20,
        arr_instance_id=1,
        path="/series/default/s01e01.mkv",
        title="Pilot",
        season=1,
        episode=1,
        audio_language="English",
        sceneName="Default.Scene",
        missing_subtitles="['en']",
        failedAttempts="[]",
        subtitles="[]",
    ))
    session.execute(insert(TableEpisodes).values(
        id=201,
        series_id=200,
        sonarrSeriesId=10,
        sonarrEpisodeId=20,
        arr_instance_id=2,
        path="/series/anime/s01e01.mkv",
        title="Pilot",
        season=1,
        episode=1,
        audio_language="Japanese",
        sceneName="Anime.Scene",
        missing_subtitles="['en']",
        failedAttempts="[]",
        subtitles="[]",
    ))


def _seed_colliding_movies(session):
    from app.database import TableMovies

    session.execute(insert(TableMovies).values(
        id=301,
        radarrId=30,
        arr_instance_id=1,
        path="/movies/default.mkv",
        title="Default Movie",
        tmdbId="300",
        imdbId="tt-default",
        audio_language="English",
        sceneName="Default.Movie",
        missing_subtitles="['en']",
        failedAttempts="[]",
        subtitles="[]",
    ))
    session.execute(insert(TableMovies).values(
        id=303,
        radarrId=30,
        arr_instance_id=3,
        path="/movies/uhd.mkv",
        title="UHD Movie",
        tmdbId="303",
        imdbId="tt-uhd",
        audio_language="English",
        sceneName="UHD.Movie",
        missing_subtitles="['en']",
        failedAttempts="[]",
        subtitles="[]",
    ))


def _download_result(path):
    return SimpleNamespace(
        message="Downloaded",
        path=path,
        language_code="en",
        provider="provider",
        score=100,
        subs_id="sub-1",
        subs_path=path.replace(".mkv", ".en.srt"),
        matched=[],
        not_matched=[],
    )


def test_wanted_episode_failed_attempts_are_scoped_to_requested_instance(schema_session, monkeypatch):
    from app.database import TableEpisodes
    import subtitles.wanted.series as wanted_series

    _seed_colliding_episodes(schema_session)

    monkeypatch.setattr(wanted_series, "database", schema_session)
    monkeypatch.setattr(wanted_series, "get_providers", lambda: ["provider"])
    monkeypatch.setattr(wanted_series, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_series, "get_profiles_list", lambda profile_id: None)
    monkeypatch.setattr(wanted_series, "is_search_active", lambda desired_language, attempt_string: True)
    monkeypatch.setattr(wanted_series, "updateFailedAttempts",
                        lambda desired_language, attempt_string: "scoped-attempt")
    monkeypatch.setattr(wanted_series, "generate_subtitles", lambda *args, **kwargs: [])
    monkeypatch.setattr(wanted_series.path_mappings, "path_replace", lambda value: value)

    wanted_series.wanted_download_subtitles(20, arr_instance_id=2)

    rows = schema_session.execute(
        select(TableEpisodes.arr_instance_id, TableEpisodes.failedAttempts)
        .where(TableEpisodes.sonarrEpisodeId == 20)
        .order_by(TableEpisodes.arr_instance_id)
    ).all()

    assert rows == [(1, "[]"), (2, "scoped-attempt")]


def test_wanted_movie_failed_attempts_are_scoped_to_requested_instance(schema_session, monkeypatch):
    from app.database import TableMovies
    import subtitles.wanted.movies as wanted_movies

    _seed_colliding_movies(schema_session)

    monkeypatch.setattr(wanted_movies, "database", schema_session)
    monkeypatch.setattr(wanted_movies, "get_providers", lambda: ["provider"])
    monkeypatch.setattr(wanted_movies, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_movies, "get_profiles_list", lambda profile_id: None)
    monkeypatch.setattr(wanted_movies, "is_search_active", lambda desired_language, attempt_string: True)
    monkeypatch.setattr(wanted_movies, "updateFailedAttempts",
                        lambda desired_language, attempt_string: "scoped-attempt")
    monkeypatch.setattr(wanted_movies, "generate_subtitles", lambda *args, **kwargs: [])
    monkeypatch.setattr(wanted_movies.path_mappings, "path_replace_movie", lambda value: value)

    wanted_movies.wanted_download_subtitles_movie(30, arr_instance_id=3)

    rows = schema_session.execute(
        select(TableMovies.arr_instance_id, TableMovies.failedAttempts)
        .where(TableMovies.radarrId == 30)
        .order_by(TableMovies.arr_instance_id)
    ).all()

    assert rows == [(1, "[]"), (3, "scoped-attempt")]


def test_wanted_episode_history_log_receives_owner(schema_session, monkeypatch):
    import subtitles.wanted.series as wanted_series

    _seed_colliding_episodes(schema_session)
    result = _download_result("/series/anime/s01e01.mkv")
    mock_history_log = Mock()

    monkeypatch.setattr(wanted_series, "database", schema_session)
    monkeypatch.setattr(wanted_series, "get_providers", lambda: ["provider"])
    monkeypatch.setattr(wanted_series, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_series, "get_profiles_list", lambda profile_id: None)
    monkeypatch.setattr(wanted_series, "generate_subtitles", lambda *args, **kwargs: [result])
    monkeypatch.setattr(wanted_series, "store_subtitles", lambda *args, **kwargs: None)
    monkeypatch.setattr(wanted_series, "history_log", mock_history_log)
    monkeypatch.setattr(wanted_series, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(wanted_series, "send_notifications", lambda *args, **kwargs: None)
    monkeypatch.setattr(wanted_series.path_mappings, "path_replace", lambda value: value)

    wanted_series.wanted_download_subtitles(20, arr_instance_id=2)

    mock_history_log.assert_called_once_with(1, 10, 20, result, arr_instance_id=2)


def test_wanted_movie_history_log_receives_owner(schema_session, monkeypatch):
    import subtitles.wanted.movies as wanted_movies

    _seed_colliding_movies(schema_session)
    result = _download_result("/movies/uhd.mkv")
    mock_history_log = Mock()

    monkeypatch.setattr(wanted_movies, "database", schema_session)
    monkeypatch.setattr(wanted_movies, "get_providers", lambda: ["provider"])
    monkeypatch.setattr(wanted_movies, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_movies, "get_profiles_list", lambda profile_id: None)
    monkeypatch.setattr(wanted_movies, "generate_subtitles", lambda *args, **kwargs: [result])
    monkeypatch.setattr(wanted_movies, "store_subtitles_movie", lambda *args, **kwargs: None)
    monkeypatch.setattr(wanted_movies, "history_log_movie", mock_history_log)
    monkeypatch.setattr(wanted_movies, "event_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr(wanted_movies, "send_notifications_movie", lambda *args, **kwargs: None)
    monkeypatch.setattr(wanted_movies.path_mappings, "path_replace_movie", lambda value: value)

    wanted_movies.wanted_download_subtitles_movie(30, arr_instance_id=3)

    mock_history_log.assert_called_once_with(1, 30, result, arr_instance_id=3)


def test_wanted_episode_translation_metadata_uses_requested_instance(schema_session, monkeypatch):
    import subtitles.wanted.series as wanted_series

    _seed_colliding_episodes(schema_session)
    mock_translate = Mock()
    mock_jobs_queue = SimpleNamespace(_is_an_existing_job=Mock(return_value=False))
    episode = SimpleNamespace(
        arr_instance_id=2,
        audio_language="Japanese",
        failedAttempts="[]",
        missing_subtitles="['hu']",
        path="/series/anime/s01e01.mkv",
        profileId=2,
        sceneName="Anime.Scene",
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        subtitles="[['en', '/series/anime/s01e01.en.srt', 100]]",
        title="Pilot",
    )

    monkeypatch.setattr(wanted_series, "database", schema_session)
    monkeypatch.setattr(wanted_series, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_series, "get_profiles_list", lambda profile_id: {
        "items": [{"language": "hu", "translate_from": "en", "forced": "False", "hi": "False"}]
    })
    monkeypatch.setattr(wanted_series, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: "/series/anime/s01e01.en.srt")
    monkeypatch.setattr(wanted_series, "settings",
                        SimpleNamespace(
                            general=SimpleNamespace(use_whisper_fallback=False),
                            translator=SimpleNamespace(min_source_score=0),
                        ))
    monkeypatch.setattr(wanted_series, "jobs_queue", mock_jobs_queue)
    monkeypatch.setattr(wanted_series, "generate_subtitles", lambda *args, **kwargs: [])
    monkeypatch.setattr(wanted_series.path_mappings, "path_replace", lambda value: value)
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file", mock_translate)

    wanted_series._wanted_episode(episode, providers_list=[])

    mock_translate.assert_called_once()
    metadata = mock_translate.call_args.kwargs["metadata"]
    assert metadata.imdbId == "tt-anime"
    assert metadata.tvdbId == 200


def test_wanted_movie_translation_metadata_uses_requested_instance(schema_session, monkeypatch):
    import subtitles.wanted.movies as wanted_movies

    _seed_colliding_movies(schema_session)
    mock_translate = Mock()
    mock_jobs_queue = SimpleNamespace(_is_an_existing_job=Mock(return_value=False))
    movie = SimpleNamespace(
        arr_instance_id=3,
        audio_language="English",
        failedAttempts="[]",
        missing_subtitles="['hu']",
        path="/movies/uhd.mkv",
        profileId=2,
        radarrId=30,
        sceneName="UHD.Movie",
        subtitles="[['en', '/movies/uhd.en.srt', 100]]",
        title="UHD Movie",
    )

    monkeypatch.setattr(wanted_movies, "database", schema_session)
    monkeypatch.setattr(wanted_movies, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_movies, "get_profiles_list", lambda profile_id: {
        "items": [{"language": "hu", "translate_from": "en", "forced": "False", "hi": "False"}]
    })
    monkeypatch.setattr(wanted_movies, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: "/movies/uhd.en.srt")
    monkeypatch.setattr(wanted_movies, "settings",
                        SimpleNamespace(
                            general=SimpleNamespace(use_whisper_fallback=False),
                            translator=SimpleNamespace(min_source_score=0),
                        ))
    monkeypatch.setattr(wanted_movies, "jobs_queue", mock_jobs_queue)
    monkeypatch.setattr(wanted_movies, "generate_subtitles", lambda *args, **kwargs: [])
    monkeypatch.setattr(wanted_movies.path_mappings, "path_replace_movie", lambda value: value)
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file", mock_translate)

    wanted_movies._wanted_movie(movie, providers_list=[])

    mock_translate.assert_called_once()
    metadata = mock_translate.call_args.kwargs["metadata"]
    assert metadata.imdbId == "tt-uhd"
    assert metadata.tmdbId == "303"

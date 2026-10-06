# coding=utf-8

"""Every queue path asks the translation gate before it asks a provider.

A profile can ask for Hungarian by translating English. When no translator can
do that, the scans kept the language away from the provider search anyway: they
queued a job that died inside the engine, and the language stayed missing. The
gate answers first, once per scan run with the reason logged when it is closed,
and the missing language then joins the provider search exactly as if the
profile had no translate-from for it at all.
"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock


_PROFILE = {"items": [{"language": "hu", "translate_from": "en", "forced": "False", "hi": "False"}]}


def _wanted_movie_row(arr_instance_id=1, radarr_id=30):
    return SimpleNamespace(
        arr_instance_id=arr_instance_id,
        audio_language="English",
        failedAttempts="[]",
        missing_subtitles="['hu']",
        path="/movies/fixture.mkv",
        profileId=1,
        radarrId=radarr_id,
        sceneName="Fixture.Scene",
        subtitles="[['en', '/movies/fixture.en.srt', 100]]",
        title="Fixture Movie",
    )


def _wanted_episode_row(arr_instance_id=1, sonarr_episode_id=20):
    return SimpleNamespace(
        arr_instance_id=arr_instance_id,
        audio_language="English",
        failedAttempts="[]",
        missing_subtitles="['hu']",
        path="/series/fixture/s01e01.mkv",
        profileId=1,
        sceneName="Fixture.Scene",
        sonarrEpisodeId=sonarr_episode_id,
        sonarrSeriesId=10,
        subtitles="[['en', '/series/fixture/s01e01.en.srt', 100]]",
        title="Fixture Series",
    )


def _capture_searches(searches):
    def _capture_generate(video_path, languages, *args, **kwargs):
        searches.append(languages)
        return []

    return _capture_generate


def _prepare_movie_scan(monkeypatch, schema_session, searches, translations, gate=None):
    import subtitles.wanted.movies as wanted_movies

    monkeypatch.setattr(wanted_movies, "database", schema_session)
    monkeypatch.setattr(wanted_movies, "get_providers", lambda: ["provider"])
    monkeypatch.setattr(wanted_movies, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_movies, "get_profiles_list", lambda profile_id: _PROFILE)
    monkeypatch.setattr(wanted_movies, "is_search_active", lambda desired_language, attempt_string: True)
    monkeypatch.setattr(wanted_movies, "updateFailedAttempts",
                        lambda desired_language, attempt_string: "scoped-attempt")
    monkeypatch.setattr(wanted_movies, "generate_subtitles", _capture_searches(searches))
    monkeypatch.setattr(wanted_movies, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: "/movies/fixture.en.srt")
    monkeypatch.setattr(wanted_movies, "settings",
                        SimpleNamespace(
                            general=SimpleNamespace(use_whisper_fallback=False),
                            translator=SimpleNamespace(min_source_score=0),
                        ))
    monkeypatch.setattr(wanted_movies, "jobs_queue",
                        SimpleNamespace(_is_an_existing_job=Mock(return_value=False)))
    monkeypatch.setattr(wanted_movies.path_mappings, "path_replace_movie", lambda value: value)
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file", translations)
    if gate is not None:
        monkeypatch.setattr(wanted_movies, "evaluate_translation_gate", gate)
    return wanted_movies


def _prepare_series_scan(monkeypatch, schema_session, searches, translations, gate=None):
    import subtitles.wanted.series as wanted_series

    monkeypatch.setattr(wanted_series, "database", schema_session)
    monkeypatch.setattr(wanted_series, "get_providers", lambda: ["provider"])
    monkeypatch.setattr(wanted_series, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_series, "get_profiles_list", lambda profile_id: _PROFILE)
    monkeypatch.setattr(wanted_series, "is_search_active", lambda desired_language, attempt_string: True)
    monkeypatch.setattr(wanted_series, "updateFailedAttempts",
                        lambda desired_language, attempt_string: "scoped-attempt")
    monkeypatch.setattr(wanted_series, "generate_subtitles", _capture_searches(searches))
    monkeypatch.setattr(wanted_series, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: "/series/fixture/s01e01.en.srt")
    monkeypatch.setattr(wanted_series, "settings",
                        SimpleNamespace(
                            general=SimpleNamespace(use_whisper_fallback=False),
                            translator=SimpleNamespace(min_source_score=0),
                        ))
    monkeypatch.setattr(wanted_series, "jobs_queue",
                        SimpleNamespace(_is_an_existing_job=Mock(return_value=False)))
    monkeypatch.setattr(wanted_series.path_mappings, "path_replace", lambda value: value)
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file", translations)
    if gate is not None:
        monkeypatch.setattr(wanted_series, "evaluate_translation_gate", gate)
    return wanted_series


def _seed_language_profile(session):
    from sqlalchemy import insert, select

    from app.database import TableLanguagesProfiles

    if session.execute(
            select(TableLanguagesProfiles.profileId)
            .where(TableLanguagesProfiles.profileId == 1)).first() is None:
        session.execute(insert(TableLanguagesProfiles).values(
            profileId=1, name="Fixture", items="[]"))


def _seed_missing_movie(session, radarr_id, arr_instance_id):
    from sqlalchemy import insert

    from app.database import TableMovies

    _seed_language_profile(session)
    session.execute(insert(TableMovies).values(
        id=radarr_id,
        radarrId=radarr_id,
        arr_instance_id=arr_instance_id,
        path=f"/movies/movie-{radarr_id}.mkv",
        title=f"Fixture Movie {radarr_id}",
        tmdbId=str(radarr_id),
        imdbId="tt-fixture",
        audio_language="English",
        sceneName="Fixture.Scene",
        missing_subtitles="['hu']",
        failedAttempts="[]",
        subtitles="[]",
        profileId=1,
    ))


def _seed_missing_episode(session, sonarr_episode_id, arr_instance_id):
    from sqlalchemy import insert

    from app.database import TableEpisodes, TableShows

    _seed_language_profile(session)
    session.execute(insert(TableShows).values(
        id=sonarr_episode_id,
        sonarrSeriesId=sonarr_episode_id,
        arr_instance_id=arr_instance_id,
        path="/series/fixture",
        title="Fixture Series",
        imdbId="tt-fixture",
        tvdbId=100,
        profileId=1,
    ))
    session.execute(insert(TableEpisodes).values(
        id=sonarr_episode_id,
        series_id=sonarr_episode_id,
        sonarrSeriesId=sonarr_episode_id,
        sonarrEpisodeId=sonarr_episode_id,
        arr_instance_id=arr_instance_id,
        path=f"/series/fixture/s01e{sonarr_episode_id:02d}.mkv",
        title="Fixture Series",
        season=1,
        episode=1,
        audio_language="English",
        sceneName="Fixture.Scene",
        missing_subtitles="['hu']",
        failedAttempts="[]",
        subtitles="[]",
    ))


def test_a_closed_gate_leaves_the_movie_language_to_the_provider_search(schema_session, monkeypatch):
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=False)

    assert searches == [[("hu", "False", "False")]]
    translations.assert_not_called()


def test_an_open_gate_queues_the_movie_translation_and_skips_the_search(schema_session, monkeypatch):
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_closed_gate_leaves_the_episode_language_to_the_provider_search(schema_session, monkeypatch):
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=False)

    assert searches == [[("hu", "False", "False")]]
    translations.assert_not_called()


def test_an_open_gate_queues_the_episode_translation_and_skips_the_search(schema_session, monkeypatch):
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def _prepare_movie_run(monkeypatch, schema_session, searches, translations, gate):
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations, gate)
    monkeypatch.setattr(wanted_movies, "get_exclusion_clause", lambda media_type: [])
    monkeypatch.setattr(wanted_movies, "activity", Mock())
    monkeypatch.setattr(wanted_movies, "jobs_queue",
                        SimpleNamespace(update_job_progress=lambda **kwargs: None,
                                        update_job_name=lambda **kwargs: None,
                                        _is_an_existing_job=Mock(return_value=False)))
    return wanted_movies


def _prepare_series_run(monkeypatch, schema_session, searches, translations, gate):
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations, gate)
    monkeypatch.setattr(wanted_series, "get_exclusion_clause", lambda media_type: [])
    monkeypatch.setattr(wanted_series, "activity", Mock())
    monkeypatch.setattr(wanted_series, "jobs_queue",
                        SimpleNamespace(update_job_progress=lambda **kwargs: None,
                                        update_job_name=lambda **kwargs: None,
                                        _is_an_existing_job=Mock(return_value=False)))
    return wanted_series


def test_the_movie_scan_run_reads_the_gate_once(schema_session, monkeypatch):
    _seed_missing_movie(schema_session, 30, 1)
    _seed_missing_movie(schema_session, 31, 2)
    searches, translations = [], Mock()
    gate = Mock(return_value=False)
    wanted_movies = _prepare_movie_run(monkeypatch, schema_session, searches, translations, gate)

    wanted_movies.wanted_search_missing_subtitles_movies(job_id=1)

    gate.assert_called_once()
    assert len(searches) == 2
    assert all(entry == [("hu", "False", "False")] for entry in searches)
    translations.assert_not_called()


def test_the_series_scan_run_reads_the_gate_once(schema_session, monkeypatch):
    _seed_missing_episode(schema_session, 20, 1)
    _seed_missing_episode(schema_session, 21, 2)
    searches, translations = [], Mock()
    gate = Mock(return_value=False)
    wanted_series = _prepare_series_run(monkeypatch, schema_session, searches, translations, gate)

    wanted_series.wanted_search_missing_subtitles_series(job_id=1)

    gate.assert_called_once()
    assert len(searches) == 2
    assert all(entry == [("hu", "False", "False")] for entry in searches)
    translations.assert_not_called()


def test_a_single_item_search_reads_the_gate_once_itself(schema_session, monkeypatch):
    _seed_missing_movie(schema_session, 30, 1)
    searches, translations = [], Mock()
    gate = Mock(return_value=False)
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations, gate)

    wanted_movies.wanted_download_subtitles_movie(30, arr_instance_id=1)

    gate.assert_called_once()
    assert searches == [[("hu", "False", "False")]]
    translations.assert_not_called()


def test_a_single_item_search_reuses_the_gate_answer_it_is_handed(schema_session, monkeypatch):
    _seed_missing_movie(schema_session, 30, 1)
    searches, translations = [], Mock()
    gate = Mock(return_value=False)
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations, gate)

    wanted_movies.wanted_download_subtitles_movie(30, arr_instance_id=1, translation_gate=False)

    gate.assert_not_called()
    assert searches == [[("hu", "False", "False")]]
    translations.assert_not_called()


def test_the_sports_scan_run_reads_the_gate_once(monkeypatch):
    from sportarr import workflows

    class _NeverCancelled:
        # The real signal asks the arr_instances table whether its owner is
        # still enabled, and this test owns no database at all.
        def __init__(self, owner, job_id=None, parent=None):
            self.owner, self.job_id = owner, job_id

        def is_set(self):
            return False

    gate = Mock(return_value=False)
    seen = []

    def _fake_search_event(event_id, arr_instance_id, **kwargs):
        seen.append(kwargs.get("translation_gate"))
        return {"status": "published", "message": "Fixture", "downloads": 0}

    monkeypatch.setattr(workflows, "evaluate_translation_gate", gate)
    monkeypatch.setattr(workflows, "search_event", _fake_search_event)
    monkeypatch.setattr(workflows, "SportsJobSignal", _NeverCancelled)
    monkeypatch.setattr(workflows, "jobs_queue",
                        SimpleNamespace(update_job_progress=lambda *args, **kwargs: None))
    rows = [
        {"id": 61, "arr_instance_id": 1, "language": None},
        {"id": 62, "arr_instance_id": 1, "language": None},
    ]

    workflows._run_events(rows, 1)

    gate.assert_called_once()
    assert seen == [False, False]


def test_evaluate_translation_gate_logs_the_reason_once_when_closed(monkeypatch, caplog):
    from subtitles.tools.translate import availability
    from subtitles.wanted import utils as wanted_utils

    monkeypatch.setattr(
        availability, "translation_available",
        lambda: availability.TranslationAvailability(False, "No translator is configured"))

    with caplog.at_level(logging.INFO):
        assert wanted_utils.evaluate_translation_gate() is False

    messages = [
        record for record in caplog.records
        if "No translator is configured" in record.getMessage()
    ]
    assert len(messages) == 1
    assert messages[0].levelno == logging.INFO


def test_evaluate_translation_gate_is_quiet_when_open(monkeypatch, caplog):
    from subtitles.tools.translate import availability
    from subtitles.wanted import utils as wanted_utils

    monkeypatch.setattr(
        availability, "translation_available",
        lambda: availability.TranslationAvailability(True, ""))

    with caplog.at_level(logging.INFO):
        assert wanted_utils.evaluate_translation_gate() is True

    assert not caplog.records


def _prepare_auto_translation(monkeypatch, available):
    import subtitles.processing as processing
    from subtitles.tools.translate.availability import TranslationAvailability

    translate = Mock()
    database = Mock()
    monkeypatch.setattr(
        processing, "translation_available",
        lambda: TranslationAvailability(available, "" if available else "No translator is configured"))
    monkeypatch.setattr("subtitles.tools.translate.main.translate_subtitles_file", translate)
    monkeypatch.setattr("app.database.database", database)
    monkeypatch.setattr("app.database.get_profiles_list", lambda profile_id: _PROFILE)
    monkeypatch.setattr(
        "subtitles.download.check_missing_languages",
        lambda **kwargs: [SimpleNamespace(alpha3="hun", hi=False, forced=False)])
    # The settings-languages table is empty in a test process, so the real
    # alpha3 to alpha2 lookup would answer None and satisfy every language.
    monkeypatch.setattr(processing, "alpha2_from_alpha3", lambda value: value[:2])
    return processing, translate, database


def test_download_time_auto_translation_returns_early_when_the_gate_is_closed(monkeypatch, caplog):
    processing, translate, database = _prepare_auto_translation(monkeypatch, available=False)

    with caplog.at_level(logging.DEBUG):
        processing._trigger_auto_translation(
            downloaded_lang="en", subtitle_path="/subs/source.en.srt",
            video_path="/video/movie.mkv", media_type="movie", radarr_id=30,
        )

    translate.assert_not_called()
    database.execute.assert_not_called()
    assert any(
        "No translator is configured" in record.getMessage() for record in caplog.records)


def test_download_time_auto_translation_translates_when_the_gate_is_open(monkeypatch):
    processing, translate, database = _prepare_auto_translation(monkeypatch, available=True)

    processing._trigger_auto_translation(
        downloaded_lang="en", subtitle_path="/subs/source.en.srt",
        video_path="/video/movie.mkv", media_type="movie", radarr_id=30,
    )

    translate.assert_called_once()
    database.execute.assert_called_once()

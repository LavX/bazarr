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
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.usefixtures('languages_loaded')

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


def test_a_local_score_sidecar_below_the_threshold_still_translates_a_movie(schema_session, monkeypatch):
    """A sidecar with the configured local score is translated even when that
    score is below the translation threshold."""
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_movies, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=90),
    ))
    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_local_score_sidecar_below_the_threshold_still_translates_an_episode(schema_session, monkeypatch):
    """A sidecar with the configured local score is translated even when that
    score is below the translation threshold."""
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_series, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=90),
    ))
    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_at_the_local_score_below_the_threshold_still_translates_a_movie(schema_session,
                                                                                     monkeypatch):
    """A hand upload stored at the local score is a translation source even
    when that score is below the threshold, the same as a sidecar."""
    from datetime import datetime

    from sqlalchemy import insert
    from subliminal_patch.score import MAX_SCORES

    from app.database import TableHistoryMovie

    schema_session.execute(insert(TableHistoryMovie).values(
        action=4, description="Subtitle uploaded manually.", language="en", provider=None,
        score=round(MAX_SCORES["movie"] * 40 / 100), score_out_of=MAX_SCORES["movie"],
        radarrId=30, arr_instance_id=1, timestamp=datetime.now(), video_path="/movies/fixture.mkv",
        subtitles_path="/movies/fixture.en.srt"))
    schema_session.commit()
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_movies, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=90),
    ))
    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_at_the_local_score_below_the_threshold_still_translates_an_episode(schema_session,
                                                                                         monkeypatch):
    """A hand upload stored at the local score is a translation source even
    when that score is below the threshold, the same as a sidecar."""
    from datetime import datetime

    from sqlalchemy import insert
    from subliminal_patch.score import MAX_SCORES

    from app.database import TableHistory

    schema_session.execute(insert(TableHistory).values(
        action=4, description="Subtitle uploaded manually.", language="en", provider=None,
        score=round(MAX_SCORES["episode"] * 40 / 100), score_out_of=MAX_SCORES["episode"],
        sonarrSeriesId=10, sonarrEpisodeId=20, arr_instance_id=1, timestamp=datetime.now(),
        video_path="/series/fixture/s01e01.mkv", subtitles_path="/series/fixture/s01e01.en.srt"))
    schema_session.commit()
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_series, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=90),
    ))
    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def _insert_hand_upload(session, model, score, score_out_of, video_path, subtitles_path):
    from datetime import datetime

    from sqlalchemy import insert

    if model == "movie":
        from app.database import TableHistoryMovie as table
        keys = dict(radarrId=30, video_path=video_path)
    else:
        from app.database import TableHistory as table
        keys = dict(sonarrSeriesId=10, sonarrEpisodeId=20, video_path=video_path)
    session.execute(insert(table).values(
        action=4, description="Subtitle uploaded manually.", language="en", provider=None,
        score=score, score_out_of=score_out_of, arr_instance_id=1, timestamp=datetime.now(),
        subtitles_path=subtitles_path, **keys))
    session.commit()


def test_a_hand_upload_follows_the_local_score_when_the_local_score_changes_for_a_movie(
        schema_session, monkeypatch):
    """The upload was stored at local 60 (108 of 180). The local score is now
    40, so the list scores the upload at 40% and the gate reads it the same way."""
    from subliminal_patch.score import MAX_SCORES

    _insert_hand_upload(schema_session, "movie", round(MAX_SCORES["movie"] * 60 / 100),
                        MAX_SCORES["movie"], "/movies/fixture.mkv", "/movies/fixture.en.srt")
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_movies, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=70),
    ))
    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_at_local_zero_is_read_at_full_scale_for_a_movie(schema_session, monkeypatch):
    """With the local score at 0 a hand upload is scored at the full scale, so
    it clears a threshold of 70 whatever the stored score."""
    from subliminal_patch.score import MAX_SCORES

    _insert_hand_upload(schema_session, "movie", MAX_SCORES["movie"], MAX_SCORES["movie"],
                        "/movies/fixture.mkv", "/movies/fixture.en.srt")
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_movies, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=0),
        translator=SimpleNamespace(min_source_score=70),
    ))
    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_follows_the_local_score_when_the_local_score_changes_for_an_episode(
        schema_session, monkeypatch):
    """The upload was stored at local 60 (216 of 360). The local score is now
    40, so the list scores the upload at 40% and the gate reads it the same way."""
    from subliminal_patch.score import MAX_SCORES

    _insert_hand_upload(schema_session, "episode", round(MAX_SCORES["episode"] * 60 / 100),
                        MAX_SCORES["episode"], "/series/fixture/s01e01.mkv",
                        "/series/fixture/s01e01.en.srt")
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_series, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=70),
    ))
    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def _source_subtitle(tmp_path, minutes_old):
    source = tmp_path / "fixture.en.srt"
    source.write_text("fixture", encoding="utf-8")
    stamp = time.time() - minutes_old * 60
    os.utime(source, (stamp, stamp))
    return str(source)


def _point_source_at(monkeypatch, module, source):
    monkeypatch.setattr(module, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: source)


def _minimum_source_wait(monkeypatch, minutes):
    monkeypatch.setattr("subtitles.wanted.utils.settings",
                        SimpleNamespace(translator=SimpleNamespace(min_source_wait_minutes=minutes)))


def test_a_source_written_just_now_translates_when_no_minimum_wait_is_set(schema_session, monkeypatch, tmp_path):
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_movies, _source_subtitle(tmp_path, minutes_old=0))
    _minimum_source_wait(monkeypatch, 0)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_movie_source_younger_than_the_minimum_wait_is_left_to_the_provider_search(
        schema_session, monkeypatch, tmp_path):
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_movies, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    assert searches == [[("hu", "False", "False")]]
    translations.assert_not_called()


def test_an_episode_source_younger_than_the_minimum_wait_is_left_to_the_provider_search(
        schema_session, monkeypatch, tmp_path):
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_series, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    assert searches == [[("hu", "False", "False")]]
    translations.assert_not_called()


def test_a_movie_source_older_than_the_minimum_wait_is_translated(schema_session, monkeypatch, tmp_path):
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_movies, _source_subtitle(tmp_path, minutes_old=120))
    _minimum_source_wait(monkeypatch, 60)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_movie_at_the_local_score_is_translated_without_waiting(schema_session, monkeypatch, tmp_path):
    """A hand upload is a translation source as soon as it exists, so the
    minimum wait does not hold it back."""
    from subliminal_patch.score import MAX_SCORES

    source = _source_subtitle(tmp_path, minutes_old=0)
    _insert_hand_upload(schema_session, "movie", round(MAX_SCORES["movie"] * 40 / 100),
                        MAX_SCORES["movie"], "/movies/fixture.mkv", source)
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_movies, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=90),
    ))
    _point_source_at(monkeypatch, wanted_movies, source)
    _minimum_source_wait(monkeypatch, 60)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_episode_at_the_local_score_is_translated_without_waiting(schema_session, monkeypatch, tmp_path):
    """A hand upload is a translation source as soon as it exists, so the
    minimum wait does not hold it back."""
    from subliminal_patch.score import MAX_SCORES

    source = _source_subtitle(tmp_path, minutes_old=0)
    _insert_hand_upload(schema_session, "episode", round(MAX_SCORES["episode"] * 40 / 100),
                        MAX_SCORES["episode"], "/series/fixture/s01e01.mkv", source)
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_series, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=40),
        translator=SimpleNamespace(min_source_score=90),
    ))
    _point_source_at(monkeypatch, wanted_series, source)
    _minimum_source_wait(monkeypatch, 60)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_movie_at_local_zero_is_translated_without_waiting(schema_session, monkeypatch, tmp_path):
    """A hand upload at local 0 is scored at the full scale, so it clears the
    threshold, and the minimum wait never holds a hand upload back."""
    from subliminal_patch.score import MAX_SCORES

    source = _source_subtitle(tmp_path, minutes_old=0)
    _insert_hand_upload(schema_session, "movie", MAX_SCORES["movie"], MAX_SCORES["movie"],
                        "/movies/fixture.mkv", source)
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_movies, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=0),
        translator=SimpleNamespace(min_source_score=90),
    ))
    _point_source_at(monkeypatch, wanted_movies, source)
    _minimum_source_wait(monkeypatch, 60)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_hand_upload_episode_at_local_zero_is_translated_without_waiting(schema_session, monkeypatch, tmp_path):
    """A hand upload at local 0 is scored at the full scale, so it clears the
    threshold, and the minimum wait never holds a hand upload back."""
    from subliminal_patch.score import MAX_SCORES

    source = _source_subtitle(tmp_path, minutes_old=0)
    _insert_hand_upload(schema_session, "episode", MAX_SCORES["episode"], MAX_SCORES["episode"],
                        "/series/fixture/s01e01.mkv", source)
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    monkeypatch.setattr(wanted_series, "settings", SimpleNamespace(
        general=SimpleNamespace(use_whisper_fallback=False, local_subtitle_score=0),
        translator=SimpleNamespace(min_source_score=90),
    ))
    _point_source_at(monkeypatch, wanted_series, source)
    _minimum_source_wait(monkeypatch, 60)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


def test_a_movie_sidecar_is_held_when_the_newest_hand_upload_is_another_file(
        schema_session, monkeypatch, tmp_path):
    """The newest English row is a hand upload of another file. That upload
    does not vouch for this sidecar, which is younger than the minimum wait."""
    from subliminal_patch.score import MAX_SCORES

    _insert_hand_upload(schema_session, "movie", MAX_SCORES["movie"], MAX_SCORES["movie"],
                        "/movies/fixture.mkv", "/movies/fixture.en.srt")
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_movies, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


def test_an_episode_sidecar_is_held_when_the_newest_hand_upload_is_another_file(
        schema_session, monkeypatch, tmp_path):
    """The newest English row is a hand upload of another file. That upload
    does not vouch for this sidecar, which is younger than the minimum wait."""
    from subliminal_patch.score import MAX_SCORES

    _insert_hand_upload(schema_session, "episode", MAX_SCORES["episode"], MAX_SCORES["episode"],
                        "/series/fixture/s01e01.mkv", "/series/fixture/s01e01.en.srt")
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_series, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


def test_a_held_movie_source_with_its_translation_on_disk_skips_the_provider_search(
        schema_session, monkeypatch, tmp_path):
    """A source still younger than the minimum wait does not send the language
    to the provider search when its translated file already exists. This test
    pins the order of the already-translated guard against the hold on source-
    age, and not the wait itself."""
    from datetime import datetime

    from sqlalchemy import insert

    from app.database import TableHistoryMovie

    translated = tmp_path / "fixture.hu.srt"
    translated.write_text("translated", encoding="utf-8")
    schema_session.execute(insert(TableHistoryMovie).values(
        action=6, description="Subtitle translated.", language="hu", provider=None,
        radarrId=30, arr_instance_id=1, timestamp=datetime.now(), video_path="/movies/fixture.mkv",
        subtitles_path=str(translated)))
    schema_session.commit()
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_movies, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)

    translations.assert_not_called()
    assert searches == [[]]


def test_a_held_episode_source_with_its_translation_on_disk_skips_the_provider_search(
        schema_session, monkeypatch, tmp_path):
    """A source still younger than the minimum wait does not send the language
    to the provider search when its translated file already exists. This test
    pins the order of the already-translated guard against the hold on source-
    age, and not the wait itself."""
    from datetime import datetime

    from sqlalchemy import insert

    from app.database import TableHistory

    translated = tmp_path / "fixture.hu.srt"
    translated.write_text("translated", encoding="utf-8")
    schema_session.execute(insert(TableHistory).values(
        action=6, description="Subtitle translated.", language="hu", provider=None,
        sonarrSeriesId=10, sonarrEpisodeId=20, arr_instance_id=1, timestamp=datetime.now(),
        video_path="/series/fixture/s01e01.mkv", subtitles_path=str(translated)))
    schema_session.commit()
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_series, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_not_called()
    assert searches == [[]]


def test_a_held_movie_source_with_a_subtitle_at_the_destination_skips_the_provider_search(
        schema_session, monkeypatch, tmp_path):
    """A source still younger than the minimum wait does not send the language
    to the provider search when a subtitle already sits at its translation
    destination. This test pins the order of the destination check against the
    hold on source-age, and not the wait itself."""
    (tmp_path / "fixture.hu.srt").write_text("provider", encoding="utf-8")
    movie = _wanted_movie_row()
    movie.path = str(tmp_path / "fixture.mkv")
    searches, translations = [], Mock()
    wanted_movies = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_movies, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)
    monkeypatch.setattr("subtitles.indexer.utils.settings",
                        SimpleNamespace(general=SimpleNamespace(subfolder="current")))

    wanted_movies._wanted_movie(movie, providers_list=[], translation_gate=True)

    translations.assert_not_called()
    assert searches == [[]]


def test_a_held_episode_source_with_a_subtitle_at_the_destination_skips_the_provider_search(
        schema_session, monkeypatch, tmp_path):
    """A source still younger than the minimum wait does not send the language
    to the provider search when a subtitle already sits at its translation
    destination. This test pins the order of the destination check against the
    hold on source-age, and not the wait itself."""
    (tmp_path / "s01e01.hu.srt").write_text("provider", encoding="utf-8")
    episode = _wanted_episode_row()
    episode.path = str(tmp_path / "s01e01.mkv")
    searches, translations = [], Mock()
    wanted_series = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, wanted_series, _source_subtitle(tmp_path, minutes_old=5))
    _minimum_source_wait(monkeypatch, 60)
    monkeypatch.setattr("subtitles.indexer.utils.settings",
                        SimpleNamespace(general=SimpleNamespace(subfolder="current")))

    wanted_series._wanted_episode(episode, providers_list=[], translation_gate=True)

    translations.assert_not_called()
    assert searches == [[]]


def test_a_source_that_is_missing_is_not_held_back_by_the_minimum_wait(monkeypatch, tmp_path):
    from subtitles.wanted.utils import source_wait_elapsed

    _minimum_source_wait(monkeypatch, 60)

    assert source_wait_elapsed(str(tmp_path / "missing.en.srt")) is True


def test_a_source_older_than_the_minimum_wait_has_elapsed_it(monkeypatch, tmp_path):
    from subtitles.wanted.utils import source_wait_elapsed

    _minimum_source_wait(monkeypatch, 60)

    assert source_wait_elapsed(_source_subtitle(tmp_path, minutes_old=120)) is True


def test_a_source_younger_than_the_minimum_wait_has_not_elapsed_it(monkeypatch, tmp_path):
    from subtitles.wanted.utils import source_wait_elapsed

    _minimum_source_wait(monkeypatch, 60)

    assert source_wait_elapsed(_source_subtitle(tmp_path, minutes_old=5)) is False


def test_a_source_dated_far_in_the_future_is_not_held_back_by_the_minimum_wait(monkeypatch, tmp_path):
    """A date further ahead than the wait itself cannot be a file written a
    moment ago, so holding it would hold it for as long as the date stays ahead."""
    from subtitles.wanted.utils import source_wait_elapsed

    source = tmp_path / "fixture.en.srt"
    source.write_text("fixture", encoding="utf-8")
    future = time.time() + 3 * 60 * 60
    os.utime(source, (future, future))
    _minimum_source_wait(monkeypatch, 60)

    assert source_wait_elapsed(str(source)) is True


def test_a_source_dated_a_few_seconds_ahead_is_held_for_the_minimum_wait(monkeypatch, tmp_path):
    """A clock that runs a little ahead of this one dates a file written just
    now in the future. Its age is 0, so it waits like any fresh source."""
    from subtitles.wanted.utils import source_wait_elapsed

    source = tmp_path / "fixture.en.srt"
    source.write_text("fixture", encoding="utf-8")
    future = time.time() + 5
    os.utime(source, (future, future))
    _minimum_source_wait(monkeypatch, 60)

    assert source_wait_elapsed(str(source)) is False


def _insert_history_row(session, model, action, subtitles_path, seconds_after, score=None, score_out_of=None):
    from datetime import datetime, timedelta

    from sqlalchemy import insert

    if model == "movie":
        from app.database import TableHistoryMovie as table
        keys = dict(radarrId=30, video_path="/movies/fixture.mkv")
    else:
        from app.database import TableHistory as table
        keys = dict(sonarrSeriesId=10, sonarrEpisodeId=20, video_path="/series/fixture/s01e01.mkv")
    session.execute(insert(table).values(
        action=action, description="Later event.", language="en", provider=None,
        score=score, score_out_of=score_out_of, arr_instance_id=1,
        timestamp=datetime.now() + timedelta(seconds=seconds_after),
        subtitles_path=subtitles_path, **keys))
    session.commit()


@pytest.mark.parametrize("later_action", [5, 9])
@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_hand_upload_stays_unheld_after_a_sync_or_a_manual_score(
        schema_session, monkeypatch, tmp_path, model, later_action):
    """A sync rewrites the same file and a manual score only rescores it, so
    neither says the file did not come from the upload."""
    from subliminal_patch.score import MAX_SCORES

    source = _source_subtitle(tmp_path, minutes_old=0)
    video = "/movies/fixture.mkv" if model == "movie" else "/series/fixture/s01e01.mkv"
    _insert_hand_upload(schema_session, model, MAX_SCORES[model], MAX_SCORES[model], video, source)
    _insert_history_row(schema_session, model, later_action, source, seconds_after=30)
    searches, translations = [], Mock()
    if model == "movie":
        scan = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    else:
        scan = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, scan, source)
    _minimum_source_wait(monkeypatch, 60)

    if model == "movie":
        scan._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)
    else:
        scan._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    assert searches == [[]]


@pytest.mark.parametrize("wait,captured", [(60, 2), (0, 1)])
@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_queued_scan_translation_captures_its_source_only_while_the_wait_is_on(
        schema_session, monkeypatch, tmp_path, model, wait, captured):
    searches, translations = [], Mock()
    source = _source_subtitle(tmp_path, minutes_old=120)
    if model == "movie":
        scan = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    else:
        scan = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, scan, source)
    _minimum_source_wait(monkeypatch, wait)

    if model == "movie":
        scan._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)
    else:
        scan._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)

    translations.assert_called_once()
    expected = translations.call_args.kwargs["expected_destination_versions"]
    assert len(expected) == captured
    assert expected[0].path.endswith(".hu.srt")
    if captured == 2:
        assert expected[1].path == source


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_scan_translation_cancels_when_its_source_is_replaced_while_it_waits(
        schema_session, monkeypatch, tmp_path, model):
    """The wait is decided for the file seen at queueing. A new file in the
    source's place has not waited, so the captured version refuses it and the
    next scan applies the wait."""
    from subtitles.tools.subsync_engines import SubtitleSourceChanged, validate_subtitle_versions

    searches, translations = [], Mock()
    source = _source_subtitle(tmp_path, minutes_old=120)
    if model == "movie":
        scan = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    else:
        scan = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, scan, source)
    _minimum_source_wait(monkeypatch, 60)
    if model == "movie":
        scan._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)
    else:
        scan._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)
    expected = translations.call_args.kwargs["expected_destination_versions"]
    validate_subtitle_versions(expected)

    with open(source, "w", encoding="utf-8") as handle:
        handle.write("a replacement written just now")

    with pytest.raises(SubtitleSourceChanged):
        validate_subtitle_versions(expected)


def _scan_once(model, monkeypatch, schema_session, source, minutes):
    searches, translations = [], Mock()
    if model == "movie":
        scan = _prepare_movie_scan(monkeypatch, schema_session, searches, translations)
    else:
        scan = _prepare_series_scan(monkeypatch, schema_session, searches, translations)
    _point_source_at(monkeypatch, scan, source)
    _minimum_source_wait(monkeypatch, minutes)
    if model == "movie":
        scan._wanted_movie(_wanted_movie_row(), providers_list=[], translation_gate=True)
    else:
        scan._wanted_episode(_wanted_episode_row(), providers_list=[], translation_gate=True)
    return searches, translations


def _record_download(session, model, source, minutes_ago, action=1):
    from subliminal_patch.score import MAX_SCORES

    _insert_history_row(session, model, action, source, seconds_after=-minutes_ago * 60,
                        score=MAX_SCORES[model], score_out_of=MAX_SCORES[model])


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_the_wait_counts_from_the_download_on_record_not_the_file_time(
        schema_session, monkeypatch, tmp_path, model):
    """The file is old, but the history says it was downloaded five minutes
    ago, so it is still inside the wait."""
    source = _source_subtitle(tmp_path, minutes_old=120)
    _record_download(schema_session, model, source, minutes_ago=5)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_file_replaced_after_its_download_row_is_measured_from_the_file(
        schema_session, monkeypatch, tmp_path, model):
    """The download row is two hours old, but the file was written a moment ago
    by something the history does not know: a concurrent save not recorded yet,
    an arr import or a copy by hand. The old row does not vouch for it."""
    source = _source_subtitle(tmp_path, minutes_old=0)
    _record_download(schema_session, model, source, minutes_ago=120)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_file_replaced_after_a_hand_upload_row_does_not_inherit_the_exemption(
        schema_session, monkeypatch, tmp_path, model):
    source = _source_subtitle(tmp_path, minutes_old=0)
    _record_download(schema_session, model, source, minutes_ago=180, action=4)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_file_replaced_after_its_row_but_old_enough_by_its_own_time_translates(
        schema_session, monkeypatch, tmp_path, model):
    source = _source_subtitle(tmp_path, minutes_old=90)
    _record_download(schema_session, model, source, minutes_ago=180)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_called_once()
    assert searches == [[]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_sync_rewrite_does_not_restart_the_wait(schema_session, monkeypatch, tmp_path, model):
    """A sync rewrites the file in place and logs its own row afterwards. The
    download is two hours old, so the wait stays elapsed though the file and the
    sync row are brand new."""
    source = _source_subtitle(tmp_path, minutes_old=1)
    _record_download(schema_session, model, source, minutes_ago=120)
    _insert_history_row(schema_session, model, 5, source, seconds_after=-30)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_called_once()
    assert searches == [[]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_source_replaced_after_a_sync_does_not_inherit_the_old_age(
        schema_session, monkeypatch, tmp_path, model):
    """Download two hours ago, sync an hour and a half ago, then something other
    than a sync wrote the file a moment ago. The sync row only explains the
    modification time up to its own, so the file time decides."""
    source = _source_subtitle(tmp_path, minutes_old=0)
    _record_download(schema_session, model, source, minutes_ago=120)
    _insert_history_row(schema_session, model, 5, source, seconds_after=-90 * 60)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_file_dated_far_ahead_does_not_count_as_a_later_write(
        schema_session, monkeypatch, tmp_path, model):
    """A modification time hours ahead of now cannot be told from a clock
    disagreement, so the row it sits next to keeps standing."""
    source = _source_subtitle(tmp_path, minutes_old=-4 * 60)
    _record_download(schema_session, model, source, minutes_ago=5)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    # The row says five minutes. Read as the file's own time the date would be
    # hours ahead of the wait and release it.
    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_source_with_no_history_row_goes_by_the_file_time(
        schema_session, monkeypatch, tmp_path, model):
    young = _source_subtitle(tmp_path, minutes_old=5)
    searches, translations = _scan_once(model, monkeypatch, schema_session, young, 60)
    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]

    old = _source_subtitle(tmp_path, minutes_old=120)
    searches, translations = _scan_once(model, monkeypatch, schema_session, old, 60)
    translations.assert_called_once()
    assert searches == [[]]


@pytest.mark.parametrize("model", ["movie", "episode"])
def test_a_removal_on_record_leaves_the_file_time_to_decide(
        schema_session, monkeypatch, tmp_path, model):
    """The newest row for the path removed the file, so it says nothing about
    when the file that is there now arrived."""
    source = _source_subtitle(tmp_path, minutes_old=5)
    _insert_history_row(schema_session, model, 0, source, seconds_after=-3 * 3600)

    searches, translations = _scan_once(model, monkeypatch, schema_session, source, 60)

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


def test_a_written_time_in_the_history_is_measured_like_a_file_time(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    from subtitles.wanted.utils import source_wait_elapsed

    source = _source_subtitle(tmp_path, minutes_old=0)
    _minimum_source_wait(monkeypatch, 60)

    assert source_wait_elapsed(source, datetime.now() - timedelta(minutes=5)) is False
    assert source_wait_elapsed(source, datetime.now() - timedelta(minutes=120)) is True
    # A few seconds ahead is a clock disagreement and waits; hours ahead cannot be a fresh file.
    assert source_wait_elapsed(source, datetime.now() + timedelta(seconds=5)) is False
    assert source_wait_elapsed(source, datetime.now() + timedelta(hours=3)) is True

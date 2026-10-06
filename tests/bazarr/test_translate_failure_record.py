# coding=utf-8

"""Items whose last translation job failed are held back from translating.

A failed translation leaves nothing the next scan can act on: the history row
and the translated file are written only on success, so before this record
every scan queued the same doomed job again, once per missing item per scan.
The hold is in memory, keyed per item and per source language, and clears when
the translation succeeds, when the translator settings or language profiles
change, and after a day.
"""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


_PROFILE = {"items": [{"language": "hu", "translate_from": "en", "forced": "False", "hi": "False"}]}
_PROFILE_FROM_FRENCH = {"items": [
    {"language": "hu", "translate_from": "fr", "forced": "False", "hi": "False"}]}


@pytest.fixture(autouse=True)
def _empty_record():
    from subtitles.tools.translate.failure_record import clear_failed_translations

    clear_failed_translations()
    yield
    clear_failed_translations()


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


def _run_failed_translation_job(monkeypatch, media_type, **media_ids):
    from subtitles.tools.translate import main as translate_main

    def _refuse(*args, **kwargs):
        raise ValueError("Unknown translator type: 'none'")

    monkeypatch.setattr(translate_main, "activity", Mock())
    monkeypatch.setattr(translate_main, "validate_translation_params", _refuse)
    monkeypatch.setattr(translate_main, "jobs_queue", SimpleNamespace(
        get_job_name=lambda job_id: None, update_job_name=lambda **kwargs: None))

    with pytest.raises(ValueError):
        translate_main.translate_subtitles_file(
            video_path=f"/movies/fixture-{media_type}.mkv",
            source_srt_file=f"/movies/fixture-{media_type}.en.srt",
            from_lang="en", to_lang="hu", forced=False, hi=False,
            media_type=media_type, metadata=None, job_id=1, **media_ids,
        )


def _prepare_movie_scan(monkeypatch, schema_session, translations, profile=None):
    import subtitles.wanted.movies as wanted_movies

    searches = []

    def _capture_generate(video_path, languages, *args, **kwargs):
        searches.append(languages)
        return []

    monkeypatch.setattr(wanted_movies, "database", schema_session)
    monkeypatch.setattr(wanted_movies, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_movies, "get_profiles_list", lambda profile_id: profile or _PROFILE)
    monkeypatch.setattr(wanted_movies, "generate_subtitles", _capture_generate)
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
    return wanted_movies, searches


def _prepare_series_scan(monkeypatch, schema_session, translations, profile=None):
    import subtitles.wanted.series as wanted_series

    searches = []

    def _capture_generate(video_path, languages, *args, **kwargs):
        searches.append(languages)
        return []

    monkeypatch.setattr(wanted_series, "database", schema_session)
    monkeypatch.setattr(wanted_series, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_series, "get_profiles_list", lambda profile_id: profile or _PROFILE)
    monkeypatch.setattr(wanted_series, "generate_subtitles", _capture_generate)
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
    return wanted_series, searches


def test_a_recorded_failure_holds_the_item():
    from subtitles.tools.translate import failure_record

    failure_record.record_failed_translation(1, "movies", 30, "en", "hu")

    assert failure_record.translation_recently_failed(1, "movies", 30, "en", "hu") is True


def test_the_hold_expires_after_a_day(monkeypatch):
    from subtitles.tools.translate import failure_record

    clock = {"now": 0.0}
    monkeypatch.setattr(failure_record, "_now", lambda: clock["now"])
    failure_record.record_failed_translation(1, "movies", 30, "en", "hu")

    clock["now"] = failure_record.FAILURE_TTL_SECONDS - 1
    assert failure_record.translation_recently_failed(1, "movies", 30, "en", "hu") is True

    clock["now"] = failure_record.FAILURE_TTL_SECONDS
    assert failure_record.translation_recently_failed(1, "movies", 30, "en", "hu") is False


def test_another_item_or_language_is_not_held_back():
    from subtitles.tools.translate import failure_record

    failure_record.record_failed_translation(1, "movies", 30, "en", "hu")

    assert failure_record.translation_recently_failed(1, "movies", 31, "en", "hu") is False
    assert failure_record.translation_recently_failed(1, "movies", 30, "en", "sv") is False
    assert failure_record.translation_recently_failed(2, "movies", 30, "en", "hu") is False
    assert failure_record.translation_recently_failed(1, "episode", 30, "en", "hu") is False


def test_after_a_failed_movie_job_the_next_scan_searches_providers(schema_session, monkeypatch):
    _run_failed_translation_job(
        monkeypatch, "movies", sonarr_series_id=None, sonarr_episode_id=None,
        radarr_id=30, arr_instance_id=1)
    translations = Mock()
    wanted_movies, searches = _prepare_movie_scan(monkeypatch, schema_session, translations)

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[])

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


def test_after_a_failed_episode_job_the_next_scan_searches_providers(schema_session, monkeypatch):
    _run_failed_translation_job(
        monkeypatch, "episode", sonarr_series_id=10, sonarr_episode_id=20,
        radarr_id=None, arr_instance_id=1)
    translations = Mock()
    wanted_series, searches = _prepare_series_scan(monkeypatch, schema_session, translations)

    wanted_series._wanted_episode(_wanted_episode_row(), providers_list=[])

    translations.assert_not_called()
    assert searches == [[("hu", "False", "False")]]


def test_an_edited_source_language_is_not_held_back_by_the_old_failure(schema_session, monkeypatch):
    from subtitles.tools.translate import failure_record

    failure_record.record_failed_translation(1, "movies", 30, "en", "hu")
    translations = Mock()
    wanted_movies, searches = _prepare_movie_scan(
        monkeypatch, schema_session, translations, profile=_PROFILE_FROM_FRENCH)
    monkeypatch.setattr(wanted_movies, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: "/movies/fixture.fr.srt")

    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[])

    translations.assert_called_once()
    assert translations.call_args.kwargs["from_lang"] == "fr"


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


def test_saving_translator_settings_clears_the_record(schema_session, monkeypatch):
    from subtitles.tools.translate import failure_record

    failure_record.record_failed_translation(1, "movies", 30, "en", "hu")

    from app import config

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None),
        update=lambda _model: _FakeUpdate(),
        System=object,
    ))
    previous = config.settings.translator.translator_type
    try:
        config.save_settings([("settings-translator-translator_type", ["lingarr"])])
    finally:
        config.settings["translator.translator_type"] = previous

    assert failure_record.translation_recently_failed(1, "movies", 30, "en", "hu") is False

    translations = Mock()
    wanted_movies, searches = _prepare_movie_scan(monkeypatch, schema_session, translations)
    wanted_movies._wanted_movie(_wanted_movie_row(), providers_list=[])

    translations.assert_called_once()


def test_saving_language_profiles_clears_the_record(schema_session, monkeypatch):
    from subtitles.tools.translate import failure_record

    failure_record.record_failed_translation(1, "movies", 30, "en", "hu")

    from api.system import settings as api_settings

    monkeypatch.setattr(api_settings, "database", schema_session)
    profile = {
        "profileId": 1,
        "name": "Fixture",
        "cutoff": None,
        "items": [],
        "mustContain": "[]",
        "mustNotContain": "[]",
        "originalFormat": None,
    }

    api_settings._write_settings_rows([], [profile], [])

    assert failure_record.translation_recently_failed(1, "movies", 30, "en", "hu") is False

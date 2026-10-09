# coding=utf-8

"""A queued-or-existing sports translation is not a download, and the wanted
scans' precheck has to match the job it asks the queue about.

The sports wanted scan counted every translate_from_existing answer of True as
a download, including the answer that only found its job already pending, so
the language was kept away from the provider search while nothing new was
coming. The movie and series scans asked the queue about an identical job with
fewer arguments than the queue binds for it, so the answer was always no and
every scan tick re-offered the same translation to a queue that refused it.
"""

# ruff: noqa: F401, F811

import logging
from collections import deque
from types import SimpleNamespace

from test_sportarr_kind_migration import migration_engine
from test_sportarr_indexer import indexed_library
from test_sportarr_manual import manual_library
from test_sportarr_profile_hooks import profile_library

_PROFILE = {"items": [{"language": "hu", "translate_from": "en", "forced": "False", "hi": "False"}]}


def _translation_jobs(queue):
    return [job for job in queue.jobs_pending_queue + queue.jobs_running_queue
            if job.module == "subtitles.tools.translate.main"]


def test_a_fresh_sports_translation_is_queued_once(profile_library):
    from sportarr.profile_hooks import translate_from_existing

    assert translate_from_existing(profile_library.base.context, "hu") is True
    assert len(_translation_jobs(profile_library.queue)) == 1


def test_a_dedupe_is_not_counted_as_a_download(profile_library):
    from sportarr.profile_hooks import translate_from_existing

    context = profile_library.base.context

    assert translate_from_existing(context, "hu") is True
    assert translate_from_existing(context, "hu") is False
    assert len(_translation_jobs(profile_library.queue)) == 1


def test_a_closed_gate_answers_false_without_queueing(profile_library):
    from sportarr.profile_hooks import translate_from_existing

    assert translate_from_existing(
        profile_library.base.context, "hu", translation_gate=False) is False
    assert _translation_jobs(profile_library.queue) == []


def test_a_recent_failure_answers_false_without_queueing(profile_library, monkeypatch):
    from sportarr.profile_hooks import translate_from_existing
    from subtitles.tools.translate import failure_record

    failure_record.record_failed_translation(1, "sports", 61, "en", "hu")

    assert translate_from_existing(profile_library.base.context, "hu") is False
    assert _translation_jobs(profile_library.queue) == []


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


def _prepare_precheck(monkeypatch, schema_session, wanted_module, source_path, path_replace):
    """Patch one wanted scan the way the gate tests do, but leave its queue real.

    The real queue is what makes the proof: the first scan run queues a real
    translation job, and only a precheck asking about the job the queue really
    stored can answer yes on the second run.
    """
    from app import jobs_queue as queue_module
    from subtitles.tools.translate import main as translate_main

    answers = []
    real_check = queue_module.jobs_queue._is_an_existing_job

    def _recording_check(module, func, args, kwargs):
        answer = real_check(module, func, args, kwargs)
        answers.append(answer)
        return answer

    monkeypatch.setattr(wanted_module, "database", schema_session)
    monkeypatch.setattr(wanted_module, "get_audio_profile_languages", lambda value: [])
    monkeypatch.setattr(wanted_module, "get_profiles_list", lambda profile_id: _PROFILE)
    monkeypatch.setattr(wanted_module, "generate_subtitles", lambda *args, **kwargs: [])
    monkeypatch.setattr(wanted_module, "_find_existing_subtitle_path",
                        lambda subtitles, source_lang, path_replace_fn=None: source_path)
    monkeypatch.setattr(wanted_module, "settings",
                        SimpleNamespace(
                            general=SimpleNamespace(use_whisper_fallback=False),
                            translator=SimpleNamespace(min_source_score=0),
                        ))
    monkeypatch.setattr(wanted_module, "jobs_queue", queue_module.jobs_queue)
    monkeypatch.setattr(wanted_module.path_mappings, path_replace, lambda value: value)
    monkeypatch.setattr(translate_main, "get_title", lambda *args, **kwargs: "")
    for queue_name in ("jobs_pending_queue", "jobs_running_queue"):
        monkeypatch.setattr(queue_module.jobs_queue, queue_name, deque())
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(queue_module.jobs_queue, "_is_an_existing_job", _recording_check)
    return answers


def test_the_movie_precheck_matches_the_job_the_scan_queued(schema_session, monkeypatch, caplog):
    import subtitles.wanted.movies as wanted_movies

    answers = _prepare_precheck(
        monkeypatch, schema_session, wanted_movies,
        "/movies/fixture.en.srt", "path_replace_movie")
    movie = _wanted_movie_row()

    with caplog.at_level(logging.INFO):
        wanted_movies._wanted_movie(movie, providers_list=[])
        wanted_movies._wanted_movie(movie, providers_list=[])

    assert answers == [False, True]
    assert len(_translation_jobs_from_module()) == 1
    queuing = [record for record in caplog.records
               if "auto-translate (wanted-scan) queuing" in record.getMessage()]
    assert len(queuing) == 1


def test_the_episode_precheck_matches_the_job_the_scan_queued(schema_session, monkeypatch, caplog):
    import subtitles.wanted.series as wanted_series

    answers = _prepare_precheck(
        monkeypatch, schema_session, wanted_series,
        "/series/fixture/s01e01.en.srt", "path_replace")
    episode = _wanted_episode_row()

    with caplog.at_level(logging.INFO):
        wanted_series._wanted_episode(episode, providers_list=[])
        wanted_series._wanted_episode(episode, providers_list=[])

    assert answers == [False, True]
    assert len(_translation_jobs_from_module()) == 1
    queuing = [record for record in caplog.records
               if "auto-translate (wanted-scan) queuing" in record.getMessage()]
    assert len(queuing) == 1


def _translation_jobs_from_module():
    from app.jobs_queue import jobs_queue

    return _translation_jobs(jobs_queue)

"""The translation entry points with the runtime on and off: enqueue, the job, publication and the editor.

The dispatcher itself is tested in test_translator_dispatch.py. Runs on SQLite, and on PostgreSQL when
BAZARR_PG_TEST_URL is set.
"""
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import translator_dispatch_helpers as helpers
from translator_dispatch_helpers import PLUGIN, SOURCE
from subtitles.tools.translate.runtime import enqueue
from subtitles.tools.translate.runtime.dispatch import Declined, GateClosed
from subtitles.tools.translate.runtime.jobs import (JobsQueuePort, TranslationJobError, TranslationJobs,
                                                    target_key)

pytestmark = pytest.mark.usefixtures("languages_loaded")


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path, monkeypatch):
    from app import database

    for holder in helpers.build_env(request.param, tmp_path):
        monkeypatch.setattr(database, "database", holder.session)
        yield holder


@pytest.fixture
def queue_status(monkeypatch):
    """What the jobs queue says about a queue job id: set ``statuses[id]``."""
    statuses = {}
    monkeypatch.setattr(JobsQueuePort, "status", lambda self, queue_job_id: statuses.get(queue_job_id))
    return statuses


def jobs_of(env):
    return TranslationJobs(env.session, clock=env.clock)


# ---------------------------------------------------------------------------------------------------- enqueue


def test_a_new_request_makes_a_job_and_the_queue_job_is_recorded_on_it(env):
    decision = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert decision.action == enqueue.QUEUE and decision.queue_job_id is None
    enqueue.attach_for(41, SOURCE, "fr", session=env.session, clock=env.clock)
    row = jobs_of(env).active_for(SOURCE, "fr")
    assert (row.id, row.state, row.queue_job_id) == (decision.translator_job_id, "queued", 41)


def test_a_variant_is_another_job(env):
    enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    forced = enqueue.decide(SOURCE, "fr", forced=True, session=env.session, clock=env.clock)
    assert jobs_of(env).get(forced.translator_job_id).target == target_key("fr", forced=True)


def test_an_equal_request_follows_the_queue_job_that_exists(env, queue_status):
    enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    enqueue.attach_for(41, SOURCE, "fr", session=env.session, clock=env.clock)
    queue_status[41] = "pending"
    again = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert (again.action, again.queue_job_id) == (enqueue.FOLLOW, 41)


def test_a_job_whose_queue_job_is_gone_is_queued_again(env, queue_status):
    first = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    enqueue.attach_for(41, SOURCE, "fr", session=env.session, clock=env.clock)
    again = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)  # the queue knows no job 41
    assert again.action == enqueue.QUEUE and again.translator_job_id == first.translator_job_id


def test_a_job_awaiting_its_retry_waits_for_its_time_and_is_then_queued_again(env):
    jobs = jobs_of(env)
    job_id = jobs.request(SOURCE, "fr").job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    jobs.await_retry(job_id, "rate_limited", "later", retry_at=env.clock() + timedelta(minutes=5))
    early = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert (early.action, early.translator_job_id) == (enqueue.NOTHING, job_id)
    env.clock.advance(minutes=6)
    due = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert due.action == enqueue.QUEUE and due.translator_job_id == job_id
    enqueue.attach_for(52, SOURCE, "fr", session=env.session, clock=env.clock)
    row = jobs.get(job_id)
    assert (row.state, row.queue_job_id, row.retry_at) == ("queued", 52, None)


def test_a_manual_pick_replaces_a_queued_job_and_ends_it_superseded(env, queue_status):
    from subtitles.tools.translate.runtime.history import HistoryStore

    first = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    HistoryStore(env.session, clock=env.clock).open_job(str(first.translator_job_id))
    pick = enqueue.decide(SOURCE, "fr", pinned_connection_id=3, session=env.session, clock=env.clock)
    assert pick.action == enqueue.QUEUE and pick.translator_job_id != first.translator_job_id
    old = jobs_of(env).get(first.translator_job_id)
    assert old.state == "superseded"
    assert HistoryStore(env.session, clock=env.clock).get_by_job(str(first.translator_job_id))["status"] == "superseded"
    row = jobs_of(env).active_for(SOURCE, "fr")
    assert (row.manual, row.pinned_connection_id) == (1, 3)


def test_a_request_that_follows_a_manual_pick_joins_the_pinned_job(env, queue_status):
    pick = enqueue.decide(SOURCE, "fr", pinned_connection_id=3, session=env.session, clock=env.clock)
    later = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)  # the request that queues it
    assert later.translator_job_id == pick.translator_job_id
    assert jobs_of(env).get(pick.translator_job_id).pinned_connection_id == 3


def test_a_manual_pick_is_refused_with_the_reason_while_the_job_runs(env):
    jobs = jobs_of(env)
    job_id = jobs.request(SOURCE, "fr").job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    with pytest.raises(TranslationJobError) as caught:
        enqueue.decide(SOURCE, "fr", pinned_connection_id=3, session=env.session, clock=env.clock)
    assert "already being translated" in str(caught.value)


def test_an_embedded_track_is_keyed_by_the_track_and_not_by_a_file():
    from subtitles.tools.translate import main

    track = {"language": "en", "hi": True, "forced": False}
    assert main._job_source_key("/v/film.mkv", "/v/ignored.srt", track) == "/v/film.mkv#embedded:en:hi:"
    assert main._job_source_key("/v/film.mkv", "/v/film.en.srt", None) == "/v/film.en.srt"


# ------------------------------------------------------------------------------- the enqueue in main.py


def _request(main, **overrides):
    values = dict(video_path="/v/film.mkv", source_srt_file=SOURCE, from_lang="en", to_lang="fr", forced=False,
                  hi=False, media_type="movie", sonarr_series_id=None, sonarr_episode_id=None, radarr_id=7,
                  metadata=None)
    values.update(overrides)
    return main.translate_subtitles_file(**values)


@pytest.fixture
def queued(monkeypatch):
    """The jobs queue stands in: it numbers the jobs it is asked to add and remembers the labels."""
    from subtitles.tools.translate import main

    added = []

    def add(label, **kwargs):
        added.append(label)
        return 100 + len(added)

    monkeypatch.setattr(main, "jobs_queue", SimpleNamespace(add_job_from_function=add))
    monkeypatch.setattr(main, "get_title", lambda *args, **kwargs: "Film")
    return added


def test_with_the_switch_off_a_request_is_queued_exactly_as_before_and_the_runtime_is_never_touched(
        queued, monkeypatch):
    from app.config import settings
    from subtitles.tools.translate import main

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    for name in [name for name in sys.modules if name.startswith("subtitles.tools.translate.runtime")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "subtitles.tools.translate.runtime", None)  # an import now raises
    assert _request(main) == 101
    assert queued == ["Translating Film (EN to FR)"]


def test_with_the_switch_on_a_request_for_a_source_that_has_a_job_follows_it(env, queued, queue_status, monkeypatch):
    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "_runtime_active", lambda: True)
    assert _request(main) == 101  # the first request queues
    queue_status[101] = "pending"
    assert _request(main) == 101, "the second one follows the job that exists"
    assert queued == ["Translating Film (EN to FR)"], "nothing was queued twice"
    row = jobs_of(env).active_for(SOURCE, "fr")
    assert row.queue_job_id == 101


def test_with_the_switch_on_a_request_waiting_for_its_retry_time_queues_nothing(env, queued, monkeypatch):
    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "_runtime_active", lambda: True)
    jobs = jobs_of(env)
    job_id = jobs.request(SOURCE, "fr").job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    jobs.await_retry(job_id, "rate_limited", "later", retry_at=datetime.now() + timedelta(hours=1))
    assert _request(main) is None and queued == []


def test_a_fault_in_the_runtimes_bookkeeping_does_not_stop_a_request_being_queued(queued, monkeypatch):
    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "_runtime_active", lambda: True)
    monkeypatch.setattr(enqueue, "decide", Mock(side_effect=RuntimeError("the database is gone")))
    assert _request(main) == 101


# ---------------------------------------------------------------------------------- the job in main.py


@pytest.fixture
def job(monkeypatch):
    """Runs ``translate_subtitles_file`` as its own job with everything around it stubbed."""
    from subzero.language import Language

    from subtitles.tools.translate import main

    renamed = []
    monkeypatch.setattr(main, "validate_translation_params", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "convert_language_codes", lambda to_lang, _forced, _hi: (Language("fra"), to_lang))
    monkeypatch.setattr(main, "translated_destination_path", lambda video_path, to_lang, forced, hi: "/v/film.fr.srt")
    monkeypatch.setattr(main, "alpha3_from_alpha2", lambda code: "fra")
    monkeypatch.setattr(main, "activity", Mock())
    monkeypatch.setattr(main, "jobs_queue", SimpleNamespace(
        get_job_name=lambda job_id: "Translating Film (EN to FR)",
        update_job_name=lambda job_id, new_job_name: renamed.append(new_job_name)))
    monkeypatch.setattr("api.subtitles.subtitles.postprocess_subtitles", lambda *args, **kwargs: None)
    monkeypatch.setattr("subtitles.tools.combine.main.try_combine_for_video", lambda **kwargs: None)
    searched = []
    monkeypatch.setattr(main, "_search_providers_instead", lambda *args: searched.append(args))

    def run(**overrides):
        return _request(main, job_id=9, **overrides)

    run.renamed, run.searched, run.main = renamed, searched, main
    return run


def _translator(monkeypatch, **attributes):
    from subtitles.tools.translate.runtime import service

    made = []

    class Fake:
        partial_error = None
        skipped = False

        def __init__(self, **kwargs):
            made.append(kwargs)

        def translate(self, job_id=None):
            if "raises" in attributes:
                raise attributes["raises"]
            return attributes.get("returns", "/v/film.fr.srt")

    for key, value in attributes.items():
        if key not in ("raises", "returns"):
            setattr(Fake, key, value)
    monkeypatch.setattr(service, "RuntimeTranslatorService", Fake)
    return made


def test_with_the_switch_off_the_job_builds_the_factorys_translator(job, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    built = []
    translator = Mock(partial_error=None, skipped=False)
    translator.translate.return_value = True

    def create(translator_type, **kwargs):
        built.append((translator_type, sorted(kwargs)))
        return translator

    monkeypatch.setattr(job.main.TranslatorFactory, "create_translator", staticmethod(create))
    job()
    assert built[0][0] == "google_translate"
    assert "source_key" not in built[0][1] and "pinned_connection_id" not in built[0][1]
    assert "to_lang" in built[0][1] and "expected_destination_versions" in built[0][1]


def test_with_the_switch_on_the_job_builds_the_runtime_service_and_never_the_factory(job, monkeypatch):
    monkeypatch.setattr(job.main, "_runtime_active", lambda: True)
    monkeypatch.setattr(job.main.TranslatorFactory, "create_translator",
                        staticmethod(lambda *a, **k: pytest.fail("the factory was called")))
    made = _translator(monkeypatch)
    job()
    assert made[0]["source_key"] == SOURCE and "pinned_connection_id" not in made[0]
    assert made[0]["to_lang"] == "fra" and made[0]["orig_to_lang"] == "fr"


def test_the_job_of_an_embedded_track_is_keyed_before_the_extraction_replaces_its_source(job, monkeypatch):
    monkeypatch.setattr(job.main, "_runtime_active", lambda: True)
    monkeypatch.setattr(job.main, "_extract_embedded_source", lambda *args, **kwargs: "/cache/extracted.srt")
    made = _translator(monkeypatch)
    job(embedded_source={"language": "en", "hi": False, "forced": False})
    assert made[0]["source_key"] == "/v/film.mkv#embedded:en::"
    assert made[0]["source_srt_file"] == "/cache/extracted.srt"


def test_a_job_that_found_itself_replaced_is_skipped_and_publishes_nothing(job, monkeypatch):
    monkeypatch.setattr(job.main, "_runtime_active", lambda: True)
    _translator(monkeypatch, skipped=True, returns=None)
    assert job() is None
    assert job.renamed == ["Skipped Film (EN to FR)"]


def test_a_gate_that_closed_while_the_job_waited_falls_back_to_the_provider_search(job, monkeypatch):
    from subtitles.tools.translate.failure_record import clear_failed_translations, translation_recently_failed

    clear_failed_translations()
    monkeypatch.setattr(job.main, "_runtime_active", lambda: True)
    _translator(monkeypatch, raises=GateClosed("No translator is configured", "no_engine"))
    assert job(radarr_id=7) is None
    assert job.searched == [("movie", 7, None, None)], "the item is searched at providers instead"
    assert job.renamed == ["Skipped Film (EN to FR)"]
    assert not translation_recently_failed(None, "movie", 7, "en", "fr", False, False), "nothing is held back"


def test_a_decline_leaves_the_search_result_in_place_and_holds_the_item_back(job, monkeypatch):
    from subtitles.tools.translate.failure_record import clear_failed_translations, translation_recently_failed

    clear_failed_translations()
    monkeypatch.setattr(job.main, "_runtime_active", lambda: True)
    _translator(monkeypatch, raises=Declined(["The language is not supported"]))
    assert job(radarr_id=7) is None and job.searched == []
    assert translation_recently_failed(None, "movie", 7, "en", "fr", False, False)


def test_a_failure_that_will_be_retried_does_not_hold_the_item_back_but_a_final_one_does(job, monkeypatch):
    from subtitles.tools.translate.failure_record import clear_failed_translations, translation_recently_failed
    from subtitles.tools.translate.runtime.service import TranslationRuntimeError

    clear_failed_translations()
    monkeypatch.setattr(job.main, "_runtime_active", lambda: True)
    _translator(monkeypatch, raises=TranslationRuntimeError("slow down", "rate_limited", retry_later=True))
    with pytest.raises(TranslationRuntimeError):
        job(radarr_id=7)
    assert not translation_recently_failed(None, "movie", 7, "en", "fr", False, False)
    _translator(monkeypatch, raises=TranslationRuntimeError("bad", "invalid_config", retry_later=False))
    with pytest.raises(TranslationRuntimeError):
        job(radarr_id=7)
    assert translation_recently_failed(None, "movie", 7, "en", "fr", False, False)


# -------------------------------------------------------------------------------- the file the service writes


SRT = "1\n00:00:01,000 --> 00:00:02,000\nOne\n\n2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n3\n00:00:05,000 --> 00:00:06,000\nThree\n"


@pytest.fixture
def media(tmp_path, monkeypatch):
    from subtitles.tools.translate.runtime import service

    video = tmp_path / "film.mkv"
    video.write_bytes(b"x")
    source = tmp_path / "film.en.srt"
    source.write_text(SRT, encoding="utf-8")
    logged = []
    monkeypatch.setattr(service, "history_log_movie", lambda **kwargs: logged.append(kwargs))
    monkeypatch.setattr(service, "history_log", lambda **kwargs: logged.append(kwargs))
    monkeypatch.setattr(service, "publication_callback", lambda *args: None)
    monkeypatch.setattr(service, "_media_context", lambda *args: {"type": "movie", "title": "Film"})
    monkeypatch.setattr(service, "get_title", lambda *args, **kwargs: "Film")
    monkeypatch.setattr(service, "create_process_result",
                        lambda message, *args, **kwargs: SimpleNamespace(message=message))
    monkeypatch.setattr(service, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kwargs: True, raise_if_cancelled=lambda job_id: None))
    return SimpleNamespace(video=video, source=source, dest=tmp_path / "film.fr.srt", logged=logged)


def build_service(env, media, to="fr", **overrides):
    from subtitles.tools.translate.runtime.service import RuntimeTranslatorService

    values = dict(source_srt_file=str(media.source), dest_srt_file=str(media.dest), lang_obj=None, to_lang="fra",
                  from_lang="en", media_type="movie", video_path=str(media.video), orig_to_lang=to, forced=False,
                  hi=False, sonarr_series_id=None, sonarr_episode_id=None, radarr_id=7, arr_instance_id=None,
                  dispatcher=env.dispatcher())
    values.update(overrides)
    return RuntimeTranslatorService(**values)


def test_the_service_publishes_what_the_engine_translated_with_its_credit_line_and_history(env, media, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "translator_info", True)
    env.ai_engine()
    service = build_service(env, media)
    assert service.translate(job_id=None) == str(media.dest)
    written = media.dest.read_text(encoding="utf-8")
    assert "[fr] One" in written and "[fr] Three" in written
    assert "# Subtitles translated with AI Subtitle Translator #" in written
    assert media.logged[0]["action"] == 6 and media.logged[0]["radarr_id"] == 7
    assert "using AI Subtitle Translator" in media.logged[0]["result"].message
    assert service.partial_error is None and service.skipped is False
    assert jobs_of(env).active_for(str(media.source), "fr") is None, "the job is over"


def test_a_partial_answer_is_published_as_partial_and_says_so(env, media, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "translator_info", True)
    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"partial": [1]}]}
    service = build_service(env, media)
    service.translate(job_id=None)
    written = media.dest.read_text(encoding="utf-8")
    assert "[fr] One" in written and "Two" in written and "[fr] Two" not in written
    assert "partially translated with AI Subtitle Translator" in written
    assert service.partial_error == "No translated text was returned for 1 of 3 cues."
    assert "Some lines may remain in the source language" in media.logged[0]["result"].message


def test_a_job_that_is_not_ours_to_run_returns_nothing_and_writes_nothing(env, media):
    env.ai_engine()
    jobs = jobs_of(env)
    job_id = jobs.request(str(media.source), "fr").job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    service = build_service(env, media)
    assert service.translate(job_id=None) is None
    assert service.skipped is True and not media.dest.exists()


def test_a_source_with_no_lines_is_a_failure_and_never_asks_an_engine(env, media, monkeypatch):
    from subtitles.tools.translate.runtime.service import TranslationRuntimeError

    import pysubs2

    from subtitles.tools.translate.runtime import service

    monkeypatch.setattr(service.pysubs2, "load", lambda *args, **kwargs: pysubs2.SSAFile())
    env.ai_engine()
    with pytest.raises(TranslationRuntimeError):
        build_service(env, media).translate(job_id=None)
    assert env.requests() == []


def test_a_failure_the_dispatcher_recorded_reaches_the_queue_as_a_job_failure_with_its_class(env, media):
    from app.jobs_queue import JobFailed
    from subtitles.tools.translate.runtime.service import TranslationRuntimeError

    env.ai_engine()
    env.scripts[PLUGIN] = {"*": [{"fail": {"code": "rate_limited", "message": "slow down"}}]}
    with pytest.raises(TranslationRuntimeError) as caught:
        build_service(env, media).translate(job_id=None)
    assert isinstance(caught.value, JobFailed)
    assert (caught.value.reason, caught.value.retry_later) == ("rate_limited", True)


def test_lingarr_publishes_through_its_own_service_and_the_runtime_writes_nothing(env, media, monkeypatch):
    from app.config import settings
    from subtitles.tools.translate.runtime import legacy_engines

    monkeypatch.setattr(settings.translator, "lingarr_url", "http://lingarr.example.test:9876")
    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, settings={legacy_engines.SAVED_FLAG: True})
    env.pointers.engine = instance.id
    built = []
    old = Mock(partial_error=None)
    old.translate.return_value = str(media.dest)

    def create(translator_type, **kwargs):
        built.append((translator_type, kwargs))
        return old

    from subtitles.tools.translate.services.translator_factory import TranslatorFactory
    monkeypatch.setattr(TranslatorFactory, "create_translator", staticmethod(create))
    service = build_service(env, media)
    assert service.translate(job_id=None) == str(media.dest)
    assert built[0][0] == "lingarr" and built[0][1]["to_lang"] == "fra" and built[0][1]["source_srt_file"]
    assert not media.dest.exists(), "the old service wrote the file, and it is a stub here"
    assert media.logged == [] and env.requests() == []


def test_a_lingarr_server_that_cannot_be_reached_declines_and_the_fallback_engine_publishes(env, media, monkeypatch):
    from deep_translator.exceptions import RequestError

    from app.config import settings
    from subtitles.tools.translate.runtime import legacy_engines
    from subtitles.tools.translate.services.translator_factory import TranslatorFactory

    monkeypatch.setattr(settings.translator, "translator_info", False)
    monkeypatch.setattr(settings.translator, "lingarr_url", "http://lingarr.example.test:9876")
    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, settings={legacy_engines.SAVED_FLAG: True})
    env.pointers.engine = instance.id
    env.pointers.fallback = env.ai_engine(select=False).id
    old = Mock(partial_error=None)
    old.translate.side_effect = RequestError("Connection error")
    monkeypatch.setattr(TranslatorFactory, "create_translator", staticmethod(lambda *a, **k: old))
    assert build_service(env, media).translate(job_id=None) == str(media.dest)
    assert "[fr] One" in media.dest.read_text(encoding="utf-8")


def test_a_failure_of_the_old_service_that_is_not_the_server_being_down_is_not_a_decline(env, media, monkeypatch):
    from app.config import settings
    from subtitles.tools.translate.runtime import legacy_engines
    from subtitles.tools.translate.services.translator_factory import TranslatorFactory

    monkeypatch.setattr(settings.translator, "lingarr_url", "http://lingarr.example.test:9876")
    instance = env.store.create_instance(legacy_engines.LINGARR_PLUGIN_ID, settings={legacy_engines.SAVED_FLAG: True})
    env.pointers.engine = instance.id
    old = Mock(partial_error=None)
    old.translate.side_effect = ValueError("Authentication failed")
    monkeypatch.setattr(TranslatorFactory, "create_translator", staticmethod(lambda *a, **k: old))
    with pytest.raises(ValueError):
        build_service(env, media).translate(job_id=None)


# ------------------------------------------------------------------------------------------------- the editor


@pytest.fixture
def editor_queue(monkeypatch):
    from app import jobs_queue as jobs_queue_module
    from subtitles.tools.translate import editor
    from subtitles.tools.translate.runtime import service
    from utilities import job_dedupe

    fresh = jobs_queue_module.JobsQueue()
    monkeypatch.setattr(jobs_queue_module, "event_stream", lambda **kwargs: None)
    for module in (editor, job_dedupe, service):
        monkeypatch.setattr(module, "jobs_queue", fresh)
    monkeypatch.setattr(editor, "_results", type(editor._results)())
    monkeypatch.setattr(editor, "_followers", type(editor._followers)())
    return fresh


def _run_editor_job(queue, job_id):
    job = next(job for job in queue.jobs_pending_queue if job.job_id == job_id)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)
    queue._run_job(job)
    return job


def test_the_editor_translates_through_the_selected_engine_when_the_runtime_is_on(env, editor_queue, monkeypatch):
    from subtitles.tools.translate import editor
    from subtitles.tools.translate.runtime import service

    env.ai_engine()
    monkeypatch.setattr(service, "Dispatcher", lambda: env.dispatcher())
    monkeypatch.setattr(editor, "_runtime_editor_service", lambda media_type: editor._RuntimeEditorService(media_type))
    lines = [{"position": 4, "line": "Hello"}, {"position": 9, "line": "World"}]
    job_id = editor.enqueue_editor_translation(lines, "en", "fr", title="Example")
    _run_editor_job(editor_queue, job_id)
    state = editor.editor_translation_state(job_id)
    assert state["status"] == "completed"
    assert state["lines"] == [{"position": 4, "line": "[fr] Hello"}, {"position": 9, "line": "[fr] World"}]
    assert env.requests()[0]["request"]["media"] == {"type": "", "title": "Example"} or \
        env.requests()[0]["request"]["media"]["title"] == "Example"


def test_the_editor_says_why_when_the_gate_is_closed(env, editor_queue, monkeypatch):
    from subtitles.tools.translate import editor
    from subtitles.tools.translate.runtime import service

    monkeypatch.setattr(service, "Dispatcher", lambda: env.dispatcher())
    monkeypatch.setattr(editor, "_runtime_editor_service", lambda media_type: editor._RuntimeEditorService(media_type))
    job_id = editor.enqueue_editor_translation([{"position": 0, "line": "Hello"}], "en", "fr")
    _run_editor_job(editor_queue, job_id)
    state = editor.editor_translation_state(job_id)
    assert state["status"] == "failed" and "No translator is configured" in state["error"]
    assert "Translation page" in state["error"]


def test_with_the_switch_off_the_editor_uses_the_sidecar_service(monkeypatch):
    from app.config import settings
    from subtitles.tools.translate import editor

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    assert editor._runtime_editor_service("movie") is None

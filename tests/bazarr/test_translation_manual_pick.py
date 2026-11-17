"""A manual pick of one back end: the gate that answers for it, and every surface that carries it.

The translate action, the mass translate and the subtitle editor can name one back end of the v2 runtime. The job
then runs on that back end alone, whatever engine the Translation page has selected. Runs on SQLite, and on
PostgreSQL when BAZARR_PG_TEST_URL is set.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask

import translator_dispatch_helpers as helpers
from translator_dispatch_helpers import PLUGIN, SECOND_PLUGIN, SOURCE
from subtitles.tools.translate import availability
from subtitles.tools.translate import pick
from subtitles.tools.translate.runtime import enqueue, gate
from subtitles.tools.translate.runtime.jobs import JobsQueuePort, TranslationJobError, TranslationJobs

pytestmark = pytest.mark.usefixtures("languages_loaded")


@pytest.fixture(params=["sqlite", "postgres"])
def env(request, tmp_path, monkeypatch):
    from app import database

    for holder in helpers.build_env(request.param, tmp_path):
        monkeypatch.setattr(database, "database", holder.session)
        yield holder


@pytest.fixture(autouse=True)
def _no_noted_picks():
    pick._noted.clear()
    pick.forget_expected()
    yield
    pick._noted.clear()
    pick.forget_expected()


@pytest.fixture
def runtime_on(monkeypatch):
    monkeypatch.setattr(availability, "runtime_active", lambda: True)


# ------------------------------------------------------------------------------------------------- the gate


def test_a_usable_back_end_is_available_whatever_engine_is_selected(env):
    selected = env.ai_engine()
    other = env.engine(SECOND_PLUGIN, select=False)
    picked = env.connection(other, model="pick")
    assert env.pointers.engine == selected.id
    answer = gate.pinned_gate(picked.id, env.store, env.holds, env.workers)
    assert (answer.available, answer.reason, answer.instance_id) == (True, "", other.id)


def test_a_pick_is_available_with_no_engine_selected_at_all(env):
    instance = env.engine(select=False)
    picked = env.connection(instance)
    assert env.pointers.engine is None
    assert gate.evaluate(env.store, env.holds, env.workers).available is False
    assert gate.pinned_gate(picked.id, env.store, env.holds, env.workers).available is True


def test_a_back_end_that_does_not_exist_is_refused_with_its_reason(env):
    answer = gate.pinned_gate(404, env.store, env.holds, env.workers)
    assert (answer.available, answer.reason, answer.cause) == (False, gate.PICK_MISSING, gate.ENGINE_MISSING)


def test_a_back_end_that_is_off_is_refused_with_its_reason(env):
    instance = env.ai_engine()
    off = env.connection(instance, model="off", enabled=False)
    answer = gate.pinned_gate(off.id, env.store, env.holds, env.workers)
    assert (answer.available, answer.reason) == (False, gate.PICK_UNUSABLE)


def test_a_back_end_whose_plugin_stopped_is_refused_with_the_engines_reason(env):
    instance = env.ai_engine()
    picked = env.connection(instance, model="pick")
    env.active.discard(PLUGIN)
    answer = gate.pinned_gate(picked.id, env.store, env.holds, env.workers)
    assert answer.available is False and "plugin is not installed" in answer.reason


def test_a_back_end_of_a_paused_engine_is_refused_with_the_cause(env):
    instance = env.ai_engine()
    picked = env.connection(instance, model="pick")
    env.holds.pause_for_drain(instance.id)
    answer = gate.pinned_gate(picked.id, env.store, env.holds, env.workers)
    assert (answer.available, answer.cause) == (False, gate.SIDECAR_DRAIN)


def test_a_pick_is_a_back_end_or_an_engine_and_the_job_record_tells_them_apart():
    assert pick.pin_value() is None
    assert pick.pin_value(connection_id=6) == 6 and pick.split_pin(6) == ("connection", 6)
    assert pick.pin_value(engine_id=6) == -6 and pick.split_pin(-6) == ("engine", 6)
    with pytest.raises(ValueError, match="not both"):
        pick.pin_value(connection_id=1, engine_id=2)


@pytest.mark.parametrize("bad", [0, -3, True, "4", 2.5])
def test_an_id_that_is_not_a_whole_number_of_one_or_more_is_no_pick(bad):
    for name in ("connection_id", "engine_id"):
        with pytest.raises(ValueError, match="whole number of 1 or more"):
            pick.pin_value(**{name: bad})


def test_an_engine_with_no_back_ends_is_available_whatever_engine_is_selected(env):
    env.ai_engine()
    google = env.engine(SECOND_PLUGIN, select=False)
    answer = gate.pinned_gate(pick.pin_value(engine_id=google.id), env.store, env.holds, env.workers)
    assert (answer.available, answer.instance_id) == (True, google.id)


def test_an_engine_that_does_not_exist_is_refused_with_its_reason(env):
    answer = gate.pinned_gate(pick.pin_value(engine_id=404), env.store, env.holds, env.workers)
    assert (answer.available, answer.reason, answer.cause) == (
        False, gate.PICK_ENGINE_MISSING, gate.ENGINE_MISSING)


def test_an_engine_that_has_back_ends_is_refused_because_it_says_nothing_about_which(env):
    instance = env.ai_engine()
    answer = gate.pinned_gate(pick.pin_value(engine_id=instance.id), env.store, env.holds, env.workers)
    assert (answer.available, answer.reason) == (False, gate.PICK_ENGINE_HAS_BACKENDS)


def test_an_engine_that_cannot_take_a_job_is_refused_with_the_engines_reason(env):
    google = env.engine(SECOND_PLUGIN, select=False)
    env.active.discard(SECOND_PLUGIN)
    answer = gate.pinned_gate(pick.pin_value(engine_id=google.id), env.store, env.holds, env.workers)
    assert answer.available is False and "plugin is not installed" in answer.reason
    env.active.add(SECOND_PLUGIN)
    env.holds.pause_for_drain(google.id)
    assert gate.pinned_gate(pick.pin_value(engine_id=google.id), env.store, env.holds, env.workers).cause == (
        gate.SIDECAR_DRAIN)


def test_a_gate_that_cannot_read_the_configuration_is_closed_and_says_why(env):
    class Broken:
        def get_connection(self, *_args, **_kwargs):
            raise RuntimeError("the database is gone")

    answer = gate.pinned_gate(1, Broken(), env.holds)
    assert (answer.available, answer.cause) == (False, gate.UNREADABLE) and "database is gone" in answer.reason


# ------------------------------------------------------------------------------- the answer the surfaces read


def test_with_the_runtime_on_a_pick_is_answered_for_that_back_end(runtime_on, monkeypatch):
    seen = []

    def pinned(connection_id, *_args, **_kwargs):
        seen.append(connection_id)
        return gate.GateResult(False, "closed for the pick", gate.NOT_CONFIGURED)

    monkeypatch.setattr(gate, "pinned_gate", pinned)
    monkeypatch.setattr(gate, "evaluate", lambda *a, **k: pytest.fail("the selected engine was asked"))
    assert tuple(availability.translation_available(pin=7)) == (False, "closed for the pick")
    assert tuple(availability.translation_available("openrouter", pin=7)) == (False, "closed for the pick")
    assert seen == [7, 7]


def test_with_the_runtime_off_a_pick_is_refused_and_never_dropped_silently(monkeypatch):
    import sys

    from app.config import settings

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    for name in [name for name in sys.modules if name.startswith("subtitles.tools.translate.runtime")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "subtitles.tools.translate.runtime", None)  # an import now raises
    assert tuple(availability.translation_available(pin=3)) == (False, availability.PICK_NEEDS_RUNTIME)
    # Without a pick the old gate answers as it always did.
    assert tuple(availability.translation_available()) == (True, "")


# ----------------------------------------------------------------------------------- the queue in main.py


def _request(main, **overrides):
    values = dict(video_path="/v/film.mkv", source_srt_file=SOURCE, from_lang="en", to_lang="fr", forced=False,
                  hi=False, media_type="movie", sonarr_series_id=None, sonarr_episode_id=None, radarr_id=7,
                  metadata=None)
    values.update(overrides)
    return main.translate_subtitles_file(**values)


def _pin(main, pin, **overrides):
    values = dict(video_path="/v/film.mkv", source_srt_file=SOURCE, to_lang="fr", forced=False, hi=False,
                  embedded_source=None, pin=pin)
    values.update(overrides)
    return main.pin_translation(**values)


@pytest.fixture
def queued(monkeypatch):
    from subtitles.tools.translate import main

    added = []

    def add(label, **kwargs):
        added.append(label)
        return 100 + len(added)

    monkeypatch.setattr(main, "jobs_queue", SimpleNamespace(add_job_from_function=add))
    monkeypatch.setattr(main, "get_title", lambda *args, **kwargs: "Film")
    monkeypatch.setattr(main, "_runtime_active", lambda: True)
    return added


@pytest.fixture
def queue_status(monkeypatch):
    statuses = {}
    monkeypatch.setattr(JobsQueuePort, "status", lambda self, queue_job_id: statuses.get(queue_job_id))
    return statuses


def test_a_pick_is_recorded_on_the_job_and_the_request_that_follows_queues_it(env, queued):
    from subtitles.tools.translate import main

    picked = env.connection(env.engine())
    _pin(main, picked.id)
    jobs = TranslationJobs(env.session, clock=env.clock)
    row = jobs.active_for(SOURCE, "fr")
    assert (row.manual, row.pinned_connection_id, row.queue_job_id) == (1, picked.id, None)
    assert _request(main) == 101
    row = jobs.active_for(SOURCE, "fr")
    assert (row.pinned_connection_id, row.queue_job_id) == (picked.id, 101), "the same job, now on the queue"


def test_a_pick_replaces_a_job_that_has_not_started_and_the_request_queues_its_own(
        env, queued, queue_status, monkeypatch):
    from subtitles.tools.translate import main

    instance = env.engine()
    env.connection(instance)
    second = env.connection(instance, model="second")
    assert _request(main) == 101
    queue_status[101] = "pending"
    removed = []
    monkeypatch.setattr(JobsQueuePort, "remove_pending", lambda self, queue_job_id: removed.append(queue_job_id) or True)
    _pin(main, second.id)
    assert _request(main) == 102, "a new queue job is added, not the old one followed"
    assert removed == [101]
    row = TranslationJobs(env.session, clock=env.clock).active_for(SOURCE, "fr")
    assert (row.pinned_connection_id, row.queue_job_id) == (second.id, 102)


def test_a_pick_is_refused_with_the_reason_while_the_job_runs(env, queued):
    from subtitles.tools.translate import main

    picked = env.connection(env.engine())
    jobs = TranslationJobs(env.session, clock=env.clock)
    job_id = jobs.request(SOURCE, "fr").job_id
    jobs.start(job_id, source_digest="d", engine_instance_id=1, settings_revision=1)
    with pytest.raises(TranslationJobError) as caught:
        _pin(main, picked.id)
    assert "already being translated" in str(caught.value) and queued == []


def test_a_pick_of_an_engine_is_recorded_on_the_job_as_an_engine(env, queued):
    from subtitles.tools.translate import main

    google = env.engine(SECOND_PLUGIN, select=False)
    _pin(main, pick.pin_value(engine_id=google.id))
    row = TranslationJobs(env.session, clock=env.clock).active_for(SOURCE, "fr")
    assert (row.manual, pick.split_pin(row.pinned_connection_id)) == (1, ("engine", google.id))


def _picked_job(env, queue_status, connection_id, queue_job_id=101):
    decision = enqueue.decide(SOURCE, "fr", pinned_connection_id=connection_id, session=env.session, clock=env.clock)
    enqueue.attach_for(queue_job_id, SOURCE, "fr", session=env.session, clock=env.clock)
    return decision


def _active(env):
    return TranslationJobs(env.session, clock=env.clock).active_for(SOURCE, "fr")


def test_a_request_with_no_pick_clears_the_pick_of_a_job_whose_queue_job_was_removed(env, queue_status):
    picked = env.connection(env.engine())
    _picked_job(env, queue_status, picked.id)  # the queue knows no job 101 any more
    again = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert again.action == enqueue.QUEUE
    row = _active(env)
    assert (row.pinned_connection_id, row.manual) == (None, 0)


def test_a_request_with_no_pick_follows_a_picked_job_that_is_still_on_the_queue_and_keeps_it_picked(
        env, queue_status):
    picked = env.connection(env.engine())
    _picked_job(env, queue_status, picked.id)
    queue_status[101] = "pending"
    again = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert (again.action, again.queue_job_id) == (enqueue.FOLLOW, 101)
    assert _active(env).pinned_connection_id == picked.id


def test_a_request_with_no_pick_clears_the_pick_of_a_job_that_waited_for_its_retry(env, queue_status):
    picked = env.connection(env.engine())
    _picked_job(env, queue_status, picked.id)
    jobs = TranslationJobs(env.session, clock=env.clock)
    row = _active(env)
    jobs.start(row.id, source_digest="d", engine_instance_id=1, settings_revision=1)
    jobs.await_retry(row.id, "rate_limited", "later", retry_at=env.clock() + timedelta(minutes=5))
    early = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert early.action == enqueue.QUEUED_FOR_RETRY
    assert _active(env).pinned_connection_id == picked.id, "it still waits as the picked job it is"
    env.clock.advance(minutes=6)
    due = enqueue.decide(SOURCE, "fr", session=env.session, clock=env.clock)
    assert due.action == enqueue.QUEUE
    assert _active(env).pinned_connection_id is None


def test_a_request_with_the_same_pick_keeps_it_and_a_different_one_replaces_it(env, queue_status):
    instance = env.engine()
    first, second = env.connection(instance), env.connection(instance, model="second")
    _picked_job(env, queue_status, first.id)
    same = enqueue.decide(SOURCE, "fr", pinned_connection_id=first.id, session=env.session, clock=env.clock)
    assert _active(env).pinned_connection_id == first.id
    other = enqueue.decide(SOURCE, "fr", pinned_connection_id=second.id, session=env.session, clock=env.clock)
    assert other.translator_job_id != same.translator_job_id
    assert _active(env).pinned_connection_id == second.id


def test_a_pick_for_an_embedded_track_is_keyed_by_the_track(env, queued):
    from subtitles.tools.translate import main

    picked = env.connection(env.engine())
    track = {"language": "en", "hi": False, "forced": False}
    _pin(main, picked.id, source_srt_file="", embedded_source=track)
    key = main._job_source_key("/v/film.mkv", "", track)
    assert TranslationJobs(env.session, clock=env.clock).active_for(key, "fr").pinned_connection_id == picked.id


def test_a_request_with_no_pick_records_nothing(env, queued, monkeypatch):
    from subtitles.tools.translate import main

    monkeypatch.setattr(enqueue, "decide", lambda *a, **k: pytest.fail("the runtime was asked"))
    _pin(main, None)


def test_with_the_runtime_off_a_pick_records_nothing_and_never_touches_the_runtime(monkeypatch):
    import sys

    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "_runtime_active", lambda: False)
    for name in [name for name in sys.modules if name.startswith("subtitles.tools.translate.runtime")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "subtitles.tools.translate.runtime", None)  # an import now raises
    _pin(main, 5)


def test_the_request_function_takes_no_parameter_for_a_pick():
    """The queue records every parameter of the call, and the callers that look for an identical pending job
    compare all of them: a new parameter would make every one of those comparisons miss."""
    import inspect

    from subtitles.tools.translate import main

    assert "connection" not in " ".join(inspect.signature(main.translate_subtitles_file).parameters)


# --------------------------------------------------------------------------------------------- the editor


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


def test_the_editor_runs_on_the_picked_back_end_alone(env, editor_queue, monkeypatch):
    from subtitles.tools.translate import editor
    from subtitles.tools.translate.runtime import service

    instance = env.engine()
    env.connection(instance, model="first")
    picked = env.connection(instance, model="picked")
    fallback = env.engine(SECOND_PLUGIN, select=False)
    env.pointers.fallback = fallback.id
    monkeypatch.setattr(service, "Dispatcher", lambda: env.dispatcher())
    monkeypatch.setattr(editor, "_runtime_editor_service",
                        lambda media_type, pinned_connection_id=None: editor._RuntimeEditorService(
                            media_type, pinned_connection_id))
    lines = [{"position": 1, "line": "Hello"}]
    job_id = editor.enqueue_editor_translation(lines, "en", "fr", title="Example", pinned_connection_id=picked.id)
    _run_editor_job(editor_queue, job_id)
    state = editor.editor_translation_state(job_id)
    assert state["status"] == "completed" and state["lines"] == [{"position": 1, "line": "[fr] Hello"}]
    assert env.served() == [str(picked.id)]


def test_a_pick_makes_the_editor_job_a_different_job_from_the_same_lines_without_one(editor_queue):
    from subtitles.tools.translate import editor

    lines = [{"position": 1, "line": "Hello"}]
    plain = editor.enqueue_editor_translation(lines, "en", "fr")
    picked = editor.enqueue_editor_translation(lines, "en", "fr", pinned_connection_id=4)
    again = editor.enqueue_editor_translation(lines, "en", "fr", pinned_connection_id=4)
    assert plain != picked and again == picked
    kwargs = {job.job_id: job.kwargs for job in editor_queue.jobs_pending_queue}
    assert "pinned_connection_id" not in kwargs[plain] and kwargs[picked]["pinned_connection_id"] == 4


# ------------------------------------------------------------------------------------------- the endpoints


def _open_the_gate(monkeypatch):
    monkeypatch.setattr(availability, "runtime_active", lambda: True)
    asked = []

    def pinned(connection_id, *_args, **_kwargs):
        asked.append(connection_id)
        return gate.GateResult(True)

    monkeypatch.setattr(gate, "pinned_gate", pinned)
    return asked


def _manual_translate(schema_session, monkeypatch, pin=None):
    from api.subtitles import subtitles as api_mod
    from app.database import TableMovies

    monkeypatch.setattr(api_mod, "database", schema_session)
    monkeypatch.setattr(api_mod, "alpha3_from_alpha2", lambda code: "eng")
    monkeypatch.setattr(api_mod.path_mappings, "path_replace_instance", lambda path, arr_instance_id, media_type: path)
    events = []
    monkeypatch.setattr(api_mod, "translate_subtitles_file", lambda **kwargs: events.append(("queue", kwargs)))
    monkeypatch.setattr(api_mod, "pin_translation", pin or (lambda *args: events.append(("pin", args))))
    schema_session.add(TableMovies(id=2, arr_instance_id=3, radarrId=9, title="M", path="/movies/m.mkv", tmdbId="1"))
    schema_session.commit()

    def call(**extra):
        args = {"action": "translate", "language": "hu", "path": "", "type": "movie", "id": 9, "arr_instance_id": 3,
                "from_language": "en", "forced": "False", "hi": "False", "connection_id": None}
        args.update(extra)
        stub = SimpleNamespace(patch_request_parser=SimpleNamespace(parse_args=lambda: args))
        return api_mod.Subtitles.patch.__wrapped__(stub), events

    return call


def test_the_translate_action_records_the_pick_before_it_queues(schema_session, monkeypatch):
    asked = _open_the_gate(monkeypatch)
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call(connection_id=6)
    assert answer == ("", 204) and asked == [6]
    assert [kind for kind, _ in events] == ["pin", "queue"]
    video_path, source, to_lang, forced, hi, embedded, pin = events[0][1]
    assert (to_lang, forced, hi, pin) == ("hu", False, False, 6)
    assert "pinned_connection_id" not in events[1][1], "the queued call is exactly what it always was"


def test_the_translate_action_without_a_pick_asks_the_selected_engine(schema_session, monkeypatch):
    from api.subtitles import subtitles as api_mod

    monkeypatch.setattr(api_mod, "translation_available", lambda: availability.TranslationAvailability(True, ""))
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call()
    # The pin step is always called and does nothing without a pick.
    assert answer == ("", 204) and events[0][1][-1] is None and events[-1][0] == "queue"


def test_the_translate_action_refuses_a_pick_with_the_reason_and_queues_nothing(schema_session, monkeypatch):
    monkeypatch.setattr(availability, "runtime_active", lambda: True)
    monkeypatch.setattr(gate, "pinned_gate", lambda *a, **k: gate.GateResult(False, gate.PICK_UNUSABLE, "x"))
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call(connection_id=6)
    assert answer == (gate.PICK_UNUSABLE, 409) and events == []


def test_the_translate_action_hands_an_engine_pick_to_the_queue_as_an_engine(schema_session, monkeypatch):
    asked = _open_the_gate(monkeypatch)
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call(engine_id=2)
    assert answer == ("", 204) and asked == [-2]
    assert events[0][1][-1] == -2 and pick.split_pin(events[0][1][-1]) == ("engine", 2)


def test_the_translate_action_refuses_both_a_back_end_and_an_engine(schema_session, monkeypatch):
    _open_the_gate(monkeypatch)
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call(connection_id=6, engine_id=2)
    assert answer == (pick.BOTH, 400) and events == []


def test_the_translate_action_refuses_a_pick_while_the_runtime_is_off(schema_session, monkeypatch):
    monkeypatch.setattr(availability, "runtime_active", lambda: False)
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call(connection_id=6)
    assert answer == (availability.PICK_NEEDS_RUNTIME, 409) and events == []


def test_the_translate_action_says_why_when_the_job_already_runs(schema_session, monkeypatch):
    _open_the_gate(monkeypatch)

    def refuse(*_args):
        raise TranslationJobError("This subtitle is already being translated.")

    call = _manual_translate(schema_session, monkeypatch, pin=refuse)
    answer, events = call(connection_id=6)
    assert answer == ("This subtitle is already being translated.", 409) and events == []


def _batch_call(monkeypatch):
    from api.subtitles import batch as batch_mod

    fed = []
    monkeypatch.setattr(batch_mod, "jobs_queue", SimpleNamespace(
        feed_jobs_pending_queue=lambda **kwargs: fed.append(kwargs)))

    def call(options):
        body = {"action": "translate", "items": [{"type": "movie", "radarrId": 9}],
                "options": {"from_lang": "en", "to_lang": "hu", **options}}
        with Flask(__name__).test_request_context("/api/subtitles/batch", method="POST", json=body):
            return batch_mod.BatchOperation.post.__wrapped__(batch_mod.BatchOperation()), fed

    return call


def test_the_batch_carries_the_pick_into_the_job(monkeypatch):
    asked = _open_the_gate(monkeypatch)
    answer, fed = _batch_call(monkeypatch)({"connection_id": 6})
    assert answer[1] == 200 and asked == [6]
    assert fed[0]["kwargs"]["options"]["connection_id"] == 6


@pytest.mark.parametrize("bad", ["6", 6.5, True, [6]])
def test_the_batch_refuses_a_pick_that_is_not_a_whole_number(monkeypatch, bad):
    _open_the_gate(monkeypatch)
    answer, fed = _batch_call(monkeypatch)({"connection_id": bad})
    assert answer == ({"error": "connection_id must be a whole number"}, 400) and fed == []


def test_the_batch_carries_an_engine_pick_into_the_job(monkeypatch):
    asked = _open_the_gate(monkeypatch)
    answer, fed = _batch_call(monkeypatch)({"engine_id": 2})
    assert answer[1] == 200 and asked == [-2]
    assert fed[0]["kwargs"]["options"]["engine_id"] == 2


def test_the_batch_refuses_both_a_back_end_and_an_engine(monkeypatch):
    _open_the_gate(monkeypatch)
    answer, fed = _batch_call(monkeypatch)({"connection_id": 6, "engine_id": 2})
    assert answer == ({"error": pick.BOTH}, 400) and fed == []


def test_the_batch_refuses_a_pick_the_gate_refuses(monkeypatch):
    monkeypatch.setattr(availability, "runtime_active", lambda: False)
    answer, fed = _batch_call(monkeypatch)({"connection_id": 6})
    assert answer == ({"error": availability.PICK_NEEDS_RUNTIME}, 409) and fed == []


MASS_ITEM = {"video_path": "/v/film.mkv", "srt_path": SOURCE, "srt_lang": "en", "forced": False, "hi": False,
             "sonarr_series_id": None, "sonarr_episode_id": None, "radarr_id": 9, "metadata": None,
             "arr_instance_id": 3}


def test_each_item_of_a_mass_translate_goes_to_the_queue_with_the_pick(monkeypatch):
    from subtitles import mass_operations
    from subtitles.tools.translate import main

    events = []
    monkeypatch.setattr(main, "translate_subtitles_file", lambda **kwargs: events.append(("queue", kwargs)) or 77)
    monkeypatch.setattr(main, "pin_translation", lambda *args: events.append(("pin", args)))
    asked = _open_the_gate(monkeypatch)
    options = {"from_lang": "en", "to_lang": "hu", "connection_id": 6}
    assert mass_operations._process_subtitle_item(MASS_ITEM, "translate", options, 5, []) is True
    assert asked == [6] and [kind for kind, _ in events] == ["pin", "queue"]
    assert events[0][1] == ("/v/film.mkv", SOURCE, "hu", False, False, None, 6)
    assert "pinned_connection_id" not in events[1][1]


def test_an_item_of_a_mass_translate_with_no_pick_is_queued_as_before(monkeypatch):
    from subtitles import mass_operations
    from subtitles.tools.translate import main

    queued = []
    monkeypatch.setattr(main, "translate_subtitles_file", lambda **kwargs: queued.append(kwargs) or 77)
    monkeypatch.setattr(main, "pin_translation", lambda *args: pytest.fail("a pick was recorded"))
    monkeypatch.setattr(availability, "translation_available", lambda: availability.TranslationAvailability(True, ""))
    options = {"from_lang": "en", "to_lang": "hu"}
    assert mass_operations._process_subtitle_item(MASS_ITEM, "translate", options, 5, []) is True
    assert len(queued) == 1


def test_an_item_of_a_mass_translate_fails_with_the_reason_when_its_pick_is_gone(monkeypatch):
    from subtitles import mass_operations
    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "translate_subtitles_file", lambda **kwargs: pytest.fail("a doomed job was queued"))
    monkeypatch.setattr(availability, "runtime_active", lambda: True)
    monkeypatch.setattr(gate, "pinned_gate", lambda *a, **k: gate.GateResult(False, gate.PICK_MISSING, "x"))
    options = {"from_lang": "en", "to_lang": "hu", "connection_id": 6}
    with pytest.raises(ValueError, match="no longer exists"):
        mass_operations._process_subtitle_item(MASS_ITEM, "translate", options, 5, [])


def _editor_call(monkeypatch, body):
    from api.translator import translator as api_mod

    queued = []
    monkeypatch.setattr(api_mod, "enqueue_editor_translation", lambda *args, **kwargs: queued.append(kwargs) or 12)
    with Flask(__name__).test_request_context("/api/translator/editor", method="POST", json=body):
        answer = api_mod.TranslatorEditorJobs.post.__wrapped__(api_mod.TranslatorEditorJobs())
    return answer, queued


EDITOR_BODY = {"lines": [{"position": 0, "line": "Hello"}], "targetLanguage": "hu"}


def test_the_editor_endpoint_queues_the_pick(monkeypatch):
    asked = _open_the_gate(monkeypatch)
    answer, queued = _editor_call(monkeypatch, {**EDITOR_BODY, "connectionId": 6})
    assert answer == ({"jobId": 12}, 202) and asked == [6]
    assert queued[0]["pinned_connection_id"] == 6


def test_the_editor_endpoint_without_a_pick_queues_as_it_always_did(monkeypatch):
    from api.translator import translator as api_mod

    monkeypatch.setattr(api_mod, "translation_available",
                        lambda translator_type: availability.TranslationAvailability(True, ""))
    answer, queued = _editor_call(monkeypatch, EDITOR_BODY)
    assert answer == ({"jobId": 12}, 202) and "pinned_connection_id" not in queued[0]


@pytest.mark.parametrize("bad", ["6", 6.5, True])
def test_the_editor_endpoint_refuses_a_pick_that_is_not_a_whole_number(monkeypatch, bad):
    _open_the_gate(monkeypatch)
    answer, queued = _editor_call(monkeypatch, {**EDITOR_BODY, "connectionId": bad})
    assert answer == ({"error": "connectionId must be a whole number"}, 400) and queued == []


def test_the_editor_endpoint_queues_an_engine_pick_as_an_engine(monkeypatch):
    asked = _open_the_gate(monkeypatch)
    answer, queued = _editor_call(monkeypatch, {**EDITOR_BODY, "engineId": 2})
    assert answer == ({"jobId": 12}, 202) and asked == [-2]
    assert queued[0]["pinned_connection_id"] == -2


def test_the_editor_endpoint_refuses_both_a_back_end_and_an_engine(monkeypatch):
    _open_the_gate(monkeypatch)
    answer, queued = _editor_call(monkeypatch, {**EDITOR_BODY, "connectionId": 6, "engineId": 2})
    assert answer == ({"error": pick.BOTH}, 400) and queued == []


def test_the_editor_runs_on_the_picked_engine_alone(env, editor_queue, monkeypatch):
    from subtitles.tools.translate import editor
    from subtitles.tools.translate.runtime import service

    env.ai_engine()
    google = env.engine(SECOND_PLUGIN, select=False)
    monkeypatch.setattr(service, "Dispatcher", lambda: env.dispatcher())
    monkeypatch.setattr(editor, "_runtime_editor_service",
                        lambda media_type, pinned_connection_id=None: editor._RuntimeEditorService(
                            media_type, pinned_connection_id))
    job_id = editor.enqueue_editor_translation([{"position": 1, "line": "Hello"}], "en", "fr",
                                               pinned_connection_id=pick.pin_value(engine_id=google.id))
    _run_editor_job(editor_queue, job_id)
    state = editor.editor_translation_state(job_id)
    assert state["status"] == "completed" and env.served() == ["-"]
    assert env.last_job().engine_instance_id == google.id


def test_the_editor_endpoint_refuses_a_pick_the_gate_refuses(monkeypatch):
    monkeypatch.setattr(availability, "runtime_active", lambda: False)
    answer, queued = _editor_call(monkeypatch, {**EDITOR_BODY, "connectionId": 6})
    assert answer == ({"error": availability.PICK_NEEDS_RUNTIME}, 409) and queued == []


# ------------------------------------------------------------------------------- ids below one are no pick


@pytest.mark.parametrize("name", ["connection_id", "engine_id"])
def test_the_translate_action_refuses_an_id_below_one(schema_session, monkeypatch, name):
    _open_the_gate(monkeypatch)
    call = _manual_translate(schema_session, monkeypatch)
    answer, events = call(**{name: 0})
    assert answer == (f"{name} must be a whole number of 1 or more", 400) and events == []


@pytest.mark.parametrize("name", ["connection_id", "engine_id"])
@pytest.mark.parametrize("value", [0, -2])
def test_the_batch_refuses_an_id_below_one(monkeypatch, name, value):
    _open_the_gate(monkeypatch)
    answer, fed = _batch_call(monkeypatch)({name: value})
    assert answer == ({"error": f"{name} must be a whole number of 1 or more"}, 400) and fed == []


@pytest.mark.parametrize("key", ["connectionId", "engineId"])
def test_the_editor_endpoint_refuses_an_id_below_one(monkeypatch, key):
    _open_the_gate(monkeypatch)
    answer, queued = _editor_call(monkeypatch, {**EDITOR_BODY, key: -1})
    assert answer[1] == 400 and "whole number of 1 or more" in answer[0]["error"] and queued == []


# ---------------------------------------------------- a mass translate with a pick asks the pick's own gate


def test_a_mass_translate_with_a_pick_runs_when_the_selected_engines_gate_is_closed(monkeypatch):
    """The batch endpoint asks the pick's gate and queues the batch, so the batch job must ask it too, for the
    entry check, for every item and for the pre-extraction of the embedded ones."""
    from subtitles import mass_operations
    from subtitles.tools.translate import batch, main

    items = [{"video_path": f"/video/ep{n}.mkv", "srt_path": None, "srt_lang": "en", "forced": False, "hi": False,
              "sonarr_series_id": 10, "sonarr_episode_id": n, "radarr_id": None, "arr_instance_id": 7,
              "metadata": None, "embedded": True} for n in (1, 2)]
    asked, queued, extracted = [], [], []
    monkeypatch.setattr(availability, "runtime_active", lambda: True)
    monkeypatch.setattr(gate, "evaluate", lambda *a, **k: gate.GateResult(False, "No translator is configured", "x"))

    def pinned(pin, *_args, **_kwargs):
        asked.append(pin)
        return gate.GateResult(pin == 6)

    monkeypatch.setattr(gate, "pinned_gate", pinned)
    monkeypatch.setattr(mass_operations, "_collect_subtitle_items", lambda *args: (list(items), 0))
    monkeypatch.setattr(mass_operations, "settings", SimpleNamespace(
        general=SimpleNamespace(mass_embedded_extract_workers=1)))
    monkeypatch.setattr(mass_operations, "jobs_queue", SimpleNamespace(
        update_job_progress=lambda **kwargs: None, update_job_name=lambda **kwargs: None,
        publish_job_result=lambda job_id, result: None))
    monkeypatch.setattr(batch, "extract_embedded_subtitle",
                        lambda video_path, *a, **k: extracted.append(video_path) or video_path + ".srt")
    monkeypatch.setattr(main, "pin_translation", lambda *args: None)
    monkeypatch.setattr(main, "translate_subtitles_file", lambda **kwargs: queued.append(kwargs) or 100 + len(queued))

    options = {"to_lang": "hu", "connection_id": 6}
    result = mass_operations.mass_batch_operation(
        items=[{"type": "episode", "sonarrEpisodeId": 1}], action="translate", options=options, job_id=5)

    assert result == {"queued": 2, "skipped": 0, "errors": []}
    assert len(extracted) == 2 and len(queued) == 2, "every embedded item was extracted and queued"
    assert set(asked) == {6}, "the pick's gate answered every time, the selected engine's never did"
    # The same batch with no pick is refused by the selected engine's gate, as it always was.
    from app.jobs_queue import JobFailed

    with pytest.raises(JobFailed, match="No translator is configured"):
        mass_operations.mass_batch_operation(
            items=[{"type": "episode", "sonarrEpisodeId": 1}], action="translate",
            options={"to_lang": "hu"}, job_id=5)


def test_a_mass_translate_with_a_pick_that_is_gone_fails_with_the_reason(monkeypatch):
    from subtitles import mass_operations
    from app.jobs_queue import JobFailed

    monkeypatch.setattr(availability, "runtime_active", lambda: True)
    monkeypatch.setattr(gate, "pinned_gate", lambda *a, **k: gate.GateResult(False, gate.PICK_MISSING, "x"))
    with pytest.raises(JobFailed, match="no longer exists"):
        mass_operations.mass_batch_operation(
            items=[{"type": "movie", "radarrId": 1}], action="translate",
            options={"to_lang": "hu", "engine_id": 3}, job_id=5)


# ------------------------------------------------------ a pick is never dropped when the switch goes off


def test_an_editor_job_with_a_pick_fails_with_the_reason_when_the_new_translator_was_switched_off(
        editor_queue, monkeypatch):
    from subtitles.tools.translate import editor

    monkeypatch.setattr(availability, "runtime_active", lambda: False)
    monkeypatch.setattr(editor, "OpenRouterTranslatorService",
                        lambda **kwargs: pytest.fail("the old service ran without the pick"))
    job_id = editor.enqueue_editor_translation([{"position": 1, "line": "Hello"}], "en", "fr", pinned_connection_id=4)
    _run_editor_job(editor_queue, job_id)
    state = editor.editor_translation_state(job_id)
    assert state["status"] == "failed" and state["error"] == pick.SWITCHED_OFF


def test_a_queued_translation_with_a_pick_fails_with_the_reason_when_the_new_translator_was_switched_off(
        monkeypatch):
    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "jobs_queue", SimpleNamespace(
        add_job_from_function=lambda label, **kwargs: 101, get_job_name=lambda job_id: "Translating Film",
        update_job_name=lambda **kwargs: None))
    monkeypatch.setattr(main, "get_title", lambda *args, **kwargs: "Film")
    monkeypatch.setattr(main, "activity", Mock())
    monkeypatch.setattr(main, "_runtime_active", lambda: True)
    monkeypatch.setattr(enqueue, "decide", lambda *a, **k: None)
    monkeypatch.setattr(main, "_runtime_attach", lambda *a, **k: None)
    monkeypatch.setattr(main, "_runtime_follow", lambda *a, **k: None)
    # The request with the pick adds the queue job.
    _pin(main, 5)
    assert _request(main) == 101
    # The new translator is switched off while that job waits.
    monkeypatch.setattr(main, "_runtime_active", lambda: False)
    monkeypatch.setattr(main, "TranslatorFactory", SimpleNamespace(
        create_translator=lambda *a, **k: pytest.fail("the old translator ran without the pick")))
    with pytest.raises(main.JobFailed) as caught:
        _request(main, job_id=101)
    assert str(caught.value) == pick.SWITCHED_OFF and caught.value.reason == pick.SWITCHED_OFF_REASON
    # A job with no pick is not touched, and a picked one is only reported once.
    assert pick.take(101) is False


def test_a_queue_job_added_by_a_request_with_no_pick_is_not_a_picked_one(monkeypatch):
    from subtitles.tools.translate import main

    monkeypatch.setattr(main, "jobs_queue", SimpleNamespace(add_job_from_function=lambda label, **kwargs: 102))
    monkeypatch.setattr(main, "get_title", lambda *args, **kwargs: "Film")
    monkeypatch.setattr(main, "_runtime_active", lambda: False)
    _pin(main, 5)  # with the new translator off a pick records nothing and expects nothing
    assert _request(main) == 102
    assert pick.take(102) is False

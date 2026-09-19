# coding=utf-8
"""Observational helpers behind the Discover local-work summary.

These cover the app-side registry and the detached queue snapshot only. They
must not change scheduling, cancellation, retries or job return values.
"""

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def clean_registry():
    from app import activity
    activity.reset()
    yield
    activity.reset()


def test_activity_ids_are_process_scoped_and_stable_per_queue_job():
    from app import activity
    first = activity.activity_id_for_job(7)
    assert first == activity.activity_id_for_job(7)
    assert first != activity.activity_id_for_job(8)
    assert activity.PROCESS_RUN_ID and activity.PROCESS_RUN_ID in first
    assert activity.activity_id_for_job(None) is None


def test_registration_keeps_one_observation_per_activity():
    from app import activity
    identity = activity.activity_id_for_job(1)
    activity.register(identity, operation="translation", scope_kind="media")
    activity.register(identity, operation="translation", scope_kind="media")
    activity.note_scope(identity, arr_instance_id=2, media_type="episode", language="hu")
    observations = activity.snapshot()
    assert [item["activity_id"] for item in observations] == [identity]
    assert observations[0]["arr_instance_id"] == 2
    assert observations[0]["language"] == "hu"
    assert observations[0]["operation"] == "translation"


def test_snapshot_returns_detached_copies():
    from app import activity
    identity = activity.activity_id_for_job(1)
    activity.register(identity, operation="translation", scope_kind="media")
    activity.note_progress(identity, unit="item", value=1, total=4)
    observation = activity.snapshot()[0]
    observation["operation"] = "tampered"
    observation["progress"]["value"] = 99
    assert activity.snapshot()[0]["operation"] == "translation"
    assert activity.snapshot()[0]["progress"] == {"unit": "item", "value": 1, "total": 4}


def test_registry_is_bounded_and_evicts_the_oldest_finished_work():
    from app import activity
    for index in range(activity.MAX_ACTIVE + 40):
        identity = activity.activity_id_for_job(index)
        activity.register(identity, operation="manual_download", scope_kind="media")
        if index % 2 == 0:
            activity.finish(identity, outcome="success")
    assert len(activity.snapshot()) <= activity.MAX_ACTIVE
    assert len(activity.observed_publications()) <= activity.MAX_RETAINED


def test_partial_and_failure_dominate_a_completed_envelope():
    from app import activity
    partial = activity.activity_id_for_job(1)
    activity.register(partial, operation="translation", scope_kind="media")
    activity.note_publication(partial, outcome="partial", detail="12 cues untranslated")
    activity.note_publication(partial, outcome="success")
    activity.finish(partial, outcome="success")

    cancelled = activity.activity_id_for_job(2)
    activity.register(cancelled, operation="translation", scope_kind="media")
    activity.note_publication(cancelled, outcome="cancelled")
    activity.finish(cancelled, outcome="success")

    outcomes = {item["activity_id"]: item["publication_outcome"]
                for item in activity.observed_publications()}
    assert outcomes[partial] == "partial"
    assert outcomes[cancelled] == "cancelled"
    assert [item["activity_id"] for item in activity.observed_publications()
            if item["publication_outcome"] == "success"] == []


def test_positive_publication_is_retained_for_arrivals():
    from app import activity
    identity = activity.activity_id_for_job(4)
    activity.register(identity, operation="translation", scope_kind="media")
    activity.note_scope(identity, media_type="movie", movie_id=11, arr_instance_id=1,
                        title="Example", language="hu")
    activity.note_publication(identity, outcome="success")
    activity.finish(identity, outcome="success")
    published = activity.observed_publications()
    assert len(published) == 1
    assert published[0]["publication_outcome"] == "success"
    assert published[0]["title"] == "Example"
    assert published[0]["language"] == "hu"
    assert published[0]["published_at"]


def test_remote_alias_maps_a_late_service_observation_onto_one_activity():
    from app import activity
    identity = activity.activity_id_for_job(9)
    activity.register(identity, operation="translation", scope_kind="media")
    activity.note_remote_submission(identity, service_id="translator", remote_job_id="abc")
    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="queued")
    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="processing",
                               progress=40, total=100)
    observations = activity.snapshot()
    assert len(observations) == 1
    assert observations[0]["remote"]["phase"] == "processing"
    assert observations[0]["remote"]["job_id"] == "abc"
    assert observations[0]["remote"]["service_id"] == "translator"
    assert observations[0]["remote"]["observed_at"]

    activity.finish(identity, outcome="success")
    activity.note_remote_phase(service_id="translator", remote_job_id="abc", phase="completed")
    assert len(activity.snapshot()) == 0


def test_a_submission_this_host_stops_watching_expires(monkeypatch):
    from app import activity
    identity = activity.new_activity_id("translation")
    activity.register(identity, operation="translation", scope_kind="editor", ttl_seconds=600)
    assert [item["activity_id"] for item in activity.snapshot()] == [identity]
    clock = [activity.time.monotonic() + 601]
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    assert activity.snapshot() == []
    assert activity.observed_publications() == []


def test_unknown_remote_job_never_invents_an_activity():
    from app import activity
    activity.note_remote_phase(service_id="translator", remote_job_id="stray", phase="processing")
    assert activity.snapshot() == []


def test_scheduler_run_is_thread_local_and_restored():
    from app import activity
    seen = {}

    def worker():
        seen["inside_other_thread"] = activity.current_scheduler_run()

    with activity.scheduler_run("wanted_search_missing_subtitles_series"):
        run_id = activity.current_scheduler_run()
        assert run_id
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        with activity.scheduler_run("nested"):
            assert activity.current_scheduler_run() != run_id
        assert activity.current_scheduler_run() == run_id
    assert activity.current_scheduler_run() is None
    assert seen["inside_other_thread"] is None


def test_observed_operation_records_running_work_and_clears_on_exit():
    from app import activity
    with activity.observed_operation("discover_search", scope_kind="request",
                                     language="hu", title="Example") as identity:
        running = activity.snapshot()
        assert [item["activity_id"] for item in running] == [identity]
        assert running[0]["state"] == "running"
        assert running[0]["language"] == "hu"
    assert activity.snapshot() == []


def test_observed_operation_records_a_failure_without_swallowing_it():
    from app import activity
    with pytest.raises(RuntimeError):
        with activity.observed_operation("discover_search", scope_kind="request") as identity:
            del identity
            raise RuntimeError("provider exploded")
    assert activity.snapshot() == []
    assert [item["publication_outcome"] for item in activity.observed_publications()] == []


def test_progress_is_only_recorded_when_it_is_measured():
    from app import activity
    identity = activity.activity_id_for_job(3)
    activity.register(identity, operation="wanted_search", scope_kind="server")
    activity.note_progress(identity, unit="item", value=0, total=0)
    assert activity.snapshot()[0]["progress"] is None
    activity.note_progress(identity, unit="item", value=3, total=12)
    assert activity.snapshot()[0]["progress"] == {"unit": "item", "value": 3, "total": 12}
    activity.note_progress(identity, unit="item", value=-1, total=12)
    assert activity.snapshot()[0]["progress"] == {"unit": "item", "value": 3, "total": 12}


@pytest.fixture
def queue(monkeypatch):
    from app.jobs_queue import JobsQueue
    monkeypatch.setattr("app.jobs_queue.event_stream", lambda *args, **kwargs: None)
    return JobsQueue()


def test_queue_snapshot_is_detached_whitelisted_and_bounded(queue):
    from app.jobs_queue import JobsQueue
    for index in range(8):
        queue.feed_jobs_pending_queue(f"Job {index}", "subtitles.wanted.movies",
                                      "wanted_search_missing_subtitles_movies",
                                      kwargs={"secret_api_key": "do-not-leak",
                                              "arr_instance_id": index})
    reserved = queue._reserve_next_job()
    reserved.status = "running"
    running_but_not_started = queue._reserve_next_job()

    snapshot = queue.snapshot_activity(limit_per_state=3)
    assert snapshot["counts"]["running"] == 1
    assert snapshot["counts"]["reserved"] == 1
    assert snapshot["counts"]["pending"] == 6
    assert len(snapshot["samples"]["pending"]) == 3
    assert snapshot["truncated"]["pending"] is True
    assert snapshot["truncated"]["running"] is False
    body = repr(snapshot)
    assert "do-not-leak" not in body
    for sample in snapshot["samples"]["pending"] + snapshot["samples"]["running"]:
        assert set(sample) <= set(JobsQueue.SNAPSHOT_FIELDS)
        assert "kwargs" not in sample and "args" not in sample
        assert "job_returned_value" not in sample
    assert running_but_not_started.status == "pending"
    # The snapshot must not consume, reorder or mutate the queues.
    assert len(queue.jobs_pending_queue) == 6
    assert len(queue.jobs_running_queue) == 2


def test_queue_snapshot_separates_creation_start_and_finish_observations(queue, monkeypatch):
    from types import SimpleNamespace
    module = SimpleNamespace(work=lambda **kwargs: "/library/example.hu.srt")
    monkeypatch.setattr("app.jobs_queue.importlib.import_module", lambda name: module)
    queue.feed_jobs_pending_queue("Job", "tests.fake", "work", kwargs={})
    pending = queue.snapshot_activity()["samples"]["pending"][0]
    assert pending["created_at"] and pending["started_at"] is None
    assert pending["finished_at"] is None
    assert pending["age_seconds"] >= 0

    assert queue._run_job(queue._reserve_next_job()) is True
    finished = queue.snapshot_activity()["samples"]["completed"][0]
    assert finished["started_at"] and finished["finished_at"]
    assert finished["created_at"] <= finished["started_at"] <= finished["finished_at"]
    assert finished["status"] == "completed"


def test_tagged_scheduler_pool_forwards_every_argument_and_tags_the_thread():
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from app import activity
    seen = {}

    def run_job(job, alias, run_times, logger_name):
        seen["run"] = activity.current_scheduler_run()
        seen["args"] = (job.id, alias, run_times, logger_name)
        return "job result"

    with ThreadPoolExecutor(max_workers=1) as pool:
        tagged = activity.tagged_scheduler_pool(pool)
        future = tagged.submit(run_job, SimpleNamespace(id="wanted_search"), "default",
                               [], "apscheduler")
        assert future.result() == "job result"

    assert seen["args"] == ("wanted_search", "default", [], "apscheduler")
    assert seen["run"] and "wanted_search" in seen["run"]
    assert activity.current_scheduler_run() is None


def test_tagged_scheduler_pool_propagates_a_failure_untouched():
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from app import activity

    def run_job(job, *rest):
        raise RuntimeError("job exploded")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = activity.tagged_scheduler_pool(pool).submit(
            run_job, SimpleNamespace(id="task"), "default", [], "apscheduler")
        with pytest.raises(RuntimeError, match="job exploded"):
            future.result()


def test_a_real_scheduled_job_inherits_its_task_tag_into_the_queue_child(monkeypatch):
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.background import BackgroundScheduler
    from datetime import datetime
    from threading import Event

    from app import activity
    from app.jobs_queue import JobsQueue

    monkeypatch.setattr("app.jobs_queue.event_stream", lambda *args, **kwargs: None)
    queue = JobsQueue()
    finished = Event()
    recorded = {}

    def scheduled_wrapper():
        recorded["tag"] = activity.current_scheduler_run()
        queue.feed_jobs_pending_queue("Searching for missing series subtitles",
                                      "subtitles.wanted.series",
                                      "wanted_search_missing_subtitles_series", kwargs={})
        finished.set()

    executor = ThreadPoolExecutor()
    executor._pool = activity.tagged_scheduler_pool(executor._pool)
    aps = BackgroundScheduler()
    aps.add_executor(executor, "default")
    aps.start()
    try:
        aps.add_job(scheduled_wrapper, "date", run_date=datetime.now(),
                    id="wanted_search_missing_subtitles_series")
        assert finished.wait(10)
        # Let the scheduler retire the one-shot job before it is torn down,
        # otherwise its own cleanup races the shutdown and logs a lookup error.
        deadline = time.monotonic() + 5
        while aps.get_job("wanted_search_missing_subtitles_series") and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        aps.shutdown(wait=True)

    child = queue.jobs_pending_queue[0]
    assert recorded["tag"]
    assert child.scheduler_run_id == recorded["tag"]
    assert "wanted_search_missing_subtitles_series" in child.scheduler_run_id

# coding=utf-8
"""The sports manual search runs as a standard job and publishes its rows."""

from types import SimpleNamespace

import pytest

import app.database  # noqa: F401


@pytest.fixture
def harness(monkeypatch):
    from app import jobs_queue as queue_module
    from sportarr import manual_jobs
    from sportarr import subtitles as sports_subtitles

    monkeypatch.setattr(queue_module.JobsQueue, "_flush_progress_loop", lambda self: None)
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    queue = queue_module.JobsQueue()
    monkeypatch.setattr(manual_jobs, "jobs_queue", queue)
    monkeypatch.setattr(manual_jobs, "database", SimpleNamespace(get=lambda *a, **k: SimpleNamespace(title="Race")))
    monkeypatch.setattr("app.jobs_queue.jobs_queue", queue)

    def run(searching, **overrides):
        monkeypatch.setattr(sports_subtitles, "manual_search_sports", searching)
        kwargs = {"event_id": 61, "language": "en", "hi": False, "forced": False, "arr_instance_id": 1, **overrides}
        job_id = queue.feed_jobs_pending_queue(job_name="Search", module="sportarr.manual_jobs",
                                               func="run_sports_search_job", kwargs=kwargs, is_progress=True,
                                               progress_max=2, retryable=False)
        job = next(item for item in queue.jobs_pending_queue if item.job_id == job_id)
        queue.jobs_pending_queue.remove(job)
        queue.jobs_running_queue.append(job)
        queue._run_job(job)
        return job

    return SimpleNamespace(queue=queue, run=run, manual_jobs=manual_jobs)


def _rows():
    return [dict(score=90.2, orig_score=300, score_without_hash=290, forced="False", language="English",
                 hearing_impaired="False", provider="fixture", subtitle="owned", url="http://x",
                 original_format=False, matches=["title"], dont_matches=[], release_info=["Race"], uploader=None)]


def test_the_job_searches_with_its_own_stop_signal_and_publishes_marshalled_rows(harness):
    seen = {}

    def searching(event_id, language, hi, forced, owner, *, cancel=None, observed=True):
        seen.update(args=(event_id, language, hi, forced, owner), cancel=cancel, observed=observed)
        return _rows()

    job = harness.run(searching)

    assert job.status == "completed"
    assert seen["args"] == (61, "en", False, False, 1)
    assert seen["observed"] is False
    assert seen["cancel"].job_id == job.job_id
    row = harness.queue.get_job_result(job.job_id)["result"][0]
    assert row["original_format"] == "False" and row["score"] == 90


def test_a_search_that_names_its_failure_fails_the_job_with_that_sentence(harness):
    def throttled(*args, **kwargs):
        raise OSError("All providers are throttled")

    job = harness.run(throttled)

    assert job.status == "failed"
    assert job.error == {"reason": "search_failed", "message": "All providers are throttled"}
    assert job.retryable is False


def test_an_event_that_vanished_before_the_job_ran_is_not_retryable(harness):
    from sportarr.errors import SportsNotFound

    def gone(*args, **kwargs):
        raise SportsNotFound("Sports event not found for this owner")

    job = harness.run(gone)

    assert job.status == "failed"
    assert job.error == {"reason": "invalid_request", "message": "Sports event not found for this owner"}
    assert job.retryable is False


def test_pressing_search_again_follows_the_running_search_and_starts_it_at_once(harness, monkeypatch):
    started = []
    monkeypatch.setattr(harness.queue, "force_start_pending_job",
                        lambda job_id, user_start=False: started.append((job_id, user_start)) or True)

    class Immediate:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(harness.manual_jobs, "Thread", Immediate)

    first = harness.manual_jobs.queue_sports_search(61, "en", False, False, 1)
    again = harness.manual_jobs.queue_sports_search(61, "en", False, False, 1)
    other_language = harness.manual_jobs.queue_sports_search(61, "hu", False, False, 1)

    assert first == again and other_language != first
    assert started == [(first, True), (first, True), (other_language, True)]
    assert harness.queue.jobs_pending_queue[0].job_name == "Searching subtitles for Race"
    assert harness.queue.jobs_pending_queue[0].lane == "user_search"
    assert harness.queue.jobs_pending_queue[0].retryable is False

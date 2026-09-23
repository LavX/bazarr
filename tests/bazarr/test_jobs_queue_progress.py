# coding=utf-8
"""Regression tests for Jobs Manager progress payload math.

Covers the ">100% progress ring" bug where ``progress_value`` could end up far
larger than ``progress_max`` (e.g. 5967%). The root logic defect is that a
``progress_value=0`` reset was silently dropped by a truthiness check, leaving a
stale large value behind a freshly-set small max.
"""


def _make_job(progress_max=0, progress_value=0):
    from app.jobs_queue import Job
    job = Job(job_id=1, job_name="x", module="m", func="f", progress_max=progress_max)
    job.progress_value = progress_value
    return job


class TestBuildProgressPayload:
    def test_zero_value_resets_stale_progress(self):
        # A sub-step that resets progress_value to 0 must actually reset it,
        # even when a prior phase left a large value behind.
        from app.jobs_queue import JobsQueue
        job = _make_job(progress_max=58, progress_value=30)
        payload = JobsQueue._build_progress_payload(job, 0, 3, "Preparing synchronization")
        assert job.progress_value == 0
        assert job.progress_max == 3
        assert payload["progress_value"] == 0
        assert payload["progress_max"] == 3

    def test_max_sentinel_completes_job(self):
        from app.jobs_queue import JobsQueue
        job = _make_job(progress_max=5, progress_value=2)
        payload = JobsQueue._build_progress_payload(job, "max", None, "Done")
        assert job.progress_value == 5
        assert job.progress_max == 5
        assert payload["progress_value"] == 5

    def test_normal_value_update(self):
        from app.jobs_queue import JobsQueue
        job = _make_job(progress_max=10, progress_value=0)
        payload = JobsQueue._build_progress_payload(job, 4, 10, "Working")
        assert job.progress_value == 4
        assert payload["progress_value"] == 4

    def test_message_only_preserves_value_and_max(self):
        from app.jobs_queue import JobsQueue
        job = _make_job(progress_max=58, progress_value=30)
        payload = JobsQueue._build_progress_payload(job, None, None, "Searching opensubtitles (2/58)")
        assert job.progress_value == 30
        assert job.progress_max == 58
        assert payload["progress_message"] == "Searching opensubtitles (2/58)"


def test_cancelled_jobs_stop_by_default_but_can_report_committed_results():
    import pytest
    from app.jobs_queue import JobCancelled
    from test_sportarr_workflows import private_queue
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        job = _make_job(progress_max=1)
        job.cancelled = True
        queue.jobs_running_queue.append(job)
        with pytest.raises(JobCancelled):
            queue.update_job_progress(job.job_id, progress_message="Must not continue")
        assert job.progress_message == ""
        assert queue.update_job_progress(job.job_id, progress_value="max",
            progress_message="Subtitle published; remaining work cancelled", allow_cancelled=True)
        assert job.cancelled is True and job.progress_value == 1
        assert "published" in job.progress_message
        with pytest.raises(JobCancelled):
            queue.update_job_progress(job.job_id)


# Fixture job bodies for the outcome tests below. _run_job resolves them by
# module and name exactly as it does any production job.
def named_failure(job_id=None):
    from app.jobs_queue import JobFailed
    raise JobFailed("The provider took too long to answer.", reason="timeout")


def unexpected_failure(job_id=None):
    raise RuntimeError("/secret/path/in/exception")


def finished_with_action(job_id=None, queue=None):
    queue.set_job_action(job_id, {"kind": "example.view", "label": "View", "id": 7})
    return "done"


def test_a_job_outcome_carries_its_error_or_action_to_the_user(caplog):
    import logging
    from pytest import MonkeyPatch
    import app.jobs_queue as module
    from test_sportarr_workflows import private_queue

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        events = []
        monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs["payload"]))
        monkeypatch.setattr(module.activity, "finish", lambda *args, **kwargs: None)

        def run(func, **kwargs):
            job_id = queue.feed_jobs_pending_queue("Example", __name__, func, kwargs=kwargs, retryable=True)
            job = queue.jobs_pending_queue.popleft()
            queue.jobs_running_queue.append(job)
            with caplog.at_level(logging.DEBUG):
                queue._run_job(job)
            return job_id, job

        _, named = run("named_failure")
        assert named.status == "failed"
        assert named.error == {"reason": "timeout", "message": "The provider took too long to answer."}
        assert events[-1]["error"] == named.error and events[-1]["status"] == "failed"
        # A named failure is logged by the job itself, so the queue adds no traceback.
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)

        _, unexpected = run("unexpected_failure")
        assert unexpected.error == module.UNEXPECTED_JOB_ERROR
        assert "/secret" not in str(unexpected.error)

        _, finished = run("finished_with_action", queue=queue)
        assert finished.status == "completed" and finished.error is None
        assert finished.action == {"kind": "example.view", "label": "View", "id": 7}
        assert events[-1]["action"] == finished.action

        retried = queue.retry_job(named.job_id)
        assert retried and queue.jobs_pending_queue[-1].retry_of == named.job_id
        assert queue.jobs_pending_queue[-1].kwargs == {}
        assert queue.retry_job(finished.job_id) is False

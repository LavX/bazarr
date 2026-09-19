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

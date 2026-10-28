# coding=utf-8
"""Regression tests for Jobs Manager progress payload math.

Covers the ">100% progress ring" bug where ``progress_value`` could end up far
larger than ``progress_max`` (e.g. 5967%). The root logic defect is that a
``progress_value=0`` reset was silently dropped by a truthiness check, leaving a
stale large value behind a freshly-set small max.
"""

import pytest


def _make_job(progress_max=0, progress_value=0):
    from app.jobs_queue import Job
    job = Job(job_id=1, job_name="x", module="m", func="f", progress_max=progress_max)
    job.progress_value = progress_value
    return job


@pytest.fixture
def scrub_registry():
    """The live scrub value set saved, replaced and restored around one test."""
    from secret_store.scrub import clear_secret_values, register_secret_values, registered_secret_values

    previous = registered_secret_values()
    clear_secret_values()
    yield register_secret_values
    register_secret_values(previous)


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

    def test_a_progress_message_is_scrubbed_before_the_socket_and_the_api(self, scrub_registry):
        """A progress message is built by app code out of a provider's own
        words. It goes to the drawer over the socket and to the API on the
        next read, so the value is masked where the message is stored."""
        from app.jobs_queue import JobsQueue
        key = "sk-fake-provider-key-0123456789"  # pragma: allowlist secret
        scrub_registry([key])
        job = _make_job(progress_max=10, progress_value=0)
        payload = JobsQueue._build_progress_payload(job, 1, 10, f"Logging in with {key}")
        assert payload["progress_message"] == "Logging in with ***"
        assert job.progress_message == "Logging in with ***"
        assert key not in str(payload)


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


def secret_failure(job_id=None):
    from app.jobs_queue import JobFailed
    raise JobFailed("Provider login failed for key sk-fake-provider-key-0123456789",  # pragma: allowlist secret
                    reason="provider_error")


def test_a_failed_jobs_message_is_scrubbed_before_the_socket_and_the_api(scrub_registry):
    """A JobFailed message is often built from a provider's own error body,
    which can name the credential it rejected. It goes to the drawer over the
    socket and to the API on the next read, so the queue masks it once, where
    the job's error is built."""
    from pytest import MonkeyPatch
    import app.jobs_queue as module
    from test_sportarr_workflows import private_queue

    key = "sk-fake-provider-key-0123456789"  # pragma: allowlist secret
    scrub_registry([key])
    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        events = []
        monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs["payload"]))
        monkeypatch.setattr(module.activity, "finish", lambda *args, **kwargs: None)

        queue.feed_jobs_pending_queue("Example", __name__, "secret_failure", retryable=True)
        job = queue.jobs_pending_queue.popleft()
        queue.jobs_running_queue.append(job)
        queue._run_job(job)

        assert job.status == "failed"
        assert job.error == {"reason": "provider_error", "message": "Provider login failed for key ***"}
        assert events[-1]["status"] == "failed"
        assert events[-1]["error"] == job.error
        assert key not in str(job.error)


def test_a_job_action_is_scrubbed_before_the_socket_and_the_api(scrub_registry):
    """A discover download offers Save with the provider-chosen filename in
    the action. That name is the provider's text and can echo a credential,
    and the action is stored, sent and read back like any other job text."""
    from pytest import MonkeyPatch
    from test_sportarr_workflows import private_queue

    key = "sk-fake-provider-key-0123456789"  # pragma: allowlist secret
    scrub_registry([key])
    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        queue.feed_jobs_pending_queue("Example", __name__, "noop")
        job = queue.jobs_pending_queue.popleft()
        queue.jobs_running_queue.append(job)

        assert queue.set_job_action(job.job_id, {
            "kind": "discover.save",
            "label": "Save",
            "ticket": job.job_id,
            "filename": f"Show.S01E01.{key}.srt",
        }) is True

    assert job.action["filename"] == "Show.S01E01.***.srt"
    assert job.action["kind"] == "discover.save"
    assert key not in str(job.action)


def test_a_job_name_is_scrubbed_at_creation_and_when_renamed(scrub_registry):
    """A job name is built from a media title or an uploaded filename, and
    /api/system/jobs returns it verbatim, so a credential-shaped title is
    masked where the name is set, both when the job is queued and when it is
    renamed later."""
    from pytest import MonkeyPatch
    from test_sportarr_workflows import private_queue

    key = "sk-fake-provider-key-0123456789"  # pragma: allowlist secret
    scrub_registry([key])
    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        queue.feed_jobs_pending_queue(f"Translate Show.{key}.srt", __name__, "noop")
        job = queue.jobs_pending_queue[0]
        assert job.job_name == "Translate Show.***.srt"
        assert key not in str(queue.list_jobs_from_queue())

        assert queue.update_job_name(job.job_id, f"Upload {key}.srt") is True
        assert job.job_name == "Upload ***.srt"
        assert queue.get_job_name(job.job_id) == "Upload ***.srt"
        assert key not in str(queue.list_jobs_from_queue())


def test_a_job_action_keeps_its_handler_name_when_a_credential_matches_it(scrub_registry):
    """``kind`` is dispatch: a credential that happens to equal a handler name
    must not stop the frontend finding that handler."""
    from pytest import MonkeyPatch
    from test_sportarr_workflows import private_queue

    scrub_registry(["discover.save"])
    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        queue.feed_jobs_pending_queue("Example", __name__, "noop")
        job = queue.jobs_pending_queue.popleft()
        queue.jobs_running_queue.append(job)

        assert queue.set_job_action(job.job_id, {
            "kind": "discover.save", "label": "Save", "ticket": job.job_id,
        }) is True

    assert job.action["kind"] == "discover.save"
    # The label is text and stays; nothing else in this row carried the value.
    assert job.action["label"] == "Save"


def test_the_jobs_list_sends_last_run_time_as_utc():
    # The drawer showed "2 hours ago" for a job that had just finished: the
    # time went out without an offset and the browser read it as its own local
    # time. It now goes out as UTC with an explicit Z.
    from datetime import datetime, timedelta, timezone
    from flask_restx import marshal
    from api.system.jobs import SystemJobs

    job = _make_job()
    assert job.last_run_time.tzinfo is not None
    sent = marshal([vars(job)], SystemJobs.get_response_model)[0]["last_run_time"]
    assert sent.endswith("Z")
    parsed = datetime.fromisoformat(sent.replace("Z", "+00:00"))
    assert abs(parsed - datetime.now(timezone.utc)) < timedelta(minutes=1)

    # A naive value is the server's local time, whatever zone that is.
    naive = datetime(2026, 9, 23, 12, 0, 0)
    job.last_run_time = naive
    sent = marshal([vars(job)], SystemJobs.get_response_model)[0]["last_run_time"]
    assert datetime.fromisoformat(sent.replace("Z", "+00:00")) == naive.astimezone(timezone.utc)


def test_a_duplicate_is_refused_and_can_name_the_job_it_matched():
    """A job identical to one pending or running is refused with False. A
    caller that has to follow that job asks for its id instead, and gets it
    from the same look, under the queue lock, so the job cannot finish in
    between and leave the caller with nothing to follow."""
    from pytest import MonkeyPatch
    from test_sportarr_workflows import private_queue

    def feed(**options):
        return queue.feed_jobs_pending_queue("Example", "tests.fake", "work", kwargs={"index": 0}, **options)

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        first = feed()

        assert feed() is False
        assert feed(return_existing=True) == first
        queue.jobs_running_queue.append(queue.jobs_pending_queue.popleft())
        assert feed(return_existing=True) == first
        assert len(queue.jobs_pending_queue) == 0 and len(queue.jobs_running_queue) == 1

        # A finished job is no longer a duplicate: the same call queues again.
        queue.jobs_completed_queue.append(queue.jobs_running_queue.popleft())
        second = feed(return_existing=True)
        assert second not in (False, first)


def test_a_caller_asking_to_follow_a_job_is_never_handed_one_that_was_stopped():
    """A running job that was told to stop only ends at its next check. A search asked for again in
    that window must get a job of its own, not the dying one that will read as stopped."""
    from pytest import MonkeyPatch
    from test_sportarr_workflows import private_queue

    def feed(**options):
        return queue.feed_jobs_pending_queue("Example", "tests.fake", "work", kwargs={"index": 0}, **options)

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        first = feed()
        queue.jobs_running_queue.append(queue.jobs_pending_queue.popleft())
        assert queue.cancel_running_job(first) is True

        # Without asking to follow, the stopped job still counts as the one doing the work.
        assert feed() is False
        second = feed(return_existing=True)
        assert second not in (False, first)


def argument_heavy_job(payload=None, apikey=None, job_id=None):
    return "done"


class _CountsItsRepr:
    """Stands in for a large argument, such as an uploaded package's bytes."""

    def __init__(self):
        self.formatted = 0

    def __repr__(self):
        self.formatted += 1
        return "<payload>"


def test_starting_a_job_never_formats_or_logs_its_arguments(caplog):
    """The start line was an f-string with the job's args and kwargs. It built
    their repr for every job even with debug logging off, which for a Provider
    Hub package is several times its size, and with debug on it wrote whatever
    the arguments carried, API keys included, to the log."""
    import logging
    from pytest import MonkeyPatch
    import app.jobs_queue as module
    from test_sportarr_workflows import private_queue

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        monkeypatch.setattr(module.activity, "finish", lambda *args, **kwargs: None)
        payload = _CountsItsRepr()

        def run(level):
            queue.feed_jobs_pending_queue("Example", __name__, "argument_heavy_job",
                                          kwargs={"payload": payload, "apikey": "sk-fixture-key"})
            job = queue.jobs_pending_queue.popleft()
            queue.jobs_running_queue.append(job)
            with caplog.at_level(level):
                queue._run_job(job)
            return job

        assert run(logging.INFO).status == "completed"
        assert payload.formatted == 0, "the arguments were formatted with debug logging off"

        caplog.clear()
        job = run(logging.DEBUG)
        assert job.status == "completed"
        assert payload.formatted == 0
        assert "sk-fixture-key" not in caplog.text
        assert f"Running job Example (id {job.job_id}): {__name__}.argument_heavy_job" in caplog.text


# Fixture job bodies for the cancelled-status tests. _run_job resolves them by
# module and name exactly as it does any production job.
def cancelled_job(job_id=None, queue=None):
    queue.cancel_running_job(job_id)
    # The next checkpoint is where a stop is honoured.
    queue.update_job_progress(job_id, progress_value=1, progress_max=2,
                              progress_message="Working")
    return "finished anyway"


def finished_quietly(job_id=None):
    return "done"


def test_a_stopped_job_reports_a_cancelled_status_the_event_carries_too():
    """A stop used to be recorded as completed plus a flag, so anything
    reading the API directly saw a stopped job as done. The status now says
    what happened, the terminal event carries it, and the row shares the
    completed history: one cap, and the one queue the drawer's Clear empties.
    """
    from pytest import MonkeyPatch
    import app.jobs_queue as module
    from test_sportarr_workflows import private_queue

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        events = []
        monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs["payload"]))
        monkeypatch.setattr(module.activity, "finish", lambda *args, **kwargs: None)

        job_id = queue.feed_jobs_pending_queue("Example", __name__, "cancelled_job", kwargs={"queue": queue})
        job = queue.jobs_pending_queue.popleft()
        queue.jobs_running_queue.append(job)

        assert queue._run_job(job) is False

        assert job.status == "cancelled" and job.stopped is True
        assert job.progress_message == "Cancelled by user"
        assert events[-1]["status"] == "cancelled"
        assert [(row["job_id"], row["status"]) for row in queue.list_jobs_from_queue(status="cancelled")] \
            == [(job_id, "cancelled")]
        # The two terminal statuses share a deque, so 'completed' does not
        # answer the stopped row.
        assert queue.list_jobs_from_queue(status="completed") == []
        assert [row["job_id"] for row in queue.list_jobs_from_queue()] == [job_id]
        assert not queue.jobs_failed_queue and not queue.jobs_running_queue


def test_a_stop_after_the_job_finished_neither_rewrites_nor_resurrects_it():
    from pytest import MonkeyPatch
    import app.jobs_queue as module
    from test_sportarr_workflows import private_queue

    with MonkeyPatch.context() as monkeypatch:
        queue = private_queue(monkeypatch)
        monkeypatch.setattr(module, "event_stream", lambda **kwargs: None)
        monkeypatch.setattr(module.activity, "finish", lambda *args, **kwargs: None)

        job_id = queue.feed_jobs_pending_queue("Example", __name__, "finished_quietly", kwargs={})
        job = queue.jobs_pending_queue.popleft()
        queue.jobs_running_queue.append(job)

        assert queue._run_job(job) is True

        # Stop only looks at the running queue, so the finished row is left
        # exactly as it ended and no cancelled row takes its place.
        assert queue.cancel_running_job(job_id) is False
        row = queue.list_jobs_from_queue(job_id=job_id)[0]
        assert row["status"] == "completed" and row["stopped"] is False
        assert queue.list_jobs_from_queue(status="cancelled") == []


def test_the_jobs_api_offers_the_cancelled_status():
    from api.system.jobs import SystemJobs

    status_arg = next(arg for arg in SystemJobs.get_request_parser.args if arg.name == 'status')
    assert 'cancelled' in status_arg.choices

    job = _make_job()
    job.status = "cancelled"
    job.stopped = True
    from flask_restx import marshal
    sent = marshal([vars(job)], SystemJobs.get_response_model)[0]
    assert sent["status"] == "cancelled" and sent["stopped"] is True

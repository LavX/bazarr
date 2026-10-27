# coding=utf-8
"""A manual Retry pending is a standard job that reports how many refreshes it drained."""

from types import SimpleNamespace

import pytest
from flask import Flask

from app.jobs_queue import JobFailed


class Dispatcher:
    """A dispatcher whose worker finishes after a few looks, leaving what the test says is still pending."""

    def __init__(self, total, pending_after_looks, looks=2):
        self.total, self.looks = total, looks
        self.pending = list(pending_after_looks)

    def retry(self, server):
        return self.total

    def wait_idle(self, timeout, *, server=None):
        self.looks -= 1
        return self.looks < 0


@pytest.fixture
def harness(monkeypatch):
    from media_servers import retry_job

    progress, statuses = [], []

    def status(server):
        pending = harness.dispatcher.pending.pop(0) if len(harness.dispatcher.pending) > 1 else \
            harness.dispatcher.pending[0]
        return {"pending": pending, "state": harness.state, "error_code": harness.error}

    fake_queue = SimpleNamespace(
        update_job_progress=lambda **kwargs: progress.append((kwargs.get("progress_value"),
                                                              kwargs.get("progress_message"))))
    harness = SimpleNamespace(progress=progress, statuses=statuses, state="confirmed", error=None, dispatcher=None)
    monkeypatch.setattr(retry_job, "jobs_queue", fake_queue)
    monkeypatch.setattr(retry_job, "get_refresh_status", status)
    monkeypatch.setattr(retry_job, "_get_dispatcher", lambda: harness.dispatcher)
    harness.retry_job = retry_job
    return harness


def test_the_job_reports_how_many_refreshes_it_has_drained(harness):
    harness.dispatcher = Dispatcher(total=3, pending_after_looks=[3, 2, 0])

    result = harness.retry_job.retry_pending_job("emby-1", "Living room", job_id=4)

    assert result == {"retried": 3}
    messages = [message for _, message in harness.progress]
    assert messages[0] == "Living room: 0 of 3 refreshed"
    assert "Living room: 1 of 3 refreshed" in messages
    assert messages[-1] == "Living room: 3 of 3 refreshed"
    assert harness.progress[-1][0] == "max"


def test_refreshes_the_destination_could_not_confirm_fail_the_job_with_its_reason(harness, caplog):
    harness.dispatcher = Dispatcher(total=3, pending_after_looks=[3, 1])
    harness.state, harness.error = "unconfirmed", "timeout"

    with caplog.at_level("WARNING"), pytest.raises(JobFailed) as failure:
        harness.retry_job.retry_pending_job("emby-1", "Living room", job_id=4)

    assert failure.value.reason == "timeout"
    assert str(failure.value).startswith("Living room could not confirm every refresh. 2 of 3 were refreshed.")
    assert "path mappings" in str(failure.value)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_known_reason_gets_its_own_advice(harness):
    harness.dispatcher = Dispatcher(total=1, pending_after_looks=[1])
    harness.state, harness.error = "unconfirmed", "sidecar_unsupported"

    with pytest.raises(JobFailed) as failure:
        harness.retry_job.retry_pending_job("silo-1", "Silo", job_id=4)

    assert "Move the subtitle there, then retry." in str(failure.value)


def test_something_waiting_that_cannot_be_asked_fails_and_nothing_waiting_just_completes(harness):
    harness.dispatcher = Dispatcher(total=0, pending_after_looks=[2])
    harness.error = "connection_disabled"
    with pytest.raises(JobFailed) as failure:
        harness.retry_job.retry_pending_job("emby-1", "Living room", job_id=4)
    assert failure.value.reason == "connection_disabled"
    assert "turned off" in str(failure.value)

    harness.dispatcher = Dispatcher(total=0, pending_after_looks=[0])
    harness.error = None
    assert harness.retry_job.retry_pending_job("emby-1", "Living room", job_id=4) == {"retried": 0}
    assert harness.progress[-1] == ("max", "Nothing was waiting for Living room")


def test_the_press_is_queued_started_at_once_and_followed_when_pressed_again(monkeypatch):
    from app import jobs_queue as queue_module
    from media_servers import retry_job

    monkeypatch.setattr(queue_module.JobsQueue, "_flush_progress_loop", lambda self: None)
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    queue = queue_module.JobsQueue()
    started = []
    monkeypatch.setattr(queue, "force_start_pending_job",
                        lambda job_id, user_start=False: started.append((job_id, user_start)) or True)
    monkeypatch.setattr(retry_job, "jobs_queue", queue)

    class Immediate:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(retry_job, "Thread", Immediate)

    first = retry_job.queue_retry_pending("emby-1", "Living room")
    again = retry_job.queue_retry_pending("emby-1", "Living room")

    assert first == again
    assert started == [(first, True), (first, True)]
    job = queue.jobs_pending_queue[0]
    assert job.job_name == "Retrying pending refreshes for Living room"
    assert job.retryable is True and job.is_progress is True


def test_the_route_answers_at_once_with_the_job_and_queues_nothing_when_nothing_waits(monkeypatch):
    from api.system import media_server_instances as routes
    from app.process_session import PROCESS_SESSION

    monkeypatch.setattr(routes.service, "get_instance", lambda session, instance_id: ({"name": "Living room"}, 200))
    pending = {"count": 2}
    monkeypatch.setattr(routes, "get_refresh_status", lambda instance_id: {"pending": pending["count"]})
    queued = []
    monkeypatch.setattr(routes, "queue_retry_pending", lambda *args: queued.append(args) or 31)
    resource = routes.MediaServerInstanceRetry

    with Flask(__name__).test_request_context("/retry-pending", method="POST"):
        body, status = resource.post.__wrapped__(resource(), "emby-1")
    assert status == 202
    assert body == {"queued": 2, "job_id": 31, "session": PROCESS_SESSION}
    assert queued == [("emby-1", "Living room")]

    pending["count"] = 0
    with Flask(__name__).test_request_context("/retry-pending", method="POST"):
        assert resource.post.__wrapped__(resource(), "emby-1") == ({"queued": 0}, 200)
    assert len(queued) == 1

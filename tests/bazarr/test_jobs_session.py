# coding=utf-8
"""Job ids restart with the backend, so every job and every jobs event names the
backend run it belongs to, and the socket answers which run a client is on.
"""

import pytest


def test_the_process_session_is_one_stable_stamp_per_process():
    from app import process_session

    assert isinstance(process_session.PROCESS_SESSION, str)
    assert len(process_session.PROCESS_SESSION) >= 16
    # Importing it again from elsewhere must not draw a new one.
    from app.process_session import PROCESS_SESSION

    assert PROCESS_SESSION == process_session.PROCESS_SESSION


def test_a_job_carries_the_session_it_was_created_in():
    from app.jobs_queue import Job
    from app.process_session import PROCESS_SESSION

    assert Job(job_id=1, job_name="x", module="m", func="f").session == PROCESS_SESSION


@pytest.fixture
def captured_events(monkeypatch):
    from app import jobs_queue as module

    events = []
    monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs))
    return events


def test_every_jobs_event_is_stamped_with_the_session(captured_events):
    from app.jobs_queue import JobsQueue
    from app.process_session import PROCESS_SESSION

    queue = JobsQueue()
    job_id = queue.feed_jobs_pending_queue("one", "m", "f")
    assert queue.move_job_in_pending_queue(job_id, "bottom") is True
    assert queue.remove_job_from_pending_queue(job_id) is True

    actions = [(event["action"], event["payload"]["job_id"]) for event in captured_events]
    assert actions == [("update", job_id), ("update", job_id), ("delete", job_id)]
    assert {event["type"] for event in captured_events} == {"jobs"}
    assert all(event["payload"]["session"] == PROCESS_SESSION for event in captured_events)


def test_the_jobs_list_and_the_api_send_the_session(captured_events):
    from api.system.jobs import SystemJobs
    from app.jobs_queue import JobsQueue
    from app.process_session import PROCESS_SESSION
    from flask_restx import marshal

    queue = JobsQueue()
    job_id = queue.feed_jobs_pending_queue("one", "m", "f")
    listed = queue.list_jobs_from_queue(job_id=job_id)
    assert listed[0]["session"] == PROCESS_SESSION

    sent = marshal(listed, SystemJobs.get_response_model, envelope="data")
    assert sent["data"][0]["session"] == PROCESS_SESSION


def test_the_socket_answers_which_backend_run_it_is_connected_to():
    from app.app import _report_process_session, socketio
    from app.process_session import PROCESS_SESSION

    # flask-socketio stores (event, handler, namespace).
    assert "session" in [event for event, _handler, _namespace in socketio.handlers]
    assert _report_process_session() == {"session": PROCESS_SESSION}

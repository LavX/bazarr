# coding=utf-8
"""A job can publish a result for the caller that queued it, kept apart from
the job's listing and its socket events.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_sportarr_workflows import private_queue


def _running_job(queue):
    job_id = queue.feed_jobs_pending_queue("Search", "tests.fake", "work", kwargs={"index": 0})
    queue.jobs_running_queue.append(queue.jobs_pending_queue.popleft())
    queue.jobs_running_queue[0].status = "running"
    return job_id


def test_a_running_job_publishes_a_partial_result_and_then_the_whole_one(monkeypatch):
    queue = private_queue(monkeypatch)
    job_id = _running_job(queue)

    assert queue.get_job_result(job_id)["result"] is None
    assert queue.publish_job_result(job_id, {"phase": "searching", "rows": [1]}) is True
    partial = queue.get_job_result(job_id)
    assert partial["status"] == "running"
    assert partial["result"] == {"phase": "searching", "rows": [1]}

    queue.publish_job_result(job_id, {"phase": "done", "rows": [1, 2]})
    queue.jobs_completed_queue.append(queue.jobs_running_queue.popleft())
    queue.jobs_completed_queue[0].status = "completed"
    finished = queue.get_job_result(job_id)
    assert finished["status"] == "completed"
    assert finished["result"] == {"phase": "done", "rows": [1, 2]}


def test_the_result_is_read_with_the_session_and_the_error_of_a_failed_job(monkeypatch):
    from app.process_session import PROCESS_SESSION

    queue = private_queue(monkeypatch)
    job_id = _running_job(queue)
    job = queue.jobs_running_queue.popleft()
    job.status = "failed"
    job.error = {"reason": "boom", "message": "It broke."}
    queue.jobs_failed_queue.append(job)

    outcome = queue.get_job_result(job_id)
    assert outcome == {"job_id": job_id, "session": PROCESS_SESSION, "status": "failed",
                       "error": {"reason": "boom", "message": "It broke."}, "result": None}


def test_an_unknown_or_pending_job_publishes_nothing(monkeypatch):
    queue = private_queue(monkeypatch)
    pending = queue.feed_jobs_pending_queue("Search", "tests.fake", "work")

    assert queue.get_job_result(999) is None
    assert queue.publish_job_result(999, ["x"]) is False
    # Only a running job publishes; a pending one has nothing to say yet.
    assert queue.publish_job_result(pending, ["x"]) is False
    assert queue.get_job_result(pending)["status"] == "pending"


def test_the_result_never_reaches_the_listing_or_the_events(monkeypatch):
    from flask_restx import marshal

    from api.system.jobs import SystemJobs

    queue = private_queue(monkeypatch)
    events = []
    import app.jobs_queue as module

    monkeypatch.setattr(module, "event_stream", lambda **kwargs: events.append(kwargs["payload"]))
    job_id = _running_job(queue)
    queue.publish_job_result(job_id, [{"subtitle": "abc"}])
    queue.update_job_progress(job_id, 1, 2, "Halfway")

    listed = marshal(queue.list_jobs_from_queue(job_id=job_id), SystemJobs.get_response_model)
    assert "result" not in listed[0]
    assert all("result" not in payload for payload in events)


def test_the_result_route_reads_a_job_and_answers_404_for_a_stranger(monkeypatch):
    from flask import Flask

    import api.system.jobs as jobs_api
    from api import api_bp
    from app.config import settings

    queue = private_queue(monkeypatch)
    job_id = _running_job(queue)
    queue.publish_job_result(job_id, [{"provider": "x"}])
    monkeypatch.setattr(jobs_api, "jobs_queue", queue)
    monkeypatch.setitem(settings.auth, "apikey", "synthetic-bazarr-key")
    headers = {"X-API-KEY": "synthetic-bazarr-key"}

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    client = app.test_client()

    assert client.get(f"/api/system/jobs/result?id={job_id}").status_code == 401

    found = client.get(f"/api/system/jobs/result?id={job_id}", headers=headers)
    assert found.status_code == 200
    body = found.get_json()
    assert body["job_id"] == job_id
    assert body["status"] == "running"
    assert body["result"] == [{"provider": "x"}]

    assert client.get("/api/system/jobs/result?id=4040", headers=headers).status_code == 404

# coding=utf-8
"""The health re-check answers at once with the job doing it."""

from types import SimpleNamespace

from flask import Flask

import app.database  # noqa: F401


def test_the_health_recheck_answers_at_once_with_the_job_it_queued(monkeypatch):
    from api.system import health
    from app.process_session import PROCESS_SESSION

    monkeypatch.setattr(health, "check_health", lambda: 52)
    resource = health.SystemHealth

    with Flask(__name__).test_request_context("/system/health", method="POST"):
        body, status = resource.post.__wrapped__(resource())

    assert status == 202
    assert body == {"job_id": 52, "session": PROCESS_SESSION}


def test_a_health_check_queues_itself_and_follows_the_one_already_running(monkeypatch):
    from utilities import health

    calls = []
    monkeypatch.setattr(health, "jobs_queue", SimpleNamespace(
        add_job_from_function=lambda name, is_progress, wait_for_completion=False, return_existing=False:
        calls.append((name, wait_for_completion, return_existing)) or 52))

    assert health.check_health() == 52
    # The scheduler waits for its job, so Tasks shows it running until it ends.
    assert health.check_health(wait_for_completion=True) == 52
    assert calls == [("Checking Health", False, True), ("Checking Health", True, True)]

# -*- coding: utf-8 -*-
"""Verifies the APScheduler listener that releases the per-thread
scoped_session after every job. See bazarr/app/scheduler.py
`_release_session_after_job` and the audit notes in the perf series.
"""
from unittest.mock import MagicMock, patch


def test_release_session_listener_calls_database_remove():
    """The listener must call database.remove() exactly once when a job
    fires the EVENT_JOB_EXECUTED event."""
    from app import scheduler as scheduler_module

    fake_event = MagicMock()
    with patch.object(scheduler_module, 'database') as mock_db:
        scheduler_module._release_session_after_job(fake_event)

    assert mock_db.remove.call_count == 1


def test_release_session_listener_swallows_remove_errors():
    """database.remove() raising must not propagate. APScheduler logs
    listener exceptions itself, but a swallowed error in the listener
    keeps the event bus quiet and lets the next job's listener retry
    cleanup."""
    from app import scheduler as scheduler_module

    fake_event = MagicMock()
    with patch.object(scheduler_module, 'database') as mock_db:
        mock_db.remove.side_effect = RuntimeError("boom")
        # Must not raise
        scheduler_module._release_session_after_job(fake_event)

    assert mock_db.remove.call_count == 1

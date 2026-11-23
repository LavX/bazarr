# coding=utf-8
"""Asking for a translation with the runtime on: join the job that exists, replace it, refuse, or queue a new one.

The runtime keeps at most one job queued, running or awaiting a retry for each source and target. A request
for a pair that already has one joins it. A manual request that picks a back end replaces a job that has not
started, and is refused with the reason while one runs. A job awaiting a retry is queued again by the next
request for its pair once its time has come, which is how a failure that is "retried later" gets retried: the
request is made again by the next scan or by the person, and nothing else has to remember its arguments.

Only the decision and the bookkeeping live here. The queue call itself stays in ``translate_subtitles_file``,
because the jobs queue records the arguments of the function that adds a job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .history import HistoryStore
from .jobs import (AWAITING_RETRY, QUEUED, RUNNING, JobsQueuePort, Outcome, TranslationJobs, retry_hold,
                   target_key)
from .transaction import ledger_transaction

logger = logging.getLogger(__name__)

FOLLOW = "follow"    # an equal job is on the queue: follow it
QUEUE = "queue"      # queue a new queue job, then ``attach`` it
QUEUED_FOR_RETRY = "queued_for_retry"  # the job waits for its retry time: nothing is queued before it


@dataclass(frozen=True)
class Decision:
    action: str
    translator_job_id: int | None = None
    queue_job_id: int | None = None
    # For QUEUED_FOR_RETRY: when the job may be queued again.
    retry_at: datetime | None = None


def decide(source_key: str, to_lang: str, forced: bool = False, hi: bool = False, *,
           pinned_connection_id: int | None = None, owns_pin: bool = False, session=None,
           clock: Callable[[], datetime] = datetime.now) -> Decision:
    """Make or join the job for this request. Raises ``TranslationJobError`` with the reason when refused.

    A pick belongs to the request that made it. A request with no pick that finds a picked job which is not on the
    queue any more, or whose retry time has come, takes it over as an ordinary job and clears the pick. ``owns_pin``
    is for the request that has just recorded the pick itself: it joins the job it made, pick and all.
    """
    from .jobs import TranslationJobError

    if session is None:
        from app.database import database

        session = database
    jobs = TranslationJobs(session, clock=clock)
    target = target_key(to_lang, forced, hi)
    if pinned_connection_id is not None:
        # A pick of what a job is waiting to retry with waits out that time (see ``retry_hold``).
        hold = retry_hold(jobs.active_for(source_key, target), pinned_connection_id, clock())
        if hold:
            raise TranslationJobError(hold)
    result = jobs.request(source_key, target, manual=pinned_connection_id is not None,
                          pinned_connection_id=pinned_connection_id)
    if result.outcome == Outcome.REFUSED:
        raise TranslationJobError(result.reason or "This translation cannot be queued right now.")
    if result.outcome == Outcome.REPLACED and result.replaced_job_id is not None:
        # The replaced job's usage is kept in history; its row says how it ended.
        try:
            HistoryStore(session, clock=clock).finish_job(str(result.replaced_job_id), "superseded")
        except Exception:
            logger.debug("The superseded translation had no history row to finish")
    row = jobs.get(result.job_id)
    if result.outcome in (Outcome.CREATED, Outcome.REPLACED):
        return Decision(QUEUE, row.id)
    if pinned_connection_id is None and not owns_pin and row.pinned_connection_id is not None \
            and row.state in (QUEUED, AWAITING_RETRY) and not _live(row) and not _enqueueing(row, clock()):
        # A request with no pick finds a picked job that is not on the queue any more (its queue job was removed or
        # died before it ran) or that waits for its retry: the pick belongs to the request that made it, so this
        # job is queued again as an ordinary one.
        if row.state != AWAITING_RETRY or row.retry_at is None or row.retry_at <= clock():
            jobs.clear_pin(row.id)
            row = jobs.get(row.id)
    # Joined: an equal request already has a job.
    if row.state == AWAITING_RETRY:
        if row.retry_at is not None and row.retry_at > clock():
            return Decision(QUEUED_FOR_RETRY, row.id, retry_at=row.retry_at)
        return Decision(QUEUE, row.id)
    if row.state in (QUEUED, RUNNING) and row.queue_job_id is not None \
            and JobsQueuePort().status(row.queue_job_id) in ("pending", "running"):
        return Decision(FOLLOW, row.id, row.queue_job_id)
    if row.state == RUNNING:
        return Decision(FOLLOW, row.id)  # it runs, but no queue job of the queue's is known for it
    return Decision(QUEUE, row.id)


# How long a picked record that has no queue job yet is left alone. The request that recorded the pick adds its queue
# job right after, so a record this young is a request still enqueueing, not one that was abandoned; only a record
# whose queue job is gone (its id is there but the queue has none) or that never got one for longer than this is taken
# over by a request with no pick.
ENQUEUE_GRACE = timedelta(seconds=60)


def _enqueueing(row, now) -> bool:
    """Whether this record's request is still adding its queue job."""
    return row.state == QUEUED and row.queue_job_id is None and now - (row.updated_at or row.created_at) < ENQUEUE_GRACE


def _live(row) -> bool:
    """Whether the queue still has the job this record says it is on, pending or running."""
    return (row.state in (QUEUED, RUNNING) and row.queue_job_id is not None
            and JobsQueuePort().status(row.queue_job_id) in ("pending", "running"))


def attach_for(queue_job_id, source_key: str, to_lang: str, forced: bool = False, hi: bool = False, *,
               session=None, clock: Callable[[], datetime] = datetime.now) -> None:
    """Record the queue job a request to queue ended up with, on the job the request made or joined.

    The job is found again by its key, so nothing has to be carried from ``decide`` to here.
    """
    if not queue_job_id:
        return
    if session is None:
        from app.database import database

        session = database
    jobs = TranslationJobs(session, clock=clock)
    row = jobs.active_for(source_key, target_key(to_lang, forced, hi))
    if row is None:
        return
    if row.state == AWAITING_RETRY:
        jobs.requeue(row.id, queue_job_id)
    else:
        jobs.attach_queue_job(row.id, queue_job_id)



def attached_queue_job(source_key, to_lang, forced, hi):
    """Return the job attached under an older source reservation key, if it is still active."""
    from app.database import database

    row = TranslationJobs(database).active_for(source_key, target_key(to_lang, forced, hi))
    return row.queue_job_id if row is not None and row.state in (QUEUED, RUNNING) else None


_RECORDING_CHANGED_BEFORE_RETRY = "The source media file changed before the translation could retry"


def retire_source_retries(source_key, scope, to_lang, forced, hi):
    """Record terminal outcomes for older recordings that have no queue dispatch left to end them."""
    from app.database import database

    message = _RECORDING_CHANGED_BEFORE_RETRY
    jobs = TranslationJobs(database)
    with ledger_transaction(database):
        for job_id in jobs.retire_source_retries(source_key, scope, target_key(to_lang, forced, hi), message):
            _finish_source_change(database, job_id, message)


def retire_source_retry(source_key, to_lang, forced, hi):
    """End a detached retry created by a translation that was already running during replacement."""
    from app.database import database

    try:
        message = _RECORDING_CHANGED_BEFORE_RETRY
        target = target_key(to_lang, forced, hi)
        jobs = TranslationJobs(database)
        with ledger_transaction(database):
            row = jobs.active_for(source_key, target)
            if row is not None and jobs.cancel_source_retry(row.id, source_key, target, message):
                _finish_source_change(database, row.id, message)
    except Exception:
        logger.exception("Could not retire a source translation retry for the replaced recording")


def _finish_source_change(session, job_id, reason):
    history = HistoryStore(session)
    ledger_id = str(job_id)
    if history.get_by_job(ledger_id) is not None:
        history.finish_job(ledger_id, "cancelled", error_class="source_changed", error_detail=reason)


def cancel_waiting_for(queue_job_id, source_key, to_lang, forced, hi, reason):
    """Retire a refused source request without ending a replacement queue job."""
    from app.database import database

    try:
        jobs = TranslationJobs(database)
        with ledger_transaction(database):
            row = jobs.active_for(source_key, target_key(to_lang, forced, hi))
            if row is not None and jobs.cancel_waiting(row.id, queue_job_id, reason):
                _finish_source_change(database, row.id, reason)
    except Exception:
        logger.exception("Could not retire a source translation that was no longer current")

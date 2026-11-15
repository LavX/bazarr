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
from datetime import datetime
from typing import Callable

from .history import HistoryStore
from .jobs import AWAITING_RETRY, QUEUED, RUNNING, JobsQueuePort, Outcome, TranslationJobs, target_key

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
           pinned_connection_id: int | None = None, session=None,
           clock: Callable[[], datetime] = datetime.now) -> Decision:
    """Make or join the job for this request. Raises ``TranslationJobError`` with the reason when refused."""
    from .jobs import TranslationJobError

    if session is None:
        from app.database import database

        session = database
    jobs = TranslationJobs(session, clock=clock)
    target = target_key(to_lang, forced, hi)
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

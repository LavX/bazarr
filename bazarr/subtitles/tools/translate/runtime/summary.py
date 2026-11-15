# coding=utf-8
"""What the Translation page's status band and Waiting list read: this month's spend and the jobs that have not started.

Both are read-only views over tables and counters that exist already. The month's figure is the usage
ledger's, the same one a cap is checked against: settled cost plus every live reservation, in whole
micro-dollars, handed out as dollars. The waiting list is the translation job table's queued and
awaiting-retry rows, each named from the first source that knows its media: the job's history row, the job
waiting on the in-memory queue, or, failing both, the file and target of the job itself.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable, Mapping

from sqlalchemy import func, select

from app.database import TableTranslatorJob

from .history import HistoryStore
from .jobs import AWAITING_RETRY, QUEUED
from .limits import TranslationLimits, micros_to_dollars, translation_limit as configured_translation_limit
from .usage import UsageLedger

logger = logging.getLogger(__name__)

# The page shows at most this many waiting jobs and says how many more there are.
QUEUE_PAGE = 50

QueueCounts = Callable[[], Mapping[str, int]]
QueueJob = Callable[[int], Mapping[str, Any] | None]
TitleLookup = Callable[[Any, Any, Any, Any, Any], str]


def _queue_counts() -> Mapping[str, int]:
    from app.jobs_queue import jobs_queue

    return jobs_queue.translation_queue_counts()


def _queue_job(queue_job_id: int) -> Mapping[str, Any] | None:
    """The job waiting on the in-memory queue under this id, as its snapshot, or None."""
    from app.jobs_queue import jobs_queue

    if not queue_job_id:
        return None
    found = jobs_queue.list_jobs_from_queue(job_id=queue_job_id, status="pending")
    return found[0] if found else None


def _title(media_type, radarr_id, series_id, episode_id, arr_instance_id) -> str:
    from subtitles.tools.translate.core.translator_utils import get_title

    return get_title(media_type, radarr_id, series_id, episode_id, arr_instance_id)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class TranslationSummary:
    def __init__(self, session, *, limits: TranslationLimits | None = None, queue_counts: QueueCounts | None = None,
                 queue_job: QueueJob | None = None, title_lookup: TitleLookup | None = None,
                 translation_limit: Callable[[], int | None] | None = None,
                 clock: Callable[[], datetime] = datetime.now):
        self._session = session
        self._limits = limits or TranslationLimits()
        self._queue_counts = queue_counts or _queue_counts
        self._queue_job = queue_job or _queue_job
        self._title = title_lookup or _title
        self._translation_limit = translation_limit or configured_translation_limit
        self._clock = clock

    # ------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        stored = self._limits.read()
        month, committed = UsageLedger(self._session, clock=self._clock).month_usage()
        counts = self._queue_counts()
        waiting_for_retry = int(self._session.execute(
            select(func.count()).select_from(TableTranslatorJob)
            .where(TableTranslatorJob.state == AWAITING_RETRY)).scalar() or 0)
        return {
            "month": {"key": month, "spent": micros_to_dollars(committed),
                      "cap": micros_to_dollars(stored.month_cap_micros)},
            "job_cap": micros_to_dollars(stored.job_cap_micros),
            # A job awaiting its retry is in the job table and not on the in-memory queue, so the two add up.
            "queue": {"running": _count(counts.get("running")),
                      "pending": _count(counts.get("pending")) + waiting_for_retry,
                      "limit": self._translation_limit()},
            # The revision a limits save may claim. The page does not need it; a client that wants to refuse a
            # stale write sends it back as expected_revision.
            "limits_revision": stored.revision,
        }

    # --------------------------------------------------------------- queue
    def queue(self, limit: int = QUEUE_PAGE) -> dict[str, Any]:
        """The jobs that have not started, in the order they will run, and how many there are in all.

        Queued jobs come first, oldest first, then the jobs awaiting a retry by the time they are due. A
        retry goes to the back of the queue when its time comes, so it runs after what is waiting now.
        """
        waiting = TableTranslatorJob.state.in_((QUEUED, AWAITING_RETRY))
        total = int(self._session.execute(select(func.count()).select_from(TableTranslatorJob)
                                          .where(waiting)).scalar() or 0)
        rows = self._session.execute(
            select(TableTranslatorJob).where(waiting)
            .order_by((TableTranslatorJob.state == AWAITING_RETRY).asc(),
                      func.coalesce(TableTranslatorJob.retry_at, TableTranslatorJob.created_at).asc(),
                      TableTranslatorJob.id.asc())
            .limit(max(0, limit)).execution_options(populate_existing=True)).scalars().all()
        history = HistoryStore(self._session)
        return {"data": [self._queued(row, history) for row in rows], "total": total}

    def _queued(self, row, history: HistoryStore) -> dict[str, Any]:
        known = history.get_by_job(str(row.id)) or {}
        media = {key: known.get(key) for key in ("media_type", "media_id", "media_title",
                                                  "source_language", "target_language")}
        if not all(media.get(key) for key in ("media_title", "source_language", "target_language")):
            for key, value in self._from_queue_job(row).items():
                if not media.get(key):
                    media[key] = value
        target = row.target.split(":")[0] if isinstance(row.target, str) else None
        return {
            "job_id": str(row.id),
            "media_type": media.get("media_type") or None,
            "media_id": media.get("media_id"),
            "media_title": media.get("media_title") or os.path.basename(row.source_path or "") or None,
            "source_language": media.get("source_language") or None,
            "target_language": media.get("target_language") or target or None,
            "engine_instance_id": known.get("engine_instance_id") or row.engine_instance_id,
            "status": row.state,
            "queued_at": _iso(row.created_at),
        }

    def _from_queue_job(self, row) -> dict[str, Any]:
        """What the job waiting on the in-memory queue says about its media. Nothing, when it is not there."""
        try:
            job = self._queue_job(row.queue_job_id) if row.queue_job_id else None
        except Exception:
            logger.debug("Could not read the in-memory queue for a waiting translation", exc_info=True)
            return {}
        if not job:
            return {}
        kwargs = job.get("kwargs") or {}
        media_type = kwargs.get("media_type")
        media_id = kwargs.get("radarr_id") if media_type == "movie" else kwargs.get("sonarr_episode_id")
        title = ""
        try:
            title = self._title(media_type, kwargs.get("radarr_id"), kwargs.get("sonarr_series_id"),
                                kwargs.get("sonarr_episode_id"), kwargs.get("arr_instance_id"))
        except Exception:
            logger.debug("Could not read the title of a waiting translation", exc_info=True)
        return {"media_type": media_type, "media_id": media_id, "media_title": title or None,
                "source_language": kwargs.get("from_lang"), "target_language": kwargs.get("to_lang")}

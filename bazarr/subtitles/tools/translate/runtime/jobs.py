# coding=utf-8
"""The translation job type: one job per source file and target, its checkpoints and its deadlines.

Persistence and rules only. Nothing here picks an engine or talks to a worker, and nothing
enqueues work: the dispatcher decides that and calls in. The jobs queue itself stays in memory;
this module keeps the durable record beside it, so a restart can find what was interrupted and
resume it from the last finished batch.

Rules kept here:

* At most one job per source file and target is queued, running or awaiting a retry (the
  database enforces it). A second request joins it.
* A manual pick replaces a job that has not started (queued, or awaiting a retry): the old one
  ends superseded and its checkpoints go, while its record stays so usage is kept. A manual pick
  is refused, with the reason, while the job runs.
* A checkpoint is keyed by the source path, a digest of the normalized source lines, the target,
  the engine instance, that instance's settings revision and the engine release digest. The
  connection that served a batch is not part of the key, so a failover resumes where it stopped.
* Checkpoints are deleted when the job completes, is cancelled, fails without a retry, or is
  superseded, and seven days after a failure that awaits a retry.
* A running job has an overall deadline, an inactivity deadline and a grace period to stop after
  a cancel (``DeadlineWatch``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Protocol, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.database import TableTranslatorCheckpoint, TableTranslatorJob
from .transaction import write_transaction

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
AWAITING_RETRY = "awaiting_retry"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
SUPERSEDED = "superseded"
ACTIVE_STATES = (QUEUED, RUNNING, AWAITING_RETRY)

CHECKPOINT_RETENTION = timedelta(days=7)
OVERALL_DEADLINE_SECONDS = 12 * 60 * 60
INACTIVITY_DEADLINE_SECONDS = 10 * 60
CANCEL_GRACE_SECONDS = 60

# Error classes this module records itself.
DEADLINE_EXCEEDED = "deadline_exceeded"
INACTIVITY_TIMEOUT = "inactivity_timeout"
INTERRUPTED = "interrupted"

RUNNING_REFUSAL = "This subtitle is already being translated. Wait for it to finish, or stop it first."
RACE_REFUSAL = "The translation queue changed while this request was made. Try again."
MAX_ERROR_LENGTH = 500
_REQUEST_ATTEMPTS = 3



def _write(session):
    """One write transaction that commits or rolls back as a whole, on SQLite and PostgreSQL.

    The application runs both in AUTOCOMMIT, so a transaction has to be opened by hand, which
    ``write_transaction`` does: on SQLite it begins with BEGIN IMMEDIATE, so a step that reads first
    and writes later never meets "database is locked" from a stale snapshot. Called inside another
    write transaction it opens a savepoint and leaves the commit to the caller.
    """
    return write_transaction(session)


# What a job is refused or stopped for when money is the reason: the month's, the job's own cap, and the budget that ran
# out while it ran. A cap is the person's to raise, so none of these holds a repeated pick.
BUDGET_REFUSALS = frozenset({"month_cap", "job_cap", "budget_exhausted"})


def retry_hold(row, pin, now) -> str | None:
    """Why this pick has to wait, or None when it may go.

    A job that waits to be retried with the same pick is held until its stored time: that time is on the job, so it
    holds across a restart and for an engine, which has no in-memory window. Another pick is a different question,
    and a cost cap is the person's to raise (``BUDGET_REFUSALS``), so neither is held.
    """
    if row is None or pin is None or row.state != AWAITING_RETRY or row.retry_at is None or row.retry_at <= now \
            or row.pinned_connection_id != pin or row.error_class in BUDGET_REFUSALS:
        return None
    when = (f"{row.retry_at:%H:%M}" if row.retry_at.date() == now.date() else f"{row.retry_at:%Y-%m-%d %H:%M}")
    return (f"This translation waits to be retried at {when} with what you picked. Pick another back end or "
            "engine, or try again then.")


class TranslationJobError(ValueError):
    """A request about a job that cannot be honoured, with a reason a person can read."""


# --------------------------------------------------------------------------- keys

def normalize_source_path(path: Any) -> str:
    text = os.fspath(path) if path is not None else ""
    text = str(text).strip()
    if not text:
        raise TranslationJobError("a translation needs a source file")
    return os.path.normpath(text)


def target_key(language: str, forced: bool = False, hi: bool = False) -> str:
    """The target a job writes: its language, with the flags that name a different file."""
    language = str(language or "").strip()
    if not language:
        raise TranslationJobError("a translation needs a target language")
    return language + (":forced" if forced else "") + (":hi" if hi else "")


def line_digest(lines: Sequence[str]) -> str:
    """A digest of the source lines that ignores line-ending style, a leading BOM and trailing spaces.

    Two reads of the same subtitle give the same digest; any edit to a line, or to how many there
    are, gives another.
    """
    digest = hashlib.sha256()
    digest.update(str(len(lines)).encode("ascii") + b"\n")
    for index, line in enumerate(lines):
        text = str(line).replace("\r\n", "\n").replace("\r", "\n")
        if index == 0:
            text = text.lstrip("﻿")
        text = "\n".join(part.rstrip() for part in text.split("\n"))
        encoded = text.encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii") + b":" + encoded + b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class CheckpointKey:
    source_path: str
    source_digest: str
    target: str
    engine_instance_id: int
    settings_revision: int
    release_digest: str = ""


# --------------------------------------------------------------------------- the queue port

class QueuePort(Protocol):
    def remove_pending(self, queue_job_id: int) -> bool: ...

    def status(self, queue_job_id: int) -> str | None: ...


class JobsQueuePort:
    """The in-memory jobs queue, reached only when a job that was queued has to be pulled back."""

    def remove_pending(self, queue_job_id: int) -> bool:
        from app.jobs_queue import jobs_queue

        return bool(jobs_queue.remove_job_from_pending_queue(job_id=queue_job_id))

    def status(self, queue_job_id: int) -> str | None:
        from app.jobs_queue import jobs_queue

        found = jobs_queue.list_jobs_from_queue(job_id=queue_job_id)
        return found[0].get("status") if found else None


# --------------------------------------------------------------------------- checkpoints

class CheckpointStore:
    """Finished batches of a translation, kept so a restart or a retry does not pay for them twice."""

    def __init__(self, session, *, clock: Callable[[], datetime] = datetime.now):
        self._session = session
        self._clock = clock

    def _row(self, key: CheckpointKey):
        return self._session.execute(select(TableTranslatorCheckpoint).where(
            TableTranslatorCheckpoint.source_path == key.source_path,
            TableTranslatorCheckpoint.source_digest == key.source_digest,
            TableTranslatorCheckpoint.target == key.target,
            TableTranslatorCheckpoint.engine_instance_id == key.engine_instance_id,
            TableTranslatorCheckpoint.settings_revision == key.settings_revision,
            TableTranslatorCheckpoint.release_digest == key.release_digest)
            .execution_options(populate_existing=True)).scalar_one_or_none()

    def load(self, key: CheckpointKey) -> dict[int, list[str]]:
        row = self._row(key)
        if row is None:
            return {}
        try:
            stored = json.loads(row.batches_json or "{}")
        except ValueError:
            return {}
        if not isinstance(stored, dict):
            return {}
        return {int(index): [str(line) for line in lines] for index, lines in stored.items()
                if str(index).isdigit() and isinstance(lines, list)}

    def save_batch(self, key: CheckpointKey, index: int, lines: Sequence[str]) -> None:
        now = self._clock()
        with _write(self._session):
            row = self._row(key)
            if row is None:
                self._session.add(TableTranslatorCheckpoint(
                    source_path=key.source_path, source_digest=key.source_digest, target=key.target,
                    engine_instance_id=key.engine_instance_id, settings_revision=key.settings_revision,
                    release_digest=key.release_digest, batches_json=json.dumps({str(int(index)): list(lines)}),
                    created_at=now, updated_at=now))
                return
            try:
                batches = json.loads(row.batches_json or "{}")
            except ValueError:
                batches = {}
            if not isinstance(batches, dict):
                batches = {}
            batches[str(int(index))] = list(lines)
            row.batches_json = json.dumps(batches)
            row.updated_at = now

    @staticmethod
    def _delete_source_stmt(source_path: str, target: str):
        return delete(TableTranslatorCheckpoint).where(
            TableTranslatorCheckpoint.source_path == source_path, TableTranslatorCheckpoint.target == target)

    @staticmethod
    def _prune_stmt(key: CheckpointKey):
        return delete(TableTranslatorCheckpoint).where(
            TableTranslatorCheckpoint.source_path == key.source_path,
            TableTranslatorCheckpoint.target == key.target,
            (TableTranslatorCheckpoint.source_digest != key.source_digest)
            | ((TableTranslatorCheckpoint.engine_instance_id == key.engine_instance_id)
               & ((TableTranslatorCheckpoint.settings_revision != key.settings_revision)
                  | (TableTranslatorCheckpoint.release_digest != key.release_digest))))

    @staticmethod
    def _expiry_stmt(source_path: str, target: str, at: datetime | None):
        return update(TableTranslatorCheckpoint).where(
            TableTranslatorCheckpoint.source_path == source_path,
            TableTranslatorCheckpoint.target == target).values(expires_at=at)

    def delete_for_source(self, source_path: str, target: str) -> int:
        """Every checkpoint of this source and target, whatever engine wrote it."""
        with _write(self._session):
            result = self._session.execute(self._delete_source_stmt(source_path, target))
        self._session.expire_all()
        return result.rowcount or 0

    def prune_stale(self, key: CheckpointKey) -> int:
        """Drop what the key makes useless: another reading of an edited source, or an older revision or release.

        Checkpoints of other engine instances stay: a fallback engine's partial work is not stale.
        """
        with _write(self._session):
            result = self._session.execute(self._prune_stmt(key))
        self._session.expire_all()
        return result.rowcount or 0

    def expire_for_source(self, source_path: str, target: str, at: datetime) -> None:
        with _write(self._session):
            self._session.execute(self._expiry_stmt(source_path, target, at))
        self._session.expire_all()

    def keep_for_source(self, source_path: str, target: str) -> None:
        """A retry started: the checkpoints no longer count down."""
        with _write(self._session):
            self._session.execute(self._expiry_stmt(source_path, target, None))
        self._session.expire_all()

    def sweep(self, retention: timedelta = CHECKPOINT_RETENTION) -> int:
        """Delete the checkpoints that ran out, and any whose job is gone and that nobody touched for the retention."""
        now = self._clock()
        removed = 0
        with _write(self._session):
            removed += self._session.execute(delete(TableTranslatorCheckpoint).where(
                TableTranslatorCheckpoint.expires_at.is_not(None),
                TableTranslatorCheckpoint.expires_at <= now)).rowcount or 0
            active = select(TableTranslatorJob.id).where(
                TableTranslatorJob.source_path == TableTranslatorCheckpoint.source_path,
                TableTranslatorJob.target == TableTranslatorCheckpoint.target,
                TableTranslatorJob.state.in_(ACTIVE_STATES)).exists()
            removed += self._session.execute(delete(TableTranslatorCheckpoint).where(
                TableTranslatorCheckpoint.updated_at <= now - retention, ~active)).rowcount or 0
        self._session.expire_all()
        return removed


# --------------------------------------------------------------------------- jobs

class Outcome(str, Enum):
    CREATED = "created"
    JOINED = "joined"
    REPLACED = "replaced"
    REFUSED = "refused"


@dataclass(frozen=True)
class RequestResult:
    outcome: Outcome
    job_id: int | None = None
    reason: str = ""
    replaced_job_id: int | None = None


class _Raced(Exception):
    """The job changed between reading it and acting on it."""


def _scrubbed(message: str | None) -> str | None:
    if not message:
        return None
    try:
        from secret_store.scrub import scrub_leaf

        # The message stands alone in its row, so one equal to a short credential is masked whole.
        message = scrub_leaf(str(message))
    except Exception:  # a scrubber that fails must not lose the job's outcome
        message = str(message)
    return message[:MAX_ERROR_LENGTH]


class TranslationJobs:
    def __init__(self, session, *, clock: Callable[[], datetime] = datetime.now,
                 queue: QueuePort | None = None, retention: timedelta = CHECKPOINT_RETENTION):
        self._session = session
        self._clock = clock
        self._queue = queue if queue is not None else JobsQueuePort()
        self._retention = retention
        self.checkpoints = CheckpointStore(session, clock=clock)

    # ------------------------------------------------------------------ reads
    def get(self, job_id: int):
        # Read from the database each time: another session may have moved the job on since this one saw it.
        return self._session.execute(select(TableTranslatorJob).where(TableTranslatorJob.id == job_id)
                                     .execution_options(populate_existing=True)).scalar_one_or_none()

    def active_for(self, source_path: str, target: str):
        return self._session.execute(select(TableTranslatorJob).where(
            TableTranslatorJob.source_path == source_path, TableTranslatorJob.target == target,
            TableTranslatorJob.state.in_(ACTIVE_STATES)).execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def due_retries(self) -> list[int]:
        """Jobs awaiting a retry whose time has come, oldest first."""
        now = self._clock()
        rows = self._session.execute(select(TableTranslatorJob).where(
            TableTranslatorJob.state == AWAITING_RETRY,
            (TableTranslatorJob.retry_at.is_(None)) | (TableTranslatorJob.retry_at <= now))
            .order_by(TableTranslatorJob.id).execution_options(populate_existing=True)).scalars().all()
        return [row.id for row in rows]

    # --------------------------------------------------------------- requests
    def request(self, source_path: Any, target: str, *, manual: bool = False,
                pinned_connection_id: int | None = None) -> RequestResult:
        """Ask for a translation. Joins the active job, replaces it (a manual pick), or creates one."""
        path = normalize_source_path(source_path)
        if not str(target or "").strip():
            raise TranslationJobError("a translation needs a target language")
        for _ in range(_REQUEST_ATTEMPTS):
            active = self.active_for(path, target)
            try:
                if active is None:
                    return RequestResult(Outcome.CREATED,
                                         self._create(path, target, manual, pinned_connection_id).id)
                if not manual:
                    return RequestResult(Outcome.JOINED, active.id)
                if active.state == RUNNING:
                    return RequestResult(Outcome.REFUSED, active.id, RUNNING_REFUSAL)
                if not self._pull_back(active):
                    return RequestResult(Outcome.REFUSED, active.id, RUNNING_REFUSAL)
                return self._replace(active, path, target, pinned_connection_id)
            except (IntegrityError, _Raced):
                self._session.expire_all()
        return RequestResult(Outcome.REFUSED, None, RACE_REFUSAL)

    def _create(self, path: str, target: str, manual: bool, pinned_connection_id: int | None):
        now = self._clock()
        job = TableTranslatorJob(source_path=path, target=target, state=QUEUED, manual=1 if manual else 0,
                                 pinned_connection_id=pinned_connection_id, created_at=now, updated_at=now)
        with _write(self._session):
            self._session.add(job)
            self._session.flush()
        return job

    def _pull_back(self, active) -> bool:
        """Take a job that has not started off the queue. False when it turned out to be running."""
        queue_job_id = active.queue_job_id
        if active.state != QUEUED or queue_job_id is None:
            return True
        if self._queue.remove_pending(queue_job_id):
            return True
        # It was not pending any more: running, or already gone from the queue (a stale record).
        return self._queue.status(queue_job_id) != "running"

    def _replace(self, active, path: str, target: str, pinned_connection_id: int | None) -> RequestResult:
        now = self._clock()
        replaced_id = active.id
        with _write(self._session):
            changed = self._session.execute(update(TableTranslatorJob).where(
                TableTranslatorJob.id == replaced_id, TableTranslatorJob.state == active.state)
                .values(state=SUPERSEDED, queue_job_id=None, retry_at=None, updated_at=now,
                        finished_at=now)).rowcount
            if changed != 1:
                raise _Raced()
            self._session.execute(delete(TableTranslatorCheckpoint).where(
                TableTranslatorCheckpoint.source_path == path, TableTranslatorCheckpoint.target == target))
            job = TableTranslatorJob(source_path=path, target=target, state=QUEUED, manual=1,
                                     pinned_connection_id=pinned_connection_id, created_at=now, updated_at=now)
            self._session.add(job)
            self._session.flush()
            new_id = job.id
        self._session.expire_all()
        return RequestResult(Outcome.REPLACED, new_id, replaced_job_id=replaced_id)

    # ------------------------------------------------------------- life cycle
    def clear_pin(self, job_id: int) -> bool:
        """The job is no longer a picked one: a request with no pick took it over, or the request that picked it
        is gone. Its checkpoints stay, since the connection is not part of their key."""
        return self._transition(job_id, (QUEUED, AWAITING_RETRY), None, manual=0, pinned_connection_id=None)

    def attach_queue_job(self, job_id: int, queue_job_id: int) -> bool:
        return self._transition(job_id, ACTIVE_STATES, None, queue_job_id=queue_job_id)

    def requeue(self, job_id: int, queue_job_id: int | None = None) -> bool:
        """A job awaiting its retry goes back on the queue. Its checkpoints stop counting down."""
        job = self.get(job_id)
        if job is None:
            return False
        return self._transition(job_id, (AWAITING_RETRY,), QUEUED, queue_job_id=queue_job_id, retry_at=None,
                                _with=self.checkpoints._expiry_stmt(job.source_path, job.target, None))

    def start(self, job_id: int, *, source_digest: str, engine_instance_id: int, settings_revision: int,
              release_digest: str = "") -> bool:
        """The job runs now, with the engine the dispatcher chose.

        Drops checkpoints the new key makes useless (an edited source, an older revision). False when
        the job is no longer queued, because a manual pick replaced it or it was cancelled: the
        caller must not translate.
        """
        job = self.get(job_id)
        if job is None:
            return False
        now = self._clock()
        key = CheckpointKey(job.source_path, source_digest, job.target, engine_instance_id, settings_revision,
                            release_digest)
        return self._transition(job_id, (QUEUED,), RUNNING, source_digest=source_digest,
                                engine_instance_id=engine_instance_id, settings_revision=settings_revision,
                                release_digest=release_digest, started_at=now,
                                _with=self.checkpoints._prune_stmt(key))

    def switch_engine(self, job_id: int, *, engine_instance_id: int, settings_revision: int,
                      release_digest: str = "") -> bool:
        """A running job moves to another engine (the fallback). False when it is no longer running.

        The key of its checkpoints changes with the engine instance, so the new engine starts from its own;
        what the key makes useless (an older revision or release of the same instance) is dropped.
        """
        job = self.get(job_id)
        if job is None or job.source_digest is None:
            return False
        key = CheckpointKey(job.source_path, job.source_digest, job.target, engine_instance_id, settings_revision,
                            release_digest)
        return self._transition(job_id, (RUNNING,), RUNNING, engine_instance_id=engine_instance_id,
                                settings_revision=settings_revision, release_digest=release_digest,
                                _with=self.checkpoints._prune_stmt(key))

    def complete(self, job_id: int) -> bool:
        return self._end(job_id, COMPLETED)

    def cancel(self, job_id: int, error_class: str | None = None, message: str | None = None) -> bool:
        """The job ends without a result. ``error_class`` says why when it was not a person's Stop."""
        return self._end(job_id, CANCELLED, error_class=error_class, message=message)

    def fail(self, job_id: int, error_class: str, message: str | None = None) -> bool:
        """A failure that will not be retried."""
        return self._end(job_id, FAILED, error_class=error_class, message=message)

    def await_retry(self, job_id: int, error_class: str, message: str | None = None, *,
                    retry_at: datetime | None = None) -> bool:
        """A failure that will be retried. The checkpoints stay for the retention period."""
        job = self.get(job_id)
        if job is None:
            return False
        expiry = self.checkpoints._expiry_stmt(job.source_path, job.target, self._clock() + self._retention)
        return self._transition(job_id, (RUNNING, QUEUED), AWAITING_RETRY, queue_job_id=None, retry_at=retry_at,
                                error_class=error_class, error_message=_scrubbed(message), _with=expiry)

    def recover_interrupted(self) -> list[int]:
        """After a restart: every job that was queued or running lost its place, so it awaits a retry.

        The jobs queue is in memory, so nothing it held survives. Checkpoints stay, so the retry
        resumes after the last finished batch.
        """
        rows = self._session.execute(select(TableTranslatorJob).where(
            TableTranslatorJob.state.in_((QUEUED, RUNNING))).order_by(TableTranslatorJob.id)
            .execution_options(populate_existing=True)).scalars().all()
        recovered = []
        now = self._clock()
        for row in list(rows):
            expiry = self.checkpoints._expiry_stmt(row.source_path, row.target, now + self._retention)
            if self._transition(row.id, (QUEUED, RUNNING), AWAITING_RETRY, queue_job_id=None, retry_at=now,
                                error_class=INTERRUPTED, _with=expiry):
                recovered.append(row.id)
        return recovered

    def sweep(self) -> int:
        return self.checkpoints.sweep(self._retention)

    # -------------------------------------------------------------- checkpoints
    def key_of(self, job_id: int) -> CheckpointKey | None:
        return self._key_of_row(self.get(job_id))

    @staticmethod
    def _key_of_row(job) -> CheckpointKey | None:
        if (job is None or job.state != RUNNING or job.source_digest is None
                or job.engine_instance_id is None or job.settings_revision is None):
            return None
        return CheckpointKey(job.source_path, job.source_digest, job.target, job.engine_instance_id,
                             job.settings_revision, job.release_digest or "")

    def finished_batches(self, job_id: int) -> dict[int, list[str]]:
        key = self.key_of(job_id)
        return self.checkpoints.load(key) if key is not None else {}

    def save_batch(self, job_id: int, index: int, lines: Sequence[str]) -> bool:
        """Record a finished batch. False, and nothing kept, when the job is not running any more.

        The check and the write are one transaction, and on PostgreSQL the job row is locked for it. A
        cancel that lands between a check and a write would otherwise find nothing to delete and let the
        write bring its checkpoints back.
        """
        with _write(self._session):
            job = self._session.execute(
                select(TableTranslatorJob).where(TableTranslatorJob.id == job_id).with_for_update()
                .execution_options(populate_existing=True)).scalar_one_or_none()
            key = self._key_of_row(job)
            if key is None:
                return False
            self.checkpoints.save_batch(key, index, lines)
            return True

    # ---------------------------------------------------------------- internals
    def _transition(self, job_id: int, from_states: Sequence[str], to_state: str | None, *, _with=None,
                    **values) -> bool:
        """Change a job only while it is in one of ``from_states``. False when it was not.

        ``_with`` is a checkpoint statement that goes in the same transaction, run only when the job
        changed, so a crash cannot leave the job ended and its checkpoints behind (or the reverse).
        """
        changes = dict(values)
        changes["updated_at"] = self._clock()
        if to_state is not None:
            changes["state"] = to_state
        with _write(self._session):
            changed = self._session.execute(update(TableTranslatorJob).where(
                TableTranslatorJob.id == job_id, TableTranslatorJob.state.in_(from_states))
                .values(**changes)).rowcount
            if changed == 1 and _with is not None:
                self._session.execute(_with)
        self._session.expire_all()
        return changed == 1

    def _end(self, job_id: int, state: str, *, error_class: str | None = None,
             message: str | None = None) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        now = self._clock()
        return self._transition(job_id, ACTIVE_STATES, state, queue_job_id=None, retry_at=None,
                                error_class=error_class, error_message=_scrubbed(message), finished_at=now,
                                _with=self.checkpoints._delete_source_stmt(job.source_path, job.target))


# --------------------------------------------------------------------------- deadlines

class DeadlineAction(str, Enum):
    NONE = "none"
    CANCEL = "cancel"
    KILL = "kill"


@dataclass(frozen=True)
class Verdict:
    action: DeadlineAction
    reason: str = ""


class DeadlineWatch:
    """The clock of one running job: an overall deadline, inactivity, and a grace period after a cancel.

    The caller starts it with the job, calls ``activity`` for every heartbeat or progress frame, and
    asks ``check`` now and then. CANCEL means ask the job to stop; KILL means the grace period ran
    out and the worker must be stopped. The clock is injected, so tests run in no time.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 overall: float = OVERALL_DEADLINE_SECONDS, inactivity: float = INACTIVITY_DEADLINE_SECONDS,
                 grace: float = CANCEL_GRACE_SECONDS):
        self._clock = clock
        self._overall = overall
        self._inactivity = inactivity
        self._grace = grace
        now = clock()
        self._started = now
        self._last_activity = now
        self._cancel_at: float | None = None
        self._cancel_reason = ""

    def activity(self) -> None:
        self._last_activity = self._clock()

    def cancel_requested(self, reason: str = "cancelled") -> None:
        """A cancel was sent for another reason (a person pressed Stop). The grace period starts now."""
        if self._cancel_at is None:
            self._cancel_at = self._clock()
            self._cancel_reason = reason

    def deadline_passed(self) -> bool:
        """The overall deadline is behind us, whether or not ``check`` has said so yet."""
        return self._clock() - self._started >= self._overall

    @property
    def cancelling(self) -> bool:
        return self._cancel_at is not None

    def check(self) -> Verdict:
        now = self._clock()
        if self._cancel_at is not None:
            if now - self._cancel_at >= self._grace:
                return Verdict(DeadlineAction.KILL, self._cancel_reason)
            return Verdict(DeadlineAction.NONE)
        if now - self._started >= self._overall:
            self._cancel_at, self._cancel_reason = now, DEADLINE_EXCEEDED
            return Verdict(DeadlineAction.CANCEL, DEADLINE_EXCEEDED)
        if now - self._last_activity >= self._inactivity:
            self._cancel_at, self._cancel_reason = now, INACTIVITY_TIMEOUT
            return Verdict(DeadlineAction.CANCEL, INACTIVITY_TIMEOUT)
        return Verdict(DeadlineAction.NONE)

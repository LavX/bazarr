# coding=utf-8
"""Dispatch: one translation job, from the engine choice to the lines that come back.

``Dispatcher.run`` takes everything a translation needs (the source lines, both languages, the media context,
who is asking) and returns the translated lines, or the path a legacy engine published, or raises with the
reason. It owns what happens between the jobs queue and the worker, and nothing that touches a file:

* the gate is asked again, because a job can wait in the queue while the gate closes. A closed gate ends the
  job without an error and the caller runs the normal provider search for it instead;
* the engine is the selected one, then, once, the fallback engine when the first declines. A job a person
  pinned to one back end uses that back end only: its result or its error, no failover and no fallback;
* an engine with back-end connections is tried connection by connection in list order, skipping the unusable
  ones and those inside a retry time a plugin reported. Only rate limited, quota exhausted, auth required and
  dependency unavailable move on to the next connection. A content error, a refused connect (sandbox blocked)
  or a failed output check never does. When every usable connection has failed over the job fails and is
  retried later; it is never a decline, so it does not reach the fallback engine;
* money is reserved before it is spent: the job's budget at dispatch, each paid call's worst case before it is
  sent (``host_calls``), and released when the job ends. With the month spent, a connection that costs nothing
  still runs and a priced one is skipped;
* checkpoints, history rows and the job's own record are kept as the attempts go, so a restart or a retry
  resumes after the last finished batch and every connection that was tried is on record;
* the engine's answer is checked (one line per input line, bounded length, allowed tags only) and right-to-left
  marks are added by Bazarr+ for every engine;
* an engine served by code in Bazarr+ itself (Lingarr) is not sent to a worker: the caller's ``legacy``
  runner runs the in-core service, with no connections, no cost and history under the plugin id ``lingarr``.

The worker is reached through ``WorkerPort`` (see ``workers``), so this module is the same against the fake
worker of the tests and a real plugin.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from . import failure as fail
from . import gate as gate_module
from . import legacy_engines
from . import wire
from .destinations import effective_destinations, schema_for_backend, schema_for_engine
from .failure import AttemptFailure
from .history import HistoryStore
from .host_calls import AttemptContext, stored_checkpoints
from .jobs import (AWAITING_RETRY, DEADLINE_EXCEEDED, INACTIVITY_TIMEOUT, QUEUED, DeadlineAction, DeadlineWatch,
                   TranslationJobs, line_digest)
from .output_checks import add_rtl_marks, check_line
from .request import LanguageSpec, UnsupportedLanguage, normalize_request_languages
from .store import CONNECTION, INSTANCE, Notice, TranslatorStore
from .usage import JOB_CAP_SPENT, MONTH_SPENT, RESERVED, WAIT, CostLimits, UsageLedger
from .workers import WorkerPort, bound_port

logger = logging.getLogger(__name__)

# How long a job whose month's money is all held by running jobs waits for one to settle before it gives up
# and is retried later.
BUDGET_WAIT_SECONDS = 30 * 60
# When the plugin gave no retry time, a job that failed over everywhere is tried again after this.
DEFAULT_RETRY_SECONDS = 15 * 60
POLL_SECONDS = 0.5

# A job that the old Translator page's engines served with no way to say why now says why.
HISTORY_STATUS_FOR_ERROR = {fail.CANCELLED: "cancelled"}


# ------------------------------------------------------------------------------------------ what comes in

@dataclass
class JobSpec:
    """One translation, as the caller describes it."""

    source_key: str
    target_key: str
    lines: Sequence[str]
    target_language: str
    source_language: str | None = None
    target_name: str | None = None
    source_name: str | None = None
    media: dict = field(default_factory=dict)
    # What the history row says about the media: media_type, media_id, media_title, arr_instance_id.
    history_media: dict = field(default_factory=dict)
    pinned_connection_id: int | None = None
    # Raises JobCancelled when the person stopped the job.
    check_cancel: Callable[[], None] = lambda: None
    # progress(done, total, message): shown on the job in the queue.
    progress: Callable[[int, int, str], None] | None = None
    # Runs the in-core service for an engine that has no worker: ``legacy(engine, instance) -> LegacyResult``.
    legacy: Callable[[Any, Any], "LegacyResult"] | None = None


@dataclass
class LegacyResult:
    """What an in-core engine hands back: the file it published, or the lines it translated."""

    published: Any = None
    lines: list[str] | None = None
    partial_error: str | None = None


class LegacyDeclined(Exception):
    """The in-core engine cannot take the job (its server is unreachable). The fallback engine may."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------------------------------- what goes out

@dataclass
class DispatchOutcome:
    kind: str  # "lines", "published" or "skipped"
    job_id: int | None = None
    lines: list[str] = field(default_factory=list)
    partial_error: str | None = None
    published: Any = None
    engine_instance_id: int | None = None
    plugin_id: str = ""
    engine_name: str = ""
    notices: list[Notice] = field(default_factory=list)
    # For lines the caller has still to write: the job is not over until it has. The caller ends it with
    # ``complete()`` once the subtitle is published, or ``publication_failed(error)`` when it could not be.
    complete: Callable[[], None] | None = field(default=None, repr=False)
    publication_failed: Callable[[BaseException], None] | None = field(default=None, repr=False)

    @property
    def partial(self) -> bool:
        return bool(self.partial_error)


class GateClosed(Exception):
    """The gate closed while the job waited. Nothing ran: the normal provider search takes over."""

    def __init__(self, reason: str, cause: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class Declined(Exception):
    """Every engine that was asked declined. The provider search result stays in place."""

    def __init__(self, reasons: Sequence[str]):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


class EngineDeclined(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _CheckFailed(Exception):
    """The cancel check itself raised something other than a Stop. The request has been cancelled."""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


class DispatchFailed(Exception):
    """The job failed. It has been recorded as awaiting a retry or as failed; this says which and why."""

    def __init__(self, error_class: str, message: str, *, retry_later: bool, retry_at: datetime | None = None):
        super().__init__(message or error_class)
        self.error_class = error_class
        self.message = message or error_class
        self.retry_later = retry_later
        self.retry_at = retry_at


# --------------------------------------------------------------------------------------------- the plugins

@dataclass(frozen=True)
class PluginInfo:
    plugin_id: str
    name: str
    version: str
    digest: str
    manifest: Any


def installed_info(plugin_id: str) -> PluginInfo | None:
    """The plugin's active release from the Provider Hub state. The release digest keys its checkpoints."""
    from .settings_service import installed_plugin

    plugin = installed_plugin(plugin_id)
    if plugin is None or not plugin.active:
        return None
    digest = str(plugin.manifest.get("bundle_sha256") or plugin.version or "") if hasattr(plugin.manifest, "get") else ""
    return PluginInfo(plugin.plugin_id, plugin.name, plugin.version, digest, plugin.manifest)


def configured_cost_limits() -> CostLimits:
    """The caps in force: the month and job caps (micro-dollars, none for no cap) and the translation limit."""
    from app.config import settings

    section = settings.translator

    def cap(name):
        value = getattr(section, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    limit = getattr(section, "openrouter_max_concurrent", 1)
    limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else 1
    return CostLimits(cap("month_cap_micros"), cap("job_cap_micros"), limit)


class RetryWindows:
    """Connections that reported a retry time, and until when they are skipped. In memory, shared by all jobs."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._until: dict[int, tuple[float, str]] = {}

    def note(self, connection_id: int, failure: AttemptFailure) -> None:
        if failure.retry_after:
            self._until[connection_id] = (self._monotonic() + failure.retry_after, failure.error_class)

    def waiting(self, connection_id: int) -> tuple[float, str] | None:
        found = self._until.get(connection_id)
        if found is None:
            return None
        remaining = found[0] - self._monotonic()
        if remaining <= 0:
            self._until.pop(connection_id, None)
            return None
        return remaining, found[1]

    def clear(self) -> None:
        self._until.clear()


# One store for the whole process: a dispatcher is made for each request, and a connection that asked to be left
# alone has to be left alone by all of them.
RETRY_WINDOWS = RetryWindows()


# ---------------------------------------------------------------------------------------------- the engine

@dataclass
class _Choice:
    instance: Any
    pinned: Any = None  # the connection a person picked, for a pinned job


@dataclass
class _Run:
    """The state of one ``run``: the job, its clock and its budget."""

    job_id: int
    ledger_id: str
    watch: DeadlineWatch
    budget: Any = None
    # True once this dispatcher reserved the job's budget: only that one gives it back.
    budget_reserved: bool = False
    stop_reason: str | None = None
    declines: list[str] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)


class Dispatcher:
    def __init__(self, session=None, *, store: TranslatorStore | None = None, workers: WorkerPort | None = None,
                 plugins: Callable[[str], PluginInfo | None] = installed_info,
                 limits: Callable[[], CostLimits] = configured_cost_limits,
                 clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, holds: gate_module.GateHolds | None = None,
                 windows: RetryWindows | None = None, poll: float = POLL_SECONDS,
                 budget_wait: float = BUDGET_WAIT_SECONDS, deadlines: dict | None = None):
        if session is None:
            from app.database import database

            session = database
        self.session = session
        self.store = store if store is not None else TranslatorStore(session)
        self._workers = workers
        self.plugins = plugins
        self.limits = limits
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.holds = holds or gate_module.HOLDS
        self.windows = windows if windows is not None else RETRY_WINDOWS
        self.poll = poll
        self.budget_wait = budget_wait
        self.deadlines = dict(deadlines or {})

    @property
    def workers(self) -> WorkerPort:
        return self._workers if self._workers is not None else bound_port()

    # ------------------------------------------------------------------------------------------ the entry
    def run(self, spec: JobSpec) -> DispatchOutcome:
        from app.jobs_queue import JobCancelled

        jobs = TranslationJobs(self.session, clock=self.clock)
        history = HistoryStore(self.session, clock=self.clock)
        ledger = UsageLedger(self.session, clock=self.clock)
        row = self._ensure_job(jobs, spec)
        if row is None:
            return DispatchOutcome("skipped")
        _maintain_when_due(self.session, self.clock, self.monotonic, self.limits)
        state = _Run(row.id, str(row.id), DeadlineWatch(clock=self.monotonic, **self.deadlines))
        try:
            chain, state.notices = self._plan(spec)
        except GateClosed as closed:
            self._end_gate_closed(jobs, history, spec, state, closed)
            raise
        digest = line_digest(spec.lines)
        try:
            return self._run_chain(spec, chain, state, jobs, history, ledger, digest)
        except JobCancelled:
            jobs.cancel(state.job_id)
            self._finish_history(history, state, "cancelled")
            raise
        except (DispatchFailed, Declined, GateClosed):
            raise  # already recorded
        except Exception as error:
            # A bug or a database fault: the job must not stay "running" for a worker that is no longer on it.
            logger.exception("A translation job failed unexpectedly")
            jobs.await_retry(state.job_id, fail.INTERNAL, type(error).__name__)
            self._finish_history(history, state, "awaiting_retry", fail.INTERNAL, type(error).__name__)
            raise
        finally:
            if state.budget_reserved:
                try:
                    ledger.release_job(state.ledger_id)
                except Exception:
                    logger.warning("A translation job could not give back its budget", exc_info=True)

    # ------------------------------------------------------------------------------------------ the job
    def _ensure_job(self, jobs: TranslationJobs, spec: JobSpec):
        """The job's record, ready to start. None when another job already runs it or it has ended."""
        row = jobs.active_for(spec.source_key, spec.target_key)
        if row is None:
            result = jobs.request(spec.source_key, spec.target_key, manual=spec.pinned_connection_id is not None,
                                  pinned_connection_id=spec.pinned_connection_id)
            if result.job_id is None:
                return None
            row = jobs.get(result.job_id)
        if row is not None and row.state == AWAITING_RETRY:
            if not jobs.requeue(row.id):
                return None
            row = jobs.get(row.id)
        if row is None or row.state != QUEUED:
            return None
        if spec.pinned_connection_id is None and row.pinned_connection_id is not None:
            # A manual pick recorded its connection on the job when it was made.
            spec.pinned_connection_id = row.pinned_connection_id
        return row

    def _plan(self, spec: JobSpec) -> tuple[list[_Choice], list[Notice]]:
        """The engines to try, in order. Raises ``GateClosed`` when the job cannot run at all."""
        store = self.store
        notices: list[Notice] = []
        if spec.pinned_connection_id is not None:
            connection = store.get_connection(spec.pinned_connection_id, fresh=True)
            instance = store.get_instance(connection.instance_id, fresh=True) if connection is not None else None
            if connection is None or instance is None:
                raise GateClosed("The back end that was picked no longer exists", gate_module.ENGINE_MISSING)
            state = gate_module.engine_state(store, instance, self.holds, self.workers)
            if not state.available:
                raise GateClosed(state.reason, state.cause)
            if not store.connection_usable(connection):
                raise GateClosed("The back end that was picked cannot be used right now",
                                 gate_module.NOT_CONFIGURED)
            return [_Choice(instance, connection)], notices
        result = gate_module.evaluate(store, self.holds, self.workers)
        if not result.available:
            raise GateClosed(result.reason, result.cause)
        engine_id, fallback_id = store.pointers()
        chain = [_Choice(store.get_instance(engine_id, fresh=True))]
        if fallback_id is not None and fallback_id != engine_id:
            fallback = store.get_instance(fallback_id, fresh=True)
            if fallback is None:
                notices.append(Notice("fallback_skipped", "The fallback engine no longer exists.", fallback_id))
            else:
                state = gate_module.engine_state(store, fallback, self.holds, self.workers)
                if state.available:
                    chain.append(_Choice(fallback))
                else:
                    notices.append(Notice("fallback_skipped",
                                          f"The fallback engine was skipped: {state.reason.lower()}.", fallback_id))
        for notice in notices:
            logger.info("%s", notice.message)
        return chain, notices

    def _run_chain(self, spec, chain, state, jobs, history, ledger, digest) -> DispatchOutcome:
        for position, choice in enumerate(chain):
            instance = choice.instance
            legacy = legacy_engines.legacy_engine_for(instance.plugin_id)
            info = None if legacy is not None else self.plugins(instance.plugin_id)
            release = "" if info is None else info.digest
            if position == 0:
                if not jobs.start(state.job_id, source_digest=digest, engine_instance_id=instance.id,
                                  settings_revision=instance.revision, release_digest=release):
                    return DispatchOutcome("skipped", job_id=state.job_id)
            elif not jobs.switch_engine(state.job_id, engine_instance_id=instance.id,
                                        settings_revision=instance.revision, release_digest=release):
                return DispatchOutcome("skipped", job_id=state.job_id)
            plugin_id = legacy.plugin_id if legacy is not None else instance.plugin_id
            history.open_job(
                state.ledger_id, **{key: value for key, value in spec.history_media.items() if value is not None},
                source_language=spec.source_language, target_language=spec.target_language, plugin_id=plugin_id,
                plugin_version=None if info is None else info.version, engine_instance_id=instance.id)
            try:
                if legacy is not None:
                    output = self._run_legacy(spec, legacy, instance)
                else:
                    output = self._run_plugin(spec, choice, info, state, jobs, history, ledger)
            except EngineDeclined as declined:
                state.declines.append(declined.reason)
                taken_over = chain[position + 1].instance.id if position + 1 < len(chain) else None
                history.note_declined(state.ledger_id, instance_id=chain[0].instance.id,
                                      reason="; ".join(state.declines), taken_over_by=taken_over)
                if taken_over is None:
                    jobs.fail(state.job_id, fail.DECLINED, "; ".join(state.declines))
                    history.finish_job(state.ledger_id, "declined", error_class=fail.DECLINED,
                                       error_detail="; ".join(state.declines))
                    raise Declined(state.declines) from None
                continue
            except AttemptFailure as failure:
                raise self._end_failed(jobs, history, state, failure) from None
            output.complete = lambda: self._complete(jobs, history, state, output)
            output.publication_failed = lambda error: self._publication_failed(jobs, history, state, error)
            if output.kind == "published":
                output.complete()  # the engine's own service has written the subtitle already
            output.job_id = state.job_id
            output.engine_instance_id = instance.id
            output.notices = list(state.notices)
            return output
        raise Declined(state.declines)  # pragma: no cover - the loop returns or raises

    # ------------------------------------------------------------------------------------------ endings
    def _complete(self, jobs, history, state, output) -> None:
        jobs.complete(state.job_id)
        self._finish_history(history, state, "partial" if output.partial_error else "completed")

    def _publication_failed(self, jobs, history, state, error: BaseException) -> None:
        """The subtitle could not be written. The batches stay for a retry, unless the write was refused because
        a subtitle arrived meanwhile or the person stopped the job: then nothing is left to retry."""
        from app.jobs_queue import JobCancelled
        from subtitles.tools.subsync_engines import SubtitleDestinationChanged, SubtitleSourceChanged

        if isinstance(error, JobCancelled):
            jobs.cancel(state.job_id)
            self._finish_history(history, state, "cancelled")
        elif isinstance(error, (SubtitleDestinationChanged, SubtitleSourceChanged)):
            jobs.fail(state.job_id, fail.DESTINATION_CHANGED, fail.MESSAGES[fail.DESTINATION_CHANGED])
            self._finish_history(history, state, "failed", fail.DESTINATION_CHANGED,
                                 fail.MESSAGES[fail.DESTINATION_CHANGED])
        else:
            message = fail.MESSAGES[fail.PUBLISH_FAILED]
            jobs.await_retry(state.job_id, fail.PUBLISH_FAILED, message, retry_at=self.clock() +
                             timedelta(seconds=DEFAULT_RETRY_SECONDS))
            self._finish_history(history, state, "awaiting_retry", fail.PUBLISH_FAILED, type(error).__name__)

    def _finish_history(self, history, state, status, error_class=None, detail=None):
        try:
            history.finish_job(state.ledger_id, status, error_class=error_class, error_detail=detail)
        except Exception:
            logger.warning("A translation history row could not be finished", exc_info=True)

    def _end_gate_closed(self, jobs, history, spec, state, closed: GateClosed) -> None:
        jobs.cancel(state.job_id, fail.GATE_CLOSED, closed.reason)
        history.open_job(state.ledger_id, **{key: value for key, value in spec.history_media.items()
                                             if value is not None},
                         source_language=spec.source_language, target_language=spec.target_language)
        history.finish_job(state.ledger_id, "declined", error_class=fail.GATE_CLOSED, error_detail=closed.reason)

    def _end_failed(self, jobs, history, state, failure: AttemptFailure) -> DispatchFailed:
        retry_later = failure.retries_later or failure.error_class == INACTIVITY_TIMEOUT
        message = failure.message or fail.MESSAGES.get(failure.error_class, failure.error_class)
        retry_at = None
        if retry_later:
            retry_at = self._retry_at(failure)
            jobs.await_retry(state.job_id, failure.error_class, message, retry_at=retry_at)
            self._finish_history(history, state, "awaiting_retry", failure.error_class, message)
        else:
            jobs.fail(state.job_id, failure.error_class, message)
            self._finish_history(history, state, "failed", failure.error_class, message)
        return DispatchFailed(failure.error_class, message, retry_later=retry_later, retry_at=retry_at)

    def _retry_at(self, failure: AttemptFailure) -> datetime:
        now = self.clock()
        if failure.error_class == fail.MONTH_CAP and failure.retry_after is None:
            return (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)).replace(day=1)
        return now + timedelta(seconds=failure.retry_after or DEFAULT_RETRY_SECONDS)

    # ------------------------------------------------------------------------------------------- legacy
    def _run_legacy(self, spec: JobSpec, legacy, instance) -> DispatchOutcome:
        if spec.legacy is None:
            raise AttemptFailure(fail.WORKER_UNAVAILABLE, "This caller cannot run an engine served by Bazarr+.")
        try:
            result = spec.legacy(legacy, instance)
        except LegacyDeclined as declined:
            raise EngineDeclined(declined.reason) from None
        if result.published is not None:
            return DispatchOutcome("published", published=result.published, partial_error=result.partial_error,
                                   plugin_id=legacy.plugin_id, engine_name=legacy.name)
        return DispatchOutcome("lines", lines=list(result.lines or []), partial_error=result.partial_error,
                               plugin_id=legacy.plugin_id, engine_name=legacy.name)

    # -------------------------------------------------------------------------------------------- plugin
    def _languages(self, spec: JobSpec) -> tuple[LanguageSpec, LanguageSpec]:
        try:
            return normalize_request_languages(spec.source_language, spec.target_language,
                                               source_name=spec.source_name, target_name=spec.target_name)
        except UnsupportedLanguage as error:
            raise AttemptFailure(fail.INVALID_CONFIG, str(error), reason="language") from None

    def _run_plugin(self, spec, choice: _Choice, info, state: _Run, jobs, history, ledger) -> DispatchOutcome:
        instance = choice.instance
        if info is None:
            raise AttemptFailure(fail.DEPENDENCY_UNAVAILABLE, "The translator plugin is not installed or is turned off.")
        source, target = self._languages(spec)
        candidates = self._candidates(choice, instance)
        budget = self._job_budget(spec, state, ledger)
        if budget.status in (MONTH_SPENT, JOB_CAP_SPENT):
            # Nothing can be reserved. A call whose worst-case charge is 0 still fits, so a free connection runs
            # and a priced one is skipped.
            free = [item for item in candidates if _is_free(item)]
            if not free:
                raise AttemptFailure(fail.MONTH_CAP if budget.status == MONTH_SPENT else fail.JOB_CAP,
                                     fail.MESSAGES[fail.MONTH_CAP if budget.status == MONTH_SPENT else fail.JOB_CAP])
            candidates = free
        last: AttemptFailure | None = None
        for index, connection in enumerate(candidates):
            try:
                result = self._attempt(spec, state, jobs, ledger, instance, info, connection, source, target)
            except AttemptFailure as failure:
                last = failure
                more = index < len(candidates) - 1
                failing_over = failure.fails_over and choice.pinned is None and more
                if connection is not None:
                    history.note_attempt(state.ledger_id, connection_id=connection.id,
                                         backend_type=connection.backend_type, model=connection.model,
                                         outcome="failed_over" if failing_over else "failed",
                                         error_class=failure.error_class)
                if failure.error_class == fail.DECLINED:
                    raise EngineDeclined(failure.message or failure.reason or "declined") from None
                if connection is not None and failure.fails_over:
                    # A retry time is kept whether or not another connection is left to try, so the next job
                    # does not start on the one that asked to be left alone.
                    self.windows.note(connection.id, failure)
                if failing_over:
                    logger.info("A translation attempt failed over to the next connection (%s)", failure.error_class)
                    continue
                raise
            if connection is not None:
                history.note_attempt(state.ledger_id, connection_id=connection.id,
                                     backend_type=connection.backend_type, model=result.model or connection.model)
            lines, partial_error = self._compose(spec, result, target)
            return DispatchOutcome("lines", lines=lines, partial_error=partial_error, plugin_id=info.plugin_id,
                                   engine_name=info.name)
        raise last if last is not None else AttemptFailure(fail.NO_USABLE_CONNECTION,
                                                            fail.MESSAGES[fail.NO_USABLE_CONNECTION])

    def _candidates(self, choice: _Choice, instance) -> list:
        """The connections to try, in order. A connectionless engine is one attempt with ``None``."""
        store = self.store
        if choice.pinned is not None:
            return [choice.pinned]
        connections = store.list_connections(instance.id)
        if not connections:
            return [None]
        usable = [item for item in connections if store.connection_usable(item)]
        if not usable:
            raise AttemptFailure(fail.NO_USABLE_CONNECTION, fail.MESSAGES[fail.NO_USABLE_CONNECTION])
        open_ones, last_waiting = [], None
        for item in usable:
            waiting = self.windows.waiting(item.id)
            if waiting is None:
                open_ones.append(item)
            else:
                last_waiting = waiting
        if not open_ones:
            remaining, error_class = last_waiting
            raise AttemptFailure(error_class, "Every back end of this engine asked to be left alone for now.",
                                 retry_after=remaining)
        return open_ones

    # ------------------------------------------------------------------------------------------- budget
    def _job_budget(self, spec: JobSpec, state: _Run, ledger: UsageLedger):
        """Reserve the job's budget once, waiting while the month's money is held by running jobs."""
        if state.budget is not None and state.budget.status == RESERVED:
            return state.budget
        waited = 0.0
        while True:
            budget = ledger.reserve_job_budget(state.ledger_id, self.limits())
            if budget.status != WAIT:
                break
            if waited >= self.budget_wait:
                # Money held by other jobs frees as they settle, not at the start of the next month.
                raise AttemptFailure(fail.MONTH_CAP, "The month's translation budget is held by other jobs.",
                                     retry_after=DEFAULT_RETRY_SECONDS)
            spec.check_cancel()
            self.sleep(self.poll)
            waited += self.poll
        state.budget = budget
        state.budget_reserved = budget.status == RESERVED
        return budget

    # ------------------------------------------------------------------------------------------- attempt
    def _secrets(self, instance, connection, info, serving) -> dict[str, str]:
        """The stored secrets this attempt may carry: the engine's and the connection's, each only to the
        destinations it was entered for. The connection's win a name they share."""
        store = self.store
        try:
            engine_schema = schema_for_engine(info.manifest)
            engine_destinations = effective_destinations(engine_schema, store.settings_of(instance))
            self._refuse_stale((INSTANCE, instance.id), engine_destinations)
            granted = dict(store.secrets_for_request((INSTANCE, instance.id), engine_destinations))
            if connection is not None:
                schema = schema_for_backend(serving.manifest, connection.backend_type)
                destinations = effective_destinations(schema, store.settings_of(connection))
                self._refuse_stale((CONNECTION, connection.id), destinations)
                granted.update(store.secrets_for_request((CONNECTION, connection.id), destinations))
        except AttemptFailure:
            raise
        except Exception:
            logger.warning("The manifest of translator %s could not be read for a request", info.plugin_id,
                           exc_info=True)
            raise AttemptFailure(fail.INVALID_CONFIG, "The translator's manifest could not be read.") from None
        return {name: value for name, value in granted.items() if isinstance(value, str) and value}

    @staticmethod
    def _remaining_budget(ledger, state: _Run) -> float | None:
        """What the job may still spend, in dollars: what its budget holds now, which is less than what was reserved
        once earlier attempts and engines have spent some. None when no cap applies."""
        hold = ledger.job_hold(state.ledger_id)
        return None if hold is None else hold / 1_000_000

    def _refuse_stale(self, owner, destinations) -> None:
        """A key entered for other destinations is never sent to these. The owner's own flag normally keeps such
        a connection out of the list; this is the check at the point of sending."""
        if self.store.has_stale_secrets(owner, destinations):
            raise AttemptFailure(fail.AUTH_REQUIRED, "The address changed after the key was saved, so the saved "
                                                     "key is not sent to the new one. Enter the key again.")

    def _attempt(self, spec, state: _Run, jobs, ledger, instance, info, connection, source, target):
        from app.jobs_queue import JobCancelled

        serving_id = info.plugin_id if connection is None or not connection.plugin_id else connection.plugin_id
        serving = info if serving_id == info.plugin_id else self.plugins(serving_id)
        if serving is None:
            raise AttemptFailure(fail.DEPENDENCY_UNAVAILABLE, "The plugin that serves this back end is not "
                                                              "installed or is turned off.")
        stored = jobs.finished_batches(state.job_id)
        context = AttemptContext(
            session=self.session, job_id=state.job_id, connection_id=None if connection is None else connection.id,
            backend_type="" if connection is None else connection.backend_type,
            model="" if connection is None else connection.model, limits=self.limits, clock=self.clock,
            next_batch=(max(stored) + 1) if stored else 0, activity=state.watch.activity)
        request = wire.build_request(
            job_id=state.ledger_id, source=source, target=target, lines=list(spec.lines), media=spec.media,
            engine_settings=self.store.settings_of(instance),
            connection=None if connection is None else wire.connection_wire(connection,
                                                                             self.store.settings_of(connection)),
            checkpoints=stored_checkpoints(stored),
            budget=self._remaining_budget(ledger, state))
        secrets = self._secrets(instance, connection, info, serving)
        state.watch.activity()

        def on_progress(message: dict) -> None:
            state.watch.activity()
            if spec.progress is None:
                return
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            done, total = data.get("done"), data.get("total")
            if isinstance(done, int) and isinstance(total, int) and not isinstance(done, bool):
                try:
                    spec.progress(done, total, str(message.get("message") or ""))
                except Exception:
                    logger.debug("A translation progress callback failed", exc_info=True)

        try:
            handle = self.workers.submit(serving_id, request, secrets, context=context, on_progress=on_progress)
        except JobCancelled:
            raise
        except BaseException as error:
            raise fail.classify(error) from None
        def stop(reason: str) -> None:
            # A stop of the host's own (the budget ran out): the grace period starts now, and the answer of an
            # attempt that was told to stop is not taken.
            state.stop_reason = state.stop_reason or reason
            state.watch.cancel_requested(reason)
            handle.cancel(reason)

        context.cancel = stop
        if context.cancel_pending:
            stop(context.cancel_pending)
        try:
            outcome = self._await(handle, spec, state, serving_id)
        except JobCancelled:
            raise
        except AttemptFailure:
            raise
        except _CheckFailed as failed:
            raise failed.original from None
        except BaseException as error:
            if state.stop_reason:
                raise self._stopped(state.stop_reason) from None
            refusal = context.refusal or ("job_budget" if context.over_budget else None)
            raise fail.classify(error, budget_refusal=refusal) from None
        return wire.read_result(outcome.result, getattr(outcome, "data", b""), len(spec.lines))

    def _stopped(self, reason: str) -> AttemptFailure:
        if reason == "budget_exhausted":
            return AttemptFailure(fail.BUDGET_EXHAUSTED, fail.MESSAGES[fail.BUDGET_EXHAUSTED])
        if reason == DEADLINE_EXCEEDED:
            return AttemptFailure(DEADLINE_EXCEEDED, "The translation ran past its overall time limit.")
        return AttemptFailure(INACTIVITY_TIMEOUT, "The translator stopped reporting progress.")

    def _await(self, handle, spec: JobSpec, state: _Run, plugin_id: str):
        """Wait for the request, watching the person's Stop and the job's deadlines."""
        from app.jobs_queue import JobCancelled

        by_person = False
        while True:
            try:
                outcome = handle.wait(self.poll)
            except TimeoutError:
                pass
            except BaseException:
                if by_person:
                    raise JobCancelled("The translation was stopped") from None
                raise
            else:
                if by_person:
                    raise JobCancelled("The translation was stopped")
                if state.stop_reason or state.watch.deadline_passed():
                    # Told to stop, or past the overall deadline: an answer that arrives in the grace period
                    # is not taken.
                    raise self._stopped(state.stop_reason or DEADLINE_EXCEEDED)
                return outcome
            if not by_person:
                try:
                    spec.check_cancel()
                except JobCancelled:
                    by_person = True
                    state.watch.cancel_requested("cancelled")
                    handle.cancel("cancelled")
                    continue
                except BaseException as error:
                    # Whatever the check raised (a sports operation that no longer matches, say), the request
                    # must not be left running and spending.
                    handle.cancel("cancelled")
                    raise _CheckFailed(error) from None
            verdict = state.watch.check()
            if verdict.action == DeadlineAction.CANCEL:
                state.stop_reason = verdict.reason
                handle.cancel(verdict.reason)
            elif verdict.action == DeadlineAction.KILL:
                # The worker ignored the cancel for the whole grace period: its answer is discarded and its
                # worker takes no new jobs.
                self.workers.abandon(plugin_id)
                if by_person:
                    raise JobCancelled("The translation was stopped")
                raise self._stopped(state.stop_reason or INACTIVITY_TIMEOUT)

    # ------------------------------------------------------------------------------------------- output
    def _compose(self, spec: JobSpec, result: wire.EngineResult, target: LanguageSpec) -> tuple[list[str], str | None]:
        """The output lines, one per input line, after Bazarr+'s checks and right-to-left marks."""
        source = [str(line) for line in spec.lines]
        output: list[str] = []
        translated: list[int] = []
        untranslated = 0
        for position, text in enumerate(source):
            if position in result.lines:
                output.append(result.lines[position])
                translated.append(position)
            elif not text.strip():
                output.append(text)
            elif result.partial:
                output.append(text)
                untranslated += 1
            else:
                raise AttemptFailure(fail.OUTPUT_CHECK_FAILED,
                                     f"{fail.MESSAGES[fail.OUTPUT_CHECK_FAILED]} A line came back missing.",
                                     reason="line_count")
        for position in translated:
            violations = check_line(source[position], output[position], position)
            if violations:
                raise AttemptFailure(fail.OUTPUT_CHECK_FAILED,
                                     f"{fail.MESSAGES[fail.OUTPUT_CHECK_FAILED]} {violations[0].message}.",
                                     reason=violations[0].code)
        marked = add_rtl_marks([output[position] for position in translated], target.rtl)
        for position, text in zip(translated, marked):
            output[position] = text
        partial_error = None
        if result.partial or untranslated:
            partial_error = (f"No translated text was returned for {max(untranslated, len(result.missing))} "
                             f"of {len(source)} cues.")
        return output, partial_error


def _is_free(connection) -> bool:
    if connection is None:
        return True
    return not connection.price_input_per_million and not connection.price_output_per_million


# ----------------------------------------------------------------------------------------------- upkeep

MAINTENANCE_SECONDS = 3600.0
_maintained_at: float | None = None


def maintain(session=None, *, clock: Callable[[], datetime] = datetime.now, limits: CostLimits | None = None) -> dict[str, int]:
    """The ledger's and the checkpoints' upkeep: give back the budgets no live job holds, move running jobs'
    budgets into the new month, and delete the checkpoints that ran out. Nothing else schedules these."""
    from sqlalchemy import select

    from app.database import TableTranslatorJob

    from .jobs import ACTIVE_STATES

    if session is None:
        from app.database import database

        session = database
    live = [str(row) for row in session.execute(
        select(TableTranslatorJob.id).where(TableTranslatorJob.state.in_(ACTIVE_STATES))).scalars().all()]
    ledger = UsageLedger(session, clock=clock)
    released = ledger.release_stale(live)
    moved = ledger.roll_month(limits if limits is not None else configured_cost_limits())
    swept = TranslationJobs(session, clock=clock).sweep()
    return {"released": released, "moved": moved, "swept": swept}


def _maintain_when_due(session, clock, monotonic, limits) -> None:
    """Run the upkeep at most once an hour from the jobs that dispatch. A failure never stops the job."""
    global _maintained_at
    now = monotonic()
    if _maintained_at is not None and now - _maintained_at < MAINTENANCE_SECONDS:
        return
    _maintained_at = now
    try:
        maintain(session, clock=clock, limits=limits())
    except Exception:
        logger.warning("The translation upkeep failed", exc_info=True)


def recover_after_restart(session=None, *, clock: Callable[[], datetime] = datetime.now) -> dict[str, int]:
    """What the dispatcher does once at startup: jobs the restart cut off await a retry, and the upkeep runs.
    Checkpoints stay, so the retry resumes after the last finished batch."""
    if session is None:
        from app.database import database

        session = database
    recovered = TranslationJobs(session, clock=clock).recover_interrupted()
    return {"recovered": len(recovered), **maintain(session, clock=clock)}

# coding=utf-8
"""What a translator worker may ask the host while it translates: ``reserve``, ``usage`` and ``checkpoint``.

Each handler answers only once the record is stored, so a plugin that gets its answer can rely on it: a
reservation is in the ledger before the paid call goes out, a settlement is in before the next batch, a
checkpoint is on disk before the worker moves on. A kill between a call and its settlement therefore leaves
the reservation counted at its worst case, and a retry cannot push the job or the month past its cap.

The handlers are plain functions of one ``PoolHostCall``. The worker pool hands each one the context the
dispatcher attached to the request (``AttemptContext``), so one set of handlers serves every attempt of every
job. They run on the worker session's own thread and use ``context.session``, the scoped session of the
application, which gives that thread a session of its own.

The wire shapes are the plugin SDK's: ``reserve`` carries ``amount`` (dollars, the worst-case charge) and
``model`` and is answered with ``{"hold": id}``; ``usage`` carries ``tokens``, ``cost`` (dollars), ``model`` and
the ``reservations`` it settles; ``checkpoint`` carries an opaque ``data`` object, which is stored as the plugin
wrote it and handed back to a later attempt.
"""
from __future__ import annotations

import functools
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping
from uuid import uuid4

logger = logging.getLogger(__name__)

HOST_CALL_NAMES = ("reserve", "usage", "checkpoint")


@dataclass
class AttemptContext:
    """What the host calls of one attempt need to know. Built by the dispatcher, read by the handlers."""

    session: Any
    job_id: int                       # the translation job's id
    connection_id: int | None
    backend_type: str = ""
    model: str = ""
    limits: Callable[[], Any] = None  # the cost caps in force, read again for every reservation
    clock: Callable[[], datetime] = datetime.now
    # The next index a checkpoint of this job is stored under.
    next_batch: int = 0
    # Why the host refused the last reservation (month_spent, no_budget, job_budget), if it did.
    refusal: str | None = None
    # Set when a settlement showed the job's recorded cost over its budget: the dispatcher cancels the attempt.
    over_budget: bool = False
    # Stops the running request. Set by the dispatcher once the request exists; a call that finds it
    # unset leaves ``cancel_pending`` for the dispatcher to act on.
    cancel: Callable[[str], None] | None = None
    cancel_pending: str | None = None
    # Called on every reserve, usage and checkpoint: the worker is alive and making headway.
    activity: Callable[[], None] | None = None
    batches: int = 0
    held: list = field(default_factory=list)

    @property
    def ledger_id(self) -> str:
        return str(self.job_id)

    def stop(self, reason: str) -> None:
        if self.cancel is not None:
            self.cancel(reason)
        else:
            self.cancel_pending = reason


def _refuse(code: str, message: str):
    from provider_hub.runtime.errors import HostCallError

    return HostCallError(code, message)


def _number(params: Mapping[str, Any], key: str, *, whole: bool = False):
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise _refuse("invalid_request", f"{key} must be a number from 0")
    if whole and int(value) != value:
        raise _refuse("invalid_request", f"{key} must be a whole number")
    return int(value) if whole else value


def _context(call) -> AttemptContext:
    context = getattr(call, "context", None)
    if not isinstance(context, AttemptContext):
        raise _refuse("invalid_request", "this call belongs to no running translation")
    return context


def _touch(context: AttemptContext) -> None:
    if context.activity is not None:
        try:
            context.activity()
        except Exception:  # liveness bookkeeping must never fail a call
            logger.debug("A translation activity callback failed", exc_info=True)


def _releases_its_session(handler):
    """Give the thread's database session back when a call is answered.

    A handler runs on a thread of the worker session that lives only as long as calls are waiting, and
    the application's session is one per thread. Left alone, each of those threads would keep a connection
    checked out until it was garbage collected, which on PostgreSQL's small pool runs out within one job.
    """
    @functools.wraps(handler)
    def wrapper(call):
        try:
            return handler(call)
        finally:
            context = getattr(call, "context", None)
            remove = getattr(getattr(context, "session", None), "remove", None)
            if callable(remove):
                try:
                    remove()
                except Exception:
                    logger.debug("A translation host call could not release its session", exc_info=True)

    return wrapper


@_releases_its_session
def reserve(call) -> dict:
    """Record a paid call's worst-case charge before it is sent. Refused when it does not fit the budget."""
    from .usage import UsageError, UsageLedger, to_micros

    context = _context(call)
    params = call.params if isinstance(call.params, Mapping) else {}
    amount = _number(params, "amount")
    model = params.get("model")
    model = model if isinstance(model, str) else ""
    try:
        worst = to_micros(amount)
    except UsageError as error:
        raise _refuse("invalid_request", str(error)) from None
    hold = uuid4().hex
    reservation = UsageLedger(context.session, clock=context.clock).reserve_call(
        context.ledger_id, hold, worst_case_micros=worst, limits=context.limits(),
        connection_id=context.connection_id, backend_type=context.backend_type, model=model or context.model)
    if not reservation.admitted:
        context.refusal = reservation.reason
        # The plugin ends its attempt as cancelled; the dispatcher reads ``refusal`` to tell that from a Stop.
        raise _refuse("cancelled", f"the cost budget does not cover this call ({reservation.reason})")
    context.held.append(hold)
    _touch(context)
    return {"hold": hold}


@_releases_its_session
def usage(call) -> dict:
    """Settle reservations against what a call used. A call made without a reservation is recorded too."""
    from .usage import UsageError, UsageLedger, to_micros

    context = _context(call)
    params = call.params if isinstance(call.params, Mapping) else {}
    tokens = _number(params, "tokens", whole=True)
    cost = _number(params, "cost")
    model = params.get("model")
    model = model if isinstance(model, str) and model else None
    holds = params.get("reservations") or []
    if not isinstance(holds, list) or any(not isinstance(item, str) or not item for item in holds):
        raise _refuse("invalid_request", "reservations must be a list of hold ids")
    try:
        actual = to_micros(cost)
    except UsageError as error:
        raise _refuse("invalid_request", str(error)) from None
    ledger = UsageLedger(context.session, clock=context.clock)
    limits = context.limits()
    if not holds:
        # Spend reported with no hold: a free connection's tokens, or a back end that only reports its cost.
        # It is a call of worst case 0 (always admitted) settled at what was reported, so it counts.
        hold = uuid4().hex
        ledger.reserve_call(context.ledger_id, hold, worst_case_micros=0, limits=limits,
                            connection_id=context.connection_id, backend_type=context.backend_type,
                            model=model or context.model)
        holds = [hold]
    # The whole reading is one call's: the first hold takes it and any further hold of the same call settles at 0.
    # All of them are settled as one step, so a total taken half way (the reading against one hold while the
    # other still counts at its worst case) never decides whether the job is over its budget.
    try:
        settlement = ledger.settle_calls(
            context.ledger_id,
            [(hold, tokens if index == 0 else 0, 0, actual if index == 0 else 0, model)
             for index, hold in enumerate(holds)], limits=limits)
    except UsageError:
        raise _refuse("invalid_request", "that reservation is not one of this job's") from None
    over_budget = settlement.over_budget
    if over_budget:
        context.over_budget = True
        context.stop("budget_exhausted")
    _touch(context)
    return {}


@_releases_its_session
def checkpoint(call) -> dict:
    """Store a checkpoint: the plugin's own resumable state, kept as text under the job's next batch index."""
    from .history import HistoryStore
    from .jobs import TranslationJobs

    context = _context(call)
    params = call.params if isinstance(call.params, Mapping) else {}
    data = params.get("data")
    if not isinstance(data, Mapping):
        raise _refuse("invalid_request", "data must be an object")
    try:
        text = json.dumps(data, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise _refuse("invalid_request", "data must be plain JSON") from None
    jobs = TranslationJobs(context.session, clock=context.clock)
    if not jobs.save_batch(context.job_id, context.next_batch, [text]):
        raise _refuse("cancelled", "the translation is no longer running")
    context.next_batch += 1
    context.batches += 1
    if context.connection_id is not None:
        model = data.get("model")
        HistoryStore(context.session, clock=context.clock).record_batches(
            context.ledger_id, connection_id=context.connection_id,
            model=model if isinstance(model, str) and model else context.model, backend_type=context.backend_type)
    _touch(context)
    return {}


def host_calls() -> dict[str, Callable]:
    """The handlers a translator worker definition registers (``WorkerDefinition.host_calls``)."""
    return {"reserve": reserve, "usage": usage, "checkpoint": checkpoint}


def stored_checkpoints(batches: Mapping[int, list[str]]) -> list[dict]:
    """The checkpoints of earlier attempts, in the order they were stored, as the plugin wrote them."""
    found = []
    for index in sorted(batches):
        for text in batches[index]:
            try:
                value = json.loads(text)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found

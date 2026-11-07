# coding=utf-8
"""The translation usage ledger: job budgets, per-call reservations and month caps.

Money is reserved before it is spent, in three steps:

1. At dispatch a job reserves its budget from the month's unreserved remainder
   (``reserve_job_budget``): the per-job cap, or without one an equal share of the
   remainder across the translation limit, and never more than is unreserved.
2. Before each paid call the job reserves the call's worst-case charge out of its
   budget (``reserve_call``). A call that does not fit is refused. A call whose
   worst-case charge is 0, such as one to a free local server, always fits, so a
   spent month still serves free connections and skips priced ones.
3. After the call the reservation is settled against what it really used
   (``settle_call``). A reservation that is never settled, because the process died
   between the call and its settlement, keeps counting at its worst-case charge.

A call counts against the month it was made in. At a month change a running job's
unspent budget moves from the old month to the new one in one transaction, and the
job gets what is left of the new month if that is less. Every read and write that
decides how much is free runs inside ``ledger_transaction``, so two jobs never
reserve the same money.

Money is whole micro-dollars (millionths of a dollar) held in Python and database
integers, so reserving and settling never add floating-point fractions. A price per
million tokens is micro-dollars per token, which is why ``worst_case_micros`` is just
tokens times price. Amounts that come in as dollars are converted once, at the edge,
by ``to_micros``: a charge rounds up and a cap rounds down, so rounding never lets a
job spend more than was meant.

A month is the calendar month of the server's local time. Caps are passed in
(``CostLimits``); this module reads no settings.

Nothing here ends a budget by itself when its job stops without releasing it (the
process died and the job was never retried). ``release_stale`` gives those back, and
the caller that knows which jobs are alive has to call it: at startup, and before it
calls ``roll_month`` at the start of each month. ``roll_month`` is not scheduled
here either.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable

from sqlalchemy import func, select

from app.database import TableTranslationBudget, TableTranslationUsage

from .history import MICROS_PER_DOLLAR, HistoryStore
from .transaction import ledger_transaction

BUDGET_ACTIVE = "active"
BUDGET_RELEASED = "released"
CALL_RESERVED = "reserved"
CALL_SETTLED = "settled"

# JobBudget.status
RESERVED = "reserved"
MONTH_SPENT = "month_spent"
WAIT = "wait"
JOB_CAP_SPENT = "job_cap_spent"

# CallReservation.reason
REFUSED_MONTH_SPENT = "month_spent"
REFUSED_NO_BUDGET = "no_budget"
REFUSED_JOB_BUDGET = "job_budget"


class UsageError(ValueError):
    """A request the ledger refuses, with a reason a person can read."""


def to_micros(dollars, *, charge: bool = True) -> int:
    """Whole micro-dollars for an amount in dollars (an int, a float, a Decimal or a string).

    ``charge`` rounds up, for a price or a cost. ``charge=False`` rounds down, for a cap.
    """
    try:
        value = Decimal(str(dollars)) * MICROS_PER_DOLLAR
    except Exception as error:
        raise UsageError(f"Not an amount of money: {dollars!r}") from error
    if not value.is_finite() or value < 0:
        raise UsageError(f"Not an amount of money: {dollars!r}")
    return int(value.to_integral_value(rounding=ROUND_CEILING if charge else ROUND_FLOOR))


def worst_case_micros(input_tokens: int, output_token_limit: int, price_input_per_million,
                      price_output_per_million) -> int:
    """The most a call can cost: its input tokens plus the output limit set on it, at the model's prices."""
    if input_tokens < 0 or output_token_limit < 0:
        raise UsageError("A token count cannot be negative")
    total = (Decimal(input_tokens) * Decimal(str(price_input_per_million))
             + Decimal(output_token_limit) * Decimal(str(price_output_per_million)))
    if not total.is_finite() or total < 0:
        raise UsageError("A price cannot be negative")
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def _micros(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError(f"{name} is whole micro-dollars (an int), not {type(value).__name__}")
    if value < 0:
        raise UsageError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True)
class CostLimits:
    """The caps that apply right now, in micro-dollars. ``None`` means no cap."""

    month_cap: int | None = None
    job_cap: int | None = None
    translation_limit: int = 1

    def __post_init__(self):
        for name in ("month_cap", "job_cap"):
            value = getattr(self, name)
            if value is not None:
                _micros(value, name)
        if isinstance(self.translation_limit, bool) or not isinstance(self.translation_limit, int):
            raise UsageError("translation_limit is a whole number")
        if self.translation_limit < 1:
            raise UsageError("translation_limit is at least 1")

    @classmethod
    def from_dollars(cls, month_cap=None, job_cap=None, translation_limit: int = 1) -> "CostLimits":
        return cls(None if month_cap is None else to_micros(month_cap, charge=False),
                   None if job_cap is None else to_micros(job_cap, charge=False), translation_limit)


@dataclass(frozen=True)
class JobBudget:
    """``amount`` is what the job may still spend, in micro-dollars; ``None`` means no cap applies."""

    status: str
    amount: int | None
    month: str


@dataclass(frozen=True)
class CallReservation:
    admitted: bool
    reason: str | None = None
    worst_case: int = 0


@dataclass(frozen=True)
class Settlement:
    job_cost: int
    over_budget: bool
    already_settled: bool = False


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


class UsageLedger:
    def __init__(self, session, *, clock: Callable[[], datetime] = datetime.now):
        self._session = session
        self._clock = clock
        self._history = HistoryStore(session, clock=clock)

    # ------------------------------------------------------------------- reads
    def month_committed(self, month: str | None = None) -> int:
        """What the month has spent or reserved: its calls at their counted cost plus every job's hold."""
        month = month or month_key(self._clock())
        with ledger_transaction(self._session):
            return self._committed(month)

    def month_remainder(self, limits: CostLimits, month: str | None = None) -> int | None:
        """What is still unreserved in the month, or ``None`` when it has no cap."""
        month = month or month_key(self._clock())
        with ledger_transaction(self._session):
            return self._remainder(limits, month)

    def job_cost(self, job_id: str) -> int:
        """What the job has spent or has reserved at worst case, across every connection and engine."""
        return self._job_counted(job_id)

    def job_hold(self, job_id: str) -> int | None:
        """What the job may still spend, or ``None`` when no cap applies. 0 when it holds nothing."""
        row = self._budget_row(job_id)
        if row is None or row.state != BUDGET_ACTIVE:
            return 0
        return self._hold(row, self._job_counted(job_id))

    # ------------------------------------------------------------------ writes
    def reserve_job_budget(self, job_id: str, limits: CostLimits) -> JobBudget:
        """Take the job's budget from the month's remainder, in one transaction.

        Asking again while the job holds a budget returns that budget. A job retried
        later asks again after its first attempt released the budget; its cap counts
        what it spent before. ``wait`` means the month's money is all held by other
        running jobs, so the job should ask again when one settles. ``month_spent``
        and ``job_cap_spent`` mean nothing can be reserved: free connections still
        run, priced ones are skipped.
        """
        now = self._clock()
        month = month_key(now)
        with ledger_transaction(self._session):
            row = self._budget_row(job_id)
            counted = self._job_counted(job_id)
            if row is not None and row.state == BUDGET_ACTIVE:
                if row.month != month:
                    self._roll_row(row, limits, month, counted, now)
                self._fit_to_caps(row, limits, month, counted, now)
                return JobBudget(RESERVED, self._hold(row, counted), row.month)

            allowance, status = self._allowance(job_id, limits, month, counted)
            if status is not None:
                return JobBudget(status, 0, month)

            budget = None if allowance is None else counted + allowance
            if row is None:
                row = TableTranslationBudget(job_id=job_id, month=month, budget_micros=budget,
                                             state=BUDGET_ACTIVE, created_at=now, updated_at=now)
                self._session.add(row)
            else:
                row.month, row.budget_micros, row.state, row.updated_at = month, budget, BUDGET_ACTIVE, now
            self._session.flush()
            return JobBudget(RESERVED, allowance, month)

    def reserve_call(self, job_id: str, call_id: str, *, worst_case_micros: int, limits: CostLimits,
                     connection_id: int | None = None, backend_type: str = "",
                     model: str = "") -> CallReservation:
        """Reserve one paid call at its worst-case charge. Idempotent for a call id.

        The charge is the call's input tokens plus the output limit set on the
        request, at the model's prices (``worst_case_micros`` below). A call that does
        not fit what the job still holds is refused and nothing is recorded; a free
        call (0) is always recorded.
        """
        _micros(worst_case_micros, "worst_case_micros")
        now = self._clock()
        month = month_key(now)
        with ledger_transaction(self._session):
            existing = self._call_row(job_id, call_id)
            if existing is not None:
                return CallReservation(True, None, existing.worst_case_micros)
            if worst_case_micros > 0:
                refusal = self._refusal(job_id, worst_case_micros, limits, month, now)
                if refusal is not None:
                    return CallReservation(False, refusal, worst_case_micros)
            self._session.add(TableTranslationUsage(
                job_id=job_id, call_id=call_id, connection_id=connection_id, backend_type=backend_type or "",
                model=model or "", month=month, worst_case_micros=worst_case_micros, input_tokens=0,
                output_tokens=0, actual_micros=None, state=CALL_RESERVED, created_at=now))
            self._session.flush()
            self._history.refresh_in_transaction(job_id)
            return CallReservation(True, None, worst_case_micros)

    def settle_call(self, job_id: str, call_id: str, *, input_tokens: int, output_tokens: int,
                    actual_micros: int, model: str | None = None, limits: CostLimits | None = None) -> Settlement:
        """Replace a call's worst-case charge with what it used. Settling twice changes nothing.

        ``over_budget`` is true when the job's recorded cost has crossed its budget,
        which is the caller's cue to cancel the job. A call is settled even when it
        used more than it reserved: the money is spent, and the ledger says so. What a
        call did not use goes back to the job's hold only while the budget is still in
        the month the call was made in; after a rollover it goes back to that old month. With ``limits``,
        the job's budget is first held to the caps that apply now, and ``over_budget`` is also true when the
        job's cost is over the job cap in force.
        """
        _micros(actual_micros, "actual_micros")
        if input_tokens < 0 or output_tokens < 0:
            raise UsageError("Usage cannot be negative")
        now = self._clock()
        with ledger_transaction(self._session):
            row = self._call_row(job_id, call_id)
            if row is None:
                raise UsageError(f"No reservation for call {call_id} of job {job_id}")
            if row.state == CALL_SETTLED:
                return Settlement(self._job_counted(job_id), self._over_budget(job_id), already_settled=True)
            counted_before = self._job_counted(job_id)
            row.actual_micros = actual_micros
            row.input_tokens = input_tokens
            row.output_tokens = output_tokens
            if model:
                row.model = model
            row.state = CALL_SETTLED
            row.settled_at = now
            # Every read below reloads rows and sums from the database, and the session does not flush on
            # its own: write the settlement first, and each adjustment before the next read.
            self._session.flush()
            self._keep_the_refund_in_the_calls_month(job_id, row.month, counted_before)
            self._session.flush()
            if limits is not None:
                budget = self._budget_row(job_id)
                if budget is not None and budget.state == BUDGET_ACTIVE:
                    self._fit_to_caps(budget, limits, month_key(now), self._job_counted(job_id), now)
            self._history.refresh_in_transaction(job_id)
            cost = self._job_counted(job_id)
            over_cap = limits is not None and limits.job_cap is not None and cost > limits.job_cap
            return Settlement(cost, over_cap or self._over_budget(job_id))

    def release_job(self, job_id: str) -> None:
        """Give back what the job still holds. Calls it never settled keep counting at worst case."""
        now = self._clock()
        with ledger_transaction(self._session):
            row = self._budget_row(job_id)
            if row is not None and row.state == BUDGET_ACTIVE:
                row.state = BUDGET_RELEASED
                row.updated_at = now
                self._session.flush()

    def release_stale(self, live_job_ids) -> int:
        """Release the budget of every job that is not in ``live_job_ids``. Returns how many were released.

        A job that stopped without releasing (the process died, and the job was cancelled or dropped
        instead of retried) would otherwise hold its unspent budget for ever, and ``roll_month`` would
        carry the hold into each new month. ``live_job_ids`` are the ids, as strings, of the jobs that
        are queued, running or awaiting a retry; the translation job table is where they come from.
        An empty collection releases every budget. Calls those jobs never settled keep counting at
        worst case.

        The dispatcher calls this at startup, and before ``roll_month`` at the start of a month.
        """
        live = {str(job_id) for job_id in live_job_ids}
        now = self._clock()
        released = 0
        with ledger_transaction(self._session):
            for row in self._active_budgets():
                if row.job_id not in live:
                    row.state = BUDGET_RELEASED
                    row.updated_at = now
                    released += 1
            self._session.flush()
        return released

    def roll_month(self, limits: CostLimits) -> int:
        """Move every running job's unspent budget into the current month. Returns how many moved."""
        now = self._clock()
        month = month_key(now)
        moved = 0
        with ledger_transaction(self._session):
            rows = self._session.execute(
                select(TableTranslationBudget)
                .where(TableTranslationBudget.state == BUDGET_ACTIVE, TableTranslationBudget.month != month)
                .order_by(TableTranslationBudget.id).execution_options(populate_existing=True)).scalars().all()
            for row in rows:
                self._roll_row(row, limits, month, self._job_counted(row.job_id), now)
                moved += 1
        return moved

    # ----------------------------------------------------------------- helpers
    def _allowance(self, job_id: str, limits: CostLimits, month: str, counted: int):
        """What a job that holds nothing may take now: ``(allowance, None)``, or ``(0, status)`` when nothing.

        An allowance of ``None`` means no cap applies. Without a job cap the allowance is an equal
        share of the month's remainder: the remainder divided among the translation slots the other
        holders in the month have not taken.
        """
        remainder = self._remainder(limits, month)
        if limits.job_cap is not None:
            base = max(0, limits.job_cap - counted)
        elif remainder is not None:
            free_slots = max(1, limits.translation_limit - self._holders(month, excluding=job_id))
            base = remainder // free_slots
        else:
            base = None
        allowance = base if remainder is None or base is None else min(base, remainder)
        if allowance is not None and allowance <= 0:
            if limits.job_cap is not None and limits.job_cap - counted <= 0:
                return 0, JOB_CAP_SPENT
            if self._holds(month, excluding=job_id) > 0:
                return 0, WAIT
            return 0, MONTH_SPENT
        return allowance, None

    def _refusal(self, job_id, worst_case, limits, month, now) -> str | None:
        row = self._budget_row(job_id)
        if row is None or row.state != BUDGET_ACTIVE:
            remainder = self._remainder(limits, month)
            if remainder is not None and remainder <= 0:
                return REFUSED_MONTH_SPENT
            return REFUSED_NO_BUDGET
        counted = self._job_counted(job_id)
        if row.budget_micros is None:
            if limits.month_cap is None and limits.job_cap is None:
                return None
            # A cap was set after this job reserved with none: it holds a budget from now on.
            allowance, status = self._allowance(job_id, limits, month, counted)
            row.budget_micros = counted + (allowance or 0)
            row.month = month
            row.updated_at = now
            self._session.flush()
            if status == MONTH_SPENT:
                return REFUSED_MONTH_SPENT
        elif row.month != month:
            self._roll_row(row, limits, month, counted, now)
        else:
            self._fit_to_caps(row, limits, month, counted, now)
        if worst_case > self._hold(row, counted):
            return REFUSED_JOB_BUDGET
        return None

    def _roll_row(self, row, limits: CostLimits, month: str, counted: int, now: datetime) -> None:
        if row.budget_micros is not None:
            # The carried allowance is held to the job cap that applies now, then to what the new month can cover.
            budget = row.budget_micros if limits.job_cap is None else min(row.budget_micros, limits.job_cap)
            unspent = max(0, budget - counted)
            remainder = self._remainder(limits, month)  # the row still sits in the old month, so it is not counted
            grant = unspent if remainder is None else min(unspent, remainder)
            row.budget_micros = counted + grant
        row.month = month
        row.updated_at = now
        self._session.flush()

    def _fit_to_caps(self, row, limits: CostLimits, month: str, counted: int, now: datetime) -> None:
        """Hold a finite budget to the caps that apply now, which may be lower than when it was reserved.

        The job cap bounds the job's whole cost, so the budget cannot exceed it. The month cap bounds what
        the month has spent plus what jobs hold, so the hold shrinks by however far the month is over it
        (the other jobs' holds and the spending stay as they are). A budget already inside both is untouched.
        """
        in_month = row.month == month
        if row.budget_micros is None:
            # A cap set after this job reserved with none: it holds a budget from now on.
            if not in_month or (limits.month_cap is None and limits.job_cap is None):
                return
            allowance, _status = self._allowance(row.job_id, limits, month, counted)
            budget = counted + (allowance or 0)
            row.budget_micros = budget if limits.job_cap is None else min(budget, max(limits.job_cap, counted))
            row.updated_at = now
            self._session.flush()
            return
        budget = row.budget_micros
        # The job cap bounds the job's whole cost, whichever month its budget sits in. A job already over it
        # holds nothing more, but its budget is not cut below what it has spent: whether that cost is over the
        # cap is answered by the cap in force when it settles, so lifting the cap later does not cancel it.
        if limits.job_cap is not None:
            budget = min(budget, max(limits.job_cap, counted))
        # The month cap bounds one month, so it applies to a budget that sits in that month.
        if limits.month_cap is not None and in_month:
            stored_hold = max(0, row.budget_micros - counted)
            room = max(0, limits.month_cap - self._committed(month) + stored_hold)
            budget = min(budget, counted + min(max(0, budget - counted), room))
        if budget != row.budget_micros:
            row.budget_micros = budget
            row.updated_at = now
            self._session.flush()

    def _keep_the_refund_in_the_calls_month(self, job_id: str, call_month: str, counted_before: int) -> None:
        """Keep what a call did not use out of a budget that has moved on to a later month.

        The call was counted in the month it was made, so what it did not use goes back to that month.
        If the job's budget already rolled into a later month, the job's hold there must stay what it
        was before the settlement; adding the refund to it would let the job spend money the later month
        gave to other jobs. The budget is therefore lowered by exactly as much as the cost fell, no more:
        a hold that a lower cap had already taken away is not taken away twice, and a call that cost more
        than it reserved still leaves the job over its budget.
        """
        budget = self._budget_row(job_id)
        if budget is None or budget.state != BUDGET_ACTIVE or budget.budget_micros is None:
            return
        if budget.month != call_month:
            hold_before = max(0, budget.budget_micros - counted_before)
            budget.budget_micros = min(budget.budget_micros, self._job_counted(job_id) + hold_before)

    @staticmethod
    def _hold(row, counted: int) -> int | None:
        if row.budget_micros is None:
            return None
        return max(0, row.budget_micros - counted)

    def _over_budget(self, job_id: str) -> bool:
        row = self._budget_row(job_id)
        if row is None or row.state != BUDGET_ACTIVE or row.budget_micros is None:
            return False
        return self._job_counted(job_id) > row.budget_micros

    def _remainder(self, limits: CostLimits, month: str) -> int | None:
        if limits.month_cap is None:
            return None
        return max(0, limits.month_cap - self._committed(month))

    def _committed(self, month: str) -> int:
        spent = self._session.execute(
            select(func.coalesce(func.sum(func.coalesce(TableTranslationUsage.actual_micros,
                                                        TableTranslationUsage.worst_case_micros)), 0))
            .where(TableTranslationUsage.month == month)).scalar()
        return int(spent or 0) + self._holds(month)

    def _holds(self, month: str, *, excluding: str | None = None) -> int:
        rows = self._session.execute(
            select(TableTranslationBudget)
            .where(TableTranslationBudget.state == BUDGET_ACTIVE, TableTranslationBudget.month == month,
                   TableTranslationBudget.budget_micros.is_not(None))
            .execution_options(populate_existing=True)).scalars().all()
        total = 0
        for row in rows:
            if row.job_id != excluding:
                total += self._hold(row, self._job_counted(row.job_id))
        return total

    def _holders(self, month: str, *, excluding: str | None = None) -> int:
        """How many other jobs hold a capped budget against the month."""
        return int(self._session.execute(
            select(func.count()).select_from(TableTranslationBudget)
            .where(TableTranslationBudget.state == BUDGET_ACTIVE, TableTranslationBudget.month == month,
                   TableTranslationBudget.budget_micros.is_not(None),
                   TableTranslationBudget.job_id != (excluding or ""))).scalar() or 0)

    def _active_budgets(self):
        return self._session.execute(
            select(TableTranslationBudget).where(TableTranslationBudget.state == BUDGET_ACTIVE)
            .order_by(TableTranslationBudget.id).execution_options(populate_existing=True)).scalars().all()

    def _job_counted(self, job_id: str) -> int:
        total = self._session.execute(
            select(func.coalesce(func.sum(func.coalesce(TableTranslationUsage.actual_micros,
                                                        TableTranslationUsage.worst_case_micros)), 0))
            .where(TableTranslationUsage.job_id == job_id)).scalar()
        return int(total or 0)

    def _budget_row(self, job_id: str):
        return self._session.execute(
            select(TableTranslationBudget).where(TableTranslationBudget.job_id == job_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()

    def _call_row(self, job_id: str, call_id: str):
        return self._session.execute(
            select(TableTranslationUsage).where(TableTranslationUsage.job_id == job_id,
                                                TableTranslationUsage.call_id == call_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()

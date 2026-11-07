"""Translation usage ledger and history: reservations, caps, month rollover and attribution.

Runs on SQLite, and on PostgreSQL when BAZARR_PG_TEST_URL is set.
"""
import os
import threading
from datetime import datetime
from uuid import uuid4

import pytest

from subtitles.tools.translate.runtime.history import HistoryError, HistoryStore
from subtitles.tools.translate.runtime.usage import (
    JOB_CAP_SPENT, MONTH_SPENT, REFUSED_JOB_BUDGET, REFUSED_MONTH_SPENT, REFUSED_NO_BUDGET, RESERVED, WAIT,
    CostLimits, UsageError, UsageLedger, month_key, to_micros, worst_case_micros)

TABLE_NAMES = ("TableTranslationHistory", "TableTranslationHistoryConnection", "TableTranslationUsage",
               "TableTranslationBudget")


def _tables():
    from app import database

    return [getattr(database, name).__table__ for name in TABLE_NAMES]


def _production_sessions(engine):
    """Sessions configured like the application's: no autoflush, no expiry on commit."""
    from sqlalchemy.orm import scoped_session, sessionmaker

    return scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))


def _make_session(kind, tmp_path):
    from sqlalchemy import create_engine, event
    from app.database import Base

    if kind == "sqlite":
        # Built the way app/database.py builds the application's engine: one fresh connection per
        # statement, AUTOCOMMIT, and the WAL and busy_timeout pragmas.
        from sqlalchemy.pool import NullPool

        from app.database import configure_sqlite_connection

        engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}", poolclass=NullPool,
                               isolation_level="AUTOCOMMIT")
        event.listen(engine, "connect", configure_sqlite_connection)
        Base.metadata.create_all(engine, tables=_tables())
        return engine, _production_sessions(engine), None
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL tests")
    import sqlalchemy as sa

    schema = "translation_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    # The application runs PostgreSQL in AUTOCOMMIT, where rollback() does nothing.
    engine = create_engine(url, isolation_level="AUTOCOMMIT")

    @event.listens_for(engine, "connect")
    def _path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {schema}")
        cursor.close()

    Base.metadata.create_all(engine, tables=_tables())
    return engine, _production_sessions(engine), (admin, schema)


@pytest.fixture(params=["sqlite", "postgres"])
def session(request, tmp_path):
    import sqlalchemy as sa

    engine, scoped, pg = _make_session(request.param, tmp_path)
    try:
        yield scoped
    finally:
        scoped.remove()
        engine.dispose()
        if pg:
            admin, schema = pg
            with admin.begin() as connection:
                connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
            admin.dispose()


class Clock:
    def __init__(self, moment=datetime(2026, 11, 15, 12, 0, 0)):
        self.moment = moment

    def __call__(self):
        return self.moment


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(session, clock):
    return UsageLedger(session, clock=clock)


@pytest.fixture
def history(session, clock):
    return HistoryStore(session, clock=clock)


def usd(dollars):
    return to_micros(dollars)


def _call(ledger, job, call, worst, limits, **kwargs):
    return ledger.reserve_call(job, call, worst_case_micros=usd(worst), limits=limits, **kwargs)


def _settle(ledger, job, call, cost, *, input_tokens=1, output_tokens=1, **kwargs):
    return ledger.settle_call(job, call, input_tokens=input_tokens, output_tokens=output_tokens,
                              actual_micros=usd(cost), **kwargs)


# ------------------------------------------------------------------ job budgets

def test_a_job_budget_is_the_per_job_cap_when_the_month_has_room(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=10.0, translation_limit=2)
    budget = ledger.reserve_job_budget("job-1", limits)
    assert (budget.status, budget.amount, budget.month) == (RESERVED, usd(10.0), "2026-11")
    assert ledger.month_committed() == usd(10.0)
    assert ledger.month_remainder(limits) == usd(90.0)


def test_without_a_job_cap_the_budget_is_an_equal_share_of_the_remainder(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, translation_limit=4)
    assert ledger.reserve_job_budget("a", limits).amount == usd(25.0)
    assert ledger.reserve_job_budget("b", limits).amount == usd(25.0)  # still a quarter of the month


def test_a_budget_never_exceeds_what_is_unreserved(ledger):
    limits = CostLimits.from_dollars(month_cap=15.0, job_cap=10.0)
    assert ledger.reserve_job_budget("a", limits).amount == usd(10.0)
    assert ledger.reserve_job_budget("b", limits).amount == usd(5.0)


def test_no_caps_means_no_budget_to_hold(ledger):
    budget = ledger.reserve_job_budget("job", CostLimits.from_dollars())
    assert (budget.status, budget.amount) == (RESERVED, None)
    assert _call(ledger, "job", "c1", 1_000_000.0, CostLimits()).admitted


def test_asking_again_returns_the_same_budget_without_taking_more(ledger):
    limits = CostLimits.from_dollars(month_cap=20.0, job_cap=8.0)
    ledger.reserve_job_budget("job", limits)
    again = ledger.reserve_job_budget("job", limits)
    assert again.amount == usd(8.0)
    assert ledger.month_committed() == usd(8.0)


def test_a_job_that_finds_the_money_all_held_waits_and_a_spent_month_says_so(ledger):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=10.0)
    ledger.reserve_job_budget("runner", limits)
    assert ledger.reserve_job_budget("late", limits).status == WAIT
    # Once the runner settles for what it spent and ends, the money it did not spend frees up.
    _call(ledger, "runner", "c1", 4.0, limits)
    _settle(ledger, "runner", "c1", 4.0)
    ledger.release_job("runner")
    assert ledger.reserve_job_budget("late", limits).amount == usd(6.0)
    # Now that "late" holds 6 and 4 is spent, the month is spent for a third job once late spends it all.
    _call(ledger, "late", "c1", 6.0, limits)
    _settle(ledger, "late", "c1", 6.0)
    ledger.release_job("late")
    assert ledger.reserve_job_budget("third", limits).status == MONTH_SPENT


def test_a_retried_job_counts_what_it_spent_before_against_its_cap(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=10.0)
    ledger.reserve_job_budget("job", limits)
    _call(ledger, "job", "c1", 7.0, limits)
    _settle(ledger, "job", "c1", 7.0)
    ledger.release_job("job")
    retry = ledger.reserve_job_budget("job", limits)
    assert (retry.status, retry.amount) == (RESERVED, usd(3.0))
    _call(ledger, "job", "c2", 3.0, limits)
    _settle(ledger, "job", "c2", 3.0)
    ledger.release_job("job")
    assert ledger.reserve_job_budget("job", limits).status == JOB_CAP_SPENT


# ----------------------------------------------------------------- call reservations

def test_a_call_that_fits_is_reserved_at_worst_case_and_one_that_does_not_is_refused(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=5.0)
    ledger.reserve_job_budget("job", limits)
    assert _call(ledger, "job", "c1", 3.0, limits).admitted
    assert ledger.job_hold("job") == usd(2.0)
    refused = _call(ledger, "job", "c2", 3.0, limits)
    assert (refused.admitted, refused.reason) == (False, REFUSED_JOB_BUDGET)
    assert ledger.job_cost("job") == usd(3.0)  # a refused call records nothing


def test_reserving_the_same_call_twice_counts_it_once(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=5.0)
    ledger.reserve_job_budget("job", limits)
    assert _call(ledger, "job", "c1", 3.0, limits).admitted
    assert _call(ledger, "job", "c1", 3.0, limits).admitted
    assert ledger.job_cost("job") == usd(3.0)


def test_settling_replaces_the_worst_case_with_what_was_used(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=5.0)
    ledger.reserve_job_budget("job", limits)
    _call(ledger, "job", "c1", 3.0, limits)
    result = _settle(ledger, "job", "c1", 0.5, input_tokens=100, output_tokens=40)
    assert (result.job_cost, result.over_budget, result.already_settled) == (usd(0.5), False, False)
    assert ledger.job_hold("job") == usd(4.5)
    again = _settle(ledger, "job", "c1", 4.0, input_tokens=999, output_tokens=999)
    assert (again.job_cost, again.already_settled) == (usd(0.5), True)


def test_a_call_that_cost_more_than_its_budget_asks_for_a_cancel(ledger):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=2.0)
    ledger.reserve_job_budget("job", limits)
    _call(ledger, "job", "c1", 2.0, limits)
    result = _settle(ledger, "job", "c1", 2.5)
    assert result.over_budget is True
    assert ledger.job_hold("job") == usd(0.0)


def test_settling_an_unknown_call_or_negative_usage_is_refused(ledger):
    with pytest.raises(UsageError):
        _settle(ledger, "job", "missing", 0.0, input_tokens=0, output_tokens=0)
    with pytest.raises(UsageError):
        ledger.reserve_call("job", "c", worst_case_micros=-1, limits=CostLimits())
    with pytest.raises(UsageError):
        CostLimits.from_dollars(month_cap=-1.0)


def test_a_priced_call_with_no_budget_is_refused_with_the_reason(ledger):
    assert _call(ledger, "ghost", "c1", 1.0, CostLimits.from_dollars(month_cap=50.0)).reason == REFUSED_NO_BUDGET


def test_a_kill_between_the_call_and_its_settlement_counts_the_worst_case(session, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=10.0)
    first = UsageLedger(session, clock=clock)
    first.reserve_job_budget("job", limits)
    _call(first, "job", "c1", 6.0, limits)
    # The process dies here. A new process, a new ledger, no settlement ever arrives.
    session.remove()
    restarted = UsageLedger(session, clock=clock)
    restarted.release_job("job")
    assert restarted.month_committed() == usd(6.0)
    retry = restarted.reserve_job_budget("job", limits)
    assert retry.amount == usd(4.0)  # the retry cannot spend the 6 again
    assert not _call(restarted, "job", "c2", 5.0, limits).admitted
    assert restarted.month_remainder(limits) == usd(0.0)


def test_a_spent_month_still_runs_free_connections_and_skips_priced_ones(ledger):
    limits = CostLimits.from_dollars(month_cap=5.0, job_cap=5.0)
    ledger.reserve_job_budget("spender", limits)
    _call(ledger, "spender", "c1", 5.0, limits)
    _settle(ledger, "spender", "c1", 5.0)
    ledger.release_job("spender")

    budget = ledger.reserve_job_budget("local", limits)
    assert (budget.status, budget.amount) == (MONTH_SPENT, 0)
    priced = _call(ledger, "local", "paid", 0.01, limits, connection_id=2, backend_type="openrouter")
    assert (priced.admitted, priced.reason) == (False, REFUSED_MONTH_SPENT)
    free = _call(ledger, "local", "free", 0.0, limits, connection_id=1, backend_type="openai_compatible")
    assert free.admitted
    _settle(ledger, "local", "free", 0.0, input_tokens=900, output_tokens=300)
    assert ledger.job_cost("local") == usd(0.0)
    assert ledger.month_remainder(limits) == usd(0.0)


def test_a_free_call_runs_without_any_budget_row(ledger):
    assert _call(ledger, "free-job", "c1", 0.0, CostLimits.from_dollars(month_cap=1.0)).admitted


# ---------------------------------------------------------------- concurrency

def test_concurrent_jobs_never_reserve_the_same_money(session, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=4.0)
    results, errors = [], []
    gate = threading.Barrier(8)

    def work(number):
        try:
            gate.wait()
            budget = UsageLedger(session, clock=clock).reserve_job_budget(f"job-{number}", limits)
            results.append(budget.amount or 0)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)
        finally:
            session.remove()

    threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sum(results) == usd(10.0)
    assert max(results) <= usd(4.0)
    assert UsageLedger(session, clock=clock).month_committed() == usd(10.0)


def test_concurrent_calls_of_one_job_never_pass_its_budget(session, clock):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=5.0)
    UsageLedger(session, clock=clock).reserve_job_budget("job", limits)
    admitted, errors = [], []
    gate = threading.Barrier(8)

    def work(number):
        try:
            gate.wait()
            reservation = UsageLedger(session, clock=clock).reserve_call(
                "job", f"call-{number}", worst_case_micros=usd(1.0), limits=limits)
            admitted.append(reservation.admitted)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)
        finally:
            session.remove()

    threads = [threading.Thread(target=work, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert admitted.count(True) == 5
    assert UsageLedger(session, clock=clock).job_cost("job") == usd(5.0)


# ------------------------------------------------------------------ month rollover

def test_a_calls_cost_counts_in_the_month_it_was_made(ledger, clock):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=10.0)
    ledger.reserve_job_budget("job", limits)
    _call(ledger, "job", "c1", 4.0, limits)
    _settle(ledger, "job", "c1", 4.0)
    clock.moment = datetime(2026, 12, 1, 0, 5, 0)
    assert ledger.month_committed("2026-11") == usd(10.0)  # the job still holds its budget there, until it rolls
    ledger.roll_month(limits)
    assert ledger.month_committed("2026-11") == usd(4.0)
    assert ledger.month_committed("2026-12") == usd(6.0)
    assert month_key(clock.moment) == "2026-12"


def test_rollover_gives_the_running_job_only_what_the_new_month_can_cover(session, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=10.0)
    ledger = UsageLedger(session, clock=clock)
    ledger.reserve_job_budget("running", limits)
    _call(ledger, "running", "c1", 2.0, limits)
    _settle(ledger, "running", "c1", 2.0)
    clock.moment = datetime(2026, 12, 1)
    # Another job takes most of the new month before the running one rolls.
    other = ledger.reserve_job_budget("other", CostLimits.from_dollars(month_cap=10.0, job_cap=7.0))
    assert other.amount == usd(7.0)
    assert ledger.roll_month(limits) == 1
    assert ledger.job_hold("running") == usd(3.0)  # 10 less the 7 the other job holds, not the 8 it had left
    assert ledger.month_committed("2026-12") == usd(10.0)
    assert ledger.month_committed("2026-11") == usd(2.0)
    assert ledger.roll_month(limits) == 0


def test_a_call_after_the_month_changes_rolls_the_budget_first(ledger, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=10.0)
    ledger.reserve_job_budget("job", limits)
    clock.moment = datetime(2026, 12, 3)
    assert _call(ledger, "job", "c1", 4.0, limits).admitted
    assert ledger.month_committed("2026-11") == usd(0.0)
    assert ledger.month_committed("2026-12") == usd(10.0)


def test_settling_an_old_month_call_after_rollover_does_not_raise_the_new_months_hold(session, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=10.0)
    ledger = UsageLedger(session, clock=clock)
    ledger.reserve_job_budget("old", limits)
    assert _call(ledger, "old", "c1", 10.0, limits).admitted  # the whole budget is out on one call
    clock.moment = datetime(2026, 12, 1)
    assert ledger.roll_month(limits) == 1
    assert ledger.job_hold("old") == 0  # nothing was unspent, so nothing moved into December
    # Another job reserves all of December while the old call is still unsettled.
    assert ledger.reserve_job_budget("new", limits).amount == usd(10.0)
    _settle(ledger, "old", "c1", 1.0)
    # The nine dollars the call did not use belong to November, where the call was made.
    assert ledger.job_hold("old") == 0
    assert ledger.month_committed("2026-12") == usd(10.0)
    assert ledger.month_remainder(limits, "2026-12") == 0
    assert ledger.month_committed("2026-11") == usd(1.0)


def test_settling_a_call_made_in_the_budgets_own_month_still_returns_the_unused_part(ledger, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=10.0)
    ledger.reserve_job_budget("job", limits)
    assert _call(ledger, "job", "c1", 6.0, limits).admitted
    _settle(ledger, "job", "c1", 2.0)
    assert ledger.job_hold("job") == usd(8.0)
    assert ledger.month_committed("2026-11") == usd(10.0)


def test_a_budget_asked_for_again_in_a_new_month_moves_with_it(ledger, clock):
    limits = CostLimits.from_dollars(month_cap=10.0, job_cap=6.0)
    ledger.reserve_job_budget("job", limits)
    clock.moment = datetime(2026, 12, 3)
    again = ledger.reserve_job_budget("job", limits)
    assert (again.month, again.amount) == ("2026-12", usd(6.0))


# ---------------------------------------------------------------------- history

def _open(history, job="job-1", **kwargs):
    defaults = dict(media_type="episode", media_id=7, media_title="Show S01E01", arr_instance_id=3,
                    source_language="en", target_language="hu", plugin_id="ai_translator",
                    plugin_version="2.2.0", engine_instance_id=1)
    defaults.update(kwargs)
    return history.open_job(job, **defaults)


def test_a_job_row_carries_what_the_job_was_for(history):
    history_id = _open(history)
    row = history.get(history_id)
    assert (row["job_id"], row["status"], row["arr_instance_id"], row["plugin_version"]) == (
        "job-1", "running", 3, "2.2.0")
    assert (row["media_type"], row["media_id"], row["source_language"], row["target_language"]) == (
        "episode", 7, "en", "hu")
    assert row["connections"] == [] and row["cost_micros"] == 0
    assert row["started_at"] == "2026-11-15T12:00:00" and row["finished_at"] is None
    assert _open(history) == history_id  # another attempt reuses the row


def test_a_failover_is_attributed_to_both_connections(ledger, history):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=10.0)
    _open(history)
    ledger.reserve_job_budget("job-1", limits)
    # Connection 1 fails over before it produces a single batch.
    history.note_attempt("job-1", connection_id=1, backend_type="openai_compatible", model="local-7b",
                         outcome="failed_over", error_class="unreachable")
    # What it spent on the failed call still counts, settled after the failure was seen.
    _call(ledger, "job-1", "a1", 0.2, limits, connection_id=1, backend_type="openai_compatible", model="local-7b")
    _settle(ledger, "job-1", "a1", 0.0, input_tokens=500, output_tokens=0)
    # Connection 2 takes over and translates two batches.
    history.note_attempt("job-1", connection_id=2, backend_type="openrouter", model="vendor/model")
    for call in ("b1", "b2"):
        _call(ledger, "job-1", call, 1.0, limits, connection_id=2, backend_type="openrouter",
              model="vendor/model")
        _settle(ledger, "job-1", call, 0.25, input_tokens=1000, output_tokens=400)
    history.record_batches("job-1", connection_id=2, model="vendor/model", count=2)
    history.finish_job("job-1", "completed")

    row = history.get_by_job("job-1")
    assert (row["status"], row["failovers"], row["finished_at"]) == ("completed", 1, "2026-11-15T12:00:00")
    assert (row["input_tokens"], row["output_tokens"], row["cost_micros"]) == (2500, 800, usd(0.5))
    first, second = row["connections"]
    assert (first["connection_id"], first["backend_type"], first["models"]) == (1, "openai_compatible", ["local-7b"])
    assert (first["failed_over"], first["batches"], first["attempts"], first["last_error_class"]) == (
        True, 0, 1, "unreachable")
    assert (first["input_tokens"], first["cost_micros"]) == (500, 0)
    assert (second["connection_id"], second["backend_type"], second["models"]) == (2, "openrouter", ["vendor/model"])
    assert (second["batches"], second["input_tokens"], second["output_tokens"], second["cost_micros"]) == (
        2, 2000, 800, usd(0.5))
    assert second["failed_over"] is False


def test_a_connection_with_only_a_killed_call_still_gets_its_row(ledger, history):
    limits = CostLimits.from_dollars(month_cap=100.0, job_cap=10.0)
    _open(history)
    ledger.reserve_job_budget("job-1", limits)
    _call(ledger, "job-1", "lost", 2.0, limits, connection_id=9, backend_type="gemini", model="flash")
    history.finish_job("job-1", "failed", error_class="killed")
    (only,) = history.get_by_job("job-1")["connections"]
    assert (only["connection_id"], only["backend_type"], only["cost_micros"]) == (9, "gemini", usd(2.0))  # worst case counts


def test_a_declining_and_a_taking_over_engine_are_recorded(history):
    _open(history)
    history.note_declined("job-1", instance_id=1, reason="language not supported", taken_over_by=2)
    history.finish_job("job-1", "completed")
    row = history.get_by_job("job-1")
    assert (row["declined_by_instance_id"], row["declined_reason"], row["taken_over_by_instance_id"]) == (
        1, "language not supported", 2)


def test_an_error_detail_is_scrubbed_and_bounded(history):
    _open(history)
    history.finish_job("job-1", "failed", error_class="auth_required",
                       error_detail="rejected Bearer abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 900)
    row = history.get_by_job("job-1")
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in row["error_detail"]
    assert len(row["error_detail"]) <= 500
    assert row["error_class"] == "auth_required"


def test_a_job_that_will_be_retried_has_no_end_time(history):
    _open(history)
    history.finish_job("job-1", "awaiting_retry", error_class="quota_exhausted")
    assert history.get_by_job("job-1")["finished_at"] is None
    history.finish_job("job-1", "failed")
    assert history.get_by_job("job-1")["finished_at"] is not None


def test_history_refuses_what_it_cannot_record(history):
    with pytest.raises(HistoryError):
        history.open_job("")
    with pytest.raises(HistoryError):
        history.finish_job("nobody", "completed")
    _open(history)
    with pytest.raises(HistoryError):
        history.finish_job("job-1", "running")
    with pytest.raises(HistoryError):
        history.finish_job("job-1", "sideways")
    with pytest.raises(HistoryError):
        history.note_attempt("job-1", connection_id=1, outcome="exploded")
    with pytest.raises(HistoryError):
        history.record_batches("job-1", connection_id=1, count=0)


def test_usage_recorded_before_the_row_exists_is_picked_up_when_it_opens(ledger, history):
    limits = CostLimits()
    ledger.reserve_job_budget("early", limits)
    _call(ledger, "early", "c1", 1.5, limits, connection_id=4, backend_type="gemini")
    _open(history, job="early")
    history.refresh_totals("early")
    row = history.get_by_job("early")
    assert row["cost_micros"] == usd(1.5) and row["connections"][0]["connection_id"] == 4


def test_the_list_pages_newest_first_and_filters(history):
    for number in range(5):
        _open(history, job=f"job-{number}", arr_instance_id=number % 2)
    history.finish_job("job-3", "failed", error_class="validation")
    rows, total = history.list_jobs(start=0, length=2)
    assert (total, [r["job_id"] for r in rows]) == (5, ["job-4", "job-3"])
    rows, total = history.list_jobs(start=4, length=10)
    assert (total, [r["job_id"] for r in rows]) == (5, ["job-0"])
    rows, total = history.list_jobs(status="failed")
    assert (total, [r["job_id"] for r in rows]) == (1, ["job-3"])
    rows, total = history.list_jobs(arr_instance_id=0)
    assert (total, [r["job_id"] for r in rows]) == (3, ["job-4", "job-2", "job-0"])
    assert history.get(99999) is None


def test_deleting_a_history_row_removes_its_connection_rows(session, history):
    from sqlalchemy import func, select

    from app.database import TableTranslationHistory, TableTranslationHistoryConnection

    history_id = _open(history)
    history.note_attempt("job-1", connection_id=1, backend_type="gemini")
    session.delete(session.get(TableTranslationHistory, history_id))
    session.commit()
    assert session.execute(select(func.count()).select_from(TableTranslationHistoryConnection)).scalar() == 0


# ------------------------------------------------------------------- money is whole micro-dollars

def test_dollars_become_whole_micro_dollars_charges_up_and_caps_down():
    assert to_micros(1) == 1_000_000
    assert to_micros("0.1") + to_micros("0.2") == to_micros("0.3")  # the sum floats get wrong
    assert to_micros(0.0000004) == 1  # a charge never rounds down to free
    assert to_micros(0.0000004, charge=False) == 0  # a cap never rounds up
    assert CostLimits.from_dollars(month_cap=0.0000019).month_cap == 1
    for bad in (-1, "free", float("nan"), None):
        with pytest.raises(UsageError):
            to_micros(bad)


def test_a_worst_case_is_tokens_times_price_rounded_up():
    assert worst_case_micros(1000, 500, 0.15, 0.6) == 450  # $0.15 and $0.60 per million tokens
    assert worst_case_micros(1, 0, 0.15, 0.6) == 1  # a fraction of a micro-dollar still counts
    assert worst_case_micros(5000, 5000, 0, 0) == 0  # a free local server
    with pytest.raises(UsageError):
        worst_case_micros(1, 1, -1, 0)


def test_the_ledger_takes_whole_micro_dollars_and_refuses_a_float(ledger):
    with pytest.raises(UsageError):
        CostLimits(month_cap=1.5)
    with pytest.raises(UsageError):
        ledger.reserve_call("job", "c", worst_case_micros=0.5, limits=CostLimits())
    with pytest.raises(UsageError):
        ledger.settle_call("job", "c", input_tokens=1, output_tokens=1, actual_micros=True)


def test_many_small_calls_add_up_exactly(ledger):
    limits = CostLimits.from_dollars(month_cap=1.0, job_cap=1.0)
    ledger.reserve_job_budget("job", limits)
    for number in range(10):
        assert ledger.reserve_call("job", f"c{number}", worst_case_micros=to_micros("0.1"), limits=limits).admitted
        ledger.settle_call("job", f"c{number}", input_tokens=1, output_tokens=1, actual_micros=to_micros("0.1"))
    assert ledger.job_cost("job") == to_micros(1)
    assert ledger.job_hold("job") == 0
    assert not ledger.reserve_call("job", "one-more", worst_case_micros=1, limits=limits).admitted


# ------------------------------------------------------------ writers outside the ledger

def test_ledger_writes_survive_another_connection_committing_in_between(session, clock, tmp_path):
    """On SQLite in WAL mode a plain BEGIN fails the moment anyone else commits between a read and a write."""
    import sqlalchemy as sa

    if session.get_bind().dialect.name != "sqlite":
        pytest.skip("SQLite only")
    bind = session.get_bind()
    with bind.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE other (id INTEGER PRIMARY KEY, value INTEGER)")
    stop = threading.Event()
    failures = []

    def foreign_writer():
        # Any other part of the application committing, on any table, at any time.
        while not stop.is_set():
            try:
                with bind.connect() as connection:
                    connection.execute(sa.text("INSERT INTO other (value) VALUES (1)"))
                stop.wait(0.005)
            except Exception as error:  # pragma: no cover - reported below
                failures.append(error)
                return

    writer = threading.Thread(target=foreign_writer)
    writer.start()
    try:
        ledger = UsageLedger(session, clock=clock)
        limits = CostLimits.from_dollars(month_cap=1000, job_cap=500)
        for number in range(20):
            job = f"job-{number}"
            ledger.reserve_job_budget(job, limits)
            assert ledger.reserve_call(job, "c1", worst_case_micros=usd(1.0), limits=limits).admitted
            ledger.settle_call(job, "c1", input_tokens=1, output_tokens=1, actual_micros=usd(0.5))
            ledger.release_job(job)
    finally:
        stop.set()
        writer.join()
    assert not failures
    assert ledger.month_committed() == usd(10.0)


# ------------------------------------------------------------- stale holds and late caps

def test_release_stale_gives_back_the_hold_of_a_job_that_is_no_longer_alive(ledger, clock):
    limits = CostLimits.from_dollars(month_cap=10, job_cap=10)
    ledger.reserve_job_budget("dead", limits)
    assert ledger.reserve_job_budget("alive", limits).status == WAIT
    assert ledger.release_stale({"alive", 7}) == 1
    assert ledger.job_hold("dead") == 0
    assert ledger.reserve_job_budget("alive", limits).amount == usd(10)
    assert ledger.release_stale({"alive"}) == 0  # nothing left to release


def test_release_stale_takes_ids_in_any_form_and_an_empty_set_releases_everything(ledger):
    limits = CostLimits.from_dollars(month_cap=100, job_cap=5)
    ledger.reserve_job_budget("12", limits)
    ledger.reserve_job_budget("13", limits)
    assert ledger.release_stale([12]) == 1  # a job table's integer id matches its text job id
    assert ledger.job_hold("12") == usd(5) and ledger.job_hold("13") == 0
    assert ledger.release_stale([]) == 1


def test_a_stale_hold_does_not_travel_into_the_next_month(ledger, clock):
    limits = CostLimits.from_dollars(month_cap=10, job_cap=10)
    ledger.reserve_job_budget("crashed", limits)
    ledger.release_stale(set())
    clock.moment = datetime(2026, 12, 1)
    assert ledger.roll_month(limits) == 0
    assert ledger.month_committed("2026-12") == 0
    assert ledger.reserve_job_budget("next", limits).amount == usd(10)


def test_a_stale_release_keeps_the_calls_the_job_never_settled(ledger):
    limits = CostLimits.from_dollars(month_cap=10, job_cap=10)
    ledger.reserve_job_budget("crashed", limits)
    _call(ledger, "crashed", "c1", 4.0, limits)
    ledger.release_stale(set())
    assert ledger.month_committed() == usd(4)


def test_a_cap_set_while_an_uncapped_job_runs_binds_it(ledger):
    ledger.reserve_job_budget("job", CostLimits())
    assert _call(ledger, "job", "c1", 3.0, CostLimits()).admitted
    capped = CostLimits.from_dollars(month_cap=10, job_cap=5)
    assert _call(ledger, "job", "c2", 2.0, capped).admitted  # 5 less the 3 already counted leaves 2
    refused = _call(ledger, "job", "c3", 0.5, capped)
    assert (refused.admitted, refused.reason) == (False, REFUSED_JOB_BUDGET)


def test_a_month_cap_set_late_that_is_already_spent_refuses_a_priced_call(ledger):
    ledger.reserve_job_budget("job", CostLimits())
    _call(ledger, "job", "c1", 3.0, CostLimits())
    refused = _call(ledger, "job", "c2", 0.5, CostLimits.from_dollars(month_cap=3))
    assert (refused.admitted, refused.reason) == (False, REFUSED_MONTH_SPENT)
    assert _call(ledger, "job", "free", 0.0, CostLimits.from_dollars(month_cap=3)).admitted


def test_the_equal_share_is_the_same_for_every_job_up_to_the_limit(ledger):
    limits = CostLimits.from_dollars(month_cap=10, translation_limit=2)
    assert ledger.reserve_job_budget("a", limits).amount == usd(5)
    assert ledger.reserve_job_budget("b", limits).amount == usd(5)
    assert ledger.reserve_job_budget("c", limits).status == WAIT  # no slot and no money left


def test_the_translation_limit_is_a_whole_number():
    for bad in (1.5, True, "2", 0):
        with pytest.raises(UsageError):
            CostLimits(translation_limit=bad)


def test_a_budget_row_held_by_the_caller_is_refreshed_not_trusted(session, clock):
    from app.database import TableTranslationBudget

    limits = CostLimits.from_dollars(month_cap=100, job_cap=2)
    ledger = UsageLedger(session, clock=clock)
    ledger.reserve_job_budget("job", limits)
    held = session.query(TableTranslationBudget).filter_by(job_id="job").one()
    other = session.session_factory()  # a second session, as another thread would have
    try:
        other.query(TableTranslationBudget).filter_by(job_id="job").update({"budget_micros": usd(8)})
        other.commit()
    finally:
        other.close()
    assert held.budget_micros == usd(2)  # the held row is stale, as an ORM row can be
    assert ledger.job_hold("job") == usd(8)  # the ledger does not read from it


def test_error_classes_are_scrubbed_and_bounded(history):
    _open(history)
    history.note_attempt("job-1", connection_id=1, outcome="failed",
                         error_class="Bearer abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 400)
    history.finish_job("job-1", "failed", error_class="Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    row = history.get_by_job("job-1")
    assert "abcdefghijklmnopqrstuvwxyz" not in row["error_class"]
    assert "abcdefghijklmnopqrstuvwxyz" not in row["connections"][0]["last_error_class"]
    assert len(row["connections"][0]["last_error_class"]) <= 100

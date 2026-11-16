"""The translation job type: join, supersede, refuse, checkpoints, their deletion paths and the deadlines."""
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from subtitles.tools.translate.runtime import jobs as jobs_module
from subtitles.tools.translate.runtime.jobs import (
    ACTIVE_STATES, AWAITING_RETRY, CANCELLED, COMPLETED, DEADLINE_EXCEEDED, FAILED, INACTIVITY_TIMEOUT,
    INTERRUPTED, QUEUED, RUNNING, SUPERSEDED, CheckpointKey, DeadlineAction, DeadlineWatch, Outcome,
    TranslationJobError, TranslationJobs, line_digest, normalize_source_path, target_key)

SOURCE = "/media/film/film.en.srt"
STEP = dict(source_digest="d1", engine_instance_id=1, settings_revision=1, release_digest="r1")


class FakeClock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 11, 7, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class FakeQueue:
    """The in-memory jobs queue as the job type sees it: a set of pending ids and a status table."""

    def __init__(self):
        self.pending = set()
        self.statuses = {}
        self.removed = []

    def remove_pending(self, queue_job_id):
        if queue_job_id in self.pending:
            self.pending.discard(queue_job_id)
            self.removed.append(queue_job_id)
            return True
        return False

    def status(self, queue_job_id):
        return self.statuses.get(queue_job_id)


def _production_sqlite_engine(path, busy_timeout=60000):
    """Built the way bazarr/app/database.py builds the SQLite engine: no pool, AUTOCOMMIT, WAL, busy_timeout.

    Locking behaves as in production; only the fsync is relaxed, to keep the suite quick.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool, isolation_level="AUTOCOMMIT")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=OFF")  # production uses FULL; durability is not under test
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")
        cursor.close()

    return engine


def _make_session(kind, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import Base, TableTranslatorCheckpoint, TableTranslatorJob
    tables = [TableTranslatorJob.__table__, TableTranslatorCheckpoint.__table__]
    if kind == "sqlite":
        engine = _production_sqlite_engine(tmp_path / "jobs.db")
        Base.metadata.create_all(engine, tables=tables)
        return engine, scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)), None
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    import sqlalchemy as sa
    from sqlalchemy import event
    schema = "translator_jobs_" + uuid4().hex
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

    Base.metadata.create_all(engine, tables=tables)
    return engine, scoped_session(sessionmaker(bind=engine)), (admin, schema)


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


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def queue():
    return FakeQueue()


@pytest.fixture
def jobs(session, clock, queue):
    return TranslationJobs(session, clock=clock, queue=queue)


def _running(jobs, **overrides):
    result = jobs.request(SOURCE, "fr")
    assert jobs.start(result.job_id, **{**STEP, **overrides})
    return result.job_id


def _checkpoints(session):
    from app.database import TableTranslatorCheckpoint
    return session.query(TableTranslatorCheckpoint).count()


# ----------------------------------------------------------------------- keys

def test_the_digest_ignores_line_endings_trailing_spaces_and_a_bom_but_not_edits():
    base = line_digest(["Hello", "World\nsecond line"])
    assert line_digest(["﻿Hello  ", "World\r\nsecond line "]) == base
    assert line_digest(["Hello", "World\rsecond line"]) == base
    assert line_digest(["Hello", "World\nsecond lines"]) != base
    assert line_digest(["Hello", "World", "second line"]) != base  # one line split in two
    assert line_digest(["HelloWorld", "\nsecond line"]) != base  # boundaries count


def test_a_target_names_the_flags_that_make_another_file():
    assert target_key("fr") == "fr"
    assert target_key("fr", forced=True) == "fr:forced"
    assert target_key("pt-BR", hi=True) == "pt-BR:hi"
    assert target_key("fr", forced=True, hi=True) == "fr:forced:hi"
    with pytest.raises(TranslationJobError):
        target_key("  ")


def test_source_paths_are_normalized_and_must_exist_as_text():
    assert normalize_source_path("/media//film/../film/a.srt") == "/media/film/a.srt"
    with pytest.raises(TranslationJobError):
        normalize_source_path("")
    with pytest.raises(TranslationJobError):
        normalize_source_path(None)


# ------------------------------------------------------------ join, supersede, refuse

def test_a_request_creates_one_queued_job(jobs):
    result = jobs.request(SOURCE, "fr")
    assert result.outcome is Outcome.CREATED
    job = jobs.get(result.job_id)
    assert (job.state, job.source_path, job.target, job.manual) == (QUEUED, SOURCE, "fr", 0)


def test_a_second_request_joins_the_job_in_every_active_state(jobs, queue):
    first = jobs.request(SOURCE, "fr").job_id
    for state_setup in (lambda: None,
                        lambda: jobs.start(first, **STEP),
                        lambda: jobs.await_retry(first, "rate_limited")):
        state_setup()
        joined = jobs.request("/media/film/../film/film.en.srt", "fr")
        assert (joined.outcome, joined.job_id) == (Outcome.JOINED, first)


def test_other_targets_and_sources_get_their_own_jobs(jobs):
    ids = {jobs.request(SOURCE, "fr").job_id, jobs.request(SOURCE, "de").job_id,
           jobs.request(SOURCE, "fr:forced").job_id, jobs.request("/media/other.srt", "fr").job_id}
    assert len(ids) == 4


def test_an_ended_job_does_not_capture_the_next_request(jobs):
    first = _running(jobs)
    assert jobs.complete(first)
    second = jobs.request(SOURCE, "fr")
    assert second.outcome is Outcome.CREATED and second.job_id != first


def test_a_manual_pick_replaces_a_queued_job_and_pulls_it_off_the_queue(jobs, queue, session):
    first = jobs.request(SOURCE, "fr").job_id
    jobs.attach_queue_job(first, 41)
    queue.pending.add(41)
    # Work from an earlier run of this source is thrown away with the replaced job.
    jobs.checkpoints.save_batch(CheckpointKey(SOURCE, "d1", "fr", 1, 1, "r1"), 0, ["a"])

    result = jobs.request(SOURCE, "fr", manual=True, pinned_connection_id=7)

    assert (result.outcome, result.replaced_job_id) == (Outcome.REPLACED, first)
    assert queue.removed == [41]
    assert jobs.get(first).state == SUPERSEDED and jobs.get(first).finished_at is not None
    new = jobs.get(result.job_id)
    assert (new.state, new.manual, new.pinned_connection_id) == (QUEUED, 1, 7)
    assert _checkpoints(session) == 0
    assert jobs.active_for(SOURCE, "fr").id == result.job_id


def test_a_manual_pick_replaces_a_job_awaiting_a_retry_and_drops_its_checkpoints(jobs, session):
    first = _running(jobs)
    jobs.save_batch(first, 0, ["a", "b"])
    jobs.await_retry(first, "quota_exhausted", "out of credit")
    assert _checkpoints(session) == 1

    result = jobs.request(SOURCE, "fr", manual=True)

    assert (result.outcome, result.replaced_job_id) == (Outcome.REPLACED, first)
    assert jobs.get(first).state == SUPERSEDED
    assert _checkpoints(session) == 0


def test_a_manual_pick_is_refused_with_a_reason_while_the_job_runs(jobs, session):
    first = _running(jobs)
    jobs.save_batch(first, 0, ["a"])

    result = jobs.request(SOURCE, "fr", manual=True)

    assert (result.outcome, result.job_id) == (Outcome.REFUSED, first)
    assert result.reason == jobs_module.RUNNING_REFUSAL
    assert jobs.get(first).state == RUNNING
    assert _checkpoints(session) == 1  # nothing was touched


def test_a_manual_pick_is_refused_when_the_queued_job_has_just_started(jobs, queue):
    first = jobs.request(SOURCE, "fr").job_id
    jobs.attach_queue_job(first, 41)
    queue.statuses[41] = "running"  # started between the read and the pull back

    result = jobs.request(SOURCE, "fr", manual=True)

    assert (result.outcome, result.job_id) == (Outcome.REFUSED, first)
    assert jobs.get(first).state == QUEUED


def test_a_queued_record_whose_queue_job_is_gone_is_replaced(jobs, queue):
    first = jobs.request(SOURCE, "fr").job_id
    jobs.attach_queue_job(first, 41)  # the queue knows nothing of 41 any more

    result = jobs.request(SOURCE, "fr", manual=True)

    assert (result.outcome, result.replaced_job_id) == (Outcome.REPLACED, first)


def test_a_non_manual_request_never_replaces(jobs):
    first = jobs.request(SOURCE, "fr", manual=True).job_id
    again = jobs.request(SOURCE, "fr")
    assert (again.outcome, again.job_id) == (Outcome.JOINED, first)


def test_two_requests_racing_for_a_new_job_end_up_on_one(jobs, session, monkeypatch):
    first = jobs.request(SOURCE, "fr").job_id
    real = jobs.active_for
    calls = []

    def stale_once(path, target):
        calls.append(1)
        return None if len(calls) == 1 else real(path, target)

    monkeypatch.setattr(jobs, "active_for", stale_once)
    result = jobs.request(SOURCE, "fr")  # the first read said "no job", the insert hit the index
    assert (result.outcome, result.job_id) == (Outcome.JOINED, first)
    from app.database import TableTranslatorJob
    assert session.query(TableTranslatorJob).count() == 1


def test_a_manual_pick_that_loses_a_race_with_a_start_is_refused(jobs, monkeypatch):
    first = jobs.request(SOURCE, "fr").job_id
    real = jobs.active_for
    started = []

    def starts_after_the_read(path, target):
        row = real(path, target)
        if not started:
            started.append(jobs.start(first, **STEP))
        return row

    monkeypatch.setattr(jobs, "active_for", starts_after_the_read)
    result = jobs.request(SOURCE, "fr", manual=True)
    assert result.outcome is Outcome.REFUSED and result.job_id == first
    assert jobs.get(first).state == RUNNING


# ------------------------------------------------------------------ life cycle

def test_a_job_that_was_replaced_cannot_start(jobs):
    first = jobs.request(SOURCE, "fr").job_id
    jobs.request(SOURCE, "fr", manual=True)
    assert jobs.start(first, **STEP) is False  # the dispatcher must not translate for it
    assert jobs.get(first).state == SUPERSEDED


def test_start_records_the_engine_and_the_time(jobs, clock):
    job_id = jobs.request(SOURCE, "fr").job_id
    clock.advance(minutes=3)
    assert jobs.start(job_id, **STEP)
    job = jobs.get(job_id)
    assert (job.state, job.engine_instance_id, job.settings_revision, job.release_digest, job.source_digest) == (
        RUNNING, 1, 1, "r1", "d1")
    assert job.started_at == clock.now
    assert jobs.start(job_id, **STEP) is False  # already running


def test_an_ended_job_cannot_be_ended_again_or_revived(jobs):
    job_id = _running(jobs)
    assert jobs.complete(job_id)
    assert not jobs.cancel(job_id) and not jobs.fail(job_id, "x") and not jobs.await_retry(job_id, "x")
    assert jobs.get(job_id).state == COMPLETED


def test_recorded_error_messages_are_bounded_and_scrubbed(jobs, monkeypatch):
    import secret_store.scrub as scrub
    monkeypatch.setattr(scrub, "scrub_text", lambda text: text.replace("sk-secret", "***"))
    job_id = _running(jobs)
    jobs.fail(job_id, "auth_required", "bad key sk-secret " + "x" * 2000)
    job = jobs.get(job_id)
    assert "sk-secret" not in job.error_message and len(job.error_message) == jobs_module.MAX_ERROR_LENGTH
    assert job.error_class == "auth_required"


def test_an_error_message_equal_to_a_short_secret_is_masked_whole(jobs):
    import secret_store.scrub as scrub
    scrub.register_secret_values(["ab12"])
    try:
        job_id = _running(jobs)
        jobs.fail(job_id, "auth_required", "ab12")
        assert jobs.get(job_id).error_message == scrub.MASK
    finally:
        scrub.clear_secret_values()


# ----------------------------------------------------------------- checkpoints

def test_a_batch_is_kept_and_read_back_in_order(jobs):
    job_id = _running(jobs)
    assert jobs.finished_batches(job_id) == {}
    assert jobs.save_batch(job_id, 0, ["a", "b"]) and jobs.save_batch(job_id, 1, ["c"])
    assert jobs.save_batch(job_id, 0, ["a2", "b2"])  # a re-run replaces its batch
    assert jobs.finished_batches(job_id) == {0: ["a2", "b2"], 1: ["c"]}


def test_nothing_is_kept_for_a_job_that_is_not_running(jobs, session):
    job_id = jobs.request(SOURCE, "fr").job_id
    assert jobs.save_batch(job_id, 0, ["a"]) is False  # queued
    assert _checkpoints(session) == 0
    jobs.start(job_id, **STEP)
    jobs.cancel(job_id)
    assert jobs.save_batch(job_id, 0, ["late"]) is False  # stopped while a batch was still in flight
    assert _checkpoints(session) == 0


def test_the_key_has_no_connection_in_it():
    assert [name for name in CheckpointKey.__dataclass_fields__] == [
        "source_path", "source_digest", "target", "engine_instance_id", "settings_revision", "release_digest"]


def test_a_failover_between_connections_resumes_on_the_same_checkpoint(jobs):
    """The connection that served a batch is not recorded, so the next one picks up after it."""
    job_id = _running(jobs)
    jobs.save_batch(job_id, 0, ["from the first connection"])
    jobs.save_batch(job_id, 1, ["from the second connection"])
    assert sorted(jobs.finished_batches(job_id)) == [0, 1]


def test_an_edited_source_starts_afresh_and_drops_the_old_checkpoints(jobs, session):
    first = _running(jobs, source_digest="before")
    jobs.save_batch(first, 0, ["old"])
    jobs.await_retry(first, "rate_limited")
    assert jobs.requeue(first, None)

    assert jobs.start(first, **{**STEP, "source_digest": "after"})

    assert _checkpoints(session) == 0
    assert jobs.finished_batches(first) == {}


def test_a_changed_revision_or_release_of_the_same_engine_drops_its_checkpoints(jobs, session):
    for change in ({"settings_revision": 2}, {"release_digest": "r2"}):
        first = _running(jobs)
        jobs.save_batch(first, 0, ["old"])
        jobs.await_retry(first, "rate_limited")
        jobs.requeue(first)
        jobs.start(first, **{**STEP, **change})
        assert jobs.finished_batches(first) == {}
        jobs.cancel(first)
    assert _checkpoints(session) == 0


def test_another_engines_checkpoint_survives_a_start_and_is_not_resumed(jobs, session):
    """A fallback engine took over some batches. A retry on the first engine neither uses nor deletes them."""
    first = _running(jobs, engine_instance_id=2)
    jobs.save_batch(first, 0, ["by the fallback"])
    jobs.await_retry(first, "rate_limited")
    jobs.requeue(first)
    jobs.start(first, **{**STEP, "engine_instance_id": 1})
    assert jobs.finished_batches(first) == {}
    assert _checkpoints(session) == 1


def test_the_same_key_resumes_after_a_retry(jobs):
    first = _running(jobs)
    jobs.save_batch(first, 0, ["a"])
    jobs.save_batch(first, 1, ["b"])
    jobs.await_retry(first, "rate_limited")
    assert jobs.requeue(first, 9)
    assert jobs.start(first, **STEP)
    assert jobs.finished_batches(first) == {0: ["a"], 1: ["b"]}


# --------------------------------------------------------------- deletion paths

@pytest.mark.parametrize("ending,state", [
    (lambda jobs, job_id: jobs.complete(job_id), COMPLETED),
    (lambda jobs, job_id: jobs.cancel(job_id), CANCELLED),
    (lambda jobs, job_id: jobs.fail(job_id, "bad_output", "nope"), FAILED),
])
def test_completion_cancel_and_a_failure_without_retry_delete_the_checkpoints(jobs, session, ending, state):
    job_id = _running(jobs)
    jobs.save_batch(job_id, 0, ["a"])
    assert _checkpoints(session) == 1
    assert ending(jobs, job_id)
    assert jobs.get(job_id).state == state
    assert _checkpoints(session) == 0


@pytest.mark.parametrize("act", [
    lambda jobs, job_id: jobs.complete(job_id),
    lambda jobs, job_id: jobs.await_retry(job_id, "rate_limited"),
])
def test_the_job_and_its_checkpoints_change_in_one_transaction(jobs, session, monkeypatch, act):
    """A failure while the checkpoints are being changed leaves the job exactly as it was."""
    import sqlalchemy as sa

    job_id = _running(jobs)
    jobs.save_batch(job_id, 0, ["a"])
    broken = sa.text("UPDATE no_such_translator_table SET x = 1")
    monkeypatch.setattr(jobs.checkpoints, "_delete_source_stmt", staticmethod(lambda *args: broken))
    monkeypatch.setattr(jobs.checkpoints, "_expiry_stmt", staticmethod(lambda *args: broken))

    with pytest.raises(sa.exc.DBAPIError):
        act(jobs, job_id)

    session.expire_all()
    assert jobs.get(job_id).state == RUNNING and jobs.get(job_id).finished_at is None
    assert _checkpoints(session) == 1
    assert jobs.finished_batches(job_id) == {0: ["a"]}


def test_a_cancelled_queued_job_and_a_cancelled_waiting_job_end_too(jobs):
    queued = jobs.request(SOURCE, "fr").job_id
    assert jobs.cancel(queued) and jobs.get(queued).state == CANCELLED
    waiting = _running(jobs)
    jobs.await_retry(waiting, "rate_limited")
    assert jobs.cancel(waiting) and jobs.get(waiting).state == CANCELLED


def test_a_failure_awaiting_a_retry_keeps_its_checkpoints_for_seven_days(jobs, session, clock):
    job_id = _running(jobs)
    jobs.save_batch(job_id, 0, ["a"])
    retry_at = clock.now + timedelta(hours=1)
    assert jobs.await_retry(job_id, "rate_limited", "slow down", retry_at=retry_at)

    job = jobs.get(job_id)
    assert (job.state, job.retry_at, job.error_class, job.queue_job_id) == (AWAITING_RETRY, retry_at,
                                                                           "rate_limited", None)
    from app.database import TableTranslatorCheckpoint
    row = session.query(TableTranslatorCheckpoint).one()
    assert row.expires_at == clock.now + timedelta(days=7)

    clock.advance(days=6, hours=23)
    assert jobs.sweep() == 0 and _checkpoints(session) == 1
    clock.advance(hours=1)
    assert jobs.sweep() == 1 and _checkpoints(session) == 0
    assert jobs.get(job_id).state == AWAITING_RETRY  # the job waits on, and now starts from scratch


def test_a_retry_stops_the_countdown(jobs, session, clock):
    job_id = _running(jobs)
    jobs.save_batch(job_id, 0, ["a"])
    jobs.await_retry(job_id, "rate_limited")
    jobs.requeue(job_id)
    jobs.start(job_id, **STEP)
    clock.advance(days=30)
    assert jobs.sweep() == 0
    assert _checkpoints(session) == 1


def test_the_sweep_drops_checkpoints_nobody_owns_after_the_retention(jobs, session, clock):
    jobs.checkpoints.save_batch(CheckpointKey("/media/lost.srt", "d", "fr", 1, 1, ""), 0, ["a"])
    clock.advance(days=6)
    assert jobs.sweep() == 0
    clock.advance(days=2)
    assert jobs.sweep() == 1 and _checkpoints(session) == 0


def test_the_sweep_leaves_a_running_jobs_old_checkpoints_alone(jobs, session, clock):
    job_id = _running(jobs)
    jobs.save_batch(job_id, 0, ["a"])
    clock.advance(days=20)  # a long, slow job: still active, so still owned
    assert jobs.sweep() == 0 and _checkpoints(session) == 1


def test_due_retries_wait_for_their_time(jobs, clock):
    soon = _running(jobs)
    jobs.await_retry(soon, "rate_limited", retry_at=clock.now + timedelta(minutes=10))
    assert jobs.due_retries() == []
    clock.advance(minutes=10)
    assert jobs.due_retries() == [soon]


def test_a_restart_turns_what_was_in_flight_into_retries_that_resume(jobs, session, clock):
    running = _running(jobs)
    jobs.save_batch(running, 0, ["a"])
    queued = jobs.request("/media/other.srt", "fr").job_id
    jobs.attach_queue_job(queued, 5)
    done = jobs.request("/media/done.srt", "fr").job_id
    jobs.start(done, **STEP)
    jobs.complete(done)

    assert jobs.recover_interrupted() == [running, queued]

    for job_id in (running, queued):
        job = jobs.get(job_id)
        assert (job.state, job.error_class, job.queue_job_id, job.retry_at) == (AWAITING_RETRY, INTERRUPTED,
                                                                               None, clock.now)
    assert jobs.get(done).state == COMPLETED
    assert sorted(jobs.due_retries()) == [running, queued]
    # The retry resumes from the last finished batch.
    assert jobs.requeue(running, 77) and jobs.start(running, **STEP)
    assert jobs.finished_batches(running) == {0: ["a"]}
    assert jobs.recover_interrupted() == [running]  # and it is in flight again


# -------------------------------------------------------------------- deadlines

class Ticker:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_defaults_are_twelve_hours_ten_minutes_and_sixty_seconds():
    assert (jobs_module.OVERALL_DEADLINE_SECONDS, jobs_module.INACTIVITY_DEADLINE_SECONDS,
            jobs_module.CANCEL_GRACE_SECONDS) == (43200, 600, 60)


def test_a_job_that_keeps_reporting_is_cancelled_at_the_overall_deadline_then_killed_after_the_grace():
    ticker = Ticker()
    watch = DeadlineWatch(clock=ticker)
    while ticker.now - 1000.0 + 599 < 12 * 3600:  # a frame just inside the inactivity limit, for twelve hours
        ticker.now += 599
        watch.activity()
        assert watch.check().action is DeadlineAction.NONE
    ticker.now = 1000.0 + 12 * 3600
    watch.activity()
    verdict = watch.check()
    assert (verdict.action, verdict.reason) == (DeadlineAction.CANCEL, DEADLINE_EXCEEDED)
    assert watch.cancelling
    ticker.now += 59
    assert watch.check().action is DeadlineAction.NONE  # inside the grace period
    ticker.now += 1
    assert watch.check() == jobs_module.Verdict(DeadlineAction.KILL, DEADLINE_EXCEEDED)


def test_silence_for_ten_minutes_cancels_and_a_frame_resets_the_count():
    ticker = Ticker()
    watch = DeadlineWatch(clock=ticker)
    ticker.now += 599
    assert watch.check().action is DeadlineAction.NONE
    watch.activity()
    ticker.now += 599
    assert watch.check().action is DeadlineAction.NONE
    ticker.now += 1
    verdict = watch.check()
    assert (verdict.action, verdict.reason) == (DeadlineAction.CANCEL, INACTIVITY_TIMEOUT)


def test_a_cancel_is_asked_for_once_and_a_frame_does_not_cancel_the_kill():
    ticker = Ticker()
    watch = DeadlineWatch(clock=ticker)
    ticker.now += 600
    assert watch.check().action is DeadlineAction.CANCEL
    assert watch.check().action is DeadlineAction.NONE  # not asked twice
    watch.activity()  # a worker still chattering while it winds down
    ticker.now += 60
    assert watch.check().action is DeadlineAction.KILL


def test_a_stop_pressed_by_a_person_starts_the_same_grace_period():
    ticker = Ticker()
    watch = DeadlineWatch(clock=ticker)
    watch.cancel_requested("cancelled")
    watch.cancel_requested("something else")  # the first reason stands
    ticker.now += 59.9
    assert watch.check().action is DeadlineAction.NONE
    ticker.now += 0.1
    assert watch.check() == jobs_module.Verdict(DeadlineAction.KILL, "cancelled")


def test_active_states_are_the_three_that_hold_a_slot():
    assert set(ACTIVE_STATES) == {QUEUED, RUNNING, AWAITING_RETRY}


def test_a_cancel_that_lands_between_the_check_and_the_write_leaves_no_checkpoints(jobs, session, clock, queue,
                                                                                     monkeypatch):
    """The state check and the checkpoint write are one transaction. A cancel in between either waits for
    the write (and then deletes it) or comes first (and the write is refused); it never loses to the write."""
    import threading

    job_id = _running(jobs)
    real_save = jobs.checkpoints.save_batch
    outcome = {}

    def cancel_elsewhere():
        try:
            outcome["cancelled"] = TranslationJobs(session, clock=clock, queue=queue).cancel(job_id)
        finally:
            session.remove()  # this thread's own session

    def save_after_a_cancel_tried(key, index, lines):
        worker = threading.Thread(target=cancel_elsewhere)
        worker.start()
        worker.join(timeout=1.5)  # a cancel that is not held up has finished by now
        outcome["worker"] = worker
        return real_save(key, index, lines)

    monkeypatch.setattr(jobs.checkpoints, "save_batch", save_after_a_cancel_tried)
    jobs.save_batch(job_id, 0, ["a"])
    outcome["worker"].join(timeout=30)
    assert not outcome["worker"].is_alive() and outcome["cancelled"] is True
    assert jobs.get(job_id).state == CANCELLED
    assert _checkpoints(session) == 0


def test_a_write_step_inside_a_ledger_transaction_leaves_the_commit_to_the_ledger(session, jobs):
    from subtitles.tools.translate.runtime.transaction import ledger_transaction

    with pytest.raises(RuntimeError):
        with ledger_transaction(session):
            assert jobs.request(SOURCE, "fr").job_id  # a step that opens its own write transaction
            session.expire_all()
            assert jobs.active_for(SOURCE, "fr") is not None
            raise RuntimeError("the outer step fails after the inner one")
    assert jobs.active_for(SOURCE, "fr") is None
    with ledger_transaction(session):
        assert jobs.request(SOURCE, "fr").job_id
    session.expire_all()
    assert jobs.active_for(SOURCE, "fr") is not None


def test_a_ledger_step_inside_a_write_transaction_leaves_the_commit_to_the_write(session, jobs):
    from subtitles.tools.translate.runtime.jobs import _write
    from subtitles.tools.translate.runtime.transaction import ledger_transaction

    with pytest.raises(RuntimeError):
        with _write(session):
            assert jobs.request(SOURCE, "fr").job_id
            with ledger_transaction(session):  # a step that takes the ledger lock inside
                pass
            raise RuntimeError("the outer step fails after the inner one")
    assert jobs.active_for(SOURCE, "fr") is None
    assert _checkpoints(session) == 0


def test_a_failing_inner_step_rolls_back_to_its_savepoint_and_the_outer_one_decides(session, jobs):
    from subtitles.tools.translate.runtime.jobs import _write

    with _write(session):
        assert jobs.request(SOURCE, "fr").job_id
        with pytest.raises(RuntimeError):
            with _write(session):
                assert jobs.request(SOURCE, "de").job_id
                raise RuntimeError("the inner step fails")
    session.expire_all()
    assert jobs.active_for(SOURCE, "fr") is not None
    assert jobs.active_for(SOURCE, "de") is None


# ------------------------------------------------------------ rows another session changed

def _second_session(session):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)()


def test_a_session_that_saw_a_job_running_sees_what_another_session_did_to_it(session, jobs, clock, queue):
    job_id = _running(jobs)
    # Hold what this session loaded, as a long-running worker does, so the session keeps it in its identity map.
    held = (jobs.get(job_id), jobs.active_for(SOURCE, "fr"))
    assert [row.state for row in held] == [RUNNING, RUNNING]
    other = _second_session(session)
    try:
        assert TranslationJobs(other, clock=clock, queue=queue).await_retry(job_id, "rate_limited")
    finally:
        other.close()
    # This session still holds the object it loaded earlier; every read goes back to the database.
    assert jobs.get(job_id).state == AWAITING_RETRY
    assert jobs.active_for(SOURCE, "fr").state == AWAITING_RETRY
    assert jobs.key_of(job_id) is None  # no longer running, so no checkpoint key


def test_a_session_reads_the_checkpoints_another_session_saved(session, jobs, clock, queue):
    job_id = _running(jobs)
    held = jobs.checkpoints._row(jobs.key_of(job_id))  # nothing saved yet
    assert held is None and jobs.finished_batches(job_id) == {}
    assert jobs.save_batch(job_id, 5, ["first, from here"])
    held = jobs.checkpoints._row(jobs.key_of(job_id))  # the session now holds the checkpoint row
    other = _second_session(session)
    try:
        assert TranslationJobs(other, clock=clock, queue=queue).save_batch(job_id, 0, ["from the other session"])
    finally:
        other.close()
    assert jobs.finished_batches(job_id) == {0: ["from the other session"], 5: ["first, from here"]}
    # And a batch saved here adds to theirs instead of replacing the whole record from an older copy.
    assert jobs.save_batch(job_id, 1, ["here"])
    assert jobs.finished_batches(job_id) == {0: ["from the other session"], 1: ["here"], 5: ["first, from here"]}
    assert held is not None


# ------------------------------------------------------------ SQLite write locking

def test_a_read_then_write_transaction_is_not_broken_by_another_connections_commit(tmp_path, clock, queue,
                                                                                    monkeypatch):
    """save_batch reads its row and then writes it. A plain BEGIN would hold only a read snapshot, and
    another connection committing in between makes the write fail at once with "database is locked".
    The transaction takes the write lock first, so the other writer waits (here: is refused, with no
    wait) and ours goes through."""
    import sqlite3

    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import Base, TableTranslatorCheckpoint, TableTranslatorJob

    path = tmp_path / "locking.db"
    engine = _production_sqlite_engine(path)
    Base.metadata.create_all(engine, tables=[TableTranslatorJob.__table__, TableTranslatorCheckpoint.__table__])
    session = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    other.execute("PRAGMA busy_timeout=0")
    refused = []
    try:
        jobs = TranslationJobs(session, clock=clock, queue=queue)
        job_id = _running(jobs)
        real_row = jobs.checkpoints._row

        def row_then_a_commit_from_elsewhere(key):
            found = real_row(key)
            try:  # the other connection tries to commit right after our read
                other.execute("INSERT INTO translator_jobs (source_path, target, state, created_at, updated_at) "
                              "VALUES ('/media/other.srt', 'fr', 'queued', '2026-11-07', '2026-11-07')")
            except sqlite3.OperationalError as error:
                refused.append(str(error))
            return found

        monkeypatch.setattr(jobs.checkpoints, "_row", row_then_a_commit_from_elsewhere)
        assert jobs.save_batch(job_id, 0, ["a"]) is True
        assert jobs.save_batch(job_id, 1, ["b"]) is True
        assert refused and all("locked" in message for message in refused)
        monkeypatch.undo()
        assert jobs.finished_batches(job_id) == {0: ["a"], 1: ["b"]}
        # Once ours has committed the other connection is let in.
        other.execute("INSERT INTO translator_jobs (source_path, target, state, created_at, updated_at) "
                      "VALUES ('/media/other.srt', 'fr', 'queued', '2026-11-07', '2026-11-07')")
    finally:
        other.close()
        session.remove()
        engine.dispose()


def test_a_write_transaction_that_fails_is_rolled_back_and_releases_the_lock(tmp_path, clock, queue):
    import sqlite3

    import sqlalchemy as sa
    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import Base, TableTranslatorCheckpoint, TableTranslatorJob
    from subtitles.tools.translate.runtime.jobs import _write

    path = tmp_path / "rollback.db"
    engine = _production_sqlite_engine(path)
    Base.metadata.create_all(engine, tables=[TableTranslatorJob.__table__, TableTranslatorCheckpoint.__table__])
    session = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        with pytest.raises(RuntimeError):
            with _write(session):
                session.execute(sa.text("INSERT INTO translator_jobs (source_path, target, state, created_at, "
                                        "updated_at) VALUES ('/m/a.srt', 'fr', 'queued', '2026-11-07', "
                                        "'2026-11-07')"))
                raise RuntimeError("boom")
        assert other.execute("SELECT count(*) FROM translator_jobs").fetchone() == (0,)
        other.execute("INSERT INTO translator_jobs (source_path, target, state, created_at, updated_at) "
                      "VALUES ('/m/b.srt', 'fr', 'queued', '2026-11-07', '2026-11-07')")  # the lock is free
    finally:
        other.close()
        session.remove()
        engine.dispose()

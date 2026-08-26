# coding=utf-8
"""The Jobs Manager must never run more jobs than the configured limit.

Reported with Concurrent Jobs set to 1 and two jobs consistently in Running
while a backlog sat in Pending.

It is a check-then-act race across two threads. The consumer took the lock,
compared len(jobs_running_queue) against the limit, and released the lock before
acting on the answer. The job it then spawned entered jobs_running_queue only
inside the worker, and that append happened outside the lock. Between the spawn
and the append the job was invisible: already gone from pending, not yet in
running. The consumer loop has no sleep on the success path, so it immediately
re-read a running count that was stale-low and started a second worker. With a
limit of 1 and a backlog the second spawn won essentially every time, which is
why the reporter saw exactly 2 rather than an unbounded pile.

Reserving the slot is therefore one critical section: check capacity, take the
job off pending, and put it on running, before any thread is spawned. These
tests drive that reservation directly rather than trying to win a race, because
a test that only sometimes reproduces a race protects nothing.
"""

import pytest

import app.database  # noqa: F401


@pytest.fixture
def queue(monkeypatch):
    from app.jobs_queue import JobsQueue

    monkeypatch.setattr("app.jobs_queue.event_stream", lambda *args, **kwargs: None)
    return JobsQueue()


def _enqueue(queue, count, name="Job"):
    for index in range(count):
        queue.feed_jobs_pending_queue(
            job_name=f"{name} {index}",
            module="tests.fake",
            func="work",
            # Distinct kwargs: the queue refuses a job whose module, function and
            # arguments match one already queued.
            kwargs={"index": index},
        )


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_no_more_slots_are_handed_out_than_the_limit(queue, monkeypatch, limit):
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", limit)
    _enqueue(queue, limit + 3)

    reserved = []
    for _ in range(limit + 3):
        job = queue._reserve_next_job()
        if job is None:
            break
        reserved.append(job)

    assert len(reserved) == limit
    assert len(queue.jobs_running_queue) == limit
    assert len(queue.jobs_pending_queue) == 3


def test_a_reserved_job_leaves_pending_and_joins_running_at_once(queue, monkeypatch):
    """The window the race lived in. Nothing may observe the job in neither
    queue, because that is exactly when the capacity check reads stale-low."""
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    _enqueue(queue, 2)

    job = queue._reserve_next_job()

    assert job is not None
    assert job in queue.jobs_running_queue
    assert job not in queue.jobs_pending_queue
    assert queue._reserve_next_job() is None, "a second slot was handed out"


def test_a_finished_job_frees_its_slot(queue, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    _enqueue(queue, 2)

    first = queue._reserve_next_job()
    assert queue._reserve_next_job() is None

    queue.jobs_running_queue.remove(first)

    assert queue._reserve_next_job() is not None


def test_an_empty_queue_reserves_nothing(queue, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)

    assert queue._reserve_next_job() is None


def test_a_translation_counts_against_the_general_limit_too(queue, monkeypatch):
    """The settings field says 'Number of concurrent jobs allowed in the jobs
    manager'. A translation admitted purely against its own lane meant a
    translation plus a general job ran under a configured limit of 1, which is
    not what that sentence promises.
    """
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    monkeypatch.setattr(settings.translator, "openrouter_max_concurrent", 5)

    queue.feed_jobs_pending_queue(job_name="Translating something", module="tests.fake",
                                  func="work", kwargs={"index": 0})
    queue.feed_jobs_pending_queue(job_name="Syncing series", module="tests.fake",
                                  func="work", kwargs={"index": 1})

    assert queue._reserve_next_job() is not None
    assert queue._reserve_next_job() is None, (
        "a second job ran alongside a translation under a limit of 1"
    )


def test_the_translation_lane_still_caps_translations_below_the_general_limit(queue, monkeypatch):
    """The lane is a sub-limit, not a parallel gate: it can only ever admit
    fewer translations than the general cap would, never more."""
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    monkeypatch.setattr(settings.translator, "openrouter_max_concurrent", 1)

    for index in range(3):
        queue.feed_jobs_pending_queue(job_name=f"Translating {index}", module="tests.fake",
                                      func="work", kwargs={"index": index})

    assert queue._reserve_next_job() is not None
    assert queue._reserve_next_job() is None, "the translation lane admitted a second one"


def test_a_forced_job_bypasses_the_limit_without_double_counting(queue, monkeypatch):
    """Forcing a job is an explicit user action and is meant to bypass the cap.
    It must still leave the queues consistent: counted once in running, gone
    from pending."""
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    monkeypatch.setattr("app.jobs_queue.Thread", _ImmediateThread)
    _enqueue(queue, 2)

    running = queue._reserve_next_job()
    forced_id = queue.jobs_pending_queue[0].job_id

    queue.force_start_pending_job(forced_id)

    assert running in queue.jobs_running_queue
    assert all(job.job_id != forced_id for job in queue.jobs_pending_queue)
    assert [job.job_id for job in queue.jobs_running_queue].count(forced_id) <= 1


class _ImmediateThread:
    """Runs the target inline, so a test never leaves a thread behind."""

    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = False

    def start(self):
        try:
            self._target(*self._args, **self._kwargs)
        except Exception:
            pass

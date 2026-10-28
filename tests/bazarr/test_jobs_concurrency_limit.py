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

import time

import pytest

import app.database  # noqa: F401


@pytest.fixture
def queue(monkeypatch):
    from app.jobs_queue import JobsQueue

    monkeypatch.setattr("app.jobs_queue.event_stream", lambda *args, **kwargs: None)
    return JobsQueue()


# The two jobs that call the AI Subtitle Translator: a library translation and
# the editor's. The translation lane is chosen by these, never by a job's name.
TRANSLATION = ("subtitles.tools.translate.main", "translate_subtitles_file")
EDITOR_TRANSLATION = ("subtitles.tools.translate.editor", "translate_editor_lines")
DISCOVER_DOWNLOAD = ("discover.download", "run_download_job")
MASS_OPERATION = ("subtitles.mass_operations", "mass_batch_operation")
# What Discover names a download: title, language, provider and file.
LOST_IN_TRANSLATION = "Lost in Translation (2003) · en · opensubtitles · Lost.in.Translation.2003.en.srt"


def _feed(queue, name, job, index, **options):
    module, func = job
    return queue.feed_jobs_pending_queue(job_name=name, module=module, func=func,
                                         kwargs={"index": index}, **options)


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

    _feed(queue, "Translating something", TRANSLATION, 0)
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
        _feed(queue, f"Translating {index}", TRANSLATION, index)

    assert queue._reserve_next_job() is not None
    assert queue._reserve_next_job() is None, "the translation lane admitted a second one"


def _one_translation_at_a_time(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    monkeypatch.setattr(settings.translator, "openrouter_max_concurrent", 1)


def test_a_download_named_after_a_translation_is_not_held_by_the_lane(queue, monkeypatch):
    """A Discover download for "Lost in Translation" is not a translation.

    The lane used to be picked by the word in the job name, so this download
    waited for a real translation to finish, and every job queued behind it
    waited too, because only the head of the queue is ever considered.
    """
    _one_translation_at_a_time(monkeypatch)
    _feed(queue, "Translating Example (EN to HU)", TRANSLATION, 0)
    _feed(queue, LOST_IN_TRANSLATION, DISCOVER_DOWNLOAD, 1)
    queue.feed_jobs_pending_queue(job_name="Syncing series", module="tests.fake", func="work",
                                  kwargs={"index": 2})

    assert queue._reserve_next_job().func == "translate_subtitles_file"
    download = queue._reserve_next_job()
    assert download is not None and download.func == "run_download_job", (
        "the download waited for the translation lane"
    )
    assert queue._reserve_next_job() is not None, "the job behind the download stayed blocked"


def test_a_running_download_named_after_a_translation_takes_no_translation_slot(queue, monkeypatch):
    _one_translation_at_a_time(monkeypatch)
    _feed(queue, LOST_IN_TRANSLATION, DISCOVER_DOWNLOAD, 0)
    _feed(queue, "Translating Example (EN to HU)", TRANSLATION, 1)

    assert queue._reserve_next_job().func == "run_download_job"
    assert queue._reserve_next_job() is not None, "the download used up the only translation slot"


def test_an_editor_translation_is_in_the_lane_whatever_its_name(queue, monkeypatch):
    _one_translation_at_a_time(monkeypatch)
    _feed(queue, "Translating Example (EN to HU)", TRANSLATION, 0)
    _feed(queue, "Example in the editor", EDITOR_TRANSLATION, 1)

    assert queue._reserve_next_job() is not None
    assert queue._reserve_next_job() is None, "a second translation ran beside the first"


def test_a_renamed_running_translation_keeps_its_slot(queue, monkeypatch):
    """A job's name changes while it runs: a failing editor translation is
    renamed "Failed ..." before it leaves the running queue. The slot it holds
    must not go to the next translation until it has actually finished."""
    _one_translation_at_a_time(monkeypatch)
    _feed(queue, "Translating Example in the editor (English to Hungarian)", EDITOR_TRANSLATION, 0)
    running = queue._reserve_next_job()
    queue.update_job_name(running.job_id, "Failed Example in the editor (English to Hungarian)")
    _feed(queue, "Translating Other (EN to DE)", TRANSLATION, 1)

    assert queue._reserve_next_job() is None, "the renamed translation lost its slot while running"


def test_a_retried_translation_stays_in_the_lane(queue, monkeypatch):
    """A failed translation is renamed "Failed ..." and a retry keeps that
    name. The retry runs the same function, so it is still a translation."""
    _one_translation_at_a_time(monkeypatch)
    monkeypatch.setattr("app.jobs_queue.activity.finish", lambda *args, **kwargs: None)
    _feed(queue, "Translating Example (EN to HU)", TRANSLATION, 0, retryable=True)
    failed = queue._reserve_next_job()
    queue.update_job_name(failed.job_id, "Failed Example (EN to HU)")
    queue._mark_failed(failed)

    _feed(queue, "Translating Other (EN to DE)", TRANSLATION, 1)
    assert queue._reserve_next_job() is not None
    assert queue.retry_job(failed.job_id)
    assert queue._reserve_next_job() is None, "the retry ran beside another translation"


def test_the_mass_translate_job_does_not_hold_a_translation_slot(queue, monkeypatch):
    """"Translating Subtitles (N items)" only queues one translation job per
    item, and those are what the lane caps. The parent holding a slot as well
    left one fewer for the translations it had just queued."""
    _one_translation_at_a_time(monkeypatch)
    _feed(queue, "Translating Example (EN to HU)", TRANSLATION, 0)
    _feed(queue, "Translating Subtitles (3 items)", MASS_OPERATION, 1)

    assert queue._reserve_next_job() is not None
    assert queue._reserve_next_job() is not None, "the batch job waited for the translation lane"


def test_the_translator_status_counts_the_same_jobs_the_lane_does(queue, monkeypatch):
    """The Bazarr queue figures on the translator status card count
    translations the way the lane does, so a download named after a film about
    translation is not reported as one."""
    from types import SimpleNamespace

    from flask import Flask

    from api.translator import translator as api_mod

    monkeypatch.setattr(api_mod, "jobs_queue", queue)
    monkeypatch.setattr(api_mod.settings.translator, "openrouter_url", "http://translator:8765")
    monkeypatch.setattr(api_mod, "get_translator_auth_headers", lambda: {})
    monkeypatch.setattr(api_mod.requests, "get",
                        lambda url, **kwargs: SimpleNamespace(status_code=200, json=lambda: {}))
    _one_translation_at_a_time(monkeypatch)

    _feed(queue, "Translating Example (EN to HU)", TRANSLATION, 0)
    _feed(queue, LOST_IN_TRANSLATION, DISCOVER_DOWNLOAD, 1)
    queue._reserve_next_job()
    queue._reserve_next_job()
    _feed(queue, "Example in the editor", EDITOR_TRANSLATION, 2)
    _feed(queue, "Translating Subtitles (3 items)", MASS_OPERATION, 3)

    with Flask(__name__).test_request_context("/api/translator/status"):
        body, status = api_mod.TranslatorStatus.get.__wrapped__(api_mod.TranslatorStatus())

    assert status == 200
    assert body["bazarr_queue"] == {"pending": 1, "running": 1}


def test_every_translation_job_names_a_function_its_module_defines():
    """The lane knows a translation only by the module and function it runs,
    and a job it does not recognise runs uncapped. A library translation is
    queued under its file's module path and its own function name, and an
    editor translation under the editor's two constants, so moving or renaming
    either without updating TRANSLATION_JOBS would lift the limit silently."""
    import ast
    from pathlib import Path

    from app.jobs_queue import TRANSLATION_JOBS

    source = Path(__file__).resolve().parents[2] / "bazarr"
    for module, func in sorted(TRANSLATION_JOBS):
        path = source.joinpath(*module.split(".")).with_suffix(".py")
        assert path.is_file(), f"{module} is no longer a module"
        defined = {node.name for node in ast.parse(path.read_text(encoding="utf-8")).body
                   if isinstance(node, ast.FunctionDef)}
        assert func in defined, f"{module} no longer defines {func}"

    editor = ast.parse((source / "subtitles" / "tools" / "translate" / "editor.py").read_text(encoding="utf-8"))
    constants = {node.targets[0].id: node.value.value for node in editor.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, ast.Constant)}
    queued_as = (constants.get("EDITOR_TRANSLATION_MODULE"), constants.get("EDITOR_TRANSLATION_FUNC"))
    assert queued_as in TRANSLATION_JOBS, "the editor queues its translations outside the lane"


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


class _IdleThread:
    """Holds a started job on the running queue, as a search that is still waiting on its providers."""

    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.daemon = False

    def start(self):
        pass


def _enqueue_lane(queue, count, lane, name="Search", first=0):
    return [queue.feed_jobs_pending_queue(job_name=f"{name} {index}", module="tests.fake", func="work",
                                          kwargs={"index": index, "lane": lane}, lane=lane)
            for index in range(first, first + count)]


def test_a_user_started_search_waits_its_turn_once_the_lane_is_full(queue, monkeypatch):
    """Forcing a job past the general limit is for a person who is waiting, but it must not be unbounded:
    each forced job is a thread, and a page that fires searches would otherwise start them all."""
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    limit = LANE_LIMITS[USER_SEARCH_LANE]
    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    job_ids = _enqueue_lane(queue, limit + 2, USER_SEARCH_LANE)

    started = [queue.force_start_pending_job(job_id, True) for job_id in job_ids]

    assert started == [True] * limit + [False, False]
    assert len(queue.jobs_running_queue) == limit
    waiting = [job for job in queue.jobs_pending_queue]
    assert [job.job_id for job in waiting] == job_ids[limit:]
    assert all(job.status == "pending" and "other requests" in job.progress_message for job in waiting)

    # A slot frees: the next press starts the job that was told to wait.
    queue.jobs_running_queue.popleft()
    assert queue.force_start_pending_job(job_ids[limit], True) is True
    assert len(queue.jobs_running_queue) == limit
    assert queue.force_start_pending_job(job_ids[limit + 1], True) is False


def test_a_waiting_search_still_runs_in_its_turn_through_the_normal_queue(queue, monkeypatch):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    job_ids = _enqueue_lane(queue, LANE_LIMITS[USER_SEARCH_LANE] + 1, USER_SEARCH_LANE)
    for job_id in job_ids:
        queue.force_start_pending_job(job_id, True)
    queue.jobs_running_queue.clear()

    assert queue._reserve_next_job().job_id == job_ids[-1]


def _fill_search_lane_with_a_library_backlog(queue, monkeypatch):
    """Four searches running, three library jobs pending, then a fifth search forced and told to wait."""
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    searches = _enqueue_lane(queue, LANE_LIMITS[USER_SEARCH_LANE] + 1, USER_SEARCH_LANE)
    for job_id in searches[:LANE_LIMITS[USER_SEARCH_LANE]]:
        queue.force_start_pending_job(job_id, True)
    library = [queue.feed_jobs_pending_queue(job_name=f"Library {index}", module="tests.fake", func="work",
                                             kwargs={"library": index}) for index in range(3)]
    waiting = searches[-1]
    # The fifth search was queued before the library jobs only in id; moving it to the back
    # makes it sit behind them, the way a search forced after a backlog built up does.
    queue.move_job_in_pending_queue(waiting, "bottom")
    assert queue.force_start_pending_job(waiting, True) is False
    return searches, library, waiting


def test_a_waiting_search_starts_before_the_library_backlog_when_a_slot_frees(queue, monkeypatch):
    searches, library, waiting = _fill_search_lane_with_a_library_backlog(queue, monkeypatch)

    queue.jobs_running_queue.popleft()
    job = queue._reserve_next_job()

    assert job.job_id == waiting
    assert [item.job_id for item in queue.jobs_pending_queue] == library
    assert job.progress_message == ""


def test_a_waiting_search_is_not_started_while_its_lane_is_still_full(queue, monkeypatch):
    searches, library, waiting = _fill_search_lane_with_a_library_backlog(queue, monkeypatch)

    # The general limit is full as well, so the library backlog waits too.
    assert queue._reserve_next_job() is None
    assert len([job for job in queue.jobs_running_queue if job.lane == "user_search"]) == 4


def test_waiting_searches_start_oldest_first_and_never_beyond_the_lane_cap(queue, monkeypatch):
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    searches, library, waiting = _fill_search_lane_with_a_library_backlog(queue, monkeypatch)
    second = _enqueue_lane(queue, 1, USER_SEARCH_LANE, name="Later", first=50)[0]
    assert queue.force_start_pending_job(second, True) is False

    queue.jobs_running_queue.popleft()
    assert queue._reserve_next_job().job_id == waiting
    # The lane is full again, so the later search keeps waiting and says so, even with room overall.
    monkeypatch.setattr("app.config.settings.general.concurrent_jobs", 10)
    assert queue._reserve_next_job().job_id == library[0]
    running_in_lane = [job for job in queue.jobs_running_queue if job.lane == USER_SEARCH_LANE]
    assert len(running_in_lane) <= LANE_LIMITS[USER_SEARCH_LANE]
    later = next(job for job in queue.jobs_pending_queue if job.job_id == second)
    assert "other requests" in later.progress_message


def test_a_waiting_search_that_is_removed_leaves_the_next_one_to_start(queue, monkeypatch):
    from app.jobs_queue import USER_SEARCH_LANE

    searches, library, waiting = _fill_search_lane_with_a_library_backlog(queue, monkeypatch)
    second = _enqueue_lane(queue, 1, USER_SEARCH_LANE, name="Later", first=50)[0]
    queue.force_start_pending_job(second, True)

    assert queue.remove_job_from_pending_queue(waiting) is True
    queue.jobs_running_queue.popleft()

    assert queue._reserve_next_job().job_id == second
    assert all(job.job_id != waiting for job in queue.jobs_pending_queue)


def test_a_waiting_search_never_started_by_a_press_is_left_to_the_normal_order(queue, monkeypatch):
    """Only a job somebody pressed Start on is promoted: a lane job still plain-pending keeps its place."""
    from app.config import settings
    from app.jobs_queue import USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    library = queue.feed_jobs_pending_queue(job_name="Library", module="tests.fake", func="work", kwargs={"x": 1})
    _enqueue_lane(queue, 1, USER_SEARCH_LANE)

    assert queue._reserve_next_job().job_id == library


def test_other_lanes_and_unlaned_jobs_are_not_held_by_a_full_search_lane(queue, monkeypatch):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_ACTION_LANE, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    for job_id in _enqueue_lane(queue, LANE_LIMITS[USER_SEARCH_LANE], USER_SEARCH_LANE):
        queue.force_start_pending_job(job_id, True)
    action = _enqueue_lane(queue, 1, USER_ACTION_LANE, name="Retry")[0]
    plain = queue.feed_jobs_pending_queue(job_name="Plain", module="tests.fake", func="work", kwargs={"plain": True})

    assert queue.force_start_pending_job(action, True) is True
    assert queue.force_start_pending_job(plain, True) is True


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


class _RealSearches:
    """The real queue, its real consumer loop and the real library search job, with providers that wait.

    Every press of Search goes through ``queue_manual_search``: a job fed to pending, then a thread that
    forces it to start. The search itself is a fake that holds until it is released, and counts how many
    are inside it at once, which is what the lane cap is about.
    """

    def __init__(self, queue, manual, monkeypatch):
        import threading

        self.queue, self.manual = queue, manual
        self.release = threading.Event()
        self.counter = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.entered = []
        self.stop_consumer = threading.Event()
        monkeypatch.setattr(manual, "manual_search", self._search)
        monkeypatch.setattr("app.get_providers.get_providers_sorted", lambda: ["opensubtitles"])

    def _search(self, path, *args, **kwargs):
        with self.counter:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
            self.entered.append(path)
        try:
            self.release.wait(10)
            return []
        finally:
            with self.counter:
                self.inside -= 1

    def start_consumer(self):
        import threading

        original = self.queue._reserve_next_job

        def reserve():
            if self.stop_consumer.is_set():
                raise SystemExit
            return original()

        self.queue._reserve_next_job = reserve
        self.consumer = threading.Thread(target=self.queue.consume_jobs_pending_queue, daemon=True)
        self.consumer.start()

    def press_search(self, count):
        """Press Search for ``count`` different files at the same moment, each on a request thread of its own."""
        import threading

        gate = threading.Barrier(count)
        ids, errors = [], []

        def press(index):
            gate.wait(5)
            try:
                ids.append(self.manual.queue_manual_search(f"/movies/m{index}.mkv", 1, "None", f"Movie {index}",
                                                           "movie", f"Movie {index}"))
            except Exception as error:  # pragma: no cover - the assertion below reports it
                errors.append(error)

        threads = [threading.Thread(target=press, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert not errors
        return sorted(ids)

    def settle(self, seconds=0.4):
        import time

        time.sleep(seconds)

    def wait_for(self, condition, timeout=5):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return condition()

    def finish(self):
        self.release.set()
        self.wait_for(lambda: not self.queue.jobs_running_queue and not self.queue.jobs_pending_queue)
        self.stop_consumer.set()
        self.consumer.join(5)

    def running_in_lane(self):
        return [job for job in list(self.queue.jobs_running_queue) if job.lane == "user_search"]


@pytest.fixture
def searches(monkeypatch):
    from app import jobs_queue as queue_module
    from subtitles import manual

    monkeypatch.setattr(queue_module.JobsQueue, "_flush_progress_loop", lambda self: None)
    monkeypatch.setattr(queue_module, "event_stream", lambda *args, **kwargs: None)
    # The consumer sleeps half a second when it has nothing to start; a short nap keeps the test quick
    # without changing which job it picks.
    monkeypatch.setattr(queue_module, "sleep", lambda seconds: __import__("time").sleep(0.01))
    queue = queue_module.JobsQueue()
    monkeypatch.setattr(manual, "jobs_queue", queue)
    harness = _RealSearches(queue, manual, monkeypatch)
    harness.start_consumer()
    yield harness
    harness.finish()


@pytest.mark.parametrize("general_limit", [8, 4, 1])
def test_five_searches_pressed_at_once_never_run_more_than_the_lane_allows(searches, monkeypatch, general_limit):
    """A library search press is a job fed to pending and then forced to start. The consumer that runs the
    backlog used to admit a waiting search on the general limit alone, so with room overall every press ran."""
    from app.config import settings
    from app.jobs_queue import LANE_FULL_MESSAGE, LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", general_limit)
    cap = LANE_LIMITS[USER_SEARCH_LANE]

    ids = searches.press_search(cap + 3)
    searches.wait_for(lambda: searches.inside >= min(cap, max(general_limit, 1)) and len(searches.entered) >= 1)
    searches.settle()

    assert searches.peak <= cap
    assert len(searches.running_in_lane()) <= cap
    # The press is a person waiting, so the cap, not the general limit, decides how many run at once.
    assert len(searches.running_in_lane()) == cap
    waiting = [job for job in list(searches.queue.jobs_pending_queue) if job.job_id in ids]
    assert len(waiting) == 3
    assert all(job.progress_message == LANE_FULL_MESSAGE for job in waiting)


def test_a_search_waiting_for_the_lane_starts_when_one_ends_and_all_of_them_finish(searches, monkeypatch):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 8)
    cap = LANE_LIMITS[USER_SEARCH_LANE]
    ids = searches.press_search(cap + 2)
    searches.wait_for(lambda: len(searches.running_in_lane()) == cap)
    searches.settle()
    assert searches.peak == cap

    searches.release.set()
    assert searches.wait_for(lambda: len(searches.entered) == cap + 2)
    assert searches.wait_for(lambda: all(job.status == "completed" for job in list(searches.queue.jobs_completed_queue))
                             and len(searches.queue.jobs_completed_queue) == cap + 2)
    assert searches.peak == cap
    assert sorted(job.job_id for job in searches.queue.jobs_completed_queue) == ids


def test_a_library_job_is_not_held_behind_searches_waiting_for_their_lane(searches, monkeypatch):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 8)
    cap = LANE_LIMITS[USER_SEARCH_LANE]
    searches.press_search(cap + 2)
    searches.wait_for(lambda: len(searches.running_in_lane()) == cap)
    searches.settle()

    # A scan queued after the searches that wait runs at once: it is not in the lane.
    ran = []
    import sys
    import types

    module = types.ModuleType("tests_fake_library_work")
    module.work = lambda **kwargs: ran.append(True)
    monkeypatch.setitem(sys.modules, "tests_fake_library_work", module)
    searches.queue.feed_jobs_pending_queue(job_name="Library scan", module="tests_fake_library_work", func="work",
                                           kwargs={"scan": 1})

    assert searches.wait_for(lambda: ran, timeout=3), "the library job waited behind searches that wait for the lane"
    assert searches.peak == cap


# The consumer thread scans the pending queue under the lock while the queue's own editors (Remove,
# Move to top or bottom, Empty) change it. Iterating a deque another thread changes raises, and nothing
# above the consumer caught that, so the one thread that starts queued jobs ended and they never ran
# again until the process restarted.

class _RealConsumer:
    """The real queue and the real consumer loop, on a queue whose search lane is full so the scan walks it all."""

    def __init__(self, queue, monkeypatch, pending):
        import sys
        import threading
        from app.config import settings
        from app.jobs_queue import Job, USER_SEARCH_LANE

        monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
        monkeypatch.setattr("app.jobs_queue.sleep", lambda seconds: time.sleep(0.001))
        monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
        self.queue = queue
        self.deaths = []
        self.stop = threading.Event()
        for index in range(4):
            running = Job(job_id=9000 + index, job_name=f"running {index}", module="m", func="f", kwargs={},
                          lane=USER_SEARCH_LANE)
            running.status = "running"
            queue.jobs_running_queue.append(running)
        self.ids = _enqueue_lane(queue, pending, USER_SEARCH_LANE)
        for job in queue.jobs_pending_queue:
            job.lane_waiting = True
        original = queue._reserve_next_job

        def reserve():
            if self.stop.is_set():
                raise SystemExit
            return original()

        queue._reserve_next_job = reserve
        self._previous_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.thread = threading.Thread(target=self._consume, daemon=True)
        self.thread.start()

    def _consume(self):
        try:
            self.queue.consume_jobs_pending_queue()
        except BaseException as error:  # noqa: B036
            self.deaths.append(repr(error))

    def close(self):
        import sys

        self.stop.set()
        self.thread.join(5)
        sys.setswitchinterval(self._previous_interval)



@pytest.fixture
def consumer(queue, monkeypatch):
    made = []

    def make(pending):
        made.append(_RealConsumer(queue, monkeypatch, pending))
        return made[-1]

    yield make
    for item in made:
        item.close()


def test_the_consumer_survives_the_queue_being_edited_while_it_scans(consumer):
    run = consumer(600)
    queue = run.queue
    deadline = time.monotonic() + 3
    moves = 0
    while time.monotonic() < deadline and run.thread.is_alive():
        queue.move_job_in_pending_queue(run.ids[moves % len(run.ids)], "bottom")
        if moves % 50 == 0:
            queue.remove_job_from_pending_queue(run.ids[(moves * 7) % len(run.ids)])
        moves += 1

    assert run.thread.is_alive(), f"the consumer thread ended: {run.deaths}"

    # And it still starts work: a lane slot frees and a waiting search takes it.
    queue.jobs_running_queue.popleft()
    assert _wait_until(lambda: len(queue.jobs_running_queue) == 4), "queued jobs no longer start"


def test_the_consumer_carries_on_after_an_error_in_one_pass(queue, monkeypatch, caplog):
    import logging
    import threading
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    monkeypatch.setattr("app.jobs_queue.sleep", lambda seconds: time.sleep(0.001))
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    _enqueue(queue, 3)
    real = queue._reserve_next_job
    calls = []
    stop = threading.Event()

    def reserve():
        calls.append(1)
        if stop.is_set():
            raise SystemExit
        if len(calls) <= 5:
            raise RuntimeError("deque mutated during iteration")
        return real()

    queue._reserve_next_job = reserve
    deaths = []

    def consume():
        try:
            queue.consume_jobs_pending_queue()
        except BaseException as error:  # noqa: B036
            deaths.append(repr(error))

    with caplog.at_level(logging.ERROR, logger=""):
        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            assert _wait_until(lambda: len(queue.jobs_running_queue) == 3), f"the consumer did not recover: {deaths}"
        finally:
            stop.set()
            thread.join(5)

    assert not deaths
    # Five failures in a row make one log entry with the traceback, not five.
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR and record.exc_info]
    assert len(errors) == 1


def _wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _lane_with_one_older_waiter(queue, monkeypatch):
    """The search lane full, and one search that was pressed earlier waiting for it."""
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 8)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    held = _enqueue_lane(queue, LANE_LIMITS[USER_SEARCH_LANE], USER_SEARCH_LANE, name="Held")
    assert all(queue.force_start_pending_job(job_id, True) for job_id in held)
    older, fresh = _enqueue_lane(queue, 2, USER_SEARCH_LANE, name="Waiting", first=20)
    assert queue.force_start_pending_job(older, True) is False
    return held, older, fresh


def test_a_fresh_press_does_not_take_the_slot_of_a_search_that_has_waited_longer(queue, monkeypatch):
    held, older, fresh = _lane_with_one_older_waiter(queue, monkeypatch)

    queue.jobs_running_queue.popleft()  # one search ends and frees its slot
    started = queue.force_start_pending_job(fresh, True)

    assert started is False
    assert older in [job.job_id for job in queue.jobs_running_queue]
    waiting = next(job for job in queue.jobs_pending_queue if job.job_id == fresh)
    assert waiting.lane_waiting and "other requests" in waiting.progress_message
    assert waiting.origin == "user"

    # It is in line now, and takes the next slot that frees.
    queue.jobs_running_queue.popleft()
    assert queue._reserve_next_job().job_id == fresh


def test_a_press_starts_with_the_older_searches_when_there_is_room_for_all_of_them(queue, monkeypatch):
    held, older, fresh = _lane_with_one_older_waiter(queue, monkeypatch)

    queue.jobs_running_queue.popleft()
    queue.jobs_running_queue.popleft()
    started = queue.force_start_pending_job(fresh, True)

    assert started is True
    running = [job.job_id for job in queue.jobs_running_queue]
    assert older in running and fresh in running
    assert not queue.jobs_pending_queue


def test_the_search_that_waited_longest_is_the_one_a_press_lets_in(queue, monkeypatch):
    held, older, fresh = _lane_with_one_older_waiter(queue, monkeypatch)
    middle = _enqueue_lane(queue, 1, "user_search", name="Middle", first=40)[0]
    assert queue.force_start_pending_job(middle, True) is False

    queue.jobs_running_queue.popleft()
    # The press is on a later search, and the one that waited longest is the one that starts.
    assert queue.force_start_pending_job(middle, True) is False

    assert older in [job.job_id for job in queue.jobs_running_queue]
    assert {job.job_id for job in queue.jobs_pending_queue} == {fresh, middle}


def test_a_press_in_a_lane_with_no_waiter_still_starts_at_once(queue, monkeypatch):
    held, older, fresh = _lane_with_one_older_waiter(queue, monkeypatch)
    queue.remove_job_from_pending_queue(older)

    queue.jobs_running_queue.popleft()

    assert queue.force_start_pending_job(fresh, True) is True


class _CantStartThread:
    """A thread the operating system refuses to start, as it does when the process is out of threads."""

    failures = None  # how many starts fail before one succeeds, or None to fail always
    attempts = []

    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.daemon = False

    def start(self):
        type(self).attempts.append(1)
        if type(self).failures is not None and len(type(self).attempts) > type(self).failures:
            return
        raise RuntimeError("can't start new thread")


@pytest.fixture
def cant_start(monkeypatch):
    class Refusing(_CantStartThread):
        failures = None
        attempts = []

    monkeypatch.setattr("app.jobs_queue.Thread", Refusing)
    return Refusing


def test_a_pressed_search_whose_thread_cannot_start_fails_and_frees_its_slots(queue, monkeypatch, cant_start):
    from app.config import settings
    from app.jobs_queue import USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    job_id = _enqueue_lane(queue, 1, USER_SEARCH_LANE)[0]

    assert queue.force_start_pending_job(job_id, True) is False

    assert not queue.jobs_running_queue
    assert not queue.jobs_pending_queue
    failed = next(job for job in queue.jobs_failed_queue if job.job_id == job_id)
    assert failed.status == "failed"
    assert failed.error["reason"] == "could_not_start"
    assert failed.error["message"]


def test_a_search_that_cannot_start_announces_its_failure(queue, monkeypatch, cant_start):
    from app.config import settings
    from app.jobs_queue import USER_SEARCH_LANE

    events = []
    monkeypatch.setattr("app.jobs_queue._job_event", lambda action, payload: events.append((action, payload)))
    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    job_id = _enqueue_lane(queue, 1, USER_SEARCH_LANE)[0]

    queue.force_start_pending_job(job_id, True)

    assert any(action == "update" and payload.get("job_id") == job_id and payload.get("status") == "failed"
               and payload.get("error", {}).get("reason") == "could_not_start"
               for action, payload in events)


def test_repeated_thread_failures_never_fill_the_lane_or_the_general_limit(queue, monkeypatch, cant_start):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 2)
    count = LANE_LIMITS[USER_SEARCH_LANE] * 3
    for job_id in _enqueue_lane(queue, count, USER_SEARCH_LANE):
        queue.force_start_pending_job(job_id, True)

    assert not queue.jobs_running_queue
    assert len(queue.jobs_failed_queue) == count

    # The lane is still free: a search that can start does.
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    fresh = _enqueue_lane(queue, 1, USER_SEARCH_LANE, name="Fresh", first=90)[0]
    assert queue.force_start_pending_job(fresh, True) is True


def test_one_thread_failure_does_not_stop_the_other_searches_a_press_starts(queue, monkeypatch, cant_start):
    from app.config import settings
    from app.jobs_queue import USER_SEARCH_LANE

    cant_start.failures = 1
    monkeypatch.setattr(settings.general, "concurrent_jobs", 8)
    older, fresh = _enqueue_lane(queue, 2, USER_SEARCH_LANE)
    # Both are in line for the lane at once: the older one is told to wait, then one press starts both.
    older_job = next(job for job in queue.jobs_pending_queue if job.job_id == older)
    older_job.lane_waiting = True

    queue.force_start_pending_job(fresh, True)

    assert [job.job_id for job in queue.jobs_failed_queue] == [older]
    assert [job.job_id for job in queue.jobs_running_queue] == [fresh]


def test_the_consumer_fails_a_job_whose_thread_cannot_start_and_goes_on(queue, monkeypatch, cant_start):
    import threading
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 2)
    monkeypatch.setattr("app.jobs_queue.sleep", lambda seconds: time.sleep(0.001))
    cant_start.failures = 3
    _enqueue(queue, 5)
    real = queue._reserve_next_job
    stop = threading.Event()

    def reserve():
        if stop.is_set():
            raise SystemExit
        return real()

    queue._reserve_next_job = reserve
    consumer = threading.Thread(target=queue.consume_jobs_pending_queue, daemon=True)
    consumer.start()
    try:
        assert _wait_until(lambda: len(queue.jobs_failed_queue) == 3 and len(queue.jobs_running_queue) == 2)
    finally:
        stop.set()
        consumer.join(5)

    assert all(job.error["reason"] == "could_not_start" for job in queue.jobs_failed_queue)
    assert not queue.jobs_pending_queue


def test_a_press_starts_waiters_in_the_order_the_consumer_would_when_one_was_moved(queue, monkeypatch):
    """Move to bottom is the person's own ordering. A press must honour it exactly as the consumer does,
    or a search sent to the back is still the first one a press lets in."""
    held, older, fresh = _lane_with_one_older_waiter(queue, monkeypatch)
    assert queue.move_job_in_pending_queue(older, "bottom") is True

    queue.jobs_running_queue.popleft()  # one slot frees
    started = queue.force_start_pending_job(fresh, True)

    assert started is True
    assert fresh in [job.job_id for job in queue.jobs_running_queue]
    assert [job.job_id for job in queue.jobs_pending_queue] == [older]

    # And the one sent to the back takes the next slot that frees.
    queue.jobs_running_queue.popleft()
    assert queue._reserve_next_job().job_id == older


def test_a_press_agrees_with_the_consumer_on_who_is_next_after_a_move_to_the_top(queue, monkeypatch):
    held, older, fresh = _lane_with_one_older_waiter(queue, monkeypatch)
    middle = _enqueue_lane(queue, 1, "user_search", name="Middle", first=40)[0]
    assert queue.force_start_pending_job(middle, True) is False
    assert queue.move_job_in_pending_queue(middle, "top") is True

    queue.jobs_running_queue.popleft()
    queue.force_start_pending_job(fresh, True)

    assert middle in [job.job_id for job in queue.jobs_running_queue]
    assert older not in [job.job_id for job in queue.jobs_running_queue]


def test_a_press_in_one_lane_never_starts_the_waiters_of_another_lane_past_their_cap(queue, monkeypatch):
    from app.config import settings
    from app.jobs_queue import LANE_LIMITS, USER_ACTION_LANE, USER_SEARCH_LANE

    monkeypatch.setattr(settings.general, "concurrent_jobs", 8)
    monkeypatch.setattr("app.jobs_queue.Thread", _IdleThread)
    held = _enqueue_lane(queue, LANE_LIMITS[USER_SEARCH_LANE], USER_SEARCH_LANE, name="Held")
    assert all(queue.force_start_pending_job(job_id, True) for job_id in held)
    waiting_search = _enqueue_lane(queue, 1, USER_SEARCH_LANE, name="Waiting", first=20)[0]
    assert queue.force_start_pending_job(waiting_search, True) is False
    action = _enqueue_lane(queue, 1, USER_ACTION_LANE, name="Retry", first=30)[0]

    assert queue.force_start_pending_job(action, True) is True

    assert [job.job_id for job in queue.jobs_pending_queue] == [waiting_search]
    assert len([job for job in queue.jobs_running_queue if job.lane == USER_SEARCH_LANE]) == \
        LANE_LIMITS[USER_SEARCH_LANE]
    assert action in [job.job_id for job in queue.jobs_running_queue]


@pytest.mark.parametrize("destination", ["sideways", "", None, "TOP"])
def test_a_move_to_a_destination_that_does_not_exist_leaves_the_job_where_it_was(queue, destination):
    ids = _enqueue_lane(queue, 3, "user_search")
    order = [job.job_id for job in queue.jobs_pending_queue]

    assert queue.move_job_in_pending_queue(ids[1], destination) is False

    assert [job.job_id for job in queue.jobs_pending_queue] == order
    assert next(job for job in queue.jobs_pending_queue if job.job_id == ids[1]).status == "pending"


def test_moving_a_job_holds_the_queue_lock_while_the_queue_changes(queue):
    """The consumer, the press and the API all edit the pending queue, so a move may not change it unlocked.
    Each mutation asks, from another thread, whether the lock is held: a lock someone holds cannot be taken."""
    import threading
    from collections import deque

    class Probing(deque):
        held_during = []

        def _probe(self):
            taken = []

            def try_lock():
                if queue._queue_lock.acquire(blocking=False):
                    queue._queue_lock.release()
                    taken.append(True)

            probe = threading.Thread(target=try_lock)
            probe.start()
            probe.join(5)
            self.held_during.append(not taken)

        def remove(self, value):
            self._probe()
            super().remove(value)

        def append(self, value):
            self._probe()
            super().append(value)

        def appendleft(self, value):
            self._probe()
            super().appendleft(value)

    ids = _enqueue_lane(queue, 3, "user_search")
    queue.jobs_pending_queue = Probing(queue.jobs_pending_queue)

    assert queue.move_job_in_pending_queue(ids[1], "top") is True
    assert queue.move_job_in_pending_queue(ids[2], "bottom") is True

    assert len(Probing.held_during) == 4  # a remove and an add for each move
    assert all(Probing.held_during), "the pending queue was changed without the queue lock"

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

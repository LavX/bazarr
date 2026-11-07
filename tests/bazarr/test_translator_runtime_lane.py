"""The translation lane under the v2 runtime switch: passed over when on, unchanged when off.

Off is the regression guard: a translation held back by its lane holds the line exactly as it always
did, the old factory path is the one in use, and none of the new modules is reached.
"""
from pathlib import Path

import pytest

import app.database  # noqa: F401

TRANSLATION = ("subtitles.tools.translate.main", "translate_subtitles_file")
EDITOR_TRANSLATION = ("subtitles.tools.translate.editor", "translate_editor_lines")
MASS_OPERATION = ("subtitles.mass_operations", "mass_batch_operation")
SEARCH = ("tests.fake", "work")
CHILDREN = 20


@pytest.fixture
def queue(monkeypatch):
    from app.jobs_queue import JobsQueue

    monkeypatch.setattr("app.jobs_queue.event_stream", lambda *args, **kwargs: None)
    return JobsQueue()


@pytest.fixture
def limits(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 4)
    monkeypatch.setattr(settings.translator, "openrouter_max_concurrent", 1)


@pytest.fixture
def switch_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr("subtitles.tools.translate.runtime.runtime_v2_enabled", lambda: True)


@pytest.fixture
def switch_off(monkeypatch):
    """The real switch, off, with proof that nothing looked at the database to decide."""
    from app.config import settings
    from subtitles.tools.translate import runtime

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(runtime, "_runtime_tables_exist", lambda: pytest.fail("the database was consulted"))
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: pytest.fail("the runtime was asked"))


def _feed(queue, name, job, index):
    module, func = job
    return queue.feed_jobs_pending_queue(job_name=name, module=module, func=func, kwargs={"index": index})


def _one_parent_behind_twenty_children(queue):
    """One translation already holds the only lane slot, twenty children wait, the parent is queued last."""
    _feed(queue, "Translating running (EN to FR)", TRANSLATION, 0)
    assert queue._reserve_next_job().func == "translate_subtitles_file"
    for index in range(1, CHILDREN + 1):
        _feed(queue, f"Translating child {index} (EN to FR)", TRANSLATION, index)
    _feed(queue, "Mass translate", MASS_OPERATION, 99)


def _running_translations(queue):
    from app.jobs_queue import is_translation_job

    return sum(1 for job in queue.jobs_running_queue if is_translation_job(job))


def test_with_the_switch_on_the_parent_is_not_held_behind_children_waiting_for_the_lane(
        queue, limits, switch_on):
    _one_parent_behind_twenty_children(queue)

    parent = queue._reserve_next_job()

    assert parent is not None and parent.func == "mass_batch_operation", "the parent waited behind its children"
    assert _running_translations(queue) == 1, "the parent took a translation slot"
    assert queue._reserve_next_job() is None, "a child ran past the lane cap"
    assert len(queue.jobs_pending_queue) == CHILDREN


def test_with_the_switch_on_children_still_run_in_order_one_at_a_time(queue, limits, switch_on):
    _one_parent_behind_twenty_children(queue)
    queue._reserve_next_job()  # the parent
    started = []
    for _ in range(CHILDREN):
        running = next(job for job in queue.jobs_running_queue if job.func == "translate_subtitles_file")
        queue.jobs_running_queue.remove(running)  # it finished
        child = queue._reserve_next_job()
        assert child is not None and _running_translations(queue) == 1
        started.append(child.kwargs["index"])
        assert queue._reserve_next_job() is None
    assert started == list(range(1, CHILDREN + 1))


def test_with_the_switch_on_a_search_behind_a_full_lane_starts(queue, limits, switch_on):
    _feed(queue, "Translating a (EN to FR)", TRANSLATION, 0)
    _feed(queue, "Translating b (EN to FR)", EDITOR_TRANSLATION, 1)
    _feed(queue, "Searching", SEARCH, 2)

    assert queue._reserve_next_job().func == "translate_subtitles_file"
    assert queue._reserve_next_job().func == "work"
    assert queue._reserve_next_job() is None


def test_with_the_switch_on_a_full_general_limit_still_holds_everything(queue, monkeypatch, switch_on):
    from app.config import settings

    monkeypatch.setattr(settings.general, "concurrent_jobs", 1)
    monkeypatch.setattr(settings.translator, "openrouter_max_concurrent", 1)
    _feed(queue, "Translating a (EN to FR)", TRANSLATION, 0)
    _feed(queue, "Translating b (EN to FR)", TRANSLATION, 1)
    _feed(queue, "Searching", SEARCH, 2)

    assert queue._reserve_next_job() is not None
    assert queue._reserve_next_job() is None


def test_with_the_switch_off_a_translation_held_by_the_lane_holds_the_line(queue, limits, switch_off):
    _one_parent_behind_twenty_children(queue)

    assert queue._reserve_next_job() is None, "the line moved with the switch off"
    assert len(queue.jobs_pending_queue) == CHILDREN + 1


def test_with_the_switch_off_the_old_lane_decides_exactly_as_before(queue, limits, switch_off):
    _feed(queue, "Translating a (EN to FR)", TRANSLATION, 0)
    _feed(queue, "Example in the editor", EDITOR_TRANSLATION, 1)
    _feed(queue, "Searching", SEARCH, 2)

    assert queue._reserve_next_job().func == "translate_subtitles_file"
    assert queue._reserve_next_job() is None, "a second translation ran beside the first"
    assert queue._reserve_next_job() is None, "a job behind a held translation ran"


def test_with_the_switch_off_the_runtime_package_is_not_imported_by_the_queue(queue, limits, switch_off,
                                                                              monkeypatch):
    import sys

    for name in [name for name in sys.modules if name.startswith("subtitles.tools.translate.runtime")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "subtitles.tools.translate.runtime", None)  # an import now raises
    _one_parent_behind_twenty_children(queue)
    queue._warm_translation_runtime_check()
    assert queue._reserve_next_job() is None


def test_with_the_switch_on_the_consumer_checks_the_tables_outside_the_lock(queue, limits, monkeypatch):
    from app.config import settings
    from subtitles.tools.translate import runtime

    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    held = []
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: held.append(queue._queue_lock._is_owned()) or True)
    queue._warm_translation_runtime_check()
    assert held == [False]


def test_the_lane_is_still_the_same_two_functions():
    from app.jobs_queue import TRANSLATION_JOBS

    assert TRANSLATION_JOBS == frozenset({TRANSLATION, EDITOR_TRANSLATION})


def test_the_old_translation_paths_do_not_reach_the_new_runtime():
    """Enqueue, the gate and the editor are the old ones: the factory path, with no hook into the runtime."""
    from subtitles.tools.translate import main
    from subtitles.tools.translate.services.translator_factory import TranslatorFactory

    assert main.TranslatorFactory is TranslatorFactory
    base = Path(__file__).parents[2] / "bazarr/subtitles/tools/translate"
    for name in ("main.py", "editor.py", "availability.py"):
        text = (base / name).read_text(encoding="utf-8")
        assert "translate.runtime" not in text and "from .runtime" not in text and "import runtime" not in text, name


def test_the_tables_only_the_migration_makes_are_left_out_of_the_boot_create_all(tmp_path):
    import sqlalchemy as sa

    from app import database

    assert database.MIGRATION_ONLY_TABLES >= {"translator_jobs", "translator_checkpoints"}
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'boot.db'}")
    try:
        # The boot's create_all with an ordinary table added, to show the filter lets the others through.
        ordinary = "table_settings_languages"
        names = set(database.MIGRATION_ONLY_TABLES) | {ordinary}
        wanted = [table for table in database.metadata.sorted_tables if table.name in names]
        assert {table.name for table in wanted} == names
        database.metadata.create_all(engine, tables=[table for table in wanted
                                                     if table.name not in database.MIGRATION_ONLY_TABLES])
        assert set(sa.inspect(engine).get_table_names()) == {ordinary}
    finally:
        engine.dispose()

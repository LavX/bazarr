"""The hourly scan task, and the two hooks that queue a shadow refresh when an arr instance or a master toggle
changes."""
import sys
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_source_helpers import add_source, pristine_media_source_modules, stamped_video  # noqa: F401


@pytest.fixture
def source(source_db, tmp_path):
    source_id, library_id, root = add_source(source_db, tmp_path)
    return SimpleNamespace(session=source_db, id=source_id, library_id=library_id, root=root)


def _set(source, **values):
    from media_sources import repository
    repository.update_source(source.id, source.session, **values)


def test_a_source_is_due_when_its_last_scan_is_older_than_its_frequency(source):
    from media_sources import schedule

    now = datetime(2026, 5, 1, 12, 0)
    assert [row.id for row in schedule.due_sources(now, source.session)] == [source.id]  # never scanned

    _set(source, last_scan_started_at=now - timedelta(hours=13), last_scan_finished_at=now - timedelta(hours=13))
    assert [row.id for row in schedule.due_sources(now, source.session)] == [source.id]
    _set(source, last_scan_started_at=now - timedelta(hours=3), last_scan_finished_at=now - timedelta(hours=3))
    assert schedule.due_sources(now, source.session) == []


def test_manual_only_disabled_and_running_sources_are_not_due(source):
    from media_sources import schedule

    now = datetime(2026, 5, 1, 12, 0)
    _set(source, scan_frequency_hours=0)
    assert schedule.due_sources(now, source.session) == []
    _set(source, scan_frequency_hours=12, enabled=0)
    assert schedule.due_sources(now, source.session) == []
    # A scan that started a moment ago and has not finished is running.
    _set(source, enabled=1, last_scan_started_at=now - timedelta(minutes=5),
         last_scan_finished_at=now - timedelta(hours=30))
    assert schedule.due_sources(now, source.session) == []
    # One that started long ago and never finished crashed: it is due again.
    _set(source, last_scan_started_at=now - timedelta(hours=7), last_scan_finished_at=now - timedelta(hours=30))
    assert [row.id for row in schedule.due_sources(now, source.session)] == [source.id]


def test_the_hourly_task_queues_a_scan_for_each_due_source_and_survives_a_failure(source, tmp_path, monkeypatch):
    from media_sources import repository, schedule

    other = repository.create_source(kind="folder", name="Second", target=source.session)
    repository.create_library(source_id=other, content_kind="movie", locator=str(tmp_path / "Second"),
                              target=source.session)
    queued = []

    def queue(source_id, library_id=None):
        if source_id == source.id:
            raise RuntimeError("queue is down")
        queued.append(source_id)

    monkeypatch.setattr(schedule, "scan_media_source", queue)

    schedule.scan_due_media_sources()

    assert queued == [other]


def test_the_task_registers_while_an_enabled_source_exists_and_unregisters_when_none_does(source):
    from media_sources import repository, schedule

    jobs = {}

    class FakeScheduler:
        def add_job(self, function, trigger, **kwargs):
            jobs[kwargs["id"]] = (function, trigger, kwargs)

        def get_job(self, job_id):
            return jobs.get(job_id)

        def remove_job(self, job_id):
            del jobs[job_id]

    aps = FakeScheduler()
    schedule.configure_source_jobs(aps, source.session)
    function, trigger, kwargs = jobs[schedule.JOB_ID]
    assert function is schedule.scan_due_media_sources and trigger == "interval" and kwargs["hours"] == 1
    assert kwargs["replace_existing"] is True and kwargs["max_instances"] == 1
    wanted, trigger, options = jobs[schedule.WANTED_JOB_ID]
    assert wanted is schedule.wanted_search_missing_subtitles_sources
    assert trigger == "interval" and options["hours"] == 6
    assert options["kwargs"] == {"wait_for_completion": True}

    _set(source, enabled=0)
    schedule.configure_source_jobs(aps, source.session)
    assert jobs == {}
    # Nothing to remove, nothing registered, and no error.
    schedule.configure_source_jobs(aps, source.session)
    assert jobs == {}

    _set(source, enabled=1)
    schedule.configure_source_jobs(aps, source.session)
    assert schedule.JOB_ID in jobs and schedule.WANTED_JOB_ID in jobs
    repository.delete_source(source.id, source.session)
    schedule.configure_source_jobs(aps, source.session)
    assert jobs == {}


@pytest.fixture
def real_scheduler(source_db, monkeypatch, scheduler_runtime):
    """The application's Scheduler over a recording stand-in for APScheduler."""
    from app import scheduler as sched_module

    jobs = {}
    fake_aps = MagicMock()
    fake_aps.get_jobs.return_value = []
    fake_aps.add_job.side_effect = lambda function, trigger=None, **kwargs: jobs.__setitem__(kwargs.get("id"), function)
    fake_aps.get_job.side_effect = lambda job_id: jobs.get(job_id)
    fake_aps.remove_job.side_effect = lambda job_id: jobs.pop(job_id)
    monkeypatch.setattr(sched_module, "BackgroundScheduler", lambda *a, **k: fake_aps)
    monkeypatch.setattr(sched_module, "database", source_db)
    return sched_module.Scheduler(), jobs


def test_the_scheduler_rebuilds_the_scan_task_whenever_the_tasks_are_refreshed(source, real_scheduler):
    scheduler, jobs = real_scheduler
    assert "scan_media_sources" in jobs

    _set(source, enabled=0)
    scheduler.update_configurable_tasks()
    assert "scan_media_sources" not in jobs

    _set(source, enabled=1)
    scheduler.update_configurable_tasks()
    assert "scan_media_sources" in jobs


def test_a_failure_configuring_the_scan_task_never_stops_the_rest_of_the_schedule(source, real_scheduler,
                                                                                  monkeypatch, caplog):
    from media_sources import schedule

    scheduler, jobs = real_scheduler

    def broken(*args, **kwargs):
        raise RuntimeError("no table")

    monkeypatch.setattr(schedule, "configure_source_jobs", broken)
    scheduler.update_configurable_tasks()
    assert "could not configure the media source tasks" in caplog.text
    assert "provider_hub_update_check" in jobs


def test_changes_refresh_the_scheduled_tasks_only_once_the_scheduler_exists(monkeypatch):
    from media_sources import schedule

    monkeypatch.delitem(sys.modules, "app.scheduler", raising=False)
    schedule.refresh_tasks()  # nothing to refresh, and importing the scheduler from here would start it
    assert "app.scheduler" not in sys.modules

    calls = []
    fake = ModuleType("app.scheduler")
    fake.scheduler = SimpleNamespace(update_configurable_tasks=lambda: calls.append(True))
    monkeypatch.setitem(sys.modules, "app.scheduler", fake)
    schedule.refresh_tasks()
    assert calls == [True]

    fake.scheduler = SimpleNamespace(update_configurable_tasks=lambda: 1 / 0)
    schedule.refresh_tasks()  # logged, never raised


# The two hooks

def test_creating_enabling_or_disabling_an_arr_instance_queues_a_refresh_for_its_kind(source, monkeypatch):
    from arr_instances import service
    from media_sources import shadows

    queued = []
    monkeypatch.setattr(shadows, "refresh_shadows", lambda kind=None, job_id=None: queued.append(kind) or 4)
    stamped_video(source.root / "Film.2001.mkv")
    from media_sources.scanner import run_scan
    run_scan(source.id)
    # refresh_runtime follows a committed create or update; the rest of it needs the scheduler and the feeds.
    monkeypatch.setattr(service, "mirror_scalar_config_from_default", lambda session, kind: None)

    service.refresh_runtime("sportarr", instance_id=3)
    service.refresh_runtime("radarr", instance_id=4)

    assert queued == ["sportarr", "radarr"]


def test_a_toggle_that_really_changed_queues_a_refresh_for_that_kind_only(monkeypatch, pristine_media_source_modules):  # noqa: F811
    from app import config
    from media_sources import shadows

    queued = []
    monkeypatch.setattr(shadows, "queue_shadow_refresh", lambda kind=None: queued.append(kind))
    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(config.settings.validators, "validate", lambda: None)
    monkeypatch.setattr(config, "restore_persisted_settings", lambda: None)
    monkeypatch.setattr(config.settings.general, "use_radarr", False)
    monkeypatch.setattr(config.settings.general, "use_sonarr", False)
    monkeypatch.setattr(config.settings.general, "use_sportarr", False)
    # Imported before the stand-in database, so that nothing keeps the stand-in.
    monkeypatch.setattr("sportarr.scheduler.refresh_sports_runtime", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None), update=lambda model: SimpleNamespace(
            values=lambda **kwargs: None), System=object))
    # The follow-ups that talk to a live scheduler and to feeds are not what is under test.
    for name in ("scheduler", "signalr_client", "event_handler"):
        monkeypatch.setitem(sys.modules, f"app.{name}", MagicMock())

    config.save_settings([("settings-general-use_radarr", ["true"]), ("settings-general-use_sonarr", ["false"])])

    assert queued == ["radarr"]

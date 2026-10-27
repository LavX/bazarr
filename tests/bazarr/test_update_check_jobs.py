# coding=utf-8
"""The update check and the Provider Hub update check run as standard jobs.

Both used to run only on the scheduler's thread: Run now on the Tasks page
showed a spinner, and a failure went to the log and nowhere else. Each now
queues itself, the queue runs it again with a job, and a failure raises with
the reason the drawer shows.
"""

import sys
from types import SimpleNamespace

import pytest

from app.jobs_queue import JobFailed


@pytest.fixture
def queued(monkeypatch):
    """The jobs queue, recording what is enqueued instead of running it."""
    from app import check_update
    from provider_hub import tasks

    calls = []

    def add_job_from_function(job_name, is_progress, progress_max=0, wait_for_completion=False,
                              return_existing=False):
        calls.append({"name": job_name, "wait": wait_for_completion})
        return 12

    fake = SimpleNamespace(add_job_from_function=add_job_from_function,
                           update_job_name=lambda **kwargs: calls.append(("renamed", kwargs["new_job_name"])),
                           update_job_progress=lambda **kwargs: calls.append(("progress", kwargs["progress_message"])))
    monkeypatch.setattr(check_update, "jobs_queue", fake)
    monkeypatch.setattr(tasks, "jobs_queue", fake)
    return calls


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """A frozen install on the master branch, with its releases and config on a temporary disk."""
    from app import check_update

    monkeypatch.setenv("BAZARR_VERSION", "2.7.0")
    monkeypatch.setattr(check_update.settings.general, "branch", "master")
    monkeypatch.setattr(check_update.args, "no_update", False, raising=False)
    monkeypatch.setattr(check_update.args, "config_dir", str(tmp_path), raising=False)
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(check_update, "check_releases", lambda **kwargs: None)
    return tmp_path


def _releases(tmp_path, *names):
    import json

    (tmp_path / "config" / "releases.txt").write_text(json.dumps([
        {"name": name, "prerelease": False, "download_link": f"https://example.invalid/{name}.zip"}
        for name in names]))


def test_run_now_queues_the_check_and_waits_for_it_like_the_other_tasks(queued):
    from app import check_update

    check_update.check_if_new_update(wait_for_completion=True)

    assert queued == [{"name": "Checking for Bazarr+ updates", "wait": True}]


def test_startup_keeps_checking_on_the_main_thread_without_a_job(queued, installed, monkeypatch):
    from app import check_update

    _releases(installed, "v2.7.0")
    check_update.check_if_new_update(startup=True)

    assert queued == []


def test_an_unknown_branch_fails_the_job_but_only_logs_at_startup(queued, installed, monkeypatch, caplog):
    from app import check_update

    monkeypatch.setattr(check_update.settings.general, "branch", "nightly")

    check_update.check_if_new_update(startup=True)
    with pytest.raises(JobFailed) as failure:
        check_update.check_if_new_update(job_id=7)

    assert failure.value.reason == "unknown_branch" and failure.value.retryable is False
    assert "nightly" in str(failure.value)


def test_a_check_that_finds_nothing_new_completes_and_names_itself(queued, installed, monkeypatch):
    from app import check_update

    _releases(installed, "v2.7.0")
    downloads = []
    monkeypatch.setattr(check_update, "download_release", lambda **kwargs: downloads.append(kwargs))

    check_update.check_if_new_update(job_id=7)

    assert downloads == []
    assert ("renamed", "Checked for Bazarr+ updates") in queued


def test_a_check_that_has_nothing_to_compare_with_says_so_in_its_name(queued, installed, monkeypatch):
    from app import check_update

    monkeypatch.delenv("BAZARR_VERSION")
    refreshed = []
    monkeypatch.setattr(check_update, "check_releases", lambda **kwargs: refreshed.append(kwargs))

    check_update.check_if_new_update(job_id=7)

    # The release list is still refreshed; only the comparison is skipped.
    assert refreshed == [{"startup": True}]
    assert queued == [("renamed", "Skipped the update check: Bazarr+ is running from source")]


def test_a_newer_release_is_downloaded_for_the_job(queued, installed, monkeypatch):
    from app import check_update

    _releases(installed, "v2.8.0")
    downloads = []
    monkeypatch.setattr(check_update, "download_release", lambda **kwargs: downloads.append(kwargs))

    check_update.check_if_new_update(job_id=7)

    assert downloads == [{"url": "https://example.invalid/v2.8.0.zip", "job_id": 7}]


class _Download:
    def __init__(self, status_code=200, content=b"zip"):
        self.status_code, self.content = status_code, content

    def __bool__(self):
        return self.status_code < 400


def _fails(call, reason):
    with pytest.raises(JobFailed) as failure:
        call()
    assert failure.value.reason == reason
    assert str(failure.value).strip()
    return failure.value


def test_every_way_the_download_can_fail_names_what_to_do(installed, monkeypatch, caplog):
    import requests

    from app import check_update

    applied = []
    monkeypatch.setattr(check_update, "apply_update", lambda job_id=None: applied.append(job_id))

    # The server refuses the file: the response is falsy, and was ignored.
    monkeypatch.setattr(check_update.requests, "get", lambda *args, **kwargs: _Download(404))
    with caplog.at_level("WARNING"):
        failure = _fails(lambda: check_update.download_release("https://example.invalid/r.zip", job_id=7),
                         "download_refused")
    assert "404" in str(failure)
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    # The connection fails.
    def offline(*args, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(check_update.requests, "get", offline)
    _fails(lambda: check_update.download_release("https://example.invalid/r.zip", job_id=7), "download_failed")

    # The file cannot be saved.
    monkeypatch.setattr(check_update.requests, "get", lambda *args, **kwargs: _Download())
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))
    _fails(lambda: check_update.download_release("https://example.invalid/r.zip", job_id=7), "update_not_saved")
    assert applied == []


def test_the_update_folder_that_cannot_be_made_fails_the_job(installed, monkeypatch):
    from app import check_update

    monkeypatch.setattr(check_update.os, "makedirs", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ro")))

    _fails(lambda: check_update.download_release("https://example.invalid/r.zip", job_id=7),
           "update_folder_unwritable")
    # At startup nothing can show it, so it is still only logged.
    check_update.download_release("https://example.invalid/r.zip")


def test_a_release_that_cannot_be_unpacked_fails_the_job(installed, monkeypatch):
    from app import check_update

    update = installed / "update"
    update.mkdir()
    (update / "bazarr.zip").write_bytes(b"not a zip")
    restarted = []
    # Importing app.server binds the web port, so stand a stub in for it.
    monkeypatch.setitem(sys.modules, "app.server",
                        SimpleNamespace(webserver=SimpleNamespace(restart=lambda: restarted.append(True))))

    _fails(lambda: check_update.apply_update(job_id=7), "update_not_applied")

    assert restarted == []
    # The archive is still removed, so the next check starts clean.
    assert not (update / "bazarr.zip").exists()
    # Startup keeps logging and carries on.
    (update / "bazarr.zip").write_bytes(b"not a zip")
    check_update.apply_update()


def test_the_scheduled_update_check_waits_for_its_job_like_the_other_tasks(monkeypatch):
    from unittest.mock import MagicMock

    from app import scheduler as scheduler_module

    monkeypatch.setenv("BAZARR_VERSION", "2.7.0")
    monkeypatch.setattr(scheduler_module.args, "no_update", False, raising=False)
    # The scheduler imports the check only when updates are on at import time,
    # so another test file may have loaded it with the name unbound.
    monkeypatch.setattr(scheduler_module, "check_if_new_update", MagicMock(), raising=False)
    instance = MagicMock()
    instance.aps_scheduler = MagicMock()

    for auto_update in (True, False):
        instance.aps_scheduler.add_job.reset_mock()
        monkeypatch.setattr(scheduler_module.settings.general, "auto_update", auto_update)
        scheduler_module.Scheduler._Scheduler__update_bazarr_task(instance)
        update = next(call for call in instance.aps_scheduler.add_job.mock_calls
                      if call.kwargs.get("id") == "update_bazarr")
        assert update.kwargs["kwargs"] == {"wait_for_completion": True}


def test_the_provider_hub_check_queues_itself_and_runs_with_a_job(queued, monkeypatch):
    from provider_hub import tasks

    assert tasks.provider_hub_check_updates(wait_for_completion=True) == 12
    assert queued == [{"name": "Checking Provider Hub updates", "wait": True}]

    steps = []
    monkeypatch.setattr(tasks, "refresh_catalog", lambda: steps.append("refresh"))
    monkeypatch.setattr(tasks, "failed_catalog_sources", lambda: [])
    monkeypatch.setattr(tasks, "check_updates",
                        lambda: steps.append("check") or {"message": "2 update(s) available: A, B"})

    assert tasks.provider_hub_check_updates(job_id=3) == {"message": "2 update(s) available: A, B"}
    assert steps == ["refresh", "check"]
    assert ("progress", "2 update(s) available: A, B") in queued


def test_a_source_that_cannot_be_fetched_still_gets_the_check_and_then_fails_the_job(queued, monkeypatch, caplog):
    from provider_hub import tasks

    steps = []
    monkeypatch.setattr(tasks, "refresh_catalog", lambda: steps.append("refresh"))
    monkeypatch.setattr(tasks, "failed_catalog_sources", lambda: ["Mirror (timed out)"])
    monkeypatch.setattr(tasks, "check_updates", lambda: steps.append("check") or {"message": "none"})

    with caplog.at_level("WARNING"):
        failure = _fails(lambda: tasks.provider_hub_check_updates(job_id=3), "catalog_refresh_failed")

    assert str(failure) == "Could not refresh the provider catalog from Mirror (timed out)"
    assert steps == ["refresh", "check"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_refresh_or_check_that_raises_fails_the_job_with_its_own_words(queued, monkeypatch):
    from provider_hub import tasks

    def boom():
        raise RuntimeError("catalog host unreachable")

    monkeypatch.setattr(tasks, "refresh_catalog", boom)
    monkeypatch.setattr(tasks, "check_updates", lambda: {"message": "none"})
    failure = _fails(lambda: tasks.provider_hub_check_updates(job_id=3), "catalog_refresh_failed")
    assert "catalog host unreachable" in str(failure)

    monkeypatch.setattr(tasks, "refresh_catalog", lambda: None)
    monkeypatch.setattr(tasks, "failed_catalog_sources", lambda: [])
    monkeypatch.setattr(tasks, "check_updates", boom)
    failure = _fails(lambda: tasks.provider_hub_check_updates(job_id=3), "update_check_failed")
    assert "catalog host unreachable" in str(failure)

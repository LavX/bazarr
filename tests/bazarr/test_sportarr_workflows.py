"""Real provider and filesystem acceptance for owned sports workflows."""

# ruff: noqa: F811
import importlib.util
import json
from threading import Event

import pytest
import sqlalchemy as sa
from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library, sports  # noqa: F401
from test_sportarr_manual import manual_library  # noqa: F401


def private_queue(monkeypatch):
    import app.jobs_queue as module
    from app.jobs_queue import JobsQueue
    from collections import deque
    from threading import RLock

    monkeypatch.setattr(module, "event_stream", lambda **kwargs: None)
    queue = JobsQueue.__new__(JobsQueue)
    for name in ("pending", "running", "completed", "failed"):
        setattr(queue, f"jobs_{name}_queue", deque())
    queue.current_job_id = 0
    queue._job_id_lock = RLock()
    queue._queue_lock = RLock()
    queue._import_lock = RLock()
    queue._progress_buffer_lock = RLock()
    queue._progress_buffer = {}
    return queue


def test_automatic_workflow_exists():
    assert importlib.util.find_spec("sportarr.automatic"), (
        "Owned automatic sports search is required"
    )


def test_manual_publication_records_final_artifact_proof(manual_library):
    from app.database import TableHistorySports

    service, session, folder = manual_library
    result = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    service.manual_download_sports(61, result, 1)
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    proof = getattr(row, "artifact", None)
    assert proof, "Published sports history requires persisted final-artifact proof"
    import hashlib

    assert (
        json.loads(proof)["sha256"]
        == hashlib.sha256((folder / "1/event.en.srt").read_bytes()).hexdigest()
    )


@pytest.fixture
def workflow_library(manual_library, monkeypatch):
    assert importlib.util.find_spec("sportarr.automatic"), (
        "Owned automatic sports search is required"
    )
    from sportarr import automatic, history, workflows
    from app import database as db

    service, session, folder = manual_library
    monkeypatch.setattr(history, "notify", lambda *args: None)
    monkeypatch.setattr(workflows, "jobs_queue", private_queue(monkeypatch))
    for module in (automatic, history, workflows):
        monkeypatch.setattr(module, "database", session)
    for path in folder.glob("*/event.*.srt"):
        path.unlink()
    session.execute(
        sa.insert(db.TableLanguagesProfiles).values(
            profileId=1,
            name="English",
            items=json.dumps(
                [
                    {
                        "id": 1,
                        "language": "en",
                        "forced": "False",
                        "hi": "False",
                        "audio_exclude": "False",
                        "audio_only_include": "False",
                    }
                ]
            ),
            originalFormat=0,
        )
    )
    db.update_profile_id_list.invalidate()
    session.execute(
        sa.update(db.TableSportsLeagues).values(
            profileId=1, monitored="True", tags="[]"
        )
    )
    session.execute(
        sa.update(db.TableSportsEvents).values(
            monitored="True", missing_subtitles="['en']"
        )
    )
    for owner in (1, 2):
        session.execute(
            sa.update(db.TableArrInstances)
            .where(db.TableArrInstances.id == owner)
            .values(options=json.dumps({"sports_settings": {"minimum_score": 50}}))
        )
    yield automatic, history, workflows, service, session, folder


def test_automatic_download_and_blacklist_roundtrip_are_owned(workflow_library):
    from app.database import TableHistorySports, TableBlacklistSports

    automatic, history, workflows, service, session, folder = workflow_library
    result = automatic.search_event(61, 1)
    assert result["status"] == "downloaded"
    assert (folder / "1/event.en.srt").exists()
    assert not (folder / "2/event.en.srt").exists()
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    assert (row.event_id, row.arr_instance_id, row.action) == (61, 1, 1)
    outcome = history.blacklist_history(row.id, 1)
    assert outcome["file_status"] == "deleted"
    assert outcome["replacement"]["status"] == "no_result"
    assert not (folder / "1/event.en.srt").exists()
    assert automatic.search_event(62, 2)["status"] == "downloaded"
    excluded = session.execute(sa.select(TableBlacklistSports)).scalar_one()
    history.remove_blacklist(session, excluded.id, 1)
    assert automatic.search_event(61, 1)["status"] == "downloaded"


def test_threshold_no_result_is_explicit_and_has_no_publication(workflow_library):
    from app.database import TableArrInstances, TableHistorySports

    automatic, _, _, _, session, folder = workflow_library
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(options=json.dumps({"sports_settings": {"minimum_score": 100}}))
    )
    result = automatic.search_event(61, 1)
    assert result["status"] == "no_result" and "threshold" in result["message"].lower()
    assert not list(folder.glob("*/event.en.srt"))
    assert not session.execute(sa.select(TableHistorySports)).all()


@pytest.mark.parametrize(
    "changed", ["replacement", "video", "legacy", "disabled", "cancelled"]
)
def test_blacklist_old_history_preserves_unproven_files(workflow_library, changed):
    from app.database import TableHistorySports, TableArrInstances

    automatic, history, _, _, session, folder = workflow_library
    automatic.search_event(61, 1)
    row = session.execute(sa.select(TableHistorySports)).scalar_one()
    subtitle = folder / "1/event.en.srt"
    if changed == "replacement":
        subtitle.write_text("replacement bytes")
    elif changed == "video":
        with (folder / "1/event.mkv").open("ab") as stream:
            stream.write(b"new video")
    elif changed == "legacy":
        session.execute(sa.update(TableHistorySports).values(artifact=None))
    elif changed == "disabled":
        session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 1)
            .values(enabled=0)
        )
    before = subtitle.read_bytes()
    cancel = Event()
    if changed == "cancelled":
        cancel.set()
    if changed in ("disabled", "cancelled"):
        with pytest.raises(ValueError):
            history.blacklist_history(row.id, 1, cancel=cancel)
    else:
        result = history.blacklist_history(row.id, 1)
        assert result["file_status"] == "preserved"
    assert subtitle.read_bytes() == before


def test_wanted_and_mass_respect_owner_monitoring_and_profiles(workflow_library):
    from app.database import TableArrInstances, TableSportsEvents

    automatic, _, workflows, _, session, folder = workflow_library
    assert {item["id"] for item in workflows.wanted_rows(session)} == {61, 62}
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(
            options=json.dumps(
                {"sports_settings": {"minimum_score": 50, "only_monitored": True}}
            )
        )
    )
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 61)
        .values(monitored="False")
    )
    assert [item["id"] for item in workflows.wanted_rows(session)] == [62]
    assert automatic.search_event(61, 1)["status"] == "skipped"
    with pytest.raises(ValueError):
        workflows.sports_download_subtitles(51, 2, job_id="test")
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 61)
        .values(monitored="True")
    )
    workflows.sports_download_subtitles(51, 1, job_id="test")
    assert (folder / "1/event.en.srt").exists()
    assert not (folder / "2/event.en.srt").exists()


def test_scheduled_wanted_job_is_owner_scoped_and_removed_on_disable(
    workflow_library, monkeypatch
):
    from apscheduler.schedulers.background import BackgroundScheduler
    from sportarr.scheduler import configure_sports_jobs
    from app.database import TableArrInstances
    from app.get_args import args

    _, _, _, _, session, _ = workflow_library
    # configure_sports_jobs is gated on the master toggle, so a job test
    # has to turn it on the way a configured install does.
    from app.config import settings
    monkeypatch.setattr(settings.general, "use_sportarr", True)
    monkeypatch.setattr(args, "no_signalr", True)
    scheduler = BackgroundScheduler()
    configure_sports_jobs(scheduler, session)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert (
        jobs["wanted_search_missing_subtitles_sports_1"].kwargs["arr_instance_id"] == 1
    )
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0)
    )
    configure_sports_jobs(scheduler, session)
    jobs = {job.id for job in scheduler.get_jobs()}
    assert "wanted_search_missing_subtitles_sports_1" not in jobs
    assert "wanted_search_missing_subtitles_sports_2" in jobs


def test_actual_queue_capture_and_execution_has_no_hidden_locals(
    workflow_library, monkeypatch
):
    import app.jobs_queue as queue_module
    from app.jobs_queue import JobsQueue

    _, history, workflows, _, _, folder = workflow_library
    monkeypatch.setattr(queue_module, "event_stream", lambda **kwargs: None)
    queue = JobsQueue.__new__(JobsQueue)
    # Preserve the real capture, deduplication and public execution path without
    # starting the global consumer threads in this deterministic private fixture.
    from collections import deque
    from threading import RLock

    for name in ("pending", "running", "completed", "failed"):
        setattr(queue, f"jobs_{name}_queue", deque())
    queue.current_job_id = 0
    queue._job_id_lock = RLock()
    queue._queue_lock = RLock()
    queue._import_lock = RLock()
    queue._progress_buffer_lock = RLock()
    queue._progress_buffer = {}
    monkeypatch.setattr(workflows, "jobs_queue", queue)
    job_id = workflows.automatic_search_sports(61, 1)
    assert job_id
    job = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(job)
    assert queue._run_job(job)
    assert not queue.jobs_failed_queue
    assert job.job_returned_value["data"][0]["status"] == "downloaded"
    assert (folder / "1/event.en.srt").exists()
    assert not (folder / "2/event.en.srt").exists()


@pytest.mark.parametrize("change", ["file", "owner", "file_id", "cancel"])
def test_automatic_rejects_file_changes_during_actual_provider_search(
    workflow_library, monkeypatch, change
):
    from app.database import TableArrInstances, TableSportsEvents, TableHistorySports

    automatic, _, _, _, session, folder = workflow_library
    original = automatic._provider_result
    cancel = Event()

    def changed(*args, **kwargs):
        selected = original(*args)
        if change == "file":
            with (folder / "1/event.mkv").open("ab") as stream:
                stream.write(b"changed")
        elif change == "owner":
            session.execute(
                sa.update(TableArrInstances)
                .where(TableArrInstances.id == 1)
                .values(enabled=0)
            )
        elif change == "file_id":
            session.execute(
                sa.update(TableSportsEvents)
                .where(TableSportsEvents.id == 61)
                .values(file_id=10)
            )
        else:
            cancel.set()
        return selected

    monkeypatch.setattr(automatic, "_provider_result", changed)
    with pytest.raises(ValueError):
        automatic.search_event(61, 1, cancel=cancel)
    assert not (folder / "1/event.en.srt").exists()
    assert session.execute(sa.select(TableHistorySports)).all() == []


def test_real_job_cancellation_releases_waiting_provider_without_publication(
    workflow_library, monkeypatch
):
    from app.jobs_queue import Job, JobCancelled
    from threading import Thread
    import time
    from app.database import TableHistorySports

    automatic, _, workflows, _, session, folder = workflow_library
    started, release = Event(), Event()
    original = automatic.download_best_subtitles

    def delayed(**kwargs):
        started.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(automatic, "download_best_subtitles", delayed)
    job = Job(
        9876, "Sports test", "sportarr.workflows", "automatic_search_sports", [], {}
    )
    workflows.jobs_queue.jobs_running_queue.append(job)

    def stop():
        assert started.wait(5)
        job.cancelled = True

    thread = Thread(target=stop)
    thread.start()
    before = time.monotonic()
    try:
        with pytest.raises(JobCancelled):
            automatic.search_event(
                61, 1, cancel=workflows.SportsJobSignal(1, job.job_id)
            )
        assert time.monotonic() - before < 3
    finally:
        release.set()
        thread.join(5)
        workflows.jobs_queue.jobs_running_queue.remove(job)
    # Allow the isolated provider to finish, then prove its abandoned result never saves.
    for _ in range(100):
        if automatic._provider_slots._value == 4:
            break
        time.sleep(0.05)
    assert automatic._provider_slots._value == 4
    assert not (folder / "1/event.en.srt").exists()
    assert session.execute(sa.select(TableHistorySports)).all() == []


def test_upgrade_targets_keep_colliding_owner_histories_separate(
    workflow_library, monkeypatch
):
    from app.database import TableHistorySports
    from app.config import settings

    automatic, _, workflows, _, session, folder = workflow_library
    monkeypatch.setattr(settings.general, "upgrade_subs", True)
    monkeypatch.setattr(settings.general, "days_to_upgrade_subs", 30)
    automatic.search_event(61, 1)
    automatic.search_event(62, 2)
    rows = workflows.upgrade_rows(session)
    assert {(item["event_id"], item["arr_instance_id"]) for item in rows} == {
        (61, 1),
        (62, 2),
    }
    first = next(item for item in rows if item["arr_instance_id"] == 1)
    session.execute(
        sa.update(TableHistorySports)
        .where(TableHistorySports.id == first["id"])
        .values(score=1)
    )
    workflows.upgrade_sports_subtitles(job_id="fixture", arr_instance_id=1)
    rows = (
        session.execute(sa.select(TableHistorySports).order_by(TableHistorySports.id))
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert (
        rows[-1].arr_instance_id,
        rows[-1].event_id,
        rows[-1].action,
        rows[-1].upgradedFromId,
    ) == (1, 61, 3, first["id"])
    assert len([row for row in rows if row.arr_instance_id == 2]) == 1


def test_search_on_sync_control_queues_only_owned_eligible_missing_events(
    workflow_library, monkeypatch
):
    from app.database import TableArrInstances

    _, _, workflows, _, session, _ = workflow_library
    from types import SimpleNamespace

    jobs = []
    monkeypatch.setattr(
        workflows,
        "jobs_queue",
        SimpleNamespace(
            feed_jobs_pending_queue=lambda **job: jobs.append(job) or len(jobs)
        ),
    )
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(options=json.dumps({"sports_settings": {"search_on_sync": False}}))
    )
    assert workflows.search_after_sync(session, [61], 1) == []
    assert jobs == []
    assert workflows.search_after_sync(session, [62], 2) == [1]
    assert jobs[0]["kwargs"] == {"event_id": 62, "arr_instance_id": 2}


def test_public_api_job_results_and_owner_checks(workflow_library, monkeypatch):
    from flask import Flask
    from flask_restx import Api
    from types import ModuleType
    from pathlib import Path
    import sys
    from app.config import settings
    from app.database import TableArrInstances

    _, _, workflows, _, session, folder = workflow_library
    root = Path(__file__).resolve().parents[2] / "bazarr/api"
    for name in ("_sports_workflows_api", "_sports_workflows_api.sports"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    for name, path in [
        ("utils", "utils.py"),
        ("sports.leagues", "sports/leagues.py"),
        ("sports.workflows", "sports/workflows.py"),
    ]:
        spec = importlib.util.spec_from_file_location(
            "_sports_workflows_api." + name, root / path
        )
        routes = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, routes)
        spec.loader.exec_module(routes)
    monkeypatch.setattr(routes, "database", session)
    app = Flask(__name__)
    Api(app).add_namespace(routes.api_ns_sports_workflows, path="/api")
    client = app.test_client()
    headers = {"X-API-KEY": settings.auth.apikey}
    assert (
        client.post(
            "/api/sports/events/61/automatic", json={"arr_instance_id": 1}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/sports/events/61/automatic",
            json={"arr_instance_id": 2},
            headers=headers,
        ).status_code
        == 400
    )
    response = client.post(
        "/api/sports/events/61/automatic", json={"arr_instance_id": 1}, headers=headers
    )
    assert response.status_code == 202
    job_id = response.json["job_id"]
    job = workflows.jobs_queue.jobs_pending_queue.popleft()
    workflows.jobs_queue.jobs_running_queue.append(job)
    assert workflows.jobs_queue._run_job(job)
    assert (folder / "1/event.en.srt").exists()
    result = client.get(f"/api/sports/jobs/{job_id}?arr_instance_id=1", headers=headers)
    assert result.status_code == 200
    assert result.json["status"] == "completed"
    assert result.json["result"]["data"][0]["status"] == "downloaded"
    assert (
        client.get(
            f"/api/sports/jobs/{job_id}?arr_instance_id=2", headers=headers
        ).status_code
        == 404
    )
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0)
    )
    assert (
        client.get(
            f"/api/sports/jobs/{job_id}?arr_instance_id=1", headers=headers
        ).status_code
        == 400
    )


def test_wanted_no_result_records_failed_attempt_without_cross_owner(workflow_library):
    from app.database import TableArrInstances, TableSportsEvents

    _, _, workflows, _, session, _ = workflow_library
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 1)
        .values(options=json.dumps({"sports_settings": {"minimum_score": 100}}))
    )
    if session.get_bind().dialect.name == "postgresql":

        @sa.event.listens_for(session.get_bind(), "before_cursor_execute")
        def short_lock_wait(conn, cursor, statement, parameters, context, executemany):
            cursor.execute("SET lock_timeout = '750ms'")

    result = workflows.wanted_search_missing_subtitles_sports(
        job_id="fixture", arr_instance_id=1
    )
    assert result["data"][0]["status"] == "no_result"
    session.expire_all()
    assert session.get(TableSportsEvents, 61).failedAttempts != "[]"
    assert session.get(TableSportsEvents, 62).failedAttempts == "[]"


def test_upgrade_preserves_replacement_installed_during_provider_work(
    workflow_library, monkeypatch
):
    from app.database import TableHistorySports
    from app.config import settings

    automatic, _, workflows, _, session, folder = workflow_library
    monkeypatch.setattr(settings.general, "upgrade_subs", True)
    monkeypatch.setattr(settings.general, "days_to_upgrade_subs", 30)
    automatic.search_event(61, 1)
    session.execute(sa.update(TableHistorySports).values(score=1))
    original = automatic._provider_result
    path = folder / "1/event.en.srt"

    def replacement(*args, **kwargs):
        selected = original(*args)
        path.write_text("a newer replacement")
        return selected

    monkeypatch.setattr(automatic, "_provider_result", replacement)
    with pytest.raises(ValueError, match="artifact"):
        workflows.upgrade_sports_subtitles(job_id="fixture", arr_instance_id=1)
    assert path.read_text() == "a newer replacement"
    assert len(session.execute(sa.select(TableHistorySports)).all()) == 1


def test_disabling_owner_removes_pending_and_cancels_only_its_running_jobs(
    workflow_library, monkeypatch
):
    from apscheduler.schedulers.background import BackgroundScheduler
    from sportarr.scheduler import configure_sports_jobs
    from app.database import TableArrInstances
    from app.get_args import args

    _, _, workflows, _, session, _ = workflow_library
    monkeypatch.setattr(args, "no_signalr", True)
    # configure_sports_jobs is gated on the master toggle, so a job test
    # has to turn it on the way a configured install does.
    from app.config import settings
    monkeypatch.setattr(settings.general, "use_sportarr", True)
    queue = workflows.jobs_queue
    pending = workflows.automatic_search_sports(61, 1)
    sibling = workflows.automatic_search_sports(62, 2)
    running_id = workflows.sports_download_subtitles(51, 1)
    running = queue.jobs_pending_queue.pop()
    assert running.job_id == running_id
    running.status = "running"
    queue.jobs_running_queue.append(running)
    session.execute(
        sa.update(TableArrInstances).where(TableArrInstances.id == 1).values(enabled=0)
    )
    configure_sports_jobs(BackgroundScheduler(), session)
    assert [job.job_id for job in queue.jobs_pending_queue] == [sibling]
    assert running.cancelled
    assert pending != sibling


def test_automatic_job_counts_publication_when_index_refresh_fails(
    workflow_library, monkeypatch, caplog
):
    from subtitles.indexer import sports as indexer
    from app.database import TableHistorySports

    automatic, _, workflows, _, session, folder = workflow_library
    original = indexer.store_subtitles_sports

    def fail_after_publication(*args, **kwargs):
        if (folder / "1/event.en.srt").exists():
            raise OSError("injected post-publication indexing failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(indexer, "store_subtitles_sports", fail_after_publication)
    monkeypatch.setattr(automatic, "store_subtitles_sports", fail_after_publication)
    result = workflows._run_events([{"id": 61, "arr_instance_id": 1}], "test-job")
    assert result["data"][0]["downloads"] == 1
    assert result["data"][0]["status"] == "published_with_warnings"
    assert (
        "published" in result["message"].lower()
        and "refresh failed" in result["message"].lower()
    )
    assert (folder / "1/event.en.srt").read_bytes()
    assert session.execute(sa.select(TableHistorySports)).scalar_one().event_id == 61
    assert "index refresh failed" in caplog.text


@pytest.mark.parametrize(
    "boundary",
    [
        "automatic_progress",
        "upgrade_next_file",
        "batch_failure_then_cancelled_publication",
    ],
)
def test_completed_publication_survives_batch_cancellation(
    workflow_library, monkeypatch, caplog, boundary
):
    from app.config import settings
    from app.database import TableHistorySports, TableSportsEvents
    from subtitles import processing
    from subtitles.indexer import sports as indexer
    import shutil

    automatic, _, workflows, _, session, folder = workflow_library
    queue = workflows.jobs_queue
    paths = {61: folder / "1/event.en.srt", 62: folder / "1/second.en.srt"}
    if boundary != "automatic_progress":
        shutil.copyfile(folder / "1/event.mkv", folder / "1/second.mkv")
        session.execute(
            sa.update(TableSportsEvents)
            .where(TableSportsEvents.id == 62)
            .values(
                arr_instance_id=1,
                league_id=51,
                sportarrEventId=9,
                file_id=10,
                path="/sports/second.mkv",
            )
        )
    if boundary == "upgrade_next_file":
        monkeypatch.setattr(settings.general, "upgrade_subs", True)
        monkeypatch.setattr(settings.general, "days_to_upgrade_subs", 30)
        automatic.search_event(61, 1)
        automatic.search_event(62, 1)
        session.execute(
            sa.update(TableHistorySports).values(
                score=1, provider="older-provider", subs_id="older-release"
            )
        )
        job_id = workflows.upgrade_sports_subtitles(arr_instance_id=1)
    elif boundary == "batch_failure_then_cancelled_publication":
        job_id = workflows.wanted_search_missing_subtitles_sports(arr_instance_id=1)
    else:
        job_id = workflows.automatic_search_sports(61, 1)
    history_before = {
        row.id: row.to_dict()
        for row in session.execute(sa.select(TableHistorySports)).scalars()
    }
    bytes_before = {
        event: path.read_bytes() for event, path in paths.items() if path.exists()
    }
    job = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(job)
    calls, provider_calls, publications, published_bytes, final_reports = (
        [],
        [],
        [],
        {},
        [],
    )
    original_search = workflows.search_event
    original_progress = queue.update_job_progress
    original_provider = automatic._provider_result
    original_save = automatic.save_sports_subtitle
    original_index = indexer.store_subtitles_sports

    def search(event_id, owner, **kwargs):
        calls.append(event_id)
        if boundary == "upgrade_next_file" and len(calls) == 2:
            assert len(publications) == 1
            job.cancelled = True
        if boundary == "batch_failure_then_cancelled_publication" and event_id == 61:
            raise OSError("injected prepublication provider failure")
        return original_search(event_id, owner, **kwargs)

    def progress(*args, **kwargs):
        if boundary == "automatic_progress" and kwargs.get("progress_value") == 1:
            assert len(publications) == 1 and len(history_before) == 0
            job.cancelled = True
        if kwargs.get("progress_message") is not None:
            final_reports.append(kwargs)
        return original_progress(*args, **kwargs)

    def provider(*args, **kwargs):
        assert not job.cancelled, "Provider work continued after cancellation"
        provider_calls.append(True)
        return original_provider(*args, **kwargs)

    def save(video, subtitle, candidate, *args, **kwargs):
        result = original_save(video, subtitle, candidate, *args, **kwargs)
        publications.append(result.publication.copy())
        published_bytes[candidate.context.event_id] = paths[
            candidate.context.event_id
        ].read_bytes()
        return result

    def index(*args, **kwargs):
        assert not job.cancelled, "Index work continued after cancellation"
        return original_index(*args, **kwargs)

    monkeypatch.setattr(workflows, "search_event", search)
    monkeypatch.setattr(queue, "update_job_progress", progress)
    monkeypatch.setattr(automatic, "_provider_result", provider)
    monkeypatch.setattr(automatic, "save_sports_subtitle", save)
    monkeypatch.setattr(automatic, "store_subtitles_sports", index)
    monkeypatch.setattr(indexer, "store_subtitles_sports", index)
    if boundary == "batch_failure_then_cancelled_publication":
        original_process = processing.process_subtitle

        def process(*args, **kwargs):
            assert paths[62].read_bytes()
            job.cancelled = True
            return original_process(*args, **kwargs)

        monkeypatch.setattr(processing, "process_subtitle", process)
    assert queue._run_job(job)
    assert job.status == "completed" and job.cancelled
    assert len(queue.jobs_completed_queue) == 1 and not queue.jobs_failed_queue
    result = job.job_returned_value
    assert result is not None and len(result["data"]) == 1
    projection = workflows.sports_job_status(session, job_id, 1)
    assert projection["cancelled"] and projection["status"] == "completed"
    assert projection["result"]["data"] == result["data"]
    assert projection["message"] == projection["result"]["message"] == result["message"]
    assert workflows.sports_job_status(session, job_id, 2) is None
    assert len(provider_calls) == len(publications) == 1
    assert published_bytes and all(published_bytes.values())
    for event, content in (bytes_before | published_bytes).items():
        assert paths[event].read_bytes() == content
    assert set(folder.glob("*/*.en.srt")) == {
        paths[event] for event in bytes_before | published_bytes
    }
    histories = {
        row.id: row.to_dict()
        for row in session.execute(sa.select(TableHistorySports)).scalars()
    }
    assert all(histories[key] == row for key, row in history_before.items())
    outcome = result["data"][0]
    assert outcome["downloads"] == 1
    final = [
        report for report in final_reports if report.get("progress_value") == "max"
    ]
    assert len(final) == 1 and final[0]["allow_cancelled"] is True
    assert final[0]["progress_message"] == result["message"]
    if boundary == "batch_failure_then_cancelled_publication":
        assert calls == [61, 62] and len(histories) == 0
        assert result["failed_events"] == [61]
        assert "1 other sports searches failed" in result["message"]
        assert outcome["publication"] == publications[0]
        assert outcome["cancelled"] is True
        expected = {
            "published": True,
            "status": "published_with_warnings",
            "processing": "cancelled",
            "artifact": "not_started",
            "history": "not_started",
            "index": "cancelled",
            "refresh_attempts": 0,
            "refresh_queued": False,
            "failed_phase": "processing",
            "cancelled": True,
        }
        assert {key: publications[0][key] for key in expected} == expected
        assert "injected prepublication provider failure" in caplog.text
        assert "processing did not complete" in caplog.text
    else:
        assert outcome["status"] == "downloaded" and not outcome.get("cancelled")
        assert publications[0]["status"] == "published"
        assert publications[0]["history"] == "committed"
        assert publications[0]["index"] == "completed"
        assert len(histories) == len(history_before) + 1
        if boundary == "automatic_progress":
            assert calls == [61]
        else:
            assert set(calls) == {61, 62} and len(calls) == 2
            assert "Checked 1 sports upgrades; 1 published" in result["message"]


@pytest.mark.parametrize("flow", ["automatic", "upgrade"])
def test_batch_cancellation_before_publication_has_no_result(
    workflow_library, monkeypatch, flow
):
    from app.config import settings
    from app.database import TableHistorySports

    automatic, _, workflows, _, session, folder = workflow_library
    if flow == "upgrade":
        monkeypatch.setattr(settings.general, "upgrade_subs", True)
        monkeypatch.setattr(settings.general, "days_to_upgrade_subs", 30)
        automatic.search_event(61, 1)
        session.execute(
            sa.update(TableHistorySports).values(
                score=1, provider="older-provider", subs_id="older-release"
            )
        )
        job_id = workflows.upgrade_sports_subtitles(arr_instance_id=1)
    else:
        job_id = workflows.automatic_search_sports(61, 1)
    history_before = [
        row.to_dict()
        for row in session.execute(sa.select(TableHistorySports)).scalars()
    ]
    files_before = {path: path.read_bytes() for path in folder.glob("*/*.en.srt")}
    queue = workflows.jobs_queue
    job = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(job)
    original = workflows.search_event
    reached = []

    def cancel_before_search(*args, **kwargs):
        reached.append(True)
        job.cancelled = True
        return original(*args, **kwargs)

    def provider(*args, **kwargs):
        pytest.fail("Cancelled batch started provider work")

    monkeypatch.setattr(workflows, "search_event", cancel_before_search)
    monkeypatch.setattr(automatic, "_provider_result", provider)
    assert not queue._run_job(job)
    assert reached == [True] and job.cancelled and job.status == "completed"
    assert job.job_returned_value is None
    assert workflows.sports_job_status(session, job_id, 1)["result"] is None
    assert {
        path: path.read_bytes() for path in folder.glob("*/*.en.srt")
    } == files_before
    assert [
        row.to_dict()
        for row in session.execute(sa.select(TableHistorySports)).scalars()
    ] == history_before


def test_job_projection_retains_safe_publication_state(workflow_library, monkeypatch):
    _, _, workflows, _, session, _ = workflow_library
    publication = {
        "published": True,
        "status": "published_with_warnings",
        "processing": "completed",
        "artifact": "completed",
        "history": "committed",
        "index": "failed",
        "refresh_attempts": 2,
        "refresh_queued": False,
        "failed_phase": "index",
        "cancelled": False,
        "message": "Subtitle published; index refresh failed.",
    }
    monkeypatch.setattr(
        workflows.jobs_queue,
        "list_jobs_from_queue",
        lambda **kwargs: [
            {
                "module": "sportarr.workflows",
                "func": "automatic_search_sports",
                "kwargs": {"arr_instance_id": 1},
                "status": "completed",
                "progress_message": publication["message"],
                "job_returned_value": {
                    "data": [
                        {
                            "status": "published_with_warnings",
                            "downloads": 1,
                            "cancelled": False,
                            "publication": publication | {"private": "must be dropped"},
                        }
                    ]
                },
            }
        ],
    )
    state = workflows.sports_job_status(session, 1, 1)
    assert state["result"]["data"][0]["publication"] == publication
    assert state["result"]["data"][0]["cancelled"] is False


@pytest.mark.parametrize("phase", ["processing", "index"])
@pytest.mark.parametrize("flow", ["automatic", "upgrade", "replacement"])
def test_real_job_signal_cancellation_keeps_committed_publication(
    workflow_library, monkeypatch, phase, flow
):
    from subtitles import processing
    from subtitles.indexer import sports as indexer
    from app.database import TableHistorySports

    automatic, _, workflows, _, session, folder = workflow_library
    history_before = 0
    if flow != "automatic":
        from app.config import settings

        monkeypatch.setattr(settings.general, "upgrade_subs", True)
        monkeypatch.setattr(settings.general, "days_to_upgrade_subs", 30)
        automatic.search_event(61, 1)
        row = session.execute(sa.select(TableHistorySports)).scalar_one()
        session.execute(
            sa.update(TableHistorySports)
            .where(TableHistorySports.id == row.id)
            .values(score=1, provider="older-provider", subs_id="older-release")
        )
        history_before = 1 if flow == "upgrade" else 2
        job_id = (
            workflows.upgrade_sports_subtitles(arr_instance_id=1)
            if flow == "upgrade"
            else workflows.blacklist_sports_subtitle(row.id, 1)
        )
    else:
        job_id = workflows.automatic_search_sports(61, 1)
    queue = workflows.jobs_queue
    job = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(job)
    module = processing if phase == "processing" else indexer
    name = "process_subtitle" if phase == "processing" else "store_subtitles_sports"
    original = getattr(module, name)

    def cancel_after_publication(*args, **kwargs):
        if (
            flow == "replacement"
            and phase == "index"
            and not (folder / "1/event.en.srt").exists()
        ):
            return original(*args, **kwargs)
        assert (folder / "1/event.en.srt").read_bytes()
        job.cancelled = True
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, cancel_after_publication)
    assert queue._run_job(job)
    assert job.cancelled and job.status == "completed"
    assert "published" in job.progress_message.lower()
    result = workflows.sports_job_status(session, job_id, 1)["result"]
    outcome = result["replacement"] if flow == "replacement" else result["data"][0]
    assert outcome["downloads"] == 1 and outcome["cancelled"] is True
    assert outcome["publication"]["published"] is True
    assert outcome["publication"]["index"] == "cancelled"
    assert len(
        session.execute(sa.select(TableHistorySports)).all()
    ) == history_before + (phase == "index")
    assert (folder / "1/event.en.srt").read_bytes()

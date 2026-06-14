# coding=utf-8
"""API service layer for arr_instances CRUD (#156).

These exercise the request-handling logic (status codes, response shaping,
api-key safety) without the heavy Flask/flask_restx import chain: the logic
lives in arr_instances.service and returns (body, status_code) tuples. The
thin Flask resources in bazarr/api/system/arr_instances.py just parse the
request, call these, and commit.

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 6).
"""


def test_create_returns_201_and_never_echoes_api_key(schema_session):
    from arr_instances import service

    body, status = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "super-secret"})

    assert status == 201
    assert "api_key" not in body
    assert "super-secret" not in str(body)
    assert body["api_key_set"] is True
    assert body["kind"] == "sonarr"
    assert body["is_default"] is True


def test_create_invalid_kind_returns_400(schema_session):
    from arr_instances import service

    body, status = service.create_instance(
        schema_session, {"kind": "plex", "name": "Nope", "api_key": "k"})
    assert status == 400
    assert body["error"] == "invalid"


def test_list_returns_safe_dicts(schema_session):
    from arr_instances import service

    service.create_instance(schema_session, {"kind": "sonarr", "name": "Main", "api_key": "k"})
    service.create_instance(schema_session, {"kind": "radarr", "name": "Films", "api_key": "k"})

    body, status = service.list_instances(schema_session)
    assert status == 200
    assert {i["name"] for i in body} == {"Main", "Films"}
    assert all("api_key" not in i for i in body)


def test_get_missing_returns_404(schema_session):
    from arr_instances import service

    body, status = service.get_instance(schema_session, 999)
    assert status == 404


def test_update_preserves_key_and_returns_200(schema_session):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "orig"})
    body, status = service.update_instance(
        schema_session, created["id"], {"name": "Renamed"})

    assert status == 200
    assert body["name"] == "Renamed"
    # key preserved through an update that omitted it
    from arr_instances.repository import ArrInstanceRepository
    assert ArrInstanceRepository(schema_session).get_decrypted_api_key(created["id"]) == "orig"


def test_update_missing_returns_404(schema_session):
    from arr_instances import service

    body, status = service.update_instance(schema_session, 999, {"name": "X"})
    assert status == 404


def test_delete_returns_204(schema_session):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "k"})
    body, status = service.delete_instance(schema_session, created["id"])
    assert status == 204


def test_delete_with_owned_rows_returns_409(schema_session):
    from sqlalchemy import insert

    from app.database import TableShows
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "k"})
    schema_session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path="/tv/show", title="Show", arr_instance_id=created["id"]))

    body, status = service.delete_instance(schema_session, created["id"])
    assert status == 409
    assert body["error"] == "conflict"


# ------------------------------------------------------ validation + conflicts

def test_create_rejects_invalid_port(schema_session):
    from arr_instances import service

    for bad in (0, 70000, -1):
        body, status = service.create_instance(
            schema_session, {"kind": "sonarr", "name": "X", "port": bad})
        assert status == 400, bad
        assert body["error"] == "invalid"


def test_create_rejects_nonpositive_http_timeout(schema_session):
    from arr_instances import service

    body, status = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "X", "http_timeout": 0})
    assert status == 400
    assert body["error"] == "invalid"


def test_update_rejects_invalid_port(schema_session):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "X", "api_key": "k"})
    body, status = service.update_instance(
        schema_session, created["id"], {"port": 0})
    assert status == 400


def test_test_connection_rejects_invalid_port():
    from arr_instances import service

    body, status = service.test_connection({"kind": "sonarr", "port": 0})
    assert status == 400
    assert body["ok"] is False


def test_create_maps_integrity_error_to_409(schema_session, monkeypatch):
    # A unique-constraint violation (realistically a concurrent create racing
    # the check-then-insert) must surface as 409, not an unhandled 500.
    from sqlalchemy.exc import IntegrityError

    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    def boom(*args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(ArrInstanceRepository, "create", boom)
    body, status = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Dup", "api_key": "k"})

    assert status == 409
    assert body["error"] == "conflict"


# ---------------------------------------------------------------- F2 scheduler
# The lone enabled instance must sync via its arr_instances row (the per-instance
# job), NOT the legacy scalar job that reads the now-removed Host form's
# settings.sonarr.*/settings.radarr.* (#156). The Connections UI writes host +
# key only to the arr_instances table, so the scalar config diverges after the
# backfill and the scalar job would sync stale config.

import types as _types  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


class _RecordingRepo:
    """Stand-in for ArrInstanceRepository that returns canned enabled instances
    per kind, so the scheduler task methods register jobs off them."""

    def __init__(self, by_kind):
        self._by_kind = by_kind

    def __call__(self, _database):  # used as the patched class
        return self

    def list(self, kind, enabled_only=False):
        return list(self._by_kind.get(kind, []))


def _make_scheduler_with_recorder(monkeypatch, instances_by_kind,
                                   use_sonarr=True, use_radarr=True):
    """Build a Scheduler whose aps_scheduler.add_job is a recording mock and
    whose ArrInstanceRepository returns the given enabled instances. Returns
    (scheduler, add_job_mock)."""
    from app import scheduler as sched_module

    # A cheap fake BackgroundScheduler so constructing Scheduler() neither spins a
    # real thread nor runs real jobs; add_job records every registration.
    fake_aps = MagicMock()
    fake_aps.get_jobs.return_value = []
    monkeypatch.setattr(sched_module, "BackgroundScheduler", lambda *a, **k: fake_aps)
    monkeypatch.setattr(sched_module, "ArrInstanceRepository",
                        _RecordingRepo(instances_by_kind))
    monkeypatch.setattr(sched_module.settings.general, "use_sonarr", use_sonarr)
    monkeypatch.setattr(sched_module.settings.general, "use_radarr", use_radarr)

    sched = sched_module.Scheduler()
    fake_aps.add_job.reset_mock()
    return sched, fake_aps


def _sync_jobs(add_job_mock, prefix):
    """All add_job calls whose id starts with prefix (e.g. 'update_series')."""
    out = []
    for call in add_job_mock.add_job.call_args_list:
        job_id = call.kwargs.get("id")
        if job_id and job_id.startswith(prefix):
            out.append(call)
    return out


def test_f2_single_sonarr_instance_uses_per_instance_job(monkeypatch):
    from sonarr.sync.series import update_series, update_series_for_instance

    inst = _types.SimpleNamespace(id=7, name="Sonarr")
    sched, aps = _make_scheduler_with_recorder(
        monkeypatch, {"sonarr": [inst], "radarr": []}, use_radarr=False)

    sched._Scheduler__sonarr_update_task()

    jobs = _sync_jobs(aps, "update_series")
    assert len(jobs) == 1
    call = jobs[0]
    # The lone instance must register the PER-INSTANCE job bound to its row,
    # never the legacy scalar 'update_series' job.
    assert call.kwargs["id"] == "update_series_7"
    assert call.args[0] is update_series_for_instance
    assert call.args[0] is not update_series
    assert call.kwargs["kwargs"]["arr_instance_id"] == 7


def test_f2_single_radarr_instance_uses_per_instance_job(monkeypatch):
    from radarr.sync.movies import update_movies, update_movies_for_instance

    inst = _types.SimpleNamespace(id=3, name="Radarr")
    sched, aps = _make_scheduler_with_recorder(
        monkeypatch, {"sonarr": [], "radarr": [inst]}, use_sonarr=False)

    sched._Scheduler__radarr_update_task()

    jobs = _sync_jobs(aps, "update_movies")
    assert len(jobs) == 1
    call = jobs[0]
    assert call.kwargs["id"] == "update_movies_3"
    assert call.args[0] is update_movies_for_instance
    assert call.args[0] is not update_movies
    assert call.kwargs["kwargs"]["arr_instance_id"] == 3


def test_f2_multiple_sonarr_instances_register_one_job_each(monkeypatch):
    insts = [_types.SimpleNamespace(id=7, name="Sonarr"),
             _types.SimpleNamespace(id=8, name="4k Sonarr")]
    sched, aps = _make_scheduler_with_recorder(
        monkeypatch, {"sonarr": insts, "radarr": []}, use_radarr=False)

    sched._Scheduler__sonarr_update_task()

    ids = {c.kwargs["id"] for c in _sync_jobs(aps, "update_series")}
    assert ids == {"update_series_7", "update_series_8"}


def test_f2_zero_sonarr_instances_registers_no_sync_job(monkeypatch):
    sched, aps = _make_scheduler_with_recorder(
        monkeypatch, {"sonarr": [], "radarr": []}, use_radarr=False)

    sched._Scheduler__sonarr_update_task()

    # No enabled instance of the kind -> no sync job at all (no scalar fallback).
    assert _sync_jobs(aps, "update_series") == []


def test_f2_zero_radarr_instances_registers_no_sync_job(monkeypatch):
    sched, aps = _make_scheduler_with_recorder(
        monkeypatch, {"sonarr": [], "radarr": []}, use_sonarr=False)

    sched._Scheduler__radarr_update_task()

    assert _sync_jobs(aps, "update_movies") == []


# ------------------------------------------------------- F5 CRUD runtime refresh
# Instance CRUD must rebuild scheduler sync jobs and re-fan-out the affected
# kind's SignalR feed - otherwise scheduled sync and live fan-out keep using the
# OLD instance set until a restart or an unrelated settings save (#156).
# service.refresh_runtime() is the single helper the Flask create/update/delete
# handlers call AFTER commit; it lazy-imports scheduler + signalr so it stays
# importable in this in-process test batch.


def _patch_runtime(monkeypatch):
    """Replace the scheduler + signalr functions service.refresh_runtime reaches
    with recorders. Returns a namespace of the recorders."""
    from app import scheduler as sched_module
    from app import signalr_client as sc

    rec = _types.SimpleNamespace(
        update_tasks=MagicMock(),
        remove_job=MagicMock(),
        restart_sonarr=MagicMock(),
        restart_radarr=MagicMock(),
        event_stream=MagicMock(),
    )
    monkeypatch.setattr(sched_module.scheduler, "update_configurable_tasks", rec.update_tasks)
    monkeypatch.setattr(sched_module.scheduler.aps_scheduler, "remove_job", rec.remove_job)
    monkeypatch.setattr(sc, "restart_sonarr_signalr", rec.restart_sonarr)
    monkeypatch.setattr(sc, "restart_radarr_signalr", rec.restart_radarr)
    from arr_instances import service as svc
    monkeypatch.setattr(svc, "event_stream", rec.event_stream)
    return rec


def test_f5_refresh_rebuilds_jobs_and_restarts_affected_kind(monkeypatch):
    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    service.refresh_runtime("sonarr", instance_id=5)

    rec.update_tasks.assert_called_once()
    rec.restart_sonarr.assert_called_once()
    # Kind-scoped: a Sonarr change must NOT bounce the Radarr feed.
    rec.restart_radarr.assert_not_called()
    rec.event_stream.assert_called_once_with(type="task")


def test_f5_refresh_radarr_does_not_bounce_sonarr(monkeypatch):
    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    service.refresh_runtime("radarr", instance_id=2)

    rec.restart_radarr.assert_called_once()
    rec.restart_sonarr.assert_not_called()


def test_f5_create_triggers_refresh(schema_session, monkeypatch):
    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    body, status = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "k"})
    assert status == 201
    # Mirror the Flask handler: refresh after the commit.
    service.refresh_runtime(body["kind"], instance_id=body["id"])

    rec.update_tasks.assert_called_once()
    rec.restart_sonarr.assert_called_once()
    rec.restart_radarr.assert_not_called()


def test_f5_update_triggers_refresh(schema_session, monkeypatch):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "radarr", "name": "Films", "api_key": "k"})
    rec = _patch_runtime(monkeypatch)
    body, status = service.update_instance(
        schema_session, created["id"], {"name": "Renamed"})
    assert status == 200
    service.refresh_runtime(body["kind"], instance_id=body["id"])

    rec.update_tasks.assert_called_once()
    rec.restart_radarr.assert_called_once()
    rec.restart_sonarr.assert_not_called()


def test_f5_delete_removes_orphan_job_and_refreshes(monkeypatch):
    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    # removed=True -> the orphaned per-instance sync job must be explicitly
    # removed (update_configurable_tasks only replaces/adds, never removes).
    service.refresh_runtime("sonarr", instance_id=9, removed=True)

    rec.remove_job.assert_called_once_with("update_series_9")
    rec.update_tasks.assert_called_once()
    rec.restart_sonarr.assert_called_once()
    rec.restart_radarr.assert_not_called()


def test_f5_delete_radarr_removes_movie_job(monkeypatch):
    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    service.refresh_runtime("radarr", instance_id=4, removed=True)

    rec.remove_job.assert_called_once_with("update_movies_4")
    rec.restart_radarr.assert_called_once()


def test_f5_remove_orphan_job_missing_is_ignored(monkeypatch):
    from apscheduler.jobstores.base import JobLookupError

    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    rec.remove_job.side_effect = JobLookupError("update_series_9")
    # Removing a non-existent job must not raise.
    service.refresh_runtime("sonarr", instance_id=9, removed=True)
    rec.update_tasks.assert_called_once()


def test_f5_signalr_failure_does_not_propagate(monkeypatch):
    from arr_instances import service

    rec = _patch_runtime(monkeypatch)
    rec.restart_sonarr.side_effect = ConnectionError("arr down")
    # A transient arr/SignalR failure must not bubble up - the row is already
    # committed, so the API must still return success.
    service.refresh_runtime("sonarr", instance_id=5)
    # The local rebuild + event still happen despite the signalr failure.
    rec.update_tasks.assert_called_once()

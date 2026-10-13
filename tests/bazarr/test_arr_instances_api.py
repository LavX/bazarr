# coding=utf-8
"""API service layer for arr_instances CRUD (#156).

These exercise the request-handling logic (status codes, response shaping,
api-key safety) without the heavy Flask/flask_restx import chain: the logic
lives in arr_instances.service and returns (body, status_code) tuples. The
thin Flask resources in bazarr/api/system/arr_instances.py just parse the
request, call these, and commit.
"""
import pytest

from test_media_server_http import http_fixture as http_fixture

pytestmark = pytest.mark.usefixtures('scheduler_runtime')


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
    # The refusal names what the instance still holds and that deleting it
    # together with that library is possible, so the dialog can offer it.
    assert body["message"] == "cannot delete an instance that still owns rows"
    assert body["can_remove_library"] is True
    assert body["library"] == {"series": 1, "episodes": 0, "movies": 0, "history": 0,
                               "blacklist": 0, "root_folders": 0}
    assert body["last_of_kind"] is True


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


# ---------------------------------- scalar config mirroring (#276 onboarding)

def test_create_default_mirrors_connection_into_scalar_config(schema_session, monkeypatch):
    """A default instance created via the API (onboarding wizard / Connections
    page) must mirror its host/port/key into the scalar settings.<kind>.* config.

    Without this the single-instance compat paths (health check, get_<kind>_info
    version probe, scheduler/SignalR fall-back) keep targeting the default
    127.0.0.1:8989 because the wizard never populated the scalar config, spamming
    connection-refused errors even though the per-instance sync works (#276).
    """
    from app import config as app_config
    from arr_instances import service

    monkeypatch.setattr(app_config, "write_config", lambda: None)
    settings = app_config.settings
    original = (settings.sonarr.ip, settings.sonarr.port, settings.sonarr.apikey)
    try:
        created, status = service.create_instance(
            schema_session,
            {"kind": "sonarr", "name": "Main", "ip": "10.9.8.7", "port": 9999,
             "api_key": "real-key", "is_default": True},
        )
        assert status == 201

        # The mirror is a post-commit side effect the resource boundary triggers
        # via refresh_runtime; exercise the kind-scoped helper directly.
        service.mirror_scalar_config_from_default(schema_session, "sonarr")

        assert settings.sonarr.ip == "10.9.8.7"
        assert int(settings.sonarr.port) == 9999
        assert settings.sonarr.apikey == "real-key"
    finally:
        settings.sonarr.ip, settings.sonarr.port, settings.sonarr.apikey = original


def test_mirror_scalar_config_noop_without_default(schema_session, monkeypatch):
    """No default instance -> the scalar config is left untouched (no crash)."""
    from app import config as app_config
    from arr_instances import service

    wrote = []
    monkeypatch.setattr(app_config, "write_config", lambda: wrote.append(True))
    settings = app_config.settings
    original_ip = settings.radarr.ip
    try:
        service.mirror_scalar_config_from_default(schema_session, "radarr")
        assert settings.radarr.ip == original_ip
        assert wrote == []  # nothing to mirror, no write
    finally:
        settings.radarr.ip = original_ip


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


def _mirror_default_with_timeout(schema_session, kind, timeout):
    from arr_instances import service

    _, status = service.create_instance(
        schema_session,
        {"kind": kind, "name": "Main", "api_key": "k", "http_timeout": timeout,
         "is_default": True},
    )
    assert status == 201
    service.mirror_scalar_config_from_default(schema_session, kind)


@pytest.fixture
def scalar_snapshot():
    """Put back every scalar field the mirror writes."""
    from app.config import settings

    fields = ("ip", "port", "base_url", "ssl", "verify_ssl", "http_timeout", "apikey")
    saved = {kind: {f: getattr(settings, kind)[f] for f in fields} for kind in ("sonarr", "radarr")}
    trusted_proxy = settings.general.trusted_proxy
    yield
    for kind, values in saved.items():
        for field, value in values.items():
            setattr(getattr(settings, kind), field, value)
    settings.general.trusted_proxy = trusted_proxy


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_default_instance_timeout_of_30_does_not_block_settings_save(
        schema_session, monkeypatch, scalar_snapshot, kind):
    """The instance API accepts any timeout from 1 s. The mirror copies it into
    the scalar config, whose validator used to allow only 60, 120, 180, 240,
    300 or 600, so every save through /api/system/settings then failed with 406.
    """
    import sys
    from types import SimpleNamespace

    from app import config as app_config

    monkeypatch.setattr(app_config, "write_config", lambda **_kwargs: True)
    monkeypatch.setattr(app_config, "validate_log_regex", lambda: None)

    _mirror_default_with_timeout(schema_session, kind, 30)
    assert getattr(app_config.settings, kind).http_timeout == 30

    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: None),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    # An unrelated setting, as any save from the Settings pages would carry.
    app_config.save_settings([("settings-general-trusted_proxy", ["10.0.0.5"])])

    assert app_config.settings.general.trusted_proxy == "10.0.0.5"
    assert getattr(app_config.settings, kind).http_timeout == 30


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_default_instance_timeout_of_30_survives_restart(
        schema_session, monkeypatch, scalar_snapshot, kind):
    """Startup validates the config before the mirror runs. The old rule reset
    30 to 60 and the mirror then wrote 30 straight back, so a restart never
    cleared the lockout. Now the value passes and the mirror has nothing to do.
    """
    from app import config as app_config
    from arr_instances import service

    writes = []
    monkeypatch.setattr(app_config, "write_config", lambda **_kwargs: writes.append(True) or True)

    _mirror_default_with_timeout(schema_session, kind, 30)
    writes.clear()

    # The startup loop resets any value that fails here back to its default.
    app_config.settings.validators.validate_all(only=f"{kind}.http_timeout")
    assert getattr(app_config.settings, kind).http_timeout == 30

    service.mirror_scalar_config_from_default(schema_session, kind)
    assert writes == []


def test_test_connection_rejects_invalid_port():
    from arr_instances import service

    body, status = service.test_connection({"kind": "sonarr", "port": 0})
    assert status == 400
    assert body["ok"] is False


# ----------------------------------------------- booleans are not whole numbers
# Python counts True as the integer 1, so every check above let it through: as a
# one second timeout, or as port 1.

@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
@pytest.mark.parametrize("value", [True, False])
def test_the_scalar_timeout_refuses_a_boolean(kind, value):
    from dynaconf import Dynaconf
    from dynaconf.validator import ValidationError

    from app import config as app_config

    validator = next(v for v in app_config.validators if v.names == (f"{kind}.http_timeout",))

    with pytest.raises(ValidationError):
        validator.validate(Dynaconf(**{kind.upper(): {"http_timeout": value}}))
    validator.validate(Dynaconf(**{kind.upper(): {"http_timeout": 30}}))


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_settings_save_of_a_true_timeout_is_refused(monkeypatch, scalar_snapshot, kind):
    """The settings form sends 'true' as text, and a save turns that into True."""
    import sys
    from types import SimpleNamespace

    from dynaconf.validator import ValidationError

    from app import config as app_config

    writes = []
    monkeypatch.setattr(app_config, "write_config", lambda **_kwargs: writes.append(True) or True)
    monkeypatch.setattr(app_config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(app_config, "restore_persisted_settings", lambda: None)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(
        database=SimpleNamespace(execute=lambda statement: None),
        update=lambda _model: _FakeUpdate(), System=object))
    setattr(getattr(app_config.settings, kind), "http_timeout", 60)

    with pytest.raises(ValidationError):
        app_config.save_settings([(f"settings-{kind}-http_timeout", ["true"])])

    assert writes == []


@pytest.mark.parametrize("field", ["http_timeout", "port"])
def test_the_instance_service_refuses_a_boolean(schema_session, field):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "k"})

    for body, status in (
            service.create_instance(schema_session, {"kind": "sonarr", "name": "X", field: True}),
            service.update_instance(schema_session, created["id"], {field: True}),
            service.test_connection({"kind": "sonarr", field: True}),
            service.test_connection_for_instance(schema_session, created["id"], {field: True})):
        assert status == 400
        assert body["error"] == "invalid"
    assert service.get_instance(schema_session, created["id"])[0][field] != 1


_INSTANCE_PARSERS = ("_create_parser", "_update_parser", "_test_parser", "_test_by_id_parser")


@pytest.mark.parametrize("parser", _INSTANCE_PARSERS)
@pytest.mark.parametrize("field", ["http_timeout", "port"])
@pytest.mark.parametrize("value", [True, False, 1.9])
def test_the_instance_api_refuses_a_boolean_or_a_fraction(parser, field, value):
    """The request parsers used int(), which read JSON true as 1 and 1.9 as 1."""
    from flask import Flask
    from werkzeug.exceptions import BadRequest

    import api.system.arr_instances as endpoint_module

    body = {"kind": "sonarr", "name": "X", field: value}
    with Flask(__name__).test_request_context(json=body):
        with pytest.raises(BadRequest):
            getattr(endpoint_module, parser).parse_args()


@pytest.mark.parametrize("parser", _INSTANCE_PARSERS)
@pytest.mark.parametrize("value, expected", [(30, 30), ("30", 30), (30.0, 30), (None, None)])
def test_the_instance_api_still_reads_a_whole_number(parser, value, expected):
    from flask import Flask

    import api.system.arr_instances as endpoint_module

    body = {"kind": "sonarr", "name": "X", "http_timeout": value, "port": value}
    with Flask(__name__).test_request_context(json=body):
        args = getattr(endpoint_module, parser).parse_args()

    assert args["http_timeout"] == expected
    assert args["port"] == expected


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


# ------------------------------------------------- deleting with the synced library

def _owned_show(session, owner):
    from sqlalchemy import insert

    from app.database import TableShows

    session.execute(insert(TableShows).values(
        sonarrSeriesId=1, path=f"/tv/show-{owner}", title="Show", arr_instance_id=owner))


def test_delete_with_remove_library_returns_204_and_removes_the_rows(schema_session):
    from sqlalchemy import func, select

    from app.database import TableShows
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    kept, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Kept", "api_key": "k", "port": 1})
    gone, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Gone", "api_key": "k", "port": 2})
    _owned_show(schema_session, kept["id"])
    _owned_show(schema_session, gone["id"])

    body, status = service.delete_instance(schema_session, gone["id"], remove_library=True)

    assert (body, status) == ("", 204)
    assert ArrInstanceRepository(schema_session).get(gone["id"]) is None
    owners = schema_session.execute(select(TableShows.arr_instance_id, func.count())
                                    .group_by(TableShows.arr_instance_id)).all()
    assert [tuple(row) for row in owners] == [(kept["id"], 1)]


def test_a_sportarr_delete_never_waits_on_the_job_queue(schema_session, monkeypatch):
    """The flag means nothing to Sportarr, so its delete does not hold up
    starting and finishing jobs while its ownership triggers are rebuilt."""
    from app.jobs_queue import jobs_queue
    from arr_instances import service

    class Untouchable:
        def __enter__(self):
            pytest.fail("the job queue lock was taken")

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(jobs_queue, "_queue_lock", Untouchable())
    created, _ = service.create_instance(
        schema_session, {"kind": "sportarr", "name": "Sports", "api_key": "k"})
    assert service.delete_instance(schema_session, created["id"], remove_library=True) == ("", 204)
    assert service.delete_instance(schema_session, 999, remove_library=True)[1] == 404


@pytest.mark.parametrize("kind,module,func", [
    ("sonarr", "sonarr.sync.series", "update_series"),
    ("radarr", "radarr.sync.movies", "update_movies"),
])
@pytest.mark.parametrize("is_default", [True, False])
def test_a_whole_kind_sync_holds_up_every_instance_of_its_kind(
        schema_session, monkeypatch, kind, module, func, is_default):
    """The legacy whole-kind sync carries no instance id. It writes to the
    default it resolved when it started, which need not be the default now,
    so it holds up every instance of its kind, and none of the other kind."""
    from collections import deque

    from sqlalchemy import insert

    from app.database import TableMovies
    from app.jobs_queue import Job, jobs_queue
    from arr_instances import service

    first, _ = service.create_instance(
        schema_session, {"kind": kind, "name": "First", "api_key": "k", "port": 1})
    second, _ = service.create_instance(
        schema_session, {"kind": kind, "name": "Second", "api_key": "k", "port": 2})
    target = first if is_default else second
    assert target["is_default"] is is_default
    if kind == "sonarr":
        _owned_show(schema_session, target["id"])
    else:
        schema_session.execute(insert(TableMovies).values(
            radarrId=1, tmdbId="1", path="/m.mkv", title="M", arr_instance_id=target["id"]))
    other_module, other_func = (("radarr.sync.movies", "update_movies") if kind == "sonarr"
                                else ("sonarr.sync.series", "update_series"))
    other_kind_sync = Job(job_id=900003, job_name="Syncing", module=other_module,
                          func=other_func, kwargs={"job_id": None, "arr_instance_id": None})
    job = Job(job_id=900002, job_name="Syncing", module=module, func=func,
              kwargs={"job_id": None, "wait_for_completion": False, "arr_instance_id": None,
                      "arr_client": None})
    job.status = "running"
    monkeypatch.setattr(jobs_queue, "jobs_pending_queue", deque([other_kind_sync]))
    monkeypatch.setattr(jobs_queue, "jobs_running_queue", deque([job]))

    body, status = service.delete_instance(schema_session, target["id"], remove_library=True)

    assert status == 409
    assert body["error"] == "sync_in_progress"

    # Once it is done, only the other kind's sync is left, and that one does
    # not hold this instance up.
    monkeypatch.setattr(jobs_queue, "jobs_running_queue", deque())
    assert service.delete_instance(
        schema_session, target["id"], remove_library=True) == ("", 204)


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_the_last_instance_switch_off_goes_through_the_settings_save(
        schema_session, monkeypatch, scalar_snapshot, kind):
    from types import SimpleNamespace

    from app import config as app_config
    from app import database as app_database
    from app import event_handler
    from arr_instances import service

    writes, app_events = [], []
    monkeypatch.setattr(app_config, "write_config", lambda **_kwargs: writes.append(True) or True)
    monkeypatch.setattr(app_config, "validate_log_regex", lambda: None)
    monkeypatch.setattr(event_handler, "event_stream", lambda **event: app_events.append(event))
    rec = _patch_runtime(monkeypatch)
    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)

    created, _ = service.create_instance(schema_session, {"kind": kind, "name": "Only", "api_key": "k"})
    # Still has an instance: nothing to switch off.
    assert service.switch_off_kind_without_instances(schema_session, kind) is False
    assert writes == [] and getattr(app_config.settings.general, f"use_{kind}") is True

    assert service.delete_instance(schema_session, created["id"]) == ("", 204)
    # The save marks the install configured through the application database.
    statements = []
    monkeypatch.setattr(app_database, "database", SimpleNamespace(execute=statements.append))
    assert service.switch_off_kind_without_instances(schema_session, kind) is True
    assert len(statements) == 1

    assert getattr(app_config.settings.general, f"use_{kind}") is False
    assert writes == [True]
    # The same refresh a save from the Settings page gets.
    rec.update_tasks.assert_called_once()
    assert app_events == [{"type": "task"}]
    getattr(rec, f"restart_{kind}").assert_called_once()
    rec.event_stream.assert_called_with(type="settings")

    # Already off: nothing is written again.
    assert service.switch_off_kind_without_instances(schema_session, kind) is False
    assert writes == [True]


def test_sportarr_is_never_switched_off_by_an_instance_delete(schema_session, monkeypatch):
    from app import config as app_config
    from arr_instances import service

    monkeypatch.setattr(app_config, "save_settings", lambda items: pytest.fail("saved"))
    monkeypatch.setattr(app_config.settings.general, "use_sportarr", True)
    assert service.switch_off_kind_without_instances(schema_session, "sportarr") is False
    assert app_config.settings.general.use_sportarr is True


# --------------------------------------- the stored key never follows a new address

REENTER_KEY = "Re-enter the key to use a different address"


class _Probe:
    """An injectable HTTP getter that records where each request went."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, (kwargs.get("headers") or {}).get("X-Api-Key")))
        from types import SimpleNamespace
        return SimpleNamespace(status_code=200,
                               json=lambda: {"version": "4.0.0.0", "appName": "Sonarr"})


def _instance_with_key(session, **connection):
    from arr_instances import service

    created, _ = service.create_instance(
        session, dict({"kind": "sonarr", "name": "Main", "api_key": "stored",
                       "ip": "10.0.0.2", "port": 8989}, **connection))
    return created


def test_a_by_id_test_refuses_a_moved_address_without_the_key(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session)
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"], {"ip": "10.0.0.9"}, http_get=probe)

    assert status == 200
    assert body["ok"] is False
    assert body["error"] == "reenter_key"
    assert body["message"] == REENTER_KEY
    assert probe.calls == [], "the stored key never reached the new address"


def test_a_by_id_test_refuses_turning_verification_off_without_the_key(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session, verify_ssl=True)
    probe = _Probe()
    for args in ({"verify_ssl": False}, {"ssl": True}, {"port": 9090},
                 {"base_url": "/sonarr"}):
        probe.calls.clear()
        body, status = service.test_connection_for_instance(
            schema_session, created["id"], args, http_get=probe)
        assert status == 200, args
        assert body["error"] == "reenter_key", args
        assert probe.calls == [], args


def test_a_by_id_test_allows_turning_verification_back_on(schema_session):
    """Turning verification back on only hardens the connection, so the stored
    key may follow it without being typed again."""
    from arr_instances import service

    created = _instance_with_key(schema_session, verify_ssl=False)
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"], {"verify_ssl": True}, http_get=probe)

    assert status == 200
    assert body["ok"] is True
    assert probe.calls == [("http://10.0.0.2:8989/api/v3/system/status", "stored")]


def test_a_by_id_test_compares_the_values_the_writer_would_use(schema_session):
    """A port arriving as a string of the stored number is not a move."""
    from arr_instances import service

    created = _instance_with_key(schema_session)
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"], {"port": "8989"}, http_get=probe)

    assert status == 200
    assert body["ok"] is True
    assert probe.calls == [("http://10.0.0.2:8989/api/v3/system/status", "stored")]


@pytest.mark.parametrize("stored,submitted", [("/sonarr/", "/sonarr"),
                                              ("/sonarr", "/sonarr/")])
def test_equivalent_base_url_spellings_are_not_a_destination_change(schema_session,
                                                                    stored, submitted):
    """The client builds the request URL with the leading slash added and the
    trailing one dropped, and the edit form normalizes the same way, so a
    stored '/sonarr/' and a submitted '/sonarr' reach the same endpoint: the
    comparison reads the spellings the client would send, and neither a
    by-id test nor a save asks for the key again over them. The unfixed
    comparison read the raw strings, so the stored key was asked for again
    for a move the connection never made, and the edit form stayed in its
    keep-current-key mode with no explanation."""
    from arr_instances import service

    created = _instance_with_key(schema_session, base_url=stored)
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"], {"base_url": submitted}, http_get=probe)

    assert status == 200
    assert body["ok"] is True, body
    assert probe.calls == [("http://10.0.0.2:8989/sonarr/api/v3/system/status", "stored")]

    updated, status = service.update_instance(schema_session, created["id"],
                                              {"base_url": submitted})
    assert status == 200, updated
    assert updated["api_key_set"] is True, "the stored key was kept for the same path"


@pytest.mark.parametrize("stored,submitted", [("/sonarr/", "/sonarr"),
                                              ("/sonarr", "/sonarr/")])
def test_the_edit_routes_save_keeps_the_key_for_equivalent_base_url_spellings(
        arr_instance_api, stored, submitted):
    """The PATCH the edit form sends for a kept key, on the route that
    carries it: a trailing slash the client drops is not a destination move,
    so the save lands and the stored key stays stored."""
    created = arr_instance_api.post(
        ARR_ROOT, json={"kind": "sonarr", "name": "Main", "api_key": "stored",
                        "ip": "10.0.0.2", "port": 8989, "base_url": stored},
        headers=ARR_HEADERS).json

    response = arr_instance_api.patch(f"{ARR_ROOT}/{created['id']}",
                                      json={"base_url": submitted}, headers=ARR_HEADERS)

    assert response.status_code == 200, response.json
    assert response.json["base_url"] == submitted
    assert response.json["api_key_set"] is True, "the stored key was kept"


def test_a_by_id_test_still_probes_the_stored_connection(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session)
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"],
        {"ip": "10.0.0.2", "port": 8989, "ssl": False, "verify_ssl": False,
         "http_timeout": 30},
        http_get=probe)

    assert status == 200
    assert body["ok"] is True and body["version"] == "4.0.0.0"
    assert probe.calls == [("http://10.0.0.2:8989/api/v3/system/status", "stored")]


def test_a_by_id_test_without_a_stored_key_probes_freely(schema_session):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Bare", "ip": "10.0.0.2"})
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"], {"ip": "10.0.0.9"}, http_get=probe)

    assert status == 200
    assert body["ok"] is True
    assert probe.calls == [("http://10.0.0.9:8989/api/v3/system/status", "")]


def test_a_save_refuses_a_moved_address_without_the_key(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session)
    body, status = service.update_instance(
        schema_session, created["id"], {"ip": "10.0.0.9"})

    assert status == 400
    assert body["error"] == "reenter_key"
    assert body["message"] == REENTER_KEY
    saved, _ = service.get_instance(schema_session, created["id"])
    assert saved["ip"] == "10.0.0.2"


def test_a_save_refuses_turning_verification_off_without_the_key(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session, verify_ssl=True)
    body, status = service.update_instance(
        schema_session, created["id"], {"verify_ssl": False})

    assert status == 400
    assert body["error"] == "reenter_key"
    assert body["message"] == REENTER_KEY
    saved, _ = service.get_instance(schema_session, created["id"])
    assert saved["verify_ssl"] is True


def test_a_save_allows_turning_verification_back_on(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session, verify_ssl=False)
    body, status = service.update_instance(
        schema_session, created["id"], {"verify_ssl": True})

    assert status == 200
    assert body["verify_ssl"] is True
    from arr_instances.repository import ArrInstanceRepository
    assert ArrInstanceRepository(schema_session).get_decrypted_api_key(created["id"]) == "stored"


def test_a_save_with_an_empty_typed_key_still_refuses_the_move(schema_session):
    """An empty key does not replace the stored one, so it cannot move it."""
    from arr_instances import service

    created = _instance_with_key(schema_session)
    body, status = service.update_instance(
        schema_session, created["id"], {"ip": "10.0.0.9", "api_key": ""})

    assert status == 400
    assert body["error"] == "reenter_key"
    saved, _ = service.get_instance(schema_session, created["id"])
    assert saved["ip"] == "10.0.0.2"


def test_a_save_with_the_key_typed_again_moves_the_instance(schema_session):
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    created = _instance_with_key(schema_session)
    body, status = service.update_instance(
        schema_session, created["id"], {"ip": "10.0.0.9", "api_key": "again"})

    assert status == 200
    assert body["ip"] == "10.0.0.9"
    assert ArrInstanceRepository(schema_session).get_decrypted_api_key(created["id"]) == "again"


def test_a_save_that_clears_the_key_may_move_the_instance(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session)
    body, status = service.update_instance(
        schema_session, created["id"], {"ip": "10.0.0.9", "clear_api_key": True})

    assert status == 200
    assert body["ip"] == "10.0.0.9"
    assert body["api_key_set"] is False


def test_a_save_without_a_stored_key_moves_freely(schema_session):
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Bare"})
    body, status = service.update_instance(
        schema_session, created["id"], {"ip": "10.0.0.9", "verify_ssl": True})
    assert status == 200
    assert body["ip"] == "10.0.0.9" and body["verify_ssl"] is True


def test_a_save_of_the_stored_address_is_not_a_move(schema_session):
    from arr_instances import service

    created = _instance_with_key(schema_session)
    body, status = service.update_instance(
        schema_session, created["id"],
        {"ip": "10.0.0.2", "port": 8989, "http_timeout": 90})

    assert status == 200
    assert body["http_timeout"] == 90
    assert body["ip"] == "10.0.0.2"


def test_a_by_id_test_with_an_empty_base_url_override_keeps_the_saved_prefix(schema_session):
    """The probe's client keeps the saved prefix when its base_url override is
    empty, and the comparison uses that same fallback: an empty submission is
    the saved connection, not a move to the root, so the stored key probes the
    saved address unchanged. The unfixed comparison read the empty override as
    the root and refused a move the submission never made."""
    from arr_instances import service

    created = _instance_with_key(schema_session, base_url="/sonarr")
    probe = _Probe()
    body, status = service.test_connection_for_instance(
        schema_session, created["id"], {"base_url": ""}, http_get=probe)

    assert status == 200
    assert body["ok"] is True, body
    assert probe.calls == [("http://10.0.0.2:8989/sonarr/api/v3/system/status", "stored")]


# ---------------------- the same rule through the HTTP routes the modal uses

ARR_HEADERS = {"X-API-KEY": "synthetic-bazarr-key"}
ARR_ROOT = "/api/system/arr-instances"


@pytest.fixture
def arr_instance_api(schema_session, monkeypatch):
    from flask import Flask

    from api import api_bp
    from api.system import arr_instances as resource
    from app.config import settings as app_settings
    from arr_instances import service

    monkeypatch.setitem(app_settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(resource, "database", schema_session)
    monkeypatch.setattr(service, "refresh_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "event_stream", lambda **event: None)
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client()


@pytest.fixture
def recorded_clients(monkeypatch):
    """Every client the routes build, so a test sees where a key would go."""
    from types import SimpleNamespace

    from arr_instances import client as client_module

    calls = []

    class _RecordingFactory:
        def __init__(self, repository=None):
            self._repository = repository

        def from_params(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(test_connection=lambda: {"ok": True, "version": "4.0.0.0"})

    monkeypatch.setattr(client_module, "ArrClientFactory", _RecordingFactory)
    return calls


def _saved_with_key(arr_instance_api):
    created = arr_instance_api.post(
        ARR_ROOT, json={"kind": "sonarr", "name": "Main", "api_key": "stored",
                        "ip": "10.0.0.2", "port": 8989}, headers=ARR_HEADERS)
    assert created.status_code == 201, created.json
    return created.json


def test_the_update_route_refuses_a_moved_address_and_keeps_the_row(arr_instance_api):
    created = _saved_with_key(arr_instance_api)
    path = f"{ARR_ROOT}/{created['id']}"

    response = arr_instance_api.patch(path, json={"ip": "10.0.0.9"}, headers=ARR_HEADERS)

    assert response.status_code == 400
    assert response.json["error"] == "reenter_key"
    assert response.json["message"] == REENTER_KEY
    saved = arr_instance_api.get(path, headers=ARR_HEADERS).json
    assert saved["ip"] == "10.0.0.2" and saved["api_key_set"] is True


def test_the_by_id_test_route_refuses_a_moved_address_with_no_probe(arr_instance_api,
                                                                    recorded_clients):
    created = _saved_with_key(arr_instance_api)

    response = arr_instance_api.post(f"{ARR_ROOT}/{created['id']}/test",
                                     json={"ip": "10.0.0.9"}, headers=ARR_HEADERS)

    assert response.status_code == 200
    assert response.json["ok"] is False
    assert response.json["error"] == "reenter_key"
    assert recorded_clients == [], "the stored key never reached the new address"


def test_the_by_id_test_route_still_probes_the_stored_connection(arr_instance_api,
                                                                 recorded_clients):
    created = _saved_with_key(arr_instance_api)

    response = arr_instance_api.post(f"{ARR_ROOT}/{created['id']}/test",
                                     json={"ip": "10.0.0.2", "port": 8989},
                                     headers=ARR_HEADERS)

    assert response.status_code == 200
    assert response.json["ok"] is True
    assert len(recorded_clients) == 1
    assert recorded_clients[0]["ip"] == "10.0.0.2"
    assert recorded_clients[0]["api_key"] == "stored"


def test_a_moved_address_saves_and_tests_with_the_key_typed_again(
        arr_instance_api, recorded_clients):
    """The edit modal's Replace mode: the typed key travels with the typed
    address, through the same routes the modal calls."""
    created = _saved_with_key(arr_instance_api)
    path = f"{ARR_ROOT}/{created['id']}"

    tested = arr_instance_api.post(f"{ARR_ROOT}/test",
                                   json={"kind": "sonarr", "ip": "10.0.0.9", "port": 8989,
                                         "api_key": "typed-again"}, headers=ARR_HEADERS)
    assert tested.status_code == 200 and tested.json["ok"] is True
    assert recorded_clients[-1]["api_key"] == "typed-again"

    saved = arr_instance_api.patch(path, json={"ip": "10.0.0.9", "api_key": "typed-again"},
                                   headers=ARR_HEADERS)
    assert saved.status_code == 200, saved.json
    assert saved.json["ip"] == "10.0.0.9" and saved.json["api_key_set"] is True
    listed = arr_instance_api.get(path, headers=ARR_HEADERS).json
    assert listed["ip"] == "10.0.0.9"


def test_a_save_that_paused_before_its_write_keeps_the_key_a_later_save_stored(
        arr_instance_api, schema_session, monkeypatch):
    """The check and the write share the boundary the settings save commits
    under, and the row is re-read inside it: a second save storing the key
    inside the first save's check-to-write window is seen by the first save's
    re-read, so the move is refused and the later key stays at the address its
    own save typed it for. The unfixed order checks before the write, so the
    move landed with the key the second save stored following it."""
    from arr_instances import repository, service

    created = arr_instance_api.post(
        ARR_ROOT, json={"kind": "sonarr", "name": "Bare", "ip": "10.0.0.2", "port": 8989},
        headers=ARR_HEADERS).json
    assert created["api_key_set"] is False
    path = f"{ARR_ROOT}/{created['id']}"
    landed = []

    def land_the_key():
        # The second save lands inside the window wherever this head leaves
        # one: at the fixed head its entry into the write boundary runs it
        # before the real lock is taken, and at the unfixed head the row's
        # write runs it between the check and the write.
        if landed:
            return
        landed.append(True)
        service.update_instance(schema_session, created["id"], {"api_key": "late-key"})

    from discover import metadata as discover_metadata

    real_lock = discover_metadata.CONFIG_LOCK

    class _PausedLock:
        def __enter__(self):
            land_the_key()
            return real_lock.__enter__()

        def __exit__(self, *_exception):
            return real_lock.__exit__(*_exception)

    monkeypatch.setattr(discover_metadata, "CONFIG_LOCK", _PausedLock())

    real_update = repository.ArrInstanceRepository.update

    def update_with_a_late_key(self, instance_id, **fields):
        land_the_key()
        return real_update(self, instance_id, **fields)

    monkeypatch.setattr(repository.ArrInstanceRepository, "update", update_with_a_late_key)

    response = arr_instance_api.patch(path, json={"ip": "10.0.0.9"}, headers=ARR_HEADERS)

    assert response.status_code == 400, response.json
    assert response.json["error"] == "reenter_key"
    assert response.json["message"] == REENTER_KEY
    saved = arr_instance_api.get(path, headers=ARR_HEADERS).json
    assert saved["ip"] == "10.0.0.2", 'the move the second save\'s key enabled never landed'
    assert saved["api_key_set"] is True, 'the key the second save stored is kept'


def test_a_scalar_settings_save_needs_the_default_instances_key_again(schema_session,
                                                                       monkeypatch):
    """The scalar connection settings mirror the kind's default instance, its
    key included, and the paths that still read them send that mirrored key to
    the address they build. A save that moves one of them is refused unless it
    types the key again, and the instance row is untouched by either: it is
    the row that owns the connection, not the scalar settings."""
    from app import config as app_config
    from arr_instances import service
    from arr_instances.repository import ArrInstanceRepository

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "stored",
                         "ip": "10.0.0.2", "port": 8989})
    monkeypatch.setattr(app_config.settings.general, "use_sonarr", True)
    # What the default instance's mirror carries onto the scalar settings in
    # production: the stored key the rule protects. Without it there is no
    # key to re-enter, and the move needs no refusal.
    monkeypatch.setattr(app_config.settings.sonarr, "apikey", "stored")
    # The writer is stubbed out, so no runtime restart or outbound call can
    # follow from a save, and the refusal is all this test observes.
    saved = []
    monkeypatch.setattr(app_config, "_save_settings_with_native",
                        lambda items, **_kwargs: saved.append(items))

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([("settings-sonarr-ip", ["moved.example"])])
    assert "Re-enter the key to use a different address" in str(refusal.value)
    assert saved == [], "the refused save reached no writer"

    # The key typed again is all the move needs.
    app_config.save_settings([("settings-sonarr-ip", ["moved.example"]),
                              ("settings-sonarr-apikey", ["typed-again"])])
    assert saved, "the save with the key typed again was not refused"

    row = ArrInstanceRepository(schema_session).get(created["id"])
    assert row.ip == "10.0.0.2", "the instance row keeps the address it was saved with"
    assert row.port == 8989


@pytest.mark.parametrize("clear", [[""], ["null"]])
def test_a_scalar_settings_save_with_no_retained_key_moves_the_address(
        schema_session, monkeypatch, clear):
    """The scalar guard protects the key the scalar settings still carry: a
    save that clears the stored key moves the address with it, because after
    the save there is no key left for the moved address to send, and with no
    stored key at all there is nothing to re-enter either."""
    from app import config as app_config
    from app.config import settings as app_settings
    from arr_instances import service

    created, _ = service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "stored",
                         "ip": "10.0.0.2", "port": 8989})
    monkeypatch.setattr(app_config.settings.general, "use_sonarr", True)
    monkeypatch.setattr(app_settings.sonarr, "apikey", "stored-key")
    saved = []
    monkeypatch.setattr(app_config, "_save_settings_with_native",
                        lambda items, **_kwargs: saved.append(items))

    app_config.save_settings([("settings-sonarr-ip", ["moved.example"]),
                              ("settings-sonarr-apikey", clear)])
    assert saved, "a move that clears the stored key was not refused"

    monkeypatch.setattr(app_settings.sonarr, "apikey", "")
    app_config.save_settings([("settings-sonarr-ip", ["moved.example"])])
    assert saved, "a move with no stored key at all was not refused"


_DEFAULTS = {"ssl": False, "ip": "127.0.0.1", "port": 8989, "base_url": "/"}


@pytest.mark.parametrize("field,stored", [("ssl", True), ("ip", "10.0.0.2"),
                                          ("port", 8990), ("base_url", "/tv")])
@pytest.mark.parametrize("spelling", ["null", "None", "undefined"])
def test_a_scalar_settings_save_clearing_a_destination_field_still_moves_the_address(
        schema_session, monkeypatch, field, stored, spelling):
    """An empty spelling clears the stored value, and the cleared field falls
    back to the default the client applies, so the effective destination moves
    from the stored address to the default one while the mirrored key stays
    stored: the same re-enter refusal a typed move gets, not a silent move the
    cleared field makes."""
    from app import config as app_config
    from app.config import settings as app_settings
    from arr_instances import service

    service.create_instance(
        schema_session, {"kind": "sonarr", "name": "Main", "api_key": "stored",
                         "ip": "10.0.0.2", "port": 8989})
    monkeypatch.setattr(app_config.settings.general, "use_sonarr", True)
    monkeypatch.setattr(app_settings.sonarr, "apikey", "stored")
    monkeypatch.setattr(app_settings.sonarr, field, stored)
    saved = []
    monkeypatch.setattr(app_config, "_save_settings_with_native",
                        lambda items, **_kwargs: saved.append(items))

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([(f"settings-sonarr-{field}", [spelling])])
    assert "Re-enter the key to use a different address" in str(refusal.value)
    assert saved == [], "the refused save reached no writer"
    assert app_settings.sonarr.get(field) == stored, "the refused save changed nothing"

    # A field already sitting at the default it falls back to moves nothing:
    # the clear keeps the destination where it is, key included.
    monkeypatch.setattr(app_settings.sonarr, field, _DEFAULTS[field])
    app_config.save_settings([(f"settings-sonarr-{field}", [spelling])])
    assert saved, "a clear that reaches the default the field already had was refused"


# The same two answers the direct rows prove, on the HTTP route the settings
# page saves through, so the form values the route parses and the validator
# defaults the guard compares are the ones production runs with.


@pytest.fixture
def arr_settings_api(schema_session, monkeypatch):
    """The settings save route, on a real app, with the writer recorded rather
    than run and the saved-change announcement collected."""
    from api import api_bp
    from api.system import settings as settings_endpoint
    from app import config as app_config
    from app import database as app_database
    from flask import Flask

    saved = []
    monkeypatch.setitem(app_config.settings.auth, "apikey", "synthetic-bazarr-key")
    monkeypatch.setattr(app_database, "database", schema_session)
    monkeypatch.setattr(app_config, "_save_settings_with_native",
                        lambda items, **_kwargs: saved.append(items))
    monkeypatch.setattr(settings_endpoint, "event_stream", lambda *_args, **_kwargs: None)
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return app.test_client(), saved


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
@pytest.mark.parametrize("spelling", ["null", "None", "undefined"])
def test_a_settings_route_clear_of_a_moved_destination_field_is_refused_with_persistence_kept(
        arr_settings_api, monkeypatch, kind, spelling):
    """The scalar clear the guard refuses, proven through the route: the
    empty spelling arrives as a form value, the route parses it the way the
    writer does, and the clear that would move the stored key to the default
    address is answered with the re-enter refusal, nothing written and the
    stored settings unchanged."""
    from app import config as app_config

    client, saved = arr_settings_api
    section = getattr(app_config.settings, kind)
    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    monkeypatch.setattr(section, "apikey", "stored-key")
    monkeypatch.setattr(section, "ip", "10.0.0.2")

    response = client.post("/api/system/settings",
                           data={f"settings-{kind}-ip": spelling},
                           headers={"X-API-KEY": "synthetic-bazarr-key"})

    assert response.status_code == 406
    assert "Re-enter the key to use a different address" in response.get_data(as_text=True)
    assert saved == [], "the refused save reached no writer"
    assert section.get("ip") == "10.0.0.2", "the refused save changed nothing"
    assert section.get("apikey") == "stored-key", "the stored key is untouched"


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_a_settings_route_clear_of_a_field_already_at_its_default_saves(
        arr_settings_api, monkeypatch, kind):
    """A field already sitting at the default a clear falls back to moves
    nothing, so the route saves it: the clear arrives through the same form
    parsing, the guard reads the validator's own default, and the save
    reaches the writer instead of being refused."""
    from app import config as app_config

    client, saved = arr_settings_api
    section = getattr(app_config.settings, kind)
    monkeypatch.setattr(app_config.settings.general, f"use_{kind}", True)
    monkeypatch.setattr(section, "apikey", "stored-key")
    monkeypatch.setattr(section, "ip", "127.0.0.1")

    response = client.post("/api/system/settings",
                           data={f"settings-{kind}-ip": "null"},
                           headers={"X-API-KEY": "synthetic-bazarr-key"})

    assert response.status_code == 204
    assert saved, "the clear that moved nothing was refused"


@pytest.mark.parametrize("section", ["plex", "sonarr", "radarr"])
@pytest.mark.parametrize("spelling", ["null", "None", "undefined", "0"])
def test_turning_certificate_verification_off_by_an_unwritten_spelling_still_needs_the_key(
        monkeypatch, section, spelling):
    """verify_ssl spelled the way the writer maps to empty, or to zero, is the
    same off switch as 'false': the settings rules read the value the writer
    would store, not the truthy string the request submitted, so the stored key
    never travels unverified just because the switch was spelled oddly."""
    from app import config as app_config
    from app.config import settings as app_settings

    sect = getattr(app_settings, section)
    if section == "plex":
        monkeypatch.setattr(sect, "auth_method", "apikey")
    monkeypatch.setattr(sect, "apikey", "stored-key")
    monkeypatch.setattr(sect, "verify_ssl", True)
    assert app_config._plex_writer_value("verify_ssl", [spelling]) is not True, \
        "the value the writer would store is not on"
    saved = []
    monkeypatch.setattr(app_config, "_save_settings_with_native",
                        lambda items, **_kwargs: saved.append(items))

    with pytest.raises(Exception) as refusal:
        app_config.save_settings([(f"settings-{section}-verify_ssl", [spelling])])
    assert "Re-enter the key to use a different address" in str(refusal.value)
    assert saved == [], "the refused save reached no writer"
    assert sect.get("verify_ssl") is True, "verification is still on"
    assert sect.get("apikey") == "stored-key", "the stored key is untouched"


@pytest.mark.parametrize("kind", ["sonarr", "radarr", "sportarr"])
def test_every_arr_session_refuses_to_follow_a_redirect_with_the_key(kind, http_fixture):
    """The shared per-kind sessions carry the instance's API key, so a server
    that answers with a redirect is not the instance the key belongs to: the
    next hop is never contacted, on a GET or a POST."""
    import requests
    from radarr.http_session import radarr_session
    from sonarr.http_session import sonarr_session
    from sportarr.http_session import sportarr_session

    sessions = {"sonarr": sonarr_session, "radarr": radarr_session,
                "sportarr": sportarr_session}
    target, target_records = http_fixture([])
    redirect = [(302, b"", {"Location": target + "/stolen"})]
    redirector, first_records = http_fixture([*redirect, *redirect])
    session = sessions[kind]()
    url = redirector + "/api/v3/system/status"

    # A refusal is either a raised redirect cap or the redirect handed back
    # unanswered; both leave the second origin untouched.
    try:
        response = session.get(url, headers={"X-Api-Key": "arr-key"}, timeout=5, verify=False)
        assert response.history == []
    except requests.exceptions.TooManyRedirects:
        pass
    try:
        response = session.post(url, json={}, headers={"X-Api-Key": "arr-key"},
                                timeout=5, verify=False)
        assert response.history == []
    except requests.exceptions.TooManyRedirects:
        pass

    assert target_records == [], "the API key never reached the second origin"
    assert len(first_records) == 2, "each request was answered by the first origin only"

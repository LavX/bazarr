# coding=utf-8
"""API service layer for arr_instances CRUD (#156).

These exercise the request-handling logic (status codes, response shaping,
api-key safety) without the heavy Flask/flask_restx import chain: the logic
lives in arr_instances.service and returns (body, status_code) tuples. The
thin Flask resources in bazarr/api/system/arr_instances.py just parse the
request, call these, and commit.
"""
import pytest

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

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

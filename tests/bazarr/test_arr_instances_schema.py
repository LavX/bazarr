# coding=utf-8
"""Phase 0/1 schema guardrails for the multiple-arr-instances feature (#156).

These assert the invariants of the new ``arr_instances`` table as built from
the ORM metadata - the fresh-install ``create_all`` path. The matching guarded
Alembic migration covers the existing-DB upgrade path and is tested separately.

Plan: docs/superpowers/plans/2026-05-27-multiple-arr-instances-final.md (Phase 1).
"""
import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

_DEFAULT_VALUES = dict(
    kind="sonarr",
    stable_key="sonarr-main",
    name="Sonarr Main",
    enabled=1,
    is_default=1,
    ip="127.0.0.1",
    port=8989,
    base_url="/",
    ssl=0,
    verify_ssl=0,
    http_timeout=60,
    api_key="",
)


def _insert_instance(session, **overrides):
    from app.database import TableArrInstances

    values = dict(_DEFAULT_VALUES)
    values.update(overrides)
    return session.execute(insert(TableArrInstances).values(**values))


def test_arr_instances_table_exists_with_identity_columns(schema_session):
    from app.database import TableArrInstances

    _insert_instance(schema_session)
    row = schema_session.execute(select(TableArrInstances)).scalar_one()

    assert row.id is not None
    assert row.kind == "sonarr"
    assert row.stable_key == "sonarr-main"
    assert row.name == "Sonarr Main"
    assert row.enabled == 1
    assert row.is_default == 1
    # connection fields mirror the scalar sonarr settings
    assert row.ip == "127.0.0.1"
    assert row.port == 8989
    assert row.base_url == "/"
    # api_key column exists to hold the (encrypted) per-instance key
    assert row.api_key == ""


def test_duplicate_kind_and_stable_key_is_rejected(schema_session):
    _insert_instance(schema_session, stable_key="sonarr-anime", is_default=0)
    with pytest.raises(IntegrityError):
        _insert_instance(schema_session, stable_key="sonarr-anime", is_default=0)
    schema_session.rollback()


def test_same_stable_key_allowed_across_different_kinds(schema_session):
    # stable_key uniqueness is scoped by kind: 'main' can exist for both a
    # Sonarr and a Radarr instance.
    from app.database import TableArrInstances

    _insert_instance(schema_session, kind="sonarr", stable_key="main", is_default=1)
    _insert_instance(schema_session, kind="radarr", stable_key="main", is_default=1, port=7878)
    rows = schema_session.execute(select(TableArrInstances)).scalars().all()
    assert {r.kind for r in rows} == {"sonarr", "radarr"}


def test_unknown_kind_is_rejected(schema_session):
    with pytest.raises(IntegrityError):
        _insert_instance(schema_session, kind="plex", stable_key="plex-1", is_default=0)
    schema_session.rollback()


def test_only_one_enabled_default_per_kind(schema_session):
    _insert_instance(schema_session, stable_key="sonarr-main", is_default=1, enabled=1)
    with pytest.raises(IntegrityError):
        _insert_instance(schema_session, stable_key="sonarr-anime", is_default=1, enabled=1)
    schema_session.rollback()


def test_second_non_default_enabled_instance_of_same_kind_is_allowed(schema_session):
    _insert_instance(schema_session, stable_key="sonarr-main", is_default=1, enabled=1)
    # a second enabled but NON-default instance of the same kind is fine
    _insert_instance(schema_session, stable_key="sonarr-anime", is_default=0, enabled=1)
    from app.database import TableArrInstances

    count = schema_session.execute(
        select(TableArrInstances).where(TableArrInstances.kind == "sonarr")
    ).scalars().all()
    assert len(count) == 2


def test_default_instance_must_be_enabled(schema_session):
    # is_default=1 with enabled=0 violates the "a default must be enabled" rule
    with pytest.raises(IntegrityError):
        _insert_instance(schema_session, stable_key="sonarr-main", is_default=1, enabled=0)
    schema_session.rollback()

# coding=utf-8
"""The release_type_mismatches table lands on SQLite and on PostgreSQL alike.

PostgreSQL is a first-class Bazarr+ backend, so the migration is exercised
against a real server (skipped only when none is reachable; CI provides one).
Both backends are also run twice: an upgrade that has already been applied must
be a no-op, never a failure.
"""
import importlib.util
import os

import pytest
import sqlalchemy as sa

_TABLE = 'release_type_mismatches'
_INDEX = 'ix_release_type_mismatches_item'

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "migrations", "versions",
    "a3f1c7d90b21_release_type_mismatches.py")

_PG_URL = os.environ.get(
    "BAZARR_PG_TEST_URL",
    "postgresql+psycopg://postgres:test@127.0.0.1:55432/bazarr")


def _load_migration():
    spec = importlib.util.spec_from_file_location("_release_type_mismatches", _MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row_values(video_release_type="web"):
    from datetime import datetime

    return {
        'media_type': 'series', 'media_id': 101, 'arr_instance_id': 2,
        'language': 'en', 'video_release_type': video_release_type,
        'subtitle_release_type': 'blu-ray', 'provider': 'goodsubs',
        'release_info': 'Show.S01E01.1080p.BluRay.x264-GRP', 'score': 270,
        'detected_at': datetime(2026, 1, 1, 12, 0, 0),
    }


def _insert(conn, **overrides):
    values = _row_values()
    values.update(overrides)
    columns = ', '.join(values)
    placeholders = ', '.join(f':{name}' for name in values)
    conn.execute(sa.text(f'INSERT INTO {_TABLE} ({columns}) VALUES ({placeholders})'), values)


def _apply_twice(bind):
    mod = _load_migration()
    assert mod.create_release_type_mismatches(bind) is True
    # Second run is what an already-upgraded install does: no-op, no error.
    assert mod.create_release_type_mismatches(bind) is False


@pytest.fixture
def sqlite_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def pg_engine():
    try:
        engine = sa.create_engine(_PG_URL)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception:
        pytest.skip(f"Postgres not reachable at {_PG_URL}")
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", "pg"])
def bind(request):
    # Resolved lazily: an unreachable Postgres must skip only the Postgres
    # parametrisation, never the SQLite one.
    return request.getfixturevalue(f"{request.param}_engine")


def test_migration_creates_the_table_and_is_idempotent(bind):
    with bind.begin() as conn:
        _apply_twice(conn)
    inspector = sa.inspect(bind)
    assert _TABLE in inspector.get_table_names()
    assert _INDEX in {i['name'] for i in inspector.get_indexes(_TABLE)}


def test_the_migration_matches_the_orm_definition(bind):
    from app.database import TableReleaseTypeMismatch

    with bind.begin() as conn:
        _apply_twice(conn)

    migrated = {c['name'] for c in sa.inspect(bind).get_columns(_TABLE)}
    assert migrated == {c.name for c in TableReleaseTypeMismatch.__table__.columns}


def test_one_detection_per_item_language_and_release_type(bind):
    with bind.begin() as conn:
        _apply_twice(conn)
        _insert(conn)

    with pytest.raises(sa.exc.IntegrityError):
        with bind.begin() as conn:
            _insert(conn)

    # A different release type of the same item is a different situation.
    with bind.begin() as conn:
        _insert(conn, video_release_type='blu-ray')

    with bind.connect() as conn:
        assert conn.execute(sa.text(f'SELECT COUNT(*) FROM {_TABLE}')).scalar() == 2

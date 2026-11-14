"""Moving the Plugin Hub state into the database, and dropping the dead tables."""
import importlib.util
import json
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import provider_hub_state_engines as engines
from provider_hub_state_engines import raw_row

# Fixtures live in the helper module; binding them here makes pytest find them.
hub_engine = engines.hub_engine
hub_store = engines.hub_store

VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
REVISION = "b6d9f3a1c872"
PREVIOUS = "a5e2c8d19f73"
UNUSED = ("provider_hub_catalog_sources", "provider_hub_catalog_entries", "provider_hub_installations",
          "provider_hub_config", "provider_hub_secrets", "provider_hub_jobs", "provider_hub_install_events")


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration(legacy=None):
    module = _load("b6d9f3a1c872_provider_hub_state_document.py", "provider_hub_state_document_migration")
    if legacy is not None:
        module._legacy_path = lambda: str(legacy)
    return module


def state_table_exists(engine):
    return "provider_hub_state" in sa.inspect(engine).get_table_names()


def _tables(connection):
    return set(sa.inspect(connection).get_table_names())


def _run(connection, step):
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    with Operations.context(context):
        step()


def _with_old_tables(connection):
    _run(connection, _load("f2f74f2d6d0a_provider_hub_v1.py", "provider_hub_v1_migration").upgrade)
    assert set(UNUSED) <= _tables(connection)


def test_the_new_revision_is_on_the_one_chain():
    """The chain has exactly one head, and this revision is that head or one of its ancestors."""
    parents_of = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r"^revision\s*=\s*['\"](\w+)", text, re.M)
        down = re.search(r"^down_revision\s*=\s*(.+)$", text, re.M)
        if revision:
            parents_of[revision.group(1)] = tuple(re.findall(r"['\"](\w+)['\"]", down.group(1))) if down else ()
    heads = set(parents_of) - {parent for parents in parents_of.values() for parent in parents}
    assert len(heads) == 1, f"expected a single head, found {sorted(heads)}"
    reachable, pending = set(), [heads.pop()]
    while pending:
        current = pending.pop()
        if current not in reachable:
            reachable.add(current)
            pending.extend(parents_of.get(current, ()))
    assert REVISION in reachable
    migration = _migration()
    assert (migration.revision, migration.down_revision) == (REVISION, PREVIOUS)


def test_upgrade_imports_state_json_and_drops_the_unused_tables(hub_store, hub_engine):
    document = {"installations": {"demo": {"provider_id": "demo", "state": "active"}}, "jobs": [{"id": "j"}]}
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    with hub_engine.connect() as connection:
        _with_old_tables(connection)
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
        tables = _tables(connection)
    assert "provider_hub_state" in tables
    assert not set(UNUSED) & tables
    revision, stored, schema_version = raw_row(hub_engine)
    assert (revision, schema_version) == (1, 1)
    assert json.loads(stored) == document
    assert path.read_bytes() == before

    from provider_hub.state import load_state
    state = load_state()
    assert state["installations"]["demo"]["state"] == "active"
    assert state["catalog_sources"]["official"]["official"] is True


def test_upgrade_is_repeatable_and_never_overwrites_the_row(hub_store, hub_engine):
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"installations": {}, "jobs": [{"id": "first"}]}), encoding="utf-8")
    with hub_engine.connect() as connection:
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
        path.write_text(json.dumps({"installations": {}, "jobs": [{"id": "second"}]}), encoding="utf-8")
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
    assert json.loads(raw_row(hub_engine)[1]) == {"installations": {}, "jobs": [{"id": "first"}]}


def test_upgrade_after_the_app_created_the_table_is_a_no_op(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state
    load_state()
    mutate_state(lambda state: state["jobs"].append({"id": "kept"}))
    before = raw_row(hub_engine)
    with hub_engine.connect() as connection:
        _with_old_tables(connection)
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
    assert raw_row(hub_engine) == before


def test_upgrade_without_a_state_file_stores_an_empty_document(hub_store, hub_engine):
    with hub_engine.connect() as connection:
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
    assert json.loads(raw_row(hub_engine)[1]) == {"installations": {}}


def test_an_unreadable_state_file_does_not_stop_the_upgrade(hub_store, hub_engine):
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    with hub_engine.connect() as connection:
        _with_old_tables(connection)
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
        tables = _tables(connection)
    assert "provider_hub_state" in tables
    assert not set(UNUSED) & tables
    assert raw_row(hub_engine) is None
    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("content", ["[]", "null", '{"jobs": []}'])
def test_a_state_file_that_is_not_a_state_document_is_not_imported(hub_store, hub_engine, content):
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with hub_engine.connect() as connection:
        _with_old_tables(connection)
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
        tables = _tables(connection)
    assert "provider_hub_state" in tables
    assert not set(UNUSED) & tables
    assert raw_row(hub_engine) is None
    assert path.read_text(encoding="utf-8") == content


def test_downgrade_drops_the_table_and_keeps_the_file(hub_store, hub_engine):
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"installations": {}, "jobs": []}), encoding="utf-8")
    with hub_engine.connect() as connection:
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
        _run(connection, _migration().downgrade)
        _run(connection, _migration().downgrade)
        assert "provider_hub_state" not in _tables(connection)
    assert path.exists()


def test_upgrading_again_after_a_downgrade_imports_the_edited_file(hub_store, hub_engine):
    """An older build keeps editing state.json; the next upgrade reads it again."""
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"installations": {}, "jobs": [{"id": "before"}]}), encoding="utf-8")
    with hub_engine.connect() as connection:
        _run(connection, _migration(path).upgrade)
        assert json.loads(raw_row(hub_engine)[1]) == {"installations": {}, "jobs": [{"id": "before"}]}
        _run(connection, _migration().downgrade)
        assert state_table_exists(hub_engine) is False
        path.write_text(json.dumps({"installations": {}, "jobs": [{"id": "before"}, {"id": "edited"}]}), encoding="utf-8")
        _run(connection, _migration(path).upgrade)
    revision, stored, _version = raw_row(hub_engine)
    assert revision == 1
    assert [job["id"] for job in json.loads(stored)["jobs"]] == ["before", "edited"]


def test_create_all_and_the_migration_declare_the_same_table(hub_store, hub_engine):
    from app.database import TableProviderHubState
    with hub_engine.connect() as connection:
        _run(connection, _migration(hub_store.legacy_state_path()).upgrade)
        migrated = {c["name"]: (str(c["type"]), c["nullable"]) for c in sa.inspect(connection).get_columns("provider_hub_state")}
        connection.exec_driver_sql("DROP TABLE provider_hub_state")
        TableProviderHubState.__table__.create(connection)
        created = {c["name"]: (str(c["type"]), c["nullable"]) for c in sa.inspect(connection).get_columns("provider_hub_state")}
    assert migrated == created

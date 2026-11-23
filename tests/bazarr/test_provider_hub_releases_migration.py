"""Moving the active and staged releases out of the Plugin Hub state document into their own tables."""
import copy
import importlib.util
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import provider_hub_state_engines as engines
from provider_hub_state_engines import raw_row
from test_provider_hub_releases import _rich_rows, _row

hub_engine = engines.hub_engine
hub_store = engines.hub_store

VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
REVISION = "c3e8a1f5b972"
PREVIOUS = "a7c2e9f4b183"
TABLES = ("provider_hub_releases", "provider_hub_release_bindings")


def _migration():
    spec = importlib.util.spec_from_file_location(
        "provider_hub_releases_migration", VERSIONS / "c3e8a1f5b972_provider_hub_releases.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("evidence, bound", [
    ("matching", True),
    ("different-digest", False),
    ("different-digest-first", False),
    ("malformed-digest", False),
    ("malformed-peer-digest", False),
    ("duplicate", False),
    ("unrelated-source", True),
    ("historic-url", True),
    ("malformed-sources", False),
])
def test_legacy_release_binding_agrees_on_retained_artifact_evidence(evidence, bound):
    from provider_hub import releases

    plugin_id, version, source_id = "sample", "1.0.0", "community"
    url, commit = "https://example.invalid/catalog.json", "a" * 40
    digest = "b" * 64
    manifest = {"schema_version": 1, "provider_id": plugin_id, "version": version,
                "source": {"commit": commit}, "bundle_sha256": digest}
    source_records = {source_id: {"url": url, "resolved_commit": commit}}
    if evidence == "historic-url":
        manifest["source"]["catalog_url"] = url
        source_records[source_id].pop("resolved_commit")
    elif evidence == "malformed-digest":
        manifest["bundle_sha256"] = [digest]
    elif evidence == "malformed-sources":
        source_records = [source_id]

    cached = {**manifest, "source": {"commit": commit, "catalog_url": url, "trusted": False}}
    entry = {"source": source_id, "provider_id": plugin_id, "version": version,
             "resolved_commit": commit, "manifest": cached}
    entries = {} if evidence == "historic-url" else {"first": entry}
    if evidence.startswith("different-digest") or evidence in {"unrelated-source", "malformed-peer-digest"}:
        peer_digest = None if evidence == "malformed-peer-digest" else "c" * 64
        peer = {**entry, "manifest": {**cached, "bundle_sha256": peer_digest}}
        if evidence == "unrelated-source":
            peer["source"] = "other"
        entries = {"second": peer, **entries} if evidence.endswith("-first") else {**entries, "second": peer}
    elif evidence == "duplicate":
        entries["second"] = copy.deepcopy(entry)

    row = {"origin": "catalog", "source_id": source_id, "active_version": version, "manifest": manifest}
    document = {"catalog_sources": source_records, "catalog_entries": entries, "installations": {plugin_id: row}}
    expected_fields = ["catalog_registration:legacy"] if bound else []
    assert _migration()._legacy_catalog_fields(document, plugin_id, row, version, manifest) == expected_fields

    stored, plan = releases.split(releases.compose(copy.deepcopy(document), []), [])
    (release, _), = plan.inserts
    assert json.loads(release["fields"]) == ["manifest", *expected_fields]
    expected_source = source_id if bound else None
    assert release["source_id"] == stored["installations"][plugin_id]["source_id"] == expected_source


def _run(connection, step):
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})
    with Operations.context(context):
        step()


def _tables(engine):
    names = set(sa.inspect(engine).get_table_names())
    return [name for name in TABLES if name in names]


def _before_the_release_tables(hub_store, hub_engine, document):
    """The database the previous revision left: the state document with the release fields inline, no release tables."""
    hub_store.ensure_ready()
    with hub_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE provider_hub_release_bindings")
        connection.exec_driver_sql("DROP TABLE provider_hub_releases")
        connection.execute(sa.text("UPDATE provider_hub_state SET document = :document WHERE id = 1"),
                           {"document": json.dumps(document, sort_keys=True)})
    hub_store._ready.clear()
    assert _tables(hub_engine) == []


def _full_document():
    installations = {key: value for key, value in _rich_rows().items()}
    # Activation writes an empty map of bindings, and a row can carry a null manifest: both read back as written.
    installations["iota"] = _row("iota", active="1.0.0", supplied_by={}, source_id="community", origin="local")
    installations["kappa"] = _row("kappa", active="1.0.0")
    installations["kappa"]["manifest"] = None
    installations["lambda"] = _row("lambda", active="1.0.0", staged="1.1.0", supplied_by=None, staged_supplied_by=None)
    return {"installations": installations, "jobs": [{"id": "j"}], "plugin_secret_paths": ["alpha.api_key"],
            "kept_data": {}, "catalog_sources": {}, "catalog_entries": {}, "plugin_envs": {}}


def test_the_revision_follows_the_previous_head_and_is_the_one_head():
    parent_of = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r"^revision\s*=\s*['\"](\w+)", text, re.M)
        parent = re.search(r"^down_revision\s*=\s*(.+)$", text, re.M)
        if revision:
            parent_of[revision.group(1)] = tuple(re.findall(r"['\"](\w+)['\"]", parent.group(1))) if parent else ()
    assert parent_of[REVISION] == (PREVIOUS,)
    heads = set(parent_of) - {parent for parents in parent_of.values() for parent in parents}
    assert len(heads) == 1, f"expected a single head, found {sorted(heads)}"
    migration = _migration()
    assert (migration.revision, migration.down_revision) == (REVISION, PREVIOUS)


def test_an_upgrade_loses_nothing_from_a_document_with_every_kind_of_row(hub_store, hub_engine, tmp_path, monkeypatch):
    from provider_hub import state
    from provider_hub.state import load_state_strict

    monkeypatch.setattr(state, "state_file", lambda: tmp_path / "provider_hub" / "state.json")
    original = _full_document()
    _before_the_release_tables(hub_store, hub_engine, original)
    revision_before = raw_row(hub_engine)[0]

    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)

    assert _tables(hub_engine) == list(TABLES)
    revision, stored, _version = raw_row(hub_engine)
    assert revision == revision_before + 1
    stored = json.loads(stored)
    # The rows with a release lost exactly their release fields; everything else is where it was.
    assert "active_version" not in stored["installations"]["alpha"]
    assert stored["installations"]["alpha"]["config"] == {"region": "eu"}
    assert stored["installations"]["delta"]["revoked_reason"] == "withdrawn"
    assert stored["installations"]["epsilon"]["blocked_reason"].startswith("It needs alpha")
    assert stored["installations"]["gamma"]["state"] == "removed"
    assert stored["installations"]["eta"]["python_path"] == 5
    assert stored["installations"]["zeta"]["manifest"]["provider_id"] == "zeta"
    assert stored["jobs"] == [{"id": "j"}] and stored["plugin_secret_paths"] == ["alpha.api_key"]
    # Read back through the store, every row is what it was before the upgrade.
    after = load_state_strict()["installations"]
    for plugin_id, row in original["installations"].items():
        assert after[plugin_id] == row, plugin_id


def test_the_upgrade_makes_the_releases_the_store_would_have_made(hub_store, hub_engine, tmp_path, monkeypatch):
    from provider_hub import releases, state, state_store

    monkeypatch.setattr(state, "state_file", lambda: tmp_path / "provider_hub" / "state.json")
    original = _full_document()
    _before_the_release_tables(hub_store, hub_engine, original)
    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)

    expected = releases.split(copy.deepcopy(original), [])[1]
    wanted = sorted(
        (values["plugin_id"], values["version"], values["state"], values["bundle_path"], values["python_path"],
         values["manifest"], values["bundle_digest"], values.get("activated_at"), sorted(bindings.items()),
         values["fields"], values["source_id"], values["origin"])
        for values, bindings in expected.inserts)
    found = sorted(
        (item.plugin_id, item.version, item.state, item.bundle_path, item.python_path,
         json.dumps(item.manifest, sort_keys=True) if item.manifest is not None else None, item.bundle_digest,
         item.activated_at if item.state == "active" else None, sorted(item.supplied_by.items()),
         _fields_of(hub_engine, item.id), item.source_id, item.origin)
        for item in state_store.list_releases(bind=hub_engine))
    assert found == wanted
    assert len(found) == 11
    assert {(item[0], item[-2], item[-1]) for item in found if item[0] == "iota"} == {("iota", "community", "local")}


def _fields_of(engine, release_id):
    with engine.connect() as connection:
        return connection.execute(sa.text("SELECT fields FROM provider_hub_releases WHERE id = :id"),
                                  {"id": release_id}).scalar()


def test_a_second_upgrade_changes_nothing(hub_store, hub_engine):
    _before_the_release_tables(hub_store, hub_engine, _full_document())
    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)
    first = raw_row(hub_engine)
    with hub_engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT id, plugin_id, state FROM provider_hub_releases ORDER BY id")).all()
        _run(connection, _migration().upgrade)
        assert connection.execute(sa.text("SELECT id, plugin_id, state FROM provider_hub_releases ORDER BY id")).all() == rows

    assert raw_row(hub_engine) == first


def test_an_upgrade_leaves_releases_the_store_already_wrote_alone(hub_store, hub_engine, tmp_path, monkeypatch):
    from provider_hub import state, state_store
    from provider_hub.state import save_state

    monkeypatch.setattr(state, "state_file", lambda: tmp_path / "provider_hub" / "state.json")
    state.load_state()
    document = state.load_state()
    document["installations"].update(_rich_rows())
    save_state(document)
    before = (raw_row(hub_engine), [item.id for item in state_store.list_releases(bind=hub_engine)])

    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)

    assert (raw_row(hub_engine), [item.id for item in state_store.list_releases(bind=hub_engine)]) == before


def test_an_upgrade_with_no_state_row_only_creates_the_tables(hub_store, hub_engine):
    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)

    assert _tables(hub_engine) == list(TABLES)
    assert "provider_hub_state" not in sa.inspect(hub_engine).get_table_names()


def test_a_document_that_is_not_json_is_left_as_it_is(hub_store, hub_engine):
    hub_store.ensure_ready()
    with hub_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE provider_hub_release_bindings")
        connection.exec_driver_sql("DROP TABLE provider_hub_releases")
        connection.execute(sa.text("UPDATE provider_hub_state SET document = '{broken' WHERE id = 1"))
    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)

    assert raw_row(hub_engine)[1] == "{broken"
    assert _tables(hub_engine) == list(TABLES)


def test_a_downgrade_puts_the_fields_back_so_the_previous_revision_reads_what_it_always_did(
        hub_store, hub_engine, tmp_path, monkeypatch):
    from provider_hub import state

    monkeypatch.setattr(state, "state_file", lambda: tmp_path / "provider_hub" / "state.json")
    original = _full_document()
    _before_the_release_tables(hub_store, hub_engine, original)
    with hub_engine.connect() as connection:
        _run(connection, _migration().upgrade)
        _run(connection, _migration().downgrade)

    assert _tables(hub_engine) == []
    assert json.loads(raw_row(hub_engine)[1]) == original
    with hub_engine.connect() as connection:
        _run(connection, _migration().downgrade)
    assert json.loads(raw_row(hub_engine)[1]) == original


def test_the_migration_and_the_models_declare_the_same_tables(hub_store, hub_engine):
    from app.database import TableProviderHubRelease, TableProviderHubReleaseBinding

    def shape(connection, name):
        inspector = sa.inspect(connection)
        columns = {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(name)}
        indexes = {(i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(name)}
        return columns, indexes

    with hub_engine.begin() as connection:
        _run(connection, _migration().upgrade)
        migrated = {name: shape(connection, name) for name in TABLES}
        connection.exec_driver_sql("DROP TABLE provider_hub_release_bindings")
        connection.exec_driver_sql("DROP TABLE provider_hub_releases")
        TableProviderHubRelease.__table__.create(connection)
        TableProviderHubReleaseBinding.__table__.create(connection)
        modelled = {name: shape(connection, name) for name in TABLES}
        # The store creates the same tables when it meets a database the migration has not reached.
        connection.exec_driver_sql("DROP TABLE provider_hub_release_bindings")
        connection.exec_driver_sql("DROP TABLE provider_hub_releases")
        from provider_hub import releases
        releases.create_tables(connection)
        stored = {name: shape(connection, name) for name in TABLES}

    assert migrated == modelled == stored


def _through_alembic(url, schema=None):
    """Stamp the previous head, upgrade to this revision and downgrade again with Alembic itself."""
    import flask_migrate
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

    app = Flask("provider-hub-releases-migration")
    app.config["SQLALCHEMY_DATABASE_URI"] = url
    if schema:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"options": f"-csearch_path={schema}"}}
    db = SQLAlchemy(app, metadata=sa.MetaData())
    directory = str(VERSIONS.parent)
    try:
        with app.app_context():
            flask_migrate.Migrate(app, db, render_as_batch=True)
            flask_migrate.stamp(directory=directory, revision=PREVIOUS)
            flask_migrate.upgrade(directory=directory, revision=REVISION)
            assert _tables(db.engine) == list(TABLES)
            flask_migrate.downgrade(directory=directory, revision=PREVIOUS)
            assert _tables(db.engine) == []
            flask_migrate.upgrade(directory=directory, revision=REVISION)
            assert _tables(db.engine) == list(TABLES)
    finally:
        with app.app_context():
            db.engine.dispose()


def test_sqlite_alembic_goes_up_and_down(tmp_path):
    _through_alembic(f"sqlite:///{tmp_path / 'alembic.db'}")


def test_postgres_alembic_goes_up_and_down():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for real PostgreSQL coverage")
    schema = "hub_releases_alembic_" + uuid4().hex
    admin = sa.create_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
        _through_alembic(url, schema)
    finally:
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()

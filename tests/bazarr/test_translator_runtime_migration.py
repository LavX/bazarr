"""The translation runtime tables: one revision, created once, idempotent, reversible, and the same as create_all builds."""
import importlib.util
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

VERSIONS = Path(__file__).parents[2] / "migrations" / "versions"
REVISION = "a7c2e9f4b183"
PREVIOUS = "b6d9f3a1c872"
TABLES = ("translator_engine_instances", "translator_connections", "translator_secrets", "translator_jobs",
          "translator_checkpoints", "translation_history", "translation_history_connections", "translation_usage",
          "translation_budgets")
LEGACY_INDEX = "ux_translator_engine_instances_legacy"
NOW = "2026-11-07 00:00:00"


def _migration():
    path = VERSIONS / "a7c2e9f4b183_translator_runtime_tables.py"
    spec = importlib.util.spec_from_file_location("translator_runtime_tables_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _present(connection):
    names = set(sa.inspect(connection).get_table_names())
    return [name for name in TABLES if name in names]


def _index_names(connection, table):
    return {item["name"] for item in sa.inspect(connection).get_indexes(table)}


def _count(connection, table):
    return connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar()


def _insert_instance(connection, plugin_id="ai_translator"):
    connection.execute(sa.text(
        "INSERT INTO translator_engine_instances (plugin_id, created_at, updated_at) VALUES (:p, :n, :n)"),
        {"p": plugin_id, "n": NOW})
    return connection.execute(sa.text(
        "SELECT max(id) FROM translator_engine_instances WHERE plugin_id = :p"), {"p": plugin_id}).scalar()


def _rejected(connection, statement, params):
    """True when the database refuses the statement, leaving the connection usable."""
    try:
        with connection.begin_nested():
            connection.execute(statement, params)
    except sa.exc.IntegrityError:
        return True
    return False


def _exercise(connection):
    migration = _migration()
    assert (migration.revision, migration.down_revision) == (REVISION, PREVIOUS)
    context = MigrationContext.configure(connection, opts={"render_as_batch": True})

    with Operations.context(context):
        migration.upgrade()
    assert _present(connection) == list(TABLES)
    assert LEGACY_INDEX in _index_names(connection, "translator_engine_instances")

    instance_id = _insert_instance(connection)
    defaults = connection.execute(sa.text(
        "SELECT enabled, revision, awaiting_secrets, settings_json FROM translator_engine_instances")).one()
    assert tuple(defaults) == (1, 1, 0, "{}")

    insert_connection = sa.text(
        "INSERT INTO translator_connections (instance_id, position, backend_type, created_at, updated_at) "
        "VALUES (:i, :p, 'openrouter', :n, :n)")
    connection.execute(insert_connection, {"i": instance_id, "p": 0, "n": NOW})
    prices = connection.execute(sa.text(
        "SELECT price_input_per_million, price_output_per_million, enabled, revision "
        "FROM translator_connections")).one()
    assert tuple(prices) == (0, 0, 1, 1)
    assert _rejected(connection, insert_connection, {"i": instance_id, "p": 0, "n": NOW})  # one per position
    assert _rejected(connection, insert_connection, {"i": instance_id + 99, "p": 1, "n": NOW})  # parent must exist

    insert_secret = sa.text(
        "INSERT INTO translator_secrets (owner_kind, owner_id, field, encrypted_value, updated_at) "
        "VALUES (:k, 1, 'api_key', 'enc:v1:x', :n)")
    connection.execute(insert_secret, {"k": "connection", "n": NOW})
    assert _rejected(connection, insert_secret, {"k": "connection", "n": NOW})
    assert _rejected(connection, insert_secret, {"k": "other", "n": NOW})

    insert_job = sa.text(
        "INSERT INTO translator_jobs (source_path, target, state, created_at, updated_at) "
        "VALUES (:p, :t, :s, :n, :n)")
    connection.execute(insert_job, {"p": "/m/a.srt", "t": "fr", "s": "running", "n": NOW})
    for state in ("queued", "running", "awaiting_retry"):  # one active job per source and target
        assert _rejected(connection, insert_job, {"p": "/m/a.srt", "t": "fr", "s": state, "n": NOW})
    for params in ({"p": "/m/a.srt", "t": "fr", "s": "completed"}, {"p": "/m/a.srt", "t": "fr", "s": "superseded"},
                   {"p": "/m/a.srt", "t": "de", "s": "queued"}, {"p": "/m/b.srt", "t": "fr", "s": "queued"}):
        connection.execute(insert_job, {**params, "n": NOW})
    assert _rejected(connection, insert_job, {"p": "/m/c.srt", "t": "fr", "s": "bogus", "n": NOW})

    insert_checkpoint = sa.text(
        "INSERT INTO translator_checkpoints (source_path, source_digest, target, engine_instance_id, "
        "settings_revision, created_at, updated_at) VALUES ('/m/a.srt', 'd', 'fr', 1, 1, :n, :n)")
    connection.execute(insert_checkpoint, {"n": NOW})
    assert tuple(connection.execute(sa.text(
        "SELECT release_digest, batches_json, expires_at FROM translator_checkpoints")).one()) == ("", "{}", None)
    assert _rejected(connection, insert_checkpoint, {"n": NOW})

    connection.execute(sa.text(
        "INSERT INTO translation_history (job_id, created_at, updated_at) VALUES ('job-1', :n, :n)"), {"n": NOW})
    history_id = connection.execute(sa.text("SELECT id FROM translation_history")).scalar()
    assert tuple(connection.execute(sa.text(
        "SELECT status, input_tokens, cost_micros, failovers FROM translation_history")).one()) == ("running", 0, 0, 0)
    assert _rejected(connection, sa.text(
        "INSERT INTO translation_history (job_id, created_at, updated_at) VALUES ('job-1', :n, :n)"), {"n": NOW})
    insert_part = sa.text(
        "INSERT INTO translation_history_connections (history_id, connection_id, created_at, updated_at) "
        "VALUES (:h, 1, :n, :n)")
    connection.execute(insert_part, {"h": history_id, "n": NOW})
    assert _rejected(connection, insert_part, {"h": history_id, "n": NOW})
    assert _rejected(connection, insert_part, {"h": history_id + 99, "n": NOW})

    insert_usage = sa.text(
        "INSERT INTO translation_usage (job_id, call_id, month, created_at) VALUES ('job-1', 'c1', '2026-11', :n)")
    connection.execute(insert_usage, {"n": NOW})
    assert tuple(connection.execute(sa.text("SELECT state, worst_case_micros, actual_micros "
                                            "FROM translation_usage")).one()) == ("reserved", 0, None)
    assert _rejected(connection, insert_usage, {"n": NOW})
    insert_budget = sa.text(
        "INSERT INTO translation_budgets (job_id, month, created_at, updated_at) VALUES ('job-1', '2026-11', :n, :n)")
    connection.execute(insert_budget, {"n": NOW})
    assert _rejected(connection, insert_budget, {"n": NOW})

    # A second upgrade is what a fresh install runs after create_all; it leaves rows alone.
    with Operations.context(context):
        migration.upgrade()
    assert _count(connection, "translator_connections") == 1 and _count(connection, "translation_history") == 1

    # The history rows go with their parent.
    connection.execute(sa.text("DELETE FROM translation_history"))
    assert _count(connection, "translation_history_connections") == 0

    with Operations.context(context):
        migration.downgrade()
    assert _present(connection) == []
    assert LEGACY_INDEX not in {name for table in sa.inspect(connection).get_table_names()
                                for name in _index_names(connection, table)}
    with Operations.context(context):
        migration.downgrade()  # nothing left to drop
        migration.upgrade()
    assert _present(connection) == list(TABLES)


def _exercise_partial(connection):
    """Only some tables missing: a foreign key still resolves, and the legacy index is restored."""
    migration = _migration()
    for name in reversed(TABLES):  # start from nothing, whatever an earlier check left
        connection.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
    assert migration.create_translator_runtime_tables(connection) == list(TABLES)
    for name in ("translation_budgets", "translation_usage", "translation_history_connections",
                 "translation_history", "translator_checkpoints", "translator_jobs", "translator_secrets",
                 "translator_connections"):
        connection.execute(sa.text(f"DROP TABLE {name}"))
    connection.execute(sa.text(f"DROP INDEX {LEGACY_INDEX}"))
    created = migration.create_translator_runtime_tables(connection)
    assert sorted(created) == sorted(TABLES[1:])
    assert _present(connection) == list(TABLES)
    # The instances table survived without its index, so the index is added.
    assert LEGACY_INDEX in _index_names(connection, "translator_engine_instances")
    assert migration.create_translator_runtime_tables(connection) == []


def test_sqlite_translator_runtime_migration(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'translator.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            _exercise(connection)
    finally:
        engine.dispose()


def test_sqlite_creates_only_what_is_missing(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    try:
        with engine.begin() as connection:
            _exercise_partial(connection)
    finally:
        engine.dispose()


def test_postgres_translator_runtime_migration():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    engine = sa.create_engine(url)
    schema = "translator_runtime_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
            connection.execute(sa.text(f"SET search_path TO {schema}"))
            try:
                _exercise(connection)
                _exercise_partial(connection)
            finally:
                connection.execute(sa.text("SET search_path TO public"))
                connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
    finally:
        engine.dispose()


def _condition(index):
    """The WHERE of a partial index, without spaces or brackets, or an empty string."""
    options = index.get("dialect_options", {})
    where = options.get("sqlite_where")
    if where is None:
        where = options.get("postgresql_where")
    return "" if where is None else re.sub(r"[\s()]", "", str(where))


def _shape(engine):
    inspector = sa.inspect(engine)
    result = {}
    for table in TABLES:
        columns = {c["name"]: (str(c["type"]), c["nullable"], str(c["default"]))
                   for c in inspector.get_columns(table)}
        indexes = {i["name"]: (tuple(i["column_names"]), bool(i["unique"]), _condition(i))
                   for i in inspector.get_indexes(table)}
        uniques = {u["name"]: tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
        checks = {c["name"]: re.sub(r"[\s()]", "", c["sqltext"]) for c in inspector.get_check_constraints(table)}
        foreign = [(f["constrained_columns"], f["referred_table"], f["options"].get("ondelete"))
                   for f in inspector.get_foreign_keys(table)]
        result[table] = (columns, indexes, uniques, checks, foreign)
    return result


def test_fresh_schema_matches_the_migration(tmp_path):
    """create_all and the migration must build the same tables, or a fresh and an upgraded install differ."""
    from app.database import Base

    fresh = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    migrated = sa.create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    try:
        Base.metadata.create_all(fresh, tables=[Base.metadata.tables[name] for name in TABLES])
        with migrated.begin() as connection:
            _migration().create_translator_runtime_tables(connection)
        assert _shape(fresh) == _shape(migrated)
        # The partial index is the one place a shape comparison could pass by leaving the condition out.
        for engine in (fresh, migrated):
            indexes = {i["name"]: i for i in sa.inspect(engine).get_indexes("translator_engine_instances")}
            assert indexes[LEGACY_INDEX]["unique"]
            assert "lingarr" in str(indexes[LEGACY_INDEX]["dialect_options"]["sqlite_where"])
    finally:
        fresh.dispose()
        migrated.dispose()


def test_postgres_fresh_schema_matches_the_migration():
    from app.database import Base

    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    names = {"fresh": "translator_fresh_" + uuid4().hex, "migrated": "translator_migrated_" + uuid4().hex}
    engines = {}
    admin = sa.create_engine(url)
    try:
        with admin.begin() as connection:
            for schema in names.values():
                connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
        for label, schema in names.items():
            engines[label] = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        Base.metadata.create_all(engines["fresh"], tables=[Base.metadata.tables[name] for name in TABLES])
        with engines["migrated"].begin() as connection:
            _migration().create_translator_runtime_tables(connection)
        assert _shape(engines["fresh"]) == _shape(engines["migrated"])
        indexes = {i["name"]: i for i in sa.inspect(engines["migrated"]).get_indexes("translator_engine_instances")}
        assert indexes[LEGACY_INDEX]["unique"] and "lingarr" in str(indexes[LEGACY_INDEX]["dialect_options"])
    finally:
        for engine in engines.values():
            engine.dispose()
        with admin.begin() as connection:
            for schema in names.values():
                connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()


def test_every_runtime_table_is_still_left_out_of_the_boot_create_all():
    from app.database import MIGRATION_ONLY_TABLES

    assert set(TABLES) <= set(MIGRATION_ONLY_TABLES)


def test_the_revision_is_the_one_head_and_follows_the_previous_head():
    parent_of = {}
    for path in VERSIONS.glob("*.py"):
        revision = re.search(r"^revision\s*=\s*['\"](\w+)", path.read_text(encoding="utf-8"), re.M)
        parent = re.search(r"^down_revision\s*=\s*(.+)$", path.read_text(encoding="utf-8"), re.M)
        if revision:
            parent_of[revision.group(1)] = tuple(re.findall(r"['\"](\w+)['\"]", parent.group(1))) if parent else ()
    assert parent_of[REVISION] == (PREVIOUS,)
    heads = set(parent_of) - {parent for parents in parent_of.values() for parent in parents}
    assert len(heads) == 1, f"expected a single head, found {sorted(heads)}"


def test_the_migration_allows_one_instance_of_an_engine_served_by_bazarr_and_many_of_any_other(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as connection:
            _migration().create_translator_runtime_tables(connection)
            _insert_instance(connection, "example_engine")
            _insert_instance(connection, "example_engine")
            _insert_instance(connection, "lingarr")
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as connection:
                _insert_instance(connection, "lingarr")
    finally:
        engine.dispose()


def _present_in(engine):
    with engine.connect() as connection:
        return _present(connection)


def _through_alembic(url, schema=None):
    """Stamp the previous head, upgrade to this revision and downgrade again with Alembic itself."""
    import flask_migrate
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

    app = Flask("translator-runtime-migration")
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
            assert _present_in(db.engine) == list(TABLES)
            flask_migrate.downgrade(directory=directory, revision=PREVIOUS)
            assert _present_in(db.engine) == []
            flask_migrate.upgrade(directory=directory, revision=REVISION)
            assert _present_in(db.engine) == list(TABLES)
    finally:
        with app.app_context():
            db.engine.dispose()


def test_sqlite_alembic_goes_up_and_down(tmp_path):
    _through_alembic(f"sqlite:///{tmp_path / 'alembic.db'}")


def test_postgres_alembic_goes_up_and_down():
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    schema = "translator_alembic_" + uuid4().hex
    admin = sa.create_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
        _through_alembic(url, schema)
    finally:
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()

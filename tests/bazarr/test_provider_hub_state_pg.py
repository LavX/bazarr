"""The Plugin Hub state document on the AUTOCOMMIT engines the app runs on.

Every case runs on SQLite and on a real PostgreSQL server. Under AUTOCOMMIT a
bare rollback() does nothing and begin_nested fails on PostgreSQL, so what
these check is that a write which fails part-way leaves the stored row exactly
as it was, read back through a second connection.
"""
import json
import threading

import pytest
import sqlalchemy as sa

import provider_hub_state_engines as engines
from provider_hub_state_engines import raw_row

# Fixtures live in the helper module; binding them here makes pytest find them.
hub_engine = engines.hub_engine
hub_store = engines.hub_store


def _installation(provider_id, version="1.0.0"):
    return {"provider_id": provider_id, "name": provider_id, "active_version": version,
            "state": "active", "pending_restart": False, "manifest": {"name": provider_id}}


def _legacy_file(store, content):
    path = store.legacy_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_first_use_creates_the_table_and_an_empty_state(hub_store, hub_engine):
    from provider_hub.state import default_state, load_state
    assert not sa.inspect(hub_engine).has_table("provider_hub_state")
    assert load_state() == default_state()
    assert raw_row(hub_engine)[0] == 1
    assert sa.inspect(hub_engine).has_table("provider_hub_state")


def test_round_trip_keeps_the_dict_shape_and_counts_revisions(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state, state_version, load_state_strict
    version = state_version()
    result = mutate_state(lambda state: state["installations"].update(demo=_installation("demo")) or "done")
    assert result == "done"
    assert state_version() == version + 1
    assert raw_row(hub_engine)[0] == 2
    state = load_state()
    assert state["installations"]["demo"]["active_version"] == "1.0.0"
    assert state["installations"]["demo"]["kind"]
    assert load_state_strict() == state


def test_save_state_replaces_the_document(hub_store, hub_engine):
    from provider_hub.state import default_state, load_state, save_state
    document = default_state()
    document["jobs"] = [{"id": "a"}]
    save_state(document)
    assert load_state()["jobs"] == [{"id": "a"}]
    assert raw_row(hub_engine)[0] == 2


def test_state_json_is_imported_once_and_kept(hub_store, hub_engine):
    from provider_hub.state import load_state
    path = _legacy_file(hub_store, json.dumps({"installations": {"old": _installation("old")}}))
    before = path.read_bytes()
    assert "old" in load_state()["installations"]
    assert path.read_bytes() == before
    # A later edit of the file is not picked up: the row is the state now.
    path.write_text(json.dumps({"installations": {"other": _installation("other")}}), encoding="utf-8")
    hub_store.use_engine(hub_engine)
    assert list(load_state()["installations"]) == ["old"]
    assert raw_row(hub_engine)[0] == 1


def test_an_unreadable_state_file_imports_nothing(hub_store, hub_engine):
    from provider_hub.state import load_state
    path = _legacy_file(hub_store, "{not json")
    with pytest.raises(ValueError):
        load_state()
    assert raw_row(hub_engine) is None
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_failing_mutator_leaves_the_row_untouched(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state
    mutate_state(lambda state: state["installations"].update(keep=_installation("keep")))
    before = raw_row(hub_engine)

    def half_applied(state):
        state["installations"]["new"] = _installation("new")
        state["jobs"].append({"id": "j"})
        state["plugin_secret_paths"].append("new.token")
        raise RuntimeError("fails after three keys changed")

    with pytest.raises(RuntimeError):
        mutate_state(half_applied)
    assert raw_row(hub_engine) == before
    assert list(load_state()["installations"]) == ["keep"]


def test_a_write_that_fails_inside_its_transaction_rolls_back(hub_store, hub_engine, monkeypatch):
    """The UPDATE ran, then the transaction failed: the row must not move."""
    from provider_hub.state import load_state, mutate_state
    mutate_state(lambda state: state["installations"].update(keep=_installation("keep")))
    before = raw_row(hub_engine)
    real = hub_store._commit_document

    def update_then_fail(connection, revision, document):
        real(connection, revision, document)
        inside = connection.execute(sa.text(
            "SELECT revision FROM provider_hub_state WHERE id = 1")).scalar_one()
        assert inside == revision + 1
        raise RuntimeError("fails after the UPDATE, before the commit")

    monkeypatch.setattr(hub_store, "_commit_document", update_then_fail)
    with pytest.raises(RuntimeError):
        mutate_state(lambda state: state["installations"].update(extra=_installation("extra"))
                     or state["jobs"].append({"id": "x"}))
    monkeypatch.setattr(hub_store, "_commit_document", real)
    assert raw_row(hub_engine) == before
    assert list(load_state()["installations"]) == ["keep"]


def test_concurrent_mutations_all_land(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state
    load_state()
    errors = []

    def add(index):
        try:
            mutate_state(lambda state: state["jobs"].append({"id": f"job-{index}"}))
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=add, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(job["id"] for job in load_state()["jobs"]) == sorted(f"job-{i}" for i in range(8))
    assert raw_row(hub_engine)[0] == 1 + 8


def _other_writer(engine, marker):
    """Another process committing a new revision behind the mutator's back."""
    with engine.connect() as connection:
        document = json.loads(connection.execute(sa.text(
            "SELECT document FROM provider_hub_state WHERE id = 1")).scalar_one())
        document.setdefault("installations", {})
        document.setdefault("jobs", []).append({"id": marker})
        connection.execute(
            sa.text("UPDATE provider_hub_state SET document = :d, revision = revision + 1 WHERE id = 1"),
            {"d": json.dumps(document, sort_keys=True)})


def test_a_stale_revision_is_retried_from_a_fresh_read(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state
    load_state()
    runs = []

    def mutator(state):
        runs.append(len(state["jobs"]))
        if len(runs) == 1:
            _other_writer(hub_engine, "other")
        state["jobs"].append({"id": "mine"})

    mutate_state(mutator)
    assert runs == [0, 1]
    assert [job["id"] for job in load_state()["jobs"]] == ["other", "mine"]
    assert raw_row(hub_engine)[0] == 3


def test_two_lost_races_raise_and_write_nothing(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state
    load_state()
    count = []

    def mutator(state):
        count.append(1)
        _other_writer(hub_engine, f"other-{len(count)}")
        state["jobs"].append({"id": "mine"})

    with pytest.raises(hub_store.StateConflictError):
        mutate_state(mutator)
    assert [job["id"] for job in load_state()["jobs"]] == ["other-1", "other-2"]


def test_a_nested_mutate_commits_with_its_outer_call(hub_store, hub_engine):
    from provider_hub.state import load_state, mutate_state

    def inner(state):
        state["plugin_secret_paths"].append("demo.token")

    def outer(state):
        state["installations"]["demo"] = _installation("demo")
        mutate_state(inner)

    mutate_state(outer)
    state = load_state()
    assert state["plugin_secret_paths"] == ["demo.token"]
    assert "demo" in state["installations"]
    assert raw_row(hub_engine)[0] == 2

    before = raw_row(hub_engine)

    def refused(state):
        mutate_state(inner)
        raise RuntimeError("refused after the nested edit")

    with pytest.raises(RuntimeError):
        mutate_state(refused)
    assert raw_row(hub_engine) == before


def test_a_document_that_is_not_an_object_is_unreadable_to_the_strict_reader(hub_store, hub_engine):
    from provider_hub.state import default_state, load_state, load_state_strict
    load_state()
    with hub_engine.connect() as connection:
        connection.execute(sa.text("UPDATE provider_hub_state SET document = '[]' WHERE id = 1"))
    assert load_state() == default_state()
    with pytest.raises(ValueError):
        load_state_strict()


@pytest.fixture
def snapshot_file(tmp_path, monkeypatch):
    """state.json as the running app would keep it next to the database."""
    from provider_hub import state
    path = tmp_path / "snapshot" / "state.json"
    monkeypatch.setattr(state, "state_file", lambda: path)
    return path


def test_every_commit_refreshes_the_state_json_copy(hub_store, hub_engine, snapshot_file):
    from provider_hub.state import mutate_state, save_state, default_state
    mutate_state(lambda state: state["jobs"].append({"id": "a"}))
    assert json.loads(snapshot_file.read_text(encoding="utf-8"))["jobs"] == [{"id": "a"}]
    document = default_state()
    document["jobs"] = [{"id": "b"}]
    save_state(document)
    assert json.loads(snapshot_file.read_text(encoding="utf-8"))["jobs"] == [{"id": "b"}]


def test_a_rolled_back_write_does_not_touch_the_copy(hub_store, hub_engine, snapshot_file):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["jobs"].append({"id": "a"}))
    before = snapshot_file.read_bytes()

    def fail(state):
        state["jobs"].append({"id": "lost"})
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        mutate_state(fail)
    assert snapshot_file.read_bytes() == before


def test_a_copy_that_cannot_be_written_does_not_undo_the_commit(hub_store, hub_engine, snapshot_file):
    from provider_hub.state import load_state, mutate_state
    snapshot_file.parent.mkdir(parents=True)
    snapshot_file.mkdir()  # a directory where the file goes: os.replace fails
    mutate_state(lambda state: state["jobs"].append({"id": "a"}))
    assert load_state()["jobs"] == [{"id": "a"}]


def test_while_held_reads_come_from_the_copy_and_writes_are_refused(hub_store, hub_engine, snapshot_file):
    from provider_hub import state
    state.mutate_state(lambda current: current["jobs"].append({"id": "committed"}))
    state.mutate_state(lambda current: current["jobs"].append({"id": "second"}))
    snapshot_file.write_text(json.dumps({"jobs": [{"id": "from the copy"}]}), encoding="utf-8")
    before = raw_row(hub_engine)
    state.hold_database()
    try:
        assert state.database_held()
        assert state.load_state()["jobs"] == [{"id": "from the copy"}]
        assert state.load_state_strict()["jobs"] == [{"id": "from the copy"}]
        with pytest.raises(state.StateNotReadyError):
            state.mutate_state(lambda current: None)
        with pytest.raises(state.StateNotReadyError):
            state.save_state(state.default_state())
        snapshot_file.unlink()
        assert state.load_state() == state.default_state()
    finally:
        state.release_database()
    assert not state.database_held()
    assert raw_row(hub_engine) == before
    assert [job["id"] for job in state.load_state()["jobs"]] == ["committed", "second"]


def test_the_file_override_is_never_held(monkeypatch, tmp_path):
    from provider_hub import state
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "override.json"))
    state.hold_database()
    try:
        assert not state.database_held()
        state.mutate_state(lambda current: current["jobs"].append({"id": "x"}))
        assert state.load_state()["jobs"] == [{"id": "x"}]
    finally:
        state.release_database()


def test_paths_found_while_held_are_written_once_the_database_opens(hub_store, hub_engine, snapshot_file):
    from provider_hub import state
    from secret_store import registry
    registry._PENDING_PERSISTED_PATHS.clear()
    state.hold_database()
    try:
        registry._persist_plugin_secret_paths({"demo.token"})
    finally:
        state.release_database()
    assert registry._PENDING_PERSISTED_PATHS == {"demo.token"}
    assert state.load_state()["plugin_secret_paths"] == []
    registry._flush_pending_persisted_paths()
    assert registry._PENDING_PERSISTED_PATHS == set()
    assert state.load_state()["plugin_secret_paths"] == ["demo.token"]


def test_a_nested_mutate_commits_with_its_outer_call_on_the_file_backend(monkeypatch, tmp_path):
    """The file backend means the same thing as the database for a nested call."""
    from provider_hub.state import load_state, mutate_state
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))

    def inner(state):
        state["plugin_secret_paths"].append("demo.token")

    def outer(state):
        state["installations"]["demo"] = _installation("demo")
        mutate_state(inner)

    mutate_state(outer)
    state = load_state()
    assert state["plugin_secret_paths"] == ["demo.token"]
    assert "demo" in state["installations"]

    def refused(state):
        state["plugin_secret_paths"].append("other.token")
        mutate_state(inner)
        raise RuntimeError("refused after the nested edit")

    with pytest.raises(RuntimeError):
        mutate_state(refused)
    assert load_state()["plugin_secret_paths"] == ["demo.token"]


def test_a_corrupt_file_copy_does_not_block_a_populated_database(hub_store, hub_engine):
    from provider_hub.state import load_state, load_state_strict, mutate_state
    mutate_state(lambda state: state["jobs"].append({"id": "kept"}))
    _legacy_file(hub_store, "{this is not json")
    hub_store.use_engine(hub_engine)  # a new process: the first access runs the import check again
    assert [job["id"] for job in load_state()["jobs"]] == ["kept"]
    assert [job["id"] for job in load_state_strict()["jobs"]] == ["kept"]
    mutate_state(lambda state: state["jobs"].append({"id": "next"}))
    assert raw_row(hub_engine)[0] == 3


def test_a_nested_mutate_on_another_file_writes_that_file(monkeypatch, tmp_path):
    from provider_hub.state import load_state, mutate_state
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(first))

    def outer(state):
        state["jobs"].append({"id": "in a"})
        mutate_state(lambda other: other["jobs"].append({"id": "in b"}), path=second)

    mutate_state(outer)
    assert [job["id"] for job in load_state(first)["jobs"]] == ["in a"]
    assert [job["id"] for job in load_state(second)["jobs"]] == ["in b"]


def test_a_nested_mutate_on_the_same_path_still_coalesces(monkeypatch, tmp_path):
    from provider_hub.state import load_state, mutate_state
    target = tmp_path / "a.json"

    def outer(state):
        state["jobs"].append({"id": "outer"})
        mutate_state(lambda other: other["jobs"].append({"id": "inner"}), path=target)

    mutate_state(outer, path=target)
    assert [job["id"] for job in load_state(target)["jobs"]] == ["outer", "inner"]


def test_a_restart_is_reported_from_the_kind_alone(hub_store, hub_engine, service_kind):
    """The one field every restart prompt reads: set while a change waits on a kind that attaches on restart, never
    for a kind that attaches live, and read back the same through a second connection."""
    from plugin_platform_helpers import SERVICE_KIND
    from provider_hub import service
    from provider_hub.state import mutate_state

    def store(state):
        state["installations"].update(
            provider={**_installation("provider"), "pending_restart": True},
            settled={**_installation("settled")},
            service={**_installation("service"), "kind": SERVICE_KIND, "pending_restart": True},
        )

    mutate_state(store)
    reported = {item["provider_id"]: item["needs_restart"] for item in service.list_installations()}
    assert reported == {"provider": True, "settled": False, "service": False}
    assert service.get_provider("provider")["needs_restart"] is True
    assert service.get_provider("service")["needs_restart"] is False
    # What is stored is still the plain fact; the field is worked out when it is served.
    stored = raw_row(hub_engine)[1]
    document = json.loads(stored) if isinstance(stored, str) else stored
    assert "needs_restart" not in document["installations"]["provider"]


_POSTGRES_RESTORE_PROGRAM = r"""
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import text
from app import config
from app.database import engine
from plugin_platform_helpers import provider_manifest_v2
from provider_hub import datadir, state
from provider_hub.sandbox import sweep
from utilities import backup

sys.modules['app.server'] = SimpleNamespace(webserver=None)
backup.restart_bazarr = lambda: os._exit(41)
backup.stop_bazarr = lambda code: os._exit(code & 255)
backup.event_stream = lambda **fields: None
backup.jobs_queue.update_job_name = lambda **fields: True
# These children never launch plugin workers. Let the real worker check see
# a complete, empty process list so it cannot inspect or signal host workers.
sweep._list_pids = lambda: ([], False)
root = Path(backup.args.config_dir)
run_tool = backup._run_postgres_tool
def count(tool, *args, **kwargs):
    with (root / 'postgres-tool-runs').open('a') as stream:
        stream.write(tool + '\n')
    return run_tool(tool, *args, **kwargs)
backup._run_postgres_tool = count

action, argument = sys.argv[1:]
if action == 'seed':
    config.open_database_after_backup_restore()
    document = state.load_state_strict()
    document['installations']['examplehub'] = {
        'state': 'active', 'kind': 'subtitle_provider', 'active_version': '1.0.0',
        'active_data_key': 'a' * 32,
        'manifest': provider_manifest_v2('examplehub', permissions=['data_dir']),
        'name': argument,
    }
    state.save_state(document)
elif action == 'backup':
    # The running application does this in init before it can take backups.
    sweep.configure_owner(root)
    assert backup.backup_to_zip(job_id=1)
elif action == 'prepare':
    assert backup.prepare_restore(argument)
elif action == 'check':
    config.open_database_after_backup_restore()
    with engine.connect() as connection:
        database, schema = connection.execute(text('SELECT current_database(), current_schema()')).one()
    print(json.dumps({'database': database, 'schema': schema,
                      'name': state.load_state_strict()['installations']['examplehub']['name']}))
else:
    if action in ('interrupt-before-complete', 'interrupt-after-complete'):
        write_phase = backup._write_plugin_restore_phase
        def interrupt(path, phase):
            if phase == 'database-complete' and action == 'interrupt-before-complete':
                os._exit(42)
            write_phase(path, phase)
            if phase == 'database-complete' and action == 'interrupt-after-complete':
                os._exit(42)
        backup._write_plugin_restore_phase = interrupt
    if action == 'folders-fail':
        def refuse_folders(*args, **kwargs):
            raise OSError('injected folder apply failure')
        datadir.restore_backup = refuse_folders
    finish = config.finish_backup_restore_startup
    def finish_and_exit():
        finish()
        os._exit(0)
    config.finish_backup_restore_startup = finish_and_exit
    import init
    raise AssertionError('The restore startup should have reached its exit hook')
"""


@pytest.fixture
def postgres_restore_instance(tmp_path):
    """A fresh application tree and database on the explicitly supplied test server."""
    import configparser
    import os
    import shutil
    import subprocess
    import sys
    import uuid
    from pathlib import Path
    from types import SimpleNamespace
    from zipfile import ZipFile

    import yaml

    from provider_hub.datadir import ARCHIVE_DIR, OMISSIONS_ARCHIVE

    test_url = os.getenv("BAZARR_PG_TEST_URL")
    if not test_url or not all(shutil.which(program) for program in ("pg_dump", "pg_restore")):
        pytest.skip("requires the PostgreSQL test server and pg_dump/pg_restore")
    url = sa.make_url(test_url)
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    database = "restore_" + uuid.uuid4().hex
    root = tmp_path / "instance"
    config = root / "config" / "config.yaml"
    for directory in ("config", "db", "backup", "restore", "cache", "log"):
        (root / directory).mkdir(parents=True)
    for name in ("releases.txt", "announcements.txt"):
        (config.parent / name).write_text("[]", encoding="utf-8")
    sample = root / "provider_hub" / "data" / "examplehub" / ("a" * 32) / "sample"
    sample.parent.mkdir(parents=True)
    sample.write_text("archived plugin files", encoding="utf-8")
    repo = Path(__file__).resolve().parents[2]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("POSTGRES_", "PG")) and key != "BAZARR_PROVIDER_HUB_STATE"}
    environment.update(BAZARR_CONFIG_DIR=str(root), NO_CLI="true", BAZARR_VERSION="test",
                       PYTHONDONTWRITEBYTECODE="1", PGCONNECT_TIMEOUT="3",
                       PYTHONPATH=os.pathsep.join(str(repo / name) for name in ("bazarr", "custom_libs", "tests/bazarr")))
    password = url.query.get("password") or url.password
    if password:
        environment["POSTGRES_PASSWORD"] = password
    created = []
    try:
        with admin.connect() as connection:
            info = connection.connection.driver_connection.info
            host, port, user = info.host, info.port, info.user
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
            created.append(database)
        direct_url = sa.URL.create(url.drivername, username=user, database=database,
                                   query={"host": host, "port": str(port), "connect_timeout": "3"})
        schema = "plugin_restore"

        def fresh_database():
            target = "restore_" + uuid.uuid4().hex
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{target}"')
            created.append(target)
            return direct_url.set(database=target)

        def configure(mode, *, target=None, schema_name=schema, search_path=None, create_schema=True):
            connection_url = target or direct_url
            environment.pop("PGSERVICEFILE", None)
            if mode == "service":
                parser = configparser.ConfigParser(interpolation=None)
                parser["bazarr_restore"] = {"host": host, "port": str(port), "user": user, "dbname": database}
                service_file = tmp_path / "pg_service.conf"
                with service_file.open("w", encoding="utf-8") as stream:
                    parser.write(stream, space_around_delimiters=False)
                environment["PGSERVICEFILE"] = str(service_file)
                connection_url = sa.URL.create(url.drivername, query={"service": "bazarr_restore"})
                assert connection_url.database is None and "dbname" not in connection_url.query
            elif mode == "multihost":
                # This directory never exists. libpq must fall back to the
                # real local socket or TCP endpoint without a test listener.
                unavailable = tmp_path / "missing-pg-socket"
                connection_url = direct_url.difference_update_query(("port",)).update_query_dict({
                    "host": (f"{unavailable}:{port}", f"{host}:{port}")})
            elif mode == "search_path":
                if create_schema:
                    database_engine = sa.create_engine(connection_url.set(password=password),
                                                       isolation_level="AUTOCOMMIT", hide_parameters=True)
                    try:
                        with database_engine.connect() as connection:
                            quoted_schema = connection.dialect.identifier_preparer.quote_identifier(schema_name)
                            connection.exec_driver_sql(f'CREATE SCHEMA {quoted_schema}')
                    finally:
                        database_engine.dispose()
                # libpq splits options at unescaped spaces before PostgreSQL
                # interprets the quoted search_path identifiers.
                path = (search_path or schema_name).replace(' ', r'\ ')
                connection_url = connection_url.update_query_dict({"options": f"-csearch_path={path}"})
            elif mode != "direct":
                raise AssertionError(mode)
            config.write_text(yaml.safe_dump({"general": {"port": 7000}, "postgresql": {
                "enabled": True, "url": connection_url.render_as_string(),
            }}), encoding="utf-8")

        def child(action, argument="", *, expected=0):
            run = subprocess.run([sys.executable, "-c", _POSTGRES_RESTORE_PROGRAM, action, argument],
                                 env=environment, capture_output=True, text=True, timeout=60)
            assert run.returncode == expected, run.stdout + run.stderr
            return run.stdout.strip()

        def archive():
            child("seed", "archived row")
            child("backup")
            [result] = (root / "backup").glob("*.zip")
            with ZipFile(result) as backup:
                assert OMISSIONS_ARCHIVE not in backup.namelist()
                entry = f"{ARCHIVE_DIR}/{sample.relative_to(root / 'provider_hub' / 'data').as_posix()}"
                assert backup.read(entry) == b"archived plugin files"
            return result

        configure("direct")
        yield SimpleNamespace(root=root, config=config, sample=sample, database=database, schema=schema,
                              configure=configure, child=child, archive=archive,
                              fresh_database=fresh_database,
                              stage=root / "restore" / ".backup-staged",
                              tool_runs=root / "postgres-tool-runs", direct_url=direct_url.set(password=password))
    finally:
        if created:
            with admin.connect() as connection:
                for name in created:
                    connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.mark.parametrize("connection_mode", ["service", "multihost", "search_path"])
def test_postgres_restore_uses_libpq_and_retries_folders_without_replaying_database(
        postgres_restore_instance, connection_mode):
    """The published pg_restore path works with the connection forms libpq supports."""
    from literals import EXIT_RESTORE_ERROR
    import yaml
    from zipfile import ZipFile

    instance = postgres_restore_instance
    instance.configure(connection_mode)
    archive = instance.archive()
    instance.child("seed", "changed row")
    instance.sample.write_text("changed plugin files", encoding="utf-8")
    # A backup can originate on another installation. The archived settings
    # must not move the next boot to a different DB, search path or engine.
    edited_archive = archive.with_name("bazarr_backup_vedited.zip")
    with ZipFile(archive) as original, ZipFile(edited_archive, "w") as edited:
        for name in original.namelist():
            value = original.read(name)
            if name == "config.yaml":
                settings = yaml.safe_load(value)
                settings["general"]["port"] = 7100
                settings["postgresql"] = {"enabled": False, "url": "postgresql://unused/different"}
                value = yaml.safe_dump(settings)
            edited.writestr(name, value)
    instance.child("prepare", edited_archive.name)
    payload = instance.stage / "payload"
    phase = instance.stage / ".plugin-restore-phase"
    assert (payload / "bazarr_postgres.dump").is_file()
    assert not phase.exists()

    # The database restore returned and its completion is durable, but this
    # process never installed the configuration or reached the restart.
    instance.child("interrupt-after-complete", expected=42)
    assert phase.read_text(encoding="utf-8").strip() == "database-complete"
    restored = json.loads(instance.child("check"))
    assert restored == {"database": instance.database,
                        "schema": instance.schema if connection_mode == "search_path" else "public",
                        "name": "archived row"}
    instance.child("seed", "written after database restore")
    instance.child("boot", expected=41)
    assert phase.read_text(encoding="utf-8").strip() == "data-ready"
    assert instance.sample.read_text(encoding="utf-8") == "changed plugin files"

    instance.child("folders-fail", expected=EXIT_RESTORE_ERROR & 255)
    assert phase.read_text(encoding="utf-8").strip() == "data-ready"
    assert (payload / "bazarr_postgres.dump").is_file()
    assert instance.sample.read_text(encoding="utf-8") == "changed plugin files"
    instance.child("boot")
    assert instance.sample.read_text(encoding="utf-8") == "archived plugin files"
    assert yaml.safe_load(instance.config.read_text(encoding="utf-8"))["general"]["port"] == 7100
    assert json.loads(instance.child("check"))["name"] == "written after database restore"
    assert instance.tool_runs.read_text(encoding="utf-8").splitlines() == ["pg_dump", "pg_restore"]
    assert not instance.stage.exists()
    if connection_mode == "search_path":
        engine = sa.create_engine(instance.direct_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        try:
            with engine.connect() as connection:
                assert connection.execute(sa.text("SELECT to_regclass('public.provider_hub_state')")).scalar_one() is None
                assert connection.execute(sa.text(
                    "SELECT to_regclass('plugin_restore.provider_hub_state')")).scalar_one() is not None
        finally:
            engine.dispose()


def test_postgres_restore_keeps_an_ambiguous_database_step_for_operator_verification(postgres_restore_instance):
    from literals import EXIT_RESTORE_ERROR

    instance = postgres_restore_instance
    archive = instance.archive()
    instance.child("seed", "changed row")
    instance.sample.write_text("changed plugin files", encoding="utf-8")
    instance.child("prepare", archive.name)
    phase = instance.stage / ".plugin-restore-phase"
    # pg_restore has really completed, but the process exits before its
    # filesystem completion checkpoint. A fresh process must not guess.
    instance.child("interrupt-before-complete", expected=42)
    assert phase.read_text(encoding="utf-8").strip() == "database-started"
    instance.child("boot", expected=EXIT_RESTORE_ERROR & 255)
    assert json.loads(instance.child("check"))["name"] == "archived row"
    assert instance.sample.read_text(encoding="utf-8") == "changed plugin files"
    assert (instance.stage / "payload" / "bazarr_postgres.dump").is_file()
    assert (instance.stage / "payload" / "config.yaml").is_file()
    assert archive.is_file()
    assert instance.tool_runs.read_text(encoding="utf-8").splitlines() == ["pg_dump", "pg_restore"]


@pytest.mark.parametrize('path_kind', ['custom-only', 'public-fallback', 'quoted-fallback', 'populated'])
def test_postgres_restore_creates_a_configured_schema_on_a_fresh_database(postgres_restore_instance, path_kind):
    from literals import EXIT_RESTORE_ERROR
    from zipfile import ZipFile

    instance = postgres_restore_instance
    schema = 'Plugin, Restore' if path_kind == 'quoted-fallback' else instance.schema
    path = '"' + schema + '"' if path_kind == 'quoted-fallback' else schema.upper()
    instance.configure('search_path', schema_name=schema, search_path=path)
    archive = instance.archive()
    if path_kind == 'custom-only':
        payload = instance.root / 'restore'
        with ZipFile(archive) as backup:
            backup.extractall(payload)
    else:
        # Queue from the running instance. The subsequent boot below is the
        # first application process to open the new database.
        instance.child('prepare', archive.name)
        payload = instance.stage / 'payload'
    target = instance.fresh_database()

    engine = sa.create_engine(target.set(password=instance.direct_url.password), hide_parameters=True)
    try:
        with engine.begin() as connection:
            assert connection.execute(sa.text('SELECT to_regnamespace(:name)'), {'name': path}).scalar_one() is None
            assert connection.exec_driver_sql("SELECT count(*) FROM information_schema.tables "
                                              "WHERE table_schema = 'public'").scalar_one() == 0
            if path_kind == 'populated':
                connection.exec_driver_sql('CREATE TABLE public.current_records (id integer PRIMARY KEY)')
                connection.exec_driver_sql('INSERT INTO public.current_records VALUES (12)')
        if path_kind != 'custom-only':
            path += ', public'
        instance.configure('search_path', target=target, search_path=path, create_schema=False)
        instance.sample.write_text('files before restore', encoding='utf-8')

        if path_kind == 'populated':
            instance.child('boot', expected=EXIT_RESTORE_ERROR & 255)
            assert (payload / 'bazarr_postgres.dump').is_file()
            assert instance.sample.read_text(encoding='utf-8') == 'files before restore'
            assert instance.tool_runs.read_text(encoding='utf-8').splitlines() == ['pg_dump']
            with engine.connect() as connection:
                assert connection.exec_driver_sql('SELECT id FROM public.current_records').scalar_one() == 12
        else:
            instance.child('boot', expected=41)
            instance.child('boot')
            assert json.loads(instance.child('check')) == {
                'database': target.database, 'schema': schema, 'name': 'archived row',
            }
            assert instance.sample.read_text(encoding='utf-8') == 'archived plugin files'
            assert instance.tool_runs.read_text(encoding='utf-8').splitlines() == ['pg_dump', 'pg_restore']
            assert not (payload / 'bazarr_postgres.dump').exists()
            assert not instance.stage.exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize('extra_inventory', [False, True], ids=['different-schema', 'multiple-schemas'])
def test_postgres_restore_preserves_payload_when_its_inventory_does_not_match_live_schema(
        postgres_restore_instance, extra_inventory):
    from literals import EXIT_RESTORE_ERROR

    instance = postgres_restore_instance
    if extra_inventory:
        instance.child('seed', 'other inventory in archive')
    instance.configure('search_path')
    archive = instance.archive()
    instance.child('seed', 'current custom schema inventory')
    instance.sample.write_text('current plugin files', encoding='utf-8')
    if not extra_inventory:
        instance.configure('direct')
        instance.child('seed', 'current target schema inventory with matching data key')
    instance.child('prepare', archive.name)
    instance.child('boot', expected=EXIT_RESTORE_ERROR & 255)

    expected = ('current custom schema inventory' if extra_inventory
                else 'current target schema inventory with matching data key')
    assert json.loads(instance.child('check'))['name'] == expected
    assert instance.sample.read_text(encoding='utf-8') == 'current plugin files'
    assert instance.tool_runs.read_text(encoding='utf-8').splitlines() == ['pg_dump']
    assert archive.is_file()
    assert (instance.stage / 'payload' / 'bazarr_postgres.dump').is_file()
    assert (instance.stage / 'payload' / 'config.yaml').is_file()
    assert not (instance.stage / '.plugin-restore-phase').exists()


def test_postgres_restore_accepts_a_legacy_archive_without_plugin_metadata(postgres_restore_instance):
    from zipfile import ZipFile

    instance = postgres_restore_instance
    archive = instance.archive()
    legacy = archive.with_name("bazarr_backup_vlegacy.zip")
    with ZipFile(archive) as source, ZipFile(legacy, "w") as target:
        for name in ("config.yaml", "bazarr_postgres.dump"):
            target.writestr(name, source.read(name))
    instance.child("seed", "changed row")
    instance.sample.write_text("changed plugin files", encoding="utf-8")
    instance.child("prepare", legacy.name)
    # The prior inventory is unreadable to the app. Startup must reach the
    # ordinary restore before trying to migrate or scrub that inventory.
    engine = sa.create_engine(instance.direct_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("ALTER TABLE provider_hub_state RENAME COLUMN document TO unreadable")
    finally:
        engine.dispose()
    instance.child("boot", expected=41)
    instance.child("boot")
    assert json.loads(instance.child("check"))["name"] == "archived row"
    assert instance.sample.read_text(encoding="utf-8") == "changed plugin files"
    assert instance.tool_runs.read_text(encoding="utf-8").splitlines() == ["pg_dump", "pg_restore"]
    assert not instance.stage.exists()

"""Safety properties of the Plugin Hub state on the database.

Credentials must stay out of database error text, the state.json copy must end
as the last committed state whichever writer publishes last, and a write that
loses a race must not repeat anything outside the document.
"""
import contextlib
import json
import logging
import sys
import threading
import time
import traceback

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import provider_hub_state_engines as engines
from provider_hub_state_engines import raw_row
from test_provider_hub import _manifest_with_region

hub_engine = engines.hub_engine
hub_store = engines.hub_store

SECRET = "plaintext-credential-9f3a"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _scrub_values_are_restored():
    """The store registers the credential values of every document it binds; leave the set as it was."""
    from secret_store import scrub
    from provider_hub import state_store
    saved = scrub.registered_secret_values()
    yield
    scrub.register_secret_values(saved)
    state_store._set_local_values(set())


@pytest.fixture
def snapshot_file(tmp_path, monkeypatch):
    from provider_hub import state
    path = tmp_path / "snapshot" / "state.json"
    monkeypatch.setattr(state, "state_file", lambda: path)
    return path


def _installation(provider_id="examplehub", **extra):
    row = {"provider_id": provider_id, "name": "Example", "active_version": "1.0.0",
           "state": "active", "pending_restart": False, "manifest": _manifest_with_region(provider_id=provider_id)}
    row.update(extra)
    return row


def _seed(installations):
    from provider_hub.state import load_state, save_state
    state = load_state()
    state["installations"].update(installations)
    save_state(state)


# Database error text -------------------------------------------------------


def _refusing_engine(tmp_path):
    """A plain engine, as a caller outside the app could hand in, on a table that refuses the document."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'refusing.db'}", poolclass=NullPool,
                              isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE provider_hub_state (id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, "
            "revision INTEGER NOT NULL, document TEXT NOT NULL CHECK (length(document) < 5), "
            "updated_at DATETIME NOT NULL)")
    return engine


def test_a_failed_import_prints_no_credential(tmp_path, monkeypatch, caplog):
    from provider_hub import state_store
    legacy = tmp_path / "provider_hub" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"installations": {"examplehub": {"config": {"api_key": SECRET}}}}),
                      encoding="utf-8")
    monkeypatch.setattr(state_store, "legacy_state_path", lambda: legacy)
    monkeypatch.delenv("BAZARR_PROVIDER_HUB_STATE", raising=False)
    engine = _refusing_engine(tmp_path)
    state_store.use_engine(engine)
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(state_store.StateStoreError) as failure:
                state_store.ensure_ready()
            logging.getLogger("provider_hub.test").error("import failed", exc_info=failure.value)
    finally:
        state_store.reset()
        engine.dispose()
    rendered = "".join(traceback.format_exception(failure.value))
    assert SECRET not in str(failure.value)
    assert SECRET not in rendered
    assert SECRET not in caplog.text
    # The driver's error is not attached in any way a traceback or a log would print.
    assert failure.value.__cause__ is None and failure.value.__context__ is None


def test_a_failed_write_prints_no_credential(hub_store, hub_engine, caplog):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["jobs"].append({"id": "seed"}))
    with hub_engine.connect() as connection:
        # The row stops accepting a document longer than the one it holds now.
        length = connection.execute(sa.text("SELECT length(document) FROM provider_hub_state")).scalar()
        if hub_engine.dialect.name == "sqlite":
            connection.exec_driver_sql(
                "CREATE TRIGGER refuse BEFORE UPDATE ON provider_hub_state BEGIN "
                f"SELECT RAISE(ABORT, 'refused') WHERE length(NEW.document) > {length}; END")
        else:
            connection.exec_driver_sql(
                "ALTER TABLE provider_hub_state ADD CONSTRAINT refuse "
                f"CHECK (length(document) <= {length})")

    def add_credential(state):
        # A key that sorts first puts the secret at the very start of the stored
        # text, where a constraint error's row preview would show it.
        state["0credential"] = SECRET

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(hub_store.StateStoreError) as failure:
            mutate_state(add_credential)
        logging.getLogger("provider_hub.test").error("write failed", exc_info=failure.value)
    assert failure.value.__cause__ is None and failure.value.__context__ is None
    assert SECRET not in str(failure.value)
    assert SECRET not in "".join(traceback.format_exception(failure.value))
    assert SECRET not in caplog.text


def test_every_engine_the_store_uses_hides_its_parameters(tmp_path):
    from provider_hub import state_store
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'plain.db'}", poolclass=NullPool)
    assert not engine.hide_parameters
    state_store.use_engine(engine)
    try:
        assert state_store.engine() is engine
        assert engine.hide_parameters is True
    finally:
        state_store.reset()
        engine.dispose()


# The state.json copy -------------------------------------------------------


def test_a_slow_publisher_cannot_overwrite_a_newer_copy(hub_store, hub_engine, snapshot_file, monkeypatch):
    """Writer A commits and is slow to publish; B commits and publishes after it.

    Other processes share no in-process lock, so the in-process one is
    removed here: the two writers are only ordered by the file lock.
    """
    from provider_hub import state

    @contextlib.contextmanager
    def no_process_lock():
        yield

    monkeypatch.setattr(state, "state_write_lock", no_process_lock)
    real = state.write_state_file
    first_publish_started = threading.Event()
    let_first_publish_go = threading.Event()
    calls = []

    def slow_first(data, current):
        calls.append(sorted(data.get("markers", [])))
        if len(calls) == 1:
            first_publish_started.set()
            let_first_publish_go.wait(20)
        real(data, current)

    monkeypatch.setattr(state, "write_state_file", slow_first)
    errors = []

    def writer(mark):
        try:
            state.mutate_state(lambda current: current.setdefault("markers", []).append(mark))
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    a = threading.Thread(target=writer, args=("a",))
    a.start()
    assert first_publish_started.wait(60), errors
    b = threading.Thread(target=writer, args=("b",))
    b.start()
    time.sleep(0.7)  # long enough for B to commit and publish, were it allowed to
    let_first_publish_go.set()
    a.join(30)
    b.join(30)

    assert not errors
    stored = json.loads(raw_row(hub_engine)[1])
    assert sorted(stored["markers"]) == ["a", "b"]
    assert json.loads(snapshot_file.read_text(encoding="utf-8")) == stored


def test_a_publisher_that_cannot_take_the_lock_leaves_the_copy_alone(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub import state
    state.mutate_state(lambda current: current.setdefault("markers", []).append("first"))
    before = snapshot_file.read_text(encoding="utf-8")

    @contextlib.contextmanager
    def lock_unavailable():
        yield False

    monkeypatch.setattr(state, "snapshot_lock", lock_unavailable)
    state.mutate_state(lambda current: current.setdefault("markers", []).append("second"))

    assert json.loads(raw_row(hub_engine)[1])["markers"] == ["first", "second"]
    assert snapshot_file.read_text(encoding="utf-8") == before


def test_the_copy_is_written_through_a_temp_file_of_its_own(tmp_path, monkeypatch):
    from provider_hub import state
    current = tmp_path / "state.json"
    (tmp_path / "state.json.tmp").mkdir()  # the one fixed name an older writer used
    state.write_state_file({"a": 1}, current)
    assert json.loads(current.read_text(encoding="utf-8")) == {"a": 1}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.json", "state.json.tmp"]

    sources = []
    real_replace = state.os.replace

    def refuse(source, target):
        sources.append(source)
        raise OSError("disk full")

    monkeypatch.setattr(state.os, "replace", refuse)
    with pytest.raises(OSError):
        state.write_state_file({"a": 2}, current)
    monkeypatch.setattr(state.os, "replace", real_replace)
    assert sources and sources[0] != str(tmp_path / "state.json.tmp")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["state.json", "state.json.tmp"]
    assert json.loads(current.read_text(encoding="utf-8")) == {"a": 1}


def test_two_writers_never_share_a_temp_file(tmp_path, monkeypatch):
    from provider_hub import state
    names = []
    real_replace = state.os.replace

    def record(source, target):
        names.append(source)
        return real_replace(source, target)

    monkeypatch.setattr(state.os, "replace", record)
    state.write_state_file({"n": 1}, tmp_path / "state.json")
    state.write_state_file({"n": 2}, tmp_path / "state.json")
    assert len(set(names)) == 2


# Retries -------------------------------------------------------------------


def _lose_races(monkeypatch, losses, only_documents_with=""):
    """Make the next ``losses`` commits (of documents containing the text) find the row already moved on."""
    from provider_hub import state_store
    real = state_store._commit_document
    remaining = [losses]

    def commit(connection, revision, document):
        if remaining[0] > 0 and only_documents_with in document:
            remaining[0] -= 1
            raise state_store._StaleRevision()
        return real(connection, revision, document)

    monkeypatch.setattr(state_store, "_commit_document", commit)


def _enable_calls(monkeypatch, result=True):
    calls = []
    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
                        lambda provider_id, enabled: calls.append((provider_id, enabled)) or result)
    return calls


def test_a_lost_race_runs_the_toggle_once_after_the_commit(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub.service import update_provider
    from provider_hub.state import load_state
    _seed({"examplehub": _installation()})
    calls = _enable_calls(monkeypatch)
    committed_when_toggled = []
    monkeypatch.setattr(
        "provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
        lambda provider_id, enabled: committed_when_toggled.append(
            load_state()["installations"][provider_id].get("enabled")) or calls.append((provider_id, enabled)) or True)
    _lose_races(monkeypatch, 1)

    update_provider("examplehub", enabled=True)

    assert calls == [("examplehub", True)]
    assert committed_when_toggled == [True]
    assert load_state()["installations"]["examplehub"]["enabled"] is True


def test_a_write_that_never_wins_leaves_the_settings_alone(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub.service import update_provider
    from provider_hub.state import load_state
    from provider_hub.state_store import StateConflictError
    _seed({"examplehub": _installation()})
    calls = _enable_calls(monkeypatch)
    _lose_races(monkeypatch, 5)
    before = raw_row(hub_engine)

    with pytest.raises(StateConflictError):
        update_provider("examplehub", enabled=True)

    assert calls == []
    assert raw_row(hub_engine) == before
    assert "enabled" not in load_state()["installations"]["examplehub"]


def test_a_refused_toggle_puts_the_row_back(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub.service import ProviderHubSettingsError, update_provider
    from provider_hub.state import load_state
    _seed({"examplehub": _installation(enabled=False, config={"region": "eu"})})
    calls = _enable_calls(monkeypatch, result=False)

    with pytest.raises(ProviderHubSettingsError):
        update_provider("examplehub", enabled=True, config={"region": "us"})

    assert calls == [("examplehub", True)]
    row = load_state()["installations"]["examplehub"]
    assert row["enabled"] is False
    assert row["config"] == {"region": "eu"}


def test_a_catalog_refresh_is_not_run_a_second_time_after_a_lost_race(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub import service
    from provider_hub.state_store import StateConflictError
    fetches = []

    def fetch(*args, **kwargs):
        fetches.append(1)
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(service, "_fetch_github_catalog", fetch)
    # The job rows the refresh writes commit on their own; the refresh itself is the one that stamps this.
    _lose_races(monkeypatch, 1, only_documents_with="last_attempted_at")

    with pytest.raises(StateConflictError):
        service.refresh_catalog()

    assert len(fetches) == 1


def test_a_cleanup_that_never_wins_its_commit_removes_nothing(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub import env_registry
    root = snapshot_file.parent
    environment = _real_environment(root, "fp0")
    _record_environment("fp0", environment)
    _lose_races(monkeypatch, 5)

    assert env_registry.cleanup_environments(root) == []

    assert environment.is_dir()


def test_a_refused_toggle_leaves_a_newer_write_alone(hub_store, hub_engine, snapshot_file, monkeypatch):
    from provider_hub import state_store
    from provider_hub.service import ProviderHubSettingsError, update_provider
    from provider_hub.state import load_state
    _seed({"examplehub": _installation(enabled=False, config={"region": "eu"})})

    def refuse_after_another_process_writes(provider_id, enabled):
        # Between this update's commit and the refusal, another process saved a new region.
        def step(text):
            document = json.loads(text)
            document["installations"]["examplehub"]["config"] = {"region": "ap"}
            return json.dumps(document, sort_keys=True), None

        state_store.update_document(step)
        return False

    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
                        refuse_after_another_process_writes)

    with pytest.raises(ProviderHubSettingsError):
        update_provider("examplehub", enabled=True, config={"region": "us"})

    row = load_state()["installations"]["examplehub"]
    assert row["config"] == {"region": "ap"}
    assert row["enabled"] is False


def test_a_refused_toggle_restores_only_the_config_keys_it_wrote(hub_store, hub_engine, snapshot_file, monkeypatch):
    """Update A saves a credential and enables the provider; update B saves only the region."""
    from provider_hub import state_store
    from provider_hub.service import ProviderHubSettingsError, update_provider
    from provider_hub.state import load_state
    _seed({"examplehub": _installation(enabled=False, config={"region": "eu"})})

    def refuse_after_another_process_writes(provider_id, enabled):
        def step(text):
            document = json.loads(text)
            document["installations"]["examplehub"]["config"]["region"] = "ap"
            return json.dumps(document, sort_keys=True), None

        state_store.update_document(step)
        return False

    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
                        refuse_after_another_process_writes)

    with pytest.raises(ProviderHubSettingsError):
        update_provider("examplehub", enabled=True, config={"api_key": "refused-credential"})

    row = load_state()["installations"]["examplehub"]
    assert row["config"] == {"region": "ap"}  # the region is B's; A's refused credential is gone
    assert row["enabled"] is False


# Environments --------------------------------------------------------------


def _real_environment(root, name):
    """A removable environment directory with an interpreter file, under shared_envs."""
    from provider_hub.venv import python_executable
    environment = root / "shared_envs" / name
    interpreter = python_executable(environment)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    return environment


def _record_environment(name, environment):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["plugin_envs"].update({name: {"path": str(environment)}}))


def _reference(environment, provider_id="examplehub"):
    from provider_hub.venv import python_executable
    return _installation(provider_id, python_path=str(python_executable(environment)))


def test_a_cleanup_that_loses_its_commit_removes_nothing(hub_store, hub_engine, snapshot_file, monkeypatch):
    """Another process commits a reference to the environment while a cleanup is deciding."""
    from provider_hub import env_registry, state_store
    from provider_hub.state import load_state
    root = snapshot_file.parent
    environment = _real_environment(root, "fp1")
    _record_environment("fp1", environment)
    original = env_registry._legacy_environment_dirs
    competing = []

    def sweep(root_dir):
        if not competing:
            def step(text):
                document = json.loads(text)
                document["installations"]["examplehub"] = _reference(environment)
                return json.dumps(document, sort_keys=True), None

            state_store.update_document(step)
            competing.append(True)
        return original(root_dir)

    monkeypatch.setattr(env_registry, "_legacy_environment_dirs", sweep)

    removed = env_registry.cleanup_environments(root)

    assert competing
    assert removed == []
    assert environment.is_dir()
    assert "fp1" in load_state()["plugin_envs"]


def test_a_cleanup_removes_an_unreferenced_environment_after_the_commit(hub_store, hub_engine, snapshot_file):
    from provider_hub import env_registry
    from provider_hub.state import load_state
    root = snapshot_file.parent
    environment = _real_environment(root, "fp2")
    _record_environment("fp2", environment)

    assert env_registry.cleanup_environments(root) == [str(environment)]

    assert not environment.exists()
    assert "fp2" not in load_state()["plugin_envs"]


def test_a_cleanup_waits_for_a_row_another_process_is_recording(hub_store, hub_engine, snapshot_file):
    from provider_hub import env_registry
    from provider_hub.state import mutate_state
    root = snapshot_file.parent
    environment = _real_environment(root, "fp3")
    _record_environment("fp3", environment)
    recording = threading.Event()
    commit_now = threading.Event()
    removed = []

    def other_process():
        with env_registry.recording_environment(environment):
            recording.set()
            commit_now.wait(30)
            mutate_state(lambda state: state["installations"].update(examplehub=_reference(environment)))

    writer = threading.Thread(target=other_process)
    writer.start()
    assert recording.wait(60)
    cleaner = threading.Thread(target=lambda: removed.append(env_registry.cleanup_environments(root)))
    cleaner.start()
    time.sleep(0.5)
    assert cleaner.is_alive()
    assert environment.is_dir()
    commit_now.set()
    writer.join(60)
    cleaner.join(60)

    assert removed == [[]]
    assert environment.is_dir()


def test_a_row_is_not_recorded_against_a_removed_environment(hub_store, hub_engine, snapshot_file):
    from provider_hub import env_registry
    root = snapshot_file.parent
    environment = _real_environment(root, "fp4")
    with env_registry.recording_environment(environment):
        pass
    for leftover in sorted(environment.rglob("*"), reverse=True):
        leftover.unlink() if leftover.is_file() else leftover.rmdir()
    environment.rmdir()

    with pytest.raises(env_registry.EnvironmentGone):
        with env_registry.recording_environment(environment):
            pytest.fail("the row would have been recorded")


# Legacy files with a root that is not an object -------------------------------


@pytest.mark.parametrize("content", ["[]", "null", '{"jobs": []}'])
def test_a_state_file_that_is_not_a_state_document_is_not_imported(hub_store, hub_engine, content):
    from provider_hub import state
    path = hub_store.legacy_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(state.StateCorruptError):
        state.load_state()
    with pytest.raises(state.StateCorruptError):
        state.sync_snapshot()

    assert raw_row(hub_engine) is None
    assert path.read_text(encoding="utf-8") == content
    # The damaged file is copied aside, which pauses the environment cleanup.
    assert [copy.read_text(encoding="utf-8") for copy in path.parent.glob("state.json.*.corrupt")] == [content]


DAMAGED_DOCUMENTS = ["[]", "null", "{}", '{"jobs": []}', '{"installations": []}', "{broken"]


def _damage_the_row(engine, text):
    with engine.connect() as connection:
        connection.execute(sa.text("UPDATE provider_hub_state SET document = :d WHERE id = 1"), {"d": text})


@pytest.mark.parametrize("damaged", DAMAGED_DOCUMENTS)
def test_a_mutation_refuses_a_damaged_document_and_changes_neither_store(
        hub_store, hub_engine, snapshot_file, damaged):
    from provider_hub import env_registry
    from provider_hub.state import StateCorruptError, corrupt_copies, mutate_state
    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))
    _damage_the_row(hub_engine, damaged)
    row_before = raw_row(hub_engine)
    copy_before = snapshot_file.read_bytes()

    with pytest.raises(StateCorruptError):
        mutate_state(lambda state: state["jobs"].append({"id": "later"}))

    assert raw_row(hub_engine) == row_before
    assert snapshot_file.read_bytes() == copy_before
    kept = corrupt_copies()
    assert [path.read_text(encoding="utf-8") for path in kept] == [damaged]
    # A kept copy pauses the cleanup, as on the file backend.
    with pytest.raises(env_registry.StateUnreadable):
        env_registry.validate_inventory({"installations": {}})


@pytest.mark.parametrize("damaged", DAMAGED_DOCUMENTS)
def test_the_strict_reader_refuses_a_damaged_document(hub_store, hub_engine, snapshot_file, damaged):
    from provider_hub.state import StateCorruptError, load_state_strict, mutate_state
    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))
    _damage_the_row(hub_engine, damaged)

    with pytest.raises(StateCorruptError):
        load_state_strict()


def test_recovering_a_damaged_document_keeps_a_copy_and_starts_from_the_default(hub_store, hub_engine, snapshot_file):
    from provider_hub.state import corrupt_copies, load_state, mutate_state
    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))
    _damage_the_row(hub_engine, '{"jobs": []}')

    mutate_state(lambda state: state["jobs"].append({"id": "after"}), recover_corrupt=True)

    assert [path.read_text(encoding="utf-8") for path in corrupt_copies()] == ['{"jobs": []}']
    assert load_state()["installations"] == {}
    assert [job["id"] for job in load_state()["jobs"]] == ["after"]


def test_the_first_start_still_mutates_an_empty_state(hub_store, hub_engine, snapshot_file):
    """No row and no file is a hub that has not written anything, not a damaged one."""
    from provider_hub.state import corrupt_copies, load_state_strict, mutate_state
    assert load_state_strict()["installations"] == {}

    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))

    assert list(json.loads(raw_row(hub_engine)[1])["installations"]) == ["examplehub"]
    assert corrupt_copies() == []


@pytest.mark.parametrize("damaged", DAMAGED_DOCUMENTS)
def test_syncing_the_copy_from_a_damaged_document_leaves_the_copy_alone(
        hub_store, hub_engine, snapshot_file, damaged):
    from provider_hub import env_registry
    from provider_hub.state import StateCorruptError, corrupt_copies, mutate_state, sync_snapshot
    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))
    _damage_the_row(hub_engine, damaged)
    copy_before = snapshot_file.read_bytes()
    row_before = raw_row(hub_engine)

    with pytest.raises(StateCorruptError):
        sync_snapshot()

    assert snapshot_file.read_bytes() == copy_before
    assert raw_row(hub_engine) == row_before
    assert [path.read_text(encoding="utf-8") for path in corrupt_copies()] == [damaged]
    with pytest.raises(env_registry.StateUnreadable):
        env_registry.validate_inventory({"installations": {}})


@pytest.mark.parametrize("damaged", DAMAGED_DOCUMENTS)
def test_the_boot_credential_migration_survives_a_damaged_document(
        hub_store, hub_engine, snapshot_file, monkeypatch, damaged):
    from provider_hub.state import corrupt_copies, mutate_state
    from secret_store import registry
    monkeypatch.setattr("secret_store.crypto.persist_master_key", lambda: None)
    saved = set(registry.plugin_secret_paths())
    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))
    _damage_the_row(hub_engine, damaged)
    copy_before = snapshot_file.read_bytes()
    row_before = raw_row(hub_engine)
    try:
        registry.migrate_installation_config_secrets()
    finally:
        registry.reset_plugin_secret_paths(saved)

    assert snapshot_file.read_bytes() == copy_before
    assert raw_row(hub_engine) == row_before
    assert [path.read_text(encoding="utf-8") for path in corrupt_copies()] == [damaged]


# First use of the table ----------------------------------------------------


def _create_losing_the_race(monkeypatch, hub_engine):
    """The table is created by another process right after this one found it missing."""
    from provider_hub import state_store
    real = state_store._state_table.create
    calls = []

    def create(bind, checkfirst=True):
        calls.append(1)
        real(bind, checkfirst=True)  # the other process, between the check and the CREATE
        raise sa.exc.ProgrammingError("CREATE TABLE provider_hub_state", {}, Exception("relation already exists"))

    monkeypatch.setattr(state_store._state_table, "create", create)
    return calls


def test_a_table_created_by_another_process_in_the_meantime_is_not_a_failure(hub_store, hub_engine, monkeypatch):
    from provider_hub.state import load_state, mutate_state
    calls = _create_losing_the_race(monkeypatch, hub_engine)

    mutate_state(lambda state: state["installations"].update(examplehub=_installation()))

    assert calls == [1]
    assert list(load_state()["installations"]) == ["examplehub"]
    assert raw_row(hub_engine)[0] == 2


def test_a_failed_table_creation_that_left_no_table_is_still_an_error(hub_store, hub_engine, monkeypatch):
    def create(bind, checkfirst=True):
        raise sa.exc.ProgrammingError("CREATE TABLE provider_hub_state", {}, Exception("permission denied"))

    monkeypatch.setattr(hub_store._state_table, "create", create)

    with pytest.raises(hub_store.StateStoreError) as failure:
        hub_store.ensure_ready()

    assert "permission denied" not in str(failure.value)


# Every driver call of the store -------------------------------------------


def _driver_failure():
    """A driver error whose text quotes a credential, as a constraint DETAIL or a connection error can."""
    return sa.exc.OperationalError("SELECT 1", {}, Exception(f"could not connect; row preview: {SECRET}"))


def _assert_nothing_leaks(failure, caplog, hub_store):
    assert isinstance(failure.value, hub_store.StateStoreError)
    assert failure.value.__cause__ is None and failure.value.__context__ is None
    assert SECRET not in str(failure.value)
    logging.getLogger("provider_hub.test").error("store failed", exc_info=failure.value)
    assert SECRET not in "".join(traceback.format_exception(failure.value))
    assert SECRET not in caplog.text


class _BrokenConnection:
    """A connection whose statements fail, or whose result cannot be fetched."""

    def __init__(self, fail_on):
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, *args, **kwargs):
        if self.fail_on == "execute":
            raise _driver_failure()
        return self

    def first(self):
        raise _driver_failure()


def _ready(hub_store, hub_engine):
    hub_store.ensure_ready(hub_engine)


def test_a_connection_that_fails_on_first_use_is_sanitized(hub_store, hub_engine, monkeypatch, caplog):
    """Creating the table fails and so does the check that follows it."""
    def down(*args, **kwargs):
        raise _driver_failure()

    monkeypatch.setattr(hub_engine, "connect", down)
    monkeypatch.setattr(hub_engine, "begin", down)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            hub_store.ensure_ready()
        _assert_nothing_leaks(failure, caplog, hub_store)


def test_a_failed_recheck_after_a_failed_creation_is_sanitized(hub_store, hub_engine, monkeypatch, caplog):
    def create(bind, checkfirst=True):
        raise _driver_failure()

    def inspect(bind):
        raise _driver_failure()

    monkeypatch.setattr(hub_store._state_table, "create", create)
    monkeypatch.setattr(hub_store.sa, "inspect", inspect)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            hub_store.ensure_ready()
        _assert_nothing_leaks(failure, caplog, hub_store)


@pytest.mark.parametrize("fail_on", ["connect", "execute", "fetch"])
def test_a_failed_read_is_sanitized(hub_store, hub_engine, monkeypatch, caplog, fail_on):
    _ready(hub_store, hub_engine)

    def down(*args, **kwargs):
        raise _driver_failure()

    monkeypatch.setattr(hub_engine, "connect", down if fail_on == "connect" else lambda: _BrokenConnection(fail_on))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            hub_store.read_document()
        _assert_nothing_leaks(failure, caplog, hub_store)


def test_a_failed_connect_for_a_write_is_sanitized(hub_store, hub_engine, monkeypatch, caplog):
    _ready(hub_store, hub_engine)
    real = hub_engine.connect
    calls = []

    def connect(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:  # the read works, the write transaction cannot connect
            raise _driver_failure()
        return real(*args, **kwargs)

    monkeypatch.setattr(hub_engine, "connect", connect)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            hub_store.update_document(lambda text: (text, None))
        _assert_nothing_leaks(failure, caplog, hub_store)


def test_a_failed_commit_statement_is_sanitized(hub_store, hub_engine, monkeypatch, caplog):
    _ready(hub_store, hub_engine)

    def commit(connection, revision, document):
        raise _driver_failure()

    monkeypatch.setattr(hub_store, "_commit_document", commit)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            hub_store.update_document(lambda text: (text, None))
        _assert_nothing_leaks(failure, caplog, hub_store)


def test_a_raw_driver_error_is_sanitized_too(hub_store, hub_engine, monkeypatch, caplog):
    """An error the driver raises that SQLAlchemy did not wrap."""
    import sqlite3
    _ready(hub_store, hub_engine)

    def commit(connection, revision, document):
        raise sqlite3.OperationalError(f"disk I/O error near {SECRET}")

    monkeypatch.setattr(hub_store, "_commit_document", commit)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as failure:
            hub_store.update_document(lambda text: (text, None))
        _assert_nothing_leaks(failure, caplog, hub_store)


def test_an_error_that_is_not_the_drivers_passes_through(hub_store, hub_engine):
    _ready(hub_store, hub_engine)

    def step(text):
        raise KeyError("not a database error")

    with pytest.raises(KeyError):
        hub_store.update_document(step)


# What SQLAlchemy logs itself ------------------------------------------------


class _Wrapped:
    """Forwards everything to the real driver object, except what the test arms."""

    def __init__(self, target, armed):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_armed", armed)

    def __getattr__(self, name):
        return getattr(self._target, name)

    def __setattr__(self, name, value):
        setattr(self._target, name, value)

    def __iter__(self):
        return iter(self._target)


class _WrappedCursor(_Wrapped):
    """A cursor that cannot be closed after its statement failed, which is when SQLAlchemy logs it."""

    def execute(self, *args, **kwargs):
        try:
            return self._target.execute(*args, **kwargs)
        except Exception as error:
            object.__setattr__(self, "_statement_failed", True)
            object.__setattr__(self, "_failure_text", str(error))
            raise

    def close(self):
        if self._armed.get("cursor") and getattr(self, "_statement_failed", False):
            raise RuntimeError(self._armed.get("text") or f"cursor close failed, row preview: {SECRET}"
                               + self._armed.get("extra", "") + " | " + getattr(self, "_failure_text", ""))
        self._target.close()


class _WrappedConnection(_Wrapped):
    def cursor(self, *args, **kwargs):
        return _WrappedCursor(self._target.cursor(*args, **kwargs), self._armed)

    def close(self):
        if self._armed.get("connection"):
            raise RuntimeError(self._armed.get("text") or f"connection close failed, row preview: {SECRET}")
        self._target.close()


@pytest.fixture
def breakable(hub_store, hub_engine, monkeypatch):
    """The engine's driver connections, each of which can be made to fail when closed."""
    armed = {}
    if hub_engine.dialect.name == "postgresql":
        import psycopg2.extensions as extensions

        class Cursor(extensions.cursor):
            def execute(self, *args, **kwargs):
                try:
                    return super().execute(*args, **kwargs)
                except Exception as error:
                    self._statement_failed = True
                    self._failure_text = str(error)
                    raise

            def close(self):
                if armed.get("cursor") and getattr(self, "_statement_failed", False):
                    raise RuntimeError(armed.get("text") or f"cursor close failed, row preview: {SECRET}"
                                       + armed.get("extra", "") + " | " + getattr(self, "_failure_text", ""))
                super().close()

        class Connection(extensions.connection):
            def close(self):
                if armed.get("connection"):
                    raise RuntimeError(armed.get("text") or f"connection close failed, row preview: {SECRET}")
                super().close()

        connect = hub_engine.dialect.connect

        def connect_with_failing_closes(*args, **kwargs):
            kwargs.update(connection_factory=Connection, cursor_factory=Cursor)
            return connect(*args, **kwargs)

        monkeypatch.setattr(hub_engine.dialect, "connect", connect_with_failing_closes)
    else:
        creator = hub_engine.pool._creator

        def wrapped(*args, **kwargs):
            return _WrappedConnection(creator(*args, **kwargs), armed)

        monkeypatch.setattr(hub_engine.pool, "_creator", wrapped)
    hub_engine.dispose()  # connections made from now on are the failing ones
    return armed


def _leaked(caplog, capfd):
    return SECRET in caplog.text or SECRET in capfd.readouterr().err


def test_a_cursor_that_cannot_be_closed_while_a_statement_fails_logs_no_credential(
        hub_store, hub_engine, breakable, caplog, capfd):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["jobs"].append({"id": "seed"}))
    with hub_engine.connect() as connection:
        length = connection.execute(sa.text("SELECT length(document) FROM provider_hub_state")).scalar()
        if hub_engine.dialect.name == "sqlite":
            connection.exec_driver_sql(
                "CREATE TRIGGER refuse BEFORE UPDATE ON provider_hub_state BEGIN "
                f"SELECT RAISE(ABORT, 'refused') WHERE length(NEW.document) > {length}; END")
        else:
            connection.exec_driver_sql(
                "ALTER TABLE provider_hub_state ADD CONSTRAINT refuse "
                f"CHECK (length(document) <= {length})")
    handler = logging.StreamHandler(sys.stderr)
    logging.getLogger().addHandler(handler)
    breakable["cursor"] = True
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(hub_store.StateStoreError):
                mutate_state(lambda state: state.update({"0credential": SECRET}))
    finally:
        logging.getLogger().removeHandler(handler)
        breakable["cursor"] = False

    # SQLAlchemy did log the failed close, with the credential masked and the rest of the detail kept.
    assert any(record.name.startswith("sqlalchemy") and record.exc_text and "cursor close failed" in record.exc_text
               and "***" in record.exc_text for record in caplog.records)
    assert not _leaked(caplog, capfd)


def test_a_connection_the_pool_cannot_close_logs_no_credential(hub_store, hub_engine, breakable, caplog, capfd):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state.update({"0credential": SECRET}))  # the store registers the value as it binds it
    handler = logging.StreamHandler(sys.stderr)
    logging.getLogger().addHandler(handler)
    breakable["connection"] = True
    try:
        with caplog.at_level(logging.DEBUG):
            mutate_state(lambda state: state["jobs"].append({"id": "again"}))
            hub_engine.dispose()  # a pooled connection is closed here; an unpooled one already was
    finally:
        logging.getLogger().removeHandler(handler)
        breakable["connection"] = False

    assert any(record.name.startswith("sqlalchemy") and record.exc_text and "connection close failed" in record.exc_text
               and "***" in record.exc_text for record in caplog.records)
    assert not _leaked(caplog, capfd)


def test_the_filter_masks_registered_values_and_keeps_every_other_detail():
    from provider_hub import state_store
    from secret_store import scrub
    scrub.register_secret_values(scrub.registered_secret_values() | {SECRET})
    try:
        raise RuntimeError(f"row preview {SECRET} and a lock timeout on another table")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("sqlalchemy.pool.impl.NullPool", logging.ERROR, __file__, 1,
                               "Exception closing connection %r", (f"conn {SECRET}",), exc_info)

    state_store._log_filter.filter(record)

    assert record.name == "sqlalchemy.pool.impl.NullPool"
    assert SECRET not in record.getMessage() and "Exception closing connection" in record.getMessage()
    assert SECRET not in record.exc_text
    assert "a lock timeout on another table" in record.exc_text and "RuntimeError" in record.exc_text
    assert record.exc_info is None
    info = logging.LogRecord("sqlalchemy.engine.Engine", logging.INFO, __file__, 1, "SELECT %s", ("1",), None)
    state_store._log_filter.filter(info)
    assert info.getMessage() == "SELECT 1"


def test_an_unrelated_database_error_keeps_its_driver_detail(hub_store, hub_engine, breakable, caplog):
    """A connection that will not close for a reason that has nothing to do with the state keeps its text."""
    from provider_hub.state import mutate_state
    breakable["text"] = "No space left on device while closing"
    breakable["connection"] = True
    try:
        with caplog.at_level(logging.DEBUG):
            mutate_state(lambda state: state["jobs"].append({"id": "ordinary"}))
            hub_engine.dispose()
    finally:
        breakable["connection"] = False

    sqlalchemy_records = [record for record in caplog.records if record.name.startswith("sqlalchemy")]
    assert any("No space left on device while closing" in record.exc_text for record in sqlalchemy_records
               if record.exc_text)
    assert "No space left on device while closing" in caplog.text
    assert SECRET not in caplog.text


def test_a_credential_in_an_installation_config_is_masked_in_what_the_store_logs(
        hub_store, hub_engine, breakable, caplog):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["installations"].update(examplehub=_installation(config={"api_key": SECRET})))
    breakable["connection"] = True
    try:
        with caplog.at_level(logging.DEBUG):
            mutate_state(lambda state: state["jobs"].append({"id": "again"}))
            hub_engine.dispose()
    finally:
        breakable["connection"] = False

    assert any(record.name.startswith("sqlalchemy") and record.exc_text for record in caplog.records)
    assert SECRET not in caplog.text


def _document_with_ordinary_values():
    return json.dumps({"installations": {"examplehub": _installation(config={
        "api_key": SECRET, "region": "europe-west", "host": "hub.example.test"})}}, sort_keys=True)


def test_only_declared_secrets_join_the_shared_scrub_set(hub_store):
    from secret_store import scrub
    hub_store.register_document_secrets(_document_with_ordinary_values())

    assert scrub.scrub_text("region europe-west on hub.example.test") == "region europe-west on hub.example.test"
    assert scrub.scrub_text(f"key {SECRET}") == "key ***"


def test_an_ordinary_config_value_is_masked_in_sqlalchemy_records_only(hub_store, caplog):
    sqlalchemy_logger = logging.getLogger("sqlalchemy.pool.impl.NullPool")
    hub_store._install_log_filter()
    hub_store.register_document_secrets(_document_with_ordinary_values())

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("provider_hub.service").warning("using the region europe-west on hub.example.test")
        try:
            raise RuntimeError("error while talking to hub.example.test for europe-west")
        except RuntimeError:
            sqlalchemy_logger.exception("Exception closing connection")

    app_lines = [record.getMessage() for record in caplog.records if record.name == "provider_hub.service"]
    assert app_lines == ["using the region europe-west on hub.example.test"]
    database_record = next(record for record in caplog.records if record.name == sqlalchemy_logger.name)
    assert "europe-west" not in database_record.exc_text and "hub.example.test" not in database_record.exc_text
    assert "***" in database_record.exc_text and "error while talking to" in database_record.exc_text


AWKWARD_CREDENTIALS = ['p"ss\\wrd', "ünïcödé-key", 'ü"n\\ï-mix']


def _every_form(value):
    from provider_hub.state_store import rendered_forms
    return rendered_forms(value) | {json.dumps(value), repr(value), ascii(value)}


def _refuse_larger_documents(hub_engine):
    with hub_engine.connect() as connection:
        length = connection.execute(sa.text("SELECT length(document) FROM provider_hub_state")).scalar()
        if hub_engine.dialect.name == "sqlite":
            connection.exec_driver_sql(
                "CREATE TRIGGER refuse BEFORE UPDATE ON provider_hub_state BEGIN "
                f"SELECT RAISE(ABORT, 'refused') WHERE length(NEW.document) > {length}; END")
        else:
            connection.exec_driver_sql(
                "ALTER TABLE provider_hub_state ADD CONSTRAINT refuse "
                f"CHECK (length(document) <= {length})")


@pytest.mark.parametrize("credential", AWKWARD_CREDENTIALS)
def test_every_rendered_form_of_a_credential_is_masked(hub_store, hub_engine, breakable, caplog, capfd, credential):
    """A failed statement whose cursor cannot be closed: the log gets the driver's own text, and more renderings."""
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["jobs"].append({"id": "seed"}))
    _refuse_larger_documents(hub_engine)
    breakable["cursor"] = True
    breakable["extra"] = " | " + " | ".join(sorted(_every_form(credential)))
    handler = logging.StreamHandler(sys.stderr)
    logging.getLogger().addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(hub_store.StateStoreError):
                mutate_state(lambda state: state.update({"0credential": credential}))
    finally:
        logging.getLogger().removeHandler(handler)
        breakable["cursor"] = False

    assert any(record.name.startswith("sqlalchemy") and record.exc_text and "cursor close failed" in record.exc_text
               for record in caplog.records)
    seen = caplog.text + capfd.readouterr().err
    for form in _every_form(credential):
        assert form not in seen, f"the form {form!r} reached the log"


def test_no_rendering_of_a_document_keeps_a_credential_after_masking(hub_store):
    """Render a whole document every way a log could, with its own code, and look for what must be gone."""
    credentials = {
        "api_key": "uniqueAAAA'apostrophe",
        "token": 'uniqueBBBB"quote',
        "password": "uniqueCCCC\\backslash",
        "secret": "uniqueDDDD-ünï-cödé",
        "mixed": "uniqueEEEE'\"\\ü",
    }
    document = {"installations": {"examplehub": _installation(config=dict(credentials))}, "jobs": []}
    hub_store.register_document_secrets(json.dumps(document, sort_keys=True))
    as_json = json.dumps(document, sort_keys=True)
    as_json_unicode = json.dumps(document, sort_keys=True, ensure_ascii=False)
    renderings = [
        str(document), repr(document), as_json, as_json_unicode,
        repr(as_json), repr(as_json_unicode), ascii(as_json_unicode), str(repr(repr(document))),
        f"Failing row contains (1, 1, 2, {as_json})", f"parameters: {[as_json]!r}",
    ]
    for rendering in renderings:
        masked = hub_store._mask_local(rendering)
        for name in credentials:
            core = credentials[name][:10]  # the distinctive start survives every spelling unless masked
            assert core not in masked, f"{name} survived in {rendering[:60]!r}"

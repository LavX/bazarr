"""Safety properties of the Plugin Hub state on the database.

Credentials must stay out of database error text, the state.json copy must end
as the last committed state whichever writer publishes last, and a write that
loses a race must not repeat anything outside the document.
"""
import contextlib
import json
import logging
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
            with pytest.raises(sa.exc.DBAPIError) as failure:
                state_store.ensure_ready()
            logging.getLogger("provider_hub.test").error("import failed", exc_info=failure.value)
    finally:
        state_store.reset()
        engine.dispose()
    rendered = "".join(traceback.format_exception(failure.value))
    assert SECRET not in str(failure.value)
    assert SECRET not in rendered
    assert SECRET not in caplog.text
    assert "SQL parameters hidden" in str(failure.value)


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
        state["jobs"].append({"id": "later", "note": SECRET})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(sa.exc.DBAPIError) as failure:
            mutate_state(add_credential)
        logging.getLogger("provider_hub.test").error("write failed", exc_info=failure.value)
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

    with pytest.raises(ValueError):
        state.load_state()
    with pytest.raises(ValueError):
        state.sync_snapshot()

    assert raw_row(hub_engine) is None
    assert path.read_text(encoding="utf-8") == content


DAMAGED_DOCUMENTS = ["[]", "null", '{"jobs": []}', '{"installations": []}', "{broken"]


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

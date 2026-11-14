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

hub_engine = engines.hub_engine
hub_store = engines.hub_store

SECRET = "plaintext-credential-9f3a"  # pragma: allowlist secret


@pytest.fixture
def snapshot_file(tmp_path, monkeypatch):
    from provider_hub import state
    path = tmp_path / "snapshot" / "state.json"
    monkeypatch.setattr(state, "state_file", lambda: path)
    return path


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

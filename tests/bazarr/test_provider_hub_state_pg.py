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

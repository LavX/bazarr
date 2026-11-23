"""Plugin releases: the model, the tables and the one activation function.

The release rules are exercised on a real database (SQLite, and PostgreSQL when BAZARR_PG_TEST_URL names a
server), because the partial unique index and the single transaction are part of what is promised.
"""
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

import provider_hub_state_engines as engines
from plugin_platform_helpers import SERVICE_KIND, service_manifest
from provider_hub_state_engines import raw_row
from test_provider_hub import _manifest

hub_engine = engines.hub_engine
hub_store = engines.hub_store


# Where the files of the rows below live. The tests that touch the disk point this at a folder of their own.
_HUB = {"root": None}


def _bundle(plugin_id, version):
    path = f"{_HUB['root'] or '/hub'}/bundles/{plugin_id}/{version}"
    if _HUB["root"]:
        Path(path).mkdir(parents=True, exist_ok=True)
    return path


def _python(plugin_id, version=None):
    path = f"{_HUB['root'] or '/hub'}/envs/{plugin_id}/{version or 'shared'}/bin/python"
    if _HUB["root"]:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _files_under_tmp(tmp_path):
    _HUB["root"] = str(tmp_path / "provider_hub")
    yield
    _HUB["root"] = None


def _release_manifest(provider_id, version):
    return _manifest(provider_id=provider_id, version=version)


def _row(provider_id, *, active=None, staged=None, **extra):
    """An installation row the way the Hub writes one, with the release fields of its slots."""
    row = {
        "provider_id": provider_id, "kind": "subtitle_provider", "name": provider_id.title(),
        "state": "active", "pending_restart": False, "installed_at": "2026-11-01T00:00:00Z",
        "last_error": None, "trusted": True, "origin": "catalog", "source_id": "official", "source_bound": True,
        "enabled": True, "config": {},
        "active_version": None, "active_path": None, "python_path": None, "activated_at": None,
        "staged_version": None, "staged_path": None, "staged_python_path": None, "staged_manifest": None,
    }
    if active:
        version, supplied = active if isinstance(active, tuple) else (active, None)
        row.update(active_version=version, active_path=_bundle(provider_id, version),
                   python_path=_python(provider_id, version),
                   manifest=_release_manifest(provider_id, version), activated_at="2026-11-02T00:00:00Z")
        if supplied:
            row["supplied_by"] = supplied
    if staged:
        version, supplied = staged if isinstance(staged, tuple) else (staged, None)
        row.update(staged_version=version, staged_path=_bundle(provider_id, version),
                   staged_python_path=_python(provider_id, version),
                   staged_manifest=_release_manifest(provider_id, version), state="staged", pending_restart=True)
        if supplied:
            row["staged_supplied_by"] = supplied
    row.update(extra)
    return row


# ---- the release model on its own tables --------------------------------------------------------------------


@pytest.fixture
def table():
    """An empty release table on a SQLite database, and a function that stores a document the way the store does."""
    from provider_hub import releases

    engine = sa.create_engine("sqlite://", poolclass=StaticPool)
    releases.create_tables(engine)
    conn = engine.connect()

    class Table:
        def write(self, data):
            rows = releases.read_rows(conn, manifests=releases.RELEASE_STATES)
            stored, plan = releases.split(data, rows)
            releases.apply_plan(conn, plan)
            conn.commit()
            return stored

        def read(self, stored):
            rows = releases.read_rows(conn, states=releases.SLOT_STATES)
            return releases.compose(json.loads(json.dumps(stored)), rows)

        def releases(self, plugin_id=None):
            return sorted((item.version, item.state) for item in releases_of(conn, plugin_id))

        def ids(self, plugin_id):
            return {item.version: item.id for item in releases_of(conn, plugin_id)}

        def execute(self, statement):
            result = conn.execute(statement)
            conn.commit()
            return result

        connection = None

    def releases_of(conn, plugin_id):
        return [releases.release_of(row) for row in releases.read_rows(
            conn, plugin_id=plugin_id, manifests=releases.RELEASE_STATES)]

    handle = Table()
    handle.connection = conn
    try:
        yield handle
    finally:
        conn.close()
        engine.dispose()


def _document(**rows):
    return {"installations": {key: value for key, value in rows.items()}, "jobs": [], "plugin_envs": {}}


def test_the_release_fields_leave_the_stored_row_and_come_back_whole(table):
    row = _row("alpha", active=("1.0.0", {"cap.one": "beta"}), staged=("1.1.0", {"cap.one": "beta"}),
               config={"region": "eu"})
    document = _document(alpha=row)

    stored = table.write(document)

    kept = stored["installations"]["alpha"]
    assert kept["config"] == {"region": "eu"} and kept["enabled"] is True
    for key in ("active_version", "active_path", "python_path", "manifest", "supplied_by", "activated_at",
                "staged_version", "staged_path", "staged_python_path", "staged_manifest", "staged_supplied_by"):
        assert key not in kept
    assert table.read(stored)["installations"]["alpha"] == row
    assert table.releases("alpha") == [("1.0.0", "active"), ("1.1.0", "staged")]


def test_the_digest_and_the_bindings_are_stored_per_release(table):
    from provider_hub import releases

    row = _row("alpha", active=("1.0.0", {"cap.one": "beta", "cap.two": "gamma"}))
    table.write(_document(alpha=row))

    [release] = [item for item in table.connection.execute(sa.select(releases.releases_table))]
    assert release.bundle_digest == row["manifest"]["bundle_sha256"]
    stored = {(item.capability, item.supplier_id) for item in table.connection.execute(
        sa.select(releases.bindings_table))}
    assert stored == {("cap.one", "beta"), ("cap.two", "gamma")}


def test_a_staged_release_keeps_its_identity_when_it_becomes_the_active_one(table):
    document = _document(alpha=_row("alpha", active="1.0.0", staged="1.1.0"))
    table.write(document)
    ids = table.ids("alpha")

    table.write(_document(alpha=_row("alpha", active="1.1.0")))

    assert table.ids("alpha")["1.1.0"] == ids["1.1.0"]
    assert table.releases("alpha") == [("1.0.0", "retired"), ("1.1.0", "active")]


def test_only_the_previous_release_is_kept(table):
    for version in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
        table.write(_document(alpha=_row("alpha", active=version)))

    assert table.releases("alpha") == [("1.2.0", "retired"), ("1.3.0", "active")]


def _failed(row, reason):
    from provider_hub import releases

    row[releases.FAIL_STAGED] = reason
    row["last_error"] = reason
    return row


def test_a_staged_release_that_failed_to_activate_is_kept_as_failed_and_any_other_is_forgotten(table):
    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))

    table.write(_document(alpha=_failed(_row("alpha", active="1.0.0"), "it did not start")))
    assert table.releases("alpha") == [("1.0.0", "active"), ("1.1.0", "failed")]

    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.2.0")))
    table.write(_document(alpha=_row("alpha", active="1.0.0")))
    assert table.releases("alpha") == [("1.0.0", "active"), ("1.1.0", "failed")]


def test_a_healthy_staged_release_is_not_blamed_for_the_error_of_the_one_that_replaced_it(table):
    """An error on the row is not the release's: only a release that failed to activate is recorded as failed."""
    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))

    table.write(_document(alpha=_row("alpha", active="1.0.0", last_error="v1.2.0 could not be installed")))

    assert table.releases("alpha") == [("1.0.0", "active")]


def test_only_the_last_failure_is_kept(table):
    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))
    table.write(_document(alpha=_failed(_row("alpha", active="1.0.0"), "first")))
    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.2.0")))
    table.write(_document(alpha=_failed(_row("alpha", active="1.0.0"), "second")))

    [(version, state)] = [item for item in table.releases("alpha") if item[1] == "failed"]
    assert version == "1.2.0"


def test_the_previous_release_that_fails_to_come_back_stays_the_previous_release(table):
    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="2.0.0")))
    table.write(_document(alpha=_row("alpha", active="2.0.0")))
    table.write(_document(alpha=_row("alpha", active="2.0.0", staged="1.0.0")))

    table.write(_document(alpha=_failed(_row("alpha", active="2.0.0"), "it did not start")))

    assert table.releases("alpha") == [("1.0.0", "retired"), ("2.0.0", "active")]


def test_the_empty_bindings_and_the_empty_manifest_of_a_row_come_back_as_they_were(table):
    row = _row("alpha", active="1.0.0", staged="1.1.0", supplied_by={}, staged_supplied_by={})
    row["manifest"] = None
    absent = _row("beta", active="1.0.0")
    document = _document(alpha=row, beta=absent)

    stored = table.write(document)

    assert table.read(stored)["installations"] == document["installations"]
    assert "supplied_by" not in table.read(stored)["installations"]["beta"]
    assert table.read(stored)["installations"]["alpha"]["supplied_by"] == {}


def test_a_release_replaced_while_it_serves_calls_drains_and_the_mark_is_consumed(table):
    from provider_hub import releases

    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))
    promoted = _row("alpha", active="1.1.0")
    promoted[releases.DRAIN_PREVIOUS] = True
    document = _document(alpha=promoted)

    stored = table.write(document)

    assert table.releases("alpha") == [("1.0.0", "draining"), ("1.1.0", "active")]
    assert releases.DRAIN_PREVIOUS not in stored["installations"]["alpha"]
    assert releases.DRAIN_PREVIOUS not in document["installations"]["alpha"]


def test_a_draining_release_stays_put_until_it_is_retired_on_purpose(table):
    from provider_hub import releases

    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))
    promoted = _row("alpha", active="1.1.0")
    promoted[releases.DRAIN_PREVIOUS] = True
    table.write(_document(alpha=promoted))

    table.write(_document(alpha=_row("alpha", active="1.1.0", config={"changed": True})))

    assert table.releases("alpha") == [("1.0.0", "draining"), ("1.1.0", "active")]


def test_a_retired_release_comes_back_as_the_active_one_with_its_identity(table):
    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))
    table.write(_document(alpha=_row("alpha", active="1.1.0")))
    ids = table.ids("alpha")

    table.write(_document(alpha=_row("alpha", active="1.0.0")))

    assert table.ids("alpha")["1.0.0"] == ids["1.0.0"]
    assert table.releases("alpha") == [("1.0.0", "active"), ("1.1.0", "retired")]


def test_a_plugin_that_leaves_the_document_takes_its_releases_and_bindings_with_it(table):
    from provider_hub import releases

    table.write(_document(alpha=_row("alpha", active=("1.0.0", {"cap.one": "beta"})),
                          beta=_row("beta", active="2.0.0")))

    table.write(_document(beta=_row("beta", active="2.0.0")))

    assert table.releases("alpha") == []
    assert table.releases("beta") == [("2.0.0", "active")]
    assert table.connection.execute(sa.select(sa.func.count()).select_from(releases.bindings_table)).scalar() == 0


def test_a_row_in_an_odd_shape_keeps_its_fields_in_the_document(table):
    odd = _row("alpha", active="1.0.0")
    odd["python_path"] = 5
    failed = {"provider_id": "zeta", "state": "failed", "manifest": _release_manifest("zeta", "1.0.0"),
              "active_version": None, "last_error": "no"}
    document = _document(alpha=odd, zeta=failed)

    stored = table.write(document)

    assert stored["installations"]["alpha"]["active_version"] == "1.0.0"
    assert stored["installations"]["alpha"]["python_path"] == 5
    assert stored["installations"]["zeta"] == failed
    assert table.releases() == []
    assert table.read(stored) == document


def test_two_active_releases_of_one_plugin_cannot_be_stored(table):
    from provider_hub import releases

    table.write(_document(alpha=_row("alpha", active="1.0.0")))
    now = releases.now_text()

    with pytest.raises(sa.exc.IntegrityError):
        table.execute(sa.insert(releases.releases_table).values(
            plugin_id="alpha", version="2.0.0", state="active", created_at=now))


def test_a_state_the_model_does_not_know_cannot_be_stored(table):
    from provider_hub import releases

    with pytest.raises(sa.exc.IntegrityError):
        table.execute(sa.insert(releases.releases_table).values(
            plugin_id="alpha", version="1", state="paused", created_at=releases.now_text()))


@pytest.mark.parametrize("current, target, allowed", [
    ("staged", "active", True), ("staged", "failed", True), ("active", "draining", True),
    ("active", "retired", True), ("draining", "retired", True), ("retired", "active", True),
    ("failed", "active", False), ("retired", "draining", False), ("staged", "draining", False),
])
def test_the_model_says_which_moves_a_release_may_make(current, target, allowed):
    from provider_hub import releases

    assert releases.can_move(current, target) is allowed


def test_an_explicit_move_changes_the_state_and_refuses_one_the_model_forbids(table):
    from provider_hub import releases

    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))
    ids = table.ids("alpha")

    with pytest.raises(releases.ReleaseTransitionError):
        releases.set_state(table.connection, ids["1.1.0"], releases.DRAINING)
    assert releases.set_state(table.connection, ids["1.0.0"], releases.DRAINING) is True
    assert releases.set_state(table.connection, ids["1.0.0"], releases.DRAINING) is False
    assert releases.set_state(table.connection, 999, releases.RETIRED) is False
    assert releases.set_state(table.connection, ids["1.0.0"], releases.RETIRED) is True
    assert table.releases("alpha") == [("1.0.0", "retired"), ("1.1.0", "staged")]


def test_activating_through_the_model_sends_the_one_in_the_slot_away(table):
    from provider_hub import releases

    table.write(_document(alpha=_row("alpha", active="1.0.0", staged="1.1.0")))
    ids = table.ids("alpha")

    releases.set_state(table.connection, ids["1.1.0"], releases.ACTIVE)

    assert table.releases("alpha") == [("1.0.0", "retired"), ("1.1.0", "active")]


# ---- the store: the document and the releases in one transaction --------------------------------------------


@pytest.fixture
def hub(hub_store, hub_engine, tmp_path, monkeypatch):
    """The database state with the hub directory and state.json under tmp_path."""
    from provider_hub import state
    from secret_store import registry
    copy = tmp_path / "provider_hub" / "state.json"
    monkeypatch.setattr(state, "state_file", lambda: copy)
    saved = set(registry.plugin_secret_paths())
    registry.reset_plugin_secret_paths()
    state.load_state()
    try:
        yield hub_engine
    finally:
        registry.reset_plugin_secret_paths(saved)


def _seed(**rows):
    from provider_hub.state import load_state, save_state
    state = load_state()
    state["installations"].update(rows)
    save_state(state)


def _stored(engine):
    return json.loads(raw_row(engine)[1])


def _releases_in(engine, plugin_id=None):
    from provider_hub import state_store
    return sorted((item.plugin_id, item.version, item.state)
                  for item in state_store.list_releases(plugin_id, bind=engine))


def _rich_rows():
    """Every shape of row the upgrade has to carry: active, staged, removed, revoked, blocked, failed and odd ones."""
    revoked = _row("delta", active="1.0.0", revoked=True, revoked_reason="withdrawn", last_error="withdrawn",
                   pending_restart=True, revoked_restart=True)
    blocked = _row("epsilon", active=("2.0.0", {"cap.one": "alpha"}), blocked=True,
                   blocked_reason="It needs alpha, which is not running.", blocked_cause="needs")
    removed = _row("gamma", active="1.0.0", state="removed", pending_restart=True)
    first = _row("theta", staged="1.0.0", state="staged")
    failed = {"provider_id": "zeta", "kind": "subtitle_provider", "state": "failed", "active_version": None,
              "staged_version": None, "manifest": _release_manifest("zeta", "1.0.0"), "last_error": "it did not start",
              "enabled": True, "config": {}, "pending_restart": False}
    odd = _row("eta", active="1.0.0")
    odd["python_path"] = 5
    return {
        "alpha": _row("alpha", active=("1.0.0", {"cap.one": "beta"}), staged=("1.1.0", {"cap.one": "beta"}),
                      config={"region": "eu"}),
        "beta": _row("beta", active="3.0.0"),
        "gamma": removed, "delta": revoked, "epsilon": blocked, "zeta": failed, "eta": odd, "theta": first,
    }


def test_every_kind_of_row_reads_back_as_it_was_written(hub):
    from provider_hub.state import load_state

    rows = _rich_rows()
    _seed(**rows)

    state = load_state()["installations"]
    for plugin_id, row in rows.items():
        assert state[plugin_id] == row, plugin_id


def test_the_stored_document_holds_no_release_fields_and_the_tables_hold_them(hub):
    _seed(**_rich_rows())

    stored = _stored(hub)["installations"]

    assert "active_version" not in stored["alpha"] and "manifest" not in stored["alpha"]
    assert stored["alpha"]["config"] == {"region": "eu"}
    assert stored["delta"]["revoked"] is True and "active_version" not in stored["delta"]
    assert stored["epsilon"]["blocked_cause"] == "needs"
    # A row that is not a well formed release is left in the document as it is.
    assert stored["eta"]["active_version"] == "1.0.0" and stored["eta"]["python_path"] == 5
    assert stored["zeta"]["manifest"]["provider_id"] == "zeta"
    assert sorted(_releases_in(hub)) == sorted([
        ("alpha", "1.0.0", "active"), ("alpha", "1.1.0", "staged"), ("beta", "3.0.0", "active"),
        ("gamma", "1.0.0", "active"), ("delta", "1.0.0", "active"), ("epsilon", "2.0.0", "active"),
        ("theta", "1.0.0", "staged")])


def test_the_state_json_copy_is_the_whole_state(hub, tmp_path):
    from provider_hub.state import load_state_strict

    _seed(**_rich_rows())

    copy = json.loads((tmp_path / "provider_hub" / "state.json").read_text(encoding="utf-8"))
    assert copy == load_state_strict()
    assert copy["installations"]["alpha"]["staged_version"] == "1.1.0"
    assert copy["installations"]["alpha"]["manifest"]["provider_id"] == "alpha"


def test_a_write_changes_the_document_and_the_releases_together_or_not_at_all(hub):
    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active="1.0.0", staged="1.1.0"))
    before = (raw_row(hub), _releases_in(hub))

    def broken(state):
        row = state["installations"]["alpha"]
        row.update(active_version="1.1.0", staged_version=None, staged_path=None, staged_python_path=None,
                   staged_manifest=None, state="active", pending_restart=False)
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        mutate_state(broken)

    assert (raw_row(hub), _releases_in(hub)) == before
    assert load_state()["installations"]["alpha"]["staged_version"] == "1.1.0"


def test_a_write_that_loses_a_race_works_out_its_release_changes_again(hub):
    from provider_hub import state_store
    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active="1.0.0"))
    runs = []

    def promote(state):
        runs.append(1)
        if len(runs) == 1:
            # Another writer retires the active release between this write's read and its commit.
            for item in state_store.list_releases("alpha", bind=hub):
                state_store.transition_release(item.id, "retired", bind=hub)
        state["installations"]["beta"] = _row("beta", active="2.0.0")

    mutate_state(promote)

    assert len(runs) == 2
    assert load_state()["installations"]["beta"]["active_version"] == "2.0.0"
    # The second run read the retired release and, with no slot naming it, left it retired.
    assert ("alpha", "1.0.0", "retired") in _releases_in(hub)


def test_a_document_written_before_the_tables_is_read_whole_and_moved_at_its_next_write(hub):
    from provider_hub.state import load_state, mutate_state

    old = _row("alpha", active="1.0.0", staged="1.1.0")
    with hub.begin() as connection:
        document = json.loads(raw_row(hub)[1])
        document["installations"]["alpha"] = old
        connection.execute(sa.text("UPDATE provider_hub_state SET document = :document WHERE id = 1"),
                           {"document": json.dumps(document)})
    assert load_state()["installations"]["alpha"] == old
    assert _releases_in(hub) == []

    mutate_state(lambda state: state.setdefault("jobs", []).append({"id": "j"}))

    assert load_state()["installations"]["alpha"] == old
    assert "active_version" not in _stored(hub)["installations"]["alpha"]
    assert _releases_in(hub) == [("alpha", "1.0.0", "active"), ("alpha", "1.1.0", "staged")]


# ---- activation ---------------------------------------------------------------------------------------------


@pytest.fixture
def staged(hub, monkeypatch):
    """What a restart finds: ``alpha`` active at 1.0.0 with 1.1.0 staged, the worker checks stubbed to pass."""
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    _seed(alpha=_row("alpha", active=("1.0.0", {}), staged="1.1.0"))
    return hub


def test_the_boot_makes_the_staged_release_active_and_keeps_the_previous_one(staged):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    from provider_hub import state_store
    ids_before = {item.version: item.id for item in state_store.list_releases("alpha", bind=staged)}

    assert activate_staged_installations() == ["alpha"]

    row = load_state()["installations"]["alpha"]
    assert (row["state"], row["active_version"], row["pending_restart"], row["staged_version"]) == (
        "active", "1.1.0", False, None)
    assert row["manifest"]["version"] == "1.1.0" and row["last_error"] is None
    assert _releases_in(staged) == [("alpha", "1.0.0", "retired"), ("alpha", "1.1.0", "active")]
    assert {item.version: item.id for item in state_store.list_releases("alpha", bind=staged)} == ids_before


def test_a_release_that_fails_its_check_is_dropped_and_the_active_one_stays(staged, monkeypatch):
    from provider_hub.service import activate_release, list_jobs
    from provider_hub.state import load_state

    def broken(manifest, bundle_path, python_path):
        raise RuntimeError("the plugin did not start")

    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", broken)

    result = activate_release("alpha", at_boot=True)

    assert (result.outcome, result.error) == ("failed", "the plugin did not start")
    row = load_state()["installations"]["alpha"]
    assert (row["state"], row["active_version"], row["staged_version"]) == ("active", "1.0.0", None)
    assert row["last_error"] == "the plugin did not start"
    assert _releases_in(staged) == [("alpha", "1.0.0", "active"), ("alpha", "1.1.0", "failed")]
    assert any(job["action"] == "activate" and job["state"] == "failed" for job in list_jobs())


def test_a_first_install_that_fails_its_check_is_a_failed_installation_with_a_failed_release(hub, monkeypatch):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))

    def broken(manifest, bundle_path, python_path):
        raise RuntimeError("nothing to start")

    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", broken)
    _seed(theta=_row("theta", staged="1.0.0", manifest=_release_manifest("theta", "1.0.0")))

    assert activate_staged_installations() == []

    row = load_state()["installations"]["theta"]
    assert (row["state"], row["active_version"], row["last_error"]) == ("failed", None, "nothing to start")
    assert _releases_in(hub) == [("theta", "1.0.0", "failed")]


def test_the_boot_and_a_running_process_activate_through_the_same_function(staged, monkeypatch):
    from provider_hub import service

    calls = []
    real = service.activate_release

    def spy(provider_id, *, at_boot=False):
        calls.append((provider_id, at_boot))
        return real(provider_id, at_boot=at_boot)

    monkeypatch.setattr(service, "activate_release", spy)

    assert service.activate_staged_installations() == ["alpha"]

    assert calls == [("alpha", True)]


def test_a_kind_that_attaches_on_restart_is_not_activated_while_running(staged):
    from provider_hub.service import activate_release
    from provider_hub.state import load_state

    result = activate_release("alpha")

    assert (result.outcome, result.version, result.previous_version) == ("waiting", "1.1.0", "1.0.0")
    row = load_state()["installations"]["alpha"]
    assert (row["state"], row["staged_version"], row["pending_restart"]) == ("staged", "1.1.0", True)
    assert _releases_in(staged) == [("alpha", "1.0.0", "active"), ("alpha", "1.1.0", "staged")]


def _service_row(version, **extra):
    manifest = service_manifest("exampleservice", version)
    row = _row("exampleservice", state="staged", pending_restart=True, kind=SERVICE_KIND, staged_version=version,
               staged_path=_bundle("exampleservice", version),
               staged_python_path=_python("exampleservice"), staged_manifest=manifest, manifest=manifest)
    row.update(extra)
    return row


def test_a_kind_that_attaches_live_is_activated_now_and_the_release_it_replaces_drains(hub, service_kind, monkeypatch):
    from provider_hub.service import activate_release, retire_release
    from provider_hub.state import load_state

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    old = service_manifest("exampleservice", "1.0.0")
    _seed(exampleservice=_service_row(
        "1.1.0", active_version="1.0.0", active_path=_bundle("exampleservice", "1.0.0"),
        python_path=_python("exampleservice"), manifest=old))

    result = activate_release("exampleservice")

    assert result.activated and result.draining and (result.previous_version, result.version) == ("1.0.0", "1.1.0")
    row = load_state()["installations"]["exampleservice"]
    assert (row["state"], row["active_version"], row["pending_restart"]) == ("active", "1.1.0", False)
    assert "drain_previous" not in row
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "draining"), ("exampleservice", "1.1.0", "active")]

    assert retire_release("exampleservice", "1.0.0") == 1
    assert retire_release("exampleservice") == 0
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "retired"), ("exampleservice", "1.1.0", "active")]


@pytest.fixture
def stopping_kind(service_kind):
    """The service kind, made one that stops a release live: its detach is recorded and ends when ``release`` is set."""
    import threading

    from provider_hub.kinds import DETACH_LIVE

    service_kind.detach_policy = DETACH_LIVE
    service_kind.detached = []
    service_kind.release = threading.Event()
    service_kind.fail = False

    def detach(plugin_id, *, withdraw, grace=None, release_key=None):
        if service_kind.fail:
            raise RuntimeError("a pool would not let go")
        service_kind.detached.append((plugin_id, withdraw, release_key))
        drain = threading.Thread(target=service_kind.release.wait, args=(20,), daemon=True)
        drain.start()
        return [drain]

    service_kind.detach = detach
    return service_kind


def _live_update(hub, monkeypatch):
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    _seed(exampleservice=_service_row(
        "1.1.0", active_version="1.0.0", active_path=_bundle("exampleservice", "1.0.0"),
        python_path=_python("exampleservice"), manifest=service_manifest("exampleservice", "1.0.0")))


def test_a_replaced_release_drains_through_the_kind_and_retires_when_its_calls_are_done(
        hub, stopping_kind, monkeypatch):
    from provider_hub.service import activate_release

    _live_update(hub, monkeypatch)

    result = activate_release("exampleservice")

    # The old release is draining while the kind still holds its calls; its key is what the kind is told to stop.
    assert result.activated and result.draining and result.drained is not None
    assert stopping_kind.detached == [("exampleservice", False, service_manifest("exampleservice", "1.0.0")[
        "bundle_sha256"])]
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "draining"), ("exampleservice", "1.1.0", "active")]

    stopping_kind.release.set()
    result.drained.join(20)

    assert _releases_in(hub) == [("exampleservice", "1.0.0", "retired"), ("exampleservice", "1.1.0", "active")]


def test_a_replaced_release_whose_detach_failed_stays_draining_until_the_next_start_retires_it(
        hub, stopping_kind, monkeypatch):
    from provider_hub.service import activate_release, activate_staged_installations

    _live_update(hub, monkeypatch)
    stopping_kind.fail = True

    result = activate_release("exampleservice")

    assert result.activated and result.draining and result.drained is None
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "draining"), ("exampleservice", "1.1.0", "active")]
    activate_staged_installations()
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "retired"), ("exampleservice", "1.1.0", "active")]


def test_a_second_live_activation_retires_only_the_release_it_replaced(hub, stopping_kind, monkeypatch):
    import threading

    from provider_hub.service import activate_release
    from provider_hub.state import mutate_state

    _live_update(hub, monkeypatch)
    drains = []

    def detach(plugin_id, *, withdraw, grace=None, release_key=None):
        event = threading.Event()
        drains.append(event)
        thread = threading.Thread(target=event.wait, args=(20,), daemon=True)
        thread.start()
        return [thread]

    stopping_kind.detach = detach
    first = activate_release("exampleservice")
    assert (first.previous_version, first.version) == ("1.0.0", "1.1.0")

    # Another release is staged and activated while the first one's calls are still running.
    manifest = service_manifest("exampleservice", "1.2.0")

    def stage(state):
        state["installations"]["exampleservice"].update(
            state="staged", pending_restart=True, staged_version="1.2.0", staged_manifest=manifest,
            staged_path=_bundle("exampleservice", "1.2.0"), staged_python_path=_python("exampleservice"))

    mutate_state(stage)
    second = activate_release("exampleservice")
    assert (second.previous_version, second.version) == ("1.1.0", "1.2.0")
    drains[1].set()
    second.drained.join(20)

    # 1.1.0 retired with its calls done; 1.0.0 is still draining because its calls are not.
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "draining"), ("exampleservice", "1.1.0", "retired"),
                                 ("exampleservice", "1.2.0", "active")]
    drains[0].set()
    first.drained.join(20)
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "retired"), ("exampleservice", "1.1.0", "retired"),
                                 ("exampleservice", "1.2.0", "active")]


def test_the_boot_drains_nothing_because_nothing_is_running_yet(hub, stopping_kind, monkeypatch):
    from provider_hub.service import activate_release

    _live_update(hub, monkeypatch)

    result = activate_release("exampleservice", at_boot=True)

    assert result.activated and not result.draining and result.drained is None and stopping_kind.detached == []


def test_a_boot_finds_no_release_still_draining(hub, service_kind, monkeypatch):
    from provider_hub.service import activate_release, activate_staged_installations

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    _seed(exampleservice=_service_row(
        "1.1.0", active_version="1.0.0", active_path=_bundle("exampleservice", "1.0.0"),
        python_path=_python("exampleservice"), manifest=service_manifest("exampleservice", "1.0.0")))
    activate_release("exampleservice")

    activate_staged_installations()

    assert _releases_in(hub) == [("exampleservice", "1.0.0", "retired"), ("exampleservice", "1.1.0", "active")]


def test_nothing_is_activated_for_a_plugin_with_nothing_staged(staged):
    from provider_hub.service import activate_release

    assert activate_release("alpha", at_boot=True).activated
    assert activate_release("alpha", at_boot=True).outcome == "skipped"
    assert activate_release("nobody", at_boot=True).outcome == "skipped"


def test_a_release_replaced_while_it_was_being_checked_is_left_to_its_replacement(staged, monkeypatch):
    from provider_hub.service import activate_release
    from provider_hub.state import load_state, mutate_state

    def restage(manifest, bundle_path, python_path):
        # Someone stages another version while this one is being checked.
        def change(state):
            row = state["installations"]["alpha"]
            row.update(staged_version="1.2.0", staged_path=_bundle("alpha", "1.2.0"),
                       staged_manifest=_release_manifest("alpha", "1.2.0"))

        mutate_state(change)

    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", restage)

    result = activate_release("alpha", at_boot=True)

    assert result.outcome == "skipped"
    row = load_state()["installations"]["alpha"]
    assert (row["active_version"], row["staged_version"]) == ("1.0.0", "1.2.0")


def test_a_removal_the_restart_finishes_takes_the_releases_with_it(hub):
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    _seed(gamma=_row("gamma", active="1.0.0", state="removed", pending_restart=True),
          beta=_row("beta", active="3.0.0"))

    activate_staged_installations()

    assert "gamma" not in load_state()["installations"]
    assert _releases_in(hub) == [("beta", "3.0.0", "active")]


def test_the_held_back_plugin_and_its_reason_survive_an_activation(staged):
    """A plugin the boot holds back for what it relies on keeps its row marks through the activation write."""
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    _seed(epsilon=_row("epsilon", active=("2.0.0", {"cap.one": "alpha"})))

    activate_staged_installations()

    row = load_state()["installations"]["epsilon"]
    assert row["active_version"] == "2.0.0" and row["supplied_by"] == {"cap.one": "alpha"}


def test_a_failure_in_the_middle_of_a_write_leaves_the_document_the_releases_and_the_bindings_as_they_were(
        hub, monkeypatch):
    from provider_hub import releases
    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active=("1.0.0", {"cap.one": "beta"}), staged=("1.1.0", {"cap.one": "beta"})))
    before = (raw_row(hub), _releases_in(hub), _bindings_in(hub))
    real = releases._write_bindings
    calls = []

    def fail_after_the_first(connection, release_id, bindings):
        calls.append(release_id)
        real(connection, release_id, bindings)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(releases, "_write_bindings", fail_after_the_first)

    def promote(state):
        row = state["installations"]["alpha"]
        row.update(active_version="1.1.0", active_path=row["staged_path"], python_path=row["staged_python_path"],
                   manifest=row["staged_manifest"], staged_version=None, staged_path=None,
                   staged_python_path=None, staged_manifest=None, state="active", pending_restart=False,
                   supplied_by={"cap.two": "gamma"})
        state["jobs"].append({"id": "promoted"})

    with pytest.raises(Exception):
        mutate_state(promote)

    assert calls
    assert (raw_row(hub), _releases_in(hub), _bindings_in(hub)) == before
    assert load_state()["installations"]["alpha"]["staged_version"] == "1.1.0"


def _bindings_in(engine):
    with engine.connect() as connection:
        return sorted(tuple(row) for row in connection.execute(sa.text(
            "SELECT release_id, capability, supplier_id FROM provider_hub_release_bindings")))


def test_two_activations_of_one_staged_release_make_it_active_once(staged, monkeypatch):
    import threading

    from provider_hub.service import activate_release
    from provider_hub.state import load_state

    outcomes = []
    gate = threading.Barrier(2)

    def check(manifest, bundle_path, python_path):
        try:
            gate.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass

    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", check)

    def run():
        outcomes.append(activate_release("alpha", at_boot=True).outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [thread.start() for thread in threads]
    [thread.join(timeout=30) for thread in threads]

    assert sorted(outcomes) == ["activated", "skipped"]
    row = load_state()["installations"]["alpha"]
    assert (row["active_version"], row["staged_version"]) == ("1.1.0", None)
    assert _releases_in(staged) == [("alpha", "1.0.0", "retired"), ("alpha", "1.1.0", "active")]


def test_a_reader_and_a_writer_in_two_threads_never_disagree(hub):
    import threading
    import time

    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active="1.0.0", name="1.0.0"))
    stop = threading.Event()
    seen = []

    def write():
        for number in range(2, 60):
            version = f"{number}.0.0"
            mutate_state(lambda state: state["installations"]["alpha"].update(name=version, active_version=version))
        stop.set()

    def read():
        deadline = time.monotonic() + 60
        while not stop.is_set() and time.monotonic() < deadline:
            row = load_state()["installations"]["alpha"]
            seen.append((row["name"], row["active_version"]))

    threads = [threading.Thread(target=write), threading.Thread(target=read)]
    [thread.start() for thread in threads]
    [thread.join(timeout=90) for thread in threads]

    assert seen and all(name == version for name, version in seen)


def test_a_release_still_draining_at_boot_is_retired_and_a_boot_with_none_writes_nothing(hub, service_kind,
                                                                                         monkeypatch):
    from provider_hub import state_store

    _seed(alpha=_row("alpha", active="1.0.0"))
    before = raw_row(hub)

    assert state_store.retire_draining() == 0
    assert raw_row(hub) == before


def test_a_release_cannot_be_made_active_or_staged_outside_a_write_of_the_state(hub):
    from provider_hub import releases, state_store

    _seed(alpha=_row("alpha", active="1.0.0", staged="1.1.0"))
    [staged_release] = [item for item in state_store.list_releases("alpha", bind=hub) if item.state == "staged"]

    with pytest.raises(releases.ReleaseTransitionError):
        state_store.transition_release(staged_release.id, "active")
    with pytest.raises(releases.ReleaseTransitionError):
        state_store.transition_release(staged_release.id, "staged")
    assert state_store.transition_release(staged_release.id, "failed", error="stopped") is True


def test_one_plugin_that_cannot_be_activated_does_not_keep_the_others_from_starting(staged, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    _seed(beta=_row("beta", active="1.0.0", staged="1.1.0"))
    real = service.activate_release

    def fail_for_alpha(provider_id, **kwargs):
        if provider_id == "alpha":
            raise RuntimeError("the state went away")
        return real(provider_id, **kwargs)

    monkeypatch.setattr(service, "activate_release", fail_for_alpha)

    assert service.activate_staged_installations() == ["beta"]

    state = load_state()["installations"]
    assert state["beta"]["active_version"] == "1.1.0" and state["alpha"]["staged_version"] == "1.1.0"


def test_a_rollback_to_a_release_from_another_source_is_refused(staged):
    from provider_hub.service import ProviderHubInstallError, activate_staged_installations, rollback_release
    from provider_hub.state import mutate_state

    activate_staged_installations()
    mutate_state(lambda state: state["installations"]["alpha"].update(source_id="community"))

    with pytest.raises(ProviderHubInstallError, match="another source"):
        rollback_release("alpha")

    assert _releases_in(staged) == [("alpha", "1.0.0", "retired"), ("alpha", "1.1.0", "active")]


def test_the_previous_release_after_a_rollback_and_an_update_is_where_the_next_rollback_goes(staged, monkeypatch):
    from provider_hub import service
    from provider_hub.state import mutate_state

    service.activate_staged_installations()
    service.rollback_release("alpha")
    service.activate_staged_installations()

    def stage_three(state):
        row = state["installations"]["alpha"]
        row.update(staged_version="1.2.0", staged_path=_bundle("alpha", "1.2.0"),
                   staged_python_path=_python("alpha", "1.2.0"), staged_manifest=_release_manifest("alpha", "1.2.0"),
                   state="staged", pending_restart=True)

    mutate_state(stage_three)
    service.activate_staged_installations()

    assert _releases_in(staged) == [("alpha", "1.0.0", "retired"), ("alpha", "1.2.0", "active")]
    assert service.rollback_release("alpha").version == "1.0.0"


# ---- going back to the previous release ---------------------------------------------------------------------


def test_a_rollback_stages_the_previous_release_and_the_boot_makes_it_active_again(staged):
    from provider_hub import state_store
    from provider_hub.service import activate_staged_installations, rollback_release
    from provider_hub.state import load_state

    activate_staged_installations()
    ids = {item.version: item.id for item in state_store.list_releases("alpha", bind=staged)}

    result = rollback_release("alpha")

    assert (result.outcome, result.version, result.previous_version) == ("waiting", "1.0.0", "1.1.0")
    row = load_state()["installations"]["alpha"]
    assert (row["state"], row["active_version"], row["staged_version"], row["pending_restart"]) == (
        "staged", "1.1.0", "1.0.0", True)
    assert _releases_in(staged) == [("alpha", "1.0.0", "staged"), ("alpha", "1.1.0", "active")]

    assert activate_staged_installations() == ["alpha"]

    row = load_state()["installations"]["alpha"]
    assert (row["state"], row["active_version"], row["staged_version"]) == ("active", "1.0.0", None)
    assert _releases_in(staged) == [("alpha", "1.0.0", "active"), ("alpha", "1.1.0", "retired")]
    assert {item.version: item.id for item in state_store.list_releases("alpha", bind=staged)} == ids


def test_there_is_nothing_to_roll_back_to_without_a_previous_release(staged):
    from provider_hub.service import ProviderHubInstallError, rollback_release

    _seed(solo=_row("solo", active="1.0.0"))
    with pytest.raises(ProviderHubInstallError, match="no previous release"):
        rollback_release("solo")
    with pytest.raises(ProviderHubInstallError, match="update waiting"):
        rollback_release("alpha")
    with pytest.raises(ProviderHubInstallError, match="not installed"):
        rollback_release("nobody")


def test_a_previous_release_the_catalog_revoked_is_not_a_rollback_target(staged, monkeypatch):
    from provider_hub import revocation, service

    service.activate_staged_installations()
    monkeypatch.setattr(service, "_revocations", lambda state: (
        revocation.Revocation(plugin_id="alpha", version="1.0.0", reason="withdrawn"),))

    with pytest.raises(service.ProviderHubInstallError, match="withdrawn"):
        service.rollback_release("alpha")

    assert _releases_in(staged) == [("alpha", "1.0.0", "retired"), ("alpha", "1.1.0", "active")]


def test_a_rollback_of_a_kind_that_attaches_live_happens_now(hub, service_kind, monkeypatch):
    from provider_hub.service import activate_release, retire_release, rollback_release
    from provider_hub.state import load_state

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    _seed(exampleservice=_service_row(
        "1.1.0", active_version="1.0.0", active_path=_bundle("exampleservice", "1.0.0"),
        python_path=_python("exampleservice"), manifest=service_manifest("exampleservice", "1.0.0")))
    activate_release("exampleservice")
    retire_release("exampleservice")

    result = rollback_release("exampleservice")

    assert result.activated and result.draining
    assert load_state()["installations"]["exampleservice"]["active_version"] == "1.0.0"
    assert _releases_in(hub) == [("exampleservice", "1.0.0", "active"), ("exampleservice", "1.1.0", "draining")]


def _supplier_world(hub, monkeypatch):
    """``helper`` supplied translit.v1 from 2.0.0 on and ran without it before; ``user`` relies on it."""
    from provider_hub.service import activate_staged_installations

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    old = service_manifest("helper", "1.0.0")
    new = service_manifest("helper", "2.0.0", provides=["translit.v1"])
    helper = _row("helper", active="1.0.0", kind=SERVICE_KIND, manifest=old, staged=("2.0.0", None))
    helper.update(staged_manifest=new)
    user = _row("user", active="1.0.0", kind=SERVICE_KIND,
                manifest=service_manifest("user", "1.0.0", requires=["translit.v1"]))
    _seed(helper=helper, user=user)
    activate_staged_installations()


def test_the_previous_release_after_a_rollback_and_a_new_update_is_the_one_that_was_replaced(table):
    """v1, then v2, back to v1, then v3: v1 is what the next rollback goes to, and v2 is forgotten."""
    steps = [dict(active="1.0.0"), dict(active="1.0.0", staged="2.0.0"), dict(active="2.0.0"),
             dict(active="2.0.0", staged="1.0.0"), dict(active="1.0.0"), dict(active="1.0.0", staged="3.0.0"),
             dict(active="3.0.0")]
    for step in steps:
        table.write(_document(alpha=_row("alpha", **step)))

    assert table.releases("alpha") == [("1.0.0", "retired"), ("3.0.0", "active")]


def test_a_reader_never_gets_the_document_of_one_revision_with_the_releases_of_another(hub, monkeypatch):
    from provider_hub import releases, state_store
    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active="1.0.0", name="1.0.0"))
    real_read_rows = releases.read_rows
    real_read_state = state_store.read_state
    reading = []
    writes = []

    def read_state(*args, **kwargs):
        reading.append(1)
        try:
            return real_read_state(*args, **kwargs)
        finally:
            reading.pop()

    def read_rows(connection, **kwargs):
        if reading and len(writes) < 12:
            # Another writer commits between the reader's two reads, every time.
            version = f"{len(writes) + 2}.0.0"
            writes.append(version)
            held = list(reading)
            reading.clear()
            try:
                mutate_state(lambda state: state["installations"]["alpha"].update(name=version, active_version=version))
            finally:
                reading.extend(held)
        return real_read_rows(connection, **kwargs)

    monkeypatch.setattr(state_store, "read_state", read_state)
    monkeypatch.setattr(releases, "read_rows", read_rows)

    row = load_state()["installations"]["alpha"]

    assert writes
    assert row["name"] == row["active_version"]


def test_the_environment_of_the_previous_release_is_not_cleaned_up(staged):
    from provider_hub import env_registry
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    activate_staged_installations()

    referenced = env_registry.referenced_env_paths(load_state())

    assert env_registry.env_dir_of(_python("alpha", "1.0.0")) in referenced
    assert env_registry.env_dir_of(_python("alpha", "1.1.0")) in referenced


def test_a_rollback_whose_files_are_gone_is_refused_and_the_previous_release_stays_recorded(staged):
    from provider_hub.service import ProviderHubInstallError, activate_staged_installations, rollback_release

    activate_staged_installations()
    Path(_python("alpha", "1.0.0")).unlink()

    with pytest.raises(ProviderHubInstallError, match="no longer on disk"):
        rollback_release("alpha")

    assert _releases_in(staged) == [("alpha", "1.0.0", "retired"), ("alpha", "1.1.0", "active")]


def test_a_rollback_that_would_leave_a_dependent_without_its_capability_is_refused(hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, rollback_release
    from provider_hub.state import load_state

    _supplier_world(hub, monkeypatch)

    with pytest.raises(ProviderHubInstallError, match="user"):
        rollback_release("helper")

    row = load_state()["installations"]["helper"]
    assert (row["state"], row["active_version"], row["staged_version"]) == ("active", "2.0.0", None)
    assert _releases_in(hub, "helper") == [("helper", "1.0.0", "retired"), ("helper", "2.0.0", "active")]


def _two_releases(hub, monkeypatch, plugin_id, old, new, **row_extra):
    """``plugin_id`` ran ``old`` and now runs ``new`` (manifests), with ``old`` kept as the previous release."""
    from provider_hub.service import activate_staged_installations

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service.validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr("provider_hub.service.validated_interpreter", lambda root, path: Path(path))
    row = _row(plugin_id, active="1.0.0", kind=SERVICE_KIND, manifest=old, staged=("2.0.0", None), **row_extra)
    row.update(staged_manifest=new)
    _seed(**{plugin_id: row})
    activate_staged_installations()


def test_a_rollback_to_a_release_that_asks_for_more_asks_for_consent_even_when_the_feed_no_longer_lists_it(
        hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubConsentError, rollback_consent, rollback_release
    from provider_hub.state import load_state

    wide = service_manifest("helper", "1.0.0", permissions=["network"], endpoints=["https://api.example.org:443"])
    _two_releases(hub, monkeypatch, "helper", wide, service_manifest("helper", "2.0.0"))
    assert not load_state().get("catalog_entries")

    with pytest.raises(ProviderHubConsentError, match="network"):
        rollback_release("helper")
    assert load_state()["installations"]["helper"]["active_version"] == "2.0.0"

    delta = rollback_consent("helper")
    assert delta["needs_consent"] and delta["added"]["destinations"] == ["https://api.example.org:443"]
    assert rollback_release("helper", consent_digest=delta["digest"]).activated
    assert load_state()["installations"]["helper"]["active_version"] == "1.0.0"


def test_a_rollback_to_a_release_that_needs_a_helper_removed_since_is_refused_before_anything_changes(
        hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, rollback_release
    from provider_hub.state import load_state, mutate_state

    _seed(dep=_row("dep", active="1.0.0", kind=SERVICE_KIND,
                   manifest=service_manifest("dep", "1.0.0", provides=["translit.v1"])))
    needing = service_manifest("main", "1.0.0", requires=["translit.v1"])
    _two_releases(hub, monkeypatch, "main", needing, service_manifest("main", "2.0.0"),
                  supplied_by={"translit.v1": "dep"})
    mutate_state(lambda state: state["installations"].pop("dep"))

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")

    row = load_state()["installations"]["main"]
    assert (row["active_version"], row["staged_version"]) == ("2.0.0", None)
    assert _releases_in(hub, "main") == [("main", "1.0.0", "retired"), ("main", "2.0.0", "active")]


def test_a_rollback_to_a_release_that_needs_a_helper_that_is_there_records_the_binding(
        hub, service_kind, monkeypatch):
    from provider_hub.service import rollback_release
    from provider_hub.state import load_state

    _seed(dep=_row("dep", active="1.0.0", kind=SERVICE_KIND,
                   manifest=service_manifest("dep", "1.0.0", provides=["translit.v1"])))
    needing = service_manifest("main", "1.0.0", requires=["translit.v1"])
    _two_releases(hub, monkeypatch, "main", needing, service_manifest("main", "2.0.0"),
                  supplied_by={"translit.v1": "dep"})

    assert rollback_release("main").activated

    assert load_state()["installations"]["main"]["supplied_by"] == {"translit.v1": "dep"}


def test_releases_a_damaged_document_lost_survive_ordinary_writes_until_someone_discards_them(hub):
    from provider_hub import state_store
    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active="1.0.0"), beta=_row("beta", active="2.0.0"))
    with hub.begin() as connection:
        connection.execute(sa.text("UPDATE provider_hub_state SET document = '{broken' WHERE id = 1"))

    mutate_state(lambda state: state["jobs"].append({"id": "after"}), recover_corrupt=True)
    mutate_state(lambda state: state["jobs"].append({"id": "again"}))
    mutate_state(lambda state: state["installations"].update(gamma=_row("gamma", active="3.0.0")))

    assert _releases_in(hub) == [("alpha", "1.0.0", "active"), ("beta", "2.0.0", "active"),
                                 ("gamma", "3.0.0", "active")]
    assert sorted(state_store.orphan_plugin_ids(hub)) == ["alpha", "beta"]
    # A plugin listed again with the same release is matched to the record it left, not given a second one.
    kept_id = [item.id for item in state_store.list_releases("alpha", bind=hub)]
    mutate_state(lambda state: state["installations"].update(alpha=_row("alpha", active="1.0.0", name="Alpha")))
    assert [item.id for item in state_store.list_releases("alpha", bind=hub)] == kept_id
    assert load_state()["installations"]["alpha"]["active_version"] == "1.0.0"
    assert sorted(state_store.orphan_plugin_ids(hub)) == ["beta"]
    # Removing a plugin the document lists still takes its releases with it.
    mutate_state(lambda state: state["installations"].pop("gamma"))
    assert ("gamma", "3.0.0", "active") not in _releases_in(hub)
    # Only an explicit discard takes the others.
    assert state_store.discard_orphan_releases(["beta"], bind=hub) == 1
    assert _releases_in(hub) == [("alpha", "1.0.0", "active")]


def test_a_null_map_of_bindings_reads_back_as_null_and_an_empty_one_as_empty(table):
    null = _row("alpha", active="1.0.0", staged="1.1.0", supplied_by=None, staged_supplied_by=None)
    empty = _row("beta", active="1.0.0", supplied_by={})
    document = _document(alpha=null, beta=empty)

    stored = table.write(document)

    read = table.read(stored)["installations"]
    assert read["alpha"]["supplied_by"] is None and read["alpha"]["staged_supplied_by"] is None
    assert read["beta"]["supplied_by"] == {}


def _needing_world(hub, service_kind, monkeypatch, **supplier):
    """``main`` ran a release that needed translit.v1 from ``dep`` before its current one, which does not.

    ``dep`` is installed after the activation that made the rollback possible, so what the test sets on it
    (an update staged, a switch off) is as the last restart left it.
    """
    needing = service_manifest("main", "1.0.0", requires=["translit.v1"])
    _two_releases(hub, monkeypatch, "main", needing, service_manifest("main", "2.0.0"),
                  supplied_by={"translit.v1": "dep"})
    row = _row("dep", active="1.0.0", kind=SERVICE_KIND,
               manifest=service_manifest("dep", "1.0.0", provides=["translit.v1"]))
    row.update(supplier)
    _seed(dep=row)


def test_a_rollback_is_refused_when_the_supplier_has_an_update_staged_that_drops_the_capability(
        hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, rollback_release
    from provider_hub.state import load_state

    _needing_world(hub, service_kind, monkeypatch, staged_version="2.0.0", state="staged", pending_restart=True,
                   staged_path=_bundle("dep", "2.0.0"), staged_python_path=_python("dep", "2.0.0"),
                   staged_manifest=service_manifest("dep", "2.0.0"))

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")

    assert load_state()["installations"]["main"]["active_version"] == "2.0.0"
    assert _releases_in(hub, "main") == [("main", "1.0.0", "retired"), ("main", "2.0.0", "active")]


def test_a_supplier_removed_between_the_early_check_and_the_staging_write_still_refuses_the_rollback(
        hub, service_kind, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    _needing_world(hub, service_kind, monkeypatch)
    real = service.rollback_consent

    def remove_the_supplier_first(*args, **kwargs):
        mutate_state(lambda state: state["installations"].pop("dep"))
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "rollback_consent", remove_the_supplier_first)

    with pytest.raises(service.ProviderHubInstallError, match="translit.v1"):
        service.rollback_release("main")

    row = load_state()["installations"]["main"]
    assert (row["active_version"], row["staged_version"]) == ("2.0.0", None)


@pytest.mark.parametrize("change", [
    dict(enabled=False), dict(revoked=True), dict(needs_repair=True),
], ids=["switched_off", "revoked", "flagged_for_repair"])
def test_a_supplier_that_does_not_run_does_not_meet_a_rollbacks_need(hub, service_kind, monkeypatch, change):
    from provider_hub.service import ProviderHubInstallError, rollback_release

    _needing_world(hub, service_kind, monkeypatch, **change)

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")


def test_a_supplier_that_is_itself_unmet_does_not_meet_a_rollbacks_need(hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, rollback_release

    _needing_world(hub, service_kind, monkeypatch, supplied_by={"base.v1": "base"},
                   manifest=service_manifest("dep", "1.0.0", provides=["translit.v1"], requires=["base.v1"]))
    _seed(base=_row("base", active="1.0.0", kind=SERVICE_KIND, enabled=False,
                    manifest=service_manifest("base", "1.0.0", provides=["base.v1"])))

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")


def test_a_release_that_extends_a_plugin_that_is_gone_is_not_restored(hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, rollback_release

    extending = service_manifest("addon", "1.0.0", extends={"id": "host", "capability": "parent.v1"})
    _seed(host=_row("host", active="1.0.0", kind=SERVICE_KIND,
                    manifest=service_manifest("host", "1.0.0", provides=["parent.v1"])))
    _two_releases(hub, monkeypatch, "addon", extending, service_manifest("addon", "2.0.0"))
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["installations"].pop("host"))

    with pytest.raises(ProviderHubInstallError, match="host"):
        rollback_release("addon")


def test_a_need_only_another_sources_catalog_provides_does_not_block_a_rollback(hub, service_kind, monkeypatch):
    """Install resolves a need from the plugin's own source and what is installed: another source's listing is not
    a reason to refuse a release the active one's own needs would allow."""
    from provider_hub.service import rollback_release
    from provider_hub.state import load_state, mutate_state

    needing = service_manifest("main", "1.0.0", requires=["translit.v1"])
    _two_releases(hub, monkeypatch, "main", needing, service_manifest("main", "2.0.0"),
                  source_id="official")
    other = service_manifest("lister", "1.0.0", provides=["translit.v1"])
    mutate_state(lambda state: state["catalog_entries"].update({"community:plugins:lister:1.0.0": {
        "source": "community", "provider_id": "lister", "version": "1.0.0", "manifest": other}}))

    assert rollback_release("main").activated
    assert load_state()["installations"]["main"]["active_version"] == "1.0.0"


def test_a_need_the_plugins_own_source_provides_blocks_a_rollback_until_something_supplies_it(
        hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, rollback_release
    from provider_hub.state import mutate_state

    needing = service_manifest("main", "1.0.0", requires=["translit.v1"])
    _two_releases(hub, monkeypatch, "main", needing, service_manifest("main", "2.0.0"), source_id="official")
    listed = service_manifest("lister", "1.0.0", provides=["translit.v1"])
    mutate_state(lambda state: state["catalog_entries"].update({"official:plugins:lister:1.0.0": {
        "source": "official", "provider_id": "lister", "version": "1.0.0", "manifest": listed}}))

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")


def test_the_files_of_a_release_whose_plugin_a_damaged_document_lost_stay_protected_from_cleanup(hub):
    from provider_hub import env_registry
    from provider_hub.state import load_state, mutate_state

    _seed(alpha=_row("alpha", active="1.0.0"), beta=_row("beta", active="2.0.0"))
    with hub.begin() as connection:
        connection.execute(sa.text("UPDATE provider_hub_state SET document = '{broken' WHERE id = 1"))
    mutate_state(lambda state: state["jobs"].append({"id": "after"}), recover_corrupt=True)

    referenced = env_registry.referenced_env_paths(load_state())

    assert env_registry.env_dir_of(_python("alpha", "1.0.0")) in referenced
    assert env_registry.env_dir_of(_python("beta", "2.0.0")) in referenced


def test_a_whole_document_saved_after_a_recovery_does_not_take_the_lost_releases(hub):
    from provider_hub.state import load_state, save_state

    _seed(alpha=_row("alpha", active="1.0.0"))
    with hub.begin() as connection:
        connection.execute(sa.text("UPDATE provider_hub_state SET document = '{broken' WHERE id = 1"))
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["jobs"].append({"id": "after"}), recover_corrupt=True)

    save_state(load_state())
    save_state(load_state())

    assert _releases_in(hub) == [("alpha", "1.0.0", "active")]


@pytest.mark.parametrize("stored", ['{"installations": 5}', '{"installations": [{"a": 1}]}', '[1, 2]', "null"])
def test_a_stored_document_that_is_not_an_inventory_lists_no_plugin_and_never_raises_a_type_error(stored):
    from provider_hub.state import _listed_ids

    assert _listed_ids(stored) == set()


def test_a_damaged_document_is_reported_as_one_by_the_orphan_tools_and_a_whole_save_still_works(hub):
    from provider_hub import state_store
    from provider_hub.state import StateCorruptError, default_state, save_state

    _seed(alpha=_row("alpha", active="1.0.0"))
    with hub.begin() as connection:
        connection.execute(sa.text("UPDATE provider_hub_state SET document = '{\"installations\": 5}' WHERE id = 1"))

    with pytest.raises(StateCorruptError):
        state_store.orphan_plugin_ids(hub)
    with pytest.raises(StateCorruptError):
        state_store.discard_orphan_releases(["alpha"], bind=hub)
    save_state(default_state())

    assert _releases_in(hub) == [("alpha", "1.0.0", "active")]


def test_a_consent_digest_that_no_longer_matches_is_refused_again(hub, service_kind, monkeypatch):
    from provider_hub.service import ProviderHubConsentError, rollback_consent, rollback_release

    wide = service_manifest("helper", "1.0.0", permissions=["network"])
    _two_releases(hub, monkeypatch, "helper", wide, service_manifest("helper", "2.0.0"))
    digest = rollback_consent("helper")["digest"]

    with pytest.raises(ProviderHubConsentError):
        rollback_release("helper", consent_digest="0" * len(digest))
    with pytest.raises(ProviderHubConsentError):
        rollback_release("helper", consent_digest=None)


def test_a_provider_without_dependencies_has_no_consent_step_on_a_rollback(staged):
    from provider_hub.service import activate_staged_installations, rollback_consent

    activate_staged_installations()

    assert rollback_consent("alpha") is None
    assert rollback_consent("nobody") is None


def _chain_world(hub, monkeypatch, e_extra=None, e_manifest=None):
    """``main`` ran a release that needed translit.v1. ``bravo`` has an update staged that adds translit.v1 and needs
    other.v1, which ``echo`` supplies."""
    needing = service_manifest("main", "1.0.0", requires=["translit.v1"])
    _two_releases(hub, monkeypatch, "main", needing, service_manifest("main", "2.0.0"),
                  supplied_by={"translit.v1": "bravo"})
    bravo = _row("bravo", active="1.0.0", staged=("2.0.0", {"other.v1": "echo"}), kind=SERVICE_KIND,
             manifest=service_manifest("bravo", "1.0.0"),
             staged_manifest=service_manifest("bravo", "2.0.0", provides=["translit.v1"], requires=["other.v1"]))
    echo = _row("echo", active="1.0.0", kind=SERVICE_KIND,
             manifest=e_manifest or service_manifest("echo", "1.0.0", provides=["other.v1"]))
    echo.update(e_extra or {})
    _seed(bravo=bravo, echo=echo)


def test_a_rollback_is_refused_when_the_supplier_it_counts_on_would_be_unmet_after_the_restart(hub, service_kind,
                                                                                               monkeypatch):
    """b's staged update adds translit.v1 and needs other.v1, whose supplier is revoked: at the restart b would
    be held back, and main with it."""
    from provider_hub.service import ProviderHubInstallError, rollback_release
    from provider_hub.state import load_state

    _chain_world(hub, monkeypatch, e_extra={"revoked": True})

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")

    assert load_state()["installations"]["main"]["active_version"] == "2.0.0"


def test_a_rollback_is_refused_when_any_link_of_the_chain_of_suppliers_would_be_unmet(hub, service_kind,
                                                                                       monkeypatch):
    """b needs e, and e needs a plugin that is gone: two levels down."""
    from provider_hub.service import ProviderHubInstallError, rollback_release

    _chain_world(hub, monkeypatch, e_extra={"supplied_by": {"f.v1": "golf"}},
                 e_manifest=service_manifest("echo", "1.0.0", provides=["other.v1"], requires=["f.v1"]))

    with pytest.raises(ProviderHubInstallError, match="translit.v1"):
        rollback_release("main")


def test_a_rollback_goes_ahead_when_every_supplier_in_the_chain_runs_after_the_restart(hub, service_kind,
                                                                                        monkeypatch):
    from provider_hub.service import rollback_release
    from provider_hub.state import load_state

    _chain_world(hub, monkeypatch)
    assert rollback_release("main").activated

    assert load_state()["installations"]["main"]["supplied_by"] == {"translit.v1": "bravo"}


# ---- workers are keyed by release ---------------------------------------------------------------------------


def test_a_worker_is_told_apart_by_the_release_it_runs():
    from provider_hub.manifest import validate_manifest
    from provider_hub.registry import _make_provider_class
    from provider_hub.worker import ProviderWorkerClient, _live_clients, _live_clients_lock, live_workers

    manifest = validate_manifest(_manifest(provider_id="keyed"))
    provider_class = _make_provider_class(manifest)
    assert provider_class.release_key == manifest.bundle_sha256

    old = ProviderWorkerClient(["python"], provider_id="keyed", release="a" * 64)
    new = ProviderWorkerClient(["python"], provider_id="keyed", release="b" * 64)
    plain = ProviderWorkerClient(["python"], provider_id="keyed")
    other = ProviderWorkerClient(["python"], provider_id="other", release="a" * 64)
    with _live_clients_lock:
        _live_clients.update({old, new, plain, other})
    try:
        assert live_workers("keyed", "a" * 64) == [old]
        assert set(live_workers("keyed")) == {old, new, plain}
        assert set(live_workers(release="a" * 64)) == {old, other}
        assert plain.release is None
    finally:
        with _live_clients_lock:
            for client in (old, new, plain, other):
                _live_clients.discard(client)


def test_data_identity_moves_with_its_release_even_for_the_same_bundle(hub):
    from provider_hub import state_store
    from provider_hub.state import mutate_state

    _seed(alpha=_row("alpha", active="1.0.0", staged="1.0.0",
                     active_data_key="a" * 32, staged_data_key="b" * 32))

    def promote(state):
        row = state["installations"]["alpha"]
        row.update(active_data_key=row.pop("staged_data_key"), staged_version=None, staged_path=None,
                   staged_python_path=None, staged_manifest=None)

    before = {item.state: item.id for item in state_store.list_releases("alpha")}
    mutate_state(promote)

    after = {item.state: (item.id, item.data_key) for item in state_store.list_releases("alpha")}
    assert after == {"active": (before["staged"], "b" * 32), "retired": (before["active"], "a" * 32)}


def test_release_data_copy_preserves_local_links_and_rejects_external_files(tmp_path):
    from provider_hub import datadir

    source = tmp_path / "source"
    source.mkdir()
    (source / "saved").write_bytes(b"original")
    (source / "inside").symlink_to("saved")
    target = tmp_path / "target"
    before = datadir.copy_folder(source, target)
    assert (target / "inside").readlink() == Path("saved")
    (target / "saved").write_bytes(b"smoke output")
    assert datadir.fingerprint(source) == before
    assert (source / "saved").read_bytes() == b"original"

    (source / "outside").symlink_to("../other")
    with pytest.raises(datadir.DataFolderError):
        datadir.copy_folder(source, target)
    assert (target / "saved").read_bytes() == b"smoke output"


@pytest.fixture
def data_release(hub, service_kind, monkeypatch):
    from provider_hub import datadir, service
    from provider_hub.state import provider_hub_dir

    monkeypatch.setattr(service, "verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr(service, "validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr(service, "validated_interpreter", lambda root, path: Path(path))
    monkeypatch.setattr(service.PluginEnvironment, "install", lambda self, *args, **kwargs: Path(_python("exampleservice")).parent.parent)
    active = datadir.release_path(provider_hub_dir(), "exampleservice", "a" * 32)
    active.mkdir(parents=True)
    (active / "settings").write_text("from v1")
    old = service_manifest("exampleservice", "1.0.0", permissions=["data_dir"])
    new = service_manifest("exampleservice", "1.1.0", permissions=["data_dir"])
    _seed(exampleservice=_service_row(
        None, active_version="1.0.0", active_path=_bundle("exampleservice", "1.0.0"), active_data_key=active.name,
        python_path=_python("exampleservice"), state="active", pending_restart=False, staged_manifest=None,
        staged_path=None, manifest=old))
    return service, new, active


@pytest.fixture
def catalog_data_release(data_release, monkeypatch):
    from plugin_platform_helpers import verified_official_source
    from provider_hub.state import mutate_state

    service, manifest, _ = data_release

    def list_update(state):
        state["catalog_sources"]["official"] = verified_official_source()
        state["catalog_entries"]["official:plugins:exampleservice:1.1.0"] = {
            "source": "official", "provider_id": "exampleservice", "version": "1.1.0",
            "trusted": True, "manifest": manifest, "catalog_list": "plugins",
        }

    mutate_state(list_update)
    monkeypatch.setattr(service, "_fetch_bundle",
                        lambda candidate, deadline: Path(_bundle(candidate.provider_id, candidate.version)))
    return data_release


def test_stage_install_fences_active_data_copy_and_reopens_before_smoke(
        catalog_data_release, service_kind, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from provider_hub import datadir
    from provider_hub.worker import ProviderWorkerClient

    service, manifest, active = catalog_data_release
    plugin_id = manifest["id"]
    started = Event()
    dispatched = Event()
    active_settings = active / "settings"
    client = ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})
    monkeypatch.setattr(client, "_request", lambda *args: dispatched.set())

    def call_worker():
        started.set()
        client.request("health", {})

    def settle(checked_id, timeout):
        assert checked_id == plugin_id
        assert 0 < timeout <= 10.0
        active_settings.write_text("last settled write")
        return True

    copy_folder = datadir.copy_folder

    def copy(source, destination):
        assert source == active
        worker = executor.submit(call_worker)
        assert started.wait(2)
        assert not dispatched.wait(0.05)
        assert not worker.done()
        return copy_folder(source, destination)

    def smoke(validated, bundle, interpreter, *, data_dir, timeout):
        assert dispatched.wait(2)
        assert (data_dir / "settings").read_text() == "last settled write"
        assert 0 < timeout <= 10.0

    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(service_kind, "settle_data", settle)
        monkeypatch.setattr(service_kind, "smoke_test", smoke)
        monkeypatch.setattr(datadir, "copy_folder", copy)
        row = service.stage_install(manifest, source_id="official", install_timeout=10.0)

    assert row["staged_version"] == "1.1.0"
    assert row["active_version"] == "1.0.0"


@pytest.mark.parametrize("busy_at", ["dispatch", "settle"])
def test_stage_install_refuses_a_snapshot_when_active_data_does_not_settle(
        catalog_data_release, service_kind, monkeypatch, busy_at):
    from contextlib import nullcontext

    from provider_hub import datadir
    from provider_hub.state import load_state

    service, manifest, active = catalog_data_release
    plugin_id = manifest["id"]
    settled = []

    def settle(checked_id, timeout):
        settled.append(checked_id)
        assert 0 <= timeout <= 0.03
        return False

    monkeypatch.setattr(datadir, "QUIESCE_TIMEOUT", 0.03)
    monkeypatch.setattr(service_kind, "settle_data", settle)
    dispatch = datadir.dispatch(plugin_id) if busy_at == "dispatch" else nullcontext()
    with dispatch, pytest.raises(service.ProviderHubInstallError, match="busy"):
        service.stage_install(manifest, source_id="official")

    assert settled == ([plugin_id] if busy_at == "settle" else [])
    row = load_state()["installations"][plugin_id]
    assert row["active_version"] == "1.0.0" and row["staged_version"] is None
    assert list(active.parent.iterdir()) == [active]
    assert (active / "settings").read_text() == "from v1"


def test_stage_install_does_not_copy_when_the_install_budget_expires_while_settling(
        catalog_data_release, service_kind, monkeypatch):
    from types import SimpleNamespace

    from provider_hub import datadir

    service, manifest, active = catalog_data_release
    now = 0.0
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now, perf_counter=service.time.perf_counter))

    def settle(plugin_id, timeout):
        nonlocal now
        assert 0 < timeout <= 10.0
        now = 10.0
        return True

    def unexpected_copy(*args):
        pytest.fail("the snapshot started after the install deadline")

    monkeypatch.setattr(service_kind, "settle_data", settle)
    monkeypatch.setattr(datadir, "copy_folder", unexpected_copy)
    with pytest.raises(service.ProviderHubInstallError, match="time budget"):
        service.stage_install(manifest, source_id="official", install_timeout=10.0)
    assert list(active.parent.iterdir()) == [active]


def test_staging_uses_private_data_and_switch_refreshes_work_done_since_staging(data_release, service_kind, monkeypatch):
    from provider_hub import datadir, secret_paths
    from provider_hub.state import load_state, provider_hub_dir

    service, new, old = data_release
    checked = []

    def smoke(validated, bundle, interpreter, *, data_dir, timeout=None):
        checked.append((data_dir / "settings").read_text())
        (data_dir / "settings").write_text("migrated v2")

    monkeypatch.setattr(service_kind, "smoke_test", smoke)
    service._stage_validated(service_kind.validate_manifest(new, True), True, "catalog", "official",
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    assert (old / "settings").read_text() == "from v1"
    (old / "settings").write_text("v1 kept working")

    assert service.activate_release("exampleservice").activated
    assert checked == ["from v1", "v1 kept working"]
    current = load_state()["installations"]["exampleservice"]["active_data_key"]
    folder = datadir.release_path(provider_hub_dir(), "exampleservice", current)
    assert (folder / "settings").read_text() == "migrated v2"
    assert (old / "settings").read_text() == "v1 kept working"
    history = secret_paths.archived_manifests(provider_hub_dir(), "exampleservice")
    assert {manifest["version"] for manifest in history} == {"1.0.0", "1.1.0"}
    datadir.cleanup(provider_hub_dir(), load_state())
    assert (old / "settings").exists()

    # The rollback smoke test can write to a copy, while the restored release
    # must run with its original data and the previous release remains kept.
    assert service.rollback_release("exampleservice").activated
    assert load_state()["installations"]["exampleservice"]["active_data_key"] == old.name
    assert (old / "settings").read_text() == "v1 kept working"
    assert (folder / "settings").read_text() == "migrated v2"


def test_busy_activation_stays_staged_and_update_check_retries_it(data_release, service_kind, monkeypatch):
    import threading

    from provider_hub import datadir
    from provider_hub.state import load_state

    service, new, old = data_release
    service._stage_validated(service_kind.validate_manifest(new, True), True, "catalog", "official",
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    monkeypatch.setattr(datadir, "QUIESCE_TIMEOUT", 0.03)
    entered = threading.Event()
    release = threading.Event()

    def running():
        with datadir.dispatch("exampleservice"):
            entered.set()
            release.wait(2)

    job = threading.Thread(target=running)
    job.start()
    try:
        assert entered.wait(1)
        result = service.activate_release("exampleservice")
        assert result.outcome == service.ACTIVATION_WAITING
        row = load_state()["installations"]["exampleservice"]
        assert row["active_data_key"] == old.name
        assert row["staged_version"] == "1.1.0"
    finally:
        release.set()
        job.join(1)

    service.check_updates()
    assert load_state()["installations"]["exampleservice"]["active_version"] == "1.1.0"


@pytest.mark.parametrize("keep", [True, False])
def test_uninstall_settles_release_data_at_boot(data_release, keep):
    from provider_hub.state import load_state

    service, _, old = data_release
    assert (service.remove_installation("exampleservice") if keep
            else service.remove_installation("exampleservice", keep_data=False))
    assert old.exists()

    service.activate_staged_installations()

    assert "exampleservice" not in load_state()["installations"]
    assert not old.exists()
    kept = old.parent / "kept" / "settings"
    if keep:
        assert kept.read_text() == "from v1"
    else:
        assert not old.parent.exists()


@pytest.mark.parametrize("source", ["official", "another-source"])
def test_reinstall_only_stages_data_kept_by_its_own_source(data_release, service_kind, source):
    from provider_hub import datadir
    from provider_hub.state import load_state, provider_hub_dir

    service, new, _ = data_release
    assert service.remove_installation("exampleservice")
    service.activate_staged_installations()

    service._stage_validated(service_kind.validate_manifest(new, True), True, "catalog", source,
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    key = load_state()["installations"]["exampleservice"]["staged_data_key"]
    staged = datadir.release_path(provider_hub_dir(), "exampleservice", key)
    data = staged / "settings"
    if source == "official":
        assert data.read_text() == "from v1"
    else:
        assert not data.exists()
        assert not (staged.parent / "kept").exists()


def test_a_portable_copy_keeps_an_internal_nested_link_on_its_own_tree(tmp_path):
    from provider_hub import datadir

    source, target = tmp_path / "source", tmp_path / "target"
    (source / "nest").mkdir(parents=True)
    (source / "value").write_text("previous")
    (source / "nest" / "reference").symlink_to("../value")
    datadir.copy_folder(source, target)

    (target / "value").write_text("staged")
    assert (target / "nest" / "reference").read_text() == "staged"
    assert (source / "nest" / "reference").read_text() == "previous"


def test_pruning_preserves_the_oldest_releases_sign_in_exclusions(hub):
    from provider_hub import datadir, secret_paths, state_store
    from provider_hub.state import load_state

    first = _row("alpha", active="1.0.0")
    first["manifest"]["login"] = {"paths": ["session/v1"]}
    first["manifest"]["config_schema"] = {
        "properties": {"region": {"type": "string", "default": "some-value"}},
    }
    _seed(alpha=first)
    _seed(alpha=_row("alpha", active="2.0.0"))
    _seed(alpha=_row("alpha", active="3.0.0"))

    assert {release.version for release in state_store.list_releases("alpha")} == {"2.0.0", "3.0.0"}
    [history] = secret_paths.protected_history(load_state(), "alpha")
    assert ("session", "v1") in datadir.login_paths(history)
    assert history["config_schema"]["properties"] == {"region": {}}


def test_a_pending_restart_rollback_keeps_its_data_in_the_archive(data_release, service_kind, tmp_path):
    from zipfile import ZipFile

    from provider_hub import datadir
    from provider_hub.kinds import ATTACH_ON_RESTART

    service, new, old = data_release
    service._stage_validated(service_kind.validate_manifest(new, True), True, "catalog", "official",
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    assert service.activate_release("exampleservice").activated
    service_kind.attach_policy = ATTACH_ON_RESTART

    assert service.rollback_release("exampleservice").outcome == service.ACTIVATION_WAITING
    path = tmp_path / "data.zip"
    with ZipFile(path, "w") as archive:
        datadir.write_backup(archive)
    with ZipFile(path) as archive:
        assert archive.read(f"plugin_data/exampleservice/{old.name}/settings") == b"from v1"


def test_kept_legacy_data_is_available_after_a_failed_reinstall(data_release):
    from provider_hub import datadir
    from provider_hub.state import provider_hub_dir

    service, _, _ = data_release
    legacy = datadir.legacy_data_path(provider_hub_dir(), "alpha")
    legacy.mkdir(parents=True)
    manifest = {"schema_version": 2, "permissions": ["data_dir"], "kind": "subtitle_provider"}
    kept = {"source_id": "official", "origin": "catalog", "kind": "subtitle_provider"}
    failed = {"state": "failed", "source_id": "official", "origin": "catalog"}

    assert service._stage_data_source("alpha", failed, kept, "official", "subtitle_provider",
                                      "catalog", manifest) == legacy


def test_delete_also_clears_the_legacy_provider_data(staged):
    from provider_hub import datadir, service
    from provider_hub.detach import wait_idle
    from provider_hub.state import provider_hub_dir

    legacy = datadir.legacy_data_path(provider_hub_dir(), "alpha")
    legacy.mkdir(parents=True)
    (legacy / "settings").write_text("synthetic")
    service.remove_installation("alpha", keep_data=False)
    assert wait_idle()
    assert not legacy.exists()


def test_live_uninstall_waits_for_actual_worker_exit_before_moving_data(data_release, service_kind, monkeypatch):
    from provider_hub import detach, worker
    from provider_hub.kinds import DETACH_LIVE
    from provider_hub.state import load_state

    service, _, old = data_release
    service_kind.detach_policy = DETACH_LIVE
    client = object()
    monkeypatch.setattr(detach, "snapshot_workers", lambda provider_id: [client])
    monkeypatch.setattr(detach, "stop_stragglers", lambda clients, cut: None)
    monkeypatch.setattr(worker, "_client_gone", lambda client, budget: False)

    assert service.remove_installation("exampleservice", keep_data=False)
    assert detach.wait_idle()
    row = load_state()["installations"]["exampleservice"]
    assert row["state"] == "removed" and row["pending_restart"]
    assert (old / "settings").read_text() == "from v1"


def test_unreadable_historical_exclusions_omit_that_release_data(data_release, tmp_path):
    import json
    from zipfile import ZipFile

    from provider_hub import datadir
    from provider_hub.state import provider_hub_dir

    _, _, old = data_release
    folder = provider_hub_dir() / "manifests" / "exampleservice"
    folder.mkdir(parents=True)
    (folder / "older.json").write_text("unreadable test metadata")
    archive_path = tmp_path / "data.zip"
    with ZipFile(archive_path, "w") as archive:
        datadir.write_backup(archive)
    with ZipFile(archive_path) as archive:
        assert json.loads(archive.read(datadir.OMISSIONS_ARCHIVE)) == [f"exampleservice/{old.name}"]
        assert not any(name.startswith("plugin_data/") for name in archive.namelist())


def test_boot_waits_for_the_previous_workers_before_finishing_uninstall(data_release, service_kind, monkeypatch):
    from provider_hub import detach, worker
    from provider_hub.kinds import DETACH_LIVE
    from provider_hub.state import load_state

    service, _, old = data_release
    service_kind.detach_policy = DETACH_LIVE
    client = object()
    monkeypatch.setattr(detach, "snapshot_workers", lambda provider_id: [client])
    monkeypatch.setattr(detach, "stop_stragglers", lambda clients, cut: None)
    monkeypatch.setattr(worker, "_client_gone", lambda checked, budget: False)
    assert service.remove_installation("exampleservice", keep_data=False)
    assert detach.wait_idle()
    assert old.exists()

    monkeypatch.setattr(detach, "snapshot_workers", lambda provider_id: [])
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: False)
    service.activate_staged_installations()
    assert old.exists()
    assert load_state()["installations"]["exampleservice"]["pending_restart"]

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: True)
    service.activate_staged_installations()
    assert "exampleservice" not in load_state()["installations"]
    assert not old.exists()


def test_legacy_provider_keep_leaves_data_in_the_home_used_on_reinstall(staged, monkeypatch):
    from provider_hub import datadir, service
    from provider_hub.detach import wait_idle
    from provider_hub.kinds import SUBTITLE_PROVIDER, get_kind
    from provider_hub.state import load_state, provider_hub_dir

    root = provider_hub_dir()
    legacy = datadir.legacy_data_path(root, "alpha")
    legacy.mkdir(parents=True)
    (legacy / "settings").write_text("preserved settings")
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: True)
    assert service.remove_installation("alpha")
    assert wait_idle()
    assert "alpha" not in load_state()["installations"]
    assert (legacy / "settings").read_text() == "preserved settings"

    monkeypatch.setattr(service.PluginEnvironment, "install",
                        lambda self, *args, **kwargs: Path(_python("alpha")).parent.parent)
    kind = get_kind(SUBTITLE_PROVIDER)
    validation = kind.validate_manifest(_release_manifest("alpha", "1.2.0"), True)
    service._stage_validated(validation, True, "catalog", "official",
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    assert service.activate_staged_installations() == ["alpha"]
    assert (legacy / "settings").read_text() == "preserved settings"


def test_legacy_rollback_refuses_to_replace_a_working_release_without_its_data(staged):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import service
    from provider_hub.state import load_state

    old_manifest = provider_manifest_v2("alpha", "1.0.0", permissions=["data_dir"])
    _seed(alpha=_row("alpha", active="1.0.0", manifest=old_manifest))
    _seed(alpha=_row("alpha", active="2.0.0"))

    assert service.rollback_release("alpha").outcome == service.ACTIVATION_WAITING
    assert not service.activate_staged_installations()
    row = load_state()["installations"]["alpha"]
    assert row["active_version"] == "2.0.0"
    assert "data folder is missing" in row["last_error"]


def test_a_failed_restage_clears_the_stage_it_replaced(data_release, service_kind, monkeypatch):
    from provider_hub import datadir
    from provider_hub.state import load_state, provider_hub_dir

    service, new, _ = data_release
    validation = service_kind.validate_manifest(new, True)
    def stage(manifest, deadline):
        return Path(_bundle(manifest.provider_id, manifest.version))

    service._stage_validated(validation, True, "catalog", "official", stage)
    key = load_state()["installations"]["exampleservice"]["staged_data_key"]
    folder = datadir.release_path(provider_hub_dir(), "exampleservice", key)
    assert folder.is_dir()

    def fail(*args, **kwargs):
        raise ValueError("sample check failed")

    monkeypatch.setattr(service_kind, "smoke_test", fail)
    with pytest.raises(service.ProviderHubInstallError, match="sample check failed"):
        service._stage_validated(validation, True, "catalog", "official", stage)
    assert not folder.exists()


def test_boot_activation_waits_until_previous_workers_release_the_active_data(
        data_release, service_kind, monkeypatch):
    from provider_hub.kinds import ATTACH_LIVE
    from provider_hub.state import load_state

    service, new, old = data_release
    service._stage_validated(service_kind.validate_manifest(new, True), True, "catalog", "official",
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    old_file = old / "settings"
    old_file.write_text("last write from v1")
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: False)
    assert service.activate_staged_installations() == []
    row = load_state()["installations"]["exampleservice"]
    assert row["active_version"] == "1.0.0" and row["staged_version"] == "1.1.0"
    assert old_file.read_text() == "last write from v1"

    service_kind.attach_policy = ATTACH_LIVE
    assert not service.activate_release("exampleservice").activated
    assert load_state()["installations"]["exampleservice"]["active_version"] == "1.0.0"

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: True)
    assert service.activate_staged_installations() == ["exampleservice"]
    row = load_state()["installations"]["exampleservice"]
    assert row["active_version"] == "1.1.0"


@pytest.mark.parametrize("keep_data", [True, False])
def test_uninstall_preserves_the_worker_hold_set_by_boot(data_release, service_kind, monkeypatch, keep_data):
    from provider_hub import detach
    from provider_hub.kinds import DETACH_LIVE
    from provider_hub.state import load_state

    service, new, old = data_release
    service_kind.detach_policy = DETACH_LIVE
    service._stage_validated(service_kind.validate_manifest(new, True), True, "catalog", "official",
                             lambda manifest, deadline: Path(_bundle(manifest.provider_id, manifest.version)))
    sample = old / "sample"
    sample.write_text("still in use")
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: False)
    assert service.activate_staged_installations() == []

    monkeypatch.setattr(detach, "snapshot_workers", lambda provider_id: [])
    assert service.remove_installation("exampleservice", keep_data=keep_data)
    assert detach.wait_idle()
    assert load_state()["installations"]["exampleservice"]["state"] == "removed"
    assert sample.read_text() == "still in use"

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda: True)
    service.activate_staged_installations()
    assert "exampleservice" not in load_state()["installations"]
    assert not sample.exists()
    kept = old.parent / "kept" / "sample"
    if keep_data:
        assert kept.read_text() == "still in use"
    else:
        assert not kept.exists()


def test_keep_retry_does_not_replace_the_already_retained_active_data_with_legacy_home(staged, monkeypatch):
    from provider_hub import datadir, service
    from provider_hub.state import provider_hub_dir

    root = provider_hub_dir()
    plugin_id, key = "alpha", "a" * 32
    active = datadir.release_path(root, plugin_id, key)
    legacy = datadir.legacy_data_path(root, plugin_id)
    for folder, value in ((active, "latest data"), (legacy, "old data")):
        folder.mkdir(parents=True)
        (folder / "sample").write_text(value)
    row = _row(plugin_id, active="1.0.0", active_data_key=key)
    original_remove = datadir.remove

    with monkeypatch.context() as interruption:
        def fail_on_legacy(path):
            if path == legacy:
                raise OSError("simulated legacy cleanup failure")
            return original_remove(path)

        interruption.setattr(datadir, "remove", fail_on_legacy)
        with pytest.raises(OSError):
            service._settle_release_data(plugin_id, row, True)
    kept = active.parent / "kept"
    assert not active.exists() and legacy.exists()
    assert (kept / "sample").read_text() == "latest data"

    service._settle_release_data(plugin_id, row, True)
    assert not legacy.exists()
    assert (kept / "sample").read_text() == "latest data"

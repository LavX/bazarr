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


@pytest.mark.parametrize("origin", ["catalog", "local"])
def test_pre_key_provider_stage_reserves_data_identity_before_retry(hub, monkeypatch, origin):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, releases, service, state_store
    from provider_hub.kinds import SUBTITLE_PROVIDER, get_kind
    from provider_hub.state import load_state, provider_hub_dir

    plugin_id = "examplehub"
    old = provider_manifest_v2(plugin_id, "1.0.0", permissions=["data_dir"])
    new = provider_manifest_v2(plugin_id, "1.1.0", permissions=["data_dir"])
    # Development builds before per-release data keys could persist this stage through
    # either the catalogue or local upload, without any deferred-copy metadata.
    _seed(examplehub=_row(
        plugin_id, active="1.0.0", staged="1.1.0", manifest=old, staged_manifest=new,
        origin="local" if origin == "local" else "catalog", source_bound=origin != "local",
        source_id=None if origin == "local" else "official",
        trusted=origin == "catalog"))
    staged, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    row = load_state()["installations"][plugin_id]
    assert staged.data_key is None
    assert "staged_data_from_active" not in row
    legacy = datadir.legacy_data_path(provider_hub_dir(), plugin_id)
    legacy.mkdir(parents=True)
    (legacy / "settings").write_text("last legacy write")

    monkeypatch.setattr(service, "verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr(service, "validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr(service, "validated_interpreter", lambda root, path: Path(path))
    kind = get_kind(SUBTITLE_PROVIDER)
    monkeypatch.setattr(kind, "on_activated", lambda *args: None)
    checked = []

    def smoke(manifest, bundle, interpreter, *, data_dir, timeout=None):
        checked.append((data_dir, (data_dir / "settings").read_text()))
        (data_dir / "settings").write_text("smoke migration")

    monkeypatch.setattr(kind, "smoke_test", smoke)
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: False)
    assert service.activate_staged_installations() == []
    assert not checked
    assert not datadir.plugin_root(provider_hub_dir(), plugin_id).exists()

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
    monkeypatch.setattr(releases, "now_text", lambda: "2099-01-01T00:00:00Z")
    apply_plan = releases.apply_plan

    def interrupt_promotion(connection, plan):
        apply_plan(connection, plan)
        promoting = any(fields.get("state") == releases.ACTIVE and row_id == staged.id
                        for row_id, fields, _ in plan.updates)
        replacing = any(fields.get("state") == releases.ACTIVE and fields.get("plugin_id") == plugin_id
                        and fields.get("version") == "1.1.0" for fields, _ in plan.inserts)
        if promoting or replacing:
            raise RuntimeError("fixture interrupted the promotion")

    with monkeypatch.context() as database:
        database.setattr(releases, "apply_plan", interrupt_promotion)
        assert service.activate_staged_installations() == []
    reserved, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    assert (reserved.id, reserved.created_at) == (staged.id, staged.created_at)
    assert reserved.source_id == staged.source_id
    assert reserved.data_key
    reserved_data = datadir.release_path(provider_hub_dir(), plugin_id, reserved.data_key)
    assert checked == [(reserved_data, "last legacy write")]
    (legacy / "settings").write_text("final legacy write")

    assert service.activate_staged_installations() == [plugin_id]
    active, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert (active.id, active.created_at, active.data_key) == (staged.id, staged.created_at, reserved.data_key)
    assert active.source_id == staged.source_id
    assert checked == [(reserved_data, "last legacy write"), (reserved_data, "final legacy write")]
    assert (legacy / "settings").read_text() == "final legacy write"
    assert service.activate_staged_installations() == []
    assert len(checked) == 2
    assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,))[0] == active


@pytest.mark.parametrize(("bundle_valid", "detach"),
                         [(True, None), (False, None), (True, "removed"), (True, "unbound")],
                         ids=["verified", "changed-bytes", "removed-catalogs", "unbound"])
def test_reserving_data_does_not_activate_a_stage_replaced_by_another_catalogue(
        hub, monkeypatch, bundle_valid, detach):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, releases, service, state_store
    from provider_hub.kinds import SUBTITLE_PROVIDER, get_kind
    from provider_hub.state import load_state, mutate_state, provider_hub_dir

    plugin_id = "examplehub"
    kind = get_kind(SUBTITLE_PROVIDER)
    published = provider_manifest_v2(plugin_id, "1.1.0", permissions=["data_dir"])
    url = "https://github.com/owner/repo/blob/main/catalog.json"
    for source in ("catalog-a", "catalog-b"):
        service.add_catalog_source(source, url)
    monkeypatch.setattr(service, "_fetch_github_catalog", lambda *args, **kwargs: (
        {"plugins": [{"manifest": published}]}, "b" * 40))
    assert service.refresh_catalog(source_ids={"catalog-a", "catalog-b"})["entries"] == 2
    entries = load_state()["catalog_entries"]
    published_a = entries["catalog-a:plugins:examplehub:1.1.0"]["manifest"]
    published_b = entries["catalog-b:plugins:examplehub:1.1.0"]["manifest"]
    assert published_a == published_b and "provider_id" not in published_b
    manifest_a = kind.validate_manifest(published_a, False).raw
    manifest_b = kind.validate_manifest(published_b, False).raw
    assert manifest_b["provider_id"] == plugin_id
    old = {**manifest_a, "version": "1.0.0"}
    _seed(examplehub=_row(plugin_id, active="1.0.0", staged="1.1.0", manifest=old,
                          staged_manifest=manifest_a, source_id="catalog-a", catalog_list="plugins",
                          active_data_key="a" * 32))
    bundle = service._bundle_path_for(kind.validate_manifest(published_b, False))
    bundle.mkdir(parents=True)
    (bundle / "provider.py").write_bytes(b"class ExampleProvider: pass\n")
    mutate_state(lambda state: state["installations"][plugin_id].update(staged_path=str(bundle)))
    original, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    active_data = datadir.release_path(provider_hub_dir(), plugin_id, "a" * 32)
    active_data.mkdir(parents=True)
    (active_data / "settings").write_text("private catalog A data")

    # Reproduce the historical writer only at the final staging boundary. Normal
    # stage_install still resolves and validates the exact configured B entry.
    def historical_stage(validated, trusted, origin, source_id, bundle_stager, **kwargs):
        assert validated.raw == manifest_b and source_id == "catalog-b"

        def record(state):
            row = service._staged_installation(
                validated, state["installations"][plugin_id], bundle,
                Path(_python(plugin_id, "1.1.0")), trusted, origin, source_id, kwargs["catalog_list"])
            state["installations"][plugin_id] = row
            return dict(row)

        return mutate_state(record)

    reserve = service._reserve_staged_data_key

    def replace_before_reservation(provider_id, installation, *args):
        with monkeypatch.context() as historical:
            historical.setattr(service, "_stage_validated", historical_stage)
            service.stage_install(published_b, source_id="catalog-b", catalog_list="plugins")
        return reserve(provider_id, installation, *args)

    with monkeypatch.context() as racing:
        racing.setattr(service, "_reserve_staged_data_key", replace_before_reservation)
        racing.setattr(datadir, "copy_folder", lambda *args: pytest.fail("copied during stale activation"))
        racing.setattr(service, "_active_data_path", lambda *args: pytest.fail("read during stale activation"))
        racing.setattr(kind, "smoke_test", lambda *args, **kwargs: pytest.fail("smoked the stale stage"))
        result = service.activate_release(plugin_id, at_boot=True)

    assert result.outcome == service.ACTIVATION_SKIPPED
    row = load_state()["installations"][plugin_id]
    assert row["state"] == "staged" and row["staged_version"] == "1.1.0"
    assert row["source_id"] == "catalog-b" and row["catalog_list"] == "plugins"
    assert not row.get("staged_data_key")
    restaged, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    assert restaged.id == original.id and restaged.data_key is None
    previous, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    if detach == "removed":
        assert service.remove_catalog_source("catalog-a")
        assert service.remove_catalog_source("catalog-b")
        detached = load_state()
        assert not ({"catalog-a", "catalog-b"} & detached["catalog_sources"].keys())
        assert "catalog-b:plugins:examplehub:1.1.0" not in detached["catalog_entries"]
        row = detached["installations"][plugin_id]
        assert row["source_id"] is None and row["source_bound"] is True
    elif detach == "unbound":
        mutate_state(lambda state: state["installations"][plugin_id].update(
            source_id=None, source_bound=False))

    monkeypatch.setattr(service, "validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr(service, "validated_interpreter", lambda root, path: Path(path))
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
    monkeypatch.setattr(kind, "on_activated", lambda *args: None)
    if detach or not bundle_valid:
        if not bundle_valid:
            (bundle / "provider.py").write_bytes(b"changed after historical staging\n")
        with monkeypatch.context() as refusing:
            refusing.setattr(service, "_active_data_path", lambda *args: pytest.fail("read before stage validation"))
            refusing.setattr(datadir, "copy_folder", lambda *args: pytest.fail("copied an unverifiable stage's data"))
            refusing.setattr(kind, "smoke_test", lambda *args, **kwargs: pytest.fail("smoked an unverifiable stage"))
            assert service.activate_release(plugin_id, at_boot=True).outcome == service.ACTIVATION_FAILED
            failed, = state_store.list_releases(plugin_id, states=(releases.FAILED,))
            assert failed.id == original.id and failed.data_key is None
            row = load_state()["installations"][plugin_id]
            assert not row.get("pending_restart") and not row.get("staged_data_key")
            assert not row.get("staged_version") and not row.get("staged_manifest")
            assert row["active_version"] == "1.0.0" and row["active_data_key"] == "a" * 32
            assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,)) == [previous]
            assert (active_data / "settings").read_text() == "private catalog A data"
            assert service.activate_staged_installations() == []
            assert service.activate_release(plugin_id, at_boot=True).outcome == service.ACTIVATION_SKIPPED
            jobs = [job for job in service.list_jobs() if job["action"] == "activate"]
            assert [job["state"] for job in jobs] == ["failed"]
        if detach == "unbound":
            return
        (bundle / "provider.py").write_bytes(b"class ExampleProvider: pass\n")

    rebind = bundle_valid and detach is None
    target_source = "catalog-b"
    if detach == "removed":
        # Adding a source under the old name creates a new owner, even at the same URL.
        target_source = "catalog-a"
        service.add_catalog_source(target_source, url)
        assert service.refresh_catalog(source_ids={target_source})["entries"] == 1

    copied = []
    copy_folder = datadir.copy_folder

    def copy(source, target):
        if rebind:
            persisted, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
            assert persisted.source_id == "catalog-b" and persisted.manifest == manifest_b
        assert source is None
        copied.append(target)
        return copy_folder(source, target)

    checked = []

    def smoke(manifest, bundle, interpreter, *, data_dir, timeout=None):
        assert manifest.raw == manifest_b
        assert not (data_dir / "settings").exists()
        checked.append(data_dir)

    monkeypatch.setattr(datadir, "copy_folder", copy)
    monkeypatch.setattr(kind, "smoke_test", smoke)
    if not rebind:
        monkeypatch.setattr(service, "_fetch_bundle", lambda *args: bundle)
        monkeypatch.setattr(service.PluginEnvironment, "install", lambda *args, **kwargs: (
            Path(_python(plugin_id, "1.1.0")).parent.parent))
        service.stage_install(published_b, source_id=target_source, catalog_list="plugins")
        row = load_state()["installations"][plugin_id]
        assert row["source_id"] == target_source and row["staged_data_key"]
        assert len(copied) == len(checked) == 1
        monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: False)
        assert service.activate_staged_installations() == []
        assert len(copied) == len(checked) == 1
        monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
    assert service.activate_staged_installations() == [plugin_id]
    active, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert (active.id == original.id) is rebind
    assert active.source_id == target_source and active.manifest == manifest_b
    assert active.data_key
    target = datadir.release_path(provider_hub_dir(), plugin_id, active.data_key)
    assert copied == [target]
    assert checked == [target] * (1 if rebind else 2)
    assert (active_data / "settings").read_text() == "private catalog A data"
    assert service.activate_staged_installations() == []
    assert len(copied) == 1 and len(checked) == (1 if rebind else 2)
    assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,))[0] == active


@pytest.mark.parametrize("catalog", ["unconfigured", "unlisted", "different-bundle"])
def test_an_unsupported_pre_key_stage_owner_conflict_fails_before_reading_or_copying_data(hub, monkeypatch, catalog):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, releases, service, state_store
    from provider_hub.kinds import SUBTITLE_PROVIDER, get_kind
    from provider_hub.state import load_state, mutate_state

    plugin_id = "examplehub"
    manifest = provider_manifest_v2(plugin_id, "1.1.0", permissions=["data_dir"])
    _seed(examplehub=_row(plugin_id, staged="1.1.0", staged_manifest=manifest, catalog_list="plugins"))
    original, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    def conflict(state):
        state["installations"][plugin_id]["source_id"] = "catalog-b"
        if catalog != "unconfigured":
            state["catalog_sources"]["catalog-b"] = {
                "id": "catalog-b", "name": "Catalog B", "enabled": True, "trusted": False}
        if catalog == "different-bundle":
            other = provider_manifest_v2(plugin_id, "1.1.0", permissions=["data_dir"],
                                         file_payloads={"provider.py": b"class OtherProvider: pass\n"})
            state["catalog_entries"]["catalog-b:plugins:examplehub:1.1.0"] = {
                "source": "catalog-b", "provider_id": plugin_id, "version": "1.1.0",
                "catalog_list": "plugins", "manifest": other, "trusted": False}

    mutate_state(conflict)
    monkeypatch.setattr(service, "_active_data_path", lambda *args: pytest.fail("read conflicting owner data"))
    monkeypatch.setattr(datadir, "copy_folder", lambda *args: pytest.fail("copied conflicting owner data"))
    monkeypatch.setattr(get_kind(SUBTITLE_PROVIDER), "smoke_test", lambda *args, **kwargs: pytest.fail(
        "smoked a stage with conflicting ownership"))

    result = service.activate_release(plugin_id, at_boot=True)

    assert result.outcome == service.ACTIVATION_FAILED
    row = load_state()["installations"][plugin_id]
    assert row["state"] == "failed" and not row.get("pending_restart")
    assert not row.get("staged_version") and not row.get("staged_data_key")
    failed, = state_store.list_releases(plugin_id, states=(releases.FAILED,))
    assert failed.id == original.id and failed.data_key is None
    assert service.activate_release(plugin_id, at_boot=True).outcome == service.ACTIVATION_SKIPPED


@pytest.fixture
def catalog_data_owner(hub, monkeypatch):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, service
    from provider_hub.kinds import SUBTITLE_PROVIDER, get_kind
    from provider_hub.state import load_state, provider_hub_dir

    plugin_id = "examplehub"
    url = "https://github.com/owner/repo/blob/main/catalog.json"
    published = provider_manifest_v2(plugin_id, "1.1.0", permissions=["data_dir"])
    for source in ("catalog-a", "catalog-b"):
        service.add_catalog_source(source, url)
    monkeypatch.setattr(service, "_fetch_github_catalog", lambda *args, **kwargs: (
        {"plugins": [{"manifest": published}]}, "b" * 40))
    assert service.refresh_catalog(source_ids={"catalog-a", "catalog-b"})["entries"] == 2
    kind = get_kind(SUBTITLE_PROVIDER)
    published = load_state()["catalog_entries"]["catalog-a:plugins:examplehub:1.1.0"]["manifest"]
    manifest = kind.validate_manifest(published, False).raw
    _seed(examplehub=_row(
        plugin_id, active="1.0.0", manifest={**manifest, "version": "1.0.0"},
        source_id="catalog-a", catalog_list="plugins", active_data_key="a" * 32))
    active = datadir.release_path(provider_hub_dir(), plugin_id, "a" * 32)
    active.mkdir(parents=True)
    (active / "settings").write_text("private catalog A data")
    monkeypatch.setattr(service, "verify_bundle_tree", lambda *args: None)
    monkeypatch.setattr(service, "validated_bundle_path", lambda root, path: Path(path))
    monkeypatch.setattr(service, "validated_interpreter", lambda root, path: Path(path))
    monkeypatch.setattr(service, "_fetch_bundle", lambda *args: Path(_bundle(plugin_id, "1.1.0")))
    monkeypatch.setattr(service.PluginEnvironment, "install", lambda *args, **kwargs: (
        Path(_python(plugin_id, "1.1.0")).parent.parent))
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
    monkeypatch.setattr(kind, "smoke_test", lambda *args, **kwargs: None)
    monkeypatch.setattr(kind, "on_activated", lambda *args: None)
    monkeypatch.setattr(kind, "settle_data", lambda *_: True)
    return service, kind, published, active, url


def test_stage_install_refuses_a_changed_registration_before_copy_or_build(catalog_data_owner, monkeypatch):
    from provider_hub import datadir
    from provider_hub.state import load_state

    service, kind, published, active, url = catalog_data_owner
    provider_id = "examplehub"
    source_id = "catalog-a"
    registration = load_state()["catalog_sources"][source_id]["registration"]
    current = load_state()["installations"][provider_id]

    def replace_catalog(*_):
        assert service.remove_catalog_source(source_id)
        service.add_catalog_source(source_id, url)
        return True

    def unexpected(*args, **kwargs):
        pytest.fail("the old registration must not copy data or build an environment")

    monkeypatch.setattr(kind, "settle_data", replace_catalog)
    monkeypatch.setattr(datadir, "copy_folder", unexpected)
    monkeypatch.setattr(service.PluginEnvironment, "install", unexpected)

    with pytest.raises(service.ProviderHubInstallError, match="catalog registration changed"):
        service.stage_install(published, source_id=source_id, catalog_list="plugins")

    state = load_state()
    row = state["installations"][provider_id]
    assert state["catalog_sources"][source_id]["registration"] != registration
    assert row["active_version"] == current["active_version"]
    assert row["active_data_key"] == active.name and row["staged_version"] is None
    assert list(active.parent.iterdir()) == [active]
    assert (active / "settings").read_text() == "private catalog A data"


@pytest.mark.parametrize("already_active", [False, True])
def test_reinstalling_the_same_bundle_uses_the_new_catalog_registration(catalog_data_owner, monkeypatch, already_active):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import releases, state_store
    from provider_hub.state import load_state

    service, _, _, _, url = catalog_data_owner
    plugin_id = "examplehub"
    source = "catalog-a"
    published = provider_manifest_v2(plugin_id, "1.1.0")
    monkeypatch.setattr(service, "_fetch_github_catalog", lambda *args, **kwargs: (
        {"plugins": [{"manifest": published}]}, "b" * 40))

    def stage():
        entry = load_state()["catalog_entries"][f"{source}:plugins:{plugin_id}:1.1.0"]
        service.stage_install(entry["manifest"], source_id=source, catalog_list="plugins")
        return state_store.list_releases(plugin_id, states=(releases.STAGED,))[0]

    assert service.refresh_catalog(source_ids={source})["entries"] == 1
    first = stage()
    assert first.data_key is None
    assert stage().id == first.id
    if already_active:
        assert service.activate_release(plugin_id, at_boot=True).activated

    assert service.remove_catalog_source(source)
    service.add_catalog_source(source, url)
    assert service.refresh_catalog(source_ids={source})["entries"] == 1
    fresh = stage()
    assert fresh.bundle_path == first.bundle_path
    assert fresh.catalog_registration != first.catalog_registration
    assert stage().id == fresh.id
    assert service.activate_release(plugin_id, at_boot=True).activated
    active, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert (active.id, active.catalog_registration) == (fresh.id, fresh.catalog_registration)
    stage()
    assert service.activate_release(plugin_id, at_boot=True).activated
    assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,))[0].id == active.id


@pytest.mark.parametrize("unbound_stage", [False, True])
def test_approved_reinstall_binds_a_legacy_catalog_release_for_rollback(
        catalog_data_owner, hub, monkeypatch, unbound_stage):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import releases, state_store
    from provider_hub.state import mutate_state

    service, _, _, _, _ = catalog_data_owner
    plugin_id, source = "examplehub", "catalog-a"
    old_version, new_version = "1.1.0", "1.2.0"

    def publish(version, commit):
        manifest = provider_manifest_v2(plugin_id, version)
        monkeypatch.setattr(service, "_fetch_github_catalog", lambda *args, **kwargs: (
            {"plugins": [{"manifest": manifest}]}, commit * 40))
        assert service.refresh_catalog(source_ids={source})["entries"] == 1

    def install(version):
        consent = service.install_consent(source, plugin_id, version)
        service.install_catalog_entry(source, plugin_id, version, consent_digest=consent["digest"])

    publish(old_version, "b")
    install(old_version)
    assert service.activate_staged_installations() == [plugin_id]
    if unbound_stage:
        install(old_version)
    # Recreate an installation last written before registrations were recorded.
    mutate_state(lambda state: state["catalog_sources"][source].pop("registration"))
    table = releases.releases_table
    with hub.begin() as connection:
        for saved in releases.read_rows(connection, plugin_id=plugin_id, states=releases.SLOT_STATES):
            fields = [part for part in json.loads(saved["fields"]) if not part.startswith("catalog_registration:")]
            connection.execute(table.update().where(table.c.id == saved["id"]).values(fields=json.dumps(fields)))
    old_active, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert old_active.catalog_registration is None

    install(old_version)
    checked_stage, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    assert checked_stage.bundle_path == old_active.bundle_path
    assert checked_stage.data_key is old_active.data_key is None
    assert checked_stage.catalog_registration == "legacy"
    assert service.activate_staged_installations() == [plugin_id]
    active, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert active.id == checked_stage.id and active.catalog_registration == "legacy"

    publish(new_version, "c")
    monkeypatch.setattr(service, "_fetch_bundle", lambda manifest, deadline: Path(_bundle(plugin_id, manifest.version)))
    assert service.apply_update(plugin_id)["staged_version"] == new_version
    assert service.activate_staged_installations() == [plugin_id]
    assert service.rollback_release(plugin_id).outcome == service.ACTIVATION_WAITING
    assert service.activate_staged_installations() == [plugin_id]
    restored, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert (restored.id, restored.version, restored.catalog_registration) == (active.id, old_version, "legacy")


@pytest.mark.parametrize("action", ["restage", "keep", "activate"])
def test_removing_only_the_staged_catalog_preserves_the_active_catalog_data_owner(
        catalog_data_owner, monkeypatch, action):
    from provider_hub import datadir, releases, state_store
    from provider_hub.state import load_state, provider_hub_dir

    service, kind, published, active_data, _ = catalog_data_owner
    service.stage_install(published, source_id="catalog-b", catalog_list="plugins")
    active, = state_store.list_releases("examplehub", states=(releases.ACTIVE,))
    staged, = state_store.list_releases("examplehub", states=(releases.STAGED,))
    assert active.source_id == "catalog-a" and staged.source_id == "catalog-b"

    assert service.remove_catalog_source("catalog-b")
    assert "catalog-a" in load_state()["catalog_sources"]
    assert state_store.list_releases("examplehub", states=(releases.ACTIVE,)) == [active]
    if action == "restage":
        service.stage_install(published, source_id="catalog-a", catalog_list="plugins")
        row = load_state()["installations"]["examplehub"]
        staged_data = datadir.release_path(provider_hub_dir(), "examplehub", row["staged_data_key"])
        assert (staged_data / "settings").read_text() == "private catalog A data"
        assert (active_data / "settings").read_text() == "private catalog A data"
    elif action == "keep":
        assert service.remove_installation("examplehub", keep_data=True)
        service.activate_staged_installations()
        state = load_state()
        assert "examplehub" not in state["installations"]
        assert state["kept_data"]["examplehub"]["source_id"] == "catalog-a"
        assert (active_data.parent / "kept" / "settings").read_text() == "private catalog A data"
        service.stage_install(published, source_id="catalog-a", catalog_list="plugins")
        row = load_state()["installations"]["examplehub"]
        staged_data = datadir.release_path(provider_hub_dir(), "examplehub", row["staged_data_key"])
        assert (staged_data / "settings").read_text() == "private catalog A data"
    else:
        monkeypatch.setattr(datadir, "copy_folder", lambda *args: pytest.fail("copied removed staged catalog data"))
        monkeypatch.setattr(kind, "smoke_test", lambda *args, **kwargs: pytest.fail("smoked removed staged catalog"))
        assert service.activate_release("examplehub", at_boot=True).outcome == service.ACTIVATION_FAILED
        failed, = state_store.list_releases("examplehub", states=(releases.FAILED,))
        assert failed.id == staged.id
        assert state_store.list_releases("examplehub", states=(releases.ACTIVE,)) == [active]
        assert service.activate_release("examplehub", at_boot=True).outcome == service.ACTIVATION_SKIPPED
        assert [job["state"] for job in service.list_jobs() if job["action"] == "activate"] == ["failed"]
        assert (active_data / "settings").read_text() == "private catalog A data"


@pytest.mark.parametrize("unknown_owner", [False, True])
def test_a_failed_restage_restores_only_a_known_active_catalog(catalog_data_owner, monkeypatch, unknown_owner):
    from provider_hub import datadir
    from provider_hub.state import load_state, provider_hub_dir

    service, kind, published, active_data, _ = catalog_data_owner
    plugin_id = "examplehub"
    if unknown_owner:
        row = load_state()["installations"][plugin_id]
        _seed(examplehub={**row, "active_path": _bundle(plugin_id, "old-layout"),
                          "source_id": None, "origin": "catalog", "source_bound": True, "trusted": False})
    service.stage_install(published, source_id="catalog-b", catalog_list="plugins")

    def refuse(*args, **kwargs):
        raise RuntimeError("the replacement did not start")

    with monkeypatch.context() as failing:
        failing.setattr(kind, "smoke_test", refuse)
        with pytest.raises(service.ProviderHubInstallError, match="replacement did not start"):
            service.stage_install(published, source_id="catalog-b", catalog_list="plugins")

    row = load_state()["installations"][plugin_id]
    assert row["state"] == "active" and not row["staged_version"]
    assert row["source_bound"] is True
    assert row["source_id"] == (None if unknown_owner else "catalog-a")
    service.stage_install(published, source_id="catalog-a", catalog_list="plugins")
    row = load_state()["installations"][plugin_id]
    staged_data = datadir.release_path(provider_hub_dir(), plugin_id, row["staged_data_key"])
    assert (staged_data / "settings").exists() is not unknown_owner
    assert (active_data / "settings").read_text() == "private catalog A data"


def test_an_old_unbound_staged_folder_is_not_used_by_a_bound_installation(catalog_data_owner, hub, monkeypatch):
    from provider_hub import datadir, releases, state_store
    from provider_hub.state import load_state, provider_hub_dir

    service, kind, published, active_data, _ = catalog_data_owner
    plugin_id = "examplehub"
    service.stage_install(published, source_id="catalog-b", catalog_list="plugins")
    staged, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    path = datadir.release_path(provider_hub_dir(), plugin_id, staged.data_key)
    (path / "settings").write_text("data from an unbound stage")
    table = releases.releases_table
    with hub.begin() as connection:
        saved = releases.read_rows(connection, plugin_id=plugin_id, states=(releases.STAGED,))[0]
        fields = [field for field in json.loads(saved["fields"]) if not field.startswith("catalog_registration:")]
        connection.execute(table.update().where(table.c.id == staged.id).values(
            source_id=None, fields=json.dumps(fields)))

    monkeypatch.setattr(kind, "smoke_test", lambda *args, **kwargs: pytest.fail("used unbound staged data"))
    assert service.activate_release(plugin_id, at_boot=True).outcome == service.ACTIVATION_FAILED
    row = load_state()["installations"][plugin_id]
    assert row["source_id"] == "catalog-a" and row["state"] == "active"
    assert (active_data / "settings").read_text() == "private catalog A data"


@pytest.mark.parametrize("registration", ["same", "readded", "unrecorded"])
def test_rollback_restores_only_its_own_catalog_registrations_data(
        catalog_data_owner, hub, monkeypatch, registration):
    from provider_hub import datadir, releases, state_store
    from provider_hub.state import load_state, mutate_state, provider_hub_dir

    service, kind, published, old_data, url = catalog_data_owner
    former, = state_store.list_releases("examplehub", states=(releases.ACTIVE,))
    if registration == "readded":
        assert service.remove_catalog_source("catalog-a")
        service.add_catalog_source("catalog-a", url)
        assert service.refresh_catalog(source_ids={"catalog-a"})["entries"] == 1
    elif registration == "same":
        service.add_catalog_source("catalog-a", url)
    service.stage_install(published, source_id="catalog-a", catalog_list="plugins")
    row = load_state()["installations"]["examplehub"]
    new_data = datadir.release_path(provider_hub_dir(), "examplehub", row["staged_data_key"])
    assert (new_data / "settings").exists() == (registration != "readded")
    assert service.activate_staged_installations() == ["examplehub"]
    if registration == "same":
        (new_data / "settings").write_text("new release data")
        assert service.rollback_release("examplehub").outcome == service.ACTIVATION_WAITING
        assert service.activate_staged_installations() == ["examplehub"]
        active_key = load_state()["installations"]["examplehub"]["active_data_key"]
        assert active_key == old_data.name
        assert (datadir.release_path(provider_hub_dir(), "examplehub", active_key) / "settings").read_text() == (
            "private catalog A data")
        return
    if registration == "unrecorded":
        # Simulate a state saved before registrations were tracked. The old
        # release's source name alone cannot establish who owned its data.
        mutate_state(lambda state: state["catalog_sources"]["catalog-a"].pop("registration"))
        with hub.begin() as connection:
            for release in releases.read_rows(connection, plugin_id="examplehub"):
                fields = [field for field in json.loads(release["fields"])
                          if not field.startswith("catalog_registration:")]
                table = releases.releases_table
                connection.execute(table.update().where(table.c.id == release["id"]).values(fields=json.dumps(fields)))
    active, = state_store.list_releases("examplehub", states=(releases.ACTIVE,))
    previous, = state_store.list_releases("examplehub", states=(releases.RETIRED,))
    assert previous.id == former.id
    before = load_state()["installations"]["examplehub"]
    monkeypatch.setattr(datadir, "copy_folder", lambda *args: pytest.fail("copied a removed catalog's data"))
    monkeypatch.setattr(kind, "smoke_test", lambda *args, **kwargs: pytest.fail("smoked a removed catalog's data"))

    with pytest.raises(service.ProviderHubInstallError):
        service.rollback_release("examplehub")

    assert load_state()["installations"]["examplehub"] == before
    assert state_store.list_releases("examplehub", states=(releases.ACTIVE,)) == [active]
    assert state_store.list_releases("examplehub", states=(releases.RETIRED,)) == [previous]
    assert (old_data / "settings").read_text() == "private catalog A data"


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
        assert 0 < timeout <= 10.0
        return False

    def unexpected_install(*args, **kwargs):
        pytest.fail("the refused snapshot must not start an environment install")

    monkeypatch.setattr(datadir, "QUIESCE_TIMEOUT", 10.0)
    monkeypatch.setattr(service_kind, "settle_data", settle)
    monkeypatch.setattr(service.PluginEnvironment, "install", unexpected_install)
    if busy_at == "dispatch":
        # Report a drain timeout while the in-flight dispatch still owns the gate.
        monkeypatch.setattr(datadir._gate, "wait_for", lambda predicate, timeout=None: predicate())
    dispatch = datadir.dispatch(plugin_id) if busy_at == "dispatch" else nullcontext()
    with dispatch, pytest.raises(service.ProviderHubInstallError, match="busy"):
        service.stage_install(manifest, source_id="official")

    assert settled == ([plugin_id] if busy_at == "settle" else [])
    row = load_state()["installations"][plugin_id]
    assert row["active_version"] == "1.0.0" and row["staged_version"] is None
    assert list(active.parent.iterdir()) == [active]
    assert (active / "settings").read_text() == "from v1"


@pytest.mark.parametrize("reason", ["previous workers", "release changed"])
def test_stage_install_checks_snapshot_preconditions_before_environment_install(
        catalog_data_release, service_kind, probe_process_evidence, monkeypatch, reason):
    from provider_hub.state import load_state, mutate_state

    service, manifest, active = catalog_data_release
    plugin_id = manifest["id"]
    expected = load_state()["installations"][plugin_id]
    if reason == "previous workers":
        probe_process_evidence["partial"] = True
        message = "previous plugin workers stopped"
    else:
        changed_key = "b" * 32

        def settle(*_):
            mutate_state(lambda state: state["installations"][plugin_id].update(active_data_key=changed_key))
            return True

        monkeypatch.setattr(service_kind, "settle_data", settle)
        expected = {**expected, "active_data_key": changed_key}
        message = "release changed"

    def unexpected_install(*args, **kwargs):
        pytest.fail("the refused snapshot must not start an environment install")

    monkeypatch.setattr(service.PluginEnvironment, "install", unexpected_install)
    with pytest.raises(service.ProviderHubInstallError, match=message):
        service.stage_install(manifest, source_id="official")

    assert load_state()["installations"][plugin_id] == expected
    assert list(active.parent.iterdir()) == [active]


def test_fresh_stage_install_needs_no_active_writer_proof(
        catalog_data_release, service_kind, probe_process_evidence, monkeypatch):
    from provider_hub.state import mutate_state

    service, manifest, previous = catalog_data_release
    plugin_id = manifest["id"]
    mutate_state(lambda state: state["installations"].pop(plugin_id))
    probe_process_evidence["partial"] = True
    checked = []

    def smoke(validated, bundle, interpreter, *, data_dir, timeout):
        assert data_dir.is_dir() and not list(data_dir.iterdir())
        checked.append(data_dir)

    monkeypatch.setattr(service_kind, "smoke_test", smoke)
    staged = service.stage_install(manifest, source_id="official")
    assert staged["staged_version"] == manifest["version"]
    assert staged["active_version"] is None
    assert len(checked) == 1 and checked[0].exists()
    assert probe_process_evidence["scans"] == 0
    assert (previous / "settings").read_text() == "from v1"


def test_stage_install_does_not_copy_when_the_install_budget_expires_while_settling(
        catalog_data_release, service_kind, monkeypatch):
    from types import SimpleNamespace

    from provider_hub import datadir
    from provider_hub.sandbox import sweep

    service, manifest, active = catalog_data_release
    now = 0.0
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now, perf_counter=service.time.perf_counter))
    monkeypatch.setattr(sweep, "time", service.time)

    def settle(plugin_id, timeout):
        nonlocal now
        assert 0 < timeout <= 10.0
        now = 10.0
        return True

    def unexpected_copy(*args):
        pytest.fail("the snapshot started after the install deadline")

    monkeypatch.setattr(service_kind, "settle_data", settle)
    monkeypatch.setattr(datadir, "copy_folder", unexpected_copy)
    with pytest.raises(service.ProviderHubInstallError, match="time budget|timed out"):
        service.stage_install(manifest, source_id="official", install_timeout=10.0)
    assert list(active.parent.iterdir()) == [active]


def test_connection_probe_waits_for_active_data_and_reopens_dispatch(hub, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, service
    from provider_hub.kinds.subtitle_provider import SUBTITLE_PROVIDER_KIND
    from provider_hub.state import load_state, provider_hub_dir
    from provider_hub.worker import ProviderWorkerClient

    plugin_id = "examplehub"
    active = datadir.release_path(provider_hub_dir(), plugin_id, "a" * 32)
    active.mkdir(parents=True)
    settings = active / "settings"
    settings.write_text("writing")
    _seed(examplehub=_row(plugin_id, active="1.0.0", active_data_key=active.name,
                          manifest=provider_manifest_v2(permissions=["data_dir"])))
    monkeypatch.setattr(service, "verify_bundle_tree", lambda *_: None)

    attempted, dispatched = Event(), Event()
    live = ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})
    monkeypatch.setattr(live, "_request", lambda *_: dispatched.set())

    def call_worker():
        attempted.set()
        live.request("health", {})

    can_settle = False

    def settle(checked_id, timeout):
        assert checked_id == plugin_id
        assert 0 < timeout <= service.PROBE_TIMEOUT
        if can_settle:
            settings.write_text("settled")
        return can_settle

    copy_folder = datadir.copy_folder

    def copy(source, target, **kwargs):
        assert source == active
        executor.submit(call_worker)
        assert attempted.wait(2)
        assert not dispatched.wait(0.05)
        return copy_folder(source, target, **kwargs)

    probed = []

    class ProbeClient(ProviderWorkerClient):
        def _request(self, op, *_):
            assert op == "health"
            probed.append(Path(self.env["BAZARR_PLUGIN_DATA_DIR"]))
            assert dispatched.wait(2)
            copied_settings = probed[-1] / "settings"
            assert copied_settings.read_text() == "settled"
            copied_settings.write_text("probe write")
            return SimpleNamespace(payload={"initialized": True})

    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "settle_data", settle)
    monkeypatch.setattr(datadir, "copy_folder", copy)
    monkeypatch.setattr(service, "ProviderWorkerClient", ProbeClient)

    with ThreadPoolExecutor(max_workers=1) as executor:
        failed = service.test_provider_connection(plugin_id)
        assert not failed["ok"] and "busy" in failed["message"]
        assert not attempted.is_set() and not probed
        assert not load_state()["installations"][plugin_id].get("needs_repair")
        assert list(active.parent.iterdir()) == [active]

        can_settle = True
        assert service.test_provider_connection(plugin_id)["ok"]

    assert len(probed) == 1 and not probed[0].exists()
    assert settings.read_text() == "settled"


@pytest.fixture(autouse=True)
def probe_process_evidence(monkeypatch):
    """Never scan or stop real processes while testing release state transitions."""
    from provider_hub import env_registry, venv
    from provider_hub.sandbox import sweep

    monkeypatch.setattr(venv, "processes_using_directory", lambda *args, **kwargs: False)
    monkeypatch.setattr(env_registry, "processes_using_directory", lambda *args, **kwargs: False)
    monkeypatch.setattr(sweep, "_has_proc", lambda: True)
    monkeypatch.setattr(sweep, "_proc_is_ours", lambda: True)
    monkeypatch.setattr(sweep, "_pidfd_available", lambda: True)
    evidence = {"partial": False, "unreadable": 0, "sessions": [], "launchers": {}, "scans": 0}

    def scan(instance, deadline, result):
        evidence["scans"] += 1
        result.truncated = evidence["partial"]
        result.unreadable = evidence["unreadable"]
        result.sessions.extend(evidence["sessions"])
        result.launchers.update(evidence["launchers"])
        return {}, {}

    monkeypatch.setattr(sweep, "_scan", scan)
    return evidence


@pytest.fixture
def isolated_probe_release(hub, monkeypatch, probe_process_evidence):
    """A real release with process discovery confined to test-owned evidence."""
    from threading import Event

    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import datadir, service
    from provider_hub.kinds import DETACH_ON_RESTART
    from provider_hub.kinds.subtitle_provider import SUBTITLE_PROVIDER_KIND
    from provider_hub.state import provider_hub_dir

    plugin_id = "examplehub"
    active = datadir.release_path(provider_hub_dir(), plugin_id, "a" * 32)
    active.mkdir(parents=True)
    (active / "settings").write_text("before request")
    _seed(examplehub=_row(plugin_id, active="1.0.0", active_data_key=active.name,
                          manifest=provider_manifest_v2(permissions=["data_dir"])))
    monkeypatch.setattr(service, "verify_bundle_tree", lambda *_: None)
    # Keep public removal tests on the release store without loading app settings or search pools.
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "is_enabled", lambda *_: False)
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "on_removed", lambda *_: True)
    monkeypatch.setattr(SUBTITLE_PROVIDER_KIND, "detach_policy", DETACH_ON_RESTART)
    waiting = Event()
    wait_for = datadir._gate.wait_for

    def observe_wait(predicate, timeout=None):
        if plugin_id in datadir._paused and plugin_id in datadir._entering:
            waiting.set()
        return wait_for(predicate, timeout)

    monkeypatch.setattr(datadir._gate, "wait_for", observe_wait)
    return service, plugin_id, active, waiting, probe_process_evidence


def test_connection_probe_drains_a_request_already_in_flight(isolated_probe_release, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from provider_hub import datadir
    from provider_hub.worker import ProviderWorkerClient

    service, plugin_id, active, waiting, evidence = isolated_probe_release
    entered, finish = Event(), Event()
    live = ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})

    def request(*_):
        entered.set()
        assert finish.wait(5)
        (active / "settings").write_text("last in-flight write")

    monkeypatch.setattr(live, "_request", request)
    copied = Event()
    copy_folder = datadir.copy_folder

    def copy(source, target, **kwargs):
        assert source == active and finish.is_set()
        copied.set()
        return copy_folder(source, target, **kwargs)

    class ProbeClient(ProviderWorkerClient):
        def _request(self, *_):
            data = Path(self.env["BAZARR_PLUGIN_DATA_DIR"])
            assert copied.is_set()
            assert (data / "settings").read_text() == "last in-flight write"
            (data / "settings").write_text("isolated probe write")
            return SimpleNamespace(payload={"initialized": True})

    monkeypatch.setattr(datadir, "copy_folder", copy)
    monkeypatch.setattr(service, "ProviderWorkerClient", ProbeClient)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(live.request, "health", {})
        try:
            assert entered.wait(2)
            probe = pool.submit(service.test_provider_connection, plugin_id)
            assert waiting.wait(2)
            assert not copied.is_set() and not probe.done()
        finally:
            finish.set()
        running.result(timeout=3)
        assert probe.result(timeout=3)["ok"]
    assert (active / "settings").read_text() == "last in-flight write"
    assert evidence["scans"] > 0


@pytest.mark.parametrize("operation", ["connection", "repair"])
def test_probe_release_change_during_drain_refuses_before_spawn_or_environment_build(
        isolated_probe_release, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from provider_hub import datadir
    from provider_hub.state import load_state, mutate_state
    from provider_hub.worker import ProviderWorkerClient

    service, plugin_id, active, waiting, _ = isolated_probe_release
    entered, finish = Event(), Event()
    live = ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})

    def request(*_):
        entered.set()
        assert finish.wait(5)

    def unexpected(*args, **kwargs):
        pytest.fail("a stale release must refuse before copying, spawning, or rebuilding")

    monkeypatch.setattr(live, "_request", request)
    monkeypatch.setattr(service, "ProviderWorkerClient", unexpected)
    monkeypatch.setattr(service.PluginEnvironment, "install", unexpected)
    monkeypatch.setattr(datadir, "copy_folder", unexpected)
    original = load_state()["installations"][plugin_id]
    changed_key = "b" * 32

    def change(state):
        state["installations"][plugin_id]["active_data_key"] = changed_key

    def probe():
        if operation == "connection":
            result = service.test_provider_connection(plugin_id)
            assert not result["ok"] and "changed" in result["message"].lower()
            return
        with pytest.raises((service.ProviderHubInstallError, datadir.DataFolderError), match="changed"):
            service.repair_installation_environment(plugin_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(live.request, "health", {})
        try:
            assert entered.wait(2)
            checked = pool.submit(probe)
            assert waiting.wait(2)
            mutate_state(change)
        finally:
            finish.set()
        running.result(timeout=3)
        checked.result(timeout=3)
    current = load_state()["installations"][plugin_id]
    assert current == {**original, "active_data_key": changed_key}
    assert (active / "settings").read_text() == "before request"


def test_a_waiting_connection_probe_allows_another_plugin_to_be_removed(isolated_probe_release, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from provider_hub.state import load_state
    from provider_hub.worker import ProviderWorkerClient

    service, plugin_id, active, waiting, _ = isolated_probe_release
    _seed(beta=_row("beta", active="1.0.0"))
    monkeypatch.setattr(service, "PROBE_TIMEOUT", 30.0)
    entered, finish = Event(), Event()
    live = ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})

    def request(*_):
        entered.set()
        assert finish.wait(10)

    monkeypatch.setattr(live, "_request", request)

    class ProbeClient(ProviderWorkerClient):
        def _request(self, *_):
            assert (Path(self.env["BAZARR_PLUGIN_DATA_DIR"]) / "settings").read_text() == "before request"
            return SimpleNamespace(payload={"initialized": True})

    monkeypatch.setattr(service, "ProviderWorkerClient", ProbeClient)
    with ThreadPoolExecutor(max_workers=3) as pool:
        running = pool.submit(live.request, "health", {})
        try:
            assert entered.wait(2)
            probe = pool.submit(service.test_provider_connection, plugin_id)
            assert waiting.wait(5)
            removal = pool.submit(service.remove_installation, "beta")
            assert removal.result(timeout=5)
            assert load_state()["installations"]["beta"]["state"] == "removed"
            assert not probe.done()
        finally:
            finish.set()
        running.result(timeout=3)
        assert probe.result(timeout=10)["ok"]


@pytest.mark.parametrize("outcome", ["success", "deadline", "filesystem"])
def test_probe_copy_fences_only_its_release_and_does_not_publish_partial_data(
        isolated_probe_release, monkeypatch, outcome):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from provider_hub import datadir
    from provider_hub.sandbox import sweep
    from provider_hub.state import load_state, provider_hub_dir
    from provider_hub.worker import ProviderWorkerClient

    service, plugin_id, active, _, _ = isolated_probe_release
    (active / "first").write_text("first file")
    _seed(beta=_row("beta", active="1.0.0"))
    original = load_state()["installations"][plugin_id]
    copying, finish_copy, dispatched = Event(), Event(), Event()
    live = ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})
    monkeypatch.setattr(live, "_request", lambda *_: dispatched.set())

    now = 0.0
    clock = SimpleNamespace(monotonic=lambda: now, perf_counter=service.time.perf_counter)
    for module in (service, datadir, sweep):
        monkeypatch.setattr(module, "time", clock)
    open_regular = datadir._open_regular

    def open_file(path, entry, fd):
        nonlocal now
        if Path(path).name == "settings":
            copying.set()
            assert finish_copy.wait(20)
            if outcome == "deadline":
                now = service.PROBE_TIMEOUT
            if outcome == "filesystem":
                raise OSError("copy read failed")
        return open_regular(path, entry, fd)

    checks = []

    class ProbeClient(ProviderWorkerClient):
        def _request(self, *_):
            folder = Path(self.env["BAZARR_PLUGIN_DATA_DIR"])
            assert (folder / "first").read_text() == "first file"
            assert (folder / "settings").read_text() == "before request"
            checks.append(folder)
            return SimpleNamespace(payload={"initialized": True})

    monkeypatch.setattr(datadir, "_open_regular", open_file)
    monkeypatch.setattr(service, "ProviderWorkerClient", ProbeClient)
    with ThreadPoolExecutor(max_workers=4) as pool:
        probe = pool.submit(service.test_provider_connection, plugin_id)
        try:
            assert copying.wait(10)
            [temporary] = list(provider_hub_dir().glob(".probe-*"))
            assert not (temporary / "data").exists()
            assert not checks
            assert pool.submit(service.remove_installation, "beta").result(timeout=10)
            with pytest.raises(service.ProviderHubInstallError, match="busy"):
                pool.submit(service.remove_installation, plugin_id, keep_data=False).result(timeout=5)
            running = pool.submit(live.request, "health", {})
            with datadir._gate:
                assert plugin_id in datadir._paused
            assert not dispatched.is_set()
        finally:
            finish_copy.set()
        result = probe.result(timeout=10)
        running.result(timeout=5)

    assert result["status"] == {"success": "ready", "deadline": "busy", "filesystem": "failed"}[outcome]
    assert len(checks) == (outcome == "success")
    assert not temporary.exists() and dispatched.is_set()
    assert (active / "settings").read_text() == "before request"
    state = load_state()
    assert state["installations"][plugin_id] == original
    assert state["installations"]["beta"]["state"] == "removed"


@pytest.mark.parametrize("limit", ["entries", "bytes", "depth"])
def test_connection_probe_caps_data_copy_work(isolated_probe_release, monkeypatch, limit):
    from provider_hub import datadir
    from provider_hub.state import load_state, provider_hub_dir

    service, plugin_id, active, _, _ = isolated_probe_release
    original = load_state()["installations"][plugin_id]
    if limit == "entries":
        monkeypatch.setattr(datadir, "PROBE_COPY_MAX_ENTRIES", 1)
        (active / "another file").touch()
    elif limit == "bytes":
        monkeypatch.setattr(datadir, "PROBE_COPY_MAX_BYTES", 1)
    else:
        monkeypatch.setattr(datadir, "PROBE_COPY_MAX_DEPTH", 1)
        (active / "folder" / "nested").mkdir(parents=True)

    def unexpected(*args, **kwargs):
        pytest.fail("a refused copy must not start the worker")

    monkeypatch.setattr(service, "ProviderWorkerClient", unexpected)
    result = service.test_provider_connection(plugin_id)
    assert not result["ok"] and result["status"] == "busy"
    assert load_state()["installations"][plugin_id] == original
    assert not list(provider_hub_dir().glob(".probe-*"))


@pytest.mark.parametrize(("remaining", "worker_start_expires"), [(0.01, False), (6.0, False), (6.0, True)])
def test_connection_probe_admission_delay_does_not_mark_healthy_environment_for_repair(
        isolated_probe_release, monkeypatch, remaining, worker_start_expires):
    from types import SimpleNamespace

    from provider_hub import datadir
    from provider_hub.sandbox import sweep
    from provider_hub.state import load_state
    from provider_hub.worker import ProviderWorkerClient, WorkerError

    service, plugin_id, _, _, _ = isolated_probe_release
    original = load_state()["installations"][plugin_id]
    now = 0.0
    clock = SimpleNamespace(monotonic=lambda: now, perf_counter=service.time.perf_counter)
    for module in (service, datadir, sweep):
        monkeypatch.setattr(module, "time", clock)
    copy_folder = datadir.copy_folder

    def copy(*args, **kwargs):
        nonlocal now
        result = copy_folder(*args, **kwargs)
        now = service.PROBE_TIMEOUT - remaining
        return result

    health_timeouts = []

    class ProbeClient(ProviderWorkerClient):
        def _request(self, op, payload, timeout, *_):
            nonlocal now
            health_timeouts.append(timeout)
            if timeout < service.PROBE_TIMEOUT - 1:
                if worker_start_expires:
                    now += timeout
                    raise WorkerError("worker process did not start")
                raise WorkerError("worker response timed out", code="timeout")
            return SimpleNamespace(payload={"initialized": True})

    monkeypatch.setattr(datadir, "copy_folder", copy)
    monkeypatch.setattr(service, "ProviderWorkerClient", ProbeClient)
    result = service.test_provider_connection(plugin_id)
    assert not result["ok"] and result["status"] == "busy"
    assert health_timeouts == ([remaining] if remaining > 1 else [])
    assert load_state()["installations"][plugin_id] == original

    now = 0.0
    monkeypatch.setattr(datadir, "copy_folder", copy_folder)
    assert service.test_provider_connection(plugin_id)["ok"]


def test_connection_probe_without_data_can_use_the_remaining_health_time(
        hub, monkeypatch, probe_process_evidence):
    from types import SimpleNamespace

    from provider_hub import service
    from provider_hub.worker import ProviderWorkerClient

    plugin_id = "examplehub"
    _seed(examplehub=_row(plugin_id, active="1.0.0"))
    probe_process_evidence["partial"] = True
    now = 0.0
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now, perf_counter=service.time.perf_counter))

    def verify(*_):
        nonlocal now
        now = service.PROBE_TIMEOUT - 2.0

    class ProbeClient(ProviderWorkerClient):
        def _request(self, op, payload, timeout, *_):
            assert op == "health" and timeout == 2.0
            assert "BAZARR_PLUGIN_DATA_DIR" not in self.env
            return SimpleNamespace(payload={"initialized": True})

    monkeypatch.setattr(service, "verify_bundle_tree", verify)
    monkeypatch.setattr(service, "ProviderWorkerClient", ProbeClient)

    result = service.test_provider_connection(plugin_id)
    assert result["ok"] and result["status"] == "ready"
    assert probe_process_evidence["scans"] == 0


def test_connection_probe_keeps_refusing_a_former_launcher_without_terminal_proof(
        isolated_probe_release, monkeypatch):
    from provider_hub import datadir
    from provider_hub.state import load_state

    service, plugin_id, active, _, evidence = isolated_probe_release
    original = load_state()["installations"][plugin_id]
    evidence["launchers"][12345] = "4321"

    def unexpected(*args, **kwargs):
        pytest.fail("a missing terminal cleanup result must prevent a probe")

    monkeypatch.setattr(datadir, "copy_folder", unexpected)
    monkeypatch.setattr(service, "ProviderWorkerClient", unexpected)
    for _ in range(2):
        result = service.test_provider_connection(plugin_id)
        assert not result["ok"] and "previous plugin workers stopped" in result["message"]
        evidence["launchers"].clear()

    assert load_state()["installations"][plugin_id] == original
    assert list(active.parent.iterdir()) == [active]


@pytest.mark.parametrize("operation", ["connection", "repair"])
@pytest.mark.parametrize("proof", ["partial", "unreadable", "survivors", "refused"])
def test_probe_refuses_unproven_previous_worker_exit_before_mutation_or_spawn(
        isolated_probe_release, monkeypatch, operation, proof):
    from provider_hub import datadir
    from provider_hub.sandbox.sweep import SweptSession
    from provider_hub.state import load_state

    service, plugin_id, active, _, evidence = isolated_probe_release
    if proof in {"survivors", "refused"}:
        evidence["sessions"] = [SweptSession(12345, (12346,), proof)]
    else:
        evidence[proof] = True if proof == "partial" else 1
    original = load_state()["installations"][plugin_id]

    def unexpected(*args, **kwargs):
        pytest.fail("previous worker proof must precede copying, spawning, and rebuilding")

    monkeypatch.setattr(service, "ProviderWorkerClient", unexpected)
    monkeypatch.setattr(service.PluginEnvironment, "install", unexpected)
    monkeypatch.setattr(datadir, "copy_folder", unexpected)
    if operation == "connection":
        result = service.test_provider_connection(plugin_id)
        assert not result["ok"]
        assert result["status"] == "failed"
    else:
        with pytest.raises((service.ProviderHubInstallError, datadir.DataFolderError)):
            service.repair_installation_environment(plugin_id)
    assert evidence["scans"] > 0
    assert load_state()["installations"][plugin_id] == original
    assert list(active.parent.iterdir()) == [active]
    assert (active / "settings").read_text() == "before request"


@pytest.mark.parametrize("needs_repair", [False, True])
def test_startup_reconcile_keeps_its_environment_when_previous_worker_exit_is_unproven(
        isolated_probe_release, monkeypatch, needs_repair):
    import sys

    from provider_hub.state import load_state, mutate_state, provider_hub_dir
    from provider_hub.venv import environment_fingerprint

    service, plugin_id, active, _, evidence = isolated_probe_release
    if needs_repair:
        mutate_state(lambda state: state["installations"][plugin_id].update(
            needs_repair=True, last_error=service.env_registry.NEEDS_REPAIR_MESSAGE))
    original = load_state()["installations"][plugin_id]
    own_environment = Path(original["python_path"]).parent.parent
    (own_environment / "pyvenv.cfg").write_text(
        f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n")
    validated = service.validate_manifest(original["manifest"], built_in_provider_ids=set())
    shared = provider_hub_dir() / "shared_envs" / "already-recorded"
    (shared / "bin").mkdir(parents=True)
    (shared / "bin" / "python").write_text("")
    fingerprint = environment_fingerprint(validated)
    mutate_state(lambda state: state["plugin_envs"].update({fingerprint: {"path": str(shared)}}))
    evidence["partial"] = True

    def unexpected(*args, **kwargs):
        pytest.fail("startup must not spawn or build after its data snapshot was refused")

    monkeypatch.setattr(service, "_smoke_validate_worker", unexpected)
    monkeypatch.setattr(service.PluginEnvironment, "install", unexpected)
    result = service.reconcile_plugin_environments()

    assert evidence["scans"] > 0
    assert result["kept"] == [plugin_id] and result["repointed"] == []
    assert load_state()["installations"][plugin_id] == original
    assert load_state()["plugin_envs"] == {fingerprint: {"path": str(shared)}}
    assert (active / "settings").read_text() == "before request"


def test_probe_timeout_keeps_dispatch_closed_until_the_actual_worker_stop_finishes(
        isolated_probe_release, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from provider_hub import datadir, worker
    from provider_hub.sandbox import sweep
    from provider_hub.state import load_state

    service, plugin_id, active, _, evidence = isolated_probe_release
    original = load_state()["installations"][plugin_id]
    stopping, finish, stopped, waiting, dispatched = Event(), Event(), Event(), Event(), Event()
    live = worker.ProviderWorkerClient([], provider_id=plugin_id, env={"BAZARR_PLUGIN_DATA_DIR": str(active)})
    monkeypatch.setattr(service, "PROBE_TIMEOUT", 0.1)

    # Start the short stop budget after cold provider/job setup and settle-thread startup.
    # Only these modules use the clock; the real Future wait still expires normally.
    started = None
    origin = time.monotonic()

    def monotonic():
        return origin if started is None else origin + time.monotonic() - started

    clock = SimpleNamespace(monotonic=monotonic, perf_counter=time.perf_counter, sleep=time.sleep)
    for module in (service, datadir, sweep):
        monkeypatch.setattr(module, "time", clock)

    def stop(idle_seconds, *, grace_seconds):
        nonlocal started
        assert idle_seconds == 0 and 0 < grace_seconds <= 0.1
        assert evidence["scans"] > 0
        with datadir._gate:
            assert plugin_id in datadir._paused and plugin_id not in datadir._entering
        started = time.monotonic()
        stopping.set()
        assert finish.wait(5)
        stopped.set()

    settle_async = datadir._settle_async
    settling = None

    def start_settle(checked_id, settle, deadline):
        nonlocal settling
        assert checked_id == plugin_id
        settling = settle_async(checked_id, settle, deadline)
        assert stopping.wait(2), "the real worker stop did not start"
        return settling

    wait_for = datadir._gate.wait_for

    def observe_wait(predicate, timeout=None):
        def observed():
            ready = predicate()
            if not ready:
                waiting.set()
            return ready

        return wait_for(observed, timeout)

    def unexpected(*args, **kwargs):
        pytest.fail("a timed-out stop must not copy data or spawn a probe")

    def request(*_):
        assert stopped.is_set()
        dispatched.set()

    monkeypatch.setattr(live, "stop_if_idle", stop)
    monkeypatch.setattr(live, "_request", request)
    monkeypatch.setattr(datadir, "_settle_async", start_settle)
    monkeypatch.setattr(datadir._gate, "wait_for", observe_wait)
    monkeypatch.setattr(service, "ProviderWorkerClient", unexpected)
    monkeypatch.setattr(datadir, "copy_folder", unexpected)
    with worker._live_clients_lock:
        worker._live_clients.add(live)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            probe = pool.submit(service.test_provider_connection, plugin_id)
            running = None
            try:
                result = probe.result(timeout=3)
                assert not result["ok"] and result["status"] == "busy", result
                assert "busy" in result["message"].lower(), result
                assert stopping.is_set() and not stopped.is_set() and not settling.done()
                with datadir.quiesced(plugin_id, unexpected, 0.1, bounded=True) as settled:
                    assert not settled
                running = pool.submit(live.request, "health", {})
                assert waiting.wait(1)
                with datadir._gate:
                    assert plugin_id in datadir._paused
                    assert plugin_id not in datadir._entering and not dispatched.is_set()
                with datadir.dispatch("anotherhub"):
                    assert not stopped.is_set()
            finally:
                finish.set()
                try:
                    if running is not None:
                        running.result(timeout=2)
                finally:
                    datadir._resume_dispatch(plugin_id)
            assert settling.result(timeout=2)
            assert dispatched.is_set()
    finally:
        finish.set()
        with worker._live_clients_lock:
            worker._live_clients.discard(live)
    assert load_state()["installations"][plugin_id] == original
    assert list(active.parent.iterdir()) == [active]
    assert (active / "settings").read_text() == "before request"


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
    from provider_hub.state import load_state, mutate_state, provider_hub_dir

    service, new, _ = data_release
    if source != "official":
        # A failed restage can leave the installation pointing at another catalog.
        mutate_state(lambda state: state["installations"]["exampleservice"].update(source_id=source))
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

    assert service._stage_data_source({"kept_data": {"alpha": kept}}, "alpha", failed,
                                      "official", "subtitle_provider", "catalog", manifest) == (legacy, False)


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
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: False)
    service.activate_staged_installations()
    assert old.exists()
    assert load_state()["installations"]["exampleservice"]["pending_restart"]

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
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
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
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
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: False)
    assert service.activate_staged_installations() == []
    row = load_state()["installations"]["exampleservice"]
    assert row["active_version"] == "1.0.0" and row["staged_version"] == "1.1.0"
    assert old_file.read_text() == "last write from v1"

    service_kind.attach_policy = ATTACH_LIVE
    assert not service.activate_release("exampleservice").activated
    assert load_state()["installations"]["exampleservice"]["active_version"] == "1.0.0"

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
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
    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: False)
    assert service.activate_staged_installations() == []

    monkeypatch.setattr(detach, "snapshot_workers", lambda provider_id: [])
    assert service.remove_installation("exampleservice", keep_data=keep_data)
    assert detach.wait_idle()
    assert load_state()["installations"]["exampleservice"]["state"] == "removed"
    assert sample.read_text() == "still in use"

    monkeypatch.setattr(service, "_previous_workers_stopped", lambda **_: True)
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


@pytest.mark.parametrize("remove_staged_catalog", [False, True])
def test_refresh_revocation_restores_the_active_catalog_and_allows_its_previous_data_to_return(
        catalog_data_owner, monkeypatch, tmp_path, remove_staged_catalog):
    from provider_hub import catalog_feed, datadir, releases, state_store
    from provider_hub.state import load_state, provider_hub_dir
    from signed_feed_helpers import SignedWorld

    service, _, published, active_data, _ = catalog_data_owner
    plugin_id = "examplehub"
    current = dict(load_state()["installations"][plugin_id], trusted=False)
    previous_key = "c" * 32
    previous_data = datadir.release_path(provider_hub_dir(), plugin_id, previous_key)
    previous_data.mkdir(parents=True)
    (previous_data / "settings").write_text("previous catalog A data")
    _seed(examplehub={
        **current, "active_version": "0.9.0", "active_path": _bundle(plugin_id, "0.9.0"),
        "python_path": _python(plugin_id, "0.9.0"), "active_data_key": previous_key,
        "manifest": {**current["manifest"], "version": "0.9.0"}})
    _seed(examplehub=current)
    original, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    previous, = state_store.list_releases(plugin_id, states=(releases.RETIRED,))

    service.stage_install(published, source_id="catalog-b", catalog_list="plugins")
    staged, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    staged_data = datadir.release_path(provider_hub_dir(), plugin_id, staged.data_key)
    (staged_data / "settings").write_text("staged catalog B data")
    assert staged.source_id == "catalog-b" and original.source_id == "catalog-a"
    if remove_staged_catalog:
        assert service.remove_catalog_source("catalog-b")

    monkeypatch.setattr(catalog_feed, "counter_file", lambda: tmp_path / "feed-counter.json")
    world = SignedWorld(monkeypatch)
    world.publish(revocations=[{"id": plugin_id, "version": "1.1.0", "reason": "test revocation"}])
    result = service.refresh_catalog(source_ids={"official"})

    assert result["sources"] == 1
    state = load_state()
    assert state["catalog_sources"]["official"]["last_error"] is None
    row = state["installations"][plugin_id]
    assert (row["state"], row["active_version"], row["staged_version"]) == ("active", "1.0.0", None)
    assert (row["source_id"], row["origin"], row["source_bound"], row["trusted"]) == (
        "catalog-a", "catalog", True, False)
    assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,)) == [original]
    failed, = state_store.list_releases(plugin_id, states=(releases.FAILED,))
    assert failed.id == staged.id and failed.source_id == "catalog-b"
    assert (active_data / "settings").read_text() == "private catalog A data"
    assert (previous_data / "settings").read_text() == "previous catalog A data"

    assert service.rollback_release(plugin_id).outcome == service.ACTIVATION_WAITING
    assert service.activate_staged_installations() == [plugin_id]

    row = load_state()["installations"][plugin_id]
    assert (row["active_version"], row["source_id"], row["active_data_key"]) == (
        "0.9.0", "catalog-a", previous_key)
    restored, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    assert restored.id == previous.id and restored.catalog_registration == previous.catalog_registration
    assert (previous_data / "settings").read_text() == "previous catalog A data"
    assert (active_data / "settings").read_text() == "private catalog A data"


@pytest.mark.parametrize("saved_source", [False, True])
@pytest.mark.parametrize("active_trusted", [False, True])
@pytest.mark.parametrize("staged_trusted", [False, True])
def test_discarding_a_stage_restores_only_the_active_official_trust_refresh_accepted(
        catalog_data_owner, monkeypatch, tmp_path, saved_source, active_trusted, staged_trusted):
    _check_discarded_official_stage(catalog_data_owner, monkeypatch, tmp_path,
                                   saved_source, active_trusted, staged_trusted)


@pytest.mark.parametrize("active_decision", [
    "legacy-unanswered", "legacy-never-trusted", "saved-trusted", "saved-demoted", "saved-never-trusted",
])
def test_revoked_stage_leaves_only_the_active_releases_trust_decision(hub, monkeypatch, active_decision):
    from provider_hub import releases, revocation, service, state_store
    from provider_hub.state import OFFICIAL_CATALOG_REPO, load_state, mutate_state

    plugin_id = "examplehub"
    old_commit, new_commit = "a" * 40, "b" * 40
    old_version, new_version = "1.0.0", "1.1.0"
    never_trusted = active_decision.endswith("never-trusted")
    active = _row(plugin_id, active=old_version, trusted=not never_trusted)
    active["manifest"]["source"] = {
        "repo": OFFICIAL_CATALOG_REPO, "commit": old_commit, "trusted": not never_trusted,
    }
    _seed(examplehub=active)
    original, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))

    decision = {"source_id": "official", "origin": "catalog", "source_bound": True,
                "trusted": active_decision == "saved-trusted"}
    if active_decision == "saved-demoted":
        decision.update(source_id=None, demoted_commit=True, trust_note="Active commit was previously rejected.")
    staged = _row(plugin_id, active=old_version, staged=new_version, manifest=active["manifest"])
    staged["staged_manifest"]["source"] = {"repo": OFFICIAL_CATALOG_REPO, "commit": new_commit, "trusted": True}
    if active_decision.startswith("saved-"):
        staged["previous_source"] = decision.copy()
    _seed(examplehub=staged)
    discarded, = state_store.list_releases(plugin_id, states=(releases.STAGED,))

    verdicts = {old_commit: (True, "main") if never_trusted else (None, "comparison unavailable"),
                new_commit: (False, "outside main and beta")}
    monkeypatch.setattr(service._OfficialAncestry, "check", lambda self, commit: verdicts[commit])
    monkeypatch.setattr(service, "_revocations", lambda state: (revocation.Revocation(plugin_id, new_version),))

    def reconcile(state):
        service._recheck_installed_trust(state, service._OfficialAncestry(state))
        service._hold_revoked_installations(state)

    mutate_state(reconcile)

    row = load_state()["installations"][plugin_id]
    assert (row["state"], row["active_version"], row["staged_version"]) == ("active", old_version, None)
    assert service._installation_source(row) == decision
    assert "previous_source" not in row
    assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,)) == [original]
    failed, = state_store.list_releases(plugin_id, states=(releases.FAILED,))
    assert failed.id == discarded.id


def test_a_revoked_legacy_stage_preserves_the_active_official_trust_on_the_same_refresh(
        catalog_data_owner, monkeypatch, tmp_path):
    _check_discarded_official_stage(catalog_data_owner, monkeypatch, tmp_path,
                                   False, True, True, revoke_stage=True)


def _check_discarded_official_stage(
        catalog_data_owner, monkeypatch, tmp_path, saved_source, active_trusted, staged_trusted,
        revoke_stage=False):
    from provider_hub import catalog_feed, releases, state_store
    from provider_hub.state import OFFICIAL_CATALOG_REPO, load_state, mutate_state
    from signed_feed_helpers import SignedWorld

    service, kind, published, _, _ = catalog_data_owner
    plugin_id = "examplehub"
    old_commit, new_commit = "a" * 40, "d" * 40
    current = load_state()["installations"][plugin_id]
    old_source = {**current["manifest"]["source"], "repo": OFFICIAL_CATALOG_REPO,
                  "commit": old_commit, "trusted": True}
    _seed(examplehub={
        **current, "active_path": _bundle(plugin_id, "official-old"),
        "source_id": "official", "trusted": True,
        "manifest": {**current["manifest"], "source": old_source}})
    original, = state_store.list_releases(plugin_id, states=(releases.ACTIVE,))
    new_source = {**published["source"], "repo": OFFICIAL_CATALOG_REPO,
                  "commit": new_commit, "trusted": True}
    monkeypatch.setattr(catalog_feed, "counter_file", lambda: tmp_path / "feed-counter.json")
    world = SignedWorld(monkeypatch)
    world.ancestors.update((old_commit, new_commit))
    world.publish(source_commit=new_commit, plugins=[{**published, "source": new_source}])
    assert service.refresh_catalog(source_ids={"official"})["entries"] == 1
    entry = next(entry for entry in load_state()["catalog_entries"].values()
                 if entry["source"] == "official")
    service.stage_install(entry["manifest"], source_id="official", catalog_list="plugins")
    staged, = state_store.list_releases(plugin_id, states=(releases.STAGED,))
    if not saved_source:
        mutate_state(lambda state: state["installations"][plugin_id].pop("previous_source"))

    world.ancestors.clear()
    world.heads = {"main": "e" * 40, "beta": "e" * 40}
    world.publish(commit="e" * 40, source_commit="e" * 40, version=101,
                  plugins=[{**published, "source": new_source}],
                  revocations=[{"id": plugin_id, "version": "1.1.0", "reason": "test revocation"}]
                  if revoke_stage else [])
    if active_trusted:
        world.ancestors.add(old_commit)
    if staged_trusted:
        world.ancestors.add(new_commit)
    service.refresh_catalog(source_ids={"official"})
    refreshed = load_state()
    assert refreshed["catalog_sources"]["official"]["last_error"] is None
    if not revoke_stage:
        assert refreshed["installations"][plugin_id]["trusted"] is staged_trusted

        def refuse(*args, **kwargs):
            raise RuntimeError("the staged worker did not start")

        monkeypatch.setattr(kind, "smoke_test", refuse)
        assert service.activate_release(plugin_id, at_boot=True).outcome == service.ACTIVATION_FAILED

    row = load_state()["installations"][plugin_id]
    assert row["active_version"] == "1.0.0" and not row["staged_version"]
    assert row["trusted"] is active_trusted
    assert row["source_bound"] is True and row["origin"] == "catalog"
    if active_trusted:
        assert row["source_id"] == "official" and not row.get("demoted_commit")
    else:
        assert row["source_id"] is None and row["demoted_commit"] is True
    assert state_store.list_releases(plugin_id, states=(releases.ACTIVE,)) == [original]
    failed, = state_store.list_releases(plugin_id, states=(releases.FAILED,))
    assert failed.id == staged.id

    service.refresh_catalog(source_ids={"official"})
    assert load_state()["installations"][plugin_id]["trusted"] is active_trusted

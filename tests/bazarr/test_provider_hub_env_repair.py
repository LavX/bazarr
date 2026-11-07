# coding=utf-8
"""Repairing a plugin's environment, and what a failed check leaves behind."""
import threading
from pathlib import Path

import pytest

from provider_hub import env_registry
from provider_hub.state import load_state, save_state
from provider_hub.venv import environment_fingerprint, path_key, pin_scope
from provider_hub.manifest import validate_manifest
from test_provider_hub import _manifest


@pytest.fixture
def hub(tmp_path, monkeypatch):
    root = tmp_path / "provider_hub"
    root.mkdir()
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(root / "state.json"))
    return root


def _env(path):
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "python").write_text("", encoding="utf-8")
    return path


def _active_row(bundle, python_path, **extra):
    row = {
        "provider_id": "examplehub",
        "name": "Example Hub Provider",
        "active_version": "1.0.0",
        "active_path": str(bundle),
        "python_path": str(python_path),
        "state": "active",
        "pending_restart": False,
        "manifest": _manifest(dependencies={"requirements": []}),
        "config": {},
        "last_error": None,
    }
    row.update(extra)
    return row


class _BuildsNewEnvironment:
    """Stands in for PluginEnvironment: builds the directory it is asked for."""

    def __init__(self, root):
        self.root = Path(root)
        self.rebuild_requests = []

    def install(self, validated, timeout=None, *, rebuild=False):
        self.rebuild_requests.append(rebuild)
        return _env(self.root / "shared_envs" / "rebuilt")


def _patch_environment(monkeypatch):
    built = []

    class Environment(_BuildsNewEnvironment):
        def install(self, validated, timeout=None, *, rebuild=False):
            built.append(rebuild)
            return super().install(validated, timeout, rebuild=rebuild)

    monkeypatch.setattr("provider_hub.service.PluginEnvironment", Environment)
    return built


def test_repair_builds_a_new_environment_and_repoints_after_the_probe(hub, tmp_path, monkeypatch):
    from provider_hub.service import repair_installation_environment

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(
        bundle, old / "bin" / "python", needs_repair=True, last_error=env_registry.NEEDS_REPAIR_MESSAGE)}
    save_state(state)
    built = _patch_environment(monkeypatch)
    probed = []
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker",
                        lambda manifest, bundle_path, python_path: probed.append(python_path))
    monkeypatch.setattr("provider_hub.registry.register_active_provider_classes", lambda installations=None: [])

    repair_installation_environment("examplehub")

    new = hub / "shared_envs" / "rebuilt"
    row = load_state()["installations"]["examplehub"]
    assert built == [True]
    assert probed == [new / "bin" / "python"]
    assert row["python_path"] == str(new / "bin" / "python")
    assert "needs_repair" not in row
    assert row["last_error"] is None
    validated = validate_manifest(row["manifest"], built_in_provider_ids=set())
    assert load_state()["plugin_envs"] == {environment_fingerprint(validated): {"path": str(new)}}
    # The old directory is not touched here; a cleanup removes it once nothing references it.
    assert old.exists()
    assert env_registry.cleanup_environments(hub) == [str(old)]


def test_a_failed_repair_leaves_the_installation_and_removes_the_new_environment(hub, tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, repair_installation_environment

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python", needs_repair=True)}
    save_state(state)
    _patch_environment(monkeypatch)

    def failing_probe(manifest, bundle_path, python_path):
        raise RuntimeError("import failed")

    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", failing_probe)

    with pytest.raises(RuntimeError, match="import failed"):
        repair_installation_environment("examplehub")

    row = load_state()["installations"]["examplehub"]
    assert row["python_path"] == str(old / "bin" / "python")
    assert row["needs_repair"] is True
    assert load_state()["plugin_envs"] == {}
    assert not (hub / "shared_envs" / "rebuilt").exists()
    assert old.exists()
    with pytest.raises(ProviderHubInstallError):
        repair_installation_environment("missing")


def test_a_failed_worker_check_at_use_time_flags_the_installation(hub, tmp_path, monkeypatch):
    from provider_hub.service import test_provider_connection

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    env = _env(hub / "shared_envs" / "old")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, env / "bin" / "python")}
    save_state(state)

    class FailingClient:
        def __init__(self, command, cwd=None, env=None):
            pass

        def request(self, op, payload=None, timeout=30):
            raise RuntimeError("No module named 'cloudscraper'")

        def stop(self):
            pass

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda _manifest, _path: None)
    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", FailingClient)

    result = test_provider_connection("examplehub")

    row = load_state()["installations"]["examplehub"]
    assert result["ok"] is False
    assert row["needs_repair"] is True
    assert "cloudscraper" in row["last_error"]


def test_a_stopped_install_leaves_an_environment_another_install_is_using(hub):
    from provider_hub.service import _discard_stopped_install

    shared = _env(hub / "shared_envs" / "fp")
    mine = _env(hub / "shared_envs" / "mine")
    holding = threading.Event()
    release = threading.Event()

    def other_install():
        with pin_scope():
            from provider_hub import venv
            venv._pin(shared)
            holding.set()
            release.wait(5)

    worker = threading.Thread(target=other_install)
    worker.start()
    assert holding.wait(5)
    try:
        with pin_scope():
            from provider_hub import venv
            venv._pin(shared)
            venv._pin(mine)
            _discard_stopped_install([shared, mine], {})
        assert shared.exists()
        assert not mine.exists()
    finally:
        release.set()
        worker.join()
    with pin_scope():
        _discard_stopped_install([shared], {})
    assert not shared.exists()


def test_a_stopped_install_keeps_a_recorded_environment(hub):
    from provider_hub.service import _discard_stopped_install

    recorded = _env(hub / "shared_envs" / "fp")
    state = load_state()
    state["plugin_envs"] = {"fp": {"path": str(recorded)}}
    save_state(state)

    with pin_scope():
        _discard_stopped_install([recorded], {})

    assert recorded.exists()


def test_a_failed_install_removes_the_environment_it_built(hub, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install
    from test_provider_hub import _FakeResponse

    content = b"class ExampleProvider: pass\n"
    manifest = _manifest(file_payloads={"provider.py": content}, dependencies={"requirements": []})

    class Environment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            return _env(self.root / "shared_envs" / "fresh")

    def failing_probe(manifest, bundle_path, python_path):
        raise RuntimeError("worker would not start")

    monkeypatch.setattr("provider_hub.service.requests.get", lambda url, timeout: _FakeResponse(content=content))
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", Environment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", failing_probe)

    with pytest.raises(ProviderHubInstallError):
        stage_install(manifest)

    assert not (hub / "shared_envs" / "fresh").exists()


def test_a_failed_install_keeps_a_recorded_environment_it_reused(hub, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, stage_install
    from test_provider_hub import _FakeResponse

    content = b"class ExampleProvider: pass\n"
    manifest = _manifest(file_payloads={"provider.py": content}, dependencies={"requirements": []})
    recorded = _env(hub / "shared_envs" / "recorded")
    state = load_state()
    state["plugin_envs"] = {"fp": {"path": str(recorded)}}
    save_state(state)

    class Environment:
        def __init__(self, root):
            pass

        def install(self, validated, timeout=None):
            return recorded

    def failing_probe(manifest, bundle_path, python_path):
        raise RuntimeError("worker would not start")

    monkeypatch.setattr("provider_hub.service.requests.get", lambda url, timeout: _FakeResponse(content=content))
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", Environment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", failing_probe)

    with pytest.raises(ProviderHubInstallError):
        stage_install(manifest)

    assert recorded.exists()
    assert path_key(recorded) in {path_key(entry["path"]) for entry in load_state()["plugin_envs"].values()}


def test_removing_the_last_installation_frees_its_environment(hub, tmp_path):
    from provider_hub.service import activate_staged_installations

    env = _env(hub / "shared_envs" / "fp")
    other = _env(hub / "shared_envs" / "other")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    state = load_state()
    state["plugin_envs"] = {"fp": {"path": str(env)}, "other": {"path": str(other)}}
    state["installations"] = {
        "examplehub": _active_row(bundle, env / "bin" / "python", state="removed", pending_restart=True),
        "second": dict(_active_row(bundle, env / "bin" / "python"), provider_id="second"),
        "third": dict(_active_row(bundle, other / "bin" / "python"), provider_id="third", state="removed",
                      pending_restart=True),
    }
    save_state(state)

    activate_staged_installations()

    # "examplehub" is gone but "second" still runs from the shared environment.
    assert env_registry.cleanup_environments(hub) == [str(other)]
    assert env.exists() and not other.exists()

    state = load_state()
    del state["installations"]["second"]
    save_state(state)
    assert env_registry.cleanup_environments(hub) == [str(env)]


def test_a_stop_pressed_during_the_repair_probe_ends_the_repair_before_the_repoint(hub, tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubStopped, repair_installation_environment

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python", needs_repair=True)}
    save_state(state)
    _patch_environment(monkeypatch)
    stop_requested = threading.Event()
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker",
                        lambda manifest, bundle_path, python_path: stop_requested.set())

    def checkpoint(message):
        if stop_requested.is_set():
            raise ProviderHubStopped("stopped")

    with pytest.raises(ProviderHubStopped):
        repair_installation_environment("examplehub", checkpoint=checkpoint)

    row = load_state()["installations"]["examplehub"]
    assert row["python_path"] == str(old / "bin" / "python")
    assert row["needs_repair"] is True
    assert load_state()["plugin_envs"] == {}
    assert not (hub / "shared_envs" / "rebuilt").exists()


def test_the_install_worker_check_runs_inside_the_remaining_install_budget(hub, monkeypatch):
    from provider_hub.service import stage_install
    from test_provider_hub import _FakeResponse

    content = b"class ExampleProvider: pass\n"
    manifest = _manifest(file_payloads={"provider.py": content}, dependencies={"requirements": []})
    seen = []

    class Environment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            return _env(self.root / "shared_envs" / "fresh")

    def probe(manifest, bundle_path, python_path, timeout=10.0):
        seen.append(timeout)

    monkeypatch.setattr("provider_hub.service.requests.get", lambda url, timeout: _FakeResponse(content=content))
    monkeypatch.setattr("provider_hub.service.PluginEnvironment", Environment)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", probe)

    stage_install(manifest, install_timeout=3)

    assert len(seen) == 1 and 0 < seen[0] <= 3

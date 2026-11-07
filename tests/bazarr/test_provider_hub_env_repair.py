# coding=utf-8
"""Repairing a plugin's environment, and what a failed check leaves behind."""
import threading
import time
import sys
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
    import os

    # The bundle is kept where the hub keeps bundles; a path elsewhere is refused.
    bundle = Path(os.environ["BAZARR_PROVIDER_HUB_STATE"]).parent / "bundles" / "examplehub" / "1.0.0" / "c"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "provider.py").write_bytes(b"class ExampleProvider: pass\n")
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


class _BusyWorker:
    """A worker client whose stop() returns at once, as a stop does when nothing
    is queued ahead of it. It records how many calls the proxy still had admitted
    when the stop arrived, so an early stop cannot hide behind a fake that waits."""

    def __init__(self, python_path):
        self.command = [str(python_path), "-I", "-B", "runner.py"]
        self.process = None
        self.owner = None
        self.stop_calls = []

    def stop(self):
        self.stop_calls.append(getattr(self.owner, "_active_operations", None))


@pytest.fixture
def hub_registry():
    from subliminal_patch.extensions import provider_registry
    from provider_hub import registry

    before = dict(provider_registry.providers)
    ids = set(registry._REGISTERED_PROVIDER_HUB_IDS)
    yield provider_registry
    provider_registry.providers.clear()
    provider_registry.providers.update(before)
    registry._REGISTERED_PROVIDER_HUB_IDS.clear()
    registry._REGISTERED_PROVIDER_HUB_IDS.update(ids)


def test_repair_replaces_the_cached_worker_and_keeps_the_old_environment_until_it_exits(
    hub, tmp_path, monkeypatch, hub_registry
):
    from provider_hub.registry import register_active_provider_classes
    from provider_hub.service import repair_installation_environment
    from provider_hub.state import active_installations
    from subliminal_patch.core import SZProviderPool

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python", needs_repair=True)}
    save_state(state)
    _patch_environment(monkeypatch)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    register_active_provider_classes(active_installations())

    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    stale = pool["examplehub"]
    busy = _BusyWorker(old / "bin" / "python")
    busy.owner = stale
    stale.worker_client = busy
    assert stale.python_path == str(old / "bin" / "python")
    serving = threading.Event()
    finish = threading.Event()

    def serve():
        with stale._operation():
            serving.set()
            finish.wait(10)

    server = threading.Thread(target=serve)
    server.start()
    assert serving.wait(10)

    repair_installation_environment("examplehub")

    new_python = hub / "shared_envs" / "rebuilt" / "bin" / "python"
    # The next request builds an instance from the repaired class: it never reaches the old worker.
    fresh = pool["examplehub"]
    assert fresh is not stale
    assert fresh.python_path == str(new_python)
    assert fresh.worker_client is None
    # The old worker is still serving, so its environment is not removable yet.
    assert old.exists()
    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()
    time.sleep(0.3)
    assert busy.stop_calls == []

    finish.set()
    server.join(10)
    # The release follows the stop by a moment, on the drain thread.
    removed = []
    deadline = time.monotonic() + 10
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]
    assert not old.exists()
    assert busy.stop_calls and all(count == 0 for count in busy.stop_calls)


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


def test_an_instance_under_construction_while_its_provider_is_replaced_is_not_stored(
    hub, tmp_path, hub_registry
):
    from provider_hub.registry import HubProxyProvider, retire_provider_instances
    from subliminal_patch.core import SZProviderPool

    entered = threading.Event()
    proceed = threading.Event()
    terminated = []

    class OldClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = "/hub/shared_envs/old/bin/python"

        def __init__(self, **config):
            super().__init__(**config)
            entered.set()
            proceed.wait(10)

        def terminate(self):
            terminated.append(self)

    class NewClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = "/hub/shared_envs/new/bin/python"

    hub_registry.register("examplehub", OldClass)
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    result = {}
    worker = threading.Thread(target=lambda: result.update(provider=pool["examplehub"]))
    worker.start()
    assert entered.wait(10)

    # The repair registers the new class and retires the pooled instances while
    # the old class is still being constructed.
    hub_registry.register("examplehub", NewClass)
    retire_provider_instances("examplehub")
    proceed.set()
    worker.join(10)

    assert type(result["provider"]) is NewClass
    assert pool.initialized_providers["examplehub"] is result["provider"]
    assert [type(item) for item in terminated] == [OldClass]


def _pool_with_classes(hub_registry, old_python, new_python, *, new_search=None):
    """A real pool holding an instance of the old class, and the registry
    switched to a new class, as a repair leaves them."""
    from provider_hub.registry import HubProxyProvider
    from subliminal_patch.core import SZProviderPool

    class OldClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = str(old_python)

    class NewClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = str(new_python)

        def list_subtitles(self, video, languages):
            return new_search

    hub_registry.register("examplehub", OldClass)
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    stale = pool["examplehub"]
    hub_registry.register("examplehub", NewClass)
    return pool, stale, NewClass


def test_a_retired_proxy_never_starts_its_worker_and_the_call_moves_to_the_current_instance(
    hub, hub_registry, monkeypatch
):
    from provider_hub import registry
    from provider_hub.registry import retire_provider_instances
    from subliminal_patch.core import ProviderRetiredError

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    pool, stale, new_class = _pool_with_classes(
        hub_registry, old / "bin" / "python", new / "bin" / "python", new_search=["found"]
    )
    started = []
    monkeypatch.setattr(registry, "ProviderWorkerClient", lambda *args, **kwargs: started.append(args))
    stale.manifest_json = "{}"
    type(stale).bundle_path = str(hub)
    type(stale).manifest_json = "{}"

    for drain in retire_provider_instances("examplehub"):
        drain.join(10)

    # A search that fetched the proxy before the repair now reaches it.
    with pytest.raises(ProviderRetiredError):
        stale.list_subtitles(None, set())
    assert started == []
    assert stale.worker_client is None
    # The pool answers the same call from the current class.
    assert pool._call_current("examplehub", stale, lambda current: current.list_subtitles(None, set())) == ["found"]
    assert type(pool.initialized_providers["examplehub"]) is new_class


def test_an_admitted_call_holds_the_old_environment_until_it_ends(hub, hub_registry):
    from provider_hub.registry import retire_provider_instances

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    state = load_state()
    bundle = hub / "bundle"
    bundle.mkdir()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    stale.worker_client = _BusyWorker(old / "bin" / "python")
    stale.worker_client.owner = stale

    with stale._operation():
        drains = retire_provider_instances("examplehub")
        # The drain waits for the call; the call holds the environment itself.
        assert env_registry.cleanup_environments(hub) == []
        assert (old / "bin" / "python").exists()
        assert stale.worker_client.stop_calls == []

    # The last call of a retired proxy stops the worker it may have started, then frees the environment.
    for drain in drains:
        drain.join(10)
    assert stale.worker_client.stop_calls and all(count == 0 for count in stale.worker_client.stop_calls)
    assert env_registry.cleanup_environments(hub) == [str(old)]


def test_a_pool_lookup_survives_the_instance_being_detached_around_it(hub, hub_registry):
    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    pool, stale, new_class = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")

    class DetachesAfterAnswering(dict):
        def __init__(self, items, detach):
            super().__init__(items)
            self.detach = detach
            self.armed = True

        def _after(self, name):
            if self.armed:
                self.armed = False
                self.detach(name)

        def __contains__(self, name):
            answer = super().__contains__(name)
            self._after(name)
            return answer

        def get(self, name, default=None):
            answer = super().get(name, default)
            self._after(name)
            return answer

    holder = DetachesAfterAnswering(pool.initialized_providers, lambda name: dict.pop(holder, name, None))
    pool.initialized_providers = holder

    found = pool["examplehub"]

    # Detached between the existence check and the lookup, and again right after
    # the lookup: no KeyError either time, and the caller holds an instance.
    assert found is stale
    holder.armed = True
    holder.pop("examplehub", None)
    again = pool["examplehub"]
    assert type(again) is new_class


def test_a_connection_test_does_not_flag_an_installation_a_repair_has_moved(hub, tmp_path, monkeypatch):
    from provider_hub.service import test_provider_connection

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python")}
    save_state(state)
    cleaned = []

    class RepairedMidTest:
        def __init__(self, command, cwd=None, env=None):
            pass

        def request(self, op, payload=None, timeout=30):
            # A repair repoints the installation and a cleanup runs while the test is running.
            moved = load_state()
            moved["installations"]["examplehub"]["python_path"] = str(new / "bin" / "python")
            save_state(moved)
            cleaned.extend(env_registry.cleanup_environments(hub))
            raise RuntimeError("the old interpreter is gone")

        def stop(self):
            pass

    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda _manifest, _path: None)
    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", RepairedMidTest)

    result = test_provider_connection("examplehub")

    row = load_state()["installations"]["examplehub"]
    assert result["ok"] is False
    # The environment the test ran from was held until it finished.
    assert cleaned == []
    # The repaired installation is not marked broken by a result about its old interpreter.
    assert "needs_repair" not in row
    assert row["last_error"] is None
    assert env_registry.cleanup_environments(hub) == [str(old)]


def test_a_repair_that_finds_the_installation_moved_discards_its_environment(hub, tmp_path, monkeypatch):
    from provider_hub.service import ProviderHubInstallError, repair_installation_environment

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    other = _env(hub / "shared_envs" / "other")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python")}
    save_state(state)
    _patch_environment(monkeypatch)

    def probe(manifest, bundle_path, python_path):
        moved = load_state()
        moved["installations"]["examplehub"]["python_path"] = str(other / "bin" / "python")
        save_state(moved)

    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", probe)

    with pytest.raises(ProviderHubInstallError, match="changed"):
        repair_installation_environment("examplehub")

    assert load_state()["installations"]["examplehub"]["python_path"] == str(other / "bin" / "python")
    assert not (hub / "shared_envs" / "rebuilt").exists()


class _ScriptedWorkerClient:
    """The real worker client with the process replaced by a script: it answers
    every request with an empty candidate list and counts what reached it."""

    def __new__(cls):
        import json
        from types import SimpleNamespace

        from provider_hub.worker import WORKER_ABI_VERSION
        from provider_hub.worker import ProviderWorkerClient

        class Scripted(ProviderWorkerClient):
            def __init__(self):
                super().__init__(["python"], provider_id="examplehub")
                self.sent = []
                self.process = SimpleNamespace(stdin=self, stdout=object(), poll=lambda: None)

            def start(self):
                return False

            def write(self, text):
                self.sent.append(json.loads(text))

            def flush(self):
                pass

            def _read_line_with_deadline(self, timeout):
                return json.dumps({
                    "abi": WORKER_ABI_VERSION, "id": self.sent[-1]["id"], "ok": True,
                    "payload": {"candidates": []}, "events": [],
                })

        return Scripted()


def test_two_gated_requests_across_a_repair_both_reach_the_replacement(hub, hub_registry, monkeypatch):
    from babelfish import Language
    from subliminal.video import Movie
    from provider_hub import registry
    from provider_hub.registry import HubProxyProvider, retire_provider_instances
    from subliminal_patch.core import SZProviderPool

    worker = _ScriptedWorkerClient()
    monkeypatch.setattr(registry, "_global_worker_timeout", lambda: 0.5)

    class OldClass(HubProxyProvider):
        provider_name = "examplehub"
        languages = {Language("eng")}

    class NewClass(HubProxyProvider):
        provider_name = "examplehub"
        languages = {Language("eng")}

    NewClass.worker_client = worker
    hub_registry.register("examplehub", OldClass)
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    assert type(pool["examplehub"]) is OldClass
    hub_registry.register("examplehub", NewClass)
    admits = pool._admits
    repaired = []

    def admits_then_repair(name):
        # The search holds the old instance; the repair retires it before the call is made.
        answer = admits(name)
        if not repaired:
            repaired.append(True)
            retire_provider_instances("examplehub")
        return answer

    monkeypatch.setattr(pool, "_admits", admits_then_repair)
    movie = Movie("/media/example.mkv", "Example Movie", year=2024)
    movie.fps = None

    first = pool.list_subtitles_provider("examplehub", movie, {Language("eng")})
    second = pool.list_subtitles_provider("examplehub", movie, {Language("eng")})

    assert first == [] and second == []
    # The outcome of the first call was reported to the instance that served it,
    # so the second was not left waiting on a latch nobody would release.
    assert len(worker.sent) == 2


def test_two_simultaneous_first_lookups_share_one_instance_through_a_repair(hub, hub_registry):
    from provider_hub.registry import HubProxyProvider, retire_provider_instances
    from subliminal_patch.core import SZProviderPool

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    bundle = hub / "bundle"
    bundle.mkdir()
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    constructed = []
    entered = threading.Event()
    proceed = threading.Event()

    class OldClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = str(old / "bin" / "python")

        def __init__(self, **config):
            super().__init__(**config)
            constructed.append(self)
            entered.set()
            proceed.wait(10)

    class NewClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = str(new / "bin" / "python")

        def list_subtitles(self, video, languages):
            return ["found"]

    hub_registry.register("examplehub", OldClass)
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    found = {}

    def lookup(label):
        found[label] = pool["examplehub"]

    first = threading.Thread(target=lookup, args=("a",))
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=lookup, args=("b",))
    second.start()
    time.sleep(0.3)
    proceed.set()
    first.join(10)
    second.join(10)

    # One build, one instance, and both callers hold it.
    assert len(constructed) == 1
    assert found["a"] is found["b"] is pool.initialized_providers["examplehub"]
    # A duplicate that did get built is refused rather than overwriting the cached one.
    duplicate = OldClass()
    assert pool._install_provider("examplehub", duplicate, 0) is False
    assert pool.initialized_providers["examplehub"] is found["a"]

    hub_registry.register("examplehub", NewClass)
    drains = retire_provider_instances("examplehub")
    results = [pool._call_current("examplehub", found[label], lambda current: current.list_subtitles(None, set()))
               for label in ("a", "b")]
    for drain in drains:
        drain.join(10)

    # Neither caller failed, and both moved to the same replacement.
    assert results == [["found"], ["found"]]
    assert type(pool.initialized_providers["examplehub"]) is NewClass
    assert env_registry.cleanup_environments(hub) == [str(old)]


def test_a_pool_created_while_a_retirement_walks_the_pools_does_not_break_the_retirement(
    hub, hub_registry, monkeypatch
):
    from subliminal_patch import core
    from provider_hub.registry import retire_provider_instances
    from subliminal_patch.core import SZProviderPool

    class CreatesAPoolWhileBeingIterated(set):
        """A set that, like any set, refuses to change while it is walked, and
        another thread builds a pool at the moment the walk is under way."""

        def __iter__(self):
            for item in super().__iter__():
                yield item
                # Midway through the walk, another thread builds a pool.
                if not getattr(self, "spawned", False):
                    self.spawned = True
                    worker = threading.Thread(
                        target=lambda: self.newcomers.append(SZProviderPool(providers=[], provider_configs={}))
                    )
                    worker.start()
                    worker.join(0.3)

    live = CreatesAPoolWhileBeingIterated()
    live.newcomers = []
    monkeypatch.setattr(core, "_LIVE_POOLS", live)
    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")

    drains = retire_provider_instances("examplehub")
    for drain in drains:
        drain.join(10)

    # The pool that was already alive was still found and its instance retired.
    assert len(drains) == 1
    assert "examplehub" not in pool.initialized_providers
    assert stale._retired is True


def test_a_failed_retirement_is_reported_and_the_old_environment_stays_in_place(hub, tmp_path, monkeypatch, hub_registry):
    from provider_hub.registry import RetirementIncomplete, retire_provider_instances
    from provider_hub.service import repair_installation_environment
    from provider_hub.registry import register_active_provider_classes
    from provider_hub.state import active_installations
    from subliminal_patch.core import SZProviderPool

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python", needs_repair=True)}
    save_state(state)
    _patch_environment(monkeypatch)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    register_active_provider_classes(active_installations())
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    pool["examplehub"]

    def broken(name):
        raise RuntimeError("cannot detach")

    monkeypatch.setattr(pool, "detach_provider", broken)
    with pytest.raises(RetirementIncomplete):
        retire_provider_instances("examplehub")

    repair_installation_environment("examplehub")

    # The repair went through, but a worker may still run from the old environment: it is not removable.
    assert load_state()["installations"]["examplehub"]["python_path"].endswith("rebuilt/bin/python")
    assert old.exists()
    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()


WORKER_SCRIPT = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    m = json.loads(line)\n"
    "    print(json.dumps({'abi': m['abi'], 'id': m['id'], 'ok': True, 'payload': {}, 'events': []}), flush=True)\n"
    "    if m['op'] == 'shutdown':\n"
    "        break\n"
)


def _counting_popen(monkeypatch):
    import subprocess

    from provider_hub import worker

    spawned = []
    real = subprocess.Popen

    def counting(*args, **kwargs):
        process = real(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", counting)
    return spawned


def test_a_shutdown_request_never_starts_a_worker(monkeypatch):
    import sys

    from provider_hub.worker import ProviderWorkerClient, WorkerError

    spawned = _counting_popen(monkeypatch)
    client = ProviderWorkerClient([sys.executable, "-c", "pass"])
    client.start()
    first = client.process
    first.wait(10)
    assert len(spawned) == 1

    with pytest.raises(WorkerError):
        client.request("shutdown", {}, 2)

    # The process had ended before the request: nothing new was started for it.
    assert len(spawned) == 1
    assert client.process is first


def test_the_last_call_and_the_drain_stop_a_retired_worker_once(hub, hub_registry, monkeypatch):
    import sys

    from provider_hub.registry import retire_provider_instances
    from provider_hub.worker import ProviderWorkerClient

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    state = load_state()
    bundle = hub / "bundle"
    bundle.mkdir()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    spawned = _counting_popen(monkeypatch)
    client = ProviderWorkerClient([str(old / "bin" / "python")])
    client.command = [sys.executable, "-c", WORKER_SCRIPT]
    stale.worker_client = client
    stops = []
    real_stop = client.stop
    monkeypatch.setattr(client, "stop", lambda *args, **kwargs: stops.append(1) or real_stop(*args, **kwargs))

    with stale._operation():
        client.request("health", {}, timeout=10)
        drains = retire_provider_instances("examplehub")
    for drain in drains:
        drain.join(15)

    assert len(stops) == 1
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert env_registry.cleanup_environments(hub) == [str(old)]


def test_an_archive_selection_after_the_drain_deadline_still_runs_on_the_live_worker(hub, hub_registry, monkeypatch):
    import sys

    from provider_hub import registry
    from provider_hub.registry import retire_provider_instances
    from provider_hub.worker import ProviderWorkerClient

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    state = load_state()
    bundle = hub / "bundle"
    bundle.mkdir()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    # The expected time of a call is almost nothing, so the deadline passes at once.
    monkeypatch.setattr(registry, "_HOST_TIMEOUT_MARGIN_SECONDS", 0.05)
    monkeypatch.setattr(stale, "_request_timeout", lambda: 0.05)
    spawned = _counting_popen(monkeypatch)
    # The old environment's interpreter is a real executable that speaks the worker protocol.
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    client = ProviderWorkerClient([str(interpreter)])
    stale.worker_client = client

    with stale._operation():
        client.request("search", {}, timeout=10)
        drains = retire_provider_instances("examplehub")
        time.sleep(0.5)
        # Past the deadline: the call is still running, so its worker and environment are untouched.
        assert spawned[0].poll() is None
        assert env_registry.cleanup_environments(hub) == []
        # The archive selection of the same download still reaches the one live worker.
        stale._worker().select_archive_member({"members": []}, timeout=10)
        assert len(spawned) == 1
    for drain in drains:
        drain.join(15)

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert env_registry.cleanup_environments(hub) == [str(old)]


def test_no_request_starts_a_worker_after_the_proxy_is_stopped(hub, hub_registry, monkeypatch):
    import sys

    from provider_hub.worker import ProviderWorkerClient, WorkerError
    from subliminal_patch.core import ProviderRetiredError

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    spawned = _counting_popen(monkeypatch)
    client = ProviderWorkerClient([sys.executable, "-c", WORKER_SCRIPT])
    stale.worker_client = client
    client.request("search", {}, timeout=10)
    stale.retire()
    stale._shutdown_worker()
    assert spawned[0].poll() is not None

    # The proxy refuses to hand its client out, and the client refuses an ungated request on its own.
    with pytest.raises(ProviderRetiredError):
        stale._worker()
    with pytest.raises(WorkerError):
        client.select_archive_member({"members": []}, timeout=2)
    with pytest.raises(WorkerError):
        client.request("search", {}, timeout=2)
    assert len(spawned) == 1


SURVIVOR_SCRIPT = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "sys.stdin.readline()\n"
)


def _process_alive(pid):
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def test_a_failed_shutdown_still_ends_what_the_worker_started(tmp_path):
    import os
    import signal
    import sys

    from provider_hub.worker import ProviderWorkerClient

    pid_file = tmp_path / "child.pid"
    client = ProviderWorkerClient([sys.executable, "-c", SURVIVOR_SCRIPT, str(pid_file)])
    client.start()
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child = int(pid_file.read_text(encoding="utf-8"))
    try:
        assert _process_alive(child)
        # The worker dies on the shutdown request without answering: the shutdown fails after it exited.
        client.stop(grace_seconds=2)

        deadline = time.monotonic() + 5
        while _process_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_alive(child)
        assert client.group_gone(5)
    finally:
        if _process_alive(child):
            os.kill(child, signal.SIGKILL)


def test_the_environment_stays_pinned_while_the_workers_group_is_not_empty(hub, hub_registry, monkeypatch):
    import sys

    from provider_hub.registry import retire_provider_instances
    from provider_hub.worker import ProviderWorkerClient

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    state = load_state()
    bundle = hub / "bundle"
    bundle.mkdir()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    client = ProviderWorkerClient([str(interpreter)])
    stale.worker_client = client
    checks = []
    emptied = threading.Event()

    def group_gone(ceiling=10.0):
        checks.append(1)
        return emptied.wait(0.05)

    monkeypatch.setattr(client, "group_gone", group_gone)

    with stale._operation():
        client.request("search", {}, timeout=10)
        drains = retire_provider_instances("examplehub")
    deadline = time.monotonic() + 10
    while not checks and time.monotonic() < deadline:
        time.sleep(0.02)
    # The worker has exited, but something it started is still running: the environment is not free.
    assert checks
    assert env_registry.cleanup_environments(hub) == []

    emptied.set()
    for drain in drains:
        drain.join(15)
    # The hold is given up by the watcher a moment after the last survivor is gone.
    removed = []
    deadline = time.monotonic() + 10
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]


def test_a_repair_that_lands_between_the_generation_check_and_the_cache_store_still_retires_the_instance(
    hub, hub_registry, monkeypatch
):
    from subliminal_patch import core
    from provider_hub.registry import HubProxyProvider, retire_provider_instances
    from subliminal_patch.core import SZProviderPool

    old = _env(hub / "shared_envs" / "old")

    class OldClass(HubProxyProvider):
        provider_name = "examplehub"
        python_path = str(old / "bin" / "python")

    hub_registry.register("examplehub", OldClass)
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    checked = threading.Event()
    proceed = threading.Event()
    real_generation = core.provider_generation
    pausing = {"armed": False}

    def generation_then_pause(name):
        value = real_generation(name)
        if pausing["armed"] and threading.current_thread().name == "constructor":
            pausing["armed"] = False
            checked.set()
            proceed.wait(10)
        return value

    monkeypatch.setattr(core, "provider_generation", generation_then_pause)
    # The pause is armed after the build read its generation, so only the check under the lock stops.
    original_install = pool._install_provider

    def install(name, provider, generation=None):
        pausing["armed"] = True
        return original_install(name, provider, generation)

    monkeypatch.setattr(pool, "_install_provider", install)
    found = {}
    constructor = threading.Thread(target=lambda: found.setdefault("provider", pool["examplehub"]), name="constructor")
    constructor.start()
    assert checked.wait(10)

    # The check passed; the repair now bumps the generation and walks the pools while the store is still to happen.
    retiring = threading.Thread(target=lambda: found.setdefault("drains", retire_provider_instances("examplehub")))
    retiring.start()
    time.sleep(0.3)
    proceed.set()
    constructor.join(10)
    retiring.join(10)
    for drain in found.get("drains", []):
        drain.join(10)

    # The store completed first, so the retirement found the instance and took it out.
    assert "examplehub" not in pool.initialized_providers
    assert found["provider"]._retired is True
    assert len(found["drains"]) == 1


class _SurvivorsAfterStop:
    """Makes every worker report processes left in its group until ``emptied`` is set."""

    def __init__(self, monkeypatch):
        from provider_hub.worker import ProviderWorkerClient

        self.emptied = threading.Event()
        emptied = self.emptied
        monkeypatch.setattr(ProviderWorkerClient, "group_gone", lambda client, ceiling=10.0: emptied.is_set())


def _exiting_interpreter(path):
    import sys

    path.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    path.chmod(0o755)


def test_a_failed_probe_keeps_the_environment_while_its_worker_left_survivors(hub, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from provider_hub import service

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    env = _env(hub / "shared_envs" / "built")
    _exiting_interpreter(env / "bin" / "python")
    save_state(load_state())
    survivors = _SurvivorsAfterStop(monkeypatch)
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda _manifest, _path: None)

    with pytest.raises(Exception):
        service._smoke_validate_worker(SimpleNamespace(raw={}), bundle, env / "bin" / "python")
    # The install that ran the probe gives the environment up, as a failed install does.
    assert env_registry.discard_if_unused(hub, env) is False
    assert env_registry.cleanup_environments(hub) == []
    assert (env / "bin" / "python").exists()

    survivors.emptied.set()
    deadline = time.monotonic() + 10
    removed = []
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(env)]


def test_a_failed_connection_test_keeps_the_environment_while_its_worker_left_survivors(hub, tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.worker import ProviderWorkerClient

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    _exiting_interpreter(old / "bin" / "python")
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python")}
    save_state(state)
    survivors = _SurvivorsAfterStop(monkeypatch)
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda _manifest, _path: None)

    class RepairedMidTest(ProviderWorkerClient):
        def request(self, *args, **kwargs):
            # A repair repoints the installation while the test is running.
            moved = load_state()
            moved["installations"]["examplehub"]["python_path"] = str(new / "bin" / "python")
            save_state(moved)
            return super().request(*args, **kwargs)

    monkeypatch.setattr("provider_hub.service.ProviderWorkerClient", RepairedMidTest)

    result = service.test_provider_connection("examplehub")

    assert result["ok"] is False
    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()

    survivors.emptied.set()
    deadline = time.monotonic() + 10
    removed = []
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]


@pytest.mark.parametrize("path", ["delete", "config_reload", "pool_shutdown"])
def test_every_pool_termination_keeps_the_environment_until_the_workers_survivors_are_gone(
    hub, hub_registry, monkeypatch, path
):
    import sys

    from provider_hub.worker import ProviderWorkerClient

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    bundle = hub / "bundle"
    bundle.mkdir()
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    client = ProviderWorkerClient([str(interpreter)])
    stale.worker_client = client
    client.request("search", {}, timeout=10)
    survivors = _SurvivorsAfterStop(monkeypatch)

    if path == "delete":
        del pool["examplehub"]
    elif path == "config_reload":
        pool.retire_provider("examplehub")
    else:
        pool.terminate()
    # The installation is repaired meanwhile: it points at the new environment, nothing references the old one.
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)

    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()

    survivors.emptied.set()
    removed = []
    deadline = time.monotonic() + 10
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]


def test_a_worker_the_idle_reaper_stopped_still_protects_its_environment_through_a_repair(hub, hub_registry, monkeypatch):
    import sys

    from provider_hub.registry import retire_provider_instances
    from provider_hub.worker import ProviderWorkerClient, reap_idle_workers

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    bundle = hub / "bundle"
    bundle.mkdir()
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    client = ProviderWorkerClient([str(interpreter)])
    stale.worker_client = client
    client.request("search", {}, timeout=10)
    survivors = _SurvivorsAfterStop(monkeypatch)

    # The reaper ends the idle worker; something it started is still running.
    assert reap_idle_workers(idle_seconds=0) == 1
    # The repair then replaces the environment: the pooled proxy is retired through the one termination path.
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    drains = retire_provider_instances("examplehub")
    for drain in drains:
        drain.join(15)

    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()

    survivors.emptied.set()
    removed = []
    deadline = time.monotonic() + 10
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]


def test_an_earlier_workers_survivors_still_hold_the_environment_after_a_respawn(hub, hub_registry, monkeypatch):
    import os
    import sys

    from provider_hub import worker as worker_module
    from provider_hub.registry import retire_provider_instances
    from provider_hub.worker import ProviderWorkerClient, reap_idle_workers

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    bundle = hub / "bundle"
    bundle.mkdir()
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    client = ProviderWorkerClient([str(interpreter)])
    stale.worker_client = client
    client.request("search", {}, timeout=10)
    first_group = client.process.pid
    # A child of the first worker that no signal reaches: its group keeps answering.
    still_running = {first_group}
    real_killpg = os.killpg

    def killpg(group, sig):
        if sig == 0 and group in still_running:
            return None
        return real_killpg(group, sig)

    monkeypatch.setattr(worker_module.os, "killpg", killpg)

    # The idle reaper ends the first worker, a request starts a second one, and a repair follows.
    assert reap_idle_workers(idle_seconds=0) == 1
    client.request("search", {}, timeout=10)
    assert client.process.pid != first_group
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    for drain in retire_provider_instances("examplehub"):
        drain.join(15)

    # Only the second worker's group is empty; the first one's survivor still holds the environment.
    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()

    still_running.clear()
    removed = []
    deadline = time.monotonic() + 10
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]


def test_where_process_groups_cannot_be_checked_the_environment_stays_for_the_session(
    hub, hub_registry, monkeypatch, caplog
):
    import sys

    from provider_hub import worker as worker_module
    from provider_hub.worker import ProviderWorkerClient

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    bundle = hub / "bundle"
    bundle.mkdir()
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    client = ProviderWorkerClient([str(interpreter)])
    stale.worker_client = client
    client.request("search", {}, timeout=10)
    # A platform without process groups, as Windows is.
    monkeypatch.delattr(worker_module.os, "killpg", raising=False)
    worker_module._unverifiable_logged.clear()

    with caplog.at_level("INFO", logger=worker_module.logger.name):
        del pool["examplehub"]
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)

    time.sleep(0.3)
    assert env_registry.cleanup_environments(hub) == []
    assert env_registry.cleanup_environments(hub) == []
    assert (old / "bin" / "python").exists()
    notes = [record for record in caplog.records if "cannot be checked on this platform" in record.getMessage()]
    assert len(notes) == 1 and notes[0].levelname == "INFO"


def test_a_client_that_never_started_a_worker_releases_its_hold_even_without_process_groups(monkeypatch):
    from provider_hub import worker as worker_module
    from provider_hub.worker import ProviderWorkerClient, release_when_gone

    monkeypatch.delattr(worker_module.os, "killpg", raising=False)
    released = []

    assert release_when_gone(ProviderWorkerClient(["unused"]), lambda: released.append(1), "nothing") is True
    assert released == [1]


def test_two_simultaneous_first_calls_share_one_worker_client_through_a_retirement(hub, hub_registry, monkeypatch):
    import sys

    from provider_hub import registry
    from provider_hub.registry import retire_provider_instances
    from provider_hub.worker import ProviderWorkerClient

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    bundle = hub / "bundle"
    bundle.mkdir()
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    interpreter = old / "bin" / "python"
    interpreter.write_text(f"#!{sys.executable}\n{WORKER_SCRIPT}", encoding="utf-8")
    interpreter.chmod(0o755)
    type(stale).bundle_path = str(bundle)
    type(stale).manifest_json = "{}"
    created = []
    inside_creation = threading.Event()
    proceed = threading.Event()

    def make_client(command, **kwargs):
        client = ProviderWorkerClient([str(interpreter)])
        created.append(client)
        if len(created) == 1:
            # The first creation is paused half way: the second call arrives now.
            inside_creation.set()
            proceed.wait(10)
        return client

    monkeypatch.setattr(registry, "ProviderWorkerClient", make_client)
    survivors = _SurvivorsAfterStop(monkeypatch)
    clients = {}

    def first_call(label):
        with stale._operation():
            client = stale._worker()
            clients[label] = client
            client.request("search", {}, timeout=10)

    threads = [threading.Thread(target=first_call, args=(label,)) for label in ("a", "b")]
    threads[0].start()
    assert inside_creation.wait(10)
    threads[1].start()
    time.sleep(0.3)
    proceed.set()
    for thread in threads:
        thread.join(15)

    # One client was created and both calls used it.
    assert len(created) == 1
    assert clients["a"] is clients["b"] is stale.worker_client

    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, new / "bin" / "python")}
    save_state(state)
    for drain in retire_provider_instances("examplehub"):
        drain.join(15)

    # That one client was stopped, and the environment stays while its group is not empty.
    assert created[0]._has_exited(created[0].process)
    assert env_registry.cleanup_environments(hub) == []
    survivors.emptied.set()
    removed = []
    deadline = time.monotonic() + 10
    while not removed and time.monotonic() < deadline:
        removed = env_registry.cleanup_environments(hub)
        time.sleep(0.05)
    assert removed == [str(old)]


def test_a_retired_proxy_creates_no_worker_client(hub, hub_registry, monkeypatch):
    from provider_hub import registry
    from subliminal_patch.core import ProviderRetiredError

    old = _env(hub / "shared_envs" / "old")
    new = _env(hub / "shared_envs" / "new")
    pool, stale, _ = _pool_with_classes(hub_registry, old / "bin" / "python", new / "bin" / "python")
    type(stale).bundle_path = str(hub)
    type(stale).manifest_json = "{}"
    made = []
    monkeypatch.setattr(registry, "ProviderWorkerClient", lambda *args, **kwargs: made.append(1))
    stale.retire()

    with pytest.raises(ProviderRetiredError):
        stale._worker()
    assert made == [] and stale.worker_client is None


def test_cleanup_skips_an_environment_a_detached_process_still_uses_until_it_exits(hub, tmp_path):
    import signal
    import subprocess
    import sys

    env = _env(hub / "shared_envs" / "orphan")
    (env / "data.txt").write_text("in use", encoding="utf-8")
    save_state(load_state())
    ready = tmp_path / "ready"
    # A child that started its own session (so no process group of a worker holds it) and keeps a file of the environment open.
    script = (
        "import sys, time\n"
        "handle = open(sys.argv[1], 'rb')\n"
        "open(sys.argv[2], 'w').write('up')\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(env / "data.txt"), str(ready)], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()

        assert env_registry.cleanup_environments(hub) == []
        assert env_registry.discard_if_unused(hub, env) is False
        assert env.exists()

        child.send_signal(signal.SIGKILL)
        child.wait(10)
        assert env_registry.cleanup_environments(hub) == [str(env)]
        assert not env.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)


def _bundle_with(hub, content=b"class ExampleProvider: pass\n"):
    bundle = hub / "bundles" / "examplehub" / "1.0.0" / "c"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "provider.py").write_bytes(content)
    return bundle


@pytest.mark.parametrize("damage", ["changed", "symlinked"])
def test_repair_runs_no_plugin_code_from_a_bundle_that_does_not_match_its_manifest(hub, tmp_path, monkeypatch, damage):
    from provider_hub.service import repair_installation_environment

    old = _env(hub / "shared_envs" / "old")
    bundle = _bundle_with(hub)
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python")}
    save_state(state)
    built = _patch_environment(monkeypatch)
    probed = []
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda *args: probed.append(args))
    provider = bundle / "provider.py"
    if damage == "changed":
        provider.write_bytes(b"class ExampleProvider: ran_something_else = True\n")
    else:
        elsewhere = tmp_path / "elsewhere.py"
        elsewhere.write_bytes(b"class ExampleProvider: pass\n")
        provider.unlink()
        provider.symlink_to(elsewhere)

    with pytest.raises(Exception):
        repair_installation_environment("examplehub")

    row = load_state()["installations"]["examplehub"]
    assert row["needs_repair"] is True
    assert row["python_path"] == str(old / "bin" / "python")
    assert built == [] and probed == []
    assert not (hub / "shared_envs" / "rebuilt").exists()


@pytest.mark.parametrize("damage", ["changed", "symlinked"])
def test_boot_reconcile_runs_no_plugin_code_from_a_bundle_that_does_not_match_its_manifest(hub, tmp_path, damage):
    from provider_hub.manifest import validate_manifest
    from provider_hub.service import _verify_bundle_for_run

    first = _env(hub / "envs" / "alpha" / "1.0.0" / "fp1")
    second = _env(hub / "envs" / "beta" / "1.0.0" / "fp2")
    for env in (first, second):
        (env / "pyvenv.cfg").write_text(
            f"home = /usr/bin\nversion = {sys.version_info.major}.{sys.version_info.minor}.0\n", encoding="utf-8"
        )
    bundle = _bundle_with(hub)
    rows = []
    for provider_id, env in (("alpha", first), ("beta", second)):
        row = _active_row(bundle, env / "bin" / "python")
        row["provider_id"] = provider_id
        rows.append(row)
    state = load_state()
    state["installations"] = {row["provider_id"]: row for row in rows}
    save_state(state)
    provider = bundle / "provider.py"
    if damage == "changed":
        provider.write_bytes(b"class ExampleProvider: ran_something_else = True\n")
    else:
        elsewhere = tmp_path / "elsewhere.py"
        elsewhere.write_bytes(b"class ExampleProvider: pass\n")
        provider.unlink()
        provider.symlink_to(elsewhere)
    probed = []

    def validate(row, staged):
        return validate_manifest(row["manifest"], built_in_provider_ids=set())

    summary = env_registry.reconcile_environments(
        validate=validate, probe=lambda *args: probed.append(args), verify_bundle=_verify_bundle_for_run
    )

    assert probed == []
    assert summary["repointed"] == []
    assert "beta" in summary["needs_repair"]
    assert load_state()["installations"]["beta"]["needs_repair"] is True
    assert load_state()["installations"]["beta"]["python_path"] == str(second / "bin" / "python")


def test_a_pooled_worker_starts_only_from_a_bundle_that_matches_its_manifest(hub, hub_registry, monkeypatch):
    from provider_hub.registry import register_active_provider_classes
    from provider_hub.state import active_installations
    from subliminal_patch.core import SZProviderPool

    old = _env(hub / "shared_envs" / "old")
    bundle = _bundle_with(hub)
    state = load_state()
    state["installations"] = {"examplehub": _active_row(bundle, old / "bin" / "python")}
    save_state(state)
    assert register_active_provider_classes(active_installations()) == ["examplehub"]
    spawned = _counting_popen(monkeypatch)
    pool = SZProviderPool(providers=["examplehub"], provider_configs={})
    proxy = pool["examplehub"]
    # The code changes after the provider was registered and before its worker starts.
    (bundle / "provider.py").write_bytes(b"class ExampleProvider: ran_something_else = True\n")

    from provider_hub.worker import WorkerError

    with pytest.raises(WorkerError, match="does not match its manifest"):
        proxy._worker().request("search", {}, timeout=5)

    assert spawned == []
    assert load_state()["installations"]["examplehub"]["needs_repair"] is True


@pytest.mark.parametrize("how", ["run_by_the_venv_path", "venv_only_in_the_environment"])
def test_cleanup_skips_an_environment_a_detached_venv_python_uses_even_with_nothing_open(hub, tmp_path, how):
    import os
    import signal
    import subprocess
    import sys

    env = hub / "shared_envs" / "orphan"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env)], check=True, capture_output=True)
    save_state(load_state())
    ready = tmp_path / "ready"
    script = "import sys, time\nopen(sys.argv[1], 'w').write('up')\ntime.sleep(60)\n"
    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    if how == "run_by_the_venv_path":
        command = [str(env / "bin" / "python"), "-c", script, str(ready)]
    else:
        # The host interpreter, told about the environment only through the variable a venv sets.
        command = [os.path.realpath(sys.executable), "-c", script, str(ready)]
        environment["VIRTUAL_ENV"] = str(env)
    child = subprocess.Popen(command, start_new_session=True, cwd=str(tmp_path), env=environment)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()

        # It has no file of the environment open, runs the host's python binary and sits elsewhere.
        assert env_registry.cleanup_environments(hub) == []
        assert env.exists()

        child.send_signal(signal.SIGKILL)
        child.wait(10)
        assert env_registry.cleanup_environments(hub) == [str(env)]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)


def test_a_restored_environment_does_not_clear_the_flag_of_a_bundle_that_fails_its_check(hub):
    from provider_hub.manifest import validate_manifest
    from provider_hub.service import _verify_bundle_for_run

    env = _env(hub / "envs" / "alpha" / "1.0.0" / "fp1")
    bundle = _bundle_with(hub)
    row = _active_row(bundle, env / "bin" / "python", needs_repair=True, last_error=env_registry.NEEDS_REPAIR_MESSAGE)
    state = load_state()
    state["installations"] = {"examplehub": row}
    save_state(state)
    # The environment is back, but the code under it was changed.
    (bundle / "provider.py").write_bytes(b"class ExampleProvider: ran_something_else = True\n")

    def validate(row, staged):
        return validate_manifest(row["manifest"], built_in_provider_ids=set())

    summary = env_registry.reconcile_environments(
        validate=validate, probe=lambda *args: None, verify_bundle=_verify_bundle_for_run
    )

    row = load_state()["installations"]["examplehub"]
    assert summary["needs_repair"] == ["examplehub"]
    assert row["needs_repair"] is True
    assert row["last_error"] == env_registry.NEEDS_REPAIR_MESSAGE

    # With the bundle intact the same pass clears it.
    (bundle / "provider.py").write_bytes(b"class ExampleProvider: pass\n")
    env_registry.reconcile_environments(validate=validate, probe=lambda *args: None, verify_bundle=_verify_bundle_for_run)
    row = load_state()["installations"]["examplehub"]
    assert "needs_repair" not in row and row["last_error"] is None

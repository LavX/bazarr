# coding=utf-8
"""Repairing a plugin's environment, and what a failed check leaves behind."""
import threading
import time
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

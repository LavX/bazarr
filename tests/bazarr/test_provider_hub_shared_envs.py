# coding=utf-8
"""Plugins with the same dependencies share one environment.

The environment key is a fingerprint of the dependency set. Environments built
before sharing existed are adopted where they are, boot never runs pip, and an
environment is removed only when no installation points at it.
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import provider_hub.venv as venvmod
from provider_hub import env_registry
from provider_hub.state import load_state, save_state
from provider_hub.venv import PluginEnvironment, environment_fingerprint, pin_scope

REQ_A = SimpleNamespace(pip_line="alpha==1.0 --hash=sha256:" + "a" * 64)
REQ_B = SimpleNamespace(pip_line="beta==2.0 --hash=sha256:" + "b" * 64)


def _manifest(provider_id, requirements, version="1.0.0"):
    return SimpleNamespace(provider_id=provider_id, version=version, dependency_requirements=tuple(requirements))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    root = tmp_path / "provider_hub"
    root.mkdir()
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(root / "state.json"))
    return root


@pytest.fixture
def no_subprocess(monkeypatch):
    calls = []

    def refuse(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(venvmod.subprocess, "run", refuse)
    return calls


def _legacy_env(root, provider_id, requirements, version="1.0.0", old_fp="0ldf1ngerprint0"):
    """An environment as the per-provider layout left it on disk."""
    env = root / "envs" / provider_id / version / old_fp
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("", encoding="utf-8")
    (env / "pyvenv.cfg").write_text(
        f"home = /usr/bin\nversion = {sys.version_info.major}.{sys.version_info.minor}.0\n", encoding="utf-8"
    )
    if requirements:
        (env / "requirements.txt").write_text(
            "\n".join(item.pip_line for item in requirements) + "\n", encoding="utf-8"
        )
    return env


def _row(provider_id, env=None, requirements=(), active_path="bundle"):
    return {
        "provider_id": provider_id,
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "active_path": active_path,
        "python_path": str(env / "bin" / "python") if env else None,
        "manifest": {"requirements": [item.pip_line for item in requirements]},
        "last_error": None,
    }


def _validate(row, staged):
    lines = row["manifest"]["requirements"]
    return _manifest(row["provider_id"], [SimpleNamespace(pip_line=line) for line in lines])


def _never_probe(manifest, bundle, python):
    raise AssertionError("the probe must not run")


def _state_with(*rows):
    state = load_state()
    state["installations"] = {row["provider_id"]: row for row in rows}
    save_state(state)


def test_fingerprint_ignores_provider_id_version_and_order():
    first = environment_fingerprint(_manifest("one", [REQ_A, REQ_B], "1.0.0"))
    second = environment_fingerprint(_manifest("two", [REQ_B, REQ_A], "9.9.9"))
    assert first == second
    assert first != environment_fingerprint(_manifest("one", [REQ_A]))


def test_old_environment_is_adopted_in_place(hub, no_subprocess):
    env = _legacy_env(hub, "alpha", [REQ_A])
    _state_with(_row("alpha", env, [REQ_A]))

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    state = load_state()
    fingerprint = environment_fingerprint(_manifest("alpha", [REQ_A]))
    assert state["plugin_envs"] == {fingerprint: {"path": str(env)}}
    assert summary["adopted"] == ["alpha"]
    # Not moved, not rebuilt, still what the installation runs.
    assert state["installations"]["alpha"]["python_path"] == str(env / "bin" / "python")
    assert (env / "bin" / "python").exists()
    assert not state["installations"]["alpha"].get("needs_repair")
    assert no_subprocess == []


def test_environment_with_other_requirements_is_not_adopted(hub, no_subprocess):
    env = _legacy_env(hub, "alpha", [REQ_B])
    _state_with(_row("alpha", env, [REQ_A]))

    env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    state = load_state()
    assert state["plugin_envs"] == {}
    # It keeps running on what it has: nothing is flagged, nothing is touched.
    assert state["installations"]["alpha"]["python_path"] == str(env / "bin" / "python")
    assert not state["installations"]["alpha"].get("needs_repair")


def test_two_plugins_with_the_same_dependencies_share_one_environment(hub, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)
    environment = PluginEnvironment(hub)

    first_path = environment.install(_manifest("alpha", [REQ_A]))
    state = load_state()
    env_registry.register_environment(state, environment_fingerprint(_manifest("alpha", [REQ_A])), first_path)
    save_state(state)
    pip_installs_after_first = [c for c in calls if c[1:4] == ["-m", "pip", "install"]]
    calls.clear()

    second_path = environment.install(_manifest("beta_plugin", [REQ_A], version="7.1.0"))

    assert second_path == first_path
    assert first_path.parent == hub / "shared_envs"
    assert len(pip_installs_after_first) == 1
    # The second plugin runs no venv creation and no pip install, only a local consistency check.
    assert [c for c in calls if c[1:3] == ["-m", "venv"] or c[1:4] == ["-m", "pip", "install"]] == []


def test_different_dependencies_get_a_separate_environment(hub, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)
    environment = PluginEnvironment(hub)

    first = environment.install(_manifest("alpha", [REQ_A]))
    state = load_state()
    env_registry.register_environment(state, environment_fingerprint(_manifest("alpha", [REQ_A])), first)
    save_state(state)
    calls.clear()

    second = environment.install(_manifest("alpha", [REQ_A, REQ_B]))

    assert second != first
    # The first environment's interpreter is never used to install anything.
    first_python = str(first / "bin" / "python")
    assert [c for c in calls if c[0] == first_python and c[1:4] == ["-m", "pip", "install"]] == []
    assert (first / "requirements.txt").read_text(encoding="utf-8").strip() == REQ_A.pip_line


def test_missing_environment_is_flagged_at_boot_without_pip(hub, no_subprocess):
    gone = hub / "envs" / "alpha" / "1.0.0" / "missing"
    _state_with(_row("alpha", gone, [REQ_A]), _row("noenv", None, []))

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    state = load_state()
    for provider_id in ("alpha", "noenv"):
        row = state["installations"][provider_id]
        assert row["needs_repair"] is True
        assert row["last_error"] == env_registry.NEEDS_REPAIR_MESSAGE
    assert sorted(summary["needs_repair"]) == ["alpha", "noenv"]
    assert not (hub / "shared_envs").exists()
    assert no_subprocess == []


def test_flag_clears_when_the_environment_is_back(hub, no_subprocess):
    env = _legacy_env(hub, "alpha", [REQ_A])
    row = _row("alpha", env, [REQ_A])
    row["needs_repair"] = True
    row["last_error"] = env_registry.NEEDS_REPAIR_MESSAGE
    _state_with(row)

    env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    row = load_state()["installations"]["alpha"]
    assert "needs_repair" not in row
    assert row["last_error"] is None


def test_duplicate_environment_is_repointed_only_after_the_probe_passes(hub, no_subprocess):
    first = _legacy_env(hub, "alpha", [REQ_A], old_fp="fp1")
    second = _legacy_env(hub, "beta_plugin", [REQ_A], old_fp="fp2")
    _state_with(_row("alpha", first, [REQ_A]), _row("beta_plugin", second, [REQ_A]))
    probed = []

    def probe(manifest, bundle, python):
        probed.append((manifest.provider_id, python))

    summary = env_registry.reconcile_environments(validate=_validate, probe=probe)

    state = load_state()
    assert probed == [("beta_plugin", first / "bin" / "python")]
    assert summary["repointed"] == ["beta_plugin"]
    assert state["installations"]["beta_plugin"]["python_path"] == str(first / "bin" / "python")
    # Repointing deletes nothing at boot; the old directory waits for the cleanup.
    assert second.exists()
    removed = env_registry.cleanup_environments(hub)
    assert removed == [str(second)]
    assert not second.exists()
    assert first.exists()


def test_old_environment_is_kept_when_the_probe_fails(hub, no_subprocess):
    first = _legacy_env(hub, "alpha", [REQ_A], old_fp="fp1")
    second = _legacy_env(hub, "beta_plugin", [REQ_A], old_fp="fp2")
    _state_with(_row("alpha", first, [REQ_A]), _row("beta_plugin", second, [REQ_A]))

    def failing_probe(manifest, bundle, python):
        raise RuntimeError("import failed")

    summary = env_registry.reconcile_environments(validate=_validate, probe=failing_probe)

    state = load_state()
    assert summary["kept"] == ["beta_plugin"]
    assert state["installations"]["beta_plugin"]["python_path"] == str(second / "bin" / "python")
    assert env_registry.cleanup_environments(hub) == []
    assert second.exists()


def test_cleanup_removes_only_unreferenced_environments(hub):
    kept = hub / "shared_envs" / "kept"
    orphan = hub / "shared_envs" / "orphan"
    pinned = hub / "shared_envs" / "pinned"
    for env in (kept, orphan, pinned):
        (env / "bin").mkdir(parents=True)
        (env / "bin" / "python").write_text("", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {name: {"path": str(hub / "shared_envs" / name)} for name in ("kept", "orphan", "pinned")}
    state["installations"] = {"alpha": _row("alpha", kept, [])}
    state["installations"]["alpha"]["staged_python_path"] = None
    save_state(state)

    with pin_scope():
        venvmod._pin(pinned)
        removed = env_registry.cleanup_environments(hub)
    assert removed == [str(orphan)]
    assert kept.exists() and pinned.exists() and not orphan.exists()
    assert set(load_state()["plugin_envs"]) == {"kept", "pinned"}

    # The pin is gone with its scope, and a staged reference counts as a reference.
    state = load_state()
    state["installations"]["alpha"]["staged_python_path"] = str(pinned / "bin" / "python")
    save_state(state)
    assert env_registry.cleanup_environments(hub) == []
    state["installations"]["alpha"]["staged_python_path"] = None
    state["installations"]["alpha"]["python_path"] = None
    save_state(state)
    assert sorted(env_registry.cleanup_environments(hub)) == sorted([str(kept), str(pinned)])
    assert load_state()["plugin_envs"] == {}


def _fake_run_that_builds(calls):
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    return fake_run


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, "pip"),
    subprocess.TimeoutExpired("pip", 60),
])
def test_a_broken_shared_environment_is_replaced_never_modified(hub, monkeypatch, failure):
    manifest = _manifest("alpha", [REQ_A])
    old = hub / "shared_envs" / "recorded"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "python").write_text("", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(old)}}
    save_state(state)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == str(old / "bin" / "python"):
            raise failure
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)

    built = PluginEnvironment(hub).install(manifest)

    assert built != old
    assert built.parent == hub / "shared_envs"
    assert built.name.startswith(environment_fingerprint(manifest) + "-")
    # The live environment only ever saw the consistency check.
    assert [c[1:] for c in calls if c[0] == str(old / "bin" / "python")] == [["-m", "pip", "check"]]
    assert any(c[0] == str(built / "bin" / "python") and c[1:4] == ["-m", "pip", "install"] for c in calls)

    # Recording the new one frees the old one for the next cleanup.
    state = load_state()
    env_registry.register_environment(state, environment_fingerprint(manifest), built)
    state["installations"] = {"alpha": _row("alpha", built, [REQ_A])}
    save_state(state)
    assert env_registry.cleanup_environments(hub) == [str(old)]
    assert built.exists()


def test_a_recorded_directory_without_an_interpreter_is_never_rebuilt_in_place(hub, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    gone = hub / "shared_envs" / "recorded"
    (gone / "lib").mkdir(parents=True)
    (gone / "lib" / "package.py").write_text("running workers may still import this", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(gone)}}
    save_state(state)
    calls = []
    monkeypatch.setattr(venvmod.subprocess, "run", _fake_run_that_builds(calls))

    built = PluginEnvironment(hub).install(manifest)

    assert built != gone
    assert built.parent == hub / "shared_envs"
    assert built.name.startswith(environment_fingerprint(manifest) + "-")
    assert (built / "bin" / "python").exists()
    # The original directory is exactly as it was: nothing was built into it.
    assert sorted(path.name for path in gone.rglob("*")) == ["lib", "package.py"]
    assert not any(str(gone) in call or str(gone / "bin" / "python") in call for call in calls)


def test_two_concurrent_first_installs_of_one_dependency_set_build_once(hub, monkeypatch):
    import threading
    import time

    manifest_one = _manifest("alpha", [REQ_A])
    manifest_two = _manifest("beta_plugin", [REQ_A], version="3.0.0")
    calls = []
    guard = threading.Lock()

    def slow_run(cmd, **kwargs):
        with guard:
            calls.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
            time.sleep(0.2)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", slow_run)
    environment = PluginEnvironment(hub)
    results = {}
    errors = []

    def install(name, manifest):
        try:
            with pin_scope():
                results[name] = environment.install(manifest)
                time.sleep(0.3)
        except Exception as error:
            errors.append(error)

    threads = [
        threading.Thread(target=install, args=("one", manifest_one)),
        threading.Thread(target=install, args=("two", manifest_two)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not errors
    assert results["one"] == results["two"] == hub / "shared_envs" / environment_fingerprint(manifest_one)
    pip_installs = [call for call in calls if call[1:4] == ["-m", "pip", "install"]]
    venv_creations = [call for call in calls if call[1:3] == ["-m", "venv"]]
    assert len(pip_installs) == 1
    assert len(venv_creations) == 1


def test_an_unfinished_canonical_directory_is_not_built_into(hub, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    canonical = hub / "shared_envs" / environment_fingerprint(manifest)
    (canonical / "bin").mkdir(parents=True)
    (canonical / "bin" / "python").write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(venvmod.subprocess, "run", _fake_run_that_builds(calls))

    built = PluginEnvironment(hub).install(manifest)

    assert built != canonical
    assert not any(str(canonical) in call or str(canonical / "bin" / "python") in call for call in calls)


def test_a_recorded_path_outside_the_hub_is_ignored(hub, tmp_path, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    outside = tmp_path / "elsewhere"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "python").write_text("", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(outside)}}
    save_state(state)
    calls = []
    monkeypatch.setattr(venvmod.subprocess, "run", _fake_run_that_builds(calls))

    built = PluginEnvironment(hub).install(manifest)

    assert built == hub / "shared_envs" / environment_fingerprint(manifest)
    assert not any(str(outside) in " ".join(c) for c in calls)


def test_reusing_a_recorded_environment_runs_no_install(hub, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    env = hub / "shared_envs" / "recorded"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(env)}}
    save_state(state)
    calls = []
    monkeypatch.setattr(venvmod.subprocess, "run", _fake_run_that_builds(calls))

    with pin_scope():
        assert PluginEnvironment(hub).install(manifest) == env
        # Held by the install in flight: a cleanup leaves it, though nothing references it yet.
        assert env_registry.cleanup_environments(hub) == []
    assert [c[1:] for c in calls] == [["-m", "pip", "check"]]
    assert env_registry.cleanup_environments(hub) == [str(env)]


def test_fingerprint_depends_on_architecture_and_abi(monkeypatch):
    import platform
    import sysconfig

    manifest = _manifest("alpha", [REQ_A])
    here = environment_fingerprint(manifest)
    real_machine = platform.machine()
    monkeypatch.setattr(platform, "machine", lambda: "riscv64" if real_machine != "riscv64" else "x86_64")
    assert environment_fingerprint(manifest) != here
    monkeypatch.undo()
    real = sysconfig.get_config_var
    monkeypatch.setattr(sysconfig, "get_config_var",
                        lambda name: "cpython-314t-other" if name == "SOABI" else real(name))
    assert environment_fingerprint(manifest) != here


def _elf_python(env, machine):
    header = bytearray(b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 9)
    header += (3).to_bytes(2, "little") + machine.to_bytes(2, "little")
    (env / "bin" / "python").write_bytes(bytes(header))


def test_an_environment_built_for_another_architecture_is_not_adopted(hub, no_subprocess, monkeypatch):
    import platform

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    foreign = _legacy_env(hub, "alpha", [REQ_A], old_fp="fp1")
    _elf_python(foreign, 183)
    native = _legacy_env(hub, "beta_plugin", [REQ_B], old_fp="fp2")
    _elf_python(native, 62)
    _state_with(_row("alpha", foreign, [REQ_A]), _row("beta_plugin", native, [REQ_B]))

    env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    recorded = {entry["path"] for entry in load_state()["plugin_envs"].values()}
    assert recorded == {str(native)}


def test_cleanup_never_leaves_the_hub_directory(hub, tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "python").write_text("", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {"x": {"path": str(outside)}}
    save_state(state)

    assert env_registry.cleanup_environments(hub) == []
    assert outside.exists()


def _plain_env(path):
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "python").write_text("", encoding="utf-8")
    return path


@pytest.mark.parametrize("parent", ["envs/plugin", "shared_envs", "envs", "envs/plugin/1.0.0"])
def test_a_registry_entry_naming_a_parent_directory_removes_nothing_beneath_it(hub, parent):
    referenced = _plain_env(hub / "envs" / "plugin" / "1.0.0" / "fp")
    pinned = _plain_env(hub / "shared_envs" / "pinned")
    state = load_state()
    state["plugin_envs"] = {"parent": {"path": str(hub / parent)}}
    state["installations"] = {"alpha": _row("alpha", referenced, [])}
    save_state(state)

    with pin_scope():
        venvmod._pin(pinned)
        assert env_registry.cleanup_environments(hub) == []

    assert (referenced / "bin" / "python").exists()
    assert (pinned / "bin" / "python").exists()
    assert (hub / "envs").is_dir() and (hub / "shared_envs").is_dir()


def test_an_unreferenced_parent_entry_is_not_a_deletable_environment(hub):
    inside = _plain_env(hub / "envs" / "plugin" / "1.0.0" / "fp")
    state = load_state()
    state["plugin_envs"] = {"parent": {"path": str(hub / "envs" / "plugin")}}
    save_state(state)

    assert env_registry.cleanup_environments(hub) == []

    # The malformed record protects what lies beneath it rather than taking it out.
    assert inside.exists()
    assert (hub / "envs" / "plugin").is_dir()


def test_the_legacy_sweep_does_not_follow_a_symlinked_ancestor_out_of_the_hub(hub, tmp_path):
    outside = tmp_path / "elsewhere"
    victim = _plain_env(outside / "1.0.0" / "fp")
    (hub / "envs").mkdir()
    (hub / "envs" / "plugin").symlink_to(outside, target_is_directory=True)

    assert env_registry.cleanup_environments(hub) == []

    assert (victim / "bin" / "python").exists()


def test_a_symlinked_container_directory_is_not_swept(hub, tmp_path):
    outside = tmp_path / "elsewhere"
    victim = _plain_env(outside / "fp")
    (hub / "shared_envs").symlink_to(outside, target_is_directory=True)

    assert env_registry.cleanup_environments(hub) == []

    assert (victim / "bin" / "python").exists()


def test_a_registry_entry_behind_a_symlinked_ancestor_is_not_removed(hub, tmp_path):
    outside = tmp_path / "elsewhere"
    victim = _plain_env(outside / "fp")
    (hub / "shared_envs").symlink_to(outside, target_is_directory=True)
    state = load_state()
    state["plugin_envs"] = {"x": {"path": str(hub / "shared_envs" / "fp")}}
    save_state(state)

    assert env_registry.cleanup_environments(hub) == []

    assert (victim / "bin" / "python").exists()


def test_a_symlink_where_an_environment_should_be_is_left_alone(hub, tmp_path):
    outside = _plain_env(tmp_path / "elsewhere" / "fp")
    (hub / "shared_envs").mkdir()
    (hub / "shared_envs" / "link").symlink_to(outside, target_is_directory=True)

    assert env_registry.cleanup_environments(hub) == []

    assert (outside / "bin" / "python").exists()
    assert (hub / "shared_envs" / "link").is_symlink()


def test_a_stopped_install_removes_nothing_outside_the_hub(hub, tmp_path):
    from provider_hub.service import _discard_stopped_install

    outside = _plain_env(tmp_path / "elsewhere" / "fp")
    link = hub / "shared_envs"
    link.symlink_to(outside.parent, target_is_directory=True)
    sibling = tmp_path / "provider_hub_sibling"
    sibling.mkdir()

    with pin_scope():
        _discard_stopped_install([outside, link / "fp", sibling, hub], {})

    assert (outside / "bin" / "python").exists()
    assert sibling.exists() and hub.exists()


def test_a_stopped_install_removes_a_bundle_inside_the_hub(hub):
    from provider_hub.service import _discard_stopped_install

    bundle = hub / "bundles" / "alpha" / "1.0.0"
    bundle.mkdir(parents=True)
    (bundle / "provider.py").write_text("", encoding="utf-8")

    with pin_scope():
        _discard_stopped_install([bundle], {})

    assert not bundle.exists()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def _recorded_intact(hub, manifest):
    env = hub / "shared_envs" / "recorded"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("", encoding="utf-8")
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(env)}}
    save_state(state)
    return env


def test_the_reuse_check_is_bounded_by_the_remaining_install_budget(hub, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    env = _recorded_intact(hub, manifest)
    timeouts = []

    def fake_run(cmd, **kwargs):
        timeouts.append((cmd[0], kwargs.get("timeout")))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)

    assert PluginEnvironment(hub).install(manifest, timeout=5) == env
    assert len(timeouts) == 1 and timeouts[0][1] <= 5


def test_a_failed_reuse_check_does_not_restart_the_install_budget(hub, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    old = _recorded_intact(hub, manifest)
    clock = _Clock()
    monkeypatch.setattr(venvmod, "time", clock)
    timeouts = []

    def fake_run(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if cmd[0] == str(old / "bin" / "python"):
            clock.now += 4
            raise subprocess.CalledProcessError(1, "pip")
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)

    PluginEnvironment(hub).install(manifest, timeout=5)

    # The check used 4 of the 5 seconds, so every build step gets what is left.
    assert all(value <= 1.0 + 1e-9 for value in timeouts[1:])
    assert len(timeouts) > 1


def test_a_reuse_check_that_spends_the_budget_stops_the_install(hub, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    old = _recorded_intact(hub, manifest)
    clock = _Clock()
    monkeypatch.setattr(venvmod, "time", clock)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        clock.now += 6
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)

    with pytest.raises(venvmod.PluginEnvironmentError, match="time budget"):
        PluginEnvironment(hub).install(manifest, timeout=5)

    assert [c[0] for c in calls] == [str(old / "bin" / "python")]


def test_an_environment_with_packages_is_not_adopted_as_an_empty_one(hub, no_subprocess):
    mispointed = _legacy_env(hub, "alpha", [REQ_A])
    bare = _legacy_env(hub, "beta_plugin", [])
    _state_with(_row("alpha", mispointed, []), _row("beta_plugin", bare, []))

    env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    recorded = {entry["path"] for entry in load_state()["plugin_envs"].values()}
    # No requirements file is accepted for an empty set; one that lists packages is not.
    assert recorded == {str(bare)}


MALFORMED_STATES = [
    "null",
    "[]",
    '"text"',
    "42",
    "{}",
    '{"installations": null}',
    '{"installations": []}',
    '{"installations": {"alpha": 5}}',
    '{"installations": {"alpha": {"python_path": 7}}}',
    '{"installations": {}, "plugin_envs": []}',
    "{not json",
    None,
]


def _two_environments(hub):
    shared = _plain_env(hub / "shared_envs" / "recorded")
    legacy = _plain_env(hub / "envs" / "alpha" / "1.0.0" / "fp")
    return shared, legacy


@pytest.mark.parametrize("document", MALFORMED_STATES)
def test_cleanup_deletes_nothing_when_the_state_cannot_be_read(hub, document):
    shared, legacy = _two_environments(hub)
    state_file = hub / "state.json"
    if document is not None:
        state_file.write_text(document, encoding="utf-8")

    assert env_registry.cleanup_environments(hub) == []

    assert (shared / "bin" / "python").exists() and (legacy / "bin" / "python").exists()
    # The unreadable document is left as it was, not rewritten as an empty default.
    assert state_file.exists() is (document is not None)
    if document is not None:
        assert state_file.read_text(encoding="utf-8") == document


@pytest.mark.parametrize("document", [item for item in MALFORMED_STATES if item is not None])
def test_a_stopped_install_keeps_a_recorded_environment_when_the_state_cannot_be_read(hub, document):
    recorded = _plain_env(hub / "shared_envs" / "recorded")
    (hub / "state.json").write_text(document, encoding="utf-8")

    assert env_registry.discard_if_unused(hub, recorded) is False

    assert (recorded / "bin" / "python").exists()


@pytest.mark.parametrize("document", ["null", "[]", '"text"', "42", "{not json"])
def test_boot_reconcile_leaves_an_unreadable_state_alone(hub, no_subprocess, document):
    _legacy_env(hub, "alpha", [REQ_A])
    (hub / "state.json").write_text(document, encoding="utf-8")

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    assert summary == {"adopted": [], "repointed": [], "needs_repair": [], "kept": []}
    assert (hub / "state.json").read_text(encoding="utf-8") == document


CORRUPT_ROOTS = [
    "null", "[]", '"text"', "42", "{not json",
    "{}",
    '{"catalog_entries": {}, "jobs": [], "plugin_envs": {}}',
    '{"installations": null, "jobs": []}',
    '{"installations": []}',
]


@pytest.mark.parametrize("document", CORRUPT_ROOTS)
def test_a_writer_never_overwrites_an_unreadable_state_and_cleanup_then_deletes_nothing(hub, document):
    from provider_hub.state import StateCorruptError, corrupt_copies, mutate_state

    shared, legacy = _two_environments(hub)
    state_file = hub / "state.json"
    state_file.write_text(document, encoding="utf-8")

    for _ in range(2):
        with pytest.raises(StateCorruptError):
            mutate_state(lambda state: state.setdefault("jobs", []).append("x"))

    assert state_file.read_text(encoding="utf-8") == document
    copies = corrupt_copies()
    # One copy for one distinct damaged document, with its exact content, never moved or deleted.
    assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == document
    assert env_registry.cleanup_environments(hub) == []
    assert (shared / "bin" / "python").exists() and (legacy / "bin" / "python").exists()
    assert copies[0].exists()


def test_a_recreated_empty_state_does_not_unlock_cleanup_while_a_damaged_copy_is_kept(hub):
    from provider_hub.state import corrupt_copies, mutate_state

    shared, legacy = _two_environments(hub)
    state_file = hub / "state.json"
    state_file.write_text("null", encoding="utf-8")
    with pytest.raises(ValueError):
        mutate_state(lambda state: None)
    # Someone clears the broken file; the next writer starts from the default, a valid empty inventory.
    state_file.unlink()
    mutate_state(lambda state: None)
    assert load_state()["installations"] == {}

    assert env_registry.cleanup_environments(hub) == []
    assert (shared / "bin" / "python").exists() and (legacy / "bin" / "python").exists()

    # Once an operator has removed the copy, cleanup works again.
    for copy in corrupt_copies():
        copy.unlink()
    assert sorted(env_registry.cleanup_environments(hub)) == sorted([str(shared), str(legacy)])


def test_a_missing_state_file_starts_from_the_default_and_the_recovery_path_keeps_a_copy(hub):
    from provider_hub.state import corrupt_copies, mutate_state

    mutate_state(lambda state: state.update(marker=1))
    assert load_state()["marker"] == 1
    assert corrupt_copies() == []

    (hub / "state.json").write_text("[]", encoding="utf-8")
    mutate_state(lambda state: state.update(marker=2), recover_corrupt=True)
    assert load_state()["marker"] == 2
    assert [copy.read_text(encoding="utf-8") for copy in corrupt_copies()] == ["[]"]


@pytest.mark.parametrize("document", ["{}", '{"catalog_entries": {}, "jobs": []}'])
def test_boot_reconcile_and_cleanup_do_not_turn_a_document_without_an_inventory_into_an_empty_one(
    hub, no_subprocess, document
):
    from provider_hub.state import corrupt_copies

    shared, legacy = _two_environments(hub)
    (hub / "state.json").write_text(document, encoding="utf-8")

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)
    removed = env_registry.cleanup_environments(hub)

    assert summary == {"adopted": [], "repointed": [], "needs_repair": [], "kept": []}
    assert removed == []
    assert (hub / "state.json").read_text(encoding="utf-8") == document
    assert len(corrupt_copies()) == 1
    assert (shared / "bin" / "python").exists() and (legacy / "bin" / "python").exists()


def test_a_symlinked_environment_container_is_never_reused_or_built_into(hub, tmp_path, monkeypatch):
    manifest = _manifest("alpha", [REQ_A])
    outside = tmp_path / "elsewhere"
    canonical = outside / environment_fingerprint(manifest)
    _plain_env(canonical)
    (canonical / venvmod.COMPLETE_MARKER).write_text("", encoding="utf-8")
    recorded = _plain_env(outside / "recorded")
    (hub / "shared_envs").symlink_to(outside, target_is_directory=True)
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(hub / "shared_envs" / "recorded")}}
    save_state(state)
    calls = []
    monkeypatch.setattr(venvmod.subprocess, "run", _fake_run_that_builds(calls))

    with pytest.raises(venvmod.PluginEnvironmentError):
        PluginEnvironment(hub).install(manifest)

    # Neither the finished environment outside the hub nor the recorded one was used or touched.
    assert calls == []
    assert sorted(path.name for path in outside.iterdir()) == sorted([canonical.name, recorded.name])


def test_boot_reconcile_ignores_an_interpreter_outside_the_hub(hub, tmp_path, no_subprocess):
    outside_root = tmp_path / "elsewhere"
    outside = _legacy_env(outside_root, "alpha", [REQ_A])
    _state_with(_row("alpha", outside, [REQ_A]))

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    assert summary == {"adopted": [], "repointed": [], "needs_repair": [], "kept": []}
    state = load_state()
    assert state["plugin_envs"] == {}
    assert state["installations"]["alpha"]["python_path"] == str(outside / "bin" / "python")


def test_boot_reconcile_ignores_an_interpreter_reached_through_a_symlink(hub, tmp_path, no_subprocess):
    outside = _legacy_env(tmp_path / "elsewhere", "alpha", [REQ_A])
    (hub / "envs").symlink_to(tmp_path / "elsewhere" / "envs", target_is_directory=True)
    through_link = hub / "envs" / "alpha" / "1.0.0" / "0ldf1ngerprint0"
    assert (through_link / "bin" / "python").exists()
    _state_with(_row("alpha", through_link, [REQ_A]))

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    assert summary["adopted"] == [] and load_state()["plugin_envs"] == {}
    assert (outside / "bin" / "python").exists()


def test_boot_reconcile_never_probes_or_adopts_a_recorded_environment_outside_the_hub(hub, tmp_path, no_subprocess):
    outside = _legacy_env(tmp_path / "elsewhere", "recorded", [REQ_A])
    inside = _legacy_env(hub, "alpha", [REQ_A])
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(_manifest("alpha", [REQ_A])): {"path": str(outside)}}
    state["installations"] = {"alpha": _row("alpha", inside, [REQ_A])}
    save_state(state)

    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)

    # The record outside the hub is not trusted as the shared environment; the hub's own one is adopted.
    assert summary["adopted"] == ["alpha"]
    assert load_state()["plugin_envs"] == {environment_fingerprint(_manifest("alpha", [REQ_A])): {"path": str(inside)}}


def test_an_interpreter_outside_the_hub_is_not_run_by_the_test_activation_or_registration(hub, tmp_path, monkeypatch):
    from provider_hub import service
    from provider_hub.registry import register_active_provider_classes
    from provider_hub.state import active_installations
    from test_provider_hub import _manifest as plugin_manifest

    outside = _legacy_env(tmp_path / "elsewhere", "alpha", [])
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    row = {
        "provider_id": "examplehub", "name": "Example", "active_version": "1.0.0", "active_path": str(bundle),
        "python_path": str(outside / "bin" / "python"), "state": "active", "pending_restart": False,
        "manifest": plugin_manifest(dependencies={"requirements": []}), "config": {}, "last_error": None,
        "staged_version": "1.1.0", "staged_path": str(bundle), "staged_python_path": str(outside / "bin" / "python"),
    }
    state = load_state()
    state["installations"] = {"examplehub": row}
    save_state(state)

    def never(*args, **kwargs):
        raise AssertionError("an interpreter outside the hub was run")

    monkeypatch.setattr(service, "ProviderWorkerClient", never)
    monkeypatch.setattr(service, "_smoke_validate_worker", never)

    result = service.test_provider_connection("examplehub")
    assert result["ok"] is False and "hub's environments" in result["message"]
    assert register_active_provider_classes(active_installations()) == []

    row["pending_restart"] = True
    row["state"] = "staged"
    state = load_state()
    state["installations"] = {"examplehub": row}
    save_state(state)
    service.activate_staged_installations()
    assert load_state()["installations"]["examplehub"]["python_path"] == str(outside / "bin" / "python")
    assert "hub's environments" in load_state()["installations"]["examplehub"]["last_error"]


def _venv_like(env, target):
    """An environment whose interpreter is a normal venv symlink chain: python -> python3 -> the base interpreter."""
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python3").symlink_to(target)
    (env / "bin" / "python").symlink_to("python3")
    return env


def test_the_normal_venv_symlinks_of_an_interpreter_are_accepted(hub):
    import os
    import sys

    from provider_hub.venv import interpreter_inside_hub

    env = _venv_like(hub / "shared_envs" / "fp", os.path.realpath(getattr(sys, "_base_executable", sys.executable)))
    assert interpreter_inside_hub(hub, env / "bin" / "python") is True

    inside = hub / "shared_envs" / "inside"
    (inside / "bin").mkdir(parents=True)
    (inside / "bin" / "python3.14").write_text("", encoding="utf-8")
    (inside / "bin" / "python").symlink_to("python3.14")
    assert interpreter_inside_hub(hub, inside / "bin" / "python") is True


def test_an_interpreter_symlink_leading_outside_the_hub_and_the_host_python_is_refused(hub, tmp_path):
    from provider_hub.venv import interpreter_inside_hub

    outside = tmp_path / "elsewhere" / "python"
    outside.parent.mkdir()
    outside.write_text("", encoding="utf-8")
    env = hub / "shared_envs" / "fp"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").symlink_to(outside)
    assert interpreter_inside_hub(hub, env / "bin" / "python") is False

    # A chain that detours through the outside before coming back is refused too.
    detour = hub / "shared_envs" / "detour"
    (detour / "bin").mkdir(parents=True)
    hop = tmp_path / "elsewhere" / "hop"
    hop.symlink_to(detour / "bin" / "real")
    (detour / "bin" / "real").write_text("", encoding="utf-8")
    (detour / "bin" / "python").symlink_to(hop)
    assert interpreter_inside_hub(hub, detour / "bin" / "python") is False


def test_a_symlinked_bin_directory_is_refused_everywhere_an_interpreter_would_run(hub, tmp_path, no_subprocess, monkeypatch):
    from provider_hub.venv import interpreter_inside_hub

    external = _plain_env(tmp_path / "elsewhere" / "env")
    env = hub / "shared_envs" / "fp"
    env.mkdir(parents=True)
    (env / "bin").symlink_to(external / "bin", target_is_directory=True)
    (env / "pyvenv.cfg").write_text(
        f"home = /usr/bin\nversion = {sys.version_info.major}.{sys.version_info.minor}.0\n", encoding="utf-8"
    )
    assert interpreter_inside_hub(hub, env / "bin" / "python") is False

    # Boot reconcile neither adopts it nor probes it.
    _state_with(_row("alpha", env, []))
    summary = env_registry.reconcile_environments(validate=_validate, probe=_never_probe)
    assert summary["adopted"] == [] and load_state()["plugin_envs"] == {}

    # A recorded environment with such a bin is not reused: the install builds a new directory instead.
    manifest = _manifest("alpha", [REQ_A])
    state = load_state()
    state["plugin_envs"] = {environment_fingerprint(manifest): {"path": str(env)}}
    save_state(state)
    calls = []
    monkeypatch.setattr(venvmod.subprocess, "run", _fake_run_that_builds(calls))
    built = PluginEnvironment(hub).install(manifest)
    assert built != env
    assert not any(str(external) in " ".join(call) for call in calls)
    assert (external / "bin" / "python").exists()


def test_a_directory_link_inside_the_environment_cannot_carry_the_interpreter_outside(hub, tmp_path):
    from provider_hub.venv import interpreter_inside_hub

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "python").write_text("", encoding="utf-8")

    # env/bin/python -> env/linked/python, with env/linked -> the outside directory.
    one = hub / "shared_envs" / "one"
    (one / "bin").mkdir(parents=True)
    (one / "linked").symlink_to(outside, target_is_directory=True)
    (one / "bin" / "python").symlink_to(one / "linked" / "python")
    assert interpreter_inside_hub(hub, one / "bin" / "python") is False

    # Two levels down: env/a is a real directory, env/a/b is the link.
    two = hub / "shared_envs" / "two"
    (two / "bin").mkdir(parents=True)
    (two / "a").mkdir()
    (two / "a" / "b").symlink_to(outside, target_is_directory=True)
    (two / "bin" / "python").symlink_to(two / "a" / "b" / "python")
    assert interpreter_inside_hub(hub, two / "bin" / "python") is False

    # Even a directory link that stays inside the environment is refused: it can be repointed.
    inner = hub / "shared_envs" / "inner"
    (inner / "bin").mkdir(parents=True)
    (inner / "real").mkdir()
    (inner / "real" / "python").write_text("", encoding="utf-8")
    (inner / "alias").symlink_to(inner / "real", target_is_directory=True)
    (inner / "bin" / "python").symlink_to(inner / "alias" / "python")
    assert interpreter_inside_hub(hub, inner / "bin" / "python") is False

    # The plain layout is untouched.
    plain = _plain_env(hub / "shared_envs" / "plain")
    assert interpreter_inside_hub(hub, plain / "bin" / "python") is True

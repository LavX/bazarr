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

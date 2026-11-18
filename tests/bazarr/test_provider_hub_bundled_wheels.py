# coding=utf-8
"""A bundle that carries its dependency wheels installs from them, offline.

The lock stays the authority: wheels that disagree with it refuse the install. A bundle without wheels
installs from the index as before.
"""
import base64
import hashlib
import subprocess
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import provider_hub.venv as venvmod
from provider_hub.bundled_wheels import BundledWheelsError, declared_wheels, verified_wheels_dir
from provider_hub.manifest import DependencyRequirement
from provider_hub.venv import PluginEnvironment, PluginEnvironmentError, python_executable

pytestmark = pytest.mark.usefixtures("own_process_candidates")


def _make_wheel(directory: Path, name: str, version: str, requires=()) -> Path:
    """A real, tiny pure-Python wheel."""
    directory.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}"
    files = {
        f"{name}/__init__.py": f"VALUE = {name!r}\n",
        f"{dist}.dist-info/METADATA": "Metadata-Version: 2.1\nName: {}\nVersion: {}\n{}".format(
            name, version, "".join(f"Requires-Dist: {item}\n" for item in requires)),
        f"{dist}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for path, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(data)}")
    record.append(f"{dist}.dist-info/RECORD,,")
    files[f"{dist}.dist-info/RECORD"] = "\n".join(record) + "\n"
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return wheel


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path: Path, with_wheels=True):
    """A bundle tree with two locked dependencies, and the manifest describing it."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    files = {"plugin.py": _sha(root / "plugin.py")}
    requirements = []
    for name, requires in (("fixtureone", ["fixturetwo>=1"]), ("fixturetwo", [])):
        wheel = _make_wheel(root / "wheels" if with_wheels else tmp_path / "unused", name, "1.0", requires)
        requirements.append(DependencyRequirement(name=name, version="1.0", hashes=("sha256:" + _sha(wheel),)))
        if with_wheels:
            files[f"wheels/{wheel.name}"] = _sha(wheel)
    manifest = SimpleNamespace(
        provider_id="bundled", version="1.0.0", files=files, dependency_requirements=tuple(requirements))
    return root, manifest


@pytest.fixture
def hub(tmp_path, monkeypatch):
    root = tmp_path / "provider_hub"
    root.mkdir()
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(root / "state.json"))
    return root


def _deny_network(tmp_path, monkeypatch):
    """Make every pip run refuse and record a socket connection, so a reach for PyPI would show."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    attempts = tmp_path / "network-attempts"
    (blocker / "sitecustomize.py").write_text(textwrap.dedent(f"""
        import socket

        def refuse(*args, **kwargs):
            with open({str(attempts)!r}, "a") as record:
                record.write("connect\\n")
            raise OSError("network is denied")

        socket.socket.connect = refuse
        socket.socket.connect_ex = refuse
        socket.getaddrinfo = refuse
        """), encoding="utf-8")
    real_run = subprocess.run
    commands = []

    def run(cmd, **kwargs):
        commands.append(list(cmd))
        env = dict(kwargs.get("env") or {})
        env["PYTHONPATH"] = str(blocker)
        kwargs["env"] = env
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(venvmod.subprocess, "run", run)
    return commands, attempts


def test_a_bundle_with_wheels_installs_offline_from_them(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path)
    commands, attempts = _deny_network(tmp_path, monkeypatch)

    env = PluginEnvironment(hub).install(manifest, bundle_path=bundle)

    install = next(cmd for cmd in commands if cmd[1:4] == ["-m", "pip", "install"])
    assert "--no-index" in install and "--require-hashes" in install and "--only-binary" in install
    assert install[install.index("--find-links") + 1] == str(bundle.resolve() / "wheels")
    assert "--index-url" not in install and "--extra-index-url" not in install
    requirements = (env / "requirements.txt").read_text(encoding="utf-8")
    assert all(f"--hash=sha256:{_sha(wheel)}" in requirements for wheel in (bundle / "wheels").glob("*.whl"))
    # The closure is importable from the new environment, and nothing tried to connect.
    probe = subprocess.run(
        [str(python_executable(env)), "-c", "import fixtureone, fixturetwo; print(fixtureone.VALUE)"],
        capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "fixtureone"
    assert not attempts.exists()


def test_a_repair_rebuild_installs_from_the_wheels_too(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path)
    commands, attempts = _deny_network(tmp_path, monkeypatch)

    env = PluginEnvironment(hub).install(manifest, rebuild=True, bundle_path=bundle)

    assert any("--no-index" in cmd for cmd in commands)
    assert (env / venvmod.COMPLETE_MARKER).is_file()
    assert not attempts.exists()


def test_a_wheel_the_lock_does_not_list_refuses_the_install(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path)
    first = manifest.dependency_requirements[0]
    other = "sha256:" + "0" * 64
    manifest.dependency_requirements = (
        DependencyRequirement(first.name, first.version, (other,)), manifest.dependency_requirements[1])
    monkeypatch.setattr(venvmod.subprocess, "run", lambda *a, **k: pytest.fail("pip must not run"))

    with pytest.raises(PluginEnvironmentError) as refused:
        PluginEnvironment(hub).install(manifest, bundle_path=bundle)

    assert "do not match its lock" in str(refused.value)
    assert "digest the lock does not list for fixtureone 1.0" in str(refused.value)
    assert not (hub / "shared_envs").exists()


def test_a_wheel_changed_after_staging_refuses_the_install(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path)
    next(iter((bundle / "wheels").glob("fixtureone*"))).write_bytes(b"not the wheel")
    monkeypatch.setattr(venvmod.subprocess, "run", lambda *a, **k: pytest.fail("pip must not run"))

    with pytest.raises(PluginEnvironmentError, match="does not match its declared digest"):
        PluginEnvironment(hub).install(manifest, bundle_path=bundle)


def test_an_environment_that_could_be_reused_is_still_refused_on_a_mismatch(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path)
    _deny_network(tmp_path, monkeypatch)
    PluginEnvironment(hub).install(manifest, bundle_path=bundle)
    next(iter((bundle / "wheels").glob("fixturetwo*"))).write_bytes(b"swapped")

    with pytest.raises(PluginEnvironmentError, match="do not match its lock"):
        PluginEnvironment(hub).install(manifest, bundle_path=bundle)


@pytest.mark.parametrize("damage, reason", [
    ("drop_a_wheel", "carries no wheel for it"),
    ("unlock_a_wheel", "is not a locked dependency"),
    ("wrong_version", "but the lock pins"),
    ("undeclared_wheel", "the manifest does not declare it"),
    ("not_a_wheel", "is not a wheel file placed directly in wheels/"),
    ("no_lock", "its lock names no dependency"),
])
def test_wheels_that_disagree_with_the_lock_name_the_reason(tmp_path, damage, reason):
    bundle, manifest = _bundle(tmp_path)
    first, second = manifest.dependency_requirements
    if damage == "drop_a_wheel":
        path = next(path for path in manifest.files if "fixturetwo" in path)
        del manifest.files[path]
    elif damage == "unlock_a_wheel":
        manifest.dependency_requirements = (first,)
    elif damage == "wrong_version":
        manifest.dependency_requirements = (DependencyRequirement(first.name, "2.0", first.hashes), second)
    elif damage == "undeclared_wheel":
        _make_wheel(bundle / "wheels", "stray", "1.0")
    elif damage == "not_a_wheel":
        (bundle / "wheels" / "notes.txt").write_text("x", encoding="utf-8")
        manifest.files["wheels/notes.txt"] = _sha(bundle / "wheels" / "notes.txt")
    elif damage == "no_lock":
        manifest.dependency_requirements = ()

    with pytest.raises(BundledWheelsError, match=reason):
        verified_wheels_dir(manifest, bundle)


def test_a_bundle_without_wheels_installs_from_the_index_as_before(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path, with_wheels=False)
    assert declared_wheels(manifest) == {}
    assert verified_wheels_dir(manifest, bundle) is None
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append((list(cmd), kwargs))
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)

    PluginEnvironment(hub).install(manifest, bundle_path=bundle)

    install, kwargs = next((cmd, kw) for cmd, kw in commands if cmd[1:4] == ["-m", "pip", "install"])
    assert install[4:] == [
        "--disable-pip-version-check", "--no-warn-script-location", "--require-hashes", "--prefer-binary",
        "-r", install[-1]]
    assert "PIP_CONFIG_FILE" not in kwargs["env"]
    assert "capture_output" not in kwargs


def test_a_pip_failure_on_the_bundled_wheels_says_so_and_never_falls_back_to_the_index(hub, tmp_path, monkeypatch):
    bundle, manifest = _bundle(tmp_path)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            target = Path(cmd[3])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("", encoding="utf-8")
        if cmd[1:4] == ["-m", "pip", "install"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: No matching distribution found for fixtureone\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(venvmod.subprocess, "run", fake_run)

    with pytest.raises(PluginEnvironmentError) as failed:
        PluginEnvironment(hub).install(manifest, bundle_path=bundle)

    assert "from the wheels in its bundle" in str(failed.value)
    assert "No matching distribution found for fixtureone" in str(failed.value)
    assert sum(1 for cmd in commands if cmd[1:4] == ["-m", "pip", "install"]) == 1

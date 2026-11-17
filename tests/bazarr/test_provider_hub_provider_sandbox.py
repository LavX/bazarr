# coding=utf-8
"""The sandbox profile of subtitle provider workers.

A provider worker cannot read the configuration or the database, sees the media library read-only,
and keeps its ``ffmpeg`` plugins working (large temporary files, no metadata filter). Where the
ruleset cannot be applied the worker still starts, and a notice says so. The user can switch the
sandbox off.

Most tests start the real worker runner with a fixture bundle through a real ``ProviderWorkerClient``;
the ones that need the kernel's Landlock skip on a host that has none.
"""
import dataclasses
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from provider_hub import worker as worker_module
from provider_hub.sandbox import (
    SandboxProbe,
    probe_sandbox,
    provider_profile_status,
    sandbox_status,
)
from provider_hub.sandbox import policy as policy_module
from provider_hub.sandbox import spawn as spawn_module
from provider_hub.worker import ProviderLayout, ProviderWorkerClient, worker_command

RUNNER = Path(worker_module._RUNNER_PATH)

PROVIDER_SOURCE = '''
import os
import subprocess


def _try(action):
    try:
        return action()
    except PermissionError:
        return "denied"
    except OSError as error:
        return "error:%s" % error.__class__.__name__


class Fixture:
    def search(self, video, languages, config):
        op = config["op"]
        if op == "read":
            return [{"result": _try(lambda: open(config["path"], encoding="utf-8").read())}]
        if op == "write":
            def write():
                with open(config["path"], "w", encoding="utf-8") as handle:
                    handle.write("x")
                return "written"
            return [{"result": _try(write)}]
        if op == "list":
            return [{"result": _try(lambda: sorted(os.listdir(config["path"])))}]
        if op == "stub":
            completed = subprocess.run([config["stub"], config["size"]], capture_output=True, text=True)
            return [{"result": completed.stdout.strip() or completed.stderr.strip(), "code": completed.returncode}]
        if op == "env":
            return [{"result": {key: os.environ.get(key) for key in config["keys"]}}]
        raise ValueError(op)
'''

STUB = '''#!{python}
"""Stands in for ffmpeg: writes a large temporary file, then changes its mode."""
import os
import sys
import tempfile

size = int(sys.argv[1])
handle = tempfile.NamedTemporaryFile(delete=False)
try:
    handle.truncate(size)
    handle.seek(size)
    handle.write(b"x")
    handle.close()
    os.chmod(handle.name, 0o600)
    print("wrote %d" % os.stat(handle.name).st_size)
finally:
    os.unlink(handle.name)
'''


@pytest.fixture(autouse=True)
def clean_notices():
    spawn_module.reset_provider_notices()
    yield
    spawn_module.reset_provider_notices()


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A configuration folder with a config file and a database, a media folder beside it, a bundle and a stub."""
    config = tmp_path / "config"
    (config / "config").mkdir(parents=True)
    (config / "db").mkdir()
    (config / "config" / "config.yaml").write_text("general:\n  secret: hunter2\n", encoding="utf-8")
    (config / "db" / "bazarr.db").write_text("SQLite format 3", encoding="utf-8")
    (config / "provider_hub").mkdir()
    media = tmp_path / "media"
    media.mkdir()
    (media / "film.mkv").write_text("film", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "fixture_provider.py").write_text(PROVIDER_SOURCE, encoding="utf-8")
    stub = tmp_path / "bin" / "ffmpeg"
    stub.parent.mkdir()
    stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    layout = ProviderLayout(
        key="fixture",
        interpreter=Path(sys.executable),
        runner=str(RUNNER),
        bundle=str(bundle),
        hub_dir=config / "provider_hub",
        config_dir=config,
    )
    monkeypatch.setattr(worker_module, "provider_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr("provider_hub.sandbox.provider_profile_enabled", lambda: True)

    # The walk lists the entries beside every ancestor of the configuration folder. A shared
    # temp directory holds thousands of unrelated ones, which would only bloat the launch spec.
    real_outside = policy_module.provider_outside_paths

    def bounded(shield, **kwargs):
        return tuple(path for path in real_outside(shield, **kwargs) if os.path.dirname(path) not in ("/tmp", "/var/tmp"))

    monkeypatch.setattr(policy_module, "provider_outside_paths", bounded)
    return {
        "config": config,
        "media": media,
        "bundle": bundle,
        "stub": stub,
        "layout": layout,
        "root": tmp_path,
    }


def _client(host):
    manifest = json.dumps({"entry_module": "fixture_provider", "entry_class": "Fixture"})
    return ProviderWorkerClient(
        worker_command(sys.executable, RUNNER),
        cwd=host["bundle"],
        env={"BAZARR_PROVIDER_HUB_BUNDLE": str(host["bundle"]), "BAZARR_PROVIDER_HUB_MANIFEST": manifest},
        provider_id="fixture",
    )


def _ask(client, **config):
    result = client.request("search", {"config": config}, timeout=60)
    return result.payload["candidates"][0]


@pytest.fixture
def client(host):
    made = []

    def make():
        worker = _client(host)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.stop()


def _enforcing_kernel():
    probe = probe_sandbox()
    if not probe.enforced:
        pytest.skip("this host cannot enforce the worker sandbox: %s" % probe.reason)
    return probe


# ------------------------------------------------------------------------------ the enforced profile
def test_a_provider_worker_cannot_read_the_configuration_or_the_database(host, client):
    _enforcing_kernel()
    worker = client()

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert _ask(worker, op="read", path=str(host["config"] / "db" / "bazarr.db"))["result"] == "denied"
    assert _ask(worker, op="list", path=str(host["config"] / "config"))["result"] == "denied"
    assert getattr(worker.process, "enforcement", None) == "enforced"
    # Nothing was recorded against it: the ruleset is in force.
    assert provider_profile_status(SandboxProbe(abi=10, verified=True))["state"] == "enforced"


def test_a_provider_worker_reads_media_and_cannot_change_it(host, client):
    _enforcing_kernel()
    worker = client()

    assert _ask(worker, op="read", path=str(host["media"] / "film.mkv"))["result"] == "film"
    assert _ask(worker, op="write", path=str(host["media"] / "film.mkv"))["result"] == "denied"
    assert _ask(worker, op="write", path=str(host["media"] / "new.srt"))["result"] == "denied"
    assert (host["media"] / "film.mkv").read_text(encoding="utf-8") == "film"


def test_a_provider_worker_keeps_its_bundle_readable_and_its_own_folders_writable(host, client):
    _enforcing_kernel()
    worker = client()
    environment = _ask(worker, op="env", keys=["HOME", "TMPDIR", "PATH"])["result"]
    home, temp = Path(environment["HOME"]), Path(environment["TMPDIR"])

    assert home == host["config"] / "provider_hub" / "run" / "fixture" / "data"
    assert temp == host["config"] / "provider_hub" / "run" / "fixture" / "tmp"
    assert _ask(worker, op="write", path=str(home / "state.json"))["result"] == "written"
    assert _ask(worker, op="write", path=str(temp / "scratch"))["result"] == "written"
    assert _ask(worker, op="read", path=str(host["bundle"] / "fixture_provider.py"))["result"].startswith("\nimport os")
    # The folders beside its own stay shut.
    assert _ask(worker, op="write", path=str(host["config"] / "provider_hub" / "state.json"))["result"] == "denied"
    assert environment["PATH"] == os.environ["PATH"]


def test_a_stub_ffmpeg_can_write_a_large_temporary_file_and_change_its_mode(host, client):
    _enforcing_kernel()
    worker = client()
    size = 700 * 1024 * 1024  # past the 512 MiB file-size limit every other worker has

    answer = _ask(worker, op="stub", stub=str(host["stub"]), size=str(size))

    assert answer["code"] == 0, answer
    assert answer["result"] == "wrote %d" % (size + 1)


# ------------------------------------------------------------------------------------- failing open
def test_without_landlock_the_provider_still_starts_and_the_status_carries_a_notice(host, client, monkeypatch):
    probe = SandboxProbe(abi=0, verified=False, reason="Landlock is not available in this kernel")
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)

    def refuse(*args, **kwargs):
        raise AssertionError("a host that cannot sandbox must not go through the launcher")

    monkeypatch.setattr("provider_hub.sandbox.spawn_worker", refuse)
    worker = client()

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"].startswith("general:")
    status = sandbox_status(probe)["providers"]
    assert status["state"] == "unsandboxed"
    assert "Landlock is not available in this kernel" in status["notice"]
    assert status["unsandboxed"] == ["fixture"]


def test_when_the_ruleset_cannot_be_applied_the_provider_starts_anyway_and_the_notice_names_the_cause(
    host, client, monkeypatch
):
    # The probe says the host enforces, but the launcher cannot apply this worker's ruleset: a grant
    # is not the object the policy recorded. The worker never ran, so the request is repeated without it.
    probe = SandboxProbe(abi=10, verified=True, reason="kernel reports ABI 10, canary confined")
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)
    real_spec = worker_module.provider_spec

    def broken(layout, command, cwd, env):
        spec = real_spec(layout, command, cwd, env)
        bundle = os.path.realpath(layout.bundle)
        assert bundle in spec.policy.identities
        stale = dict(spec.policy.identities)
        stale[bundle] = (1, 1)
        return dataclasses.replace(spec, policy=dataclasses.replace(spec.policy, identities=stale))

    monkeypatch.setattr(worker_module, "provider_spec", broken)
    worker = client()

    answer = _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))
    assert answer["result"].startswith("general:")
    assert worker._sandbox_failed_open is True
    assert getattr(worker.process, "enforcement", "declared") == "declared"
    status = provider_profile_status(probe)
    assert status["state"] == "unsandboxed"
    assert status["unsandboxed"] == ["fixture"]
    assert "could not be applied" in status["notice"]
    # The client does not try the sandbox again for its next worker.
    assert _ask(worker, op="read", path=str(host["media"] / "film.mkv"))["result"] == "film"


def test_a_failed_preparation_also_fails_open(host, client, monkeypatch):
    probe = SandboxProbe(abi=10, verified=True)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)

    def refuse(*args, **kwargs):
        raise policy_module.PolicyError("a grant lies inside the shielded folder")

    monkeypatch.setattr(worker_module, "provider_spec", refuse)
    worker = client()

    assert _ask(worker, op="read", path=str(host["media"] / "film.mkv"))["result"] == "film"
    assert "shielded folder" in provider_profile_status(probe)["notice"]


# ------------------------------------------------------------------------------------- the switch
def test_with_the_toggle_off_the_worker_starts_without_the_ruleset(host, client, monkeypatch):
    monkeypatch.setattr("provider_hub.sandbox.provider_profile_enabled", lambda: False)

    def refuse(*args, **kwargs):
        raise AssertionError("the sandbox is switched off")

    monkeypatch.setattr("provider_hub.sandbox.spawn_worker", refuse)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", refuse)
    worker = client()

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"].startswith("general:")
    status = provider_profile_status(SandboxProbe(abi=10, verified=True), enabled=False)
    assert status == {"enabled": False, "state": "off", "notice": None, "unsandboxed": []}


def test_the_toggle_is_a_setting_that_is_on_by_default():
    from app.config import settings, validators

    names = [name for validator in validators for name in validator.names]
    assert "general.provider_hub_sandbox" in names
    assert settings.general.provider_hub_sandbox is True
    assert spawn_module.provider_profile_enabled() is True


# ------------------------------------------------------------------------------------ the status
def test_the_status_says_pending_until_the_host_check_has_answered():
    assert provider_profile_status(None, enabled=True)["state"] == "pending"
    assert provider_profile_status(None, enabled=False)["state"] == "off"


def test_a_provider_that_started_without_the_sandbox_is_named_while_the_host_enforces():
    spawn_module.note_provider_unsandboxed("embeddedsubtitles", "the sandbox could not be applied: grant changed")
    status = provider_profile_status(SandboxProbe(abi=10, verified=True))

    assert status["state"] == "unsandboxed"
    assert "embeddedsubtitles" in status["notice"]
    spawn_module.clear_provider_notice("embeddedsubtitles")
    assert provider_profile_status(SandboxProbe(abi=10, verified=True))["state"] == "enforced"


# ------------------------------------------------------------------------------ the profile itself
def test_the_profile_shields_the_configuration_folder_and_grants_the_rest_read_only(host):
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})
    config = str(host["config"])
    granted = [*spec.policy.read_only, *spec.policy.read_write]

    assert not any(path == config or path.startswith(config + "/") and "/run/" not in path for path in granted)
    assert str(host["media"]) in spec.policy.read_only
    assert str(host["bundle"]) in spec.policy.read_only
    assert sorted(spec.policy.read_write) == [
        str(host["config"] / "provider_hub" / "run" / "fixture" / "data"),
        str(host["config"] / "provider_hub" / "run" / "fixture" / "tmp"),
    ]
    assert spec.policy.connect_ports is None


def test_the_profile_leaves_out_the_file_size_and_process_limits_and_the_metadata_filter(host):
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})
    launch = json.loads(spec.to_launch_json(enforce=True, min_abi=3))

    assert launch["limits"]["fsize"] is None
    assert launch["limits"]["nproc"] is None
    assert launch["limits"]["core"] == 0
    assert launch["limits"]["nofile"] == 1024
    assert launch["seccomp"] is False
    # Every other kind keeps the filter and the file-size limit.
    other = json.loads(dataclasses.replace(spec, limits=policy_module.Limits(), seccomp=True).to_launch_json(enforce=True, min_abi=3))
    assert other["seccomp"] is True
    assert other["limits"]["fsize"] == 512 * 1024 * 1024


def test_the_profile_environment_has_the_plugins_home_and_temp_folders_and_no_proxy(host, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@proxy.invalid:3128")
    monkeypatch.setenv("TZ", "Europe/Budapest")
    spec = worker_module.provider_spec(
        host["layout"],
        worker_command(sys.executable, RUNNER),
        host["bundle"],
        {"BAZARR_PROVIDER_HUB_BUNDLE": str(host["bundle"]), "BAZARR_PROVIDER_HUB_MANIFEST": "{}", "PATH": "/ignored"},
    )

    assert spec.env["HOME"].endswith("/run/fixture/data")
    assert spec.env["TMPDIR"].endswith("/run/fixture/tmp")
    assert spec.env["BAZARR_PROVIDER_HUB_BUNDLE"] == str(host["bundle"])
    assert spec.env["PATH"] == os.environ["PATH"]
    assert spec.env["TZ"] == "Europe/Budapest"
    assert not any("proxy" in key.lower() for key in spec.env)
    assert "BAZARR_PROVIDER_HUB_MANIFEST" in spec.env


def test_the_outside_paths_hold_everything_beside_the_configuration_folder_and_not_the_folder(tmp_path):
    shield = tmp_path / "data" / "config"
    shield.mkdir(parents=True)
    (tmp_path / "data" / "cache").mkdir()
    (tmp_path / "library").mkdir()
    (tmp_path / "link").symlink_to(shield)

    found = policy_module.provider_outside_paths(shield)

    assert str(tmp_path / "library") in found
    assert str(tmp_path / "data" / "cache") in found
    assert str(shield) not in found
    # An ancestor would cover the shielded folder, and a link into it would too.
    assert str(tmp_path) not in found and str(tmp_path / "data") not in found
    assert str(tmp_path / "link") not in found
    for name in ("proc", "dev", "etc", "run"):
        assert "/" + name not in found
    with pytest.raises(policy_module.PolicyError):
        policy_module.provider_outside_paths("/")


def test_only_the_real_runner_started_from_a_hub_interpreter_is_a_provider_worker(tmp_path, monkeypatch):
    # A fixture started from the test interpreter, or any other script, runs as it always did.
    env = {"BAZARR_PROVIDER_HUB_BUNDLE": str(tmp_path)}
    assert worker_module.provider_layout([sys.executable, "-I", "-B", str(RUNNER)], tmp_path, env, "x") is None
    assert worker_module.provider_layout([sys.executable, "script.py"], tmp_path, env, "x") is None
    assert worker_module.provider_layout([], tmp_path, env, "x") is None
    assert worker_module.provider_layout(worker_command(sys.executable, RUNNER), None, {}, "x") is None


def test_a_client_that_is_not_a_provider_worker_never_asks_the_sandbox(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("not a provider worker")

    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", refuse)
    monkeypatch.setattr("provider_hub.sandbox.spawn_worker", refuse)
    script = tmp_path / "echo.py"
    script.write_text(
        "import json, sys\nfor line in sys.stdin:\n"
        "    r = json.loads(line)\n"
        "    print(json.dumps({'abi': r['abi'], 'id': r['id'], 'ok': True, 'payload': {}}), flush=True)\n",
        encoding="utf-8",
    )
    worker = ProviderWorkerClient([sys.executable, str(script)])
    try:
        assert worker.request("health", {}, timeout=10).ok
        assert isinstance(worker.process, subprocess.Popen)
        assert getattr(worker.process, "enforcement", None) is None
    finally:
        worker.stop()

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
    PROVIDER_MIN_ABI,
    SandboxProbe,
    landlock,
    probe_sandbox,
    provider_profile_gaps,
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
        if op == "chmod":
            return [{"result": _try(lambda: (os.chmod(config["path"], 0), "changed")[1])}]
        if op == "exit":
            os._exit(3)
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

    # What the profile may read besides the system, the interpreter and the bundle: the programs on
    # PATH and the media roots. A folder beside the others that is neither holds a secret.
    secret = tmp_path / "home" / "me"
    secret.mkdir(parents=True)
    (secret / "id_rsa").write_text("private key", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "backup.txt").write_text("copy of config.yaml and the database", encoding="utf-8")
    monkeypatch.setenv("PATH", str(stub.parent) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr("provider_hub.provider_roots.media_root_folders", lambda: [str(media)])
    monkeypatch.setattr("provider_hub.provider_roots.backup_folders", lambda: [backups])
    return {
        "config": config,
        "media": media,
        "bundle": bundle,
        "stub": stub,
        "layout": layout,
        "root": tmp_path,
        "secret": secret,
        "backups": backups,
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
    """The provider profile needs Landlock from ABI 1, not the full sandbox, so this runs on older kernels too."""
    probe = probe_sandbox()
    if not probe.enforces(PROVIDER_MIN_ABI):
        reason = "this host cannot enforce the provider sandbox: %s" % probe.reason
        if os.environ.get("BAZARR_REQUIRE_SANDBOX_TESTS") == "1":
            pytest.fail("BAZARR_REQUIRE_SANDBOX_TESTS=1 but %s" % reason, pytrace=False)
        pytest.skip(reason)
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


def test_when_the_kernel_cannot_apply_a_ruleset_the_provider_starts_anyway_and_the_notice_names_the_cause(
    host, client, monkeypatch
):
    # The probe says the host enforces, but the kernel the worker lands on has a lower ABI than the
    # probe saw: no ruleset can be applied at all. The worker never ran, so the request is repeated without it.
    probe = SandboxProbe(abi=99, verified=True, reads_verified=True, reason="forced for the test")
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)
    worker = client()

    answer = _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))
    assert answer["result"].startswith("general:")
    assert worker._sandbox_failed_open is True
    assert getattr(worker.process, "enforcement", "declared") == "declared"
    status = provider_profile_status(probe)
    assert status["state"] == "unsandboxed"
    assert status["unsandboxed"] == ["fixture"]
    assert "could not apply the ruleset" in status["notice"]
    # The client does not try the sandbox again for its next worker.
    assert _ask(worker, op="read", path=str(host["media"] / "film.mkv"))["result"] == "film"


def test_a_ruleset_that_fails_to_apply_for_any_other_reason_refuses_the_start_instead_of_opening(host, client, monkeypatch):
    # A grant that is not the object the policy recorded. A worker that damaged its own tree could cause
    # this, so the sandbox must not be dropped for it.
    probe = SandboxProbe(abi=10, verified=True, reads_verified=True)
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

    with pytest.raises(worker_module.WorkerError, match="was not started"):
        _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))
    assert worker._sandbox_failed_open is False
    status = provider_profile_status(probe)
    assert status["state"] == "blocked"
    assert status["blocked"] == ["fixture"] and status["unsandboxed"] == []
    assert "could not be applied" in status["notice"] and "was not started" in status["notice"]
    # Again, once the grant is right: nothing was latched open.
    monkeypatch.setattr(worker_module, "provider_spec", real_spec)
    _enforcing_kernel()
    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert provider_profile_status(probe)["state"] == "enforced"


def test_a_failed_preparation_refuses_the_start_where_the_host_can_enforce_and_never_runs_unsandboxed(host, client, monkeypatch):
    probe = SandboxProbe(abi=10, verified=True, reads_verified=True)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)

    def refuse(*args, **kwargs):
        raise policy_module.PolicyError("a grant lies inside the shielded folder")

    monkeypatch.setattr(worker_module, "provider_spec", refuse)
    worker = client()

    with pytest.raises(worker_module.WorkerError, match="shielded folder"):
        _ask(worker, op="read", path=str(host["media"] / "film.mkv"))
    assert worker.process is None
    assert "shielded folder" in provider_profile_status(probe)["notice"]


def test_a_worker_that_damages_its_own_folder_cannot_switch_the_sandbox_off_for_its_next_start(host, client):
    _enforcing_kernel()
    worker = client()
    run = host["config"] / "provider_hub" / "run" / "fixture"

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert _ask(worker, op="chmod", path=str(run / "data"))["result"] == "changed"
    assert _ask(worker, op="chmod", path=str(run))["result"] == "changed"
    with pytest.raises(worker_module.WorkerError):
        _ask(worker, op="exit")

    # The next start puts the folders right and is sandboxed again.
    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert worker._sandbox_failed_open is False
    assert (run.stat().st_mode & 0o777) == 0o700


def _launcher_failure_client(kind, message):
    worker = ProviderWorkerClient.__new__(ProviderWorkerClient)

    class Process:
        returncode = 78

        @staticmethod
        def launcher_failure():
            return spawn_module.LauncherFailure(kind, message)

        @staticmethod
        def poll():
            return 78

    worker.process = Process()
    worker._sandbox_layout = object()
    worker.command, worker.cwd, worker.env, worker.provider_id = ["x"], None, {}, "fixture"
    worker._sandbox_failed_open = False
    worker._killed = []
    worker._kill_tree = lambda process, grace: worker._killed.append(process) or True
    worker._has_exited = lambda process: True
    return worker


@pytest.mark.parametrize(
    "message",
    [
        "a descendant of the worker survived the launcher's cleanup",
        "a descendant of the worker may have survived the launcher's cleanup",
        "launcher could not fork the worker: [Errno 11] Resource temporarily unavailable",
    ],
)
def test_a_launcher_report_about_the_cleanup_or_the_fork_is_never_an_apply_failure(message):
    worker = _launcher_failure_client("sandbox", message)

    assert worker._sandbox_apply_failure() is None
    assert worker._opened_after_apply_failure() is False
    assert worker._sandbox_failed_open is False
    assert worker._killed == []


def test_only_a_report_that_the_ruleset_was_not_applied_is_one():
    ours = "could not apply sandbox: [Errno 13] Permission denied"
    worker = _launcher_failure_client("sandbox", ours)
    assert worker._sandbox_apply_failure() == ours
    # A report of another kind (a bad spec, a failed exec) is not one either.
    assert _launcher_failure_client("exec", "exec failed: x")._sandbox_apply_failure() is None
    assert _launcher_failure_client("spec", "bad working directory: x")._sandbox_apply_failure() is None
    with pytest.raises(worker_module.WorkerError, match="was not started"):
        worker._opened_after_apply_failure()
    assert worker._sandbox_failed_open is False


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
    assert (status["enabled"], status["state"], status["notice"]) == (False, "off", None)
    assert status["unsandboxed"] == [] and status["blocked"] == []


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
def test_the_profile_is_an_allowlist_of_the_system_the_plugin_the_programs_and_the_media(host):
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})
    config = str(host["config"])
    granted = [*spec.policy.read_only, *spec.policy.read_write]

    def covered(path):
        return any(path == grant or path.startswith(grant.rstrip("/") + "/") for grant in granted)

    run = config + "/provider_hub/run/fixture"
    assert not any(path == config or (path.startswith(config + "/") and not path.startswith(run + "/")) for path in granted)
    assert str(host["media"]) in spec.policy.read_only
    assert str(host["bundle"]) in spec.policy.read_only
    assert str(host["stub"].parent) in spec.policy.read_only
    # Beside them, nothing: a home folder, a backup folder, the folder that holds the test's own files.
    assert not covered(str(host["secret"] / "id_rsa"))
    assert not covered(str(host["backups"] / "backup.txt"))
    assert not covered(str(host["root"]))
    for top in ("/root", "/var", "/srv", "/opt", "/mnt", "/home", "/tmp", "/boot", "/run", "/etc/shadow"):
        assert top not in granted
    assert sorted(spec.policy.read_write) == [run + "/data", run + "/tmp"]
    assert spec.policy.connect_ports is None


def test_a_provider_worker_cannot_read_a_home_folder_or_the_backups(host, client):
    _enforcing_kernel()
    worker = client()

    assert _ask(worker, op="read", path=str(host["secret"] / "id_rsa"))["result"] == "denied"
    assert _ask(worker, op="read", path=str(host["backups"] / "backup.txt"))["result"] == "denied"
    assert _ask(worker, op="read", path=str(host["media"] / "film.mkv"))["result"] == "film"


def test_the_backup_folder_is_a_protected_path_of_the_policy(host, monkeypatch):
    # A backup folder inside a granted tree would be refused outright: no start is ever built with it readable.
    monkeypatch.setattr("provider_hub.provider_roots.backup_folders", lambda: [host["media"] / "backups"])
    (host["media"] / "backups").mkdir()
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})

    assert str(host["media"]) not in spec.policy.read_only
    assert str(host["media"] / "film.mkv") in spec.policy.read_only
    assert str(host["media"] / "backups") not in spec.policy.read_only


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


def test_tree_grants_leave_out_a_shielded_folder_inside_a_root(tmp_path):
    root = tmp_path / "data"
    (root / "config" / "db").mkdir(parents=True)
    (root / "media" / "films").mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "top.mkv").write_text("x", encoding="utf-8")
    elsewhere = tmp_path / "library"
    elsewhere.mkdir()
    (tmp_path / "link").symlink_to(root / "config")

    found = policy_module.provider_tree_grants([root, elsewhere, tmp_path / "link", tmp_path / "missing"], [root / "config"])

    assert str(elsewhere) in found
    assert str(root / "media") in found and str(root / "cache") in found and str(root / "top.mkv") in found
    # The root that held the shield is replaced by what lies beside it; the shield, a link into it and a
    # folder that does not exist are not granted.
    assert str(root) not in found and str(root / "config") not in found
    assert str(tmp_path / "link") not in found and str(tmp_path / "missing") not in found
    # A root that is itself inside a shield is dropped.
    assert policy_module.provider_tree_grants([root / "config" / "db"], [root / "config"]) == ()
    with pytest.raises(policy_module.PolicyError):
        policy_module.provider_tree_grants(["/"], [root])


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


# --------------------------------------------------------------------- the rules each ABI supports
def test_the_provider_profile_accepts_landlock_from_abi_1_and_every_other_kind_still_needs_abi_3():
    assert PROVIDER_MIN_ABI == 1
    assert spawn_module.MIN_ENFORCED_ABI == 3
    for abi in (1, 2):
        probe = SandboxProbe(abi=abi, verified=False, reads_verified=True)
        assert probe.enforces(PROVIDER_MIN_ABI) is True
        assert probe.enforced is False
        assert probe.enforces() is False
        assert probe.enforces(spawn_module.MIN_ENFORCED_ABI) is False
    full = SandboxProbe(abi=3, verified=True, reads_verified=True)
    assert full.enforced and full.enforces() and full.enforces(PROVIDER_MIN_ABI)
    # No Landlock at all, or a canary that escaped, is not enough for either.
    assert not SandboxProbe(abi=0, verified=False).enforces(PROVIDER_MIN_ABI)
    assert not SandboxProbe(abi=2, verified=False, reads_verified=False).enforces(PROVIDER_MIN_ABI)


@pytest.mark.parametrize("abi", [1, 2, 3, 6])
def test_spawn_asks_for_a_mandatory_ruleset_per_abi_only_for_the_kinds_that_accept_it(abi, monkeypatch):
    seen = []

    def fake_start(spec, **kwargs):
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(spawn_module, "_start", fake_start)
    probe = SandboxProbe(abi=abi, verified=abi >= 3, reads_verified=True)
    spec = object.__new__(policy_module.RuntimeSpec)
    object.__setattr__(spec, "policy", policy_module.SandboxPolicy(read_only=(), read_write=()))
    spawn_module.spawn_worker(spec, probe=probe, minimum_abi=PROVIDER_MIN_ABI)
    spawn_module.spawn_worker(spec, probe=probe)

    provider, other = seen
    assert provider["enforce"] is True and provider["enforcement"] == "enforced"
    assert other["enforce"] is (abi >= 3)
    assert other["enforcement"] == ("enforced" if abi >= 3 else "declared")
    assert provider["min_abi"] == abi


def test_each_abi_handles_only_the_rights_it_knows_so_the_profile_grants_what_the_kernel_supports():
    read_write = landlock.RIGHTS_READ_WRITE
    device = landlock.RIGHTS_DEVICE
    by_abi = {abi: landlock.handled_fs_for_abi(abi) for abi in (1, 2, 3, 4, 5)}

    # ABI 1 has neither file moves between folders nor truncation; ABI 2 adds the first; ABI 3 the second.
    assert not by_abi[1] & landlock.FS_REFER and not by_abi[1] & landlock.FS_TRUNCATE
    assert by_abi[2] & landlock.FS_REFER and not by_abi[2] & landlock.FS_TRUNCATE
    assert by_abi[3] & landlock.FS_REFER and by_abi[3] & landlock.FS_TRUNCATE
    # Reading, writing, creating and removing files are handled from ABI 1, so the shield and the
    # read-only media rules exist everywhere the profile runs.
    for right in (landlock.FS_READ_FILE, landlock.FS_READ_DIR, landlock.FS_WRITE_FILE, landlock.FS_MAKE_REG, landlock.FS_REMOVE_FILE):
        assert all(by_abi[abi] & right for abi in by_abi)
    # What a read-write grant of the plugin's own folders carries is the part of it the ABI handles.
    assert read_write & by_abi[1] & landlock.FS_WRITE_FILE
    assert not read_write & by_abi[1] & landlock.FS_TRUNCATE
    assert device & by_abi[1] & landlock.FS_WRITE_FILE
    # ABI 4 and 5 add nothing to the filesystem the profile uses beyond ioctl on devices (ABI 5).
    assert by_abi[4] == by_abi[3]
    assert by_abi[5] & landlock.FS_IOCTL_DEV and not by_abi[4] & landlock.FS_IOCTL_DEV


def test_the_gaps_below_abi_3_are_named_and_a_full_abi_names_none():
    assert provider_profile_gaps(3) == [] == provider_profile_gaps(10)
    abi2 = " ".join(provider_profile_gaps(2))
    abi1 = " ".join(provider_profile_gaps(1))
    assert "truncating" in abi2 and "moving or linking" not in abi2
    assert "truncating" in abi1 and "moving or linking" in abi1


@pytest.mark.parametrize("abi,gap,state", [(1, "moving or linking", "partial"), (2, "truncating", "partial"), (3, None, "enforced")])
def test_the_notice_names_what_is_not_covered_on_each_abi(abi, gap, state):
    probe = SandboxProbe(abi=abi, verified=abi >= 3, reads_verified=True, reason="kernel reports ABI %d" % abi)
    status = provider_profile_status(probe)

    assert status["state"] == state
    if gap is None:
        assert status["notice"] is None
        return
    assert "ABI %d" % abi in status["notice"]
    assert "cannot read the configuration or the database" in status["notice"]
    assert gap in status["notice"]
    # It is still sandboxed, so no provider is listed as running without it.
    assert status["unsandboxed"] == []


def test_a_kernel_without_landlock_or_a_canary_that_escaped_still_fails_open_with_a_notice():
    for probe in (
        SandboxProbe(abi=0, verified=False, reason="Landlock is not available"),
        SandboxProbe(abi=2, verified=False, reads_verified=False, reason="canary escaped the sandbox: {}"),
    ):
        status = provider_profile_status(probe)
        assert status["state"] == "unsandboxed"
        assert probe.reason.split(":")[0] in status["notice"]


@pytest.mark.parametrize("abi", [1, 2])
def test_the_probe_on_an_older_abi_verifies_reads_and_writes_but_not_the_full_sandbox(abi, monkeypatch):
    # The canary runs on this kernel's real Landlock; only the ABI the probe reports is lowered.
    _enforcing_kernel()
    monkeypatch.setattr(landlock, "probe_abi", lambda: (abi, "kernel reports ABI %d" % abi))

    probe = spawn_module._run_probe(sys.executable)

    assert probe.abi == abi
    assert probe.reads_verified is True
    assert probe.verified is False and probe.enforced is False
    assert probe.enforces(PROVIDER_MIN_ABI) is True
    assert "truncate" in probe.reason


@pytest.mark.parametrize("abi", [1, 2])
def test_on_an_older_abi_a_provider_cannot_read_the_configuration_and_ffmpeg_still_works(abi, host, client, monkeypatch):
    # A real ruleset of the older ABI on this kernel: the policy is capped, the probe reports that ABI.
    _enforcing_kernel()
    probe = SandboxProbe(abi=abi, verified=False, reads_verified=True, reason="Landlock ABI %d" % abi)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)
    real_spec = worker_module.provider_spec

    def capped(layout, command, cwd, env):
        spec = real_spec(layout, command, cwd, env)
        return dataclasses.replace(spec, policy=dataclasses.replace(spec.policy, abi_cap=abi))

    monkeypatch.setattr(worker_module, "provider_spec", capped)
    worker = client()
    size = 700 * 1024 * 1024

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert _ask(worker, op="read", path=str(host["config"] / "db" / "bazarr.db"))["result"] == "denied"
    assert _ask(worker, op="read", path=str(host["media"] / "film.mkv"))["result"] == "film"
    assert _ask(worker, op="write", path=str(host["media"] / "film.mkv"))["result"] == "denied"
    assert _ask(worker, op="write", path=str(host["media"] / "new.srt"))["result"] == "denied"
    answer = _ask(worker, op="stub", stub=str(host["stub"]), size=str(size))
    assert answer["code"] == 0 and answer["result"] == "wrote %d" % (size + 1)
    assert getattr(worker.process, "enforcement", None) == "enforced"
    assert worker._sandbox_failed_open is False


def test_the_probe_checks_the_filesystem_where_the_metadata_filter_does_not_exist(monkeypatch):
    # An architecture without the filter loses the full sandbox, not the part the provider profile uses.
    _enforcing_kernel()
    monkeypatch.setattr(spawn_module.seccomp, "supported", lambda: False)

    probe = spawn_module._run_probe(sys.executable)

    assert probe.reads_verified is True
    assert probe.enforced is False and probe.verified is False
    assert probe.enforces(PROVIDER_MIN_ABI) is True
    assert "metadata filter" in probe.reason
    assert provider_profile_status(probe)["state"] in ("enforced", "partial")


def test_the_status_lists_what_the_provider_profile_leaves_open_on_every_kernel():
    status = provider_profile_status(SandboxProbe(abi=10, verified=True, reads_verified=True))

    assert status["state"] == "enforced"
    text = " ".join(status["limitations"])
    assert "seccomp" in text and "mode, owner, times or extended attributes" in text
    assert "file-size" in text


def test_a_refused_start_is_its_own_state_and_clears_when_a_sandboxed_worker_answers():
    probe = SandboxProbe(abi=10, verified=True, reads_verified=True)
    spawn_module.note_provider_blocked("embeddedsubtitles", "the ruleset could not be applied: grant changed")

    status = provider_profile_status(probe)
    assert status["state"] == "blocked" and status["blocked"] == ["embeddedsubtitles"]
    assert "was not started" in status["notice"]
    spawn_module.clear_provider_notice("embeddedsubtitles")
    assert provider_profile_status(probe)["state"] == "enforced"

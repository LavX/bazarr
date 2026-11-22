# coding=utf-8
"""The sandbox profile of subtitle provider workers.

A provider worker cannot read the configuration or the database, sees the media library read-only,
and keeps its ``ffmpeg`` plugins working (large temporary files, no metadata filter). A kernel with
no Landlock runs it without the ruleset and a notice says so; where the host may enforce but cannot
confirm or apply the ruleset, the start is refused. The user can switch the sandbox off.

Most tests start the real worker runner with a fixture bundle through a real ``ProviderWorkerClient``;
the ones that need the kernel's Landlock skip on a host that has none.
"""
import dataclasses
import errno as errno_module
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_source_helpers import add_item, add_source
from provider_hub import worker as worker_module
from provider_hub.sandbox import (
    PROVIDER_MIN_ABI,
    SandboxProbe,
    landlock,
    probe_sandbox,
    provider_profile_gaps,
    provider_profile_limitations,
    provider_profile_status,
    sandbox_status,
)
from provider_hub.sandbox import policy as policy_module
from provider_hub.sandbox import seccomp as seccomp_module
from provider_hub.sandbox import spawn as spawn_module
from provider_hub import provider_roots as _provider_roots
from provider_hub.worker import ProviderLayout, ProviderWorkerClient, worker_command

RUNNER = Path(worker_module._RUNNER_PATH)
# The reader itself, which the autouse fixture replaces for the other tests.
REAL_AUTHENTICATION_OFF = _provider_roots.authentication_off
REAL_MEDIA_ROOT_FOLDERS = _provider_roots.media_root_folders

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
        if op == "source":
            def read(path):
                with open(path, encoding="utf-8") as handle:
                    return handle.read()

            path = video["name"]
            result = _try(lambda: read(path))
            if result.splitlines()[-1:] != ["exit 0"]:
                return []
            mode = _try(lambda: open(path, "r+").close())
            return [{"id": "source-fixture", "language": {"alpha3": "eng"}, "provider_payload": {
                "path": path, "write": mode, "sibling": _try(lambda: read(config["sibling"])),
                "backup": _try(lambda: read(config["backup"])),
                "exec": _try(lambda: subprocess.run([path], check=True).returncode),
            }}]
        if op == "list":
            return [{"result": _try(lambda: sorted(os.listdir(config["path"])))}]
        if op == "stub":
            completed = subprocess.run([config["stub"], config["size"]], capture_output=True, text=True)
            return [{"result": completed.stdout.strip() or completed.stderr.strip(), "code": completed.returncode}]
        if op == "chmod":
            return [{"result": _try(lambda: (os.chmod(config["path"], 0), "changed")[1])}]
        if op == "exit":
            os._exit(3)
        if op == "raw":
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            ctypes.set_errno(0)
            args = [ctypes.c_long(a) for a in config["args"]]
            result = libc.syscall(ctypes.c_long(config["number"]), *args)
            if result >= 0:
                os.close(result) if config.get("close") else None
                return [{"result": "created"}]
            return [{"result": "errno:%d" % ctypes.get_errno()}]
        if op == "mptcp":
            import socket

            def mptcp():
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262)
                s.close()
                return "created"
            return [{"result": _try(mptcp)}]
        if op == "connect":
            import socket

            def connect():
                with socket.create_connection(("127.0.0.1", config["port"]), timeout=3):
                    return "connected"
            return [{"result": _try(connect)}]
        if op == "env":
            return [{"result": {key: os.environ.get(key) for key in config["keys"]}}]
        raise ValueError(op)

    def download(self, provider_payload, language, config):
        with open(provider_payload["path"], encoding="utf-8") as handle:
            return handle.read().encode()
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
def clean_notices(monkeypatch):
    spawn_module.reset_provider_notices()
    worker_module._probe_again_at = 0.0
    # A login is set unless a test says otherwise: with none, the status always names the web API route.
    monkeypatch.setattr("provider_hub.provider_roots.authentication_off", lambda: False)
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


def test_source_search_and_download_get_one_read_only_file_per_request(host, client, source_db, monkeypatch):
    from media_sources import repository
    from media_sources.context import resolve_item
    from provider_hub import capabilities
    from provider_hub.registry import HubProxyProvider
    from provider_hub.state import OFFICIAL_CATALOG_REPO, OFFICIAL_CATALOG_SOURCE_ID
    from subliminal.video import Movie

    _enforcing_kernel()
    source_id, library_id, root = add_source(source_db, host["root"])
    movie_path, sibling = root / "Film.mkv", root / "Other.mkv"
    media_bytes = b"#!/bin/sh\nexit 0\n"
    movie_path.write_bytes(media_bytes)
    movie_path.chmod(0o700)
    sibling.write_text("another movie", encoding="utf-8")
    item_id = add_item(source_db, source_id, library_id, movie_path)
    video = Movie(str(movie_path), "Film")
    video.source_context = resolve_item(item_id, source_db)
    manifest = SimpleNamespace(schema_version=1, raw={}, source_repo=OFFICIAL_CATALOG_REPO, bundle_sha256="fixture")
    row = {"trusted": True, "origin": "catalog", "source_id": OFFICIAL_CATALOG_SOURCE_ID,
           "manifest": {"bundle_sha256": manifest.bundle_sha256}}
    monkeypatch.setattr(capabilities, "live_row", lambda provider_id: row)

    class Embedded(HubProxyProvider):
        provider_name = "embeddedsubtitles"

    Embedded.manifest = manifest
    worker = client()
    proxy = Embedded(worker_client=worker, op="source", sibling=str(sibling),
                     backup=str(host["backups"] / "backup.txt"))

    # The ordinary worker is already running, with only the Arr profile.
    assert _ask(worker, op="read", path=str(movie_path))["result"] == "denied"
    ordinary_process = worker.process

    [subtitle] = proxy.list_subtitles(video, [])
    assert subtitle.provider_payload == {
        "path": str(movie_path), "write": "denied", "exec": "denied", "sibling": "denied", "backup": "denied",
    }
    assert ordinary_process.poll() is not None
    assert worker.process.poll() is not None
    assert worker.launcher_cleanup_state() == "confirmed"

    # The normal pool carries this host-only context from the video to the candidate.
    subtitle.source_context = video.source_context
    assert proxy.download_subtitle(subtitle)
    assert subtitle.content == media_bytes
    assert worker.process.poll() is not None
    assert _ask(worker, op="read", path=str(movie_path))["result"] == "denied"

    # A backup subfolder inside an approved library is still excluded.
    nested_backups = root / "backups"
    nested_backups.mkdir()
    nested_path = nested_backups / movie_path.name
    nested_path.write_bytes(media_bytes)
    nested_id = add_item(source_db, source_id, library_id, nested_path)
    nested_video = Movie(str(nested_path), "Film")
    nested_video.source_context = resolve_item(nested_id, source_db)
    monkeypatch.setattr(_provider_roots, "backup_folders", lambda: [host["backups"], nested_backups])
    assert proxy.list_subtitles(nested_video, []) == []

    repository.update_library(library_id, source_db, enabled=0)
    assert proxy.list_subtitles(video, []) == []
    repository.update_library(library_id, source_db, enabled=1)
    repository.update_source(source_id, source_db, enabled=0)
    assert proxy.list_subtitles(video, []) == []
    repository.update_source(source_id, source_db, enabled=1)
    moved_root = host["root"] / "moved"
    moved_root.mkdir()
    repository.replace_roots(library_id, [(None, str(moved_root))], source_db)
    assert proxy.list_subtitles(video, []) == []
    moved_path = moved_root / movie_path.name
    moved_path.write_bytes(media_bytes)
    moved_path.chmod(0o700)
    repository.update_item(item_id, source_db, path=str(moved_path))
    moved_video = Movie(str(moved_path), "Film")
    moved_video.source_context = resolve_item(item_id, source_db)
    assert len(proxy.list_subtitles(moved_video, [])) == 1
    assert proxy.list_subtitles(video, []) == []
    row["trusted"] = False
    assert proxy.list_subtitles(moved_video, []) == []
    row["trusted"] = True
    repository.delete_source(source_id, source_db)
    assert proxy.list_subtitles(moved_video, []) == []


@pytest.mark.parametrize(("schema", "declared", "current", "allowed"), [
    (1, ["media_read"], ["media_read"], False),
    (2, None, [], False),
    (2, ["media_read"], ["network"], False),
    (2, ["media_read"], ["media_read"], True),
])
def test_standalone_media_needs_current_provider_permission(schema, declared, current, allowed, monkeypatch):
    from provider_hub import capabilities
    from provider_hub.registry import HubProxyProvider

    class CatalogProvider(HubProxyProvider):
        provider_name = "exampleprovider"
        manifest = SimpleNamespace(schema_version=schema, raw={"permissions": declared}, bundle_sha256="fixture")

    current_manifest = {"permissions": current, "bundle_sha256": "fixture"}
    monkeypatch.setattr(capabilities, "live_row", lambda provider_id: {"manifest": current_manifest, "trusted": True})
    grant = object()
    monkeypatch.setattr(_provider_roots, "source_media_file", lambda context: grant)

    assert CatalogProvider()._source_media(object()) is (grant if allowed else None)


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
    probe = SandboxProbe(abi=0, verified=False, absent=True, reason="Landlock is not available in this kernel")
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
    live = _enforcing_kernel()
    probe = SandboxProbe(abi=live.abi, verified=True, reads_verified=True)
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
    status = provider_profile_status(probe, authentication_off=False)

    assert status["state"] == state
    if gap is None:
        assert status["notice"] is None
        return
    assert "ABI %d" % abi in status["notice"]
    assert "cannot read the configuration or the database" in status["notice"]
    assert gap in status["notice"]
    # It is still sandboxed, so no provider is listed as running without it.
    assert status["unsandboxed"] == []


def test_only_a_kernel_without_landlock_fails_open_and_a_failed_host_check_refuses_the_start():
    absent = SandboxProbe(abi=0, verified=False, absent=True, reason="Landlock is not available")
    status = provider_profile_status(absent)
    assert status["state"] == "unsandboxed" and "Landlock is not available" in status["notice"]
    # The kernel may well enforce, but the check did not confirm it (it escaped, timed out, hit a file error).
    for reason in ("canary escaped the sandbox: {}", "canary timed out", "probe failed: disk full"):
        probe = SandboxProbe(abi=2, verified=False, reads_verified=False, reason=reason)
        status = provider_profile_status(probe)
        assert status["state"] == "blocked"
        assert "not started" in status["notice"] and reason.split(":")[0] in status["notice"]


@pytest.mark.parametrize("reason", ["canary timed out", "canary escaped the sandbox: {}", "probe failed: [Errno 28] No space left"])
def test_a_host_check_that_failed_on_a_kernel_that_may_enforce_refuses_every_start(host, client, monkeypatch, reason):
    probe = SandboxProbe(abi=10, verified=False, reads_verified=False, reason=reason)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)

    def refuse(*args, **kwargs):
        raise AssertionError("no worker may start, sandboxed or not")

    monkeypatch.setattr("provider_hub.sandbox.spawn_worker", refuse)
    monkeypatch.setattr(worker_module.subprocess, "Popen", refuse)
    worker = client()

    with pytest.raises(worker_module.WorkerError, match="was not started"):
        _ask(worker, op="read", path=str(host["media"] / "film.mkv"))
    assert worker._sandbox_failed_open is False and worker.process is None
    assert provider_profile_status(probe)["state"] == "blocked"
    assert provider_profile_status(probe)["blocked"] == ["fixture"]


def test_the_probe_falls_back_to_a_canary_without_the_filter_when_the_filter_cannot_be_applied(monkeypatch):
    _enforcing_kernel()
    real_start = spawn_module._start
    calls = []

    class Refused:
        returncode = 78

        @staticmethod
        def communicate(timeout=None):
            return b"", b"filter"

        @staticmethod
        def launcher_failure():
            return spawn_module.LauncherFailure("sandbox", "could not apply sandbox: metadata filter")

        @staticmethod
        def stop(grace=0):
            return None

    def start(spec, **kwargs):
        calls.append(spec.seccomp)
        return Refused() if spec.seccomp else real_start(spec, **kwargs)

    monkeypatch.setattr(spawn_module, "_start", start)

    probe = spawn_module._run_probe(sys.executable)

    assert calls == [True, False]
    assert probe.reads_verified is True and probe.absent is False
    assert probe.enforced is False and probe.enforces(PROVIDER_MIN_ABI) is True


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


# ----------------------------------------------------------------- Bazarr+'s own API and its port
@pytest.mark.parametrize("abi", [1, 2, 3])
def test_before_abi_4_the_notice_says_a_plugin_can_call_the_web_api_while_authentication_is_off(abi):
    probe = SandboxProbe(abi=abi, verified=abi >= 3, reads_verified=True)

    off = provider_profile_status(probe, authentication_off=True)
    on = provider_profile_status(probe, authentication_off=False)

    assert off["state"] == "partial"
    assert "own web API" in off["notice"] and "authentication off" in off["notice"]
    assert "turn authentication on" in off["notice"]
    assert "own web API" not in (on["notice"] or "")
    assert "network connections are not restricted" in " ".join(off["limitations"])


@pytest.mark.parametrize("abi", [4, 6, 10])
def test_with_authentication_off_the_status_is_partial_at_every_abi_and_names_the_forwarded_port_route(abi):
    probe = SandboxProbe(abi=abi, verified=True, reads_verified=True)

    off = provider_profile_status(probe, authentication_off=True)
    on = provider_profile_status(probe, authentication_off=False)

    assert off["state"] == "partial"
    assert "own web API" in off["notice"] and "turn authentication on" in off["notice"]
    assert "forwards to it" in off["notice"] and "reverse proxy" in off["notice"]
    assert on["state"] == "enforced" and on["notice"] is None
    text = " ".join(off["limitations"])
    assert "own ports are refused" in text and "io_uring" in text and "not covered" in text


def test_the_profile_asks_for_a_missing_capability_drop_to_be_best_effort_and_says_so():
    gaps = " ".join(provider_profile_gaps(10, capabilities_dropped=False))

    assert "no capability bindings" in gaps and "filesystem rules still apply" in gaps
    assert provider_profile_status(SandboxProbe(abi=10, verified=True, reads_verified=True), authentication_off=False)["state"] == "enforced"


def test_the_profile_asks_for_bazarrs_own_port_to_be_refused_and_nothing_else(host, monkeypatch):
    monkeypatch.setattr("provider_hub.provider_roots.own_ports", lambda: [6767])
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})
    launch = json.loads(spec.to_launch_json(enforce=True, min_abi=4))

    assert launch["deny_connect_ports"] == [6767]
    assert launch["connect_ports"] is None
    assert launch["block_mptcp"] is True and launch["seccomp"] is False


def test_a_provider_cannot_connect_to_bazarrs_port_where_the_kernel_has_network_rules(host, client, monkeypatch):
    import socket

    probe = _enforcing_kernel()
    if probe.abi < 4:
        pytest.skip("Landlock ABI %d has no network rules" % probe.abi)
    own, other = socket.socket(), socket.socket()
    try:
        for listener in (own, other):
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
        monkeypatch.setattr("provider_hub.provider_roots.own_ports", lambda: [own.getsockname()[1]])
        worker = client()

        refused = _ask(worker, op="connect", port=own.getsockname()[1])["result"]
        allowed = _ask(worker, op="connect", port=other.getsockname()[1])["result"]
    finally:
        own.close()
        other.close()

    assert refused == "denied"
    assert allowed == "connected"


# ------------------------------------------------------------------- the environment hold and the launcher
class _Launcher:
    """A launcher that has exited, with what its status pipe said."""

    def __init__(self, cleanup, returncode=0, running=False):
        self.cleanup, self.returncode, self.running = cleanup, returncode, running

    def poll(self):
        return None if self.running else self.returncode

    def launcher_cleanup(self):
        return self.cleanup


def _client_with(*launchers):
    worker = ProviderWorkerClient.__new__(ProviderWorkerClient)
    worker._launchers = list(launchers)
    worker.process = launchers[-1] if launchers else None
    worker._has_exited = lambda process: not process.running
    worker.group_gone = lambda ceiling=10.0: True
    return worker


def test_the_hold_is_released_only_once_every_launcher_has_said_its_cleanup_finished():
    done = _Launcher("complete")
    assert _client_with(done).launcher_cleanup_state() == "confirmed"
    assert worker_module._client_gone(_client_with(done), 0.0) is True
    # A launcher that is still running, or an earlier one of a respawn that gave up on survivors, keeps it.
    assert _client_with(_Launcher("complete", running=True)).launcher_cleanup_state() == "pending"
    survivors = _client_with(_Launcher("survivors"), done)
    assert survivors.launcher_cleanup_state() == "unconfirmed"
    assert worker_module._client_gone(survivors, 0.0) is False
    assert _client_with(_Launcher(None)).launcher_cleanup_state() == "unconfirmed"
    # Killed from outside: it never said it finished, whatever its pipe holds.
    assert _client_with(_Launcher("complete", returncode=-9)).launcher_cleanup_state() == "unconfirmed"


def test_a_launcher_that_never_confirmed_keeps_the_environment_for_good_and_ends_the_wait():
    worker = _client_with(_Launcher("survivors"))
    released, pauses = [], []

    worker_module._wait_until_gone(worker, lambda: released.append(1), sleep=pauses.append)

    assert released == [] and pauses == []


def test_a_launcher_that_confirms_later_releases_the_hold():
    launcher = _Launcher("complete", running=True)
    worker = _client_with(launcher)
    released, pauses = [], []

    def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            launcher.running = False

    worker_module._wait_until_gone(worker, lambda: released.append(1), sleep=sleep)

    assert released == [1] and len(pauses) == 3


def test_a_sandboxed_worker_that_was_stopped_confirms_its_cleanup_and_releases_the_hold(host, client):
    _enforcing_kernel()
    worker = client()
    _ask(worker, op="read", path=str(host["media"] / "film.mkv"))
    assert worker._launchers

    worker.stop()
    released = []

    assert worker_module.release_when_gone(worker, lambda: released.append(1), "a fixture", budget=10.0) is True
    assert released == [1] and worker.launcher_cleanup_state() == "confirmed"


# ------------------------------------------------------------------------------------ provider_roots
def test_program_folders_are_the_existing_absolute_directories_on_path(tmp_path):
    from provider_hub import provider_roots

    here = tmp_path / "bin"
    here.mkdir()
    value = os.pathsep.join(["relative", str(here), str(tmp_path / "missing"), "/", str(here)])

    assert provider_roots.program_folders(value) == [str(here)]


def test_the_folder_of_a_linked_tool_is_granted_where_the_file_really_is(tmp_path, monkeypatch):
    from provider_hub import provider_roots

    real = tmp_path / "vendor" / "ffmpeg"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_path / "bin" / "ffprobe"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(shutil_module(), "which", lambda name: str(link) if name == "ffprobe" else None)
    monkeypatch.setattr("utilities.binaries.get_binary", lambda name: str(real) if name == "ffmpeg" else None)

    assert provider_roots.binary_folders() == [str(real.parent)]


def shutil_module():
    import shutil

    return shutil


def test_the_backup_folder_and_the_login_state_come_from_the_settings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from provider_hub import provider_roots

    def settings(auth, folder=str(tmp_path / "backups")):
        return SimpleNamespace(auth=SimpleNamespace(type=auth), general=SimpleNamespace(port=6767), backup=SimpleNamespace(folder=folder))

    monkeypatch.setattr("app.config.settings", settings(None))
    assert REAL_AUTHENTICATION_OFF() is True
    assert provider_roots.backup_folders() == [tmp_path / "backups"]
    monkeypatch.setattr("app.config.settings", settings("form", folder=""))
    assert REAL_AUTHENTICATION_OFF() is False
    assert provider_roots.backup_folders() == []


def test_the_own_ports_are_the_ones_the_server_bound_the_port_argument_and_the_setting(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from provider_hub import provider_roots

    monkeypatch.setattr("app.config.settings", SimpleNamespace(general=SimpleNamespace(port=6767)))
    # --port overrides the setting, as under the docker supervisor, which also keeps 6767 for its front listener.
    monkeypatch.setattr("app.get_args.args", SimpleNamespace(port=41234))
    monkeypatch.delitem(sys.modules, "app.server", raising=False)
    assert provider_roots.own_ports() == [41234, 6767]
    # No --port: the setting alone.
    monkeypatch.setattr("app.get_args.args", SimpleNamespace(port=None))
    assert provider_roots.own_ports() == [6767]
    # The running server's own record wins: it fell back to another port, and the socket says what it bound.
    server_module = ModuleType("app.server")
    server_module.webserver = SimpleNamespace(port=6768, server=SimpleNamespace(effective_port=6769))
    monkeypatch.setitem(sys.modules, "app.server", server_module)
    assert provider_roots.own_ports() == [6768, 6769, 6767]


def test_a_port_argument_that_overrides_the_setting_is_refused_to_the_worker(host, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("app.config.settings", SimpleNamespace(
        general=SimpleNamespace(port=6767), auth=SimpleNamespace(type=None), backup=SimpleNamespace(folder="")))
    monkeypatch.setattr("app.get_args.args", SimpleNamespace(port=41234, config_dir=str(host["config"])))
    monkeypatch.delitem(sys.modules, "app.server", raising=False)
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})

    assert json.loads(spec.to_launch_json(enforce=True, min_abi=4))["deny_connect_ports"] == [6767, 41234]


def test_the_media_roots_come_from_every_instance_through_its_path_mappings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from provider_hub import provider_roots

    shows, movies, sports = tmp_path / "tv", tmp_path / "films", tmp_path / "events"
    mapped = tmp_path / "mapped"
    for folder in (shows, movies, sports, mapped):
        folder.mkdir()
    rows = {
        "TableShowsRootfolder": [(1, str(shows)), (2, "/remote/tv")],
        "TableMoviesRootfolder": [(1, str(movies))],
        "TableSportsLeaguesRootfolder": [(3, str(sports))],
    }

    class Column:
        def __init__(self, table):
            self.table = table

    class Table:
        def __init__(self, name):
            self.arr_instance_id, self.path = Column(name), Column(name)

    fake_tables = {name: Table(name) for name in rows}

    class Database:
        @staticmethod
        def execute(statement):
            return SimpleNamespace(all=lambda: rows[statement])

    import app.database as app_database

    for name, table in fake_tables.items():
        monkeypatch.setattr(app_database, name, table, raising=False)
    monkeypatch.setattr(app_database, "database", Database(), raising=False)
    monkeypatch.setattr(app_database, "select", lambda *columns: columns[0].table, raising=False)
    mappings = SimpleNamespace(
        path_replace_instance=lambda path, instance, kind: str(mapped) if path == "/remote/tv" else path,
        path_replace=lambda path: path,
        path_replace_movie=lambda path: path,
    )
    monkeypatch.setattr("utilities.path_mappings.path_mappings", mappings)

    found = provider_roots.media_root_folders()

    assert set(found) == {str(shows), str(movies), str(sports), str(mapped)}


def _fake_media_database(monkeypatch, execute):
    from types import SimpleNamespace

    import app.database as app_database

    class Table:
        arr_instance_id = path = None

    for name in ("TableShowsRootfolder", "TableMoviesRootfolder", "TableSportsLeaguesRootfolder"):
        monkeypatch.setattr(app_database, name, Table(), raising=False)
    monkeypatch.setattr(app_database, "database", SimpleNamespace(execute=execute), raising=False)
    monkeypatch.setattr(app_database, "select", lambda *columns: columns, raising=False)
    monkeypatch.setattr(
        "utilities.path_mappings.path_mappings",
        SimpleNamespace(path_replace_instance=lambda p, *_: p, path_replace=lambda p: p, path_replace_movie=lambda p: p),
    )


def test_a_library_with_no_media_yet_still_gives_a_ruleset_without_media_grants(host, monkeypatch):
    from types import SimpleNamespace

    # A fresh install: the database answers and holds no series, movie or sports root folder.
    _fake_media_database(monkeypatch, lambda _statement: SimpleNamespace(all=lambda: []))
    monkeypatch.setattr("provider_hub.provider_roots.media_root_folders", REAL_MEDIA_ROOT_FOLDERS)

    assert REAL_MEDIA_ROOT_FOLDERS() == []
    spec = worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})

    launch = json.loads(spec.to_launch_json(enforce=True, min_abi=4))
    assert str(host["media"]) not in json.dumps(launch)
    assert str(host["bundle"]) in json.dumps(launch)


def test_a_database_that_cannot_be_read_refuses_the_ruleset(host, monkeypatch):
    def unreadable(_statement):
        raise RuntimeError("no such table: table_shows_rootfolder")

    _fake_media_database(monkeypatch, unreadable)
    monkeypatch.setattr("provider_hub.provider_roots.media_root_folders", REAL_MEDIA_ROOT_FOLDERS)

    with pytest.raises(RuntimeError, match="no such table"):
        worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})


def test_a_link_to_the_configuration_file_in_a_granted_folder_refuses_the_profile(host):
    # config.yaml moved out and linked back: its target is in the media folder, which would be granted.
    target = host["media"] / "config.yaml"
    (host["config"] / "config" / "config.yaml").replace(target)
    (host["config"] / "config" / "config.yaml").symlink_to(target)

    with pytest.raises(policy_module.PolicyError, match="lies inside the granted tree"):
        worker_module.provider_spec(host["layout"], worker_command(sys.executable, RUNNER), host["bundle"], {})


# ------------------------------------------------------------------------------------------- MPTCP
def _run_bpf(program: bytes, arch: int, number: int, args=(0, 0, 0)) -> int:
    """Evaluate a classic BPF seccomp program against one system call and return its verdict."""
    import struct

    data = struct.pack("=iIQ6Q", number, arch, 0, *(list(args) + [0] * 6)[:6])
    code = [struct.unpack_from("=HBBI", program, offset) for offset in range(0, len(program), 8)]
    accumulator, pc = 0, 0
    while True:
        op, jt, jf, k = code[pc]
        pc += 1
        if op == 0x20:
            accumulator = struct.unpack_from("=I", data, k)[0]
        elif op == 0x15:
            pc += jt if accumulator == k else jf
        elif op == 0x45:
            pc += jt if accumulator & k else jf
        elif op == 0x06:
            return k
        else:
            raise AssertionError("unexpected instruction %#x" % op)


@pytest.mark.parametrize("arch,number", [(seccomp_module.AUDIT_ARCH_X86_64, 41), (seccomp_module.AUDIT_ARCH_AARCH64, 198)])
def test_the_socket_filter_keeps_a_worker_on_plain_tcp_and_allows_everything_else(arch, number):
    program = seccomp_module.build_socket_filter(arch)
    proto = errno_module.EPROTONOSUPPORT
    deny_proto = seccomp_module.SECCOMP_RET_ERRNO | proto
    deny_perm = seccomp_module.SECCOMP_RET_ERRNO | errno_module.EPERM
    allow, kill = seccomp_module.SECCOMP_RET_ALLOW, seccomp_module.SECCOMP_RET_KILL_PROCESS
    stream, mptcp, smc = 1, seccomp_module.IPPROTO_MPTCP, seccomp_module.IPPROTO_SMC

    for domain in (2, 10):
        assert _run_bpf(program, arch, number, (domain, stream, mptcp)) == deny_proto
        assert _run_bpf(program, arch, number, (domain, stream, smc)) == deny_proto
        # The kernel reads an int: whatever the upper half of the register holds, the low half decides.
        assert _run_bpf(program, arch, number, (domain, stream, mptcp | (1 << 32))) == deny_proto
        assert _run_bpf(program, arch, number, (domain, stream, mptcp | (0xFFFFFFFF << 32))) == deny_proto
        assert _run_bpf(program, arch, number, (domain, stream, 6)) == allow  # plain TCP
        assert _run_bpf(program, arch, number, (domain, stream, 0)) == allow
        assert _run_bpf(program, arch, number, (domain, stream, 6 | (1 << 32))) == allow  # still plain TCP
    assert _run_bpf(program, arch, number, (seccomp_module.AF_SMC, stream, 0)) == deny_proto
    assert _run_bpf(program, arch, number, (seccomp_module.AF_SMC | (1 << 32), stream, 0)) == deny_proto
    for ring in seccomp_module.IO_URING:
        assert _run_bpf(program, arch, ring) == deny_perm
    assert _run_bpf(program, arch, number + 1, (2, stream, mptcp)) == allow  # another call
    assert _run_bpf(program, arch ^ 1, number, (2, stream, 6)) == kill  # another syscall ABI
    if arch == seccomp_module.AUDIT_ARCH_X86_64:
        assert _run_bpf(program, arch, number | seccomp_module.X32_SYSCALL_BIT, (2, stream, 6)) == kill


def _network_rules_here():
    probe = _enforcing_kernel()
    if probe.abi < 4:
        pytest.skip("Landlock ABI %d has no network rules, so the socket filter is not installed" % probe.abi)
    if not seccomp_module.supported():
        pytest.skip("no seccomp filter for this architecture")


def test_a_provider_cannot_open_an_mptcp_or_smc_socket_even_with_the_upper_half_of_the_protocol_set(host, client):
    _network_rules_here()
    worker = client()
    sock = 41 if seccomp_module.native_audit_arch() == seccomp_module.AUDIT_ARCH_X86_64 else 198

    assert _ask(worker, op="mptcp")["result"].startswith("error")
    mptcp = seccomp_module.IPPROTO_MPTCP
    for protocol in (mptcp, mptcp | (1 << 32), seccomp_module.IPPROTO_SMC):
        answer = _ask(worker, op="raw", number=sock, args=[2, 1, protocol], close=True)["result"]
        assert answer == "errno:%d" % errno_module.EPROTONOSUPPORT, protocol
    assert _ask(worker, op="raw", number=sock, args=[seccomp_module.AF_SMC, 1, 0], close=True)["result"] == (
        "errno:%d" % errno_module.EPROTONOSUPPORT
    )
    # Plain TCP is untouched.
    assert _ask(worker, op="raw", number=sock, args=[2, 1, 0], close=True)["result"] == "created"


def test_a_provider_cannot_use_io_uring_to_open_sockets_around_the_filter(host, client):
    _network_rules_here()
    worker = client()

    for number in seccomp_module.IO_URING:
        answer = _ask(worker, op="raw", number=number, args=[0, 0, 0, 0, 0, 0])["result"]
        assert answer == "errno:%d" % errno_module.EPERM, number


def test_the_socket_filter_is_not_installed_where_there_is_no_port_rule(host, client, monkeypatch):
    # Below ABI 4 there is nothing for it to protect, and a failure to install it would only refuse providers.
    probe = _enforcing_kernel()
    if not seccomp_module.supported():
        pytest.skip("no seccomp filter for this architecture")
    capped = SandboxProbe(abi=3, verified=True, reads_verified=True)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: capped)
    sock = 41 if seccomp_module.native_audit_arch() == seccomp_module.AUDIT_ARCH_X86_64 else 198
    worker = client()

    assert _ask(worker, op="raw", number=sock, args=[2, 1, 0], close=True)["result"] == "created"
    # An outer policy (a container's seccomp profile, the sysctl) may refuse io_uring with EPERM for every
    # process here, which says nothing about our filter. Only where this test process gets another answer
    # does the worker's answer tell whether the filter was installed.
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    libc.syscall(ctypes.c_long(425), ctypes.c_long(0), ctypes.c_long(0))
    if ctypes.get_errno() != errno_module.EPERM:
        ring = _ask(worker, op="raw", number=425, args=[0, 0])["result"]
        assert ring != "errno:%d" % errno_module.EPERM  # io_uring is the kernel's to answer, not the filter's
    assert probe.abi >= 3


def test_without_a_filter_for_the_architecture_the_notice_names_the_mptcp_route_while_authentication_is_off(monkeypatch):
    probe = SandboxProbe(abi=6, verified=True, reads_verified=True)
    monkeypatch.setattr(spawn_module.seccomp, "supported", lambda: False)

    status = provider_profile_status(probe, authentication_off=True)
    quiet = provider_profile_status(probe, authentication_off=False)

    assert status["state"] == "partial" and "MPTCP" in status["notice"] and "turn authentication on" in status["notice"]
    assert quiet["state"] == "enforced"


def test_the_limitations_name_the_unix_socket_and_signal_gaps_of_older_kernels():
    old = " ".join(provider_profile_limitations(3))
    new = " ".join(provider_profile_limitations(10))

    assert "pathname UNIX sockets" in old and "signal other processes" in old
    assert "pathname UNIX sockets" not in new and "signal other processes" not in new
    assert "root folders of Sonarr, Radarr and Sportarr" in new


# ------------------------------------------------------------------------ the host check, again
def test_a_host_check_that_failed_is_asked_again_and_a_pass_lets_the_start_go_ahead(host, client, monkeypatch):
    real = _enforcing_kernel()
    bad = SandboxProbe(abi=real.abi, verified=False, reads_verified=False, reason="canary timed out")
    calls = []

    def probe(refresh=False, **kwargs):
        calls.append(refresh)
        return real if refresh else bad

    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", probe)
    worker = client()

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert calls == [False, True]


def test_the_refusal_says_how_it_ends(host, client, monkeypatch):
    bad = SandboxProbe(abi=10, verified=False, reads_verified=False, reason="canary timed out")
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: bad)
    worker = client()

    with pytest.raises(worker_module.WorkerError, match="switching the sandbox off"):
        _ask(worker, op="read", path=str(host["media"] / "film.mkv"))


def test_an_architecture_without_capability_bindings_still_gets_landlock_and_the_notice_names_the_missing_part(
    host, client, monkeypatch
):
    real = _enforcing_kernel()
    monkeypatch.setattr(spawn_module.capabilities, "supported", lambda: False)

    probe = spawn_module._run_probe(sys.executable)

    assert probe.absent is False and probe.reads_verified is True
    assert probe.enforced is False and probe.enforces(PROVIDER_MIN_ABI) is True
    assert "capability" in probe.reason
    status = provider_profile_status(probe, authentication_off=False)
    assert status["state"] == "partial" and "no capability bindings" in status["notice"]
    # The worker starts, under the filesystem rules, with the capability drop best effort.
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)
    worker = client()
    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert getattr(worker.process, "enforcement", None) == "enforced"
    assert real.abi >= 1


@pytest.fixture
def launcher_without_capability_calls(tmp_path, monkeypatch):
    """A copy of the launcher whose capability drop fails in the child, as on an architecture with no bindings.

    The launcher imports its siblings by path, so the parent's own modules never reach it: this is
    the only way to exercise what the child does when the drop raises.
    """
    folder = tmp_path / "launcher"
    folder.mkdir()
    source = Path(spawn_module.LAUNCHER_PATH).parent
    for name in ("launcher.py", "landlock.py", "seccomp.py", "capabilities.py"):
        (folder / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    broken = (folder / "capabilities.py").read_text(encoding="utf-8")
    (folder / "capabilities.py").write_text(
        broken
        + "\n\ndef drop_all(status_path=None):\n    raise OSError(1, 'no capability bindings for this test')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(spawn_module, "LAUNCHER_PATH", folder / "launcher.py")
    return folder


def test_when_the_childs_capability_drop_fails_the_landlock_rules_still_apply_if_the_spec_allows_it(
    host, client, monkeypatch, launcher_without_capability_calls
):
    _enforcing_kernel()
    # The parent believes the architecture has no bindings, so the spec makes the drop best effort.
    monkeypatch.setattr(spawn_module.capabilities, "supported", lambda: False)
    probe = spawn_module._run_probe(sys.executable)
    assert probe.reads_verified is True and probe.enforces(PROVIDER_MIN_ABI) is True
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)
    worker = client()

    assert _ask(worker, op="read", path=str(host["config"] / "config" / "config.yaml"))["result"] == "denied"
    assert _ask(worker, op="write", path=str(host["media"] / "film.mkv"))["result"] == "denied"
    assert getattr(worker.process, "enforcement", None) == "enforced"
    assert worker._sandbox_failed_open is False


def test_when_the_childs_capability_drop_fails_and_the_spec_requires_it_the_start_is_refused(
    host, client, monkeypatch, launcher_without_capability_calls
):
    # The control: the same broken child with the drop required (the parent thinks the bindings exist).
    real = _enforcing_kernel()
    probe = SandboxProbe(abi=real.abi, verified=True, reads_verified=True)
    monkeypatch.setattr("provider_hub.sandbox.probe_sandbox", lambda *a, **k: probe)
    worker = client()

    with pytest.raises(worker_module.WorkerError, match="was not started"):
        _ask(worker, op="read", path=str(host["media"] / "film.mkv"))
    assert worker._sandbox_failed_open is False

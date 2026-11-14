# coding=utf-8
"""Sandbox launcher, Landlock rules, probe and fallback policy.

Pure policy and mask tests run everywhere. Tests that need the kernel's
Landlock skip with a reason where it is missing or too old; tests that only
exercise the launcher's limits, environment and process handling also run in
the "declared" mode on any Linux host.

Set BAZARR_REQUIRE_SANDBOX_TESTS=1 to turn "this host cannot enforce a sandbox"
from a skip into a failure. CI sets it for this file (GitHub's Ubuntu runners
have Landlock), so a regression that makes the probe report "not enforced"
cannot hide behind skipped tests. Skips for a kernel ABI that is simply too old
for one specific test (need_abi) stay skips.
"""
import dataclasses
import errno
import gc
import importlib.util
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from provider_hub.sandbox import (
    WorkerProcess,
    Limits,
    PolicyError,
    RuntimeSpec,
    SandboxPolicy,
    SandboxProbe,
    SandboxRefused,
    admission,
    capabilities,
    clean_env,
    landlock,
    policy as policy_module,
    probe_sandbox,
    sandbox_status,
    seccomp,
    spawn,
    spawn_worker,
)

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")

DECLARED = SandboxProbe(abi=0, verified=False, reason="test: no Landlock")


def cannot_enforce(reason, require=None):
    """Skip, or fail when the run insists on a working sandbox."""
    if require is None:
        require = os.environ.get("BAZARR_REQUIRE_SANDBOX_TESTS") == "1"
    if require:
        pytest.fail("BAZARR_REQUIRE_SANDBOX_TESTS=1 but the sandbox is not enforced: %s" % reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def live():
    """The real probe of this machine, skipping where Landlock is unusable."""
    if not sys.platform.startswith("linux"):
        cannot_enforce("not Linux")
    probe = probe_sandbox(refresh=True)
    if not probe.enforced:
        cannot_enforce("Landlock not enforceable here: %s" % probe.reason)
    return probe


def need_abi(probe, abi):
    if probe.abi < abi:
        pytest.skip("needs Landlock ABI %d, kernel has %d" % (abi, probe.abi))


def make_spec(tmp_path, script, *, rw=None, ro=(), ports=None, env_extra=None, limits=None, abi_cap=None, cwd=None):
    rw_dir = Path(rw) if rw else tmp_path / "rw"
    rw_dir.mkdir(exist_ok=True)
    policy = SandboxPolicy.for_interpreter(
        sys.executable,
        read_only=ro,
        read_write=[rw_dir],
        connect_ports=ports,
        abi_cap=abi_cap,
    )
    return RuntimeSpec(
        interpreter=sys.executable,
        args=("-I", "-c", textwrap.dedent(script)),
        cwd=str(cwd or rw_dir),
        env=clean_env(home=rw_dir, tmpdir=rw_dir, extra=env_extra),
        policy=policy,
        limits=limits or Limits(),
        term_grace=1.0,
    )


class Result(tuple):
    """``(returncode, stdout, stderr)`` plus the launcher's own failure record."""

    failure = None


def run(spec, probe, timeout=30, **kwargs):
    process = spawn_worker(spec, probe=probe, **kwargs)
    try:
        out, err = process.communicate(timeout=timeout)
    finally:
        process.stop(0.5)
    result = Result((process.returncode, out.decode(), err.decode()))
    result.failure = process.launcher_failure()
    return result


def alive(pid):
    """True while the process runs (a zombie does not count). Works without /proc."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return False  # /proc exists and the process is gone
    except OSError:
        pass
    # No /proc (macOS): ask ps for the state.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_exited_unreaped(process, seconds=10.0):
    """Wait for the child to exit without letting the process object reap it."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            assert process.returncode is None
            return
        time.sleep(0.05)
    raise AssertionError("the process did not exit")


def status_records(raw):
    """``(failure record, cleanup record)`` from what the launcher wrote on its status pipe."""
    if isinstance(raw, bytes):
        raw = raw.decode()
    failure = cleanup = None
    for line in raw.splitlines():
        record = json.loads(line)
        if "kind" in record:
            assert failure is None, "more than one failure record"
            failure = record
        else:
            assert cleanup is None, "more than one cleanup record"
            cleanup = record
    return failure, cleanup


def kill_leftover_sleepers(pids):
    """Kill detached sleepers a test left on purpose. Only a pid still running a 300 s sleeper dies."""
    for pid in pids:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as handle:
                command = handle.read()
            if b"sleep(300)" in command:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def wait_dead(pid, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


# --- Pure policy -----------------------------------------------------------


def test_policy_grants_system_paths_and_never_the_root():
    policy = SandboxPolicy.for_interpreter(sys.executable)
    assert "/usr" in policy.read_only
    assert "/" not in policy.read_only
    assert "/proc/self" in policy.read_only
    # The host's own /proc entry must never be granted by resolving /proc/self.
    assert "/proc/%d" % os.getpid() not in policy.read_only
    with pytest.raises(PolicyError):
        SandboxPolicy.for_interpreter(sys.executable, read_only=["/"])


def test_policy_requires_absolute_paths(tmp_path):
    with pytest.raises(PolicyError):
        SandboxPolicy.for_interpreter(sys.executable, read_write=["relative/dir"])


def test_policy_port_rules(tmp_path):
    policy = SandboxPolicy.for_interpreter(sys.executable, connect_ports=[443, 80])
    assert policy.connect_ports == frozenset({443, 80, 53})
    assert SandboxPolicy.for_interpreter(sys.executable, connect_ports=[443], include_dns_tcp=False).connect_ports == frozenset({443})
    assert SandboxPolicy.for_interpreter(sys.executable).connect_ports is None
    with pytest.raises(PolicyError, match="reserved"):
        SandboxPolicy.for_interpreter(sys.executable, connect_ports=[443, 6767], reserved_ports=[6767])
    with pytest.raises(PolicyError):
        SandboxPolicy.for_interpreter(sys.executable, connect_ports=[70000])


def test_policy_refuses_a_grant_that_contains_a_protected_path(tmp_path):
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    config.write_text("x", encoding="utf-8")
    with pytest.raises(PolicyError, match="granted tree"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "config"], protected_paths=[config])
    with pytest.raises(PolicyError, match="granted tree"):
        SandboxPolicy.for_interpreter(sys.executable, read_only=[config], protected_paths=[config])
    sibling = tmp_path / "plugins"
    sibling.mkdir()
    SandboxPolicy.for_interpreter(sys.executable, read_write=[sibling], protected_paths=[config])


def test_policy_refuses_writable_system_paths():
    with pytest.raises(PolicyError):
        SandboxPolicy.for_interpreter(sys.executable, read_write=["/usr"])
    with pytest.raises(PolicyError, match="system path|symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=["/etc"])


def test_policy_refuses_nested_writable_grants(tmp_path):
    (tmp_path / "rw" / "inner").mkdir(parents=True)
    with pytest.raises(PolicyError, match="nested"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "rw", tmp_path / "rw" / "inner"])
    other = tmp_path / "other"
    other.mkdir()
    SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "rw", other])


@pytest.mark.parametrize("sub_first", [True, False])
def test_overlapping_writable_roots_are_refused_before_any_link_is_followed(tmp_path, sub_first):
    root = tmp_path / "root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "sub").symlink_to(elsewhere)
    grants = [root / "sub", root] if sub_first else [root, root / "sub"]
    with pytest.raises(PolicyError, match="nested"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=grants)
    # The same overlap is refused when sub is a plain directory too.
    (root / "sub").unlink()
    (root / "sub").mkdir()
    with pytest.raises(PolicyError, match="nested"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=grants)
    # A repeated root is the same grant, not an overlap.
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[root, root])
    assert policy.read_write == (str(root),)


@pytest.mark.parametrize("alias_first", [True, False])
def test_a_writable_root_whose_link_passes_through_another_root_is_refused(tmp_path, alias_first):
    root = tmp_path / "root"
    (root / "dir").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root / "dir")
    grants = [alias, root] if alias_first else [root, alias]
    with pytest.raises(PolicyError, match="through a symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=grants)
    # A link to a directory that no other root touches is fine.
    free = tmp_path / "free"
    free.mkdir()
    (tmp_path / "free-link").symlink_to(free)
    other = tmp_path / "other"
    other.mkdir()
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "free-link", other])
    assert set(policy.read_write) == {str(free), str(other)}
    # Two names for one directory are refused as well.
    twin = tmp_path / "twin"
    twin.symlink_to(other)
    with pytest.raises(PolicyError, match="through a symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[twin, other])


def test_policy_requires_existing_writable_directories(tmp_path):
    with pytest.raises(PolicyError, match="does not exist"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "missing"])


def test_policy_keeps_only_resolved_paths_and_records_identities(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[link])
    assert policy.read_write == (str(real),)
    info = os.stat(real)
    assert policy.identities[str(real)] == (info.st_dev, info.st_ino)
    spec = RuntimeSpec(
        interpreter=sys.executable, args=("-I", "-c", "pass"), cwd=str(real), env=clean_env(), policy=policy
    )
    payload = json.loads(spec.to_launch_json(enforce=True, min_abi=1))
    assert [str(real), info.st_dev, info.st_ino] in payload["read_write"]


def test_policy_refuses_grants_a_worker_could_redirect(tmp_path):
    rw = tmp_path / "rw"
    (rw / "inner").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[rw / "inner"])
    (rw / "inner").rmdir()
    (rw / "inner").symlink_to(outside)
    with pytest.raises(PolicyError, match="symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[rw / "inner"])
    # Reaching the writable tree through a link from elsewhere is refused too.
    alias = tmp_path / "alias"
    alias.symlink_to(rw)
    with pytest.raises(PolicyError, match="through a symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[alias])
    # A link above the root of the tree is the host's own business.
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(tmp_path)
    SandboxPolicy.for_interpreter(sys.executable, read_write=[parent_link / "outside"])


def test_a_worker_cannot_widen_grants_by_editing_its_venv(tmp_path):
    import venv

    rw = tmp_path / "rw"
    rw.mkdir()
    venv.create(rw / "env", with_pip=False, symlinks=True)
    interpreter = rw / "env" / "bin" / "python"
    before = SandboxPolicy.for_interpreter(interpreter, read_write=[rw])
    secret = tmp_path / "secret"
    (secret / "bin").mkdir(parents=True)
    (secret / "bin" / "python").write_text("")
    cfg = rw / "env" / "pyvenv.cfg"
    cfg.write_text("home = %s\ninclude-system-site-packages = false\n" % (secret / "bin"))
    tampered_cfg = SandboxPolicy.for_interpreter(interpreter, read_write=[rw])
    interpreter.unlink()
    interpreter.symlink_to(secret / "bin" / "python")
    tampered_link = SandboxPolicy.for_interpreter(interpreter, read_write=[rw])
    for policy in (tampered_cfg, tampered_link):
        assert policy.read_only == before.read_only
        assert not any(str(secret) in p for p in (*policy.read_only, *policy.read_write))
    # The same file read from a venv outside every writable grant is still honored.
    outside = tmp_path / "outside"
    venv.create(outside, with_pip=False, symlinks=True)
    assert policy_module.interpreter_read_paths(outside / "bin" / "python")
    assert str(secret) not in " ".join(policy_module.interpreter_read_paths(interpreter, [rw]))


def _secret_prefix(tmp_path):
    secret = tmp_path / "secret"
    (secret / "bin").mkdir(parents=True)
    (secret / "bin" / "python").write_text("")
    return secret


def test_an_alias_whose_chain_crosses_a_writable_link_is_not_followed(tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    secret = _secret_prefix(tmp_path)
    outside = tmp_path / "outside" / "bin"
    outside.mkdir(parents=True)
    alias = outside / "python"
    hop = rw / "hop"
    alias.symlink_to(hop)
    # Benign while the worker has not touched the link.
    hop.symlink_to(sys.executable)
    benign = SandboxPolicy.for_interpreter(alias, read_write=[rw])
    # The worker repoints the hop; the next build must not follow it.
    hop.unlink()
    hop.symlink_to(secret / "bin" / "python")
    steered = SandboxPolicy.for_interpreter(alias, read_write=[rw])
    assert not any(str(secret) in p for p in (*steered.read_only, *steered.read_write))
    paths = policy_module.interpreter_read_paths(alias, [rw])
    assert str(secret) not in " ".join(paths)
    assert str(tmp_path / "outside") not in paths
    # Restoring the link does not bring the chain back into favor.
    hop.unlink()
    hop.symlink_to(sys.executable)
    restored = SandboxPolicy.for_interpreter(alias, read_write=[rw])
    assert restored.read_only == steered.read_only
    assert set(benign.read_only) >= set(steered.read_only) - {sys.base_prefix, sys.base_exec_prefix}


def test_a_hop_deep_in_the_chain_counts_as_well(tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    secret = _secret_prefix(tmp_path)
    (rw / "dir").symlink_to(secret / "bin")
    first = tmp_path / "first"
    first.symlink_to(tmp_path / "second")
    (tmp_path / "second").symlink_to(rw / "dir" / "python")
    paths = policy_module.interpreter_read_paths(first, [rw])
    assert str(secret) not in " ".join(paths)


def test_a_pyvenv_cfg_reached_through_a_writable_link_is_ignored(tmp_path):
    import venv

    rw = tmp_path / "rw"
    rw.mkdir()
    secret = _secret_prefix(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "pyvenv.cfg").write_text("home = %s\n" % (secret / "bin"))
    outside = tmp_path / "outside"
    venv.create(outside, with_pip=False, symlinks=True)
    cfg = outside / "pyvenv.cfg"
    cfg.unlink()
    cfg.symlink_to(rw / "cfg-hop")
    (rw / "cfg-hop").symlink_to(decoy / "pyvenv.cfg")
    paths = policy_module.interpreter_read_paths(outside / "bin" / "python", [rw])
    assert str(secret) not in " ".join(paths)
    # With no writable grant involved the same file is honored.
    cfg.unlink()
    cfg.symlink_to(decoy / "pyvenv.cfg")
    assert str(secret) in " ".join(policy_module.interpreter_read_paths(outside / "bin" / "python", [rw]))


def test_a_read_only_grant_that_passes_through_a_writable_link_is_refused(tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (rw / "hop").symlink_to(secret)
    alias = tmp_path / "alias"
    alias.symlink_to(rw / "hop")
    with pytest.raises(PolicyError, match="through a symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[alias])
    # A plain link outside every writable tree is still fine.
    plain = tmp_path / "plain"
    plain.symlink_to(secret)
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[plain])
    assert str(secret) in policy.read_only


def test_a_runaway_link_chain_is_treated_as_steerable(tmp_path):
    loop_a, loop_b = tmp_path / "a", tmp_path / "b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    assert policy_module._trace(str(loop_a), ())[1] is True


def test_clean_env_inherits_nothing(monkeypatch):
    monkeypatch.setenv("BAZARR_TEST_SECRET", "hunter2")
    env = clean_env(home="/data/home", tmpdir="/data/tmp", extra={"TZ": "UTC"})
    assert "BAZARR_TEST_SECRET" not in env
    assert env["HOME"] == "/data/home" and env["TMPDIR"] == "/data/tmp" and env["TZ"] == "UTC"
    assert env["PYTHONNOUSERSITE"] == "1"
    with pytest.raises(PolicyError):
        clean_env(extra={"A": 1})
    with pytest.raises(PolicyError):
        clean_env(extra={"A=B": "x"})


def test_launch_spec_is_bounded(tmp_path):
    spec = make_spec(tmp_path, "pass")
    big = RuntimeSpec(**{**spec.__dict__, "env": {"BIG": "x" * (policy_module.MAX_SPEC_BYTES + 1)}})
    with pytest.raises(PolicyError, match="over the"):
        big.to_launch_json(enforce=True, min_abi=1)


def test_launcher_constants_match_policy_constants():
    spec = importlib.util.spec_from_file_location("launcher_under_test", spawn.LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.EXIT_SANDBOX_FAILED == policy_module.EXIT_SANDBOX_FAILED
    assert module.EXIT_BAD_SPEC == policy_module.EXIT_BAD_SPEC
    assert module.EXIT_EXEC_FAILED == policy_module.EXIT_EXEC_FAILED
    assert module.MESSAGE_PREFIX == policy_module.MESSAGE_PREFIX


@pytest.mark.parametrize(
    "official,interactive,strict,allowed,sandboxed,warn",
    [
        (True, False, False, True, False, False),
        (False, False, False, True, False, True),
        (False, False, True, False, False, False),
        (True, False, True, True, False, False),
        (True, True, False, False, False, False),
        (False, True, False, False, False, False),
    ],
)
def test_admission_without_a_sandbox(official, interactive, strict, allowed, sandboxed, warn):
    result = admission(sandbox_enforced=False, official=official, interactive_login=interactive, strict=strict)
    assert (result.allowed, result.sandboxed, result.warn_at_install) == (allowed, sandboxed, warn)
    assert bool(result.reason) is (not allowed)


def test_admission_with_a_sandbox_allows_everything():
    result = admission(sandbox_enforced=True, official=False, interactive_login=True, strict=True)
    assert result.allowed and result.sandboxed and not result.warn_at_install


# --- ABI masks -------------------------------------------------------------


def test_handled_rights_follow_the_abi():
    assert landlock.handled_fs_for_abi(1) == (1 << 13) - 1
    assert not landlock.handled_fs_for_abi(1) & landlock.FS_REFER
    assert landlock.handled_fs_for_abi(2) & landlock.FS_REFER
    assert not landlock.handled_fs_for_abi(2) & landlock.FS_TRUNCATE
    assert landlock.handled_fs_for_abi(3) & landlock.FS_TRUNCATE
    assert not landlock.handled_fs_for_abi(4) & landlock.FS_IOCTL_DEV
    assert landlock.handled_fs_for_abi(5) & landlock.FS_IOCTL_DEV
    assert not landlock.handled_fs_for_abi(8) & landlock.FS_RESOLVE_UNIX
    assert landlock.handled_fs_for_abi(9) & landlock.FS_RESOLVE_UNIX
    assert landlock.handled_net_for_abi(3) == 0
    assert landlock.handled_net_for_abi(4) == landlock.NET_BIND_TCP | landlock.NET_CONNECT_TCP
    assert landlock.handled_net_for_abi(10) == landlock.NET_BIND_TCP | landlock.NET_CONNECT_TCP
    assert landlock.scoped_for_abi(5) == 0
    assert landlock.scoped_for_abi(6) == landlock.SCOPE_SIGNAL | landlock.SCOPE_ABSTRACT_UNIX_SOCKET
    assert [landlock.attr_size_for_abi(a) for a in (1, 3, 4, 5, 6, 10)] == [8, 8, 16, 16, 24, 24]


def test_read_write_rights_never_execute_or_make_devices():
    assert not landlock.RIGHTS_READ_WRITE & landlock.FS_EXECUTE
    assert not landlock.RIGHTS_READ_WRITE & (landlock.FS_MAKE_CHAR | landlock.FS_MAKE_BLOCK)
    assert landlock.RIGHTS_READ_WRITE & landlock.FS_REFER


# --- Probe -----------------------------------------------------------------


@pytest.mark.parametrize(
    "err,fragment",
    [
        (errno.ENOSYS, "ENOSYS"),
        (errno.EOPNOTSUPP, "EOPNOTSUPP"),
        (errno.EPERM, "seccomp"),
        (errno.EINVAL, "EINVAL"),
    ],
)
def test_probe_abi_reports_none_when_the_syscall_fails(monkeypatch, err, fragment):
    monkeypatch.setattr(landlock, "_syscall", lambda *a: (-1, err))
    abi, reason = landlock.probe_abi()
    assert abi == 0 and fragment in reason


def test_probe_abi_reports_the_kernel_version(monkeypatch):
    seen = []

    def fake(number, *args):
        seen.append((number, args))
        return 7, 0

    monkeypatch.setattr(landlock, "_syscall", fake)
    assert landlock.probe_abi() == (7, "kernel reports ABI 7")
    number, args = seen[0]
    assert number == landlock.SYS_LANDLOCK_CREATE_RULESET
    assert args[0].value is None and args[1].value == 0
    assert args[2].value == landlock.LANDLOCK_CREATE_RULESET_VERSION


def test_probe_is_declared_when_the_kernel_has_no_landlock(monkeypatch):
    monkeypatch.setattr(landlock, "probe_abi", lambda: (0, "kernel has no Landlock (ENOSYS)"))
    result = spawn._run_probe(sys.executable)
    assert not result.enforced and result.abi == 0
    status = sandbox_status(result)
    assert status["filesystem"] == "declared" and status["network"] == "declared"
    assert status["label"] == "Not sandboxed on this host"
    assert "protect" not in status["label"].lower() and "secure" not in status["label"].lower()


def test_a_positive_abi_whose_canary_escapes_is_not_enforced(monkeypatch):
    monkeypatch.setattr(landlock, "probe_abi", lambda: (6, "kernel reports ABI 6"))

    class Escaped:
        returncode = 0

        def communicate(self, timeout=None):
            return json.dumps({"read_outside": "allowed", "write_outside": "denied", "write_inside": "allowed"}).encode(), b""

        def stop(self, grace=0):
            pass

    monkeypatch.setattr(spawn, "_start", lambda *a, **k: Escaped())
    result = spawn._run_probe(sys.executable)
    assert result.abi == 6 and not result.verified and not result.enforced
    assert "escaped" in result.reason


@linux_only
def test_a_canary_that_cannot_run_is_not_enforced(monkeypatch):
    monkeypatch.setattr(landlock, "probe_abi", lambda: (3, "kernel reports ABI 3"))
    result = spawn._run_probe("/nonexistent/python")
    assert not result.enforced
    assert "canary could not run" in result.reason


def test_probe_result_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(spawn, "_run_probe", lambda python: calls.append(python) or DECLARED)
    spawn.reset_probe_cache()
    try:
        assert probe_sandbox() is DECLARED
        assert probe_sandbox() is DECLARED
        assert len(calls) == 1
        probe_sandbox(refresh=True)
        assert len(calls) == 2
    finally:
        spawn.reset_probe_cache()


@linux_only
def test_live_probe_confines_the_canary(live):
    assert live.enforced and live.abi >= 1 and live.verified
    status = sandbox_status(live)
    assert status["filesystem"] == "enforced"
    assert status["workers_sandboxed"] is False
    assert status["label"] == "Sandbox available: Landlock ABI %d. Not active for plugin workers yet." % live.abi
    assert status["network"] == ("enforced" if live.abi >= 4 else "declared")
    assert status["signal_scope"] == ("enforced" if live.abi >= 6 else "declared")


# --- Rules applied (needs Landlock) -----------------------------------------


@linux_only
def test_writes_outside_the_allowed_paths_fail(live, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = tmp_path / "config.yaml"
    secret.write_text("secret: 1", encoding="utf-8")
    db = tmp_path / "bazarr.db"
    db.write_text("db", encoding="utf-8")
    script = """
        import json, os, sys
        out = {}
        def attempt(name, fn):
            try:
                fn(); out[name] = "allowed"
            except PermissionError:
                out[name] = "denied"
            except OSError as exc:
                out[name] = "error:" + str(exc.errno)
        def write(path):
            with open(path, "w") as h: h.write("x")
        def read(path):
            with open(path) as h: h.read()
        a = sys.argv
        attempt("write_inside", lambda: write("inside.txt"))
        attempt("write_outside", lambda: write(%(outside)r + "/new.txt"))
        attempt("read_config", lambda: read(%(secret)r))
        attempt("write_db", lambda: write(%(db)r))
        attempt("overwrite_config", lambda: write(%(secret)r))
        attempt("truncate_config", lambda: os.truncate(%(secret)r, 0))
        attempt("list_outside", lambda: os.listdir(%(outside)r))
        attempt("mkdir_outside", lambda: os.mkdir(%(outside)r + "/d"))
        attempt("read_etc_passwd", lambda: read("/etc/passwd"))
        attempt("read_usr", lambda: os.listdir("/usr"))
        attempt("dev_null", lambda: write("/dev/null"))
        attempt("home", lambda: os.listdir(os.path.expanduser("~")))
        print(json.dumps(out))
    """ % {"outside": str(outside), "secret": str(secret), "db": str(db)}
    rc, out, err = run(make_spec(tmp_path, script, ro=[]), live)
    assert rc == 0, err
    result = json.loads(out)
    assert result["write_inside"] == "allowed"
    assert result["read_etc_passwd"] == "allowed"
    assert result["read_usr"] == "allowed"
    assert result["dev_null"] == "allowed"
    assert result["home"] == "allowed"  # HOME is the plugin data dir
    for name in ("write_outside", "read_config", "write_db", "overwrite_config", "truncate_config", "list_outside", "mkdir_outside"):
        assert result[name] == "denied", (name, result)
    assert secret.read_text(encoding="utf-8") == "secret: 1"
    assert not (outside / "new.txt").exists()


@linux_only
def test_read_only_grants_cannot_be_written(live, tmp_path):
    ro = tmp_path / "bundle"
    ro.mkdir()
    (ro / "data.txt").write_text("keep", encoding="utf-8")
    script = """
        out = {}
        out["read"] = open(%(f)r).read()
        try:
            open(%(f)r, "w").close(); out["write"] = "allowed"
        except PermissionError:
            out["write"] = "denied"
        try:
            import os; os.unlink(%(f)r); out["unlink"] = "allowed"
        except PermissionError:
            out["unlink"] = "denied"
        import json; print(json.dumps(out))
    """ % {"f": str(ro / "data.txt")}
    rc, out, err = run(make_spec(tmp_path, script, ro=[ro]), live)
    assert rc == 0, err
    assert json.loads(out) == {"read": "keep", "write": "denied", "unlink": "denied"}
    assert (ro / "data.txt").read_text(encoding="utf-8") == "keep"


@linux_only
def test_rename_and_move_inside_the_data_dir_work_but_not_across_the_edge(live, tmp_path):
    need_abi(live, 2)
    # ABI 2 and above: renames inside the data dir work, moves across the edge
    # are denied. The ABI 1 behavior has its own test below.
    ro = tmp_path / "bundle"
    ro.mkdir()
    (ro / "keep.txt").write_text("keep", encoding="utf-8")
    script = """
        import json, os, shutil
        out = {}
        os.makedirs("a/b"); os.makedirs("c")
        open("a/b/f.txt", "w").write("1")
        os.rename("a/b/f.txt", "c/f.txt"); out["rename_across_dirs"] = "ok"
        shutil.move("c/f.txt", "a/g.txt"); out["move"] = os.path.exists("a/g.txt")
        try:
            shutil.move("a/g.txt", %(ro)r + "/g.txt"); out["move_out"] = "allowed"
        except PermissionError:
            out["move_out"] = "denied"
        try:
            os.rename(%(keep)r, "stolen.txt"); out["steal"] = "allowed"
        except (PermissionError, OSError) as exc:
            out["steal"] = "denied"
        print(json.dumps(out))
    """ % {"ro": str(ro), "keep": str(ro / "keep.txt")}
    rc, out, err = run(make_spec(tmp_path, script, ro=[ro]), live)
    assert rc == 0, err
    assert json.loads(out) == {"rename_across_dirs": "ok", "move": True, "move_out": "denied", "steal": "denied"}
    assert (ro / "keep.txt").exists() and not (ro / "g.txt").exists()


@linux_only
def test_abi_1_denies_cross_directory_renames_and_move_falls_back_to_copy(live, tmp_path):
    # A domain that does not handle REFER, which is every ABI 1 kernel and what
    # abi_cap=1 reproduces on a newer one, refuses cross-directory renames with
    # EXDEV, so shutil.move has to copy and delete. Its default copy_function
    # needs utime, which the metadata filter denies; copyfile does not.
    script = """
        import json, os, shutil
        os.makedirs("a"); os.makedirs("c")
        open("a/f", "w").write("1")
        out = {}
        try:
            os.rename("a/f", "c/f"); out["rename"] = 0
        except OSError as exc:
            out["rename"] = exc.errno
        out["same_dir_rename"] = os.rename("a/f", "a/g") is None
        try:
            shutil.move("a/g", "c/g")
            out["default_move"] = "ok"
        except PermissionError:
            out["default_move"] = "denied"  # copy2 wants utime
            os.unlink("c/g")
        shutil.move("a/g", "c/g", copy_function=shutil.copyfile)
        out["moved"] = os.path.exists("c/g") and not os.path.exists("a/g")
        out["content"] = open("c/g").read()
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script, abi_cap=1), live)
    assert rc == 0, err
    assert json.loads(out) == {
        "rename": errno.EXDEV, "same_dir_rename": True, "default_move": "denied", "moved": True, "content": "1",
    }


@linux_only
def test_tcp_connect_is_limited_to_the_port_set(live, tmp_path):
    need_abi(live, 4)
    import socket

    allowed = socket.socket()
    allowed.bind(("127.0.0.1", 0))
    allowed.listen(4)
    blocked = socket.socket()
    blocked.bind(("127.0.0.1", 0))
    blocked.listen(4)
    script = """
        import json, socket
        out = {}
        def connect(port):
            s = socket.socket(); s.settimeout(3)
            try:
                s.connect(("127.0.0.1", port)); return "allowed"
            except PermissionError:
                return "denied"
            finally:
                s.close()
        def bind():
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", 0)); return "allowed"
            except PermissionError:
                return "denied"
            finally:
                s.close()
        out["allowed_port"] = connect(%(a)d)
        out["other_port"] = connect(%(b)d)
        out["bind"] = bind()
        print(json.dumps(out))
    """ % {"a": allowed.getsockname()[1], "b": blocked.getsockname()[1]}
    try:
        rc, out, err = run(make_spec(tmp_path, script, ports=[allowed.getsockname()[1]]), live)
    finally:
        allowed.close()
        blocked.close()
    assert rc == 0, err
    assert json.loads(out) == {"allowed_port": "allowed", "other_port": "denied", "bind": "denied"}


@linux_only
def test_tcp_is_unrestricted_without_a_port_set(live, tmp_path):
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    script = """
        import socket
        s = socket.socket(); s.settimeout(3); s.connect(("127.0.0.1", %d)); print("connected")
    """ % listener.getsockname()[1]
    try:
        rc, out, err = run(make_spec(tmp_path, script), live)
    finally:
        listener.close()
    assert rc == 0, err
    assert out.strip() == "connected"


@linux_only
def test_udp_and_dns_stay_open_under_a_port_set(live, tmp_path):
    need_abi(live, 4)
    script = """
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("127.0.0.1", 9)); s.send(b"x"); print("udp ok")
    """
    rc, out, err = run(make_spec(tmp_path, script, ports=[443]), live)
    assert rc == 0, err
    assert out.strip() == "udp ok"


@linux_only
def test_signals_cannot_leave_the_domain_at_abi_6(live, tmp_path):
    need_abi(live, 6)
    script = """
        import os
        try:
            os.kill(os.getppid(), 0); print("allowed")
        except PermissionError:
            print("denied")
    """
    rc, out, err = run(make_spec(tmp_path, script), live)
    assert rc == 0, err
    assert out.strip() == "denied"


@linux_only
def test_abi_cap_limits_what_is_handled(live, tmp_path):
    # Pinning to ABI 3 drops the TCP rules: connecting to an unlisted port works.
    need_abi(live, 4)
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    script = """
        import socket
        s = socket.socket(); s.settimeout(3); s.connect(("127.0.0.1", %d)); print("connected")
    """ % listener.getsockname()[1]
    try:
        capped = make_spec(tmp_path, script, ports=[443], abi_cap=3)
        rc, out, err = run(capped, live)
    finally:
        listener.close()
    assert rc == 0, err
    assert out.strip() == "connected"
    # Filesystem rules still hold at the lower level.
    deny = """
        try:
            open(%r, "w"); print("allowed")
        except PermissionError:
            print("denied")
    """ % str(tmp_path / "nope.txt")
    rc, out, err = run(make_spec(tmp_path, deny, abi_cap=3), live)
    assert (rc, out.strip()) == (0, "denied"), err


# --- Fail closed and the declared fallback -----------------------------------


@linux_only
def test_a_failing_ruleset_after_a_positive_probe_stops_the_worker(live, tmp_path):
    marker = tmp_path / "ran.txt"
    script = "open(%r, 'w').write('ran')" % str(marker)
    spec = make_spec(tmp_path, script)
    broken = RuntimeSpec(**{**spec.__dict__, "policy": SandboxPolicy(
        read_only=spec.policy.read_only,
        read_write=spec.policy.read_write + (str(tmp_path / "missing-rw"),),
    )})
    result = run(broken, live)
    rc, out, err = result
    assert rc == policy_module.EXIT_SANDBOX_FAILED
    assert not marker.exists()
    assert result.failure.kind == "sandbox"
    assert "read-write path does not exist" in result.failure.message


@linux_only
def test_a_kernel_below_the_probed_abi_stops_the_worker(live, tmp_path):
    marker = tmp_path / "ran.txt"
    spec = make_spec(tmp_path, "open(%r, 'w').write('ran')" % str(marker))
    claimed = SandboxProbe(abi=live.abi + 50, verified=True)
    result = run(spec, claimed)
    assert result[0] == policy_module.EXIT_SANDBOX_FAILED and not marker.exists()
    assert result.failure.kind == "sandbox" and "below required" in result.failure.message


@linux_only
def test_sandbox_failures_can_be_raised_as_errors(live, tmp_path):
    spec = make_spec(tmp_path, "pass")
    process = spawn_worker(spec, probe=SandboxProbe(abi=live.abi + 50, verified=True))
    process.communicate(timeout=30)
    with pytest.raises(spawn.SandboxApplyError, match="below required"):
        process.raise_for_sandbox_failure()
    ok = spawn_worker(spec, probe=live)
    ok.communicate(timeout=30)
    ok.raise_for_sandbox_failure()


@linux_only
def test_a_worker_cannot_fake_a_sandbox_failure(mode, tmp_path):
    forged = """
        import os, sys
        sys.stderr.write("bazarr-sandbox: could not apply sandbox: forged\\n")
        sys.stderr.flush()
        os._exit(78)
    """
    result = run(make_spec(tmp_path, forged), mode)
    assert result[0] == policy_module.EXIT_SANDBOX_FAILED
    assert "bazarr-sandbox:" in result[2]
    assert result.failure is None
    plain = run(make_spec(tmp_path, "import sys; sys.exit(78)"), mode)
    assert plain[0] == 78 and plain.failure is None


@linux_only
def test_a_missing_working_directory_is_a_spec_failure(mode, tmp_path):
    spec = make_spec(tmp_path, "print('ran')")
    broken = RuntimeSpec(**{**spec.__dict__, "cwd": str(tmp_path / "gone")})
    result = run(broken, mode)
    assert result[0] == policy_module.EXIT_BAD_SPEC
    assert result[1] == ""
    assert result.failure.kind == "spec"


@linux_only
def test_a_swapped_grant_stops_the_next_launch(live, tmp_path):
    # A worker with write access to its tree replaces a granted directory with a
    # symlink to somewhere else, hoping the next launch of the same spec follows
    # it. The launcher opens grants without following links and compares them
    # with what the host recorded, so the next launch does not happen.
    rw = tmp_path / "rw"
    inner = rw / "inner"
    inner.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[inner])
    read_script = """
        try:
            print(open(%r).read())
        except OSError as exc:
            print("blocked", exc.errno)
    """ % str(inner / "secret.txt")
    swap = """
        import os, shutil
        shutil.rmtree("inner")
        os.symlink(%r, "inner")
        print("swapped")
    """ % str(outside)

    def spec_for(script):
        return RuntimeSpec(
            interpreter=sys.executable,
            args=("-I", "-c", textwrap.dedent(script)),
            cwd=str(rw),
            env=clean_env(home=rw, tmpdir=rw),
            policy=policy,
        )

    first = run(spec_for(swap), live)
    assert first[0] == 0 and first[1].strip() == "swapped", first[2]
    assert inner.is_symlink()
    second = run(spec_for(read_script), live)
    assert second[0] == policy_module.EXIT_SANDBOX_FAILED
    assert second[1] == ""
    assert second.failure.kind == "sandbox"
    assert "symlink" in second.failure.message or "changed" in second.failure.message
    assert "secret" not in second[1]


@linux_only
def test_a_replaced_grant_directory_stops_the_next_launch(live, tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[keep])
    spec = RuntimeSpec(
        interpreter=sys.executable,
        args=("-I", "-c", "print('ran')"),
        cwd=str(keep),
        env=clean_env(),
        policy=policy,
    )
    assert run(spec, live)[1].strip() == "ran"
    shutil.rmtree(keep)
    keep.mkdir()
    result = run(spec, live)
    assert result[0] == policy_module.EXIT_BAD_SPEC or result.failure is not None
    assert result[1] == ""


@linux_only
def test_declared_mode_runs_without_a_ruleset(tmp_path):
    outside = tmp_path / "outside.txt"
    script = "open(%r, 'w').write('x'); print('wrote')" % str(outside)
    process = spawn_worker(make_spec(tmp_path, script), probe=DECLARED)
    assert process.enforcement == "declared"
    out, err = process.communicate(timeout=30)
    assert process.returncode == 0, err
    assert out.decode().strip() == "wrote" and outside.exists()


@linux_only
def test_require_sandbox_refuses_a_declared_host(tmp_path):
    with pytest.raises(SandboxRefused):
        spawn_worker(make_spec(tmp_path, "pass"), probe=DECLARED, require_sandbox=True)


@linux_only
def test_enforced_workers_report_their_enforcement(live, tmp_path):
    process = spawn_worker(make_spec(tmp_path, "pass"), probe=live)
    process.communicate(timeout=30)
    assert process.enforcement == "enforced"


# --- Limits, environment, process handling (any Linux) ------------------------


@pytest.fixture(params=["declared", "enforced"])
def mode(request):
    if request.param == "declared":
        return DECLARED
    return request.getfixturevalue("live")


@linux_only
def test_rlimits_priority_and_umask_are_applied(mode, tmp_path):
    script = """
        import json, os, resource
        print(json.dumps({
            "core": resource.getrlimit(resource.RLIMIT_CORE),
            "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
            "fsize": resource.getrlimit(resource.RLIMIT_FSIZE),
            "nice": os.getpriority(os.PRIO_PROCESS, 0),
            "oom": int(open("/proc/self/oom_score_adj").read()),
            "umask": os.umask(0),
            "nnp": [l for l in open("/proc/self/status") if l.startswith("NoNewPrivs")][0].split()[1],
        }))
    """
    limits = Limits(nofile=321, fsize=123456)
    rc, out, err = run(make_spec(tmp_path, script, limits=limits), mode)
    assert rc == 0, err
    result = json.loads(out)
    assert result["core"] == [0, 0]
    assert result["nofile"] == [321, 321]
    assert result["fsize"] == [123456, 123456]
    assert result["nice"] >= 10
    assert result["oom"] >= 800
    assert result["umask"] == 0o077
    assert result["nnp"] == "1"


@linux_only
def test_the_file_size_limit_is_enforced(mode, tmp_path):
    script = """
        import signal
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        try:
            with open("big.bin", "wb") as h:
                h.write(b"x" * 100000)
            print("wrote")
        except OSError as exc:
            print("blocked", exc.errno)
    """
    rc, out, err = run(make_spec(tmp_path, script, limits=Limits(fsize=4096)), mode)
    assert rc == 0, err
    assert out.strip().startswith("blocked")


@linux_only
def test_the_worker_environment_is_exactly_the_spec(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("BAZARR_TEST_SECRET", "hunter2")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pass@proxy:3128")
    script = "import json, os; print(json.dumps(dict(os.environ)))"
    spec = make_spec(tmp_path, script, env_extra={"TZ": "UTC"})
    rc, out, err = run(spec, mode)
    assert rc == 0, err
    seen = json.loads(out)
    assert "BAZARR_TEST_SECRET" not in seen and "HTTPS_PROXY" not in seen
    assert "hunter2" not in out
    assert set(seen) - set(spec.env) <= {"LC_CTYPE"}
    assert seen["TZ"] == "UTC" and seen["HOME"] == spec.env["HOME"]


@linux_only
def test_the_launcher_gets_an_empty_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BAZARR_TEST_SECRET", "hunter2")
    script = """
        import json, os
        try:
            seen = open("/proc/%d/environ" % os.getppid()).read()
        except PermissionError:
            # The launcher is closed to its own user's processes: even better than empty.
            seen = ""
        print(json.dumps(seen))
    """
    rc, out, err = run(make_spec(tmp_path, script), DECLARED)
    assert rc == 0, err
    assert json.loads(out) == ""


@linux_only
def test_the_host_never_forks_or_runs_code_between_fork_and_exec(tmp_path, monkeypatch):
    forks = []
    monkeypatch.setattr(os, "fork", lambda: forks.append(1) or (_ for _ in ()).throw(AssertionError("host forked")))
    seen = {}
    real_popen = subprocess.Popen.__init__

    def spy(self, args, **kwargs):
        seen["args"] = list(args)
        seen["kwargs"] = kwargs
        return real_popen(self, args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    rc, out, err = run(make_spec(tmp_path, "print('hi')"), DECLARED)
    assert rc == 0, err
    assert not forks
    assert seen["kwargs"].get("preexec_fn") is None
    assert seen["kwargs"]["start_new_session"] is True and seen["kwargs"]["close_fds"] is True
    assert seen["kwargs"]["env"] == {}
    assert seen["args"][1:4] == ["-I", "-S", "-B"]
    assert seen["args"][4] == str(spawn.LAUNCHER_PATH)
    with pytest.raises(TypeError, match="preexec_fn"):
        spawn_worker(make_spec(tmp_path, "pass"), probe=DECLARED, preexec_fn=lambda: None)


@linux_only
def test_the_launcher_is_single_threaded_and_is_the_workers_parent(tmp_path):
    script = """
        import json, os
        ppid = os.getppid()
        status = dict(l.split(":", 1) for l in open("/proc/%d/status" % ppid).read().splitlines() if ":" in l)
        cmdline = open("/proc/%d/cmdline" % ppid, "rb").read().split(b"\\0")
        print(json.dumps({"threads": status["Threads"].strip(), "flags": [c.decode() for c in cmdline[1:4]]}))
    """
    process = spawn_worker(make_spec(tmp_path, script), probe=DECLARED)
    out, err = process.communicate(timeout=30)
    assert process.returncode == 0, err
    result = json.loads(out)
    assert result["threads"] == "1"
    assert result["flags"] == ["-I", "-S", "-B"]


@linux_only
def test_the_exit_code_of_the_worker_comes_through(mode, tmp_path):
    rc, _out, _err = run(make_spec(tmp_path, "import sys; sys.exit(7)"), mode)
    assert rc == 7
    rc, _out, _err = run(make_spec(tmp_path, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"), mode)
    assert rc == 128 + 9


@linux_only
def test_an_exec_failure_is_reported_apart_from_a_sandbox_failure(tmp_path):
    spec = make_spec(tmp_path, "pass")
    broken = RuntimeSpec(**{**spec.__dict__, "interpreter": str(tmp_path / "no-such-python")})
    result = run(broken, DECLARED)
    assert result[0] == policy_module.EXIT_EXEC_FAILED
    assert result.failure.kind == "exec"


GRANDCHILD = """
    import os, subprocess, sys, time
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import os, time; open('child.pid', 'w').write(str(os.getpid())); time.sleep(300)"],
        start_new_session=True,
    )
    while not os.path.exists("child.pid") or not open("child.pid").read():
        time.sleep(0.02)
    open("worker.pid", "w").write(str(os.getpid()))
    %s
"""


def read_pid(path, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            text = Path(path).read_text(encoding="utf-8")
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("no pid in %s" % path)


@linux_only
def test_a_setsid_child_dies_with_the_worker(mode, tmp_path):
    spec = make_spec(tmp_path, GRANDCHILD % "sys.exit(0)")
    process = spawn_worker(spec, probe=mode)
    try:
        process.communicate(timeout=30)
        child = read_pid(tmp_path / "rw" / "child.pid")
        assert wait_dead(child), "a setsid child outlived its worker"
    finally:
        process.stop(0.5)


@linux_only
def test_stop_kills_the_whole_tree_including_setsid_children(mode, tmp_path):
    spec = make_spec(tmp_path, GRANDCHILD % "time.sleep(300)")
    process = spawn_worker(spec, probe=mode)
    child = read_pid(tmp_path / "rw" / "child.pid")
    worker = read_pid(tmp_path / "rw" / "worker.pid")
    assert alive(child) and alive(worker)
    process.stop(1.0)
    assert process.poll() is not None
    assert wait_dead(worker) and wait_dead(child)


STUBBORN = """
    import os, signal, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            open("grand.pid", "w").write(str(os.getpid()))
            time.sleep(300)
        os._exit(0)
    while not os.path.exists("grand.pid") or not open("grand.pid").read():
        time.sleep(0.02)
    open("worker.pid", "w").write(str(os.getpid()))
    %s
"""


@linux_only
@pytest.mark.parametrize("worker_ending", ["time.sleep(300)", "os.kill(os.getpid(), signal.SIGSTOP)"], ids=["ignores-sigterm", "stopped"])
def test_stop_with_the_default_grace_leaves_no_descendant_and_lets_the_launcher_finish(mode, tmp_path, worker_ending):
    spec = make_spec(tmp_path, STUBBORN % worker_ending)
    spec = dataclasses.replace(spec, term_grace=3.0)
    process = spawn_worker(spec, probe=mode)
    grand = read_pid(tmp_path / "rw" / "grand.pid")
    worker = read_pid(tmp_path / "rw" / "worker.pid")
    assert alive(grand) and alive(worker)
    started = time.monotonic()
    process.stop(0.1)
    elapsed = time.monotonic() - started
    assert process.poll() is not None
    # The launcher ran its own cleanup and reported the worker's death; it was not killed itself.
    assert process.returncode == 128 + 9, process.returncode
    assert wait_dead(worker) and wait_dead(grand), "a descendant outlived stop()"
    assert elapsed < 8, elapsed


HIDDEN = """
    import ctypes, os, signal, sys, time

    def hide():
        os.setsid()
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.fork() == 0:
        hide()
        if os.fork() == 0:
            hide()
            if os.fork() == 0:
                hide()
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                open("grand.pid", "w").write(str(os.getpid()))
                %s
            os._exit(0)
        os.kill(os.getpid(), signal.SIGSTOP)
    while not os.path.exists("grand.pid") or not open("grand.pid").read():
        time.sleep(0.02)
    open("worker.pid", "w").write(str(os.getpid()))
    time.sleep(300)
"""


@linux_only
@pytest.mark.parametrize("grand_ending", ["time.sleep(300)", "os.kill(os.getpid(), signal.SIGSTOP)"], ids=["running", "stopped"])
def test_stop_leaves_no_undumpable_setsid_double_forked_descendant(mode, tmp_path, grand_ending):
    spec = make_spec(tmp_path, HIDDEN % grand_ending)
    process = spawn_worker(spec, probe=mode)
    grand = read_pid(tmp_path / "rw" / "grand.pid")
    worker = read_pid(tmp_path / "rw" / "worker.pid")
    assert alive(grand) and alive(worker)
    process.stop(0.2)
    assert process.poll() is not None
    assert wait_dead(worker) and wait_dead(grand), "an undumpable descendant outlived stop()"


@linux_only
def test_stop_wakes_a_stopped_launcher_so_it_can_clean_up(mode, tmp_path):
    spec = make_spec(tmp_path, GRANDCHILD % "time.sleep(300)")
    process = spawn_worker(spec, probe=mode)
    child = read_pid(tmp_path / "rw" / "child.pid")
    worker = read_pid(tmp_path / "rw" / "worker.pid")
    os.kill(process.pid, signal.SIGSTOP)
    process.stop(0.2)
    assert process.poll() is not None
    assert wait_dead(worker) and wait_dead(child)


def test_stop_waits_for_the_launchers_grace_and_cleanup_before_killing_the_group(tmp_path, monkeypatch):
    # A launcher that outlasts the caller's grace gets the lifeline closed and then its
    # own term_grace plus the cleanup allowance, not a fixed two seconds.
    waits = []
    killed = []

    class Stub(spawn.LauncherProcess):
        def __init__(self):
            self._own_lock = threading.RLock()
            self._stop_running = None
            self._fds = {"lifeline": None, "status": None}
            self.pid = 4242
            self.term_grace = 3.0
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def _still_ours(self):
            return self.returncode is None

        def __del__(self):
            pass

    stub = Stub()

    def fake_wait(self, timeout=None):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("launcher", timeout)

    monkeypatch.setattr(spawn.WorkerProcess, "wait", fake_wait)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    stub.stop(0.1)
    assert waits[0] == 0.1
    assert waits[1] == 3.0 + spawn.LAUNCHER_CLEANUP_SECONDS
    assert killed == [(4242, signal.SIGKILL)]


@linux_only
def test_the_tree_dies_when_the_host_goes_away(mode, tmp_path):
    spec = make_spec(tmp_path, GRANDCHILD % "time.sleep(300)")
    process = spawn_worker(spec, probe=mode)
    child = read_pid(tmp_path / "rw" / "child.pid")
    worker = read_pid(tmp_path / "rw" / "worker.pid")
    # What the kernel does to the lifeline when the host dies.
    process.close_lifeline()
    assert wait_dead(worker) and wait_dead(child)
    assert process.wait(timeout=10) is not None


@linux_only
def test_a_host_killed_with_sigkill_takes_its_workers_along(tmp_path):
    host = tmp_path / "host.py"
    host.write_text(
        textwrap.dedent(
            """
            import os, sys, time
            sys.path.insert(0, %(root)r)
            from provider_hub.sandbox import *
            from provider_hub.sandbox import SandboxProbe
            policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[%(rw)r])
            spec = RuntimeSpec(
                interpreter=sys.executable,
                args=("-I", "-c", "import os, time; open('worker.pid', 'w').write(str(os.getpid())); time.sleep(300)"),
                cwd=%(rw)r, env=clean_env(), policy=policy,
            )
            keep = spawn_worker(spec, probe=SandboxProbe(abi=0, verified=False))
            time.sleep(300)
            """
        )
        % {"root": str(Path(spawn.__file__).parents[2]), "rw": str(tmp_path / "rw")},
        encoding="utf-8",
    )
    (tmp_path / "rw").mkdir()
    proc = subprocess.Popen([sys.executable, "-I", str(host)])
    try:
        worker = read_pid(tmp_path / "rw" / "worker.pid")
        assert alive(worker)
        proc.kill()
        proc.wait(timeout=10)
        assert wait_dead(worker), "worker survived the death of its host"
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_dropping_the_process_object_closes_the_lifeline_and_ends_the_tree(mode, tmp_path):
    spec = make_spec(tmp_path, GRANDCHILD % "time.sleep(300)")
    process = spawn_worker(spec, probe=mode)
    child = read_pid(tmp_path / "rw" / "child.pid")
    worker = read_pid(tmp_path / "rw" / "worker.pid")
    launcher = process.pid
    del process
    gc.collect()
    assert wait_dead(worker) and wait_dead(child) and wait_dead(launcher)


def test_status_lists_what_is_not_covered():
    full = sandbox_status(SandboxProbe(abi=10, verified=True, network_verified=True, signal_scope_verified=True))
    assert not any("UNIX" in n or "signal" in n.lower() or "Network" in n for n in full["limitations"])
    assert any("listening TCP" in n for n in full["limitations"])
    mid = sandbox_status(SandboxProbe(abi=5, verified=True))
    notes = " ".join(mid["limitations"])
    assert "pathname UNIX sockets" in notes and "launcher included" in notes and "Network connections" in notes
    none = sandbox_status(DECLARED)
    assert none["label"] == "Not sandboxed on this host"
    assert len(none["limitations"]) == 1 and "nothing confines" in none["limitations"][0]


@linux_only
def test_the_launch_spec_never_blocks_on_a_small_pipe(tmp_path):
    # The spec travels in a memfd, so a spec larger than a pipe buffer starts fine.
    extra = {"BIG%d" % i: "x" * 30000 for i in range(5)}
    spec = make_spec(tmp_path, "import os; print(sum(len(v) for k, v in os.environ.items() if k.startswith('BIG')))", env_extra=extra)
    rc, out, err = run(spec, DECLARED)
    assert rc == 0, err
    assert out.strip() == "150000"


def test_the_status_payload_carries_the_sandbox_state(monkeypatch):
    import provider_hub.sandbox as package
    from test_build_stamp import _status_payload

    monkeypatch.setattr(package, "sandbox_status_nowait", lambda: sandbox_status(DECLARED))
    data = _status_payload(monkeypatch, {})
    assert data["sandbox"]["label"] == "Not sandboxed on this host"
    assert data["sandbox"]["enforced"] is False
    assert data["sandbox"]["pending"] is False

    def broken():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(package, "sandbox_status_nowait", broken)
    assert "sandbox" not in _status_payload(monkeypatch, {})


@pytest.fixture
def slow_probe(monkeypatch):
    """A probe that takes as long as the test lets it, with a clean cache around it."""
    import threading

    gate = threading.Event()
    calls = []

    def slow(python):
        calls.append(python)
        gate.wait(30)
        return SandboxProbe(abi=0, verified=False, reason="test: slow probe finished")

    spawn.reset_probe_cache()
    spawn._probe_thread = None
    monkeypatch.setattr(spawn, "_run_probe", slow)
    yield gate, calls
    gate.set()
    thread = spawn._probe_thread
    if thread is not None:
        thread.join(5)
    spawn._probe_thread = None
    spawn.reset_probe_cache()


def test_the_status_never_runs_the_probe_and_reports_pending_until_it_is_cached(slow_probe):
    gate, calls = slow_probe
    assert spawn.start_probe_in_background() is True
    assert spawn.start_probe_in_background() is False  # one probe at a time
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    # The probe now holds its lock for as long as the gate stays shut. A status
    # request must neither wait for it nor start a second probe.
    started = time.monotonic()
    first = spawn.sandbox_status_nowait()
    second = spawn.sandbox_status_nowait()
    assert time.monotonic() - started < 1.0
    assert len(calls) == 1
    for payload in (first, second):
        assert payload["pending"] is True
        assert payload["enforced"] is False
        assert payload["workers_sandboxed"] is False
        assert payload["label"] == "Checking the worker sandbox"
    gate.set()
    spawn._probe_thread.join(5)
    done = spawn.sandbox_status_nowait()
    assert done["pending"] is False
    assert done["reason"] == "test: slow probe finished"
    assert spawn.start_probe_in_background() is False  # cached already


def test_a_status_request_starts_the_probe_when_boot_did_not(slow_probe):
    gate, calls = slow_probe
    started = time.monotonic()
    assert spawn.sandbox_status_nowait()["pending"] is True
    assert time.monotonic() - started < 1.0
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1


def test_the_status_endpoint_answers_while_the_probe_is_still_running(monkeypatch, slow_probe):
    from test_build_stamp import _status_payload

    gate, calls = slow_probe
    spawn.start_probe_in_background()
    started = time.monotonic()
    data = _status_payload(monkeypatch, {})
    assert time.monotonic() - started < 2.0
    assert data["sandbox"]["pending"] is True


def test_a_probe_that_raises_is_cached_as_not_enforced(monkeypatch):
    def boom(python):
        raise RuntimeError("canary exploded")

    spawn.reset_probe_cache()
    spawn._probe_thread = None
    monkeypatch.setattr(spawn, "_run_probe", boom)
    try:
        assert spawn.start_probe_in_background() is True
        spawn._probe_thread.join(5)
        status = spawn.sandbox_status_nowait()
        assert status["pending"] is False
        assert status["enforced"] is False
        assert "canary exploded" in status["reason"]
    finally:
        spawn._probe_thread = None
        spawn.reset_probe_cache()


# --- Metadata filter ---------------------------------------------------------


def run_bpf(program, arch, number, args=()):
    """Evaluate a classic BPF program on a synthetic seccomp_data."""
    insns = [struct.unpack_from("=HBBI", program, offset) for offset in range(0, len(program), 8)]
    data = {0: number, 4: arch}
    for index in range(6):
        value = args[index] if index < len(args) else 0
        data[16 + 8 * index] = value & 0xFFFFFFFF
        data[20 + 8 * index] = value >> 32
    pc = acc = 0
    for _ in range(1000):
        code, jt, jf, k = insns[pc]
        if code == 0x20:
            acc = data[k]
            pc += 1
        elif code == 0x15:
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x45:
            pc += 1 + (jt if acc & k else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError("unexpected opcode %#x" % code)
    raise AssertionError("filter does not terminate")


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_filter_denies_exactly_the_listed_calls(arch):
    program = seccomp.build_filter(arch)
    assert len(program) % 8 == 0
    eperm = seccomp.SECCOMP_RET_ERRNO | errno.EPERM
    denied = set(seccomp.DENIED_SYSCALLS[arch].values())
    for number in range(0, 600):
        expected = eperm if number in denied else seccomp.SECCOMP_RET_ALLOW
        assert run_bpf(program, arch, number) == expected, number


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_filter_kills_calls_from_another_syscall_abi(arch):
    program = seccomp.build_filter(arch)
    for other in (0x40000003, 0xC0000028, 0x40000028, arch ^ 1):  # i386, ppc64le, arm, nonsense
        for number in (0, 90, 15, 5):
            assert run_bpf(program, other, number) == seccomp.SECCOMP_RET_KILL_PROCESS
    if arch == seccomp.AUDIT_ARCH_X86_64:
        # The x32 ABI reuses the arch but ORs a bit into the number, so chmod
        # would otherwise slip through as an unknown call.
        assert run_bpf(program, arch, seccomp.X32_SYSCALL_BIT | 90) == seccomp.SECCOMP_RET_KILL_PROCESS


def test_filter_lists_the_dangerous_calls():
    names = set(seccomp.DENIED_SYSCALLS[seccomp.AUDIT_ARCH_X86_64])
    assert {"chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat"} <= names
    assert {"setxattr", "lsetxattr", "fsetxattr", "removexattr", "lremovexattr", "fremovexattr"} <= names
    assert {"utime", "utimes", "utimensat", "futimesat"} <= names
    arm = set(seccomp.DENIED_SYSCALLS[seccomp.AUDIT_ARCH_AARCH64])
    assert {"fchmod", "fchmodat", "fchmodat2", "fchownat", "fchown", "utimensat", "setxattr"} <= arm
    with pytest.raises(ValueError):
        seccomp.build_filter(0x40000003)


METADATA_SCRIPT = """
    import errno, json, os
    out = {}
    def attempt(name, fn):
        try:
            fn(); out[name] = "allowed"
        except OSError as exc:
            out[name] = errno.errorcode.get(exc.errno, exc.errno)
    inside = "inside.txt"
    open(inside, "w").close()
    outside = %(outside)r
    for label, path in (("outside", outside), ("inside", inside)):
        attempt("chmod_" + label, lambda p=path: os.chmod(p, 0))
        attempt("chown_" + label, lambda p=path: os.chown(p, -1, -1))
        attempt("lchown_" + label, lambda p=path: os.lchown(p, -1, -1))
        attempt("utime_" + label, lambda p=path: os.utime(p, None))
        attempt("setxattr_" + label, lambda p=path: os.setxattr(p, "user.x", b"1"))
        attempt("removexattr_" + label, lambda p=path: os.removexattr(p, "user.x"))
    fd = os.open(inside, os.O_RDONLY)
    attempt("fchmod", lambda: os.fchmod(fd, 0))
    attempt("fchown", lambda: os.fchown(fd, -1, -1))
    attempt("futime", lambda: os.utime(fd, None))
    dirfd = os.open(".", os.O_RDONLY)
    attempt("fchmodat", lambda: os.chmod(inside, 0, dir_fd=dirfd))
    attempt("fchownat", lambda: os.chown(inside, -1, -1, dir_fd=dirfd))
    attempt("utimensat", lambda: os.utime(inside, None, dir_fd=dirfd))
    print(json.dumps(out))
"""


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_metadata_changes_fail_with_eperm_inside_and_outside_the_grants(mode, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    outside.chmod(0o644)
    before = outside.stat()
    rc, out, err = run(make_spec(tmp_path, METADATA_SCRIPT % {"outside": str(outside)}), mode)
    assert rc == 0, err
    result = json.loads(out)
    assert result and set(result.values()) == {"EPERM"}, result
    after = outside.stat()
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)


# --- Capabilities -------------------------------------------------------------


class FakeKernel:
    """Just enough of libc's prctl and syscall to follow the capability drop."""

    CAPS = 41  # capabilities 0..40, like a current kernel

    def __init__(self, held=(), setpcap=True, ambient=(), fail=None, ambient_supported=True):
        full = (1 << self.CAPS) - 1
        self.effective = self.permitted = (1 << 8 if setpcap else 0) | sum(1 << c for c in held)
        self.inheritable = self.permitted
        self.bounding = full
        self.ambient = sum(1 << c for c in ambient)
        self.securebits = 0
        self.fail = fail
        self.ambient_supported = ambient_supported
        self.calls = []
        self.path = self.lie = None

    def prctl(self, option, a2=None, *rest):
        option = option.value
        arg = a2.value if a2 is not None else 0
        self.calls.append(("prctl", option, arg))
        ctypes_module = capabilities.ctypes
        if self.fail == option:
            ctypes_module.set_errno(errno.EPERM)
            return -1
        if option == capabilities.PR_SET_SECUREBITS:
            self.securebits = arg
            return 0
        if option == capabilities.PR_CAP_AMBIENT:
            if not self.ambient_supported:
                ctypes_module.set_errno(errno.EINVAL)
                return -1
            self.ambient = 0
            return 0
        if option == capabilities.PR_CAPBSET_READ:
            if arg >= self.CAPS:
                ctypes_module.set_errno(errno.EINVAL)
                return -1
            return (self.bounding >> arg) & 1
        if option == capabilities.PR_CAPBSET_DROP:
            self.bounding &= ~(1 << arg)
            return 0
        raise AssertionError("unexpected prctl %d" % option)

    def syscall(self, number, header, data):
        ctypes_module = capabilities.ctypes
        self.calls.append(("syscall", number.value))
        words = ctypes_module.cast(data, ctypes_module.POINTER(capabilities._Data * 2)).contents
        if number.value == 125:
            for name in ("effective", "permitted", "inheritable"):
                value = getattr(self, name)
                setattr(words[0], name, value & 0xFFFFFFFF)
                setattr(words[1], name, value >> 32)
            return 0
        if self.fail == "capset":
            ctypes_module.set_errno(errno.EPERM)
            return -1
        for name in ("effective", "permitted", "inheritable"):
            setattr(self, name, getattr(words[0], name) | (getattr(words[1], name) << 32))
        if self.path is not None:
            self.render()
        return 0

    def status(self, tmp_path, lie=None):
        """A status file that follows the kernel state; ``lie`` overrides fields after every change."""
        self.path, self.lie = tmp_path / "status", lie
        self.render()
        return str(self.path)

    def render(self):
        fields = {
            "CapInh": self.inheritable, "CapPrm": self.permitted, "CapEff": self.effective,
            "CapBnd": self.bounding, "CapAmb": self.ambient, **(self.lie or {}),
        }
        self.path.write_text("Name:\tx\n" + "".join("%s:\t%016x\n" % item for item in fields.items()), encoding="ascii")


@pytest.fixture
def kernel(monkeypatch):
    def install(**kwargs):
        fake = FakeKernel(**kwargs)
        monkeypatch.setattr(capabilities, "_get_libc", lambda: fake)
        monkeypatch.setattr(capabilities, "_syscall_numbers", lambda: (125, 126))
        return fake

    return install


def test_the_drop_locks_securebits_clears_ambient_empties_bounding_and_capset(kernel, tmp_path):
    fake = kernel(held=(21, 0, 36), ambient=(21,))
    result = capabilities.drop_all(fake.status(tmp_path))
    assert result == {"bounding_dropped": True}
    assert fake.securebits == capabilities.LOCKED_SECUREBITS
    assert capabilities.LOCKED_SECUREBITS == 0b11101111
    assert (fake.effective, fake.permitted, fake.inheritable, fake.ambient, fake.bounding) == (0, 0, 0, 0, 0)
    order = [(kind, what) for kind, what, *_ in fake.calls if (kind, what) != ("prctl", capabilities.PR_CAPBSET_READ)]
    firsts = [pair for index, pair in enumerate(order) if pair not in order[:index]]
    assert firsts == [
        ("syscall", 125),
        ("prctl", capabilities.PR_SET_SECUREBITS),
        ("prctl", capabilities.PR_CAP_AMBIENT),
        ("prctl", capabilities.PR_CAPBSET_DROP),
        ("syscall", 126),
    ]
    dropped = [arg for kind, what, arg in (c for c in fake.calls if c[0] == "prctl") if what == capabilities.PR_CAPBSET_DROP]
    assert dropped == list(range(FakeKernel.CAPS))


def test_without_setpcap_the_bounding_set_and_securebits_are_left_alone(kernel, tmp_path):
    fake = kernel(setpcap=False)
    result = capabilities.drop_all(fake.status(tmp_path))
    assert result == {"bounding_dropped": False}
    assert fake.securebits == 0 and fake.bounding == (1 << FakeKernel.CAPS) - 1
    assert (fake.effective, fake.permitted, fake.inheritable, fake.ambient) == (0, 0, 0, 0)
    options = {what for kind, what, *_ in fake.calls if kind == "prctl"}
    assert options == {capabilities.PR_CAP_AMBIENT}


def test_a_kernel_without_ambient_capabilities_is_not_an_error(kernel, tmp_path):
    fake = kernel(ambient_supported=False)
    assert capabilities.drop_all(fake.status(tmp_path)) == {"bounding_dropped": True}


@pytest.mark.parametrize(
    "failing",
    [capabilities.PR_SET_SECUREBITS, capabilities.PR_CAP_AMBIENT, capabilities.PR_CAPBSET_DROP, "capset"],
)
def test_a_step_that_fails_aborts_the_drop(kernel, tmp_path, failing):
    fake = kernel(held=(21,), fail=failing)
    with pytest.raises(OSError):
        capabilities.drop_all(fake.status(tmp_path))


@pytest.mark.parametrize(
    "lie",
    [{"CapEff": 1}, {"CapPrm": 1 << 21}, {"CapAmb": 1 << 21}, {"CapInh": 1}, {"CapBnd": 1 << 3}],
)
def test_a_status_that_still_shows_capabilities_is_an_error(kernel, tmp_path, lie):
    fake = kernel()
    with pytest.raises(OSError, match="still|missing"):
        capabilities.drop_all(fake.status(tmp_path, lie))


def test_a_status_without_the_capability_lines_cannot_confirm_the_drop(kernel, tmp_path):
    kernel()
    path = tmp_path / "status"
    path.write_text("Name:\tx\n", encoding="ascii")
    with pytest.raises(OSError, match="missing"):
        capabilities.drop_all(str(path))


def test_the_unprivileged_bounding_set_is_not_demanded_empty(kernel, tmp_path):
    # An unprivileged user keeps a full bounding set; only the usable sets matter.
    fake = kernel(setpcap=False)
    assert capabilities.drop_all(fake.status(tmp_path))["bounding_dropped"] is False


def test_a_launcher_child_drops_capabilities_before_the_filter_and_fails_closed(monkeypatch):
    import types

    file_spec = importlib.util.spec_from_file_location("launcher_under_test", spawn.LAUNCHER_PATH)
    launcher = importlib.util.module_from_spec(file_spec)
    file_spec.loader.exec_module(launcher)
    # Nothing here may touch the test process: no signal handlers, no limits.
    monkeypatch.setattr(
        launcher,
        "signal",
        types.SimpleNamespace(
            SIGKILL=9, SIGTERM=15, SIGINT=2, SIGHUP=1, SIGCHLD=17, SIG_DFL=0,
            signal=lambda *a: None, set_wakeup_fd=lambda *a: None,
        ),
    )
    monkeypatch.setattr(launcher, "_apply_limits", lambda limits: None)
    order = []

    class Boom(Exception):
        pass

    class FakeCaps:
        @staticmethod
        def drop_all():
            order.append("caps")
            raise OSError(errno.EPERM, "nope")

    class FakeSeccomp:
        @staticmethod
        def apply_filter():
            order.append("filter")

    class FakeLandlock:
        PR_SET_PDEATHSIG = PR_SET_NO_NEW_PRIVS = PR_SET_DUMPABLE = 1

        @staticmethod
        def prctl(*args):
            return 0

    reports = []
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(os, "getppid", lambda: 4242)

    def fake_exit(code):
        reports.append(code)
        raise Boom()

    monkeypatch.setattr(os, "_exit", fake_exit)
    spec = {"interpreter": "/bin/true", "args": [], "cwd": "/", "env": {}, "mode": "enforce"}
    try:
        with pytest.raises(Boom):
            launcher._child(spec, FakeLandlock, FakeSeccomp, FakeCaps, 4242, (), write_fd)
        os.close(write_fd)
        record, cleanup = status_records(os.read(read_fd, 4096))
    finally:
        os.close(read_fd)
    assert cleanup is None, "the child reports a failure; only the launcher reports the cleanup"
    assert order == ["caps"], "the filter must not go on after a failed drop"
    assert reports == [launcher.EXIT_SANDBOX_FAILED]
    assert record["kind"] == "sandbox" and "nope" in record["message"]


CAPS_SCRIPT = """
    import json
    out = {}
    with open("/proc/self/status") as handle:
        for line in handle:
            name, _, value = line.partition(":")
            if name in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
                out[name] = int(value.strip(), 16)
    print(json.dumps(out))
"""


@linux_only
def test_a_worker_holds_no_capabilities(mode, tmp_path):
    rc, out, err = run(make_spec(tmp_path, CAPS_SCRIPT), mode)
    assert rc == 0, err
    result = json.loads(out)
    assert result["CapEff"] == 0 and result["CapPrm"] == 0 and result["CapInh"] == 0
    assert result.get("CapAmb", 0) == 0


@linux_only
def test_the_live_probe_reports_the_capabilities_as_dropped(live):
    assert live.capabilities_dropped
    assert sandbox_status(live)["capabilities"] == "dropped"
    assert sandbox_status(DECLARED)["capabilities"] == "declared"
    assert sandbox_status(SandboxProbe(abi=6, verified=True))["capabilities"] == "declared"


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_ordinary_writes_still_work_and_modes_come_from_the_umask(mode, tmp_path):
    script = """
        import json, os, shutil, stat
        out = {}
        with open("a.txt", "w") as handle:
            handle.write("hello")
        os.mkdir("d")
        os.makedirs("d/e/f")
        fd = os.open("b.bin", os.O_WRONLY | os.O_CREAT, 0o666)
        os.write(fd, b"x"); os.close(fd)
        shutil.copyfile("a.txt", "c.txt")
        shutil.copyfile("a.txt", "d/g.txt")
        try:
            shutil.copy("a.txt", "d/h.txt")
            out["shutil_copy"] = "ok"
        except PermissionError:
            out["shutil_copy"] = "denied"
        os.rename("c.txt", "d/c.txt")
        os.symlink("a.txt", "link")
        os.truncate("b.bin", 0)
        out["modes"] = {n: stat.S_IMODE(os.stat(n).st_mode) for n in ("a.txt", "b.bin", "d", "d/e/f", "d/g.txt", "d/c.txt")}
        out["content"] = open("d/g.txt").read()
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script), mode)
    assert rc == 0, err
    result = json.loads(out)
    assert result["content"] == "hello"
    assert result["shutil_copy"] == "denied"  # copymode needs chmod, which workers do not get
    assert result["modes"] == {
        "a.txt": 0o600, "b.bin": 0o600, "d": 0o700, "d/e/f": 0o700, "d/g.txt": 0o600, "d/c.txt": 0o600,
    }


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_a_venv_built_by_the_host_is_usable_but_not_rebuildable_by_the_worker(mode, tmp_path):
    import venv

    data = tmp_path / "rw"
    data.mkdir()
    venv.create(data / "env", with_pip=False, symlinks=True)
    script = """
        import json, os
        out = {"cfg": open("env/pyvenv.cfg").read().startswith("home"), "bin": sorted(os.listdir("env/bin"))[:1]}
        try:
            import venv
            venv.create("mine", with_pip=False)
            out["create"] = "ok"
        except PermissionError:
            out["create"] = "denied"
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script, rw=data), mode)
    assert rc == 0, err
    result = json.loads(out)
    assert result["cfg"] is True and result["bin"]
    assert result["create"] == "denied"


# --- Enforcement level -------------------------------------------------------


def test_enforced_needs_landlock_abi_3_or_more():
    assert not SandboxProbe(abi=2, verified=True).enforced
    assert not SandboxProbe(abi=1, verified=True).enforced
    assert SandboxProbe(abi=3, verified=True).enforced
    assert not SandboxProbe(abi=9, verified=False).enforced


def test_a_kernel_below_abi_3_is_declared_because_truncate_is_not_covered(monkeypatch):
    monkeypatch.setattr(landlock, "probe_abi", lambda: (2, "kernel reports ABI 2"))
    result = spawn._run_probe(sys.executable)
    assert result.abi == 2 and not result.enforced and not result.verified
    assert "truncate" in result.reason
    assert sandbox_status(result)["label"] == "Not sandboxed on this host"


def test_a_host_without_a_metadata_filter_is_declared(monkeypatch):
    monkeypatch.setattr(landlock, "probe_abi", lambda: (6, "kernel reports ABI 6"))
    monkeypatch.setattr(seccomp, "supported", lambda: False)
    result = spawn._run_probe(sys.executable)
    assert not result.enforced and "metadata filter" in result.reason


def test_a_canary_that_can_truncate_or_chmod_is_not_verified(monkeypatch):
    monkeypatch.setattr(landlock, "probe_abi", lambda: (6, "kernel reports ABI 6"))
    good = {
        "read_outside": "denied", "write_outside": "denied", "write_inside": "allowed", "truncate_outside": "denied",
        "chmod_outside": "denied", "chmod_inside": "denied", "chown_outside": "denied", "utime_outside": "denied",
        "setxattr_outside": "denied", "io_uring_setup": "denied",
    }

    class Fake:
        returncode = 0

        def __init__(self, result):
            self.result = result

        def communicate(self, timeout=None):
            return json.dumps(self.result).encode(), b""

        def stop(self, grace=0):
            pass

    monkeypatch.setattr(spawn, "_start", lambda *a, **k: Fake(good))
    assert spawn._run_probe(sys.executable).enforced
    for key in ("truncate_outside", "chmod_outside", "chmod_inside", "chown_outside", "utime_outside", "setxattr_outside", "io_uring_setup"):
        monkeypatch.setattr(spawn, "_start", lambda *a, _k=key, **k: Fake({**good, _k: "allowed"}))
        assert not spawn._run_probe(sys.executable).enforced, key


@linux_only
def test_a_policy_capped_below_abi_3_is_reported_as_declared(live, tmp_path):
    need_abi(live, 3)
    capped = make_spec(tmp_path, "pass", abi_cap=2)
    process = spawn_worker(capped, probe=live)
    process.communicate(timeout=30)
    assert process.enforcement == "declared"
    with pytest.raises(SandboxRefused):
        spawn_worker(capped, probe=live, require_sandbox=True)
    full = spawn_worker(make_spec(tmp_path, "pass", abi_cap=3), probe=live)
    full.communicate(timeout=30)
    assert full.enforcement == "enforced"


@linux_only
def test_the_live_canary_checks_truncation_and_metadata(live):
    # The real probe passed, so the canary saw all of these denied and the
    # trunc file untouched; verify the keys it demands are the ones it asks for.
    assert live.verified
    assert "truncate_outside" in spawn._CANARY_SCRIPT and "chmod_outside" in spawn._CANARY_SCRIPT


# --- Walk with descriptors ----------------------------------------------------


@pytest.mark.skipif(not policy_module._use_descriptors(), reason="the descriptor walk only exists where O_PATH does")
def test_a_directory_swapped_during_the_walk_cannot_redirect_the_recorded_identity(tmp_path, monkeypatch):
    rw = tmp_path / "rw"
    (rw / "a" / "b").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "b").mkdir(parents=True)
    real_rw = Path(os.path.realpath(rw))
    original = os.stat(rw / "a" / "b")
    foreign = os.stat(elsewhere / "b")
    swapped = []

    def hook(reached):
        if reached == str(real_rw / "a") and not swapped:
            os.rename(rw / "a", rw / "a_old")
            os.symlink(elsewhere, rw / "a")
            swapped.append(reached)

    monkeypatch.setattr(policy_module, "_walk_hook", hook)
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[rw / "a" / "b"])
    assert swapped, "the hook never ran"
    recorded = policy.identities[str(real_rw / "a" / "b")]
    assert recorded == (original.st_dev, original.st_ino)
    assert recorded != (foreign.st_dev, foreign.st_ino)


@linux_only
def test_a_directory_swapped_after_the_walk_stops_the_launch(live, tmp_path):
    rw = tmp_path / "rw"
    (rw / "a" / "b").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "b").mkdir(parents=True)
    (elsewhere / "b" / "secret.txt").write_text("secret", encoding="utf-8")
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[rw / "a" / "b"])
    os.rename(rw / "a", rw / "a_old")
    os.symlink(elsewhere, rw / "a")
    spec = RuntimeSpec(
        interpreter=sys.executable,
        args=("-I", "-c", "print(open('a/b/secret.txt').read())"),
        cwd=str(rw),
        env=clean_env(),
        policy=policy,
    )
    result = run(spec, live)
    assert result[0] == policy_module.EXIT_SANDBOX_FAILED and result[1] == ""
    assert result.failure.kind == "sandbox" and "changed" in result.failure.message


def test_dotdot_in_a_grant_is_refused(tmp_path):
    with pytest.raises(PolicyError, match="'..'"):
        SandboxPolicy.for_interpreter(sys.executable, read_only=[str(tmp_path) + "/x/../y"])


# --- Platforms without the launcher --------------------------------------------


def test_the_fallback_process_answers_the_same_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    spec = make_spec(tmp_path, "print('plain')")
    process = spawn_worker(spec, probe=DECLARED)
    assert isinstance(process, WorkerProcess) and not isinstance(process, spawn.LauncherProcess)
    out, err = process.communicate(timeout=30)
    assert out.decode().strip() == "plain" and process.returncode == 0, err
    assert process.enforcement == "declared"
    assert process.launcher_failure() is None
    process.raise_for_sandbox_failure()
    process.close_lifeline()
    process.stop(0.5)
    with pytest.raises(SandboxRefused):
        spawn_worker(spec, probe=DECLARED, require_sandbox=True)


def test_the_fallback_stop_kills_children_of_a_worker_that_exits_on_sigterm(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    spec = make_spec(
        tmp_path,
        """
        import signal, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        open("child.pid", "w").write(str(child.pid))
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        time.sleep(300)
        """,
    )
    process = spawn_worker(spec, probe=DECLARED)
    child = read_pid(tmp_path / "rw" / "child.pid")
    assert alive(child)
    process.stop(5.0)
    assert process.poll() == 0
    assert wait_dead(child), "a child outlived a worker that left promptly"


@linux_only
def test_the_fallback_stop_kills_children_of_a_worker_that_already_exited(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    spec = make_spec(
        tmp_path,
        """
        import subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        open("child.pid", "w").write(str(child.pid))
        """,
    )
    process = spawn_worker(spec, probe=DECLARED)
    child = read_pid(tmp_path / "rw" / "child.pid")
    wait_exited_unreaped(process)
    assert process.returncode is None and alive(child)
    process.stop(1.0)
    assert process.returncode == 0
    assert wait_dead(child), "a child outlived a worker that had already exited"


@linux_only
def test_the_fallback_stop_kills_children_after_communicate_reaped_the_leader(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    spec = make_spec(
        tmp_path,
        """
        import subprocess, sys
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        open("child.pid", "w").write(str(child.pid))
        """,
    )
    process = spawn_worker(spec, probe=DECLARED)
    out, err = process.communicate(timeout=30)
    child = read_pid(tmp_path / "rw" / "child.pid")
    assert process.returncode == 0 and alive(child)
    process.stop(1.0)
    assert wait_dead(child), "a child outlived stop() once the leader had been reaped"
    process.stop(1.0)


@linux_only
def test_the_fallback_group_is_held_by_the_anchor_not_the_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    process = spawn_worker(make_spec(tmp_path, "import time; time.sleep(300)"), probe=DECLARED)
    anchor = process._anchor
    assert anchor is not None and anchor.pid != process.pid
    assert os.getpgid(process.pid) == anchor.pid
    process.kill()
    process.wait(timeout=10)
    # The leader is gone, the group id is still the anchor's, unreaped.
    assert os.getpgid(anchor.pid) == anchor.pid and anchor.poll() is None
    process.stop(0.5)
    assert anchor.returncode is not None and process._anchor is None


@linux_only
def test_dropping_the_fallback_handle_ends_the_group(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    spec = make_spec(
        tmp_path,
        """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        open("child.pid", "w").write(str(child.pid))
        time.sleep(300)
        """,
    )
    process = spawn_worker(spec, probe=DECLARED)
    child = read_pid(tmp_path / "rw" / "child.pid")
    anchor = process._anchor
    # Only the handle goes away: Popen.__del__ parks the still-running child on
    # a module list, so the anchor's pipe has to be closed before that.
    del process
    gc.collect()
    assert wait_dead(child), "the anchor did not end its group when the handle was dropped"
    assert anchor.wait(timeout=10) is not None


def test_without_an_anchor_a_reaped_leader_gets_no_group_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    monkeypatch.setattr(spawn, "_start_anchor", lambda: None)
    sent = []
    process = spawn_worker(make_spec(tmp_path, "print('done')"), probe=DECLARED)
    process.communicate(timeout=30)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    process.stop(0.5)
    assert sent == []


@linux_only
def test_the_fallback_group_kill_never_names_a_reaped_leader(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    monkeypatch.setattr(spawn, "_start_anchor", lambda: None)
    spec = make_spec(
        tmp_path,
        """
        import subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        open("child.pid", "w").write(str(child.pid))
        """,
    )
    process = spawn_worker(spec, probe=DECLARED)
    child = read_pid(tmp_path / "rw" / "child.pid")
    wait_exited_unreaped(process)
    real_killpg = os.killpg
    seen = []

    def checked(pgid, sig):
        # Still this process's own, unreaped child: the number cannot be anyone else's.
        try:
            still_ours = os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            still_ours = False
        seen.append((pgid, still_ours))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", checked)
    process.stop(1.0)
    assert seen == [(process.pid, True)], seen
    assert process.returncode == 0 and wait_dead(child)


@linux_only
def test_the_fallback_stop_without_waitid_signals_only_a_worker_it_has_not_reaped(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    monkeypatch.setattr(spawn, "_start_anchor", lambda: None)
    monkeypatch.delattr(os, "waitid")
    real_killpg = os.killpg
    seen = []

    def checked(pgid, sig):
        # The worker is a zombie or running here, never reaped: poll() would not find it gone.
        seen.append(pgid)
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", checked)
    # A worker that leaves on SIGTERM is not reaped before its group is ended: the group is
    # signalled while the leader is a zombie, so same-group children cannot survive it.
    quick = spawn_worker(
        make_spec(tmp_path, "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); print('up', flush=True); time.sleep(300)"),
        probe=DECLARED,
    )
    assert quick.stdout.readline().strip() == b"up"
    quick.stop(5.0)
    assert quick.returncode == 0 and seen == [quick.pid] and quick.group_cleaned is True
    # One that ignores SIGTERM outlives its grace: the group is signalled while the leader is unreaped.
    stubborn = spawn_worker(
        make_spec(tmp_path, "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); time.sleep(300)"),
        probe=DECLARED,
    )
    assert stubborn.stdout.readline().strip() == b"up"
    stubborn.stop(0.3)
    assert seen == [quick.pid, stubborn.pid] and stubborn.returncode is not None


@linux_only
def test_a_launcher_that_exited_on_its_own_is_never_signalled_by_pid(tmp_path, monkeypatch):
    process = spawn_worker(make_spec(tmp_path, "print('done')"), probe=DECLARED)
    wait_exited_unreaped(process)
    real_kill = os.kill
    sent = []

    def recording(pid, sig):
        if pid == process.pid:
            sent.append((sig, process.returncode))
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", recording)
    # The launcher exits between stop()'s own check and the signals it sends:
    # the first poll still sees it running, the next one reaps it.
    real_poll = process.poll
    calls = []

    def late_poll():
        calls.append(1)
        return None if len(calls) == 1 else real_poll()

    monkeypatch.setattr(process, "poll", late_poll)
    process.stop(1.0)
    assert process.returncode == 0
    # Popen.send_signal polls first. A raw os.kill would hit the reaped pid.
    assert all(returncode is None for _sig, returncode in sent), sent


def test_the_launch_closes_every_descriptor_when_a_channel_cannot_be_made(tmp_path, monkeypatch):
    spec = make_spec(tmp_path, "pass")
    opened = []
    real_pair = spawn._socket_pair
    real_memfd = getattr(os, "memfd_create", None)

    def failing_second_pair():
        if len([fd for fd in opened if fd[0] == "pair"]) >= 1:
            raise OSError(errno.EMFILE, "Too many open files")
        pair = real_pair()
        opened.append(("pair", pair))
        return pair

    monkeypatch.setattr(spawn, "_socket_pair", failing_second_pair)
    if real_memfd is not None:

        def recording_memfd(*args, **kwargs):
            fd = real_memfd(*args, **kwargs)
            opened.append(("memfd", (fd,)))
            return fd

        monkeypatch.setattr(os, "memfd_create", recording_memfd)
    with pytest.raises(OSError) as raised:
        spawn._start(spec, enforce=False, min_abi=0, enforcement="declared")
    assert raised.value.errno == errno.EMFILE
    assert opened, "nothing was opened before the failure"
    leaked = []
    for _kind, fds in opened:
        for fd in fds:
            try:
                os.fstat(fd)
                leaked.append(fd)
            except OSError:
                pass
    assert leaked == []


def test_the_anchor_is_skipped_where_it_cannot_work(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert spawn._start_anchor() is None
    monkeypatch.undo()
    monkeypatch.delattr(os, "killpg", raising=False)
    assert spawn._start_anchor() is None


def test_status_does_not_claim_resource_limits_where_none_are_applied(tmp_path, monkeypatch):
    assert "with resource limits" in " ".join(sandbox_status(DECLARED)["limitations"])
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    notes = " ".join(sandbox_status(DECLARED)["limitations"])
    assert "no resource limits" in notes and "with resource limits" not in notes
    # The path itself applies none: the worker keeps the host's own limit.
    import resource

    spec = make_spec(
        tmp_path,
        "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])",
        limits=Limits(nofile=64),
    )
    process = spawn_worker(spec, probe=DECLARED)
    out, err = process.communicate(timeout=30)
    assert process.returncode == 0, err
    assert int(out) == resource.getrlimit(resource.RLIMIT_NOFILE)[0] != 64


def test_the_fallback_process_can_be_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    process = spawn_worker(make_spec(tmp_path, "import time; time.sleep(300)"), probe=DECLARED)
    process.stop(0.5)
    assert process.poll() is not None


# --- prlimit64 and io_uring --------------------------------------------------


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_prlimit_may_only_query_and_setrlimit_is_denied(arch):
    program = seccomp.build_filter(arch)
    number = seccomp.PRLIMIT64[arch]
    eperm = seccomp.SECCOMP_RET_ERRNO | errno.EPERM
    allow = seccomp.SECCOMP_RET_ALLOW
    pointer = 0x7FFF00001000
    assert run_bpf(program, arch, number, (0, 7, 0, pointer)) == allow  # query on the caller
    assert run_bpf(program, arch, number, (1234, 7, 0, pointer)) == allow  # query on another process
    assert run_bpf(program, arch, number, (0, 7, pointer, 0)) == eperm  # set on the caller
    assert run_bpf(program, arch, number, (1234, 7, pointer, 0)) == eperm
    assert run_bpf(program, arch, number, (0, 7, 1 << 32, 0)) == eperm  # high half only
    assert run_bpf(program, arch, number, (0, 7, pointer, pointer)) == eperm
    assert seccomp.DENIED_SYSCALLS[arch]["setrlimit"] in set(seccomp.DENIED_SYSCALLS[arch].values())
    assert run_bpf(program, arch, seccomp.DENIED_SYSCALLS[arch]["setrlimit"], (7, pointer)) == eperm


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_io_uring_is_denied_on_every_architecture(arch):
    names = seccomp.DENIED_SYSCALLS[arch]
    assert {"io_uring_setup", "io_uring_enter", "io_uring_register"} <= set(names)
    program = seccomp.build_filter(arch)
    for number in (425, 426, 427):
        assert run_bpf(program, arch, number) == seccomp.SECCOMP_RET_ERRNO | errno.EPERM


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_a_worker_cannot_set_any_resource_limit_but_can_read_them(mode, tmp_path):
    script = """
        import errno, json, os, resource
        out = {}
        parent = os.getppid()
        def attempt(name, fn):
            try:
                fn(); out[name] = "allowed"
            except OSError as exc:
                out[name] = errno.errorcode.get(exc.errno, exc.errno)
            except ValueError:
                # resource.setrlimit reports EPERM as a ValueError.
                out[name] = "EPERM"
        attempt("set_other", lambda: resource.prlimit(parent, resource.RLIMIT_NOFILE, (0, 0)))
        attempt("query_other", lambda: resource.prlimit(parent, resource.RLIMIT_NOFILE))
        attempt("get_self", lambda: resource.getrlimit(resource.RLIMIT_CORE))
        attempt("set_self", lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
        attempt("set_self_implicit", lambda: resource.prlimit(0, resource.RLIMIT_CORE, (0, 0)))
        attempt("set_self_by_pid", lambda: resource.prlimit(os.getpid(), resource.RLIMIT_CORE, (0, 0)))
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script), mode)
    assert rc == 0, err
    assert json.loads(out) == {
        "set_other": "EPERM",
        "query_other": "allowed",
        "get_self": "allowed",
        "set_self": "EPERM",
        "set_self_implicit": "EPERM",
        "set_self_by_pid": "EPERM",
    }


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_io_uring_returns_eperm_inside_the_sandbox(mode, tmp_path):
    script = """
        import ctypes, errno, json
        libc = ctypes.CDLL(None, use_errno=True)
        out = {}
        for name, number in (("setup", 425), ("enter", 426), ("register", 427)):
            ctypes.set_errno(0)
            result = libc.syscall(number, 0, 0, 0, 0, 0, 0)
            out[name] = errno.errorcode.get(ctypes.get_errno(), ctypes.get_errno()) if result < 0 else "allowed"
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script), mode)
    assert rc == 0, err
    assert json.loads(out) == {"setup": "EPERM", "enter": "EPERM", "register": "EPERM"}


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_file_attribute_ioctls_are_denied_and_other_ioctls_are_not(arch):
    program = seccomp.build_filter(arch)
    number = seccomp.IOCTL[arch]
    eperm = seccomp.SECCOMP_RET_ERRNO | errno.EPERM
    allow = seccomp.SECCOMP_RET_ALLOW
    assert {"FS_IOC_SETFLAGS", "FS_IOC_FSSETXATTR", "FS_IOC_ENABLE_VERITY", "FIFREEZE"} <= set(seccomp.DENIED_IOCTLS)
    for command in seccomp.DENIED_IOCTLS.values():
        assert run_bpf(program, arch, number, (3, command)) == eperm, hex(command)
        # Only the low half is the command; the high half is ignored by the kernel.
        assert run_bpf(program, arch, number, (3, command, 0)) == eperm
        assert run_bpf(program, arch, number, (3, (7 << 32) | command)) == eperm
        # The same value as an argument of some other call is not an ioctl.
        assert run_bpf(program, arch, number + 1000, (3, command)) == allow
    for command in (0x5401, 0x541B, 0x5413, 0x40086603, 0x80086601):  # TCGETS, FIONREAD, TIOCGWINSZ, nearby values
        assert run_bpf(program, arch, number, (3, command)) == allow, hex(command)


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_keyrings_and_the_scheduling_of_other_processes_are_denied(arch):
    names = seccomp.DENIED_SYSCALLS[arch]
    wanted = {"add_key", "request_key", "keyctl", "setpriority", "ioprio_set", "sched_setparam", "sched_setscheduler", "sched_setaffinity", "sched_setattr"}
    assert wanted <= set(names)
    program = seccomp.build_filter(arch)
    for name in wanted:
        assert run_bpf(program, arch, names[name]) == seccomp.SECCOMP_RET_ERRNO | errno.EPERM, name


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_a_worker_cannot_reach_host_keyrings_attributes_or_scheduling(mode, tmp_path):
    script = """
        import ctypes, errno, fcntl, json, os, struct
        libc = ctypes.CDLL(None, use_errno=True)
        out = {}
        parent = os.getppid()
        def attempt(name, fn):
            try:
                fn(); out[name] = "allowed"
            except OSError as exc:
                out[name] = errno.errorcode.get(exc.errno, exc.errno)
        def raw(number, *args):
            ctypes.set_errno(0)
            if libc.syscall(number, *[ctypes.c_long(a) for a in args]) < 0:
                raise OSError(ctypes.get_errno(), "syscall")
        import platform
        arm = platform.machine().lower() in ("aarch64", "arm64")
        keyctl, add_key = (219, 217) if arm else (250, 248)
        attempt("keyctl_user_keyring", lambda: raw(keyctl, 0, -4, 0))
        attempt("add_key", lambda: raw(add_key, 0, 0, 0, 0, 0))
        attempt("setpriority_parent", lambda: os.setpriority(os.PRIO_PROCESS, parent, 19))
        attempt("setpriority_self", lambda: os.setpriority(os.PRIO_PROCESS, 0, 19))
        attempt("affinity_parent", lambda: os.sched_setaffinity(parent, {0}))
        attempt("scheduler_self", lambda: os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0)))
        attempt("getpriority", lambda: os.getpriority(os.PRIO_PROCESS, 0))
        attempt("getaffinity", lambda: os.sched_getaffinity(0))
        fd = os.open("f.txt", os.O_RDWR | os.O_CREAT, 0o600)
        attempt("fs_ioc_setflags", lambda: fcntl.ioctl(fd, 0x40086602, struct.pack("l", 0)))
        attempt("fs_ioc_fssetxattr", lambda: fcntl.ioctl(fd, 0x401C5820, bytes(28)))
        attempt("enable_verity", lambda: fcntl.ioctl(fd, 0x40806685, bytes(128)))
        r, w = os.pipe()
        os.write(w, b"abc")
        attempt("ordinary_ioctl", lambda: fcntl.ioctl(r, 0x541B, bytes(4)))
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script), mode)
    assert rc == 0, err
    result = json.loads(out)
    for name in ("keyctl_user_keyring", "add_key", "setpriority_parent", "setpriority_self", "affinity_parent", "scheduler_self",
                 "fs_ioc_setflags", "fs_ioc_fssetxattr", "enable_verity"):
        assert result[name] == "EPERM", (name, result)
    for name in ("getpriority", "getaffinity", "ordinary_ioctl"):
        assert result[name] == "allowed", (name, result)


SHARED_STATE_CALLS = (
    "shmget", "shmat", "shmctl", "shmdt", "semget", "semop", "semtimedop", "semctl",
    "msgget", "msgsnd", "msgrcv", "msgctl",
    "mq_open", "mq_unlink", "mq_timedsend", "mq_timedreceive", "mq_notify", "mq_getsetattr",
    "perf_event_open", "bpf", "userfaultfd", "inotify_init1", "fanotify_init",
    "process_vm_readv", "process_vm_writev", "kcmp", "open_by_handle_at", "syslog", "lsm_set_self_attr",
    "mount", "umount2", "pivot_root", "chroot", "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick",
    "mount_setattr",
)


@pytest.mark.parametrize("arch", sorted(seccomp.DENIED_SYSCALLS))
def test_system_v_ipc_posix_queues_and_other_shared_state_calls_are_denied(arch):
    names = seccomp.DENIED_SYSCALLS[arch]
    assert set(SHARED_STATE_CALLS) <= set(names)
    program = seccomp.build_filter(arch)
    for name in SHARED_STATE_CALLS:
        assert run_bpf(program, arch, names[name]) == seccomp.SECCOMP_RET_ERRNO | errno.EPERM, name
    # Numbers must match the kernel tables (checked against ausyscall for both arches).
    expected = {
        seccomp.AUDIT_ARCH_X86_64: {"shmget": 29, "shmat": 30, "shmctl": 31, "shmdt": 67, "semget": 64, "semop": 65, "semctl": 66,
                                    "semtimedop": 220, "msgget": 68, "msgsnd": 69, "msgrcv": 70, "msgctl": 71, "mq_open": 240,
                                    "mq_getsetattr": 245, "bpf": 321, "perf_event_open": 298},
        seccomp.AUDIT_ARCH_AARCH64: {"shmget": 194, "shmat": 196, "shmctl": 195, "shmdt": 197, "semget": 190, "semop": 193, "semctl": 191,
                                     "semtimedop": 192, "msgget": 186, "msgsnd": 189, "msgrcv": 188, "msgctl": 187, "mq_open": 180,
                                     "mq_getsetattr": 185, "bpf": 280, "perf_event_open": 241},
    }[arch]
    for name, number in expected.items():
        assert names[name] == number, name


@linux_only
@pytest.mark.skipif(not seccomp.supported(), reason="no metadata filter for this architecture")
def test_a_worker_cannot_reach_system_v_ipc_posix_queues_or_kernel_state(mode, tmp_path):
    arch = seccomp.native_audit_arch()
    numbers = {name: seccomp.DENIED_SYSCALLS[arch][name] for name in SHARED_STATE_CALLS}
    script = """
        import ctypes, errno, json
        libc = ctypes.CDLL(None, use_errno=True)
        numbers = %r
        out = {}
        IPC_CREAT, IPC_RMID = 0o1000, 0
        def call(name, *args):
            ctypes.set_errno(0)
            result = libc.syscall(numbers[name], *[ctypes.c_long(a) for a in args])
            return result, ctypes.get_errno()
        for name in numbers:
            args = (0, 0, 0, 0, 0, 0)
            if name == "syslog":
                args = (3, 0, 0)
            result, err = call(name, *args)
            out[name] = errno.errorcode.get(err, err) if result < 0 else "allowed"
            if result >= 0 and name in ("inotify_init1", "fanotify_init", "userfaultfd", "open_tree", "fsopen", "fspick"):
                import os
                os.close(result)
        # Calls that would create something if they were allowed.
        for name, args in (("shmget", (0, 4096, 0o600 | IPC_CREAT)), ("msgget", (0, 0o600 | IPC_CREAT)), ("semget", (0, 1, 0o600 | IPC_CREAT))):
            result, err = call(name, *args)
            out["create_" + name] = errno.errorcode.get(err, err) if result < 0 else "allowed"
            if result >= 0:
                call({"shmget": "shmctl", "msgget": "msgctl", "semget": "semctl"}[name], result, 0 if name != "semget" else 0, IPC_RMID if name != "semget" else IPC_RMID)
        print(json.dumps(out))
    """ % (numbers,)
    rc, out, err = run(make_spec(tmp_path, script), mode)
    assert rc == 0, err
    result = json.loads(out)
    assert set(result.values()) == {"EPERM"}, result


@linux_only
def test_landlock_keeps_a_worker_from_tracing_or_reading_its_launcher(live, tmp_path):
    script = """
        import ctypes, errno, json, os
        libc = ctypes.CDLL(None, use_errno=True)
        out = {}
        parent = os.getppid()
        def raw(number, *args):
            ctypes.set_errno(0)
            if libc.syscall(number, *[ctypes.c_long(a) for a in args]) < 0:
                raise OSError(ctypes.get_errno(), "syscall")
        def attempt(name, fn):
            try:
                fn(); out[name] = "allowed"
            except OSError as exc:
                out[name] = errno.errorcode.get(exc.errno, exc.errno)
        import platform
        arm = platform.machine().lower() in ("aarch64", "arm64")
        ptrace, pidfd_open, pidfd_getfd = (117, 434, 438) if arm else (101, 434, 438)
        attempt("ptrace_seize", lambda: raw(ptrace, 0x4206, parent, 0, 0))
        attempt("ptrace_attach", lambda: raw(ptrace, 16, parent, 0, 0))
        attempt("pidfd_getfd", lambda: raw(pidfd_getfd, libc.syscall(pidfd_open, parent, 0), 0, 0))
        attempt("proc_mem", lambda: open("/proc/%d/mem" % parent, "rb").read(1))
        attempt("proc_environ", lambda: open("/proc/%d/environ" % parent, "rb").read(1))
        attempt("proc_fd", lambda: os.listdir("/proc/%d/fd" % parent))
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script), live)
    assert rc == 0, err
    result = json.loads(out)
    assert set(result.values()) <= {"EPERM", "EACCES"}, result


def test_the_canary_demands_io_uring_be_denied():
    assert "io_uring_setup" in spawn._CANARY_SCRIPT
    assert "io_uring_setup" in spawn._CANARY_REQUIRED_DENIED


# --- A launcher that cannot see its tree fails safe ---------------------------


def load_launcher():
    spec = importlib.util.spec_from_file_location("launcher_failsafe", spawn.LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@linux_only
def test_an_unreadable_proc_is_an_error_never_an_empty_tree(monkeypatch):
    launcher = load_launcher()
    assert os.getpid() in launcher._children_map()

    def no_listing(path):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(launcher.os, "listdir", no_listing)
    with pytest.raises(launcher.EnumerationError):
        launcher._children_map()
    with pytest.raises(launcher.EnumerationError):
        launcher._own_children()
    monkeypatch.undo()

    monkeypatch.setattr(launcher.os, "listdir", lambda path: [])
    with pytest.raises(launcher.EnumerationError, match="missing"):
        launcher._children_map()
    monkeypatch.undo()

    real_open = open

    def out_of_descriptors(path, *args, **kwargs):
        if str(path).startswith("/proc/") and str(path).endswith("/stat"):
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", out_of_descriptors)
    with pytest.raises(launcher.EnumerationError):
        launcher._children_map()


@linux_only
def test_cleanup_uses_the_emergency_path_only_when_nothing_can_be_listed(monkeypatch):
    launcher = load_launcher()
    reasons = []

    def broken():
        raise launcher.EnumerationError("no descriptors")

    monkeypatch.setattr(launcher, "_children_map", broken)
    monkeypatch.setattr(launcher, "_emergency_kill", lambda reason: reasons.append(str(reason)))
    # The scan fails but the children list works: the launcher carries on.
    monkeypatch.setattr(launcher, "_direct_children", lambda: [])
    states = iter([(None, True), (None, False)])
    monkeypatch.setattr(launcher, "_reap_all", lambda main_pid=None: next(states))
    assert launcher._cleanup_tree() is True
    assert reasons == []
    # Neither works: nothing can be named, so the emergency path runs.
    monkeypatch.setattr(launcher, "_direct_children", lambda: None)
    monkeypatch.setattr(launcher, "_reap_all", lambda main_pid=None: (None, True))
    assert launcher._cleanup_tree() is False
    assert reasons == ["no descriptors"]


@linux_only
@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.ENOENT, errno.ESRCH])
def test_a_proc_entry_the_kernel_refuses_is_skipped_not_an_error(monkeypatch, code):
    launcher = load_launcher()
    real_open = open
    own = "/proc/%d/stat" % os.getpid()
    refused = []

    def hidden(path, *args, **kwargs):
        text = str(path)
        if text.startswith("/proc/") and text.endswith("/stat") and text != own:
            refused.append(text)
            raise OSError(code, os.strerror(code))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", hidden)
    assert launcher._children_map().keys() == {os.getpid()}
    assert refused
    # The launcher's own entry is the one that must always be readable.
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: hidden(path, *a, **k) if str(path) != own else (_ for _ in ()).throw(PermissionError(code, "denied")))
    with pytest.raises(launcher.EnumerationError, match="missing"):
        launcher._children_map()


@linux_only
def test_the_children_list_falls_back_to_a_scan_by_parent_id(monkeypatch):
    launcher = load_launcher()
    me = os.getpid()
    monkeypatch.setattr(launcher, "_direct_children", lambda: [31, 32])
    assert launcher._own_children() == [31, 32]
    # No children file: only processes whose parent is this one count, never a grandchild.
    monkeypatch.setattr(launcher, "_direct_children", lambda: None)
    monkeypatch.setattr(launcher, "_children_map", lambda: {me: 1, 41: me, 42: 41, 43: me})
    assert launcher._own_children() == [41, 43]

    def broken():
        raise launcher.EnumerationError("denied")

    monkeypatch.setattr(launcher, "_children_map", broken)
    with pytest.raises(launcher.EnumerationError):
        launcher._own_children()


@linux_only
def test_a_descendant_that_survives_the_cleanup_is_reported_not_forgotten(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setattr(launcher, "CLEANUP_SECONDS", 0.3)
    monkeypatch.setattr(launcher, "_direct_children", lambda: [])
    monkeypatch.setattr(launcher, "_reap_all", lambda main_pid=None: (None, True))
    assert launcher._cleanup_tree() is False
    monkeypatch.setattr(launcher, "_reap_all", lambda main_pid=None: (None, False))
    assert launcher._cleanup_tree() is True


@linux_only
def test_an_empty_listing_is_not_proof_the_tree_is_gone(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setattr(launcher, "CLEANUP_SECONDS", 0.3)
    monkeypatch.setattr(launcher, "_direct_children", lambda: [])
    monkeypatch.setattr(launcher, "_children_map", lambda: {os.getpid(): 1})
    monkeypatch.setattr(launcher, "_reap_all", lambda main_pid=None: (None, True))
    assert launcher._cleanup_tree() is False
    monkeypatch.setattr(launcher, "_reap_all", lambda main_pid=None: (None, False))
    assert launcher._cleanup_tree() is True


@linux_only
def test_the_kill_loop_waits_for_nobody_between_listing_and_signalling(monkeypatch):
    launcher = load_launcher()
    events = []
    states = iter([(None, True), (None, True), (None, False)])

    def reap(main_pid=None):
        events.append("reap")
        return next(states)

    def children():
        events.append("list")
        return [10, 11]

    monkeypatch.setattr(launcher, "_reap_all", reap)
    monkeypatch.setattr(launcher, "_own_children", children)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: events.append((pid, sig)))
    assert launcher._kill_tree() == (None, True)
    kill = signal.SIGKILL
    assert events == ["reap", "list", (10, kill), (11, kill), "reap", "list", (10, kill), (11, kill), "reap"]


MOUNTINFO = (
    "25 1 0:23 / /proc rw,nosuid,nodev,noexec,relatime shared:13 - proc proc %s\n"
    "30 25 0:24 / /proc/sys/fs/binfmt_misc rw,relatime shared:14 - autofs systemd-1 rw\n"
)


@linux_only
@pytest.mark.parametrize(
    "options, unfiltered",
    [
        ("rw", True),
        ("rw,hidepid=0", True),
        ("rw,hidepid=off", True),
        ("rw,hidepid=1", False),
        ("rw,hidepid=2", False),
        ("rw,hidepid=invisible", False),
        ("rw,hidepid=noaccess", False),
        ("rw,hidepid=ptraceable,gid=1000", False),
        ("rw,subset=pid", False),
    ],
)
def test_a_filtered_proc_is_recognised_from_the_mount_options(options, unfiltered):
    launcher = load_launcher()
    assert launcher._proc_is_unfiltered(MOUNTINFO % options) is unfiltered


@linux_only
def test_a_proc_that_cannot_be_seen_in_the_mount_table_is_not_trusted():
    launcher = load_launcher()
    assert launcher._proc_is_unfiltered("") is False
    assert launcher._proc_is_unfiltered("36 35 98:0 /a /b rw - ext4 /dev/sda rw\n") is False
    # The last mount at /proc wins, so a filtered mount over a plain one counts.
    stacked = MOUNTINFO % "rw" + MOUNTINFO % "rw,hidepid=2"
    assert launcher._proc_is_unfiltered(stacked) is False
    assert launcher._proc_is_unfiltered(MOUNTINFO % "rw,hidepid=2" + MOUNTINFO % "rw") is True


@linux_only
def test_a_filtered_proc_without_the_children_file_refuses_to_start(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setattr(launcher, "_proc_is_unfiltered", lambda mountinfo=None: False)
    monkeypatch.setattr(launcher, "_direct_children", lambda: None)
    assert "hides processes" in launcher._tree_tracking_error()
    monkeypatch.setattr(launcher, "_direct_children", lambda: [])
    assert launcher._tree_tracking_error() is None
    monkeypatch.setattr(launcher, "_proc_is_unfiltered", lambda mountinfo=None: True)
    monkeypatch.setattr(launcher, "_direct_children", lambda: None)
    assert launcher._tree_tracking_error() is None


@linux_only
def test_the_launcher_reports_a_sandbox_failure_when_it_cannot_track_its_tree(tmp_path):
    status_r, status_w = os.pipe()
    spec_r, spec_w = os.pipe()
    life_r, life_w = os.pipe()
    spec = {"interpreter": sys.executable, "args": ["-c", "pass"], "cwd": str(tmp_path), "env": {}, "mode": "declared"}
    os.write(spec_w, json.dumps(spec).encode())
    os.close(spec_w)
    code = textwrap.dedent(
        """
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("l", %r)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        launcher._proc_is_unfiltered = lambda mountinfo=None: False
        launcher._direct_children = lambda: None
        sys.exit(launcher.main(["l", %d, %d, %d]))
        """
    ) % (str(spawn.LAUNCHER_PATH), spec_r, life_r, status_w)
    process = subprocess.run(
        [sys.executable, "-I", "-c", code], pass_fds=(spec_r, life_r, status_w), capture_output=True, timeout=30
    )
    os.close(status_w)
    try:
        assert process.returncode == 78, process.stderr
        record, cleanup = status_records(os.read(status_r, 4096))
        assert record["kind"] == "sandbox" and "descendants" in record["message"]
        assert cleanup == {"cleanup": "complete"}
    finally:
        for fd in (status_r, spec_r, life_r, life_w):
            os.close(fd)


BLIND_TREE = """
    import ctypes, importlib.util, os, signal, sys, time

    spec = importlib.util.spec_from_file_location("l", %r)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER

    def detach_and_hide():
        os.setsid()
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE

    # worker -> middle (exits) -> grandchild: every one undumpable, in its own session.
    worker = os.fork()
    if worker == 0:
        detach_and_hide()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        middle = os.fork()
        if middle == 0:
            detach_and_hide()
            grand = os.fork()
            if grand == 0:
                detach_and_hide()
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                open(%r, "a").write("%%d\\n" %% os.getpid())
                time.sleep(300)
            os._exit(0)
        open(%r, "a").write("%%d\\n" %% os.getpid())
        time.sleep(300)
    time.sleep(0.5)
    open(%r, "a").write("%%d\\n" %% worker)

    # PROC_VIEW
    print(launcher._cleanup_tree(), flush=True)
    print(launcher._children_remain(), flush=True)
"""


def blind_tree_code(pids_file, proc_view):
    code = textwrap.dedent(BLIND_TREE) % (str(spawn.LAUNCHER_PATH), str(pids_file), str(pids_file), str(pids_file))
    return code.replace("# PROC_VIEW", textwrap.dedent(proc_view))


@linux_only
def test_an_undumpable_setsid_double_forked_descendant_is_reaped_with_a_blind_proc(tmp_path):
    pids_file = tmp_path / "pids"
    code = blind_tree_code(
        pids_file,
        "# A /proc that shows nothing but the launcher: no listing can find them.\n"
        "launcher._children_map = lambda: {os.getpid(): os.getppid()}",
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert result.stdout.split() == ["True", "False"], (result.stdout, result.stderr)
    pids = [int(p) for p in pids_file.read_text().split()]
    assert len(pids) == 3
    assert all(wait_dead(pid) for pid in pids), "a hidden descendant survived the cleanup"


DENIED_PROC = """
    # hidepid=1: every other process refuses its stat file with EACCES.
    import builtins
    real_open = builtins.open

    def refusing(path, *args, **kwargs):
        text = str(path)
        if text.startswith("/proc/") and text.endswith("/stat") and text != "/proc/%d/stat" % os.getpid():
            raise PermissionError(13, "Permission denied", text)
        return real_open(path, *args, **kwargs)

    builtins.open = refusing
"""


@linux_only
def test_descendants_a_filtered_proc_refuses_to_show_are_still_killed_and_reaped(tmp_path):
    pids_file = tmp_path / "pids"
    code = blind_tree_code(pids_file, DENIED_PROC)
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    # Before the fix the launcher took the whole group (and itself) down here.
    assert result.returncode == 0 and result.stdout.split() == ["True", "False"], (result.returncode, result.stdout, result.stderr)
    pids = [int(p) for p in pids_file.read_text().split()]
    assert len(pids) == 3
    assert all(wait_dead(pid) for pid in pids), "a descendant the scan was refused survived the cleanup"


STORM = """
    import os, time
    def grow(depth):
        open("pids", "a").write("%d\\n" % os.getpid())
        if depth:
            for _ in range(2):
                if os.fork() == 0:
                    time.sleep(0.01)
                    grow(depth - 1)
                    os._exit(0)
        time.sleep(300)
    grow(6)
"""


@linux_only
def test_stop_ends_a_worker_that_keeps_forking(mode, tmp_path):
    process = spawn_worker(make_spec(tmp_path, STORM), probe=mode)
    pids_file = tmp_path / "rw" / "pids"
    end = time.monotonic() + 10
    while time.monotonic() < end:
        try:
            if len(pids_file.read_text().split()) >= 60:
                break
        except OSError:
            pass
        time.sleep(0.05)
    process.stop(0.2)
    assert process.poll() is not None
    pids = [int(p) for p in pids_file.read_text().split()]
    assert pids
    assert all(wait_dead(pid) for pid in pids), "part of a forking tree outlived stop()"


EMERGENCY = """
    import ctypes, importlib.util, os, signal, subprocess, sys, time
    spec = importlib.util.spec_from_file_location("l", %r)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    launcher._STATUS_FD = %d
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    same_group = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(300)"])
    detached = subprocess.Popen(
        [sys.executable, "-I", "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"],
        start_new_session=True,
    )
    print(same_group.pid, detached.pid, flush=True)
    %s
    launcher._emergency_kill("test")
"""


@pytest.fixture
def leftover_sleepers():
    pids = []
    yield pids
    kill_leftover_sleepers(pids)


def run_emergency(leftovers, extra=""):
    status_r, status_w = os.pipe()
    code = textwrap.dedent(EMERGENCY) % (str(spawn.LAUNCHER_PATH), status_w, extra)
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, start_new_session=True, pass_fds=(status_w,)
    )
    os.close(status_w)
    pids = []
    try:
        pids = [int(p) for p in process.stdout.readline().split()]
        code = process.wait(timeout=30)
        record, cleanup = status_records(os.read(status_r, 4096))
        return code, pids, record, cleanup
    finally:
        os.close(status_r)
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        # The detached child has a session of its own, so the group kill above
        # misses it. The caller's fixture kills what is left after its asserts.
        leftovers.extend(pids)


@linux_only
def test_the_emergency_kill_reaches_detached_children_through_the_children_list(leftover_sleepers):
    code, pids, record, cleanup = run_emergency(leftover_sleepers)
    assert code == 78
    assert cleanup == {"cleanup": "complete"}
    assert all(wait_dead(pid) for pid in pids), "a child survived the emergency kill"
    assert record["kind"] == "sandbox"
    assert "process tree" in record["message"] and "reaped every child" in record["message"]


@linux_only
def test_the_emergency_kill_ends_the_group_only_when_nothing_lists_the_children(leftover_sleepers):
    code, pids, record, cleanup = run_emergency(leftover_sleepers, "launcher._direct_children = lambda: None")
    assert code == -9
    # Nothing proved the tree empty, so the launcher does not say it is clean.
    assert cleanup == {"cleanup": "survivors"}
    assert wait_dead(pids[0]), "a member of the process group survived"
    assert record["kind"] == "sandbox" and "killed its process group" in record["message"]
    # The record is written before the group goes, so the host always learns why.


@linux_only
def test_the_emergency_kill_reports_a_survivor_it_could_not_reap(leftover_sleepers):
    code, _pids, record, cleanup = run_emergency(
        leftover_sleepers,
        "launcher.CLEANUP_SECONDS = 0.3; launcher._children_remain = lambda: True; launcher._kill_own_children = lambda: True"
    )
    assert code == 78
    assert "survived" in record["message"]
    assert cleanup == {"cleanup": "survivors"}


DIRECT_ONLY = """
    import ctypes, importlib.util, os, signal, sys, time

    spec = importlib.util.spec_from_file_location("l", %r)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER

    # worker -> grandchild: the grandchild's parent is alive, so the launcher
    # has no claim on its pid.
    worker = os.fork()
    if worker == 0:
        if os.fork() == 0:
            open(%r, "a").write("%%d\\n" %% os.getpid())
            time.sleep(300)
        open(%r, "a").write("%%d\\n" %% os.getpid())
        time.sleep(300)
    end = time.monotonic() + 10
    while time.monotonic() < end and len(open(%r).read().split()) < 2:
        time.sleep(0.02)

    sent = []
    real_kill = os.kill

    def recording(pid, sig):
        with open("/proc/%%d/stat" %% pid) as handle:
            parent = int(handle.read().rsplit(")", 1)[1].split()[1])
        sent.append(parent == os.getpid())
        real_kill(pid, sig)

    os.kill = recording
    print(launcher._cleanup_tree(), flush=True)
    print(bool(sent) and all(sent), flush=True)
"""


@linux_only
def test_the_launcher_signals_only_its_own_children_never_a_grandchild(tmp_path):
    pids_file = tmp_path / "pids"
    pids_file.write_text("")
    code = textwrap.dedent(DIRECT_ONLY) % (str(spawn.LAUNCHER_PATH), str(pids_file), str(pids_file), str(pids_file))
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert result.stdout.split() == ["True", "True"], (result.stdout, result.stderr)
    pids = [int(p) for p in pids_file.read_text().split()]
    assert len(pids) == 2 and all(wait_dead(pid) for pid in pids)


def run_launcher_main(tmp_path, preamble, *, mode="declared", script="pass"):
    """Run ``launcher.main`` in a child interpreter after ``preamble``. Returns (process, status text)."""
    status_r, status_w = os.pipe()
    spec_r, spec_w = os.pipe()
    life_r, life_w = os.pipe()
    spec = {"interpreter": sys.executable, "args": ["-I", "-c", script], "cwd": str(tmp_path), "env": {}, "mode": mode}
    os.write(spec_w, json.dumps(spec).encode())
    os.close(spec_w)
    code = (
        "import importlib.util, os, sys\n"
        "spec = importlib.util.spec_from_file_location('l', %r)\n"
        "launcher = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(launcher)\n" % str(spawn.LAUNCHER_PATH)
        + textwrap.dedent(preamble)
        + "\nsys.exit(launcher.main(['l', %d, %d, %d]))\n" % (spec_r, life_r, status_w)
    )
    try:
        process = subprocess.run(
            [sys.executable, "-I", "-c", code], pass_fds=(spec_r, life_r, status_w), capture_output=True, timeout=60
        )
        os.close(status_w)
        status_w = None
        os.set_blocking(status_r, False)
        try:
            text = os.read(status_r, 65536).decode()
        except BlockingIOError:
            text = ""
        return process, text
    finally:
        for fd in (status_r, spec_r, life_r, life_w, status_w):
            if fd is not None:
                os.close(fd)


@linux_only
def test_a_failure_after_the_worker_was_reaped_signals_nobody_by_its_pid(tmp_path):
    process, _status = run_launcher_main(
        tmp_path,
        """
        sent = []
        real_kill = os.kill
        os.kill = lambda pid, sig: (sent.append((pid, sig)), real_kill(pid, sig))[1]

        def broken(report_r):
            raise RuntimeError("late failure")

        launcher._forward_child_report = broken
        import atexit
        atexit.register(lambda: print("SENT", sent, flush=True))
        """,
    )
    out = process.stdout.decode()
    assert "late failure" in process.stderr.decode(), (out, process.stderr)
    # The worker was reaped by then, so no signal may name its pid.
    assert "SENT []" in out, out


@linux_only
def test_the_launcher_reports_pipes_it_cannot_create_and_leaks_none(tmp_path):
    process, status = run_launcher_main(
        tmp_path,
        """
        made = []
        real_pipe = os.pipe

        def failing_second():
            if len(made) >= 2:
                raise OSError(24, "Too many open files")
            pair = real_pipe()
            made.extend(pair)
            return pair

        os.pipe = failing_second
        import atexit

        def leaked():
            open_ones = []
            for fd in made:
                try:
                    os.fstat(fd)
                    open_ones.append(fd)
                except OSError:
                    pass
            print("LEAKED", open_ones, flush=True)

        atexit.register(leaked)
        """,
    )
    assert process.returncode == 78, process.stderr
    assert "LEAKED []" in process.stdout.decode(), process.stdout
    record, cleanup = status_records(status)
    assert record["kind"] == "sandbox" and "pipes" in record["message"]
    assert cleanup == {"cleanup": "complete"}


def test_a_failure_record_stays_under_the_atomic_pipe_write_size_after_escaping():
    launcher = load_launcher()
    limit = launcher._RECORD_LIMIT
    assert 512 <= limit <= 4096
    for message in ("\u00e9" * 5000, "\U0001f600" * 5000, '"' * 5000, "\x00" * 5000, "plain " * 2000, ""):
        for newline in (True, False):
            data = launcher._record("sandbox", message, newline=newline)
            assert len(data) <= limit
            assert data.endswith(b"\n") is newline
            record = json.loads(data)
            assert record["kind"] == "sandbox" and message.startswith(record["message"])
    # A long kind cannot push the record over the limit either.
    assert len(launcher._record("k" * 9000, "m" * 9000)) <= limit
    short = launcher._record("exec", "exec failed: nope", newline=False)
    assert json.loads(short) == {"kind": "exec", "message": "exec failed: nope"}


@linux_only
def test_the_host_facing_report_is_bounded_too(monkeypatch):
    launcher = load_launcher()
    read_fd, write_fd = os.pipe()
    try:
        # A pipe nobody reads: a write over the limit could not be atomic.
        monkeypatch.setattr(launcher, "_STATUS_FD", write_fd)
        launcher._report("sandbox", "\u00e9" * 5000)
        os.set_blocking(read_fd, False)
        data = os.read(read_fd, 65536)
        assert len(data) <= launcher._RECORD_LIMIT and data.endswith(b"\n")
        assert json.loads(data)["kind"] == "sandbox"
    finally:
        os.close(read_fd)
        os.close(write_fd)


@linux_only
def test_a_long_non_ascii_failure_path_reaches_the_host_in_one_bounded_record(tmp_path):
    # JSON escaping turns every non-ASCII character into six bytes.
    deep = Path(str(tmp_path))
    for _ in range(14):
        deep = deep / ("\u00e9" * 120)
    spec = make_spec(tmp_path, "pass", cwd=deep)
    process = spawn_worker(spec, probe=DECLARED)
    try:
        process.communicate(timeout=30)
        failure = process.launcher_failure()
        raw = process._status_text
    finally:
        process.stop(0.5)
    assert process.returncode == policy_module.EXIT_BAD_SPEC
    assert failure is not None and failure.kind == "spec" and failure.message.startswith("bad working directory")
    assert 0 < len(raw) <= 4096


# --- Platforms without O_PATH ---------------------------------------------------


def test_policy_and_declared_launch_work_without_descriptor_walks(tmp_path, monkeypatch):
    # macOS and Windows have no os.O_PATH: building a policy and starting a
    # declared worker must not touch it.
    monkeypatch.delattr(os, "O_PATH", raising=False)
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    rw = tmp_path / "data"
    (rw / "sub").mkdir(parents=True)
    policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[rw / "sub"])
    real_rw = os.path.realpath(rw)
    assert policy.read_write == (real_rw,)
    assert os.path.join(real_rw, "sub") in policy.read_only
    spec = RuntimeSpec(
        interpreter=sys.executable,
        args=("-I", "-c", "print('ran')"),
        cwd=str(rw),
        env=clean_env(),
        policy=policy,
    )
    process = spawn_worker(spec, probe=DECLARED)
    out, err = process.communicate(timeout=30)
    assert out.decode().strip() == "ran" and process.enforcement == "declared", err


def test_path_checks_still_refuse_links_in_a_writable_tree_without_descriptors(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_PATH", raising=False)
    rw = tmp_path / "data"
    rw.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (rw / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PolicyError, match="symlink"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[rw], read_only=[rw / "link"])


def test_the_descriptor_walk_is_used_on_linux(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(policy_module, "_walk_hook", seen.append)
    (tmp_path / "rw" / "a").mkdir(parents=True)
    SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "rw"], read_only=[tmp_path / "rw" / "a"])
    assert policy_module._use_descriptors() == (sys.platform.startswith("linux") and hasattr(os, "O_PATH"))
    if policy_module._use_descriptors():
        assert seen


# --- TLS trust data: certificates yes, private keys no --------------------------


def test_system_grants_name_certificates_never_a_whole_tls_tree():
    grants = set(policy_module.SYSTEM_READ_PATHS)
    assert not grants & {"/etc/ssl", "/etc/pki", "/etc/pki/tls", "/etc/pki/ca-trust", "/etc/ssl/private"}
    for path in grants:
        parts = Path(path).parts
        assert "private" not in parts and "CA" not in parts, path
        # Nothing may be an ancestor of a conventional private key directory.
        for secret in ("/etc/ssl/private", "/etc/pki/tls/private", "/etc/pki/CA/private", "/etc/pki/nssdb"):
            assert not (secret == path or secret.startswith(path.rstrip("/") + "/")), (path, secret)
    # The files and directories TLS verification needs are still granted.
    assert {"/etc/ssl/certs", "/etc/ssl/cert.pem", "/etc/pki/tls/certs", "/etc/pki/ca-trust/extracted"} <= grants


def test_a_built_policy_grants_no_private_key_directory():
    policy = SandboxPolicy.for_interpreter(sys.executable)
    for granted in policy.read_only:
        for secret in ("/etc/ssl/private", "/etc/pki/tls/private", "/etc/pki/CA/private"):
            assert not (granted == secret or secret.startswith(granted.rstrip("/") + "/")), (granted, secret)


@linux_only
def test_a_worker_still_verifies_tls_and_cannot_read_other_tls_files(live, tmp_path):
    import ssl
    import threading

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed")
    host_ctx = ssl.create_default_context()
    host_ctx.load_default_certs()
    host_count = len(host_ctx.get_ca_certs())
    # World-readable files next to the CA bundle that the old whole-tree grant exposed.
    wanted = (
        "/etc/ssl/certs", "/etc/ssl/cert.pem", "/etc/ssl/openssl.cnf", "/etc/ssl/ct_log_list.cnf",
        "/etc/pki/tls/certs", "/etc/pki/tls/cert.pem", "/etc/pki/tls/openssl.cnf", "/etc/pki/tls/ct_log_list.cnf",
        "/etc/pki/ca-trust/extracted",
    )

    def covered(path):
        real = os.path.realpath(path)
        return any(path == g or path.startswith(g + "/") or real == os.path.realpath(g) or real.startswith(os.path.realpath(g) + "/") for g in wanted)

    siblings = []
    for base in ("/etc/ssl", "/etc/pki"):
        for found in sorted(Path(base).rglob("*"))[:4000]:
            name = str(found)
            if found.is_file() and (found.stat().st_mode & 0o004) and not covered(name):
                siblings.append(name)
    siblings = siblings[:5]

    certs = tmp_path / "certs"
    certs.mkdir()
    ca_key, ca_crt, key, csr, crt = (str(certs / n) for n in ("ca.key", "ca.crt", "srv.key", "srv.csr", "srv.crt"))
    ext = certs / "ext.cnf"
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    for args in (
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca_crt, "-subj", "/CN=test-ca", "-days", "2"],
        ["req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=localhost"],
        ["x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial", "-out", crt, "-days", "2", "-extfile", str(ext)],
    ):
        subprocess.run([openssl, *args], check=True, capture_output=True)

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(crt, key)
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def serve():
        for _ in range(2):
            try:
                conn, _addr = listener.accept()
                with server_ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
                    tls.sendall(b"hello")
            except (OSError, ssl.SSLError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    script = """
        import errno, json, socket, ssl
        out = {}
        paths = ssl.get_default_verify_paths()
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        out["default_cas"] = len(ctx.get_ca_certs())
        for name, value in (("cafile", paths.cafile or paths.openssl_cafile), ("capath", paths.capath or paths.openssl_capath)):
            try:
                if value:
                    open(value, "rb").read(1) if name == "cafile" else __import__("os").listdir(value)
                out[name] = "readable"
            except FileNotFoundError:
                out[name] = "absent"
            except OSError as exc:
                out[name] = errno.errorcode.get(exc.errno, exc.errno)
        verify = ssl.create_default_context(cafile=%(ca)r)
        verify.verify_flags &= ~ssl.VERIFY_X509_STRICT
        with socket.create_connection(("127.0.0.1", %(port)d), timeout=10) as raw:
            with verify.wrap_socket(raw, server_hostname="localhost") as tls:
                tls.send(b"x")
                out["tls"] = tls.recv(5).decode()
        denied = {}
        for sibling in %(siblings)r:
            try:
                open(sibling, "rb").read(1)
                denied[sibling] = "readable"
            except OSError as exc:
                denied[sibling] = errno.errorcode.get(exc.errno, exc.errno)
        out["siblings"] = denied
        print(json.dumps(out))
    """ % {"ca": ca_crt, "port": listener.getsockname()[1], "siblings": siblings}
    try:
        rc, out, err = run(make_spec(tmp_path, script, ro=(certs,), ports=[listener.getsockname()[1]]), live)
    finally:
        listener.close()
    assert rc == 0, err
    result = json.loads(out)
    assert result["tls"] == "hello"
    assert result["cafile"] in ("readable", "absent"), result
    assert result["capath"] in ("readable", "absent"), result
    assert result["default_cas"] == host_count, result
    for path, state in result["siblings"].items():
        assert state == "EACCES", (path, state)


# --- Requiring a working sandbox --------------------------------------------------


def test_the_require_flag_turns_a_skip_into_a_failure():
    with pytest.raises(pytest.skip.Exception):
        cannot_enforce("no Landlock", require=False)
    with pytest.raises(pytest.fail.Exception, match="BAZARR_REQUIRE_SANDBOX_TESTS"):
        cannot_enforce("no Landlock", require=True)


def test_the_require_flag_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("BAZARR_REQUIRE_SANDBOX_TESTS", "1")
    with pytest.raises(pytest.fail.Exception):
        cannot_enforce("no Landlock")
    monkeypatch.setenv("BAZARR_REQUIRE_SANDBOX_TESTS", "0")
    with pytest.raises(pytest.skip.Exception):
        cannot_enforce("no Landlock")
    monkeypatch.delenv("BAZARR_REQUIRE_SANDBOX_TESTS")
    with pytest.raises(pytest.skip.Exception):
        cannot_enforce("no Landlock")


# --- The cleanup record ----------------------------------------------------------


@linux_only
def test_a_launcher_that_cleaned_its_tree_says_so(tmp_path):
    process = spawn_worker(make_spec(tmp_path, "pass"), probe=DECLARED)
    try:
        process.communicate(timeout=30)
        assert process.wait(timeout=30) == 0
        assert process.launcher_failure() is None
        assert process.launcher_cleanup() == "complete"
    finally:
        process.stop(0.5)


@linux_only
def test_a_launcher_that_never_started_a_worker_still_reports_a_complete_cleanup(tmp_path):
    spec = make_spec(tmp_path, "pass", cwd=tmp_path / "missing")
    process = spawn_worker(spec, probe=DECLARED)
    try:
        process.communicate(timeout=30)
        failure = process.launcher_failure()
        assert failure is not None and failure.kind == "spec"
        assert process.launcher_cleanup() == "complete"
    finally:
        process.stop(0.5)


@linux_only
def test_a_launcher_killed_before_its_cleanup_leaves_no_cleanup_record(tmp_path):
    process = spawn_worker(make_spec(tmp_path, "import time; time.sleep(60)"), probe=DECLARED)
    try:
        time.sleep(0.5)
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=30)
        assert process.returncode == -signal.SIGKILL
        assert process.launcher_cleanup() is None
    finally:
        process.stop(0.5)


def test_the_cleanup_record_is_one_short_line_without_a_kind():
    spec = importlib.util.spec_from_file_location("launcher_for_record", str(spawn.LAUNCHER_PATH))
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    read_fd, write_fd = os.pipe()
    launcher._STATUS_FD = write_fd
    try:
        launcher._report_cleanup("survivors", 3)
        launcher._report_cleanup("complete")
        os.close(write_fd)
        write_fd = None
        raw = os.read(read_fd, 4096)
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)
        launcher._STATUS_FD = None
    assert len(raw) < launcher._RECORD_LIMIT
    lines = [json.loads(line) for line in raw.decode().splitlines()]
    assert lines == [{"cleanup": "survivors", "count": 3}, {"cleanup": "complete"}]
    assert all("kind" not in line for line in lines)


# --- One lock per process object ---------------------------------------------------


@linux_only
def test_a_reap_cannot_slip_in_between_the_ownership_check_and_the_group_signal(tmp_path, monkeypatch):
    """The check and the signal are one step: a reap waits for it, so no signal follows a reap."""
    import threading

    plain = WorkerProcess([sys.executable, "-c", "pass"], start_new_session=True,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait until it is a zombie, unreaped.
        while os.waitid(os.P_PID, plain.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            time.sleep(0.01)
        seen = []
        in_check = threading.Event()
        real_killpg = os.killpg

        def pausing_killpg(pgid, sig):
            # We are past the ownership check, about to signal: let the reaper try.
            seen.append((pgid, sig, plain.returncode))
            in_check.set()
            time.sleep(0.4)
            seen.append(("after-pause", plain.returncode))
            real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", pausing_killpg)
        results = {}

        def signaller():
            results["signal"] = plain.signal_group(signal.SIGKILL)

        def reaper():
            in_check.wait(5)
            results["reaped_at"] = time.monotonic()
            plain.wait(timeout=10)
            results["reap_done"] = time.monotonic()

        threads = [threading.Thread(target=signaller), threading.Thread(target=reaper)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        assert results["signal"] is True
        # Still unreaped while the signal was being sent, and the reap came after it.
        assert seen[0][2] is None and seen[1] == ("after-pause", None)
        assert results["reap_done"] - results["reaped_at"] >= 0.3
        # Once reaped nothing is sent any more.
        seen.clear()
        assert plain.signal_group(signal.SIGKILL) is None
        assert seen == []
    finally:
        monkeypatch.undo()
        plain.stop(0.5)


@linux_only
def test_concurrent_stop_calls_share_one_run(tmp_path, monkeypatch):
    import threading

    process = spawn_worker(make_spec(tmp_path, "import time; time.sleep(60)"), probe=DECLARED)
    runs = []
    real = type(process)._do_stop

    def counting(self, grace):
        runs.append(grace)
        time.sleep(0.3)
        return real(self, grace)

    monkeypatch.setattr(type(process), "_do_stop", counting)
    try:
        threads = [threading.Thread(target=process.stop, args=(1.0,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert len(runs) == 1
        assert process.returncode is not None
    finally:
        monkeypatch.undo()
        process.stop(0.5)


@linux_only
def test_the_anchor_is_not_reaped_while_a_group_signal_is_being_sent(monkeypatch):
    """A group signal must never be sent to an anchor that has been reaped meanwhile."""
    import threading

    plain = spawn._start_plain([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    anchor = plain._anchor
    if anchor is None:
        plain.stop(0.5)
        pytest.skip("no process group anchor on this interpreter")
    in_finish = threading.Event()
    sent = []
    real_killpg = os.killpg
    real_finish = spawn._finish_anchor

    def slow_finish(target):
        # stop() is about to reap the anchor; give a signaller time to get in first.
        in_finish.set()
        time.sleep(0.5)
        real_finish(target)

    def pausing_killpg(pgid, sig):
        if sig == signal.SIGCONT:
            # Past the ownership check, about to signal: the reap may now happen.
            time.sleep(0.5)
            sent.append((pgid, anchor.returncode))
        real_killpg(pgid, sig)

    monkeypatch.setattr(spawn, "_finish_anchor", slow_finish)
    monkeypatch.setattr(os, "killpg", pausing_killpg)
    results = {}

    def signaller():
        in_finish.wait(5)
        time.sleep(0.2)
        results["signal"] = plain.signal_group(signal.SIGCONT)

    threads = [threading.Thread(target=signaller), threading.Thread(target=plain.stop, args=(0.5,))]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        # Either nothing was sent (the group was already ended) or it went to an unreaped anchor.
        assert all(returncode is None for _pgid, returncode in sent), sent
        assert anchor.returncode is not None and plain._anchor is None
    finally:
        monkeypatch.undo()
        plain.stop(0.5)


@linux_only
def test_a_worker_cannot_open_the_launchers_status_pipe_through_proc(mode, tmp_path):
    """The launcher's descriptors, the status pipe among them, are closed to its own user's processes."""
    script = """
        import json, os
        ppid = os.getppid()
        cmdline = None
        try:
            cmdline = open("/proc/%d/cmdline" % ppid, "rb").read().split(b"\\0")
        except OSError:
            pass
        status_fd = int(cmdline[-2]) if cmdline and len(cmdline) >= 3 and cmdline[-2].isdigit() else None
        opened = []
        for fd in range(3, 64):
            try:
                os.close(os.open("/proc/%d/fd/%d" % (ppid, fd), os.O_WRONLY | os.O_NONBLOCK))
                opened.append(fd)
            except OSError:
                pass
        try:
            listing = len(os.listdir("/proc/%d/fd" % ppid))
        except OSError as exc:
            listing = exc.errno
        print(json.dumps({"opened": opened, "status_fd": status_fd, "listing": listing}))
    """
    rc, out, err = run(make_spec(tmp_path, script), mode)
    assert rc == 0, err
    seen = json.loads(out)
    assert seen["opened"] == [], seen
    assert isinstance(seen["listing"], int) and seen["listing"] in (errno.EACCES, errno.EPERM, errno.ENOENT), seen


@linux_only
def test_the_launcher_closes_itself_off_before_it_starts_a_worker(tmp_path):
    process = spawn_worker(make_spec(tmp_path, "import time; time.sleep(30)"), probe=DECLARED)
    try:
        # Not even the user that started it may look into its descriptors.
        deadline = time.monotonic() + 10
        denied = False
        while time.monotonic() < deadline and not denied:
            try:
                os.listdir("/proc/%d/fd" % process.pid)
            except PermissionError:
                denied = True
            except OSError:
                pass
            time.sleep(0.05)
        assert denied
    finally:
        process.stop(0.5)


def test_a_status_stream_that_overflows_is_not_trusted(tmp_path):
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    process = spawn.LauncherProcess([sys.executable, "-c", "pass"], lifeline=None, status=read_fd, env={},
                                    close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=10)
        os.write(write_fd, b'{"cleanup": "complete"}\n')
        assert process.launcher_cleanup() == "complete"
        # A flood follows: more than the host is willing to keep.
        os.set_blocking(write_fd, False)
        sent = 0
        try:
            while sent < 200_000:
                sent += os.write(write_fd, b"x" * 4096)
        except BlockingIOError:
            pass
        for _ in range(10):
            process.launcher_cleanup()
        assert process.launcher_cleanup() is None
        assert len(process._status_text) <= spawn.STATUS_LIMIT
    finally:
        os.close(write_fd)
        process.stop(0.5)


@linux_only
def test_a_worker_cannot_reopen_the_hosts_channels_to_the_launcher(mode, tmp_path):
    """The host's ends of the status and lifeline channels (and the anchor's) cannot be reopened through /proc."""
    script = """
        import json, os, time
        host = int(os.environ["BAZARR_TEST_HOST_PID"])
        path = os.path.join(os.getcwd(), "fds.json")
        deadline = time.monotonic() + 20
        while not os.path.exists(path) and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)
        wanted = json.load(open(path))
        result = {}
        for name, fd in wanted.items():
            try:
                os.close(os.open("/proc/%d/fd/%d" % (host, fd), os.O_WRONLY | os.O_NONBLOCK))
                result[name] = "opened"
            except OSError as exc:
                result[name] = exc.errno
        print(json.dumps(result))
    """
    spec = make_spec(tmp_path, script, env_extra={"BAZARR_TEST_HOST_PID": str(os.getpid())})
    process = spawn_worker(spec, probe=mode)
    try:
        wanted = {"status": process._fds["status"], "lifeline": process._fds["lifeline"]}
        anchor = getattr(process, "_anchor", None)
        if anchor is not None and anchor.stdin is not None:
            wanted["anchor"] = anchor.stdin.fileno()
        (tmp_path / "rw" / "fds.json").write_text(json.dumps(wanted))
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, err
    finally:
        process.stop(0.5)
    seen = json.loads(out)
    assert set(seen) == set(wanted)
    # ENXIO: a socket cannot be opened by path. Any refusal is as good as that;
    # a successful open is the failure.
    assert all(value != "opened" for value in seen.values()), seen


@linux_only
def test_the_anchor_and_launcher_channels_are_sockets(tmp_path):
    process = spawn_worker(make_spec(tmp_path, "import time; time.sleep(30)"), probe=DECLARED)
    try:
        for name in ("status", "lifeline"):
            assert os.path.islink("/proc/self/fd/%d" % process._fds[name])
            assert os.readlink("/proc/self/fd/%d" % process._fds[name]).startswith("socket:")
    finally:
        process.stop(0.5)
    plain = spawn._start_plain([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if plain._anchor is not None:
            assert os.readlink("/proc/self/fd/%d" % plain._anchor.stdin.fileno()).startswith("socket:")
    finally:
        plain.stop(0.5)


@linux_only
def test_an_anchor_reaped_by_somebody_else_is_never_signalled(monkeypatch):
    """A direct waitpid elsewhere never updates the anchor's returncode: the probe must catch it."""
    plain = spawn._start_plain([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    anchor = plain._anchor
    if anchor is None:
        plain.stop(0.5)
        pytest.skip("no process group anchor on this interpreter")
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    try:
        os.kill(anchor.pid, signal.SIGKILL)
        os.waitpid(anchor.pid, 0)
        # Popen was not told: it still thinks the anchor is running.
        assert anchor.returncode is None
        assert plain.signal_group(signal.SIGCONT) is None
        assert plain._anchor_lost is True
        plain.stop(0.5)
        assert sent == [], sent
        assert plain.returncode is not None
    finally:
        monkeypatch.undo()
        plain.stop(0.5)


@linux_only
def test_a_launcher_that_cannot_fork_says_so_and_that_nothing_was_created(tmp_path):
    process, status = run_launcher_main(
        tmp_path,
        """
        def refuse():
            raise OSError(11, "Resource temporarily unavailable")

        os.fork = refuse
        """,
    )
    assert process.returncode == 78, process.stderr
    record, cleanup = status_records(status)
    assert record["kind"] == "sandbox" and "fork" in record["message"]
    assert cleanup == {"cleanup": "complete"}


# --- A fallback worker (no anchor, no waitid) keeps its leader until the group is ended -----

LEADER_WITH_CHILD = """
import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
print(child.pid, flush=True)
{body}
"""


def _fallback_leader(body):
    process = WorkerProcess(
        [sys.executable, "-c", LEADER_WITH_CHILD.format(body=body)], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    child = int(process.stdout.readline())
    return process, child


@linux_only
def test_a_worker_that_leaves_while_being_asked_to_does_not_leave_its_group_behind(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    process, child = _fallback_leader("signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\ntime.sleep(60)")
    try:
        time.sleep(0.3)  # the handler is installed
        process.stop(2.0)
        assert wait_dead(child), "a same-group child outlived the stop"
        assert process.returncode is not None
        assert process.group_cleaned is True
    finally:
        monkeypatch.undo()
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass


@linux_only
def test_a_leader_reaped_before_the_group_was_ended_is_reported_unconfirmed(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    process, child = _fallback_leader("pass")
    try:
        process.wait(timeout=10)  # the leader left on its own and was reaped
        process.stop(0.5)
        # The group number may be somebody else's now: nothing was sent, nothing is claimed.
        assert sent == []
        assert process.group_cleaned is False
        assert not wait_dead(child, seconds=0.3)
    finally:
        monkeypatch.undo()
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass


def test_the_limitations_say_that_deleted_files_may_not_stay_mapped_in_a_plugin_folder(monkeypatch):
    text = " ".join(sandbox_status(SandboxProbe(abi=9, verified=True, network_verified=True,
                                               signal_scope_verified=True, capabilities_dropped=True))["limitations"])
    assert "may not keep deleted files mapped inside its folders" in text
    assert "may not keep deleted files mapped inside its folders" in " ".join(sandbox_status(DECLARED)["limitations"])
    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    assert "may not keep deleted files mapped inside its folders" in " ".join(
        sandbox_status(DECLARED)["limitations"])


def test_the_limitations_say_that_a_memfd_may_not_stay_mapped_after_it_is_closed():
    text = " ".join(sandbox_status(SandboxProbe(abi=9, verified=True, network_verified=True,
                                               signal_scope_verified=True, capabilities_dropped=True))["limitations"])
    assert "may not keep a memfd mapped after closing it" in text
    assert "shared anonymous mappings" in text


# --- Ownership where there is no waitid ------------------------------------------------


@linux_only
def test_an_anchor_reaped_by_somebody_else_is_not_signalled_even_without_waitid(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    plain = spawn._start_plain([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    anchor = plain._anchor
    if anchor is None:
        plain.stop(0.5)
        pytest.skip("no process group anchor on this interpreter")
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    try:
        # While it is ours, /proc says so and the signal goes.
        assert plain.signal_group(signal.SIGCONT) is True
        os.kill(anchor.pid, signal.SIGKILL)
        os.waitpid(anchor.pid, 0)
        assert anchor.returncode is None  # Popen was not told
        sent.clear()
        assert plain.signal_group(signal.SIGCONT) is None
        assert plain._anchor_lost is True
        plain.stop(0.5)
        assert sent == [], sent
        assert plain.group_cleaned is False
    finally:
        monkeypatch.undo()
        plain.stop(0.5)


@linux_only
def test_a_leader_reaped_by_somebody_else_is_not_signalled_even_without_waitid(monkeypatch):
    monkeypatch.delattr(os, "waitid", raising=False)
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    process = WorkerProcess([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.kill(process.pid, signal.SIGKILL)
        os.waitpid(process.pid, 0)
        assert process.returncode is None
        assert process.signal_group(signal.SIGCONT) is None
        assert process._leader_lost is True
        assert sent == []
    finally:
        monkeypatch.undo()


def test_a_pid_that_answers_proves_nothing_when_neither_waitid_nor_proc_can_tell(monkeypatch):
    monkeypatch.setattr(spawn.os.path, "isdir", lambda path: False if path == "/proc/self" else os.path.exists(path))
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # something answers to the pid
    # A zombie, the child and a stranger all answer signal 0: unknown (waitpid on the Popen decides).
    assert spawn._owned_without_waitid(424242, None) is None

    def refused(pid, sig):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    # A pid that refuses signal 0 is somebody else's: unknown, and never signalled.
    monkeypatch.setattr(os, "kill", refused)
    assert spawn._owned_without_waitid(424242, None) is None

    def gone(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "kill", gone)
    assert spawn._owned_without_waitid(424242, None) is False


@linux_only
def test_a_pid_taken_by_a_later_process_is_not_ours_without_waitid(monkeypatch):
    mark = spawn._start_mark(os.getpid())
    assert mark is not None
    assert spawn._owned_without_waitid(os.getpid(), mark) is True
    assert spawn._owned_without_waitid(os.getpid(), str(int(mark) + 1)) is False


@linux_only
@pytest.mark.parametrize("anchored", [True, False])
def test_a_process_with_no_saved_identity_is_unknown_not_ours_without_waitid(monkeypatch, anchored):
    # The identity read at spawn failed, then somebody else reaped the worker and a stranger took its
    # pid. With no start time to compare, a live /proc entry proves nothing: no group signal, and
    # what the worker left behind stays unconfirmed.
    monkeypatch.delattr(os, "waitid", raising=False)
    monkeypatch.setattr(spawn, "_start_mark", lambda pid: None)
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if anchored:
        process = spawn._start_plain(command, **quiet)
        if process._anchor is None:
            process.stop(0.5)
            pytest.skip("no process group anchor on this interpreter")
        reaped = process._anchor.pid
    else:
        process = WorkerProcess(command, start_new_session=True, **quiet)
        reaped = process.pid
    assert process._started_mark is None
    pids = [process.pid] + ([process._anchor.pid] if anchored else [])
    try:
        os.killpg(reaped, signal.SIGKILL)
        os.waitpid(process.pid, 0)
        if anchored:
            os.waitpid(reaped, 0)
        sent.clear()
        # The pid now answers to a process that started later.
        monkeypatch.setattr(spawn, "_stat_fields", lambda pid: ("S", "999999999999"))
        assert process.probe_exit() in (None, "gone")
        assert process.signal_group(signal.SIGCONT) is None
        process.stop(0.5)
        assert [sig for _pgid, sig in sent if _pgid in pids] == [], sent
        assert process.group_cleaned is False
    finally:
        monkeypatch.undo()
        process.stop(0.5)


@linux_only
def test_a_stranger_on_the_pid_is_unknown_when_no_start_time_was_saved(monkeypatch):
    monkeypatch.setattr(spawn, "_stat_fields", lambda pid: ("S", "12345"))
    assert spawn._owned_without_waitid(os.getpid(), None) is None
    assert spawn._owned_without_waitid(os.getpid(), "12345") is True
    assert spawn._owned_without_waitid(os.getpid(), "54321") is False


def test_the_shm_state_is_none_where_there_is_no_statvfs(monkeypatch):
    monkeypatch.delattr(os, "statvfs", raising=False)
    assert spawn.shm_state() is None
    assert spawn.shm_guardable(spawn.shm_state()) is False
    assert "not limited per plugin" in " ".join(sandbox_status(DECLARED)["limitations"])


@linux_only
def test_stop_works_where_waitid_exists_but_cannot_leave_an_exit_unconsumed(monkeypatch):
    monkeypatch.delattr(os, "WNOWAIT", raising=False)
    process = WorkerProcess([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process.stop(1.0)
    assert process.returncode is not None and process.group_cleaned is True


# --- A waitid that fails for another reason than ECHILD says nothing about ownership -----------


def _failing_waitid(errno_value):
    def failing(*args, **kwargs):
        raise OSError(errno_value, os.strerror(errno_value))

    return failing


@linux_only
@pytest.mark.parametrize("code", [errno.EPERM, errno.EINVAL, errno.ENOSYS])
def test_an_anchor_reaped_by_somebody_else_is_not_signalled_when_waitid_fails_oddly(monkeypatch, code):
    plain = spawn._start_plain([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    anchor = plain._anchor
    if anchor is None:
        plain.stop(0.5)
        pytest.skip("no process group anchor on this interpreter")
    sent = []
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: (sent.append((pgid, sig)), real_killpg(pgid, sig))[1])
    try:
        monkeypatch.setattr(os, "waitid", _failing_waitid(code))
        # While the identity check still finds it, it is ours.
        assert plain.signal_group(signal.SIGCONT) is True
        os.kill(anchor.pid, signal.SIGKILL)
        os.waitpid(anchor.pid, 0)
        sent.clear()
        assert plain.signal_group(signal.SIGCONT) is None
        assert plain._anchor_lost is True
        plain.stop(0.5)
        assert sent == []
    finally:
        monkeypatch.undo()
        plain.stop(0.5)


@linux_only
@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOSYS])
def test_a_leader_that_a_stranger_now_holds_is_not_signalled_when_waitid_fails_oddly(monkeypatch, code):
    process = WorkerProcess([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    try:
        monkeypatch.setattr(os, "waitid", _failing_waitid(code))
        with process._own_lock:
            assert process._still_ours() is True
        # Same pid, alive, but it started later than the child did: a stranger.
        monkeypatch.setattr(spawn, "_stat_fields", lambda pid: ("S", "999999999999"))
        assert process.signal_group(signal.SIGCONT) is None
        assert process._leader_lost is True
        assert sent == []
        assert process.probe_exit() == "gone"
    finally:
        monkeypatch.undo()
        process.kill()
        process.wait()


@linux_only
def test_a_live_worker_is_still_ours_when_waitid_fails_and_nothing_else_can_tell(monkeypatch):
    process = WorkerProcess([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        monkeypatch.setattr(os, "waitid", _failing_waitid(errno.EPERM))
        monkeypatch.setattr(spawn.os.path, "isdir", lambda path: False if path == "/proc/self" else os.path.exists(path))
        with process._own_lock:
            assert process._still_ours() is True
        assert process._leader_lost is False
    finally:
        monkeypatch.undo()
        process.kill()
        process.wait()


@linux_only
@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOSYS])
def test_a_sigterm_ignoring_worker_is_still_killed_when_waitid_fails(monkeypatch, code):
    script = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = WorkerProcess([sys.executable, "-c", script], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    child = int(process.stdout.readline())
    time.sleep(0.3)  # SIGTERM is ignored from here on
    monkeypatch.setattr(os, "waitid", _failing_waitid(code))
    try:
        started = time.monotonic()
        process.stop(0.5)
        assert time.monotonic() - started < 4
        assert process.returncode is not None, "a worker that ignores SIGTERM outlived stop()"
        assert wait_dead(child), "its same-group child outlived stop()"
        assert process.group_cleaned is True
    finally:
        monkeypatch.undo()
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass


# ------------------------------------------------ /dev/shm: multiprocessing works under enforcement


@linux_only
def test_the_policy_grants_dev_shm_to_the_worker_and_the_launch_spec_carries_it(tmp_path):
    if not os.path.isdir("/dev/shm"):
        pytest.skip("this host has no /dev/shm")
    spec = make_spec(tmp_path, "pass")
    assert spec.policy.shared_memory == ("/dev/shm",)
    payload = json.loads(spec.to_launch_json(enforce=True, min_abi=1))
    assert [grant[0] if isinstance(grant, list) else grant for grant in payload["shared_memory"]] == ["/dev/shm"]
    assert "/dev/shm" not in spec.policy.read_write, "it is a shared-memory grant, not a data folder"


@linux_only
def test_an_enforced_worker_can_use_multiprocessing_primitives(tmp_path, live):
    if not os.path.isdir("/dev/shm") or not os.access("/dev/shm", os.W_OK):
        pytest.skip("this host has no writable /dev/shm")
    need_abi(live, 3)  # ftruncate on a shared-memory object needs the truncate right
    script = """
        import multiprocessing
        import multiprocessing.shared_memory as shared

        lock = multiprocessing.Lock()
        lock.acquire()
        lock.release()
        queue = multiprocessing.Queue()
        queue.put(7)
        assert queue.get(timeout=5) == 7
        segment = shared.SharedMemory(create=True, size=4096)
        segment.buf[0] = 9
        assert segment.buf[0] == 9
        segment.close()
        segment.unlink()
        print("ok")
    """
    result = run(make_spec(tmp_path, script), live)
    assert result[0] == 0 and result[1].strip().endswith("ok"), result
    # The grant is for shared memory only: no other file under /dev is writable, and nothing executes there.
    script = """
        import os
        try:
            os.open('/dev/bazarr-should-not-exist', os.O_CREAT | os.O_WRONLY)
        except OSError as error:
            print('denied', error.errno)
    """
    result = run(make_spec(tmp_path, script), live)
    assert result[1].startswith("denied"), result

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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


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
        import json
        print(json.dumps(open("/proc/%d/environ" % __import__("os").getppid()).read()))
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
            self._fds = {"lifeline": None, "status": None}
            self.pid = 4242
            self.term_grace = 3.0
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def __del__(self):
            pass

    stub = Stub()

    def fake_wait(self, timeout=None):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("launcher", timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fake_wait)
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

    monkeypatch.setattr(package, "sandbox_status", lambda: sandbox_status(DECLARED))
    data = _status_payload(monkeypatch, {})
    assert data["sandbox"]["label"] == "Not sandboxed on this host"
    assert data["sandbox"]["enforced"] is False

    def broken():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(package, "sandbox_status", broken)
    assert "sandbox" not in _status_payload(monkeypatch, {})


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
        PR_SET_PDEATHSIG = PR_SET_NO_NEW_PRIVS = 1

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
        record = json.loads(os.read(read_fd, 4096))
    finally:
        os.close(read_fd)
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
        launcher._descendants(os.getpid())
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
def test_cleanup_falls_back_to_killing_the_group_when_the_scan_fails(monkeypatch):
    launcher = load_launcher()
    reasons = []

    def broken(root):
        raise launcher.EnumerationError("no descriptors")

    monkeypatch.setattr(launcher, "_descendants", broken)
    monkeypatch.setattr(launcher, "_emergency_kill", lambda reason: reasons.append(str(reason)))
    launcher._kill_descendants()
    launcher._cleanup_tree()
    assert reasons == ["no descriptors", "no descriptors"]


@linux_only
def test_a_descendant_that_survives_the_cleanup_is_reported_not_forgotten(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setattr(launcher, "CLEANUP_SECONDS", 0.3)
    monkeypatch.setattr(launcher, "_descendants", lambda root: [999999])
    monkeypatch.setattr(launcher, "_kill_descendants", lambda sig=9: None)
    assert launcher._cleanup_tree() is False
    monkeypatch.setattr(launcher, "_descendants", lambda root: [])
    assert launcher._cleanup_tree() is True


@linux_only
def test_the_tree_is_frozen_before_it_is_killed(monkeypatch):
    launcher = load_launcher()
    scans = iter([[10, 11], [10, 11, 12], [10, 11, 12], [10, 11, 12, 13]])
    sent = []
    monkeypatch.setattr(launcher, "_descendants", lambda root: next(scans))
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    launcher._kill_descendants()
    stops = [pid for pid, sig in sent if sig == signal.SIGSTOP]
    kills = [pid for pid, sig in sent if sig == signal.SIGKILL]
    assert stops == [10, 11, 12]
    assert kills == [10, 11, 12, 13]
    assert sent.index((12, signal.SIGSTOP)) < sent.index((10, signal.SIGKILL))


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


@linux_only
def test_the_emergency_kill_ends_the_whole_group_and_reports_why(tmp_path):
    status_r, status_w = os.pipe()
    code = textwrap.dedent(
        """
        import importlib.util, os, subprocess, sys, time
        spec = importlib.util.spec_from_file_location("l", %r)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        launcher._STATUS_FD = %d
        sleeper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(300)"])
        print(sleeper.pid, flush=True)
        launcher._emergency_kill("test")
        """
    ) % (str(spawn.LAUNCHER_PATH), status_w)
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, start_new_session=True, pass_fds=(status_w,)
    )
    os.close(status_w)
    try:
        sleeper = int(process.stdout.readline())
        assert process.wait(timeout=20) == -9
        assert wait_dead(sleeper), "a member of the process group survived"
        record = json.loads(os.read(status_r, 4096).decode())
        assert record["kind"] == "sandbox" and "process tree" in record["message"]
    finally:
        os.close(status_r)
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass


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

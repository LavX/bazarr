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
import errno
import gc
import importlib.util
import json
import os
import shutil
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

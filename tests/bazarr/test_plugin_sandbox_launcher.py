# coding=utf-8
"""Sandbox launcher, Landlock rules, probe and fallback policy.

Pure policy and mask tests run everywhere. Tests that need the kernel's
Landlock skip with a reason where it is missing or too old; tests that only
exercise the launcher's limits, environment and process handling also run in
the "declared" mode on any Linux host.
"""
import errno
import gc
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from provider_hub.sandbox import (
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
    spawn,
    spawn_worker,
)

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")

DECLARED = SandboxProbe(abi=0, verified=False, reason="test: no Landlock")


@pytest.fixture(scope="module")
def live():
    """The real probe of this machine, skipping where Landlock is unusable."""
    if not sys.platform.startswith("linux"):
        pytest.skip("not Linux")
    probe = probe_sandbox(refresh=True)
    if not probe.enforced:
        pytest.skip("Landlock not enforceable here: %s" % probe.reason)
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
    with pytest.raises(PolicyError, match="system path"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=["/etc"])


def test_policy_refuses_nested_writable_grants(tmp_path):
    (tmp_path / "rw" / "inner").mkdir(parents=True)
    with pytest.raises(PolicyError, match="nested"):
        SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "rw", tmp_path / "rw" / "inner"])
    other = tmp_path / "other"
    other.mkdir()
    SandboxPolicy.for_interpreter(sys.executable, read_write=[tmp_path / "rw", other])


def test_policy_requires_existing_writable_directories(tmp_path):
    with pytest.raises(PolicyError, match="existing directory"):
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
    spec = make_spec(tmp_path, "pass")
    payload = json.loads(spec.to_launch_json(enforce=True, min_abi=1))
    assert [str(real), info.st_dev, info.st_ino] in payload["read_write"] or any(
        isinstance(g, list) and g[0] == str(spec.policy.read_write[0]) for g in payload["read_write"]
    )


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
    for name in ("write_outside", "read_config", "write_db", "overwrite_config", "list_outside", "mkdir_outside"):
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
    # EXDEV. shutil.move must then copy and delete.
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
        shutil.move("a/g", "c/g")
        out["moved"] = os.path.exists("c/g") and not os.path.exists("a/g")
        out["content"] = open("c/g").read()
        print(json.dumps(out))
    """
    rc, out, err = run(make_spec(tmp_path, script, abi_cap=1), live)
    assert rc == 0, err
    assert json.loads(out) == {"rename": errno.EXDEV, "same_dir_rename": True, "moved": True, "content": "1"}


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

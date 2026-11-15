# coding=utf-8
"""Boot sweep of launcher process groups a previous run left behind.

Real processes throughout: a leftover is a tree whose owner (the process named in its
marker) is gone, started in a session of its own and orphaned the way a hard kill of the
host orphans a worker. The tests assert what dies, and just as much what does not.
"""
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from provider_hub.sandbox import Limits, RuntimeSpec, SandboxPolicy, SandboxProbe, clean_env, spawn, spawn_worker, sweep

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.path.isdir("/proc/self")), reason="the sweep reads /proc"
)

DECLARED = SandboxProbe(abi=0, verified=False, reason="test: no Landlock")
SLEEP = shutil.which("sleep") or "/bin/sleep"
ENV = shutil.which("env") or "/usr/bin/env"

# A tree: A forks B and leaves, so B is orphaned and becomes a session leader. B forks a
# plain child, a child that drops its environment (so it carries no marker), and a child
# that starts a session of its own. "dead-leader" has B leave too. "bare-leader" starts B
# without the marker and gives only its plain child one.
TREE = textwrap.dedent(
    """
    import json, os, signal, sys, time
    out, mode, sleeper, env, marker = sys.argv[1:6]
    if os.fork():
        os._exit(0)
    os.setsid()
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pids = {"leader": os.getpid()}
    child = os.fork()
    if child == 0:
        if mode == "bare-leader":
            os.execve(sleeper, [sleeper, "300"], {"BAZARR_SANDBOX_OWNER": marker})
        time.sleep(300)
        os._exit(0)
    pids["child"] = child
    bare = os.fork()
    if bare == 0:
        os.execve(env, [env, "-i", sleeper, "300"], {})
    pids["bare"] = bare
    own = os.fork()
    if own == 0:
        os.setsid()
        time.sleep(300)
        os._exit(0)
    pids["own_session"] = own
    with open(out + ".tmp", "w") as handle:
        json.dump(pids, handle)
    os.rename(out + ".tmp", out)
    if mode != "dead-leader":
        time.sleep(300)
    os._exit(0)
    """
)


def state(pid):
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None


def alive(pid):
    return state(pid) not in (None, "Z", "X")


def wait_until(check, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


@pytest.fixture(autouse=True)
def no_owner_marker():
    spawn.set_owner_marker(None)
    yield
    spawn.set_owner_marker(None)


@pytest.fixture
def instance(tmp_path):
    return sweep.instance_key(tmp_path / "config")


@pytest.fixture
def started(request):
    """Every pid a test saw, killed at the end whatever the test did."""
    pids = []
    popens = []
    yield pids, popens
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for popen in popens:
        try:
            popen.kill()
        except OSError:
            pass
        popen.wait()


def dead_owner(instance_name):
    """A marker naming a host process that no longer exists, with its real start time."""
    process = subprocess.Popen([SLEEP, "60"])
    entry = sweep._read_proc(process.pid)
    process.kill()
    process.wait()
    assert not os.path.exists("/proc/%d" % process.pid)
    return "%s:%d:%s" % (instance_name, process.pid, entry.start)


def make_tree(tmp_path, started, marker, mode="live", name="tree"):
    pids, _popens = started
    out = tmp_path / (name + ".json")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if marker is not None and mode != "bare-leader":
        env[spawn.OWNER_VARIABLE] = marker
    subprocess.run(
        [sys.executable, "-c", TREE, str(out), mode, SLEEP, ENV, marker or ""], env=env, check=True, timeout=30
    )
    assert wait_until(out.exists), "the tree script did not report"
    seen = json.loads(out.read_text())
    pids.extend(seen.values())
    return seen


def all_dead(seen, names=None):
    return all(not alive(pid) for key, pid in seen.items() if names is None or key in names)


def test_a_session_whose_leader_is_gone_is_left_alone_but_a_detached_leader_is_ended(tmp_path, instance, started):
    seen = make_tree(tmp_path, started, dead_owner(instance), mode="dead-leader")
    assert wait_until(lambda: not alive(seen["leader"]))
    assert alive(seen["child"]) and alive(seen["bare"]) and alive(seen["own_session"])
    result = sweep.sweep_leftovers(instance=instance, term_wait=1.0)
    assert result.supported
    assert wait_until(lambda: not alive(seen["own_session"])), "the detached grandchild leads its own session"
    time.sleep(0.3)
    assert alive(seen["child"]) and alive(seen["bare"]), "nothing ties a leaderless session's members together"
    outcomes = {item.session: item for item in result.sessions}
    assert outcomes[seen["own_session"]].outcome == "ended"
    assert "leader" in outcomes[seen["leader"]].detail and outcomes[seen["leader"]].outcome == "refused"


def test_the_sweep_ends_a_live_leftover_launcher_with_its_whole_tree(tmp_path, instance, started):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    assert alive(seen["leader"])
    result = sweep.sweep_leftovers(instance=instance, term_wait=5.0)
    assert wait_until(lambda: all_dead(seen)), {k: state(v) for k, v in seen.items()}
    assert all(item.outcome == "ended" for item in result.sessions)


def test_a_leader_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, instance, started):
    seen = make_tree(tmp_path, started, dead_owner(instance), mode="ignore-term")
    began = time.monotonic()
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert time.monotonic() - began < 10
    assert wait_until(lambda: all_dead(seen)), {k: state(v) for k, v in seen.items()}
    assert all(item.outcome == "ended" for item in result.sessions)


def test_a_member_without_the_marker_dies_with_its_proved_session(tmp_path, instance, started):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    assert sweep._read_marker(seen["bare"]) is None
    sweep.sweep_leftovers(instance=instance, term_wait=1.0)
    assert wait_until(lambda: not alive(seen["bare"]))


def test_a_marker_naming_a_running_owner_is_left_alone(tmp_path, instance, started):
    owner = subprocess.Popen([SLEEP, "60"])
    started[1].append(owner)
    marker = "%s:%d:%s" % (instance, owner.pid, sweep._read_proc(owner.pid).start)
    seen = make_tree(tmp_path, started, marker)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    time.sleep(0.3)
    assert all(alive(pid) for pid in seen.values())
    assert result.ended == 0 and {item.outcome for item in result.sessions} == {"refused"}


def test_the_sweep_never_touches_the_process_that_runs_it(tmp_path, instance, started):
    marker = "%s:%d:%s" % (instance, os.getpid(), sweep._read_proc(os.getpid()).start)
    seen = make_tree(tmp_path, started, marker)
    sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert all(alive(pid) for pid in seen.values())


def test_an_owner_pid_taken_by_another_process_is_not_proof_the_owner_is_gone(tmp_path, instance, started):
    """Same pid as a live process, another start time: the number answers signal 0, so it is unknown."""
    start = sweep._read_proc(os.getpid()).start
    stale = "%s:%d:%d" % (instance, os.getpid(), int(start) - 1000)
    assert sweep._owner_state(stale) == "unknown"
    seen = make_tree(tmp_path, started, stale)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    time.sleep(0.3)
    assert all(alive(pid) for pid in seen.values())
    assert result.ended == 0


def test_an_owner_is_gone_only_when_signal_zero_says_there_is_no_such_process(tmp_path, instance, started, monkeypatch):
    marker = dead_owner(instance)
    assert sweep._owner_state(marker) == "gone"
    real_kill = os.kill

    def kill(pid, sig):
        if sig == 0:
            return None  # something answers to the pid
        pytest.fail("signalled by number")

    monkeypatch.setattr(os, "kill", kill)
    assert sweep._owner_state(marker) == "unknown"
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))
    assert sweep._owner_state(marker) == "unknown"
    monkeypatch.setattr(os, "kill", real_kill)


def test_another_installs_leftovers_are_not_ours(tmp_path, instance, started):
    other = sweep.instance_key(tmp_path / "another-config")
    seen = make_tree(tmp_path, started, dead_owner(other))
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    time.sleep(0.3)
    assert all(alive(pid) for pid in seen.values())
    assert result.sessions == []


def test_a_process_with_no_marker_is_never_touched(tmp_path, instance, started):
    seen = make_tree(tmp_path, started, None)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    time.sleep(0.3)
    assert all(alive(pid) for pid in seen.values())
    assert result.sessions == []


@pytest.mark.parametrize("marker", ["", "garbage", "a:b:c", "%s:12" % ("0" * 16), "x:1:2:3"])
def test_a_malformed_marker_is_not_ours(tmp_path, instance, started, marker):
    seen = make_tree(tmp_path, started, marker)
    sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    time.sleep(0.2)
    assert all(alive(pid) for pid in seen.values())


def test_without_an_instance_nothing_is_signalled(monkeypatch):
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers()
    assert not result.supported and result.sessions == [] and signals.sent == []


def test_a_child_of_this_process_is_never_swept_and_never_reaped(tmp_path, instance, started):
    """A child of ours is a live worker of this run: its stale-looking marker does not make it a leftover."""
    env = {"PATH": os.environ.get("PATH", ""), spawn.OWNER_VARIABLE: dead_owner(instance)}
    child = subprocess.Popen([SLEEP, "60"], env=env, start_new_session=True)
    started[1].append(child)
    sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert child.poll() is None, "the child was signalled or reaped"
    assert alive(child.pid)


def test_a_process_in_this_session_is_never_swept(tmp_path, instance, started):
    env = {"PATH": os.environ.get("PATH", ""), spawn.OWNER_VARIABLE: dead_owner(instance)}
    child = subprocess.Popen([SLEEP, "60"], env=env)  # same session and group as the test
    started[1].append(child)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert child.poll() is None and result.ended == 0


class Signals:
    """Records every signal the sweep sends, by pid and signal, and forbids signalling by number."""

    def __init__(self, monkeypatch):
        self.sent = []
        real_send = signal.pidfd_send_signal

        def send(fd, sig, *args, **kwargs):
            with open("/proc/self/fdinfo/%d" % fd) as handle:
                pid = int(next(line for line in handle if line.startswith("Pid:")).split()[1])
            self.sent.append((pid, sig))
            return real_send(fd, sig, *args, **kwargs)

        monkeypatch.setattr(signal, "pidfd_send_signal", send)
        monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("signalled a group by number"))
        real_kill = os.kill
        monkeypatch.setattr(
            os, "kill", lambda pid, sig: real_kill(pid, sig) if sig == 0 else pytest.fail("signalled by number")
        )

    def pids(self):
        return {pid for pid, _sig in self.sent}


def test_signals_go_through_pidfds_to_proved_members_only(tmp_path, instance, started, monkeypatch):
    bystander = subprocess.Popen([SLEEP, "60"], start_new_session=True)
    started[1].append(bystander)
    seen = make_tree(tmp_path, started, dead_owner(instance))
    signals = Signals(monkeypatch)
    sweep.sweep_leftovers(instance=instance, term_wait=2.0)
    assert wait_until(lambda: all_dead(seen))
    assert signals.pids() <= set(seen.values())
    assert [sig for pid, sig in signals.sent if pid == seen["leader"]][0] == signal.SIGTERM
    assert {sig for _pid, sig in signals.sent} <= {signal.SIGTERM, signal.SIGKILL}
    assert bystander.poll() is None and os.getpgrp() not in signals.pids()


class Reuse:
    """Makes a pid look, from the moment ``pidfd_open`` is first called, like another process."""

    def __init__(self, monkeypatch, pid, **changes):
        self.armed = False
        real_open = os.pidfd_open
        real_read = sweep._read_proc

        def opened(target, *args, **kwargs):
            # The sweep's own capability probe pins this process; the changes start with the first real pin.
            self.armed = self.armed or target != os.getpid()
            return real_open(target, *args, **kwargs)

        def read(target):
            entry = real_read(target)
            if self.armed and target == pid and entry is not None:
                return type(entry)(**{**entry.__dict__, **changes})
            return entry

        monkeypatch.setattr(os, "pidfd_open", opened)
        monkeypatch.setattr(sweep, "_read_proc", read)


def test_a_leader_that_is_another_process_by_the_time_it_is_pinned_gets_no_signal(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    Reuse(monkeypatch, seen["leader"], start="1")
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.2)
    assert signals.pids() & {seen["leader"], seen["child"], seen["bare"]} == set()
    assert any(item.outcome == "refused" for item in result.sessions)
    assert all(alive(seen[key]) for key in ("leader", "child", "bare"))


def test_a_member_that_changed_session_by_the_time_it_is_pinned_gets_no_signal(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    Reuse(monkeypatch, seen["child"], session=1)
    signals = Signals(monkeypatch)
    sweep.sweep_leftovers(instance=instance, term_wait=2.0)
    assert wait_until(lambda: not alive(seen["leader"]))
    assert seen["child"] not in signals.pids() and alive(seen["child"])


def test_an_unmarked_member_that_is_another_process_by_the_time_it_is_pinned_gets_no_signal(
    tmp_path, instance, started, monkeypatch
):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    assert sweep._read_marker(seen["bare"]) is None
    Reuse(monkeypatch, seen["bare"], start="1")
    signals = Signals(monkeypatch)
    sweep.sweep_leftovers(instance=instance, term_wait=2.0)
    assert wait_until(lambda: not alive(seen["leader"]))
    assert seen["bare"] not in signals.pids() and alive(seen["bare"])


def test_a_member_whose_marker_changed_by_the_time_it_is_pinned_gets_no_signal(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    real_marker = sweep._read_marker
    state = {"armed": False}
    real_open = os.pidfd_open

    def opened(target, *args, **kwargs):
        state["armed"] = state["armed"] or target != os.getpid()
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(os, "pidfd_open", opened)
    monkeypatch.setattr(
        sweep, "_read_marker", lambda pid: "other:1:2" if state["armed"] and pid == seen["child"] else real_marker(pid)
    )
    signals = Signals(monkeypatch)
    sweep.sweep_leftovers(instance=instance, term_wait=2.0)
    assert seen["child"] not in signals.pids() and alive(seen["child"])


def test_a_pid_reused_between_the_stat_and_the_environment_read_is_skipped(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    real_marker = sweep._read_marker
    real_read = sweep._read_proc
    state = {"swap": False}

    def marker(pid):
        value = real_marker(pid)
        state["swap"] = True  # from here every pid reads as a process that started later
        return value

    def read(pid):
        entry = real_read(pid)
        if state["swap"] and entry is not None:
            return type(entry)(**{**entry.__dict__, "start": entry.start + "9"})
        return entry

    monkeypatch.setattr(sweep, "_read_marker", marker)
    monkeypatch.setattr(sweep, "_read_proc", read)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and result.sessions == []
    assert all(alive(pid) for pid in seen.values())


def test_a_process_in_another_pid_namespace_is_not_swept(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    assert sweep._same_pid_namespace(os.getpid())
    monkeypatch.setattr(sweep, "_same_pid_namespace", lambda pid: False)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and result.sessions == []
    assert all(alive(pid) for pid in seen.values())


def test_a_live_leader_that_shows_its_environment_without_the_marker_is_refused(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance), mode="bare-leader")
    assert sweep._read_marker(seen["leader"]) is None and sweep._read_marker(seen["child"]) is not None
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.pids() & {seen["leader"], seen["child"]} == set()
    assert any("no marker" in item.detail for item in result.sessions)
    assert alive(seen["leader"]) and alive(seen["child"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a non-dumpable process's environment")
def test_a_hidden_environment_is_never_read_and_never_swept(tmp_path, instance, started, monkeypatch):
    """A process that made itself non-dumpable cannot show a marker, so it proves nothing."""
    out = tmp_path / "hidden.pid"
    script = (
        "import ctypes, os, sys, time\n"
        "ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)\n"
        "os.setsid()\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(300)\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), spawn.OWNER_VARIABLE: dead_owner(instance)}
    child = subprocess.Popen([sys.executable, "-c", script, str(out)], env=env)
    started[1].append(child)
    assert wait_until(out.exists)
    with pytest.raises(PermissionError):
        sweep._read_marker(child.pid)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and result.sessions == [] and child.poll() is None


def test_a_permission_error_on_the_environment_counts_as_unreadable(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))

    def denied(pid):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(sweep, "_read_marker", denied)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and result.sessions == [] and result.unreadable > 0
    assert all(alive(pid) for pid in seen.values())


@pytest.mark.parametrize("cut", ["time", "entries"])
def test_a_partial_scan_signals_nothing(tmp_path, instance, started, monkeypatch, cut):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    if cut == "time":
        monkeypatch.setattr(sweep, "SCAN_BUDGET_SECONDS", -1.0)
    else:
        monkeypatch.setattr(sweep, "MAX_PROC_ENTRIES", 5)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert result.truncated and "partial" in result.reason
    assert signals.sent == [] and result.sessions == []
    assert all(alive(pid) for pid in seen.values())


def test_a_host_without_proc_marks_nothing_and_signals_nothing(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    monkeypatch.setattr(sweep, "_has_proc", lambda: False)
    signals = Signals(monkeypatch)
    assert sweep.configure_owner(tmp_path) is None
    assert spawn.owner_marker() is None
    result = sweep.sweep_leftovers(tmp_path / "config")
    assert not result.supported and "/proc" in result.reason and result.sessions == []
    assert signals.sent == []
    assert all(alive(pid) for pid in seen.values())


def test_a_host_without_pidfds_does_nothing(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    monkeypatch.delattr(os, "pidfd_open")
    monkeypatch.setattr(os, "kill", lambda pid, sig: None if sig == 0 else pytest.fail("signalled by number"))
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("signalled a group"))
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert not result.supported and "pidfd" in result.reason and result.sessions == []
    monkeypatch.undo()
    assert all(alive(pid) for pid in seen.values())


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EPERM, errno.EINVAL])
def test_a_kernel_that_refuses_pidfd_open_does_nothing(tmp_path, instance, started, monkeypatch, code):
    seen = make_tree(tmp_path, started, dead_owner(instance))

    def refuse(*args, **kwargs):
        raise OSError(code, "refused")

    monkeypatch.setattr(os, "pidfd_open", refuse)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert not result.supported and signals.sent == []
    assert all(alive(pid) for pid in seen.values())


def test_the_sweep_has_no_numeric_signal_calls_in_its_source():
    import inspect

    code = inspect.getsource(sweep)
    body = code.split('"""', 2)[2]
    assert "killpg(" not in body and "os.kill(" not in body.replace("os.kill(pid, 0)", "")


def test_an_unreadable_proc_listing_signals_nothing(tmp_path, instance, monkeypatch):
    monkeypatch.setattr(sweep, "_list_pids", lambda: None)
    Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance)
    assert not result.supported


def test_a_failure_inside_the_sweep_is_contained(instance, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sweep, "_scan", boom)
    assert sweep.sweep_leftovers(instance=instance).sessions == []


def test_the_instance_key_follows_the_config_folder(tmp_path):
    first = sweep.instance_key(tmp_path / "a")
    assert first == sweep.instance_key(str(tmp_path / "a") + "/")
    assert first != sweep.instance_key(tmp_path / "b")
    link = tmp_path / "link"
    (tmp_path / "a").mkdir()
    link.symlink_to(tmp_path / "a")
    assert sweep.instance_key(link) == first


def test_configure_owner_names_this_process(tmp_path):
    marker = sweep.configure_owner(tmp_path)
    assert marker == spawn.owner_marker()
    key, pid, start = marker.split(":")
    assert key == sweep.instance_key(tmp_path) and int(pid) == os.getpid()
    assert start == sweep._read_proc(os.getpid()).start
    assert sweep._owner_state(marker) == "alive"


def worker_spec(tmp_path, script):
    rw = tmp_path / "rw"
    rw.mkdir(exist_ok=True)
    return RuntimeSpec(
        interpreter=sys.executable,
        args=("-I", "-c", textwrap.dedent(script)),
        cwd=str(rw),
        env=clean_env(home=rw, tmpdir=rw),
        policy=SandboxPolicy.for_interpreter(sys.executable, read_write=[rw]),
        limits=Limits(),
        term_grace=1.0,
    )


def test_workers_carry_the_marker_once_configured(tmp_path):
    marker = sweep.configure_owner(tmp_path / "config")
    script = "import os, sys, time\nprint(os.environ.get(%r), flush=True)\ntime.sleep(30)\n" % spawn.OWNER_VARIABLE
    process = spawn_worker(worker_spec(tmp_path, script), probe=DECLARED)
    try:
        line = process.stdout.readline().decode().strip()
        assert line == marker, "the worker lost the marker"
    finally:
        process.stop(0.5)


def test_nothing_is_marked_until_the_host_is_configured(tmp_path):
    script = "import os\nprint(os.environ.get(%r))\n" % spawn.OWNER_VARIABLE
    process = spawn_worker(worker_spec(tmp_path, script), probe=DECLARED)
    try:
        out, _err = process.communicate(timeout=30)
        assert out.decode().strip() == "None"
    finally:
        process.stop(0.5)


def test_a_live_run_is_never_swept_by_its_own_boot_sweep(tmp_path):
    """The owner is this process and alive, so this run's workers are refused, not killed."""
    sweep.configure_owner(tmp_path / "config")
    script = "import time\nprint('up', flush=True)\ntime.sleep(60)\n"
    process = spawn_worker(worker_spec(tmp_path, script), probe=DECLARED)
    try:
        assert process.stdout.readline().decode().strip() == "up"
        result = sweep.sweep_leftovers(tmp_path / "config", term_wait=0.5)
        time.sleep(0.3)
        assert process.poll() is None and alive(process.pid)
        assert result.ended == 0
    finally:
        process.stop(0.5)


@pytest.mark.parametrize("how", ["launcher-killed", "launcher-stopped", "launcher-stopped-seen-as-root"])
def test_a_killed_host_leaves_a_launcher_tree_that_the_next_boot_removes(tmp_path, monkeypatch, how):
    """End to end: a host that owns a launcher dies hard, a new run sweeps what it left.

    ``launcher-stopped`` is the production case: the launcher is alive (stopped, so it cannot
    see the lifeline close) and, being non-dumpable, shows no environment to a user that is not
    root. ``seen-as-root`` has the sweep read that launcher's environment as an empty one, which
    is what root sees.
    """
    config = tmp_path / "config"
    pids_file = tmp_path / "pids.json"
    host = textwrap.dedent(
        """
        import json, os, subprocess, sys, textwrap, time
        from provider_hub.sandbox import (Limits, RuntimeSpec, SandboxPolicy, SandboxProbe, clean_env, spawn_worker, sweep)
        config, out, rw = sys.argv[1:4]
        sweep.configure_owner(config)
        policy = SandboxPolicy.for_interpreter(sys.executable, read_write=[rw])
        worker = (
            "import os, time\\nkid = os.fork()\\nif kid == 0:\\n    os.setsid()\\n    time.sleep(300)\\n"
            "print(os.getpid(), kid, flush=True)\\ntime.sleep(300)\\n"
        )
        spec = RuntimeSpec(sys.executable, ("-I", "-c", worker), rw, clean_env(home=rw, tmpdir=rw), policy, Limits(), 1.0)
        process = spawn_worker(spec, probe=SandboxProbe(abi=0, verified=False, reason="test"))
        worker_pid, detached = (int(item) for item in process.stdout.readline().split())
        with open(out, "w") as handle:
            json.dump({"host": os.getpid(), "launcher": process.pid, "worker": worker_pid, "detached": detached}, handle)
        time.sleep(300)
        """
    )
    (tmp_path / "rw").mkdir()
    code_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(spawn.__file__))))
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": code_root}
    process = subprocess.Popen(
        [sys.executable, "-c", host, str(config), str(pids_file), str(tmp_path / "rw")], env=env, start_new_session=True
    )
    pids = {}
    try:
        assert wait_until(pids_file.exists, 30), "the host did not start a launcher"
        pids = json.loads(pids_file.read_text())
        assert alive(pids["launcher"]) and alive(pids["worker"]) and alive(pids["detached"])
        if how == "launcher-killed":
            # A hard kill of the host and of its launcher: no cleanup runs anywhere, and the worker's
            # child that started a session of its own is reparented and keeps running.
            os.kill(process.pid, signal.SIGKILL)
            process.wait()
            os.kill(pids["launcher"], signal.SIGKILL)
            assert wait_until(lambda: not alive(pids["launcher"]) and not alive(pids["worker"]))
            assert alive(pids["detached"]), "the detached child should outlive its launcher"
            result = sweep.sweep_leftovers(config, term_wait=1.0)
            assert wait_until(lambda: not alive(pids["detached"])), result
            assert result.ended == 1
            return
        os.kill(pids["launcher"], signal.SIGSTOP)
        assert wait_until(lambda: state(pids["launcher"]) == "T")
        os.kill(process.pid, signal.SIGKILL)
        process.wait()
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                sweep._read_marker(pids["launcher"])  # really unreadable: it is non-dumpable
        if how == "launcher-stopped-seen-as-root":
            real = sweep._read_marker
            monkeypatch.setattr(sweep, "_read_marker", lambda pid: None if pid == pids["launcher"] else real(pid))
        assert alive(pids["launcher"]) and alive(pids["worker"]) and alive(pids["detached"])
        result = sweep.sweep_leftovers(config, term_wait=1.0)
        gone = wait_until(lambda: not any(alive(pids[key]) for key in ("launcher", "worker", "detached")))
        assert gone, ({k: state(v) for k, v in pids.items()}, result)
        assert result.ended == 2 and {item.outcome for item in result.sessions} == {"ended"}
    finally:
        for pid in pids.values():
            try:
                os.kill(pid, signal.SIGCONT)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if process.poll() is None:
            process.kill()
            process.wait()


def _nondumpable_leader_tree(tmp_path, started, marker, name="hidden"):
    """A session leader that hides its environment, with one marked worker below it."""
    out = tmp_path / (name + ".json")
    script = textwrap.dedent(
        """
        import ctypes, json, os, sys, time
        out, sleeper, marker = sys.argv[1:4]
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
        if os.fork():
            os._exit(0)
        os.setsid()
        kid = os.fork()
        if kid == 0:
            os.execve(sleeper, [sleeper, "300"], {"BAZARR_SANDBOX_OWNER": marker})
        with open(out, "w") as handle:
            json.dump({"leader": os.getpid(), "worker": kid}, handle)
        time.sleep(300)
        """
    )
    env = {"PATH": os.environ.get("PATH", "")}
    subprocess.run([sys.executable, "-c", script, str(out), SLEEP, marker], env=env, check=True, timeout=30)
    assert wait_until(out.exists)
    seen = json.loads(out.read_text())
    started[0].extend(seen.values())
    return seen


def test_a_hidden_leader_that_is_not_the_launcher_is_refused(tmp_path, instance, started, monkeypatch):
    seen = _nondumpable_leader_tree(tmp_path, started, dead_owner(instance))
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and alive(seen["leader"]) and alive(seen["worker"])
    assert any("launcher" in item.detail for item in result.sessions)


def test_an_unreadable_command_line_is_a_refusal(tmp_path, instance, started, monkeypatch):
    """No command name or process age stands in for the launcher's argv."""
    seen = _nondumpable_leader_tree(tmp_path, started, dead_owner(instance))
    monkeypatch.setattr(sweep, "_read_cmdline", lambda pid: None)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and alive(seen["leader"]) and alive(seen["worker"])
    assert any("launcher" in item.detail for item in result.sessions)


def test_a_hidden_leader_with_the_launcher_argv_is_swept(tmp_path, instance, started, monkeypatch):
    seen = _nondumpable_leader_tree(tmp_path, started, dead_owner(instance))
    real = sweep._read_cmdline
    argv = ["python3", "-I", "-S", "-B", str(spawn.LAUNCHER_PATH), "5", "6", "7"]
    monkeypatch.setattr(sweep, "_read_cmdline", lambda pid: argv if pid == seen["leader"] else real(pid))
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert wait_until(lambda: not alive(seen["leader"]) and not alive(seen["worker"])), result


def test_a_leader_of_another_user_is_not_a_launcher(tmp_path, instance, started, monkeypatch):
    seen = _nondumpable_leader_tree(tmp_path, started, dead_owner(instance))
    real_cmdline = sweep._read_cmdline
    argv = ["python3", "-I", "-S", "-B", str(spawn.LAUNCHER_PATH)]
    monkeypatch.setattr(sweep, "_read_cmdline", lambda pid: argv if pid == seen["leader"] else real_cmdline(pid))
    real = sweep._uids_of
    monkeypatch.setattr(sweep, "_uids_of", lambda pid: (12345, 12345) if pid == seen["leader"] else real(pid))
    signals = Signals(monkeypatch)
    sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert signals.sent == [] and alive(seen["leader"]) and alive(seen["worker"])


def test_the_launcher_argv_is_recognised_by_its_script():
    launcher = str(spawn.LAUNCHER_PATH)
    assert sweep._is_launcher_argv(["python3", "-I", "-S", "-B", launcher, "5", "6", "7"])
    assert sweep._is_launcher_argv(["/opt/x/provider_hub/sandbox/launcher.py"])
    assert not sweep._is_launcher_argv(["python3", "-c", "import time"])
    assert not sweep._is_launcher_argv(["sshd", "-D"])


def test_a_status_uid_is_read_from_status_not_from_the_proc_directory(tmp_path):
    assert sweep._uids_of(os.getpid()) == (os.geteuid(), os.geteuid()) or os.getuid() != os.geteuid()
    assert sweep._uids_of(2**30) is None


@pytest.mark.parametrize(
    "value",
    [
        "x:99999999999999999999:5",
        "x:4194305:5",
        "x:0:5",
        "x:1:" + "9" * 21,
        "x:\u0663:5",
        "x:%d:5" % (2**64),
        ":1:5",
        "a" * 65 + ":1:5",
    ],
)
def test_an_oversized_or_odd_marker_is_unknown(value):
    assert sweep._parse_marker(value) is None
    assert sweep._owner_state(value) == "unknown"


def test_an_owner_check_that_overflows_is_unknown(monkeypatch):
    monkeypatch.setattr(sweep, "_parse_marker", lambda value: ("x", 2**70, "5"))
    assert sweep._owner_state("x:1:5") == "unknown"
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(OverflowError()))
    monkeypatch.setattr(sweep, "_parse_marker", lambda value: ("x", 2**22, "5"))
    assert sweep._owner_state("x:1:5") == "unknown"


def test_a_proc_that_is_not_this_namespaces_does_nothing(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    real = sweep._pid_namespace
    monkeypatch.setattr(sweep, "_pid_namespace", lambda pid: "pid:[1]" if pid == "thread-self" else real(pid))
    assert sweep.configure_owner(tmp_path) is None
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert not result.supported and "namespace" in result.reason and signals.sent == []
    assert all(alive(pid) for pid in seen.values())


def test_proc_must_name_this_process_by_its_own_pid(tmp_path, instance, started, monkeypatch):
    """A /proc of another pid namespace answers ``self`` with another pid, or lists more than one in NSpid."""
    me = os.getpid()
    assert sweep._proc_is_ours()
    seen = make_tree(tmp_path, started, dead_owner(instance))
    real_readlink = os.readlink
    monkeypatch.setattr(os, "readlink", lambda path, *a, **k: "4242" if path == "/proc/self" else real_readlink(path, *a, **k))
    assert not sweep._proc_is_ours()
    monkeypatch.setattr(os, "readlink", real_readlink)
    import builtins

    real_open = builtins.open

    class Status:
        def __init__(self, text):
            import io

            self._io = io.BytesIO(text)

        def __enter__(self):
            return self._io

        def __exit__(self, *exc):
            return False

    for text in (b"Name:\tx\nNSpid:\t4242\n", b"Name:\tx\nNSpid:\t%d\t7\n" % me, b"Name:\tx\n"):
        monkeypatch.setattr(
            builtins, "open", lambda path, *a, _t=text, **k: Status(_t) if path == "/proc/self/status" else real_open(path, *a, **k)
        )
        assert not sweep._proc_is_ours()
    monkeypatch.setattr(builtins, "open", real_open)
    monkeypatch.setattr(sweep, "_proc_is_ours", lambda: False)
    signals = Signals(monkeypatch)
    assert sweep.configure_owner(tmp_path) is None
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    assert not result.supported and signals.sent == []
    assert all(alive(pid) for pid in seen.values())


def test_routine_refusals_log_at_debug(tmp_path, instance, started, caplog):
    import logging

    owner = subprocess.Popen([SLEEP, "60"])
    started[1].append(owner)
    marker = "%s:%d:%s" % (instance, owner.pid, sweep._read_proc(owner.pid).start)
    make_tree(tmp_path, started, marker)
    own = subprocess.Popen([SLEEP, "60"], env={"PATH": os.environ.get("PATH", ""), spawn.OWNER_VARIABLE: dead_owner(instance)})
    started[1].append(own)
    with caplog.at_level(logging.DEBUG, logger="provider_hub.sandbox.sweep"):
        sweep.sweep_leftovers(instance=instance, term_wait=0.2)
    left_alone = [r for r in caplog.records if "left alone" in r.getMessage()]
    assert left_alone and all(r.levelno == logging.DEBUG for r in left_alone)


def test_the_second_scan_loop_honours_the_deadline(tmp_path, instance, started, monkeypatch):
    seen = make_tree(tmp_path, started, dead_owner(instance))
    skew = [0.0]
    real_clock = time.monotonic
    monkeypatch.setattr(sweep.time, "monotonic", lambda: real_clock() + skew[0])

    def slow(pid):
        skew[0] = 1e6  # the budget is gone by the time the first candidate is examined
        return True

    monkeypatch.setattr(sweep, "_same_pid_namespace", slow)
    signals = Signals(monkeypatch)
    result = sweep.sweep_leftovers(instance=instance, term_wait=0.5)
    monkeypatch.undo()
    assert result.truncated and result.sessions == [] and signals.sent == []
    assert all(alive(pid) for pid in seen.values())

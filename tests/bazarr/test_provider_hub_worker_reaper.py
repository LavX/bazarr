# coding=utf-8
"""Idle Provider Hub workers have to be reclaimed, without ending a live one.

Each Hub provider runs its plugin in a subprocess, spawned lazily the first time
a pool searches with that provider. Nothing stopped them afterwards: the pool's
own recycle only fires the next time that pool is searched, so on a quiet
install the fleet sat at full size indefinitely, at 17.6 to 44.6 MiB PSS each
and roughly 45 of them on a warm install.

The hard part is not finding the idle ones, it is not ending a busy one. The
first version of this decided idleness in the sweep and shut the worker down
afterwards, which is a check-then-act race: a search can pass `start()` and be
queued on the lock in the window between the two, then write into a process the
sweep had already ended. It also blocked on that lock, so a request legitimately
running for hours (transcription is allowed up to a 24 hour ceiling) would have
pinned the sweep for its whole duration and then killed the worker the moment it
succeeded.

So the decision and the shutdown happen together, under the same lock a request
holds, and the acquire never blocks. These tests use a real lock and assert on
the process, rather than mocking the call under test.
"""
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def empty_registry():
    from provider_hub import worker as worker_mod

    worker_mod._live_clients.clear()
    yield
    worker_mod._live_clients.clear()


@pytest.fixture
def group_signals(monkeypatch):
    """Record every process-group signal instead of sending one."""
    from provider_hub import worker as worker_mod

    sent = []
    monkeypatch.setattr(worker_mod.os, 'killpg',
                        lambda pgid, sig: sent.append((pgid, sig)))
    return sent


def _client(idle_for, alive=True, pid=4242):
    """A client wired to a stand-in process and a real lock, spawning nothing."""
    from provider_hub.worker import ProviderWorkerClient

    client = ProviderWorkerClient([])
    client.process = MagicMock()
    client.process.pid = pid
    client.process.poll.return_value = None if alive else 0
    client.last_used = time.monotonic() - idle_for
    return client


def test_an_idle_worker_is_ended(group_signals):
    from provider_hub import worker as worker_mod

    stale = _client(idle_for=3600)
    worker_mod._live_clients.add(stale)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    assert (4242, worker_mod.signal.SIGTERM) in group_signals
    assert stale not in worker_mod._live_clients


def test_the_normal_reap_path_ends_the_whole_group(group_signals):
    """A worker that exits on the first SIGTERM never reaches the kill path, so
    signalling the pid alone would leave whatever the plugin forked running and
    reparented while the sweep reported the memory reclaimed."""
    from provider_hub import worker as worker_mod

    stale = _client(idle_for=3600)
    stale.process.wait.return_value = 0
    worker_mod._live_clients.add(stale)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    assert (4242, worker_mod.signal.SIGTERM) in group_signals, (
        'the group was never asked to stop, only the worker')
    assert (4242, worker_mod.signal.SIGKILL) in group_signals, (
        'children that ignored SIGTERM were left behind by a successful reap')


def test_a_platform_without_process_groups_still_ends_the_worker(monkeypatch):
    """os.killpg does not exist on Windows. AttributeError is not an OSError, so
    an unguarded call escapes the fallback and the worker is never killed."""
    from provider_hub import worker as worker_mod

    monkeypatch.delattr(worker_mod.os, 'killpg', raising=False)
    stubborn = _client(idle_for=3600)
    stubborn.process.wait.side_effect = [Exception('ignored SIGTERM'), 0]
    worker_mod._live_clients.add(stubborn)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    stubborn.process.kill.assert_called_once()


def test_a_recently_used_worker_is_left_alone():
    from provider_hub import worker as worker_mod

    busy = _client(idle_for=60)
    worker_mod._live_clients.add(busy)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 0
    busy.process.terminate.assert_not_called()
    assert busy in worker_mod._live_clients, 'a live worker must stay reapable later'


def test_a_worker_serving_a_request_is_never_ended():
    """The whole point. A request holds the lock for its duration, including a
    transcription that legitimately runs far longer than the idle window."""
    from provider_hub import worker as worker_mod

    working = _client(idle_for=7200)  # stamped long ago, still mid-request
    working._lock.acquire()           # stand in for a request in flight
    worker_mod._live_clients.add(working)

    try:
        assert worker_mod.reap_idle_workers(idle_seconds=1800) == 0
        working.process.terminate.assert_not_called()
    finally:
        working._lock.release()


def test_the_sweep_does_not_block_on_a_busy_worker():
    """A blocking acquire would pin the scheduler thread for the length of the
    request, which for a long transcription is hours."""
    from provider_hub import worker as worker_mod

    working = _client(idle_for=7200)
    working._lock.acquire()
    worker_mod._live_clients.add(working)

    try:
        started = time.monotonic()
        worker_mod.reap_idle_workers(idle_seconds=1800)
        assert time.monotonic() - started < 1.0, 'the sweep waited on a held lock'
    finally:
        working._lock.release()


def test_idleness_is_rechecked_once_the_lock_is_held():
    """The sweep selects candidates without the lock, so the decision has to be
    made again with it: a request can start in between."""
    from provider_hub import worker as worker_mod

    client = _client(idle_for=3600)

    class _LockThatBecomesBusy:
        """A real lock whose acquire coincides with a request arriving."""

        def __init__(self, owner):
            self._inner = threading.Lock()
            self._owner = owner

        def acquire(self, *args, **kwargs):
            got = self._inner.acquire(*args, **kwargs)
            if got:
                self._owner.last_used = time.monotonic()
            return got

        def release(self):
            self._inner.release()

    client._lock = _LockThatBecomesBusy(client)
    worker_mod._live_clients.add(client)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 0
    client.process.terminate.assert_not_called()


def test_a_worker_that_already_exited_is_not_ended_again():
    from provider_hub import worker as worker_mod

    dead = _client(idle_for=3600, alive=False)
    worker_mod._live_clients.add(dead)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 0
    dead.process.terminate.assert_not_called()


def test_a_worker_that_will_not_terminate_has_its_group_killed(group_signals):
    """start_new_session makes the worker a process-group leader, so killing the
    pid alone would leave anything the plugin forked orphaned and resident."""
    from provider_hub import worker as worker_mod

    stubborn = _client(idle_for=3600)
    stubborn.process.wait.side_effect = [Exception('ignored SIGTERM'), 0]
    worker_mod._live_clients.add(stubborn)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    assert (4242, worker_mod.signal.SIGKILL) in group_signals, (
        f'expected the process group to be killed, got {group_signals!r}')


def test_one_failing_worker_does_not_strand_the_rest(monkeypatch):
    from provider_hub import worker as worker_mod

    angry = _client(idle_for=3600)
    angry.process.terminate.side_effect = OSError('will not die')
    calm = _client(idle_for=3600)
    monkeypatch.setattr(worker_mod.os, 'killpg',
                        lambda *a: (_ for _ in ()).throw(OSError('nor that')))
    worker_mod._live_clients.add(angry)
    worker_mod._live_clients.add(calm)

    reaped = worker_mod.reap_idle_workers(idle_seconds=1800)

    calm.process.terminate.assert_called_once()
    assert reaped >= 1, 'the survivor should still be counted'
    assert angry._lock.acquire(blocking=False), 'the lock was not released on failure'


def test_a_worker_that_outlives_its_stop_stays_reapable():
    """start() registers only when it spawns, so discarding a client whose
    process is still running would make that subprocess permanently invisible
    to the reaper: the exact leak this exists to prevent."""
    from provider_hub import worker as worker_mod

    survivor = _client(idle_for=3600)
    survivor.request = MagicMock(side_effect=Exception('shutdown ignored'))
    survivor.process.pid = 99
    survivor.process.wait.side_effect = Exception('outlived SIGKILL too')
    worker_mod._live_clients.add(survivor)

    survivor.stop()

    assert survivor in worker_mod._live_clients, (
        'a worker that survived its own stop was dropped from the registry and '
        'can never be reaped again')


def test_a_started_worker_registers_and_a_stopped_one_deregisters(monkeypatch, tmp_path):
    """The registry is what the sweep walks, so membership is the contract."""
    from provider_hub import worker as worker_mod

    client = worker_mod.ProviderWorkerClient(['true'], cwd=tmp_path)

    fake = MagicMock()
    fake.poll.return_value = None
    monkeypatch.setattr(worker_mod.subprocess, 'Popen', lambda *a, **kw: fake)
    monkeypatch.setattr(worker_mod.threading, 'Thread',
                        lambda *a, **kw: MagicMock(start=lambda: None))

    client.start()
    assert client in worker_mod._live_clients

    fake.poll.return_value = 0  # exited
    client.stop()
    assert client not in worker_mod._live_clients


def test_a_platform_without_sigkill_still_ends_the_worker(monkeypatch):
    """Windows has neither os.killpg nor signal.SIGKILL. The SIGKILL name is
    evaluated at the call sites, so it must not be looked up unguarded or the
    AttributeError fires before _signal_group can report False and fall back
    to process.kill()."""
    from provider_hub import worker as worker_mod

    monkeypatch.delattr(worker_mod.os, "killpg", raising=False)
    monkeypatch.delattr(worker_mod.signal, "SIGKILL", raising=False)

    client = _client(idle_for=3600)
    process = client.process

    def _ended():
        process.poll.return_value = 0

    process.terminate.side_effect = _ended
    process.kill.side_effect = _ended
    process.wait.return_value = 0
    worker_mod._live_clients.add(client)

    assert client.stop_if_idle(idle_seconds=60) is True
    assert process.terminate.called or process.kill.called
    assert client not in worker_mod._live_clients


def test_a_worker_surviving_the_kill_stays_registered(group_signals):
    """If both termination attempts leave the process running, deregistering
    anyway makes it permanently unreapable; it must stay registered so a later
    sweep retries."""
    import subprocess

    from provider_hub import worker as worker_mod

    client = _client(idle_for=3600)
    process = client.process
    process.poll.return_value = None  # survives everything
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="worker", timeout=0.01)
    worker_mod._live_clients.add(client)

    assert client.stop_if_idle(idle_seconds=60, grace_seconds=0.01) is False
    assert client in worker_mod._live_clients


def test_request_startup_is_serialized_with_the_sweep():
    """start() and the last_used stamp must happen under the request lock:
    outside it, the sweep can acquire the lock after start() returns, read the
    old timestamp, and kill the worker the request is about to write to."""
    import json as json_mod
    import queue

    from provider_hub.worker import WORKER_ABI_VERSION

    client = _client(idle_for=0)
    client.process.stdin = MagicMock()
    client._stdout_queue = queue.Queue()
    client._stdout_thread = None
    client._stderr_thread = None

    seen = {}

    def probe_start():
        seen["locked_during_start"] = client._lock.locked()

    client.start = probe_start

    def canned_line(timeout):
        seen["locked_during_read"] = client._lock.locked()
        return json_mod.dumps({
            "abi": WORKER_ABI_VERSION,
            "id": seen.setdefault("request_id", None) or _last_request_id(client),
            "ok": True,
            "payload": {},
            "events": [],
        })

    def _last_request_id(c):
        # The id is embedded in the message written to stdin.
        written = c.process.stdin.write.call_args[0][0]
        return json_mod.loads(written)["id"]

    client._read_line_with_deadline = canned_line
    result = client.request("ping", {})
    assert result.ok
    assert seen["locked_during_start"] is True


def test_a_stop_survivor_is_retained_for_later_sweeps(group_signals):
    """A worker stop() could not kill must stay strongly reachable: the weak
    registry alone lets garbage collection drop the client once the pool does,
    and with it the only handle to the still-running process."""
    import gc
    import subprocess

    from provider_hub import worker as worker_mod

    client = _client(idle_for=3600)
    process = client.process
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="worker", timeout=0.01)
    client.request = MagicMock(side_effect=RuntimeError("no shutdown channel"))
    worker_mod._live_clients.add(client)

    client.stop(grace_seconds=0.01)
    ref = None
    try:
        assert client in worker_mod._live_clients
        ref = client
        del client
        gc.collect()
        assert len(list(worker_mod._live_clients)) == 1, (
            "the surviving client fell out of the weak registry")
    finally:
        if ref is not None:
            worker_mod._unreaped_survivors.discard(ref)


def test_a_survivor_whose_process_dies_later_is_released(group_signals):
    """A held survivor must not be pinned forever once its process finally
    exits on its own: the next sweep finds it dead and lets it go."""
    import subprocess

    from provider_hub import worker as worker_mod

    client = _client(idle_for=3600)
    process = client.process
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="worker", timeout=0.01)
    worker_mod._live_clients.add(client)

    assert client.stop_if_idle(idle_seconds=60, grace_seconds=0.01) is False
    assert client in worker_mod._unreaped_survivors

    process.poll.return_value = 0  # it finally died on its own
    assert client.stop_if_idle(idle_seconds=60, grace_seconds=0.01) is False
    assert client not in worker_mod._unreaped_survivors, (
        'a dead survivor stayed pinned in the strong set')


def test_an_exited_provider_worker_stays_registered_until_its_cleanup_is_confirmed(monkeypatch, tmp_path):
    import sys

    from provider_hub.worker import ProviderWorkerClient, live_workers

    plugin_id = "pending_cleanup_fixture"
    client = ProviderWorkerClient([sys.executable, "-c", "pass"], provider_id=plugin_id,
                                  env={"BAZARR_PLUGIN_DATA_DIR": str(tmp_path / "data")})
    cleanup = ["pending"]
    monkeypatch.setattr(client, "launcher_cleanup_state", lambda: cleanup[0])
    assert client.start()
    client.process.wait(timeout=5)

    assert client in live_workers(plugin_id)
    assert not client.stop_if_idle(0)
    assert client in live_workers(plugin_id)
    client.cancel()
    assert client in live_workers(plugin_id)

    cleanup[0] = "confirmed"
    client.stop_if_idle(0)
    assert live_workers(plugin_id) == []


def test_windows_job_keeps_the_data_fence_after_the_worker_exits(monkeypatch, tmp_path):
    import json
    import os
    from types import SimpleNamespace
    from unittest.mock import Mock

    from provider_hub import worker
    from provider_hub.runtime.pool import WorkerPool
    from provider_hub.sandbox import activity, spawn

    class JobError(Exception):
        def __init__(self, winerror):
            self.winerror = winerror

    handles = []
    counts = [0]
    named = []

    def create_job(security, name):
        named.append(name)
        handle = SimpleNamespace(Close=Mock())
        handles.append(handle)
        return handle

    def open_job(access, inheritable, name):
        assert not inheritable and name == named[-1]
        if not counts[0] and handles[0].Close.called:
            raise JobError(2)
        return SimpleNamespace(Close=Mock())

    def query_job(handle, field):
        if field == 1:
            return {"ActiveProcesses": counts[0]}
        return {"BasicLimitInformation": {"LimitFlags": 0}}

    api = SimpleNamespace(
        CreateJobObject=Mock(side_effect=create_job), OpenJobObject=Mock(side_effect=open_job),
        QueryInformationJobObject=Mock(side_effect=query_job), SetInformationJobObject=Mock(),
        TerminateJobObject=Mock(side_effect=lambda handle, code: counts.__setitem__(0, 0)),
        JobObjectBasicAccountingInformation=1, JobObjectExtendedLimitInformation=9,
        JOB_OBJECT_QUERY=4, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
    )
    monkeypatch.setitem(sys.modules, "win32job", api)
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=JobError))
    monkeypatch.setattr(activity, "is_windows", lambda: True)
    monkeypatch.setattr(activity, "_root", activity._directory(tmp_path))
    previous_host_running = [True]
    monkeypatch.setattr(activity, "_windows_process", lambda pid: (42, previous_host_running[0]))
    process = SimpleNamespace(pid=321, poll=lambda: 0, _terminate_and_wait=Mock(), _reap_quietly=Mock())

    def start(command):
        [record] = list(activity._root.glob("*.json"))
        assert json.loads(record.read_text())["host"] == [os.getpid(), 42]
        assert command[5:8] == [named[-1], str(os.getpid()), "42"]
        counts[0] = 2
        return process

    assert activity.start_worker(start, ["worker.exe"]) is process
    api.SetInformationJobObject.assert_called_once_with(
        handles[0], 9, {"BasicLimitInformation": {"LimitFlags": 0x2000}})
    assert activity.previous_workers_stopped(tmp_path)
    monkeypatch.setattr(activity, "_owner", "second-run")
    counts[0] = 0  # The earlier host can still start the registered worker.
    assert not activity.previous_workers_stopped(tmp_path)
    previous_host_running[0] = False

    # The direct worker has exited, but an ordinary helper still has data open.
    counts[0] = 1
    assert not activity.previous_workers_stopped(tmp_path)
    client = worker.ProviderWorkerClient([], provider_id="alpha")
    client.process = process
    assert not worker._client_gone(client, 0)
    assert not WorkerPool._cleanup_confirmed(process)

    assert spawn.WorkerProcess._end_tree(process, 0)
    api.TerminateJobObject.assert_called_once_with(handles[0], 1)
    assert worker._client_gone(client, 0)
    assert activity.previous_workers_stopped(tmp_path)
    assert not process.activity_record.exists()
    handles[0].Close.assert_called_once()
    pool = SimpleNamespace(_settle_unverified=Mock())
    slot = SimpleNamespace(unverified_noted=False, session=SimpleNamespace(process=process))
    WorkerPool._note_unverified(pool, slot)
    pool._settle_unverified.assert_called_once_with(slot)


def test_windows_launch_interrupted_before_spawn_leaves_a_complete_job_record(monkeypatch, tmp_path):
    import json
    import os
    from types import SimpleNamespace
    from unittest.mock import Mock

    from provider_hub.sandbox import activity

    job = SimpleNamespace(close=Mock())
    monkeypatch.setattr(activity, "is_windows", lambda: True)
    monkeypatch.setattr(activity, "_root", activity._directory(tmp_path))
    monkeypatch.setattr(activity, "_windows_process", lambda pid: (42, True))
    monkeypatch.setattr(activity, "_open_windows_job", lambda name, create=False: job if create else None)

    def interrupt(command):
        raise SystemExit(41)

    with pytest.raises(SystemExit):
        activity.start_worker(interrupt, ["worker.exe"])
    [record] = list(activity._root.iterdir())
    assert json.loads(record.read_text())["host"] == [os.getpid(), 42]
    job.close.assert_called_once()
    monkeypatch.setattr(activity, "_owner", "second-run")
    assert not activity.previous_workers_stopped(tmp_path)
    monkeypatch.setattr(activity, "_windows_process", lambda pid: (42, False))
    assert activity.previous_workers_stopped(tmp_path)
    assert not record.exists()


def test_windows_launcher_enters_the_job_and_checks_its_host_before_starting_work(monkeypatch):
    from provider_hub.sandbox import activity

    events = []
    monkeypatch.setattr(activity, "_attach_windows_job", lambda name: events.append(("job", name)))

    def host(pid):
        events.append(("host", pid))
        return 42, True

    def run(command, **stdio):
        assert stdio == {"stdin": 0, "stdout": 1, "stderr": 2}
        events.append(("worker", command))
        return 0

    monkeypatch.setattr(activity, "_windows_process", host)
    monkeypatch.setattr(activity.subprocess, "call", run)
    assert activity._run_windows_worker("job", 321, 42, ["worker.exe"]) == 0
    assert events == [("job", "job"), ("host", 321), ("worker", ["worker.exe"])]
    events.clear()
    assert activity._run_windows_worker("job", 321, 43, ["worker.exe"]) == 1
    assert events == [("job", "job"), ("host", 321)]


def test_posix_launch_interrupted_before_spawn_can_clear_the_group_record(monkeypatch, tmp_path):
    import json

    from provider_hub.sandbox import activity

    monkeypatch.setattr(activity, "is_windows", lambda: False)
    monkeypatch.setattr(activity, "_root", activity._directory(tmp_path))

    def interrupt(command):
        [record] = list(activity._root.iterdir())
        assert json.loads(record.read_text())["group"] == 321
        raise SystemExit(41)

    with pytest.raises(SystemExit):
        activity.start_worker(interrupt, ["worker"], group=321)
    running = [True]
    monkeypatch.setattr(activity, "_owner", "second-run")
    monkeypatch.setattr(activity, "_group_running", lambda group: running[0])
    assert not activity.previous_workers_stopped(tmp_path)
    running[0] = False
    assert activity.previous_workers_stopped(tmp_path)
    assert list(activity._root.iterdir()) == []


def test_posix_interruption_after_the_child_starts_never_reaches_plugin_code(monkeypatch, tmp_path):
    import os
    import subprocess

    from provider_hub.sandbox import activity, spawn

    if not hasattr(os, "killpg"):
        pytest.skip("requires POSIX process groups")
    monkeypatch.setattr(activity, "_root", activity._directory(tmp_path))
    sample = tmp_path / "worker.started"
    command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(sample)]
    processes = []
    original_worker = spawn.WorkerProcess

    class InterruptedWorker(original_worker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            processes.append(self)
            raise SystemExit(41)

    monkeypatch.setattr(spawn, "WorkerProcess", InterruptedWorker)
    with pytest.raises(SystemExit):
        spawn._start_plain(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    [process] = processes
    process.wait(timeout=5)
    assert not sample.exists()
    monkeypatch.setattr(activity, "_owner", "second-run")
    assert activity.previous_workers_stopped(tmp_path)

    class RecordingWorker(original_worker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            processes.append(self)

    real_write = os.write

    def acknowledge(fd, data):
        if data == b"1":
            assert processes[-1]._anchor.pid == processes[-1].activity_group
        return real_write(fd, data)

    monkeypatch.setattr(spawn, "WorkerProcess", RecordingWorker)
    monkeypatch.setattr(os, "write", acknowledge)
    process = spawn._start_plain(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert process.wait(timeout=5) == 0
        assert sample.is_file()
    finally:
        process.stop(0)
        activity.forget(process)


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_provider_probe_uses_the_registered_worker_path_without_a_provider_id(monkeypatch, tmp_path, platform):
    from types import SimpleNamespace

    from provider_hub import worker
    from provider_hub.sandbox import spawn

    env = {"BAZARR_PROVIDER_HUB_BUNDLE": str(tmp_path), "BAZARR_PLUGIN_DATA_DIR": str(tmp_path / "probe")}
    command = worker.worker_command(sys.executable, worker._RUNNER_PATH)
    client = worker.ProviderWorkerClient(command, cwd=tmp_path, env=env)
    process = object()
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(client, "_sandbox_profile", lambda: None)
    monkeypatch.setattr(spawn, "_start_plain", lambda argv, **options: process if argv == command else None)
    assert client._spawn(env) is process


@pytest.mark.skipif(sys.platform != "win32", reason="requires a native Windows Job Object")
def test_windows_worker_job_reaps_a_real_helper_after_its_runner_exits(monkeypatch, tmp_path):
    import subprocess

    from provider_hub.sandbox import activity, spawn

    monkeypatch.setattr(activity, "_root", activity._directory(tmp_path))
    sample = tmp_path / "helper.ready"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import subprocess, sys\n"
        "helper = 'from pathlib import Path; import sys, time; Path(sys.argv[1]).touch(); time.sleep(30)'\n"
        "subprocess.Popen([sys.executable, '-c', helper, sys.argv[1]], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n",
        encoding="utf-8",
    )
    process = spawn._start_plain([sys.executable, str(worker), str(sample)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        assert process.wait(timeout=10) == 0, process.stderr.read()
        deadline = time.monotonic() + 10
        while not sample.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sample.is_file()
        assert not activity.tree_gone(process)
        process.stop(0)
        assert activity.tree_gone(process)
        activity.forget(process)
        assert not process.activity_record.exists()
    finally:
        process.stop(0)
        process.stderr.close()

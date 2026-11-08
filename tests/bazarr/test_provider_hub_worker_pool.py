# coding=utf-8
"""Worker pool: states, limits, restart policy, recycling and secret hygiene.

Everything runs against the scriptable fake worker. Time-dependent policy
(backoff, breaker, idle stop) uses a fake clock and calls ``sweep()`` by hand;
tests that need the live sampler thread say so.
"""
import json
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import pytest

from provider_hub.runtime import pool as pool_module
from provider_hub.runtime.errors import RequestCancelled, RequestFailed, SessionError
from provider_hub.runtime.pool import (
    CONFIG_RESERVE_BYTES,
    DEFAULT_MAX_RESIDENT,
    GIB,
    MIB,
    FolderBudget,
    MemorySample,
    PoolClosed,
    PoolHostCall,
    PoolLimits,
    PoolUnavailable,
    UnknownWorker,
    WorkerDefinition,
    WorkerKilled,
    WorkerPool,
    WorkerState,
    folder_usage,
    launcher_spawn,
    memory_limit_for,
    read_process_tree_memory,
)
from provider_hub.runtime.session import SessionLimits
from provider_hub.runtime.testing.fake_worker import fake_worker_command, fake_worker_env

has_smaps = os.path.exists("/proc/self/smaps_rollup")
needs_smaps = pytest.mark.skipif(not has_smaps, reason="needs /proc/<pid>/smaps_rollup")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def fast_session(**overrides):
    values = dict(handshake_timeout=10.0, heartbeat_interval=0.1, heartbeat_timeout=2.0,
                  cancel_grace=0.4, kill_grace=3.0, tick=0.02)
    values.update(overrides)
    return SessionLimits(**values)


def fast_pool_limits(**overrides):
    values = dict(
        tick=0.05, heartbeat_interval=0.1, backoff_base=1.0, backoff_cap=8.0,
        breaker_threshold=3, breaker_window=300.0, breaker_open=30.0, idle_timeout=60.0,
        acquire_timeout=1.0, close_grace=1.0, budget_interval=0.0,
    )
    values.update(overrides)
    return PoolLimits(**values)


def definition(key="tr", *, session=None, **kwargs):
    config = kwargs.pop("config", {})
    kwargs.setdefault("kind", "translator")
    return WorkerDefinition(
        key,
        command=fake_worker_command(),
        env=fake_worker_env(**config),
        session_limits=session or fast_session(),
        **kwargs,
    )


def pid_alive(pid):
    if os.name != "posix":
        # os.kill(pid, 0) terminates the process on Windows.
        pytest.skip("probing a pid with signal 0 is POSIX only")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not sys.platform.startswith("linux"):
        # No /proc here: signal 0 is all there is.
        return True
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        # /proc is hidden or unreadable (hidepid): signal 0 said it exists.
        return True


def wait_until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_pool(clock):
    pools = []

    def make(limits=None, *, autostart=False, use_clock=True, **kwargs):
        if use_clock:
            kwargs.setdefault("clock", clock)
        pool = WorkerPool(limits or fast_pool_limits(), autostart=autostart, **kwargs)
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        pool.close()


def pid_of(pool, key):
    return pool.status()["workers"][key]["pid"]


# ----------------------------------------------------------------- defaults


def test_defaults_match_the_agreed_limits():
    limits = PoolLimits()
    assert limits.max_resident == DEFAULT_MAX_RESIDENT == 8
    assert limits.heartbeat_interval == 15.0
    assert limits.config_reserve_bytes == CONFIG_RESERVE_BYTES == GIB
    assert memory_limit_for("translator") == GIB
    assert definition(kind="translator").memory_limit() == GIB
    assert definition(kind="translator", memory_limit_bytes=5 * MIB).memory_limit() == 5 * MIB
    assert definition(kind="other").memory_limit() < GIB


def test_definition_needs_a_way_to_start():
    with pytest.raises(ValueError):
        WorkerDefinition("x").check()
    pool = WorkerPool(autostart=False)
    try:
        with pytest.raises(ValueError):
            pool.register(WorkerDefinition("x"))
        with pytest.raises(UnknownWorker):
            pool.state("nope")
        with pytest.raises(UnknownWorker):
            pool.submit("nope", "echo")
    finally:
        pool.close()


def test_pool_session_defaults_use_the_agreed_heartbeat(make_pool):
    pool = make_pool(PoolLimits(), autostart=False)
    plain = WorkerDefinition("tr", kind="translator", command=fake_worker_command(), env=fake_worker_env())
    pool.register(plain)
    pool.warm("tr")
    session = pool._entries["tr"].slot.session
    assert session.limits.heartbeat_interval == 15.0
    assert session.limits.heartbeat_timeout == 45.0


# ------------------------------------------------------------------- states


def test_state_transitions_from_stopped_to_stopped(make_pool):
    seen = []
    pool = make_pool(event_sink=lambda key, name, payload: seen.append(payload["to"]) if name == "state" else None)
    pool.register(definition())
    assert pool.state("tr") is WorkerState.STOPPED
    assert pool.run("tr", "echo", {"a": 1}).result["echo"] == {"a": 1}
    assert pool.state("tr") is WorkerState.READY
    handle = pool.submit("tr", "sleep", {"seconds": 5})
    assert wait_until(lambda: pool.state("tr") is WorkerState.BUSY)
    handle.cancel()
    with pytest.raises(RequestCancelled):
        handle.wait(5)
    assert wait_until(lambda: pool.state("tr") is WorkerState.READY)
    pool.stop("tr")
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)
    assert seen == ["starting", "ready", "stopping", "stopped"]


def test_a_worker_is_reused_between_requests(make_pool):
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo")
    first = pid_of(pool, "tr")
    pool.run("tr", "echo")
    assert pid_of(pool, "tr") == first
    assert pool.status()["workers"]["tr"]["starts"] == 1


def test_stop_ends_the_process_and_the_next_request_restarts(make_pool):
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo")
    first = pid_of(pool, "tr")
    pool.stop("tr")
    assert wait_until(lambda: not pid_alive(first))
    pool.run("tr", "echo")
    assert pid_of(pool, "tr") != first


def test_cancel_mid_request_leaves_the_worker_healthy(make_pool):
    pool = make_pool()
    pool.register(definition())
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    handle.cancel("user")
    with pytest.raises(RequestCancelled):
        handle.wait(5)
    status = pool.status()["workers"]["tr"]
    assert status["state"] == "ready" and status["failures"] == 0


def test_a_plugin_error_is_not_a_worker_failure(make_pool):
    pool = make_pool()
    pool.register(definition())
    with pytest.raises(RequestFailed):
        pool.run("tr", "raise", {"message": "nope"})
    status = pool.status()["workers"]["tr"]
    assert status["state"] == "ready" and status["failures"] == 0


def test_unregister_stops_the_worker(make_pool):
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    pool.unregister("tr")
    assert wait_until(lambda: not pid_alive(pid))
    with pytest.raises(UnknownWorker):
        pool.run("tr", "echo")


def test_close_stops_everything_and_refuses_new_work(make_pool):
    pool = make_pool()
    pool.register(definition("a"))
    pool.register(definition("b"))
    pool.run("a", "echo")
    pool.run("b", "echo")
    pids = [pid_of(pool, "a"), pid_of(pool, "b")]
    pool.close()
    assert wait_until(lambda: not any(pid_alive(p) for p in pids))
    with pytest.raises(PoolClosed):
        pool.run("a", "echo")
    pool.close()


# ------------------------------------------------------- backoff and breaker


def crash(pool, key="tr"):
    with pytest.raises(SessionError):
        pool.run(key, "exit", {"code": 3})


def test_a_crash_starts_backoff_and_requests_are_refused_until_it_ends(make_pool, clock):
    pool = make_pool()
    pool.register(definition())
    crash(pool)
    assert pool.state("tr") is WorkerState.BACKOFF
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("tr", "echo")
    assert refused.value.code == "backoff"
    assert 0 < refused.value.retry_after <= 1.0
    clock.advance(1.5)
    assert pool.run("tr", "echo").result["echo"] == {}
    status = pool.status()["workers"]["tr"]
    assert status["state"] == "ready" and status["failures"] == 0


def test_backoff_doubles_up_to_the_cap(make_pool, clock):
    pool = make_pool(fast_pool_limits(breaker_threshold=99, backoff_base=1.0, backoff_cap=3.0))
    pool.register(definition())
    delays = []
    for _ in range(4):
        crash(pool)
        delays.append(pool.status()["workers"]["tr"]["retry_after"])
        clock.advance(10)
    assert delays == [1.0, 2.0, 3.0, 3.0]


def test_the_breaker_opens_at_the_limit_and_a_clean_probe_closes_it(make_pool, clock):
    pool = make_pool()
    pool.register(definition())
    for _ in range(3):
        crash(pool)
        clock.advance(9)
    assert pool.state("tr") is WorkerState.BROKEN
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("tr", "echo")
    assert refused.value.code == "circuit_open"
    clock.advance(31)
    pool.run("tr", "echo")
    status = pool.status()["workers"]["tr"]
    assert status["state"] == "ready" and status["failures"] == 0


def test_a_failed_probe_reopens_the_breaker(make_pool, clock):
    pool = make_pool()
    pool.register(definition())
    for _ in range(3):
        crash(pool)
        clock.advance(9)
    clock.advance(31)
    crash(pool)
    assert pool.state("tr") is WorkerState.BROKEN


def test_old_failures_age_out_of_the_window(make_pool, clock):
    pool = make_pool(fast_pool_limits(breaker_window=100.0))
    pool.register(definition())
    crash(pool)
    clock.advance(200)
    crash(pool)
    assert pool.status()["workers"]["tr"]["failures"] == 1


def test_reset_clears_backoff(make_pool):
    pool = make_pool()
    pool.register(definition())
    crash(pool)
    pool.reset("tr")
    assert pool.state("tr") is WorkerState.STOPPED
    pool.run("tr", "echo")


def test_a_failed_spawn_backs_off_with_start_failed(make_pool):
    pool = make_pool()
    pool.register(WorkerDefinition("x", command=["/nonexistent/worker-binary"], kind="translator"))
    with pytest.raises(PoolUnavailable) as failed:
        pool.run("x", "echo")
    assert failed.value.code == "start_failed"
    assert pool.state("x") is WorkerState.BACKOFF


def test_a_worker_that_never_says_hello_fails_the_start(make_pool):
    pool = make_pool()
    pool.register(definition(config={"no_hello": True}, session=fast_session(handshake_timeout=0.4)))
    with pytest.raises(PoolUnavailable) as failed:
        pool.run("tr", "echo")
    assert failed.value.code == "start_failed"
    assert pool.state("tr") is WorkerState.BACKOFF


def test_a_refused_sandbox_is_a_policy_answer_not_a_failure(make_pool):
    from provider_hub.sandbox import SandboxRefused

    def refuse():
        raise SandboxRefused("this host cannot sandbox workers")

    pool = make_pool()
    pool.register(WorkerDefinition("x", spawn=refuse, kind="translator"))
    with pytest.raises(PoolUnavailable) as failed:
        pool.run("x", "echo")
    assert failed.value.code == "sandbox_refused"
    assert pool.state("x") is WorkerState.STOPPED
    assert pool.status()["workers"]["x"]["failures"] == 0


def test_a_worker_that_dies_while_idle_is_noticed_by_the_sweep(make_pool, clock):
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    os.kill(pid, 9)
    assert wait_until(lambda: not pid_alive(pid))
    assert wait_until(lambda: (pool.sweep() or pool.state("tr") is WorkerState.STOPPED))
    status = pool.status()["workers"]["tr"]
    assert status["crashes"] == 1 and status["failures"] == 1
    # Nothing was running, so there is no backoff: the next request just starts a worker.
    assert pool.run("tr", "echo").result["echo"] == {}
    assert pool.status()["workers"]["tr"]["crashes"] == 1


def test_a_worker_found_dead_by_a_request_is_replaced_and_counted_once(make_pool):
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    session = pool._entries["tr"].slot.session
    os.kill(pid, 9)
    assert wait_until(lambda: not pid_alive(pid))
    assert wait_until(lambda: session._closed)
    assert pool.run("tr", "echo").result["echo"] == {}
    assert pid_of(pool, "tr") != pid
    assert pool.status()["workers"]["tr"]["crashes"] == 1


# ------------------------------------------------------ idle stop and LRU cap


def test_idle_workers_are_stopped_after_the_idle_timeout(make_pool, clock):
    pool = make_pool(fast_pool_limits(idle_timeout=30.0))
    pool.register(definition())
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    clock.advance(20)
    pool.sweep()
    assert pool.state("tr") is WorkerState.READY
    clock.advance(20)
    pool.sweep()
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)
    assert wait_until(lambda: not pid_alive(pid))
    assert pool.status()["workers"]["tr"]["failures"] == 0
    pool.run("tr", "echo")
    assert pool.state("tr") is WorkerState.READY


def test_a_busy_worker_is_never_idle_stopped(make_pool, clock):
    pool = make_pool(fast_pool_limits(idle_timeout=1.0))
    pool.register(definition())
    handle = pool.submit("tr", "sleep", {"seconds": 1.0})
    clock.advance(100)
    pool.sweep()
    assert pool.state("tr") is WorkerState.BUSY
    handle.wait(5)


def test_a_definition_can_keep_its_worker_resident(make_pool, clock):
    pool = make_pool(fast_pool_limits(idle_timeout=1.0))
    pool.register(definition(idle_timeout=float("inf")))
    pool.run("tr", "echo")
    clock.advance(10_000)
    pool.sweep()
    assert pool.state("tr") is WorkerState.READY


def test_the_least_recently_used_idle_worker_is_stopped_at_the_cap(make_pool, clock):
    pool = make_pool(fast_pool_limits(max_resident=2))
    for key in "abc":
        pool.register(definition(key))
    pool.run("a", "echo")
    clock.advance(1)
    pool.run("b", "echo")
    clock.advance(1)
    pool.run("a", "echo")
    clock.advance(1)
    pid_b = pid_of(pool, "b")
    pool.run("c", "echo")
    assert wait_until(lambda: pool.state("b") is WorkerState.STOPPED)
    assert pool.state("a") is WorkerState.READY and pool.state("c") is WorkerState.READY
    assert wait_until(lambda: not pid_alive(pid_b))
    assert pool.status()["resident"] == 2


def test_a_full_pool_of_busy_workers_refuses_with_pool_full(make_pool):
    pool = make_pool(fast_pool_limits(max_resident=1, acquire_timeout=0.3), use_clock=False)
    pool.register(definition("a"))
    pool.register(definition("b"))
    handle = pool.submit("a", "sleep", {"seconds": 5})
    with pytest.raises(PoolUnavailable) as full:
        pool.run("b", "echo")
    assert full.value.code == "pool_full"
    handle.cancel()
    with pytest.raises(RequestCancelled):
        handle.wait(5)
    pool.run("b", "echo")
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)


def test_lowering_the_cap_stops_idle_surplus_workers(make_pool, clock):
    pool = make_pool()
    for key in "abc":
        pool.register(definition(key))
        pool.run(key, "echo")
        clock.advance(1)
    pool.set_max_resident(1)
    assert pool.max_resident == 1
    assert [pool.state(k) for k in "abc"] == [WorkerState.STOPPED, WorkerState.STOPPED, WorkerState.READY]


def test_concurrent_first_requests_start_one_worker(make_pool):
    import concurrent.futures

    pool = make_pool(use_clock=False)
    pool.register(definition())
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: pool.run("tr", "echo").result, range(4)))
    assert len(results) == 4
    assert pool.status()["workers"]["tr"]["starts"] == 1


# -------------------------------------------------------------------- memory


@needs_smaps
def test_process_tree_memory_reads_smaps_rollup_for_descendants():
    alone = read_process_tree_memory(os.getpid())
    assert alone is not None and alone.bytes > 0 and alone.source == "smaps_rollup"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wait_until(lambda: read_process_tree_memory(os.getpid()).processes > alone.processes)
        both = read_process_tree_memory(os.getpid())
        assert both.bytes > alone.bytes
    finally:
        child.kill()
        child.wait()
    assert read_process_tree_memory(2 ** 22 + 12345) is None


@needs_smaps
def test_a_memory_hog_is_killed_and_the_caller_is_told_why(make_pool):
    pool = make_pool(fast_pool_limits(), autostart=True, use_clock=False)
    pool.register(definition(memory_limit_bytes=80 * MIB))
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    with pytest.raises(WorkerKilled) as killed:
        pool.run("tr", "alloc", {"mb": 300, "seconds": 20}, timeout=20)
    assert killed.value.code == "memory_limit"
    assert wait_until(lambda: not pid_alive(pid))
    status = pool.status()["workers"]["tr"]
    assert status["state"] == "backoff" and status["failures"] == 1


def test_memory_over_the_limit_is_enforced_from_the_injected_reader(make_pool, clock):
    size = {"bytes": 10 * MIB}
    pool = make_pool(memory_reader=lambda pid: MemorySample(size["bytes"], size["bytes"], 1, "test"))
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    pool.sweep()
    assert pool.state("tr") is WorkerState.BUSY
    size["bytes"] = 500 * MIB
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"
    assert pool.state("tr") is WorkerState.BACKOFF


def test_a_worker_that_finishes_and_is_then_too_big_is_stopped_without_a_failure(make_pool):
    size = {"bytes": 10 * MIB}
    pool = make_pool(memory_reader=lambda pid: MemorySample(size["bytes"], size["bytes"], 1, "test"))
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 0.3})
    size["bytes"] = 500 * MIB
    handle.wait(10)
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)
    status = pool.status()["workers"]["tr"]
    assert status["failures"] == 0 and status["peak_bytes"] == 500 * MIB


def test_memory_is_sampled_around_every_request(make_pool):
    calls = []

    def reader(pid):
        calls.append(pid)
        return MemorySample(7 * MIB, 7 * MIB, 1, "test")

    pool = make_pool(memory_reader=reader)
    pool.register(definition())
    pool.run("tr", "echo")
    assert len(calls) >= 2
    assert pool.status()["workers"]["tr"]["memory_bytes"] == 7 * MIB


# -------------------------------------------------- folders and the disk reserve


def test_folder_usage_counts_files_and_stops_at_the_entry_cap(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_bytes(b"x" * 100_000)
    (tmp_path / "link").symlink_to(tmp_path / "sub")
    used, truncated = folder_usage(str(tmp_path))
    assert used >= 100_000 and not truncated
    for index in range(20):
        (tmp_path / ("n%d" % index)).write_bytes(b"")
    assert folder_usage(str(tmp_path), max_entries=5)[1] is True
    assert folder_usage(str(tmp_path / "missing")) == (0, False)


def test_a_folder_over_budget_blocks_the_start(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "big").write_bytes(os.urandom(3 * MIB))
    pool = make_pool()
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("tr", "echo")
    assert refused.value.code == "folder_budget"
    assert pool.state("tr") is WorkerState.STOPPED
    (data / "big").unlink()
    pool.run("tr", "echo")


def test_a_worker_that_fills_its_folder_is_killed(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pool = make_pool()
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    (data / "big").write_bytes(os.urandom(3 * MIB))
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "folder_budget"
    assert pool.state("tr") is WorkerState.BACKOFF


def test_a_request_on_a_running_worker_over_budget_is_refused(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pool = make_pool()
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    (data / "big").write_bytes(os.urandom(3 * MIB))
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("tr", "echo")
    assert refused.value.code == "folder_budget"
    assert wait_until(lambda: not pid_alive(pid))


def test_a_short_config_reserve_refuses_new_work_but_kills_nothing(make_pool):
    free = {"bytes": 10 * GIB}
    pool = make_pool(disk_free=lambda: free["bytes"])
    pool.register(definition("writer"))
    pool.register(definition("reader", writes_data=False))
    pool.register(definition("fresh"))
    handle = pool.submit("writer", "sleep", {"seconds": 1.0})
    pool.run("reader", "echo")
    free["bytes"] = 100 * MIB
    pool.sweep()
    pool.sweep()
    assert pool.state("writer") is WorkerState.BUSY
    # A request on the running writer, and a start, are refused. The reader is not a writer.
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("writer", "echo")
    assert refused.value.code == "disk_reserve"
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("fresh", "echo")
    assert refused.value.code == "disk_reserve"
    assert "reserve" in pool.status()["workers"]["fresh"]["last_error"]
    assert pool.run("reader", "echo").result["echo"] == {}
    # The running request finishes, nothing counted against the worker.
    assert handle.wait(10).result == {"slept": 1.0}
    status = pool.status()["workers"]["writer"]
    assert status["state"] == "ready" and status["failures"] == 0
    free["bytes"] = 10 * GIB
    pool.run("writer", "echo")
    pool.run("fresh", "echo")


def test_a_start_refused_by_a_folder_budget_says_why(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "big").write_bytes(os.urandom(3 * MIB))
    pool = make_pool()
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    with pytest.raises(PoolUnavailable):
        pool.run("tr", "echo")
    assert "budget" in pool.status()["workers"]["tr"]["last_error"]


# --------------------------------------------------- recycling and new settings


def test_recycle_needed_replaces_the_worker_on_the_next_request(make_pool):
    pool = make_pool()
    pool.register(definition(session=fast_session(max_secret_values=2)))
    pool.run("tr", "echo", secrets={"k": "alpha-secret-value"})
    first = pid_of(pool, "tr")
    pool.run("tr", "echo", secrets={"k": "beta-secret-value"})
    assert pid_of(pool, "tr") == first
    assert pool.run("tr", "echo", secrets={"k": "gamma-secret-value"}).result["echo"] == {}
    assert pid_of(pool, "tr") != first
    pool.sweep()
    assert wait_until(lambda: not pid_alive(first))


def test_recycle_lets_the_old_worker_finish_what_it_holds(make_pool):
    pool = make_pool()
    pool.register(definition(session=fast_session(max_secret_values=2)))
    slow = pool.submit("tr", "sleep", {"seconds": 0.8}, secrets={"k": "one-secret-value"})
    first = pid_of(pool, "tr")
    pool.run("tr", "echo", secrets={"a": "two-secret-value", "b": "three-secret-value"})
    second = pid_of(pool, "tr")
    assert second != first
    assert pool.status()["workers"]["tr"]["retiring"] == 1
    assert slow.wait(10).result == {"slept": 0.8}
    pool.sweep()
    assert wait_until(lambda: not pid_alive(first))
    assert pool.status()["workers"]["tr"]["retiring"] == 0


def test_restart_after_drain_applies_new_settings_without_interrupting_work(make_pool):
    pool = make_pool()
    pool.register(definition())
    slow = pool.submit("tr", "sleep", {"seconds": 0.8})
    first = pid_of(pool, "tr")
    updated = definition(config={"max_in_flight": 2})
    pool.restart_after_drain("tr", updated)
    assert pool.state("tr") is WorkerState.DRAINING
    assert pool.definition("tr") is updated
    pool.run("tr", "echo")
    assert pid_of(pool, "tr") != first
    assert pid_alive(first)
    assert slow.wait(10).result == {"slept": 0.8}
    pool.sweep()
    assert wait_until(lambda: not pid_alive(first))


def test_restart_after_drain_on_an_idle_worker_stops_it_at_once(make_pool):
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo")
    first = pid_of(pool, "tr")
    pool.restart_after_drain("tr")
    pool.sweep()
    # Stopped once the teardown of its worker is confirmed, not when it is merely queued.
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)
    assert wait_until(lambda: not pid_alive(first))
    assert pool.status()["processes"] == 0
    with pytest.raises(ValueError):
        pool.restart_after_drain("tr", definition("other"))


# ------------------------------------------------------------- secret hygiene


def test_a_secret_never_reaches_a_log_line_or_the_status(make_pool, caplog):
    secret = "sk-live-9f8e7d6c5b4a39281706"
    caplog.set_level(logging.DEBUG)
    pool = make_pool(use_clock=False)
    pool.register(definition())
    pool.run("tr", "leak", {"pad": 3}, secrets={"api_key": secret})
    assert wait_until(lambda: pool.status()["workers"]["tr"]["recent_stderr"])
    with pytest.raises(RequestFailed) as failed:
        pool.run("tr", "leak_error", secrets={"api_key": secret})
    tail = pool.status()["workers"]["tr"]["recent_stderr"]
    assert any("***" in line for line in tail)
    everything = caplog.text + json.dumps(pool.status()) + str(failed.value) + str(failed.value.traceback)
    assert secret not in everything
    assert "api_key" not in caplog.text


def test_secrets_are_not_kept_in_the_definition_or_entry(make_pool):
    secret = "sk-live-000011112222"
    pool = make_pool()
    pool.register(definition())
    pool.run("tr", "echo", secrets={"k": secret})
    assert secret not in repr(pool.definition("tr"))
    assert secret not in json.dumps(pool.status())


def test_pool_stderr_logging_is_rate_limited(caplog):
    caplog.set_level(logging.INFO)
    limits = PoolLimits(stderr_log_per_second=3, stderr_tail_lines=10)
    slot = pool_module._Slot(definition(), 0.0, limits)
    sink = slot.stderr_sink("tr")
    for index in range(300):
        sink("noise %d" % index)
    logged = [r for r in caplog.records if "stderr: noise" in r.getMessage()]
    assert 1 <= len(logged) <= 6
    assert len(slot.stderr_tail) == 10 and slot.stderr_tail[-1] == "noise 299"


# ------------------------------------------------------------------- run_once


def test_run_once_uses_a_throwaway_worker(make_pool):
    pool = make_pool(use_clock=False)
    draft = definition("draft")
    result = pool.run_once(draft, "echo", {"draft": True})
    assert result.result["echo"] == {"draft": True}
    assert pool.status()["workers"] == {} and pool.status()["resident"] == 0
    with pytest.raises(RequestFailed):
        pool.run_once(draft, "raise")


def test_run_once_worker_is_gone_afterwards(make_pool):
    pool = make_pool(use_clock=False)
    result = pool.run_once(definition("draft"), "fork", {})
    child = result.result["child"]
    assert pool._ephemeral == set()
    assert wait_until(lambda: not pid_alive(child))


@needs_smaps
def test_run_once_enforces_the_memory_limit(make_pool):
    pool = make_pool(fast_pool_limits(), autostart=True, use_clock=False)
    with pytest.raises(WorkerKilled) as killed:
        pool.run_once(definition("draft", memory_limit_bytes=80 * MIB), "alloc", {"mb": 300, "seconds": 20}, timeout=20)
    assert killed.value.code == "memory_limit"


def test_run_once_respects_the_disk_reserve(make_pool):
    pool = make_pool(disk_free=lambda: 1 * MIB)
    with pytest.raises(PoolUnavailable) as refused:
        pool.run_once(definition("draft"), "echo")
    assert refused.value.code == "disk_reserve"


def test_run_once_does_not_touch_the_breaker(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition("tr"))
    with pytest.raises(SessionError):
        pool.run_once(definition("tr"), "exit", {"code": 2})
    assert pool.state("tr") is WorkerState.STOPPED
    assert pool.status()["workers"]["tr"]["failures"] == 0


# ---------------------------------------------------------- through the launcher


def launcher_definition(tmp_path, config=None):
    from provider_hub.sandbox import Limits, RuntimeSpec, SandboxPolicy, SandboxProbe, clean_env
    from provider_hub.runtime import testing

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    runtime_dir = Path(testing.__file__).resolve().parents[1]
    command = fake_worker_command()

    def build():
        policy = SandboxPolicy.for_interpreter(sys.executable, read_only=[runtime_dir], read_write=[data])
        return RuntimeSpec(
            interpreter=command[0],
            args=tuple(command[1:]),
            cwd=str(data),
            env=clean_env(home=data, tmpdir=data, extra=fake_worker_env(**(config or {}))),
            policy=policy,
            limits=Limits(),
            term_grace=1.0,
        )

    declared = SandboxProbe(abi=0, verified=False, reason="test: declared mode")
    return build, declared


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")
def test_a_worker_started_through_the_launcher_runs_and_is_measured(make_pool, tmp_path):
    build, declared = launcher_definition(tmp_path)
    pool = make_pool(use_clock=False)
    pool.register(WorkerDefinition("tr", kind="translator", spawn=launcher_spawn(build, probe=declared),
                                   session_limits=fast_session()))
    assert pool.run("tr", "echo", {"x": 1}).result["echo"] == {"x": 1}
    status = pool.status()["workers"]["tr"]
    assert status["state"] == "ready"
    if has_smaps:
        assert wait_until(lambda: pool.status()["workers"]["tr"]["memory_bytes"] > 0)
        # The launcher and the worker are both in the sampled tree.
        tree = read_process_tree_memory(status["pid"])
        assert tree is not None and tree.processes >= 2
    pid = status["pid"]
    pool.stop("tr")
    assert wait_until(lambda: not pid_alive(pid))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")
def test_launcher_spawn_refuses_without_a_sandbox_when_required(make_pool, tmp_path):
    build, declared = launcher_definition(tmp_path)
    pool = make_pool(use_clock=False)
    pool.register(WorkerDefinition("tr", kind="translator", session_limits=fast_session(),
                                   spawn=launcher_spawn(build, probe=declared, require_sandbox=True)))
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("tr", "echo")
    assert refused.value.code == "sandbox_refused"
    assert pool.status()["workers"]["tr"]["failures"] == 0


linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")


def stubborn_launcher_pool(make_pool, tmp_path, **limits):
    """A pool with one launcher-started worker that ignores SIGTERM and the shutdown frame."""
    build, declared = launcher_definition(tmp_path, {"stubborn": True})
    pool = make_pool(fast_pool_limits(close_grace=0.3, **limits), use_clock=False)
    pool.register(WorkerDefinition("tr", kind="translator", spawn=launcher_spawn(build, probe=declared),
                                   session_limits=fast_session()))
    return pool


def start_with_setsid_child(pool, key="tr"):
    child = pool.run(key, "fork", {"detached": True}).result["child"]
    assert pid_alive(child)
    return child, pid_of(pool, key)


@linux_only
@pytest.mark.parametrize("how", ["stop", "close", "drain", "unregister"])
def test_a_stubborn_worker_and_its_setsid_child_are_gone_after_a_polite_stop(make_pool, tmp_path, clock, how):
    pool = stubborn_launcher_pool(make_pool, tmp_path)
    child, launcher = start_with_setsid_child(pool)
    started = time.monotonic()
    if how == "stop":
        pool.stop("tr")
    elif how == "close":
        pool.close()
    elif how == "drain":
        pool.restart_after_drain("tr")
        pool.sweep()
    else:
        pool.unregister("tr")
    assert wait_until(lambda: not pid_alive(child), 20)
    assert wait_until(lambda: not pid_alive(launcher), 20)
    assert time.monotonic() - started < 15


@linux_only
def test_an_idle_stop_ends_a_stubborn_launcher_worker_tree(make_pool, tmp_path, clock):
    build, declared = launcher_definition(tmp_path, {"stubborn": True})
    pool = make_pool(fast_pool_limits(close_grace=0.3, idle_timeout=30.0))
    pool.register(WorkerDefinition("tr", kind="translator", spawn=launcher_spawn(build, probe=declared),
                                   session_limits=fast_session()))
    child, launcher = start_with_setsid_child(pool)
    clock.advance(100)
    pool.sweep()
    assert wait_until(lambda: not pid_alive(child), 20)
    assert wait_until(lambda: not pid_alive(launcher), 20)
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)


@linux_only
def test_lru_eviction_ends_a_stubborn_launcher_worker_tree(make_pool, tmp_path):
    build, declared = launcher_definition(tmp_path, {"stubborn": True})
    pool = make_pool(fast_pool_limits(close_grace=0.3, max_resident=1, acquire_timeout=20.0), use_clock=False)
    pool.register(WorkerDefinition("tr", kind="translator", spawn=launcher_spawn(build, probe=declared),
                                   session_limits=fast_session()))
    pool.register(definition("other"))
    child, launcher = start_with_setsid_child(pool)
    pool.run("other", "echo")
    assert not pid_alive(launcher) and not pid_alive(child)


# ------------------------------------------------------------- settings during a start


def test_settings_changed_during_a_start_apply_to_the_next_worker(make_pool):
    import threading

    pool = make_pool(use_clock=False)
    pool.register(definition(idle_timeout=float("inf"), config={"hello_delay": 0.6}))
    outcome = {}

    def first():
        outcome["result"] = pool.run("tr", "echo")

    thread = threading.Thread(target=first)
    thread.start()
    assert wait_until(lambda: pool.state("tr") is WorkerState.STARTING)
    updated = definition(idle_timeout=float("inf"))
    pool.restart_after_drain("tr", updated)
    thread.join(10)
    # The request that asked for the worker was served; the worker is already retired.
    assert outcome["result"].result["echo"] == {}
    assert pool.state("tr") is WorkerState.DRAINING
    old = [slot.session.process.pid for slot in pool._entries["tr"].retiring]
    assert len(old) == 1
    pool.run("tr", "echo")
    assert pid_of(pool, "tr") != old[0]
    assert pool._entries["tr"].slot.definition is updated
    pool.sweep()
    assert wait_until(lambda: not pid_alive(old[0]))
    assert pool.status()["workers"]["tr"]["retiring"] == 0


def test_a_restart_with_no_new_definition_also_catches_a_start_under_way(make_pool):
    import threading

    pool = make_pool(use_clock=False)
    pool.register(definition(idle_timeout=float("inf"), config={"hello_delay": 0.5}))
    thread = threading.Thread(target=lambda: pool.run("tr", "echo"))
    thread.start()
    assert wait_until(lambda: pool.state("tr") is WorkerState.STARTING)
    pool.restart_after_drain("tr")
    thread.join(10)
    assert pool.state("tr") is WorkerState.DRAINING


# ------------------------------------------------------------------ request context


def test_the_first_host_call_carries_the_request_context_even_before_submit_returns(make_pool, monkeypatch):
    from provider_hub.runtime.session import WorkerSession

    seen = []

    def reserve(call):
        seen.append((call.name, call.request_id, call.context, call.key))
        return {"granted": True}

    real_submit = WorkerSession.submit

    def slow_submit(self, *args, **kwargs):
        handle = real_submit(self, *args, **kwargs)
        # The worker already has its request; the pool does not know the id yet.
        time.sleep(0.4)
        return handle

    monkeypatch.setattr(WorkerSession, "submit", slow_submit)
    pool = make_pool(use_clock=False)
    pool.register(definition(host_calls={"reserve": reserve}))
    job = {"job": 41}
    result = pool.run("tr", "call", {"name": "reserve", "params": {}}, context=job)
    assert result.result["ack"] == {"granted": True}
    assert len(seen) == 1
    name, request_id, context, key = seen[0]
    assert name == "reserve" and context is job and key == "reserve"
    assert pool._entries["tr"].slot.contexts == {}


def test_requests_get_their_own_context(make_pool):
    seen = []

    def reserve(call):
        seen.append(call.context)
        return {}

    pool = make_pool(use_clock=False)
    pool.register(definition(host_calls={"reserve": reserve}))
    pool.run("tr", "call", {"name": "reserve", "params": {}}, context="first")
    pool.run("tr", "call", {"name": "reserve", "params": {}}, context="second")
    pool.run("tr", "call", {"name": "reserve", "params": {}})
    assert seen == ["first", "second", None]
    assert isinstance(PoolHostCall("n", {}, 1, None), PoolHostCall)


def test_run_once_passes_the_context_too(make_pool):
    seen = []
    pool = make_pool(use_clock=False)
    pool.run_once(definition("d", host_calls={"reserve": lambda call: seen.append(call.context) or {}}),
                  "call", {"name": "reserve", "params": {}}, context={"draft": True})
    assert seen == [{"draft": True}]


# ------------------------------------------------------ cap, closing and status


def test_a_worker_that_is_still_stopping_counts_toward_the_cap(make_pool):
    pool = make_pool(fast_pool_limits(max_resident=1, acquire_timeout=20.0, close_grace=0.5), use_clock=False)
    pool.register(definition("slow", config={"stubborn": True}))
    pool.register(definition("next"))
    pool.run("slow", "echo")
    slow_pid = pid_of(pool, "slow")
    pool.run("next", "echo")
    # "next" only started once the stubborn worker's process was really gone.
    assert not pid_alive(slow_pid)
    assert pool.status()["processes"] == 1


def test_sweep_does_not_wait_for_a_slow_stop(make_pool, clock):
    pool = make_pool(fast_pool_limits(idle_timeout=30.0, close_grace=1.0))
    pool.register(definition(config={"stubborn": True}))
    pool.run("tr", "echo")
    pid = pid_of(pool, "tr")
    clock.advance(100)
    started = time.monotonic()
    pool.sweep()
    assert time.monotonic() - started < 0.5
    assert pool.state("tr") is WorkerState.STOPPING
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED, 15)
    assert not pid_alive(pid)


def test_last_error_in_status_has_no_paths(make_pool):
    pool = make_pool()
    pool.register(WorkerDefinition("x", command=["/nonexistent/dir/worker-binary"], kind="translator"))
    with pytest.raises(PoolUnavailable):
        pool.run("x", "echo")
    error = pool.status()["workers"]["x"]["last_error"]
    assert error and "/nonexistent" not in error and "<path>" in error
    assert pool_module._public_text("open '/config/plugins/run/x/y.py' failed in /tmp/a") == \
        "open '<path>' failed in <path>"
    assert pool_module._public_text("and/or 10 / 5 http://host/x") == "and/or 10 / 5 http://host/x"


# ---------------------------------------------------------------------- runtime seam


class StubRuntime:
    """A worker that is not a process: the shape another runtime has to provide."""

    def __init__(self):
        self.process = None
        self.exit_code = None
        self.recycle_needed = False
        self.started = False
        self.closed_with = None
        self.alive_flag = True

    @property
    def alive(self):
        return self.started and self.alive_flag

    @property
    def in_flight(self):
        return 0

    def start(self):
        self.started = True

    def request(self, op, params=None, **kwargs):
        from provider_hub.runtime.session import RequestResult

        return RequestResult({"op": op})

    def submit(self, op, params=None, **kwargs):
        raise NotImplementedError

    def close(self, grace=5.0):
        self.alive_flag = False
        self.closed_with = grace


def test_the_pool_runs_a_non_session_runtime_through_the_factory(make_pool, clock):
    created = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtime = StubRuntime()
        created.append((runtime, limits, sorted(host_calls)))
        return runtime

    pool = make_pool(fast_pool_limits(idle_timeout=10.0), memory_reader=lambda pid: pytest.fail("no process to sample"))
    pool.register(WorkerDefinition("daemon", kind="daemon", runtime=factory, host_calls={"x": lambda c: {}}))
    pool.warm("daemon")
    assert pool.state("daemon") is WorkerState.READY
    assert created[0][2] == ["x"]
    assert pool.status()["workers"]["daemon"]["pid"] is None
    clock.advance(60)
    pool.sweep()
    assert wait_until(lambda: pool.state("daemon") is WorkerState.STOPPED)
    assert created[0][0].closed_with is not None


# ------------------------------------------------- ownership and confirmed exit


class StubProcess:
    """A process handle the pool can stop but that never reports an exit by itself."""

    def __init__(self):
        self.pid = 424242
        self.returncode = None
        self.stops = []
        self.lifeline_cuts = 0

    def close_lifeline(self):
        self.lifeline_cuts += 1

    def stop(self, grace=3.0):
        self.stops.append(grace)


class StubProcessRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.process = StubProcess()

    @property
    def alive(self):
        return self.started and self.process.returncode is None

    def submit(self, op, params=None, **kwargs):
        from provider_hub.runtime.session import RequestResult

        class Handle:
            id = 1

            def done(self):
                return True

            def wait(self, timeout=None):
                return RequestResult({"op": op})

        return Handle()

    def close(self, grace=5.0):
        self.closed_with = grace


def test_close_grace_must_fit_a_shutdown_frame():
    for bad in (float("nan"), float("inf"), -1.0, 601, True, "2"):
        with pytest.raises(ValueError):
            PoolLimits(close_grace=bad)
    assert PoolLimits(close_grace=0).close_grace == 0
    assert PoolLimits(close_grace=600).close_grace == 600


def test_bad_secrets_and_timeouts_are_refused_before_a_worker_starts(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition("tr"))
    for secrets in ({"token": 1}, {"token": ""}, {"token": {"nested": "x"}}, {1: "x"}):
        with pytest.raises(ValueError):
            pool.submit("tr", "echo", secrets=secrets)
    for timeout in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            pool.submit("tr", "echo", timeout=timeout)
    with pytest.raises(ValueError):
        pool.run_once(definition("draft"), "echo", secrets={"token": ["x"]})
    assert pool.state("tr") is WorkerState.STOPPED
    assert pool.status()["workers"]["tr"]["starts"] == 0
    assert pool.status()["processes"] == 0


def test_a_worker_whose_process_is_not_confirmed_gone_keeps_its_place(make_pool):
    runtimes = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(StubProcessRuntime())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=1), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.register(WorkerDefinition("b", kind="daemon", runtime=factory))
    pool.warm("a")
    process = runtimes[0].process
    pool.stop("a")
    # The process never reported an exit: still counted, entry still stopping.
    assert pool.state("a") is WorkerState.STOPPING
    assert pool.status()["processes"] == 1
    assert process.lifeline_cuts >= 1 and process.stops
    with pytest.raises(PoolUnavailable):
        pool.warm("b")
    process.returncode = 0
    pool.sweep()
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)
    assert pool.status()["processes"] == 0
    pool.warm("b")
    assert pool.state("b") is WorkerState.READY


def test_run_once_holds_its_permit_until_the_process_is_gone(make_pool):
    holder = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        holder.append(StubProcessRuntime())
        return holder[-1]

    pool = make_pool(fast_pool_limits(max_oneshot=1, acquire_timeout=0.3), use_clock=False)
    stuck = WorkerDefinition("draft", kind="daemon", runtime=factory)
    pool.run_once(stuck, "echo")
    # Another plugin's one-off finds the only permit taken; the same plugin's is
    # refused earlier, because its survivor is still on the same folders.
    with pytest.raises(PoolUnavailable) as full:
        pool.run_once(WorkerDefinition("other", kind="daemon", runtime=factory), "echo")
    assert full.value.code == "pool_full"
    with pytest.raises(PoolUnavailable) as same:
        pool.run_once(stuck, "echo")
    assert same.value.code == "worker_unavailable"
    holder[0].process.returncode = 0
    pool.sweep()
    assert pool.run_once(stuck, "echo").result == {"op": "echo"}


def test_the_sampler_does_not_read_a_leader_that_was_reaped(make_pool):
    runtime = StubProcessRuntime()
    pool = make_pool(memory_reader=lambda pid: pytest.fail("sampled a reaped process"))
    pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *args: runtime))
    pool.warm("a")
    runtime.process.returncode = 0
    pool.sweep()
    assert pool.status()["workers"]["a"]["pid"] is None or pool.state("a") is not WorkerState.READY


def test_the_pool_never_signals_a_process_group_itself():
    source = Path(pool_module.__file__).read_text()
    assert "killpg" not in source and "os.kill(" not in source


# ------------------------------------------- incomplete measurements fail closed


def test_a_process_tree_larger_than_the_read_limit_is_reported_truncated(monkeypatch):
    fake = list(range(2 ** 22 + 100, 2 ** 22 + 400))
    monkeypatch.setattr(pool_module, "_children_of", lambda pid: (fake if pid == os.getpid() else [], False))
    sample = read_process_tree_memory(os.getpid(), max_processes=256)
    if sample is not None:
        assert sample.truncated
    members, overflow = pool_module._process_tree(os.getpid(), 256)
    assert overflow and len(members) == 256
    members, overflow = pool_module._process_tree(os.getpid(), 301)
    assert not overflow and len(members) == 301


def test_a_worker_whose_tree_is_too_big_to_measure_is_killed_as_over_its_limit(make_pool):
    late = {"bad": False}
    pool = make_pool(memory_reader=lambda pid: MemorySample(1 * MIB, 1 * MIB, 256, "test", truncated=late["bad"]))
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    late["bad"] = True
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"
    assert "measured" in str(killed.value)


def test_a_folder_scan_that_cannot_read_a_subtree_is_incomplete(tmp_path, monkeypatch):
    (tmp_path / "sealed").mkdir()
    (tmp_path / "sealed" / "big").write_bytes(b"x" * 100_000)
    real = os.open

    def denying(path, *args, **kwargs):
        if os.fspath(path).endswith("sealed"):
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denying)
    used, truncated = folder_usage(str(tmp_path))
    assert truncated is True
    # A path that is simply not there is empty, not unknown.
    assert folder_usage(str(tmp_path / "missing")) == (0, False)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_a_directory_without_list_permission_fails_the_budget_check(make_pool, tmp_path):
    data = tmp_path / "data"
    sealed = data / "sealed"
    sealed.mkdir(parents=True)
    (sealed / "big").write_bytes(os.urandom(3 * MIB))
    sealed.chmod(0o300)
    try:
        assert folder_usage(str(data))[1] is True
        pool = make_pool()
        pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
        with pytest.raises(PoolUnavailable) as refused:
            pool.run("tr", "echo")
        assert refused.value.code == "folder_budget"
    finally:
        sealed.chmod(0o700)


def test_a_draining_worker_that_fills_its_folder_is_killed(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pool = make_pool()
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    slow = pool.submit("tr", "sleep", {"seconds": 30})
    first = pid_of(pool, "tr")
    # New settings arrive with no budget: the old worker keeps the budget it was started under.
    pool.restart_after_drain("tr", definition())
    assert pool.state("tr") is WorkerState.DRAINING
    pool.sweep()
    assert pid_alive(first)
    (data / "big").write_bytes(os.urandom(3 * MIB))
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        slow.wait(10)
    assert killed.value.code == "folder_budget"
    assert wait_until(lambda: not pid_alive(first))


def test_two_sweeps_release_a_stuck_one_off_permit_once(make_pool):
    import threading

    holder = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        holder.append(StubProcessRuntime())
        return holder[-1]

    pool = make_pool(fast_pool_limits(max_oneshot=1), use_clock=False)
    pool.run_once(WorkerDefinition("draft", kind="daemon", runtime=factory), "echo")
    assert len(pool._stuck) == 1
    slot, entry, _release = pool._stuck[0]
    released = []
    pool._stuck[0] = (slot, entry, lambda: released.append(1))
    holder[0].process.returncode = 0
    # Both passes must have seen the process gone before either removes the item.
    meeting = threading.Barrier(2)
    real = pool._confirmed_gone

    def meet(target):
        answer = real(target)
        try:
            meeting.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        return answer

    pool._confirmed_gone = meet
    workers = [threading.Thread(target=pool._retry_stuck) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert released == [1]
    assert pool._stuck == []


# ------------------------------------------------- a launcher's exit is not its cleanup


def _launcher_stub(how, record):
    """A real ``LauncherProcess`` around a sleeper, inside a runtime that ends it as ``how``.

    ``record`` is what the sleeper's status pipe holds when it is ended: the cleanup
    record a launcher would have written, or None for a launcher that wrote none.
    """
    import signal

    from provider_hub.sandbox import LauncherProcess

    class Runtime(StubProcessRuntime):
        def __init__(self):
            super().__init__()
            # Leaves with status 0 on SIGTERM, as a launcher does after its cleanup.
            code = ("import signal, sys, time\n"
                    "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
                    "print('ready', flush=True)\n"
                    "time.sleep(60)\n")
            self.status_r, self.status_w = os.pipe()
            os.set_blocking(self.status_r, False)
            self.process = LauncherProcess(
                [sys.executable, "-c", code], lifeline=None, status=self.status_r, env={}, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert self.process.stdout.readline().strip() == b"ready"

        def close(self, grace=5.0):
            self.closed_with = grace
            if record is not None:
                os.write(self.status_w, record.encode() + b"\n")
            os.close(self.status_w)
            if how == "killed":
                self.process.send_signal(signal.SIGKILL)
            else:
                self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=10)

    return Runtime


COMPLETE = json.dumps({"cleanup": "complete"})
SURVIVORS = json.dumps({"cleanup": "survivors", "count": 2})


def _launcher_pool(make_pool, cls):
    runtimes = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(cls())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=1), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.warm("a")
    return pool, runtimes


@linux_only
@pytest.mark.parametrize("how,record", [
    ("clean", SURVIVORS),
    ("clean", None),
    ("killed", COMPLETE),
    ("killed", None),
], ids=["survivors", "no-record", "killed-after-record", "killed"])
def test_a_launcher_without_a_complete_cleanup_record_keeps_the_slot(make_pool, how, record):
    pool, runtimes = _launcher_pool(make_pool, _launcher_stub(how, record))
    pool.stop("a")
    assert runtimes[0].process.returncode is not None
    # The launcher is gone but its descendants may not be: the place stays taken.
    assert pool.state("a") is WorkerState.STOPPING
    status = pool.status()
    assert status["processes"] == 1 and status["stuck"] == 1
    pool.sweep()
    assert pool.status()["stuck"] == 1 and pool.status()["processes"] == 1


@linux_only
def test_a_launcher_that_exited_by_itself_and_said_complete_releases_the_slot(make_pool):
    pool, runtimes = _launcher_pool(make_pool, _launcher_stub("clean", COMPLETE))
    pool.stop("a")
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)
    assert wait_until(lambda: pool.status()["processes"] == 0 and pool.status()["stuck"] == 0)


# ------------------------------------------------ capacity follows teardown, not failure


def test_a_dead_worker_still_counts_until_its_teardown_is_done(make_pool):
    import threading

    release = threading.Event()
    runtimes = []

    class SlowRuntime(StubProcessRuntime):
        @property
        def alive(self):
            return self.started and self.alive_flag

        def close(self, grace=5.0):
            release.wait(10)
            self.process.returncode = 0
            super().close(grace)

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(SlowRuntime())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=1, acquire_timeout=0.5), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.register(WorkerDefinition("b", kind="daemon", runtime=factory))
    pool.warm("a")
    # The session failed; its process is not confirmed gone yet.
    runtimes[0].alive_flag = False
    pool.sweep()
    assert pool.state("a") is not WorkerState.READY
    assert pool.status()["processes"] == 1
    with pytest.raises(PoolUnavailable) as full:
        pool.warm("b")
    assert full.value.code == "pool_full"
    release.set()
    assert wait_until(lambda: pool.status()["processes"] == 0)
    pool.warm("b")
    assert pool.state("b") is WorkerState.READY


# --------------------------------------------- removal while a worker is starting


@pytest.mark.parametrize("how", ["unregister", "close"])
def test_a_worker_whose_registration_vanished_during_its_start_is_torn_down(make_pool, how):
    import threading

    entered = threading.Event()
    proceed = threading.Event()
    runtimes = []

    class SlowStart(StubProcessRuntime):
        def start(self):
            entered.set()
            proceed.wait(10)
            super().start()

        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(SlowStart())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    outcome = []

    def starter():
        try:
            pool.warm("a")
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)

    starting = threading.Thread(target=starter)
    starting.start()
    assert entered.wait(5)
    remover = threading.Thread(target=pool.unregister if how == "unregister" else (lambda key: pool.close()),
                               args=("a",))
    remover.start()
    # The process in the making still counts while its entry is gone.
    assert wait_until(lambda: how == "close" or "a" not in pool._entries)
    time.sleep(0.2)
    assert pool.status()["processes"] == 1
    proceed.set()
    starting.join(10)
    remover.join(10)
    assert not starting.is_alive() and not remover.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], (UnknownWorker, PoolClosed))
    # Never published, and its teardown ran to the end.
    assert runtimes[0].closed_with is not None
    assert wait_until(lambda: pool.status()["processes"] == 0 and pool.status()["stuck"] == 0)
    if how == "unregister":
        assert "a" not in pool._entries


def test_a_worker_reported_stuck_twice_is_listed_once_and_keeps_its_release(make_pool):
    pool = make_pool(use_clock=False)
    slot = pool_module._Slot(definition(), 0.0, pool.limits)
    freed = []
    with pool._cond:
        pool._add_stuck(slot, None, None)
        pool._add_stuck(slot, None, lambda: freed.append(1))
        pool._add_stuck(slot, None, None)
    assert len(pool._stuck) == 1
    assert pool.status()["processes"] == 1
    pool._stuck[0][2]()
    assert freed == [1]
    pool._stuck.clear()


def test_a_draining_worker_keeps_the_memory_limit_it_started_with(make_pool):
    pool = make_pool(memory_reader=lambda pid: MemorySample(200 * MIB, 200 * MIB, 1, "test"))
    pool.register(definition(memory_limit_bytes=500 * MIB))
    slow = pool.submit("tr", "sleep", {"seconds": 1.0})
    first = pid_of(pool, "tr")
    # New settings lower the limit; the worker already running was started under 500 MiB.
    pool.restart_after_drain("tr", definition(memory_limit_bytes=100 * MIB))
    assert pool.state("tr") is WorkerState.DRAINING
    pool.sweep()
    assert pid_alive(first)
    assert slow.wait(10).result == {"slept": 1.0}


# ------------------------------------------------ shutdown during a one-off start


def test_closing_the_pool_during_a_one_off_start_leaves_no_worker_behind(make_pool):
    import threading

    entered = threading.Event()
    proceed = threading.Event()
    runtimes = []

    class SlowStart(StubProcessRuntime):
        def start(self):
            entered.set()
            proceed.wait(10)
            super().start()

        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(SlowStart())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(), use_clock=False)
    outcome = []

    def one_off():
        try:
            pool.run_once(WorkerDefinition("draft", kind="daemon", runtime=factory), "echo")
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)

    runner = threading.Thread(target=one_off)
    runner.start()
    assert entered.wait(5)
    closer = threading.Thread(target=pool.close)
    closer.start()
    time.sleep(0.2)
    # close() waits for the start; it has not returned and the worker is not torn down yet.
    assert closer.is_alive()
    proceed.set()
    runner.join(10)
    closer.join(10)
    assert not runner.is_alive() and not closer.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], PoolClosed)
    assert runtimes[0].closed_with is not None
    assert runtimes[0].process.returncode == 0
    assert wait_until(lambda: pool.status()["processes"] == 0 and pool.status()["stuck"] == 0)


def test_no_one_off_worker_is_spawned_once_the_pool_is_closing(make_pool):
    spawned = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        spawned.append(1)
        return StubProcessRuntime()

    pool = make_pool(fast_pool_limits(), use_clock=False)
    slot = pool_module._Slot(WorkerDefinition("draft", kind="daemon", runtime=factory), 0.0, pool.limits,
                             ephemeral=True)
    pool.close()
    with pytest.raises(PoolClosed):
        pool._open_session(slot)
    assert spawned == [] and slot.session is None


@needs_smaps
def test_a_live_descendant_whose_memory_cannot_be_read_makes_the_sample_incomplete(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    real = pool_module._read_kb
    try:
        assert wait_until(lambda: read_process_tree_memory(os.getpid()).processes >= 2)
        assert read_process_tree_memory(os.getpid()).truncated is False

        def denying(path, *names):
            if "/proc/%d/" % child.pid in path:
                raise PermissionError(13, "Permission denied")
            return real(path, *names)

        monkeypatch.setattr(pool_module, "_read_kb", denying)
        sample = read_process_tree_memory(os.getpid())
        assert sample is not None and sample.truncated is True

        def vanished(path, *names):
            if "/proc/%d/" % child.pid in path:
                raise FileNotFoundError(2, "No such file or directory")
            return real(path, *names)

        # A process that exited between the listing and the read is not an unknown.
        monkeypatch.setattr(pool_module, "_read_kb", vanished)
        assert read_process_tree_memory(os.getpid()).truncated is False
    finally:
        monkeypatch.undo()
        child.kill()
        child.wait()


def test_a_tree_with_an_unreadable_live_process_is_killed_as_over_its_limit(make_pool):
    late = {"bad": False}
    pool = make_pool(memory_reader=lambda pid: MemorySample(1 * MIB, 1 * MIB, 3, "test", truncated=late["bad"]))
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    late["bad"] = True
    pool.sweep()
    with pytest.raises(WorkerKilled):
        handle.wait(10)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_pid_alive_keeps_the_signal_zero_answer_when_proc_is_unreadable(monkeypatch):
    real_open = open

    def denied(path, *args, **kwargs):
        if str(path).startswith("/proc/%d/stat" % os.getpid()):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    assert pid_alive(os.getpid()) is True


# ------------------------------------------ a worker is judged by its own definition


def test_a_restart_while_a_worker_is_being_sampled_does_not_change_its_limits(make_pool):
    calls = []
    armed = []

    def reader(pid):
        calls.append(pid)
        if armed and len(calls) == 1:
            # New settings with a lower limit arrive in the middle of the sweep.
            pool.restart_after_drain("tr", definition(memory_limit_bytes=100 * MIB))
        return MemorySample(200 * MIB, 200 * MIB, 1, "test")

    pool = make_pool(memory_reader=reader)
    pool.register(definition(memory_limit_bytes=500 * MIB))
    slow = pool.submit("tr", "sleep", {"seconds": 0.8})
    first = pid_of(pool, "tr")
    calls.clear()
    armed.append(1)
    pool.sweep()
    assert calls, "the sweep never sampled"
    assert pool.state("tr") is WorkerState.DRAINING
    assert pid_alive(first)
    assert slow.wait(10).result == {"slept": 0.8}


def test_a_start_is_judged_by_the_definition_it_was_started_under(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "big").write_bytes(os.urandom(3 * MIB))
    pool = make_pool()
    started_under = definition()
    pool.register(started_under)
    entry = pool._entry("tr")
    slot = pool_module._Slot(started_under, 0.0, pool.limits)
    with pool._cond:
        pool._set_state(entry, WorkerState.STARTING, "request")
        # Settings with a folder budget it exceeds arrive between the lease and the start.
        entry.definition = definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")])
        entry.generation += 1
    got_entry, got_slot = pool._start_slot(entry, slot)
    assert got_slot is slot
    # It started, and drains because the settings changed meanwhile.
    assert pool.state("tr") is WorkerState.DRAINING


def test_a_running_worker_keeps_its_folder_budget_when_settings_change(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pool = make_pool()
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    slot_first = None
    pool.warm("tr")
    entry = pool._entry("tr")
    slot_first = entry.slot
    # New settings drop the budget; the cached answer belongs to the slot, not the entry.
    entry.definition = definition()
    (data / "big").write_bytes(os.urandom(3 * MIB))
    with pytest.raises(PoolUnavailable) as refused:
        pool._preflight_running(entry, slot_first)
    assert refused.value.code == "folder_budget"


def test_a_kill_decided_before_a_retirement_ends_the_retiring_worker_cleanly(make_pool):
    pool = make_pool()
    pool.register(definition())
    slow = pool.submit("tr", "sleep", {"seconds": 30})
    entry = pool._entry("tr")
    slot = entry.slot
    pool.restart_after_drain("tr", definition())
    assert slot in entry.retiring
    pool._kill_current(entry, slot, ("folder_budget", "over its budget"))
    with pytest.raises(WorkerKilled):
        slow.wait(10)
    assert slot not in entry.retiring
    # A slot the pool no longer holds is left alone.
    other = pool_module._Slot(definition(), 0.0, pool.limits)
    pool._kill_current(entry, other, ("folder_budget", "over its budget"))
    assert other.kill_reason is None


@needs_smaps
def test_a_tree_where_nothing_can_be_read_is_unknown_not_empty(monkeypatch):
    def denying(path, *names):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pool_module, "_read_kb", denying)
    sample = read_process_tree_memory(os.getpid())
    assert sample is not None and sample.truncated is True and sample.processes == 0
    monkeypatch.undo()
    # A process that is simply not there is still None.
    assert read_process_tree_memory(2 ** 22 + 12345) is None


def test_a_worker_whose_every_process_denies_the_read_is_over_budget(make_pool, monkeypatch):
    def denying(path, *names):
        raise PermissionError(13, "Permission denied")

    pool = make_pool(memory_reader=read_process_tree_memory)
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    monkeypatch.setattr(pool_module, "_read_kb", denying)
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"


# ------------------------------------------- bookkeeping settles without wait()


def test_requests_that_are_only_polled_settle_their_bookkeeping(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition())
    for _ in range(1000):
        request = pool.submit("tr", "echo", {"n": 1}, context=object())
        deadline = time.monotonic() + 10
        while not request.done() and time.monotonic() < deadline:
            time.sleep(0.0005)
        assert request.done()
    slot = pool._entry("tr").slot
    assert wait_until(lambda: slot.active == 0 and not slot.contexts, timeout=10), (slot.active, len(slot.contexts))


def test_a_cancelled_request_nobody_waits_on_still_settles(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition())
    request = pool.submit("tr", "sleep", {"seconds": 30}, context=object())
    slot = pool._entry("tr").slot
    assert slot.active == 1 and len(slot.contexts) == 1
    request.cancel()
    assert wait_until(request.done)
    assert wait_until(lambda: slot.active == 0 and not slot.contexts)


def test_a_caller_that_gets_its_result_finds_the_books_settled(make_pool, clock):
    pool = make_pool()
    pool.register(definition())
    slot = None
    for _ in range(200):
        pool.run("tr", "echo")
        slot = pool._entry("tr").slot
        # Settled by the time run() returns, whichever thread got there first.
        assert slot.active == 0 and not slot.contexts


# --------------------------------------------- failures are logged without their text


SECRET_TEXT = "hunter2-very-secret-token"


def _assert_nothing_leaked(caplog):
    assert SECRET_TEXT not in caplog.text
    assert not any(record.exc_info for record in caplog.records), "a traceback was attached to a log record"


def test_an_event_sink_that_fails_with_a_secret_is_logged_by_type_only(make_pool, caplog):
    caplog.set_level(logging.DEBUG)

    def sink(key, name, payload):
        raise RuntimeError("credential %s in the callback" % SECRET_TEXT)

    pool = make_pool(event_sink=sink)
    pool.register(definition())
    pool.run("tr", "echo")
    pool.stop("tr")
    assert "pool event sink failed (RuntimeError)" in caplog.text
    _assert_nothing_leaked(caplog)


def test_a_failing_sweep_is_logged_by_type_only(make_pool, caplog):
    import threading

    caplog.set_level(logging.DEBUG)
    pool = make_pool(fast_pool_limits(tick=0.01), use_clock=False)

    def broken():
        pool._sampler_stop.set()
        raise RuntimeError("token %s" % SECRET_TEXT)

    pool.sweep = broken
    thread = threading.Thread(target=pool._sampler_loop)
    thread.start()
    thread.join(5)
    assert "worker pool sweep failed (RuntimeError)" in caplog.text
    _assert_nothing_leaked(caplog)


def test_a_stuck_retry_and_a_failed_close_are_logged_by_type_only(make_pool, caplog):
    caplog.set_level(logging.DEBUG)

    class Leaky(StubProcessRuntime):
        def close(self, grace=5.0):
            raise RuntimeError("close saw %s" % SECRET_TEXT)

    pool = make_pool(fast_pool_limits(), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *args: Leaky()))
    pool.warm("a")
    pool.stop("a")
    pool.sweep()
    assert "worker close failed (RuntimeError)" in caplog.text
    _assert_nothing_leaked(caplog)


# ------------------------------------------------ stop() reaches every worker of a plugin


def test_stop_during_a_start_cancels_it_and_nothing_becomes_ready(make_pool):
    import threading

    entered = threading.Event()
    proceed = threading.Event()
    runtimes = []

    class SlowStart(StubProcessRuntime):
        def start(self):
            entered.set()
            proceed.wait(10)
            super().start()

        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(SlowStart())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    outcome = []

    def starter():
        try:
            pool.warm("a")
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)

    starting = threading.Thread(target=starter)
    starting.start()
    assert entered.wait(5)
    stopper = threading.Thread(target=pool.stop, args=("a",))
    stopper.start()
    time.sleep(0.2)
    assert stopper.is_alive(), "stop() returned before the cancelled start had ended"
    proceed.set()
    starting.join(10)
    stopper.join(10)
    assert not starting.is_alive() and not stopper.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], PoolUnavailable)
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)
    assert runtimes[0].closed_with is not None
    assert pool.status()["processes"] == 0 and pool.status()["stuck"] == 0
    # The next request simply starts a new worker.
    proceed.set()
    pool.warm("a")
    assert pool.state("a") is WorkerState.READY


def test_stop_after_restart_after_drain_ends_the_draining_request_too(make_pool):
    pool = make_pool()
    pool.register(definition())
    retired = pool.submit("tr", "sleep", {"seconds": 30})
    first = pid_of(pool, "tr")
    pool.restart_after_drain("tr", definition())
    # The replacement is ready and serving; the retired worker still holds its request.
    pool.run("tr", "echo")
    second = pid_of(pool, "tr")
    assert second != first and pid_alive(first)
    pool.stop("tr")
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)
    assert wait_until(lambda: not pid_alive(first)) and wait_until(lambda: not pid_alive(second))
    with pytest.raises((SessionError, RequestCancelled, WorkerKilled)):
        retired.wait(10)
    assert pool.status()["processes"] == 0


def test_stop_with_only_draining_workers_ends_them(make_pool):
    pool = make_pool()
    pool.register(definition())
    retired = pool.submit("tr", "sleep", {"seconds": 30})
    first = pid_of(pool, "tr")
    pool.restart_after_drain("tr", definition())
    assert pool.state("tr") is WorkerState.DRAINING
    pool.stop("tr")
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED)
    assert wait_until(lambda: not pid_alive(first))
    with pytest.raises((SessionError, RequestCancelled, WorkerKilled)):
        retired.wait(10)


# ---------------------------------------------- the table in the module docstring


def test_unregister_kills_a_draining_worker_too(make_pool):
    pool = make_pool()
    pool.register(definition())
    retired = pool.submit("tr", "sleep", {"seconds": 30})
    first = pid_of(pool, "tr")
    pool.restart_after_drain("tr", definition())
    pool.run("tr", "echo")
    second = pid_of(pool, "tr")
    pool.unregister("tr")
    assert wait_until(lambda: not pid_alive(first)) and wait_until(lambda: not pid_alive(second))
    with pytest.raises((SessionError, RequestCancelled, WorkerKilled)):
        retired.wait(10)
    assert wait_until(lambda: pool.status()["processes"] == 0)


def test_a_restart_during_a_start_lets_the_start_finish_and_then_drains_it(make_pool):
    import threading

    entered = threading.Event()
    proceed = threading.Event()
    runtimes = []

    class SlowStart(StubProcessRuntime):
        def start(self):
            entered.set()
            proceed.wait(10)
            super().start()

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(SlowStart())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    starter = threading.Thread(target=pool.warm, args=("a",))
    starter.start()
    assert entered.wait(5)
    pool.restart_after_drain("a", WorkerDefinition("a", kind="daemon", runtime=factory))
    proceed.set()
    starter.join(10)
    # It came up for the request that asked for it and drains, rather than serving others.
    assert pool.state("a") is WorkerState.DRAINING
    assert runtimes[0].closed_with is None


def test_a_stuck_worker_is_ended_again_by_stop_unregister_and_close(make_pool):
    for how in ("stop", "unregister", "close"):
        holder = []

        def factory(definition, limits, host_calls, on_event, on_stderr, holder=holder):
            holder.append(StubProcessRuntime())
            return holder[-1]

        pool = make_pool(fast_pool_limits(), use_clock=False)
        pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
        pool.warm("a")
        pool.stop("a")
        assert pool.status()["stuck"] == 1, how
        holder[0].process.returncode = 0
        if how == "stop":
            pool.stop("a")
        elif how == "unregister":
            pool.unregister("a")
        else:
            pool.close()
        assert wait_until(lambda: pool.status()["stuck"] == 0 and pool.status()["processes"] == 0), how


def test_lowering_the_cap_leaves_busy_and_draining_workers_alone(make_pool):
    pool = make_pool(fast_pool_limits(max_resident=3))
    for key in "ab":
        pool.register(definition(key))
    busy = pool.submit("a", "sleep", {"seconds": 0.8})
    retired = pool.submit("b", "sleep", {"seconds": 0.8})
    pool.restart_after_drain("b", definition("b"))
    pool.set_max_resident(1)
    assert pool.state("a") is WorkerState.BUSY and pool.state("b") is WorkerState.DRAINING
    assert busy.wait(10).result == {"slept": 0.8} and retired.wait(10).result == {"slept": 0.8}


# ------------------------------------- a plugin is stopped only when every generation is


def test_an_entry_is_not_stopped_while_one_generation_is_stuck(make_pool):
    runtimes = []

    class Survivor(StubProcessRuntime):
        """Its close never gets the process out of the way."""

    class Obedient(StubProcessRuntime):
        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(Survivor() if not runtimes else Obedient())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=8, acquire_timeout=0.4), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.warm("a")
    pool.restart_after_drain("a", WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.warm("a")
    assert len(runtimes) == 2 and pool.state("a") is WorkerState.READY
    pool.stop("a")
    # The second generation is gone, the first is stuck: the plugin is still stopping.
    assert runtimes[1].process.returncode == 0 and runtimes[0].process.returncode is None
    assert pool.state("a") is WorkerState.STOPPING
    assert pool.status()["stuck"] == 1
    # With plenty of spare capacity, no second instance starts beside the survivor.
    with pytest.raises(PoolUnavailable):
        pool.warm("a")
    assert len(runtimes) == 2
    runtimes[0].process.returncode = 0
    pool.sweep()
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)
    pool.warm("a")
    assert len(runtimes) == 3 and pool.state("a") is WorkerState.READY


# --------------------------------- an enumeration or a read that falls short is incomplete


def test_a_capped_proc_scan_marks_the_tree_incomplete(monkeypatch):
    real_listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path: [str(n) for n in range(1, 50)] if path == "/proc" else real_listdir(path))
    tree, complete = pool_module._parent_map(limit=10)
    assert complete is False
    # Children lists unavailable and the scan falls short: the sample cannot be called complete.
    monkeypatch.undo()
    monkeypatch.setattr(pool_module, "_children_of", lambda pid: (None, False))
    monkeypatch.setattr(pool_module, "_parent_map", lambda limit=0: ({}, False))
    members, incomplete = pool_module._process_tree(os.getpid(), 256)
    assert incomplete is True and members == [os.getpid()]
    monkeypatch.setattr(pool_module, "_parent_map", lambda limit=0: ({}, True))
    assert pool_module._process_tree(os.getpid(), 256) == ([os.getpid()], False)


def test_a_proc_record_that_cannot_be_read_marks_the_scan_incomplete(monkeypatch):
    real_open = open

    def denying(path, *args, **kwargs):
        if str(path).startswith("/proc/") and str(path).endswith("/stat") and "/task/" not in str(path):
            if str(path).split("/")[2] == str(os.getpid()):
                raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denying)
    tree, complete = pool_module._parent_map()
    assert complete is False
    monkeypatch.undo()
    tree, complete = pool_module._parent_map()
    assert complete is True and os.getpid() in [p for kids in tree.values() for p in kids]


def test_an_unreadable_children_list_marks_the_walk_incomplete(monkeypatch):
    real_open = open

    def broken(path, *args, **kwargs):
        if str(path).endswith("/children"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", broken)
    kids, incomplete = pool_module._children_of(os.getpid())
    assert incomplete is True
    # Every list unreadable: the kernel's lists are as good as absent, and the scan takes over.
    assert kids is None


@needs_smaps
def test_a_missing_smaps_rollup_falls_back_to_status_instead_of_dropping_the_process(monkeypatch):
    real = pool_module._read_kb

    def no_smaps(path, *names):
        if path.endswith("/smaps_rollup"):
            raise FileNotFoundError(2, "No such file or directory")
        return real(path, *names)

    monkeypatch.setattr(pool_module, "_read_kb", no_smaps)
    sample = read_process_tree_memory(os.getpid())
    assert sample is not None and sample.source == "status" and sample.bytes > 0 and not sample.truncated


# ---------------------------------------- a one-off run is policed like a resident worker


def test_a_one_off_worker_that_fills_its_folder_while_starting_is_refused_work(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()

    class FillsOnStart(StubProcessRuntime):
        def start(self):
            (data / "big").write_bytes(os.urandom(3 * MIB))
            super().start()

    submitted = []

    class Counting(FillsOnStart):
        def submit(self, op, params=None, **kwargs):
            submitted.append(op)
            return super().submit(op, params, **kwargs)

    pool = make_pool(fast_pool_limits(), use_clock=False)
    draft = WorkerDefinition("draft", kind="daemon", runtime=lambda *args: Counting(),
                             budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")])
    with pytest.raises(PoolUnavailable) as refused:
        pool.run_once(draft, "echo")
    assert refused.value.code == "folder_budget"
    assert submitted == []


def test_a_one_off_worker_that_is_too_big_after_init_gets_no_work(make_pool):
    calls = []

    def reader(pid):
        calls.append(pid)
        return MemorySample(500 * MIB, 500 * MIB, 1, "test")

    pool = make_pool(memory_reader=reader, use_clock=False)
    with pytest.raises(WorkerKilled) as killed:
        pool.run_once(definition("draft", memory_limit_bytes=100 * MIB), "sleep", {"seconds": 5})
    assert killed.value.code == "memory_limit" and calls


def test_a_long_one_off_run_is_sampled_while_it_runs(make_pool):
    calls = []

    def reader(pid):
        calls.append(pid)
        # Fine at first, then it balloons while the request is still running.
        return MemorySample((10 if len(calls) < 4 else 500) * MIB, 10 * MIB, 1, "test")

    pool = make_pool(fast_pool_limits(heartbeat_interval=0.1), memory_reader=reader, use_clock=False)
    started = time.monotonic()
    with pytest.raises(WorkerKilled) as killed:
        pool.run_once(definition("draft", memory_limit_bytes=100 * MIB), "sleep", {"seconds": 20}, timeout=20)
    assert killed.value.code == "memory_limit"
    assert time.monotonic() - started < 10 and len(calls) >= 4


def test_a_one_off_run_is_sampled_once_more_when_it_finishes(make_pool):
    calls = []

    def reader(pid):
        calls.append(pid)
        return MemorySample(10 * MIB, 10 * MIB, 1, "test")

    pool = make_pool(memory_reader=reader, use_clock=False)
    pool.run_once(definition("draft"), "echo")
    # Before the work, and after it.
    assert len(calls) >= 2


# ---------------------------------------------------- evidence that fails closed


@linux_only
def test_an_anchored_worker_is_gone_only_when_its_anchor_has_been_let_go():
    from provider_hub.sandbox import spawn as spawn_module

    process = spawn_module._start_plain([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=10)
        assert process.returncode is not None
        if process._anchor is None:
            pytest.skip("no process group anchor on this interpreter")
        # The leader is reaped, but its group is still held: not confirmed.
        assert WorkerPool._cleanup_confirmed(process) is False
    finally:
        process.stop(0.5)
    assert WorkerPool._cleanup_confirmed(process) is True
    process._anchor_lost = True
    assert WorkerPool._cleanup_confirmed(process) is False
    process._anchor_lost = False
    process._leader_lost = True
    assert WorkerPool._cleanup_confirmed(process) is False


def test_a_memory_reader_that_fails_is_not_read_as_zero(make_pool):
    def broken(pid):
        raise RuntimeError("reader failed")

    late = {"bad": False}

    def reader(pid):
        if late["bad"]:
            return broken(pid)
        return MemorySample(1 * MIB, 1 * MIB, 1, "test")

    pool = make_pool(memory_reader=reader)
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    late["bad"] = True
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"


# --------------------------------------------- nothing is sent to a worker that is over its limit


@pytest.mark.parametrize("sample", [
    MemorySample(500 * MIB, 500 * MIB, 1, "test"),
    MemorySample(1 * MIB, 1 * MIB, 3, "test", truncated=True),
], ids=["over-limit", "incomplete"])
def test_a_request_is_not_sent_to_a_worker_whose_latest_sample_fails(make_pool, sample):
    state = {"bad": False}

    def reader(pid):
        # Fine while it starts; the sample taken right before dispatch fails.
        return sample if state["bad"] else MemorySample(10 * MIB, 10 * MIB, 1, "test")

    sent = []

    class Counting(StubProcessRuntime):
        def submit(self, op, params=None, **kwargs):
            sent.append(op)
            return super().submit(op, params, **kwargs)

    pool = make_pool(memory_reader=reader)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *args: Counting(),
                                   memory_limit_bytes=100 * MIB))
    pool.warm("a")
    first = pool._entry("a").slot
    state["bad"] = True
    with pytest.raises(WorkerKilled) as refused:
        pool.submit("a", "echo")
    assert refused.value.code == "memory_limit"
    assert sent == []
    assert pool._entry("a").slot is not first


# --------------------------- forgetting a plugin does not forget a teardown that is unconfirmed


def test_a_plugin_registered_again_does_not_start_beside_a_survivor(make_pool):
    runtimes = []

    class Survivor(StubProcessRuntime):
        """Its process stays until the test says it is gone."""

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(Survivor())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=8, acquire_timeout=0.4), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.warm("a")
    pool.unregister("a")
    assert pool.status()["stuck"] == 1
    # The key is registered again (new settings, same writable folders).
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    with pytest.raises(PoolUnavailable):
        pool.warm("a")
    with pytest.raises(PoolUnavailable):
        pool.run_once(WorkerDefinition("a", kind="daemon", runtime=factory), "echo")
    assert len(runtimes) == 1, "a second worker started beside the survivor"
    runtimes[0].process.returncode = 0
    pool.sweep()
    assert wait_until(lambda: pool.status()["stuck"] == 0)
    pool.warm("a")
    assert len(runtimes) == 2 and pool.state("a") is WorkerState.READY


@pytest.mark.skipif(not hasattr(os, "waitid") or os.name != "posix", reason="needs POSIX process groups")
def test_a_leader_reaped_by_somebody_else_does_not_prove_its_descendants_gone(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition())
    import threading

    from provider_hub.runtime.session import WorkerSession

    child = pool.run("tr", "fork", {}).result["child"]
    leader = pid_of(pool, "tr")
    assert pid_alive(child)
    # The session's reader would race the outside reaper to the dead leader; hold it back.
    outside_reaper_done = threading.Event()
    real_reap = WorkerSession._reap_exit_code

    def late_reap(self):
        outside_reaper_done.wait(10)
        return real_reap(self)

    WorkerSession._reap_exit_code = late_reap
    try:
        # An outside reaper takes the leader while its descendant lives on.
        os.kill(leader, 9)
        os.waitpid(leader, 0)
        outside_reaper_done.set()
        pool.stop("tr")
        assert pid_alive(child), "the session had no group to signal and signalled none"
        # Nothing vouches for the descendant: the worker is stuck, not stopped.
        assert pool.state("tr") is WorkerState.STOPPING
        assert pool.status()["stuck"] == 1 and pool.status()["processes"] == 1
    finally:
        WorkerSession._reap_exit_code = real_reap
        outside_reaper_done.set()
        try:
            os.kill(child, 9)
        except OSError:
            pass


def test_a_session_that_ended_its_own_group_is_confirmed(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition())
    child = pool.run("tr", "fork", {}).result["child"]
    pool.stop("tr")
    assert wait_until(lambda: pool.state("tr") is WorkerState.STOPPED and pool.status()["stuck"] == 0)
    assert wait_until(lambda: not pid_alive(child))


def test_a_plugin_registered_again_while_its_cut_off_start_is_running_waits_for_it(make_pool):
    import threading

    entered = threading.Event()
    proceed = threading.Event()
    runtimes = []

    class SlowStart(StubProcessRuntime):
        def start(self):
            entered.set()
            proceed.wait(10)
            super().start()

        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    def factory(definition, limits, host_calls, on_event, on_stderr):
        runtimes.append(SlowStart())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=8, acquire_timeout=0.4), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    starter = threading.Thread(target=lambda: _quiet(pool.warm, "a"))
    starter.start()
    assert entered.wait(5)
    remover = threading.Thread(target=pool.unregister, args=("a",))
    remover.start()
    assert wait_until(lambda: "a" not in pool._entries)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    # The first start is still spawning; a second one must not begin beside it.
    with pytest.raises(PoolUnavailable):
        pool.warm("a")
    assert len(runtimes) == 1
    proceed.set()
    starter.join(10)
    remover.join(10)
    assert not starter.is_alive() and not remover.is_alive()
    pool.warm("a")
    assert len(runtimes) == 2 and pool.state("a") is WorkerState.READY


def _quiet(call, *args):
    try:
        call(*args)
    except BaseException:
        pass


# ------------------------------- one-off teardowns count, and the key is rechecked when it publishes


def test_an_unconfirmed_one_off_teardown_blocks_the_key_even_with_spare_permits(make_pool):
    holder = []

    def factory(definition, limits, host_calls, on_event, on_stderr):
        holder.append(StubProcessRuntime())
        return holder[-1]

    pool = make_pool(fast_pool_limits(max_oneshot=2, acquire_timeout=0.4), use_clock=False)
    draft = WorkerDefinition("a", kind="daemon", runtime=factory)
    pool.run_once(draft, "echo")
    assert pool.status()["stuck"] == 1
    # One permit is free, but the survivor is on the same writable folders.
    with pytest.raises(PoolUnavailable):
        pool.run_once(draft, "echo")
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    with pytest.raises(PoolUnavailable):
        pool.warm("a")
    assert len(holder) == 1
    holder[0].process.returncode = 0
    pool.sweep()
    assert wait_until(lambda: pool.status()["stuck"] == 0)
    # A different key was never affected.
    pool.run_once(WorkerDefinition("b", kind="daemon", runtime=factory), "echo")
    pool.warm("a")
    assert pool.state("a") is WorkerState.READY


def test_a_resident_that_becomes_stuck_while_a_one_off_waits_for_its_permit_blocks_it(make_pool):
    import threading

    release = threading.Event()
    running = threading.Event()
    started = []

    class Blocking(StubProcessRuntime):
        def submit(self, op, params=None, **kwargs):
            from provider_hub.runtime.session import RequestResult

            running.set()

            class Handle:
                id = 1

                def done(self):
                    return release.is_set()

                def wait(self, timeout=None):
                    if not release.wait(timeout):
                        raise TimeoutError("still running")
                    return RequestResult({"op": op})

            return Handle()

        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    def draft_factory(definition, limits, host_calls, on_event, on_stderr):
        started.append("draft")
        return Blocking()

    class Survivor(StubProcessRuntime):
        pass

    pool = make_pool(fast_pool_limits(max_oneshot=1, acquire_timeout=5.0), use_clock=False)
    first = threading.Thread(
        target=lambda: _quiet(pool.run_once, WorkerDefinition("a", kind="daemon", runtime=draft_factory), "echo"))
    first.start()
    assert running.wait(5)
    outcome = []

    def second():
        try:
            pool.run_once(WorkerDefinition("a", kind="daemon", runtime=draft_factory), "echo")
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)

    waiter = threading.Thread(target=second)
    waiter.start()
    time.sleep(0.3)
    assert waiter.is_alive(), "the second one-off should be waiting for the permit"
    # Meanwhile a resident of the same key is started and left stuck.
    pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *args: Survivor()))
    pool.warm("a")
    pool.stop("a")
    assert pool.status()["stuck"] == 1
    release.set()
    first.join(10)
    waiter.join(10)
    assert len(outcome) == 1 and isinstance(outcome[0], PoolUnavailable)
    assert started == ["draft"], "a second one-off worker started beside the survivor"


# ----------------------------- hosts without a launcher: unverified teardowns are counted and capped


class FallbackProcess:
    """A worker handle of a host with no subreaper launcher: ended by group, tree not provable."""

    def __init__(self):
        self.pid = 4242
        self.returncode = None


class FallbackRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.process = FallbackProcess()

    @property
    def alive(self):
        return self.started and self.process.returncode is None

    def close(self, grace=5.0):
        self.process.returncode = 0
        super().close(grace)


def test_workers_ended_without_whole_tree_proof_are_counted_logged_and_capped(make_pool, clock, caplog):
    caplog.set_level(logging.INFO)
    pool = make_pool(fast_pool_limits(unverified_limit=3, unverified_window=3600.0, max_resident=8))
    pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *args: FallbackRuntime()))
    pool.register(WorkerDefinition("b", kind="daemon", runtime=lambda *args: FallbackRuntime()))
    for _ in range(3):
        pool.warm("a")
        pool.stop("a")
    status = pool.status()["unverified_teardowns"]
    assert status["a"]["count"] == 3 and status["a"]["limit"] == 3
    assert "detached processes cannot be tracked on this platform" in status["a"]["note"]
    # One INFO line for the key, not one per teardown.
    lines = [r for r in caplog.records if "cannot be tracked on this platform" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    # The cap: no new start for the key, with a clear error, until the window passes.
    with pytest.raises(PoolUnavailable) as capped:
        pool.warm("a")
    assert capped.value.code == "unverified_limit" and "cannot be tracked" in str(capped.value)
    assert capped.value.retry_after and capped.value.retry_after > 3000
    with pytest.raises(PoolUnavailable):
        pool.run_once(WorkerDefinition("a", kind="daemon", runtime=lambda *args: FallbackRuntime()), "echo")
    # Other keys are not affected.
    pool.warm("b")
    clock.advance(3601)
    assert pool.status()["unverified_teardowns"].get("a") is None
    pool.warm("a")
    assert pool.state("a") is WorkerState.READY


def test_a_launcher_worker_is_never_counted_as_unverified(make_pool):
    pool = make_pool(fast_pool_limits(unverified_limit=1))
    pool.register(definition("tr"))
    # A plain child (no launcher) counts; the seam runtime with no local process does not.
    pool.register(WorkerDefinition("daemon", kind="daemon", runtime=lambda *args: StubRuntime()))
    for _ in range(3):
        pool.warm("daemon")
        pool.stop("daemon")
    assert "daemon" not in pool.status()["unverified_teardowns"]


def test_the_sandbox_status_says_detached_processes_cannot_be_tracked_where_that_is_so(monkeypatch):
    from provider_hub.sandbox import landlock, sandbox_limitations
    from provider_hub.sandbox.spawn import SandboxProbe

    monkeypatch.setattr(landlock, "is_supported_platform", lambda: False)
    notes = " ".join(sandbox_limitations(SandboxProbe(abi=0, verified=False)))
    assert "Detached processes cannot be tracked on this platform" in notes


# ----------------------------------- files unlinked but still open count against the folder


def _unlinked_file(folder, name, size):
    """An open descriptor on a file of ``size`` bytes that no longer has a directory entry."""
    path = folder / name
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, b"x" * size)
    os.fsync(fd)
    os.unlink(path)
    return fd


@needs_smaps
def test_deleted_open_usage_totals_are_per_folder_by_ancestry_not_by_filesystem(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    elsewhere = tmp_path / "elsewhere"
    for folder in (first, second, elsewhere):
        folder.mkdir()
    # Three folders on one filesystem; a file is charged to the folder it was in, and only that.
    held = [_unlinked_file(first, "a", 2 * MIB), _unlinked_file(second, "b", 1 * MIB),
            _unlinked_file(elsewhere, "c", 3 * MIB)]
    try:
        usage, incomplete = pool_module.deleted_open_usage([os.getpid(), os.getpid()], [str(first), str(second)])
        assert incomplete is False
        assert 2 * MIB <= usage[str(first)] < 2 * MIB + 256 * 1024
        assert 1 * MIB <= usage[str(second)] < 1 * MIB + 256 * 1024
        # The file in the third folder is on the same device and charged to neither.
        usage, _ = pool_module.deleted_open_usage([os.getpid()], [str(first)])
        assert usage[str(first)] < 3 * MIB
        # A parent folder includes its children's files.
        usage, _ = pool_module.deleted_open_usage([os.getpid()], [str(tmp_path)])
        assert usage[str(tmp_path)] >= 6 * MIB
    finally:
        for fd in held:
            os.close(fd)
    usage, _ = pool_module.deleted_open_usage([os.getpid()], [str(first), str(second)])
    assert usage == {str(first): 0, str(second): 0}


@needs_smaps
def test_a_deleted_file_both_open_and_mapped_is_counted_once(tmp_path):
    import mmap

    folder = tmp_path / "data"
    folder.mkdir()
    fd = _unlinked_file(folder, "both", 2 * MIB)
    mapping = mmap.mmap(fd, 2 * MIB)
    try:
        usage, incomplete = pool_module.deleted_open_usage([os.getpid()], [str(folder)])
        assert incomplete is False
        assert 2 * MIB <= usage[str(folder)] < 2 * MIB + 256 * 1024
    finally:
        mapping.close()
        os.close(fd)


def _map_with_c_mmap(fd, length):
    """Map ``fd`` with the C call, so the mapping can outlive the descriptor (Python's mmap keeps a dup)."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
    address = libc.mmap(None, length, 3, 1, fd, 0)  # PROT_READ|PROT_WRITE, MAP_SHARED
    assert address not in (None, ctypes.c_void_p(-1).value)

    def unmap():
        libc.munmap(ctypes.c_void_p(address), ctypes.c_size_t(length))

    return unmap


@needs_smaps
@pytest.mark.parametrize("pages", [1, 512], ids=["one-page-of-a-large-file", "the-whole-file"])
def test_a_deleted_file_referenced_only_by_a_mapping_fails_the_scan_closed(tmp_path, monkeypatch, pages):
    folder = tmp_path / "data"
    folder.mkdir()
    fd = _unlinked_file(folder, "mapped", 2 * MIB)
    unmap = _map_with_c_mmap(fd, pages * 4096)
    os.close(fd)
    real_stat = os.stat

    def no_map_files(path, *args, **kwargs):
        if "/map_files/" in str(path):
            raise PermissionError(1, "Operation not permitted")
        return real_stat(path, *args, **kwargs)

    # An ordinary host cannot dereference map_files, and its extent would say nothing
    # reliable (one page of a huge file; a sparse map): so the folder cannot be vouched for.
    monkeypatch.setattr(os, "stat", no_map_files)
    try:
        usage, incomplete = pool_module.deleted_open_usage([os.getpid()], [str(folder)])
        assert incomplete is True
        assert usage[str(folder)] == 0
    finally:
        unmap()


@needs_smaps
def test_a_mapped_only_deleted_file_makes_the_budget_check_fail(make_pool, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    fd = _unlinked_file(folder, "mapped", 2 * MIB)
    unmap = _map_with_c_mmap(fd, 4096)
    os.close(fd)
    try:
        class Here(StubProcessRuntime):
            def __init__(self):
                super().__init__()
                self.process.pid = os.getpid()

        pool = make_pool(use_clock=False)
        pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *a: Here(),
                                       budgets=[FolderBudget(str(folder), 100 * MIB, "plugin data")]))
        pool.warm("a")
        slot = pool._entry("a").slot
        problem = pool._budget_problem(slot, 0.0, slot.definition, force=True, tree=slot)
        assert problem is not None and problem[0] == "folder_budget"
    finally:
        unmap()


@needs_smaps
def test_a_deleted_file_that_is_open_and_mapped_is_charged_by_its_blocks_not_its_mapping(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    fd = _unlinked_file(folder, "both", 4 * MIB)
    unmap = _map_with_c_mmap(fd, 4096)
    try:
        usage, incomplete = pool_module.deleted_open_usage([os.getpid()], [str(folder)])
        assert incomplete is False
        assert 4 * MIB <= usage[str(folder)] < 4 * MIB + 256 * 1024
    finally:
        unmap()
        os.close(fd)


@needs_smaps
def test_memfds_and_anonymous_mappings_are_never_charged_to_a_folder(tmp_path):
    import mmap

    folder = tmp_path / "data"
    folder.mkdir()
    fd = os.memfd_create("scratch")
    os.write(fd, b"x" * (4 * MIB))
    anonymous = mmap.mmap(-1, 4 * MIB)
    mapped = mmap.mmap(fd, 4 * MIB)
    try:
        usage, incomplete = pool_module.deleted_open_usage([os.getpid()], [str(folder), "/memfd:scratch"])
        assert incomplete is False
        assert usage[str(folder)] == 0
    finally:
        mapped.close()
        anonymous.close()
        os.close(fd)


@needs_smaps
def test_an_unreadable_descriptor_directory_of_a_live_process_is_an_incomplete_scan(tmp_path, monkeypatch):
    real = os.listdir

    def denying(path):
        if str(path) == "/proc/%d/fd" % os.getpid():
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(os, "listdir", denying)
    usage, incomplete = pool_module.deleted_open_usage([os.getpid()], [str(tmp_path)])
    assert incomplete is True
    monkeypatch.undo()
    gone = 2 ** 22 + 99
    assert pool_module.deleted_open_usage([gone], [str(tmp_path)]) == ({str(tmp_path): 0}, False)


@needs_smaps
@pytest.mark.parametrize("where", ["worker", "child"])
def test_a_worker_that_writes_through_an_unlinked_file_is_killed_for_its_folder(make_pool, tmp_path, where):
    data = tmp_path / "data"
    data.mkdir()
    pool = make_pool(use_clock=False)
    pool.register(definition(budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    written = []
    handle = pool.submit("tr", "write_deleted", {"path": str(data / "hidden"), "mb": 4, "child": where == "child"},
                         on_progress=lambda message: written.append(message))
    assert wait_until(lambda: bool(written), timeout=10)
    # The folder looks empty to a directory scan.
    assert folder_usage(str(data)) == (0, False) or folder_usage(str(data))[0] < MIB
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "folder_budget"


# ------------------------------------- the unverified allowance is reserved at admission


class BlockingFallback(FallbackRuntime):
    release = None

    def submit(self, op, params=None, **kwargs):
        from provider_hub.runtime.session import RequestResult

        gate = type(self).release

        class Handle:
            id = 1

            def done(self):
                return gate.is_set()

            def wait(self, timeout=None):
                if not gate.wait(timeout):
                    raise TimeoutError("still running")
                return RequestResult({"op": op})

        return Handle()


def test_simultaneous_admissions_cannot_spend_more_than_the_allowance(make_pool):
    import threading

    gate = threading.Event()

    class Runtime(BlockingFallback):
        release = gate

    pool = make_pool(fast_pool_limits(unverified_limit=20, max_oneshot=2, acquire_timeout=5.0), use_clock=False)
    now = time.monotonic()
    with pool._cond:
        pool._unverified["a"] = deque([now] * 19)
    start = threading.Barrier(2)
    outcomes = []

    def attempt():
        start.wait(5)
        try:
            pool.run_once(WorkerDefinition("a", kind="daemon", runtime=lambda *args: Runtime()), "echo")
            outcomes.append("admitted")
        except PoolUnavailable as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=attempt, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        # One is admitted and runs (blocked on the gate); the other is turned away.
        assert wait_until(lambda: "unverified_limit" in outcomes, timeout=5), outcomes
        assert pool._unverified_reserved.get("a") == 1
    finally:
        gate.set()
    for thread in threads:
        thread.join(10)
    assert sorted(outcomes) == ["admitted", "unverified_limit"]
    # The admitted one ended without proof: its unit became the 20th record.
    assert len(pool._unverified["a"]) == 20 and not pool._unverified_reserved


def test_a_reserved_unit_is_given_back_when_nothing_was_spawned_or_the_launcher_proved_the_tree(make_pool):
    pool = make_pool(fast_pool_limits(unverified_limit=5))
    # A runtime with no local process: nothing to detach from, the unit goes back.
    pool.register(WorkerDefinition("d", kind="daemon", runtime=lambda *args: StubRuntime()))
    for _ in range(3):
        pool.warm("d")
        pool.stop("d")
    assert not pool._unverified_reserved and not pool._unverified.get("d")

    # A start that fails before a session exists gives its unit back too.
    def broken(*args):
        raise RuntimeError("no runtime")

    pool.register(WorkerDefinition("f", kind="daemon", runtime=broken))
    with pytest.raises(PoolUnavailable):
        pool.warm("f")
    assert not pool._unverified_reserved


def test_a_cancelled_start_is_counted_once_while_it_is_still_running(make_pool):
    import threading

    entered = threading.Event()
    proceed = threading.Event()

    class SlowStart(StubProcessRuntime):
        def start(self):
            entered.set()
            proceed.wait(10)
            super().start()

        def close(self, grace=5.0):
            self.process.returncode = 0
            super().close(grace)

    pool = make_pool(fast_pool_limits(max_resident=2, acquire_timeout=0.5), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=lambda *args: SlowStart()))
    pool.register(WorkerDefinition("b", kind="daemon", runtime=lambda *args: StubProcessRuntime()))
    starter = threading.Thread(target=lambda: _quiet(pool.warm, "a"))
    starter.start()
    assert entered.wait(5)
    assert pool.status()["processes"] == 1
    stopper = threading.Thread(target=pool.stop, args=("a",))
    stopper.start()
    try:
        assert wait_until(lambda: pool._entry("a").state is WorkerState.STOPPED)
        # One start in the making, however it is looked at: a cap of two leaves room for another.
        assert pool.status()["processes"] == 1
        pool.warm("b")
        assert pool.state("b") is WorkerState.READY and pool.status()["processes"] == 2
    finally:
        proceed.set()
    starter.join(10)
    stopper.join(10)
    assert not starter.is_alive() and not stopper.is_alive()
    assert pool.status()["processes"] == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")
@needs_smaps
def test_a_healthy_launcher_worker_with_an_empty_folder_is_not_killed_by_the_deleted_file_scan(make_pool, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    build, declared = launcher_definition(tmp_path)
    pool = make_pool(use_clock=False)
    pool.register(WorkerDefinition("tr", kind="translator", spawn=launcher_spawn(build, probe=declared),
                                   session_limits=fast_session(),
                                   budgets=[FolderBudget(str(data), 1 * MIB, "plugin data")]))
    slow = pool.submit("tr", "sleep", {"seconds": 1.0})
    for _ in range(3):
        pool.sweep()
        time.sleep(0.1)
    assert slow.wait(10).result == {"slept": 1.0}
    assert pool.state("tr") is WorkerState.READY
    # The scan of the worker's own tree sees nothing and is complete.
    slot = pool._entry("tr").slot
    usage, incomplete = pool._deleted_open_usage(slot, [FolderBudget(str(data), 1 * MIB)])
    assert incomplete is False and usage.get(str(data), 0) == 0


def test_settling_the_same_worker_twice_counts_it_once(make_pool):
    pool = make_pool(fast_pool_limits(unverified_limit=5), use_clock=False)
    runtime = FallbackRuntime()
    slot = pool_module._Slot(WorkerDefinition("a", kind="daemon", runtime=lambda *args: runtime), 0.0, pool.limits,
                             ephemeral=True)
    slot.session = runtime
    with pool._cond:
        pool._reserve_unverified("a", slot)
        pool._teardown_confirmed(slot, None)
        pool._teardown_confirmed(slot, None)
    assert len(pool._unverified["a"]) == 1 and not pool._unverified_reserved


def test_close_overlapping_a_one_off_run_counts_its_worker_once(make_pool):
    import threading

    gate = threading.Event()

    class Runtime(BlockingFallback):
        release = gate

    pool = make_pool(fast_pool_limits(unverified_limit=20), use_clock=False)
    runner = threading.Thread(
        target=lambda: _quiet(pool.run_once, WorkerDefinition("a", kind="daemon", runtime=lambda *a: Runtime()), "echo"),
        daemon=True)
    runner.start()
    assert wait_until(lambda: bool(pool._ephemeral))
    time.sleep(0.2)
    closer = threading.Thread(target=pool.close, daemon=True)
    closer.start()
    closer.join(15)
    gate.set()
    runner.join(15)
    assert not closer.is_alive() and not runner.is_alive()
    assert len(pool._unverified.get("a", ())) == 1 and not pool._unverified_reserved


@pytest.mark.skipif(not hasattr(os, "waitid") or os.name != "posix", reason="needs POSIX process groups")
def test_a_worker_handle_that_could_not_end_its_group_is_not_confirmed_gone():
    from provider_hub.sandbox import WorkerProcess

    process = WorkerProcess([sys.executable, "-c", "pass"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process.wait(timeout=10)  # reaped before anyone ended its group
    process.stop(0.5)
    assert process.group_cleaned is False
    assert WorkerPool._cleanup_confirmed(process) is False


# ---------------------------------------- the last teardown of a key settles its entry


def _resident_and_one_off_stuck(make_pool, order):
    """A resident and a one-off run of one key, both torn down, finishing in ``order`` (last one is named)."""
    import threading

    gate = threading.Event()
    running = threading.Event()
    runtimes = {}

    class Resident(StubProcessRuntime):
        pass

    class OneOff(StubProcessRuntime):
        def submit(self, op, params=None, **kwargs):
            from provider_hub.runtime.session import RequestResult

            running.set()

            class Handle:
                id = 1

                def done(self):
                    return gate.is_set()

                def wait(self, timeout=None):
                    if not gate.wait(timeout):
                        raise TimeoutError("still running")
                    return RequestResult({"op": op})

            return Handle()

    def resident_factory(*args):
        runtimes["resident"] = Resident()
        return runtimes["resident"]

    def one_off_factory(*args):
        runtimes["one-off"] = OneOff()
        return runtimes["one-off"]

    pool = make_pool(fast_pool_limits(max_oneshot=2, acquire_timeout=2.0), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=resident_factory))
    pool.warm("a")
    runner = threading.Thread(
        target=lambda: _quiet(pool.run_once, WorkerDefinition("a", kind="daemon", runtime=one_off_factory), "echo"),
        daemon=True)
    runner.start()
    assert running.wait(5)
    # The resident is stopped (its process stays: stuck) while the one-off still runs;
    # then the one-off ends (its process stays too: stuck).
    pool.stop("a")
    gate.set()
    runner.join(10)
    assert pool.status()["stuck"] == 2 and pool.state("a") is WorkerState.STOPPING
    return pool, runtimes


@pytest.mark.parametrize("last", ["one-off", "resident"])
def test_the_entry_is_stopped_when_the_last_teardown_of_its_key_is_confirmed(make_pool, last):
    pool, runtimes = _resident_and_one_off_stuck(make_pool, last)
    first = "resident" if last == "one-off" else "one-off"
    runtimes[first].process.returncode = 0
    pool.sweep()
    # One survivor is left: the plugin is still stopping.
    assert pool.status()["stuck"] == 1 and pool.state("a") is WorkerState.STOPPING
    runtimes[last].process.returncode = 0
    pool.sweep()
    assert wait_until(lambda: pool.status()["stuck"] == 0)
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)
    pool.warm("a")
    assert pool.state("a") is WorkerState.READY


# ------------------------------------- memory held in open memfds and /dev/shm is counted


@needs_smaps
def test_a_worker_that_fills_unmapped_memfds_is_killed_for_memory(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition(memory_limit_bytes=100 * MIB))
    written = []
    handle = pool.submit("tr", "memfds", {"count": 4, "mb": 40}, on_progress=lambda message: written.append(message))
    assert wait_until(lambda: bool(written), timeout=15)
    pid = pid_of(pool, "tr")
    # Nothing is mapped: the process's own Pss and Rss show almost none of the 160 MiB.
    plain = 0
    with open("/proc/%d/smaps_rollup" % pid) as handle_file:
        for line in handle_file:
            if line.startswith("Pss:"):
                plain = int(line.split()[1]) * 1024
    assert plain < 100 * MIB
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"


@needs_smaps
def test_memfd_usage_counts_each_memfd_once_across_descriptors_and_ignores_closed_ones():
    fd = os.memfd_create("one")
    other = os.memfd_create("two")
    try:
        os.write(fd, b"z" * (3 * MIB))
        os.write(other, b"z" * (1 * MIB))
        duplicate = os.dup(fd)
        try:
            held, incomplete = pool_module.memfd_usage([os.getpid(), os.getpid()])
            assert incomplete is False
            assert 4 * MIB <= held < 4 * MIB + 512 * 1024
            # The trusted launcher's own descriptors are not read.
            assert pool_module.memfd_usage([os.getpid()], skip={os.getpid()}) == (0, False)
        finally:
            os.close(duplicate)
    finally:
        os.close(fd)
        os.close(other)
    assert pool_module.memfd_usage([os.getpid()])[0] < 512 * 1024


@pytest.mark.skipif(not os.path.isdir("/dev/shm") or not os.access("/dev/shm", os.W_OK), reason="needs /dev/shm")
@needs_smaps
def test_a_shm_open_file_held_open_counts_like_a_memfd():
    path = "/dev/shm/bazarr-test-%d" % os.getpid()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.write(fd, b"s" * (2 * MIB))
        held, _ = pool_module.memfd_usage([os.getpid()])
        assert held >= 2 * MIB
        os.unlink(path)  # still held, now unlinked
        assert pool_module.memfd_usage([os.getpid()])[0] >= 2 * MIB
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


@needs_smaps
def test_an_unreadable_descriptor_directory_makes_the_memory_sample_incomplete(monkeypatch):
    real = os.listdir

    def denying(path):
        if str(path) == "/proc/%d/fd" % os.getpid():
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(os, "listdir", denying)
    assert read_process_tree_memory(os.getpid()).truncated is True
    # The trusted launcher's directory is the one exception, by design.
    assert read_process_tree_memory(os.getpid(), skip_fds={os.getpid()}).truncated is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="launcher is Linux only")
@needs_smaps
def test_a_launcher_worker_is_not_made_incomplete_by_its_hidden_descriptors(make_pool, tmp_path):
    build, declared = launcher_definition(tmp_path)
    pool = make_pool(use_clock=False)
    pool.register(WorkerDefinition("tr", kind="translator", spawn=launcher_spawn(build, probe=declared),
                                   session_limits=fast_session(), memory_limit_bytes=512 * MIB))
    slow = pool.submit("tr", "sleep", {"seconds": 1.0})
    for _ in range(3):
        pool.sweep()
        time.sleep(0.1)
    assert slow.wait(10).result == {"slept": 1.0}
    assert pool.state("tr") is WorkerState.READY
    slot = pool._entry("tr").slot
    assert slot.memory_truncated is False and slot.memory_bytes > 0


# ----------------------------------------------- allocated size: blocks, even when zero


def test_allocated_size_is_the_blocks_when_there_are_any_even_none():
    class Info:
        st_size = 100 * MIB
        st_blocks = 0

    assert pool_module._allocated(Info()) == 0
    Info.st_blocks = 8
    assert pool_module._allocated(Info()) == 4096

    class NoBlocks:
        st_size = 123

    assert pool_module._allocated(NoBlocks()) == 123


def test_a_sparse_file_counts_as_what_it_allocates_in_the_folder_scan(tmp_path):
    path = tmp_path / "sparse"
    with open(path, "wb") as handle:
        handle.truncate(200 * MIB)
    if os.stat(path).st_blocks * 512 > 1 * MIB:
        pytest.skip("this filesystem allocates truncated files")
    used, truncated = folder_usage(str(tmp_path))
    assert truncated is False and used < 1 * MIB


@needs_smaps
def test_a_sparse_deleted_file_counts_as_what_it_allocates_in_the_open_file_scan(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    path = folder / "sparse"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.ftruncate(fd, 200 * MIB)
        os.unlink(path)
        if os.fstat(fd).st_blocks * 512 > 1 * MIB:
            pytest.skip("this filesystem allocates truncated files")
        usage, incomplete = pool_module.deleted_open_usage([os.getpid()], [str(folder)])
        assert incomplete is False and usage[str(folder)] < 1 * MIB
    finally:
        os.close(fd)


def test_a_drained_entry_reads_stopping_until_its_teardown_is_confirmed(make_pool):
    runtimes = []

    class Survivor(StubProcessRuntime):
        pass

    def factory(*args):
        runtimes.append(Survivor())
        return runtimes[-1]

    pool = make_pool(fast_pool_limits(max_resident=8, acquire_timeout=0.4), use_clock=False)
    pool.register(WorkerDefinition("a", kind="daemon", runtime=factory))
    pool.warm("a")
    pool.restart_after_drain("a")
    assert pool.state("a") is WorkerState.DRAINING
    pool.sweep()  # the idle retiring worker is queued for teardown; its process stays
    assert wait_until(lambda: pool.status()["stuck"] == 1)
    pool.sweep()
    assert pool.state("a") is WorkerState.STOPPING
    with pytest.raises(PoolUnavailable):
        pool.warm("a")
    assert len(runtimes) == 1
    runtimes[0].process.returncode = 0
    pool.sweep()
    assert wait_until(lambda: pool.state("a") is WorkerState.STOPPED)
    pool.warm("a")
    assert len(runtimes) == 2 and pool.state("a") is WorkerState.READY


# --------------------------- shared memory kept alive only by a mapping cannot be measured


@needs_smaps
def test_a_worker_that_keeps_a_memfd_alive_by_one_mapped_page_is_killed_for_memory(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition(memory_limit_bytes=100 * MIB))
    written = []
    handle = pool.submit("tr", "memfd_mapped", {"mb": 200}, on_progress=lambda message: written.append(message))
    assert wait_until(lambda: bool(written), timeout=20)
    pid = pid_of(pool, "tr")
    plain = 0
    with open("/proc/%d/smaps_rollup" % pid) as handle_file:
        for line in handle_file:
            if line.startswith("Pss:"):
                plain = int(line.split()[1]) * 1024
    # 200 MiB of shared memory, and the process's own Pss shows nowhere near it.
    assert plain < 100 * MIB
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"


@needs_smaps
def test_a_memfd_both_open_and_mapped_is_counted_not_refused(make_pool):
    pool = make_pool(use_clock=False)
    pool.register(definition(memory_limit_bytes=1024 * MIB))
    written = []
    handle = pool.submit("tr", "memfd_mapped", {"mb": 40, "keep_fd": True, "seconds": 2},
                         on_progress=lambda message: written.append(message))
    assert wait_until(lambda: bool(written), timeout=20)
    pool.sweep()
    slot = pool._entry("tr").slot
    assert slot.memory_truncated is False and slot.memory_bytes >= 40 * MIB
    assert handle.wait(10).result["kept"] is True


@needs_smaps
def test_memfd_usage_refuses_a_mapped_only_memfd_and_counts_an_open_one(tmp_path):
    fd = os.memfd_create("mapped-only")
    os.write(fd, b"q" * (3 * MIB))
    unmap = _map_with_c_mmap(fd, 4096)
    try:
        held, incomplete = pool_module.memfd_usage([os.getpid()])
        assert incomplete is False and held >= 3 * MIB  # open and mapped: counted
        os.close(fd)
        held, incomplete = pool_module.memfd_usage([os.getpid()])
        assert incomplete is True and held < 512 * 1024  # mapped only: cannot be measured
        # The trusted launcher's own mappings are not read.
        assert pool_module.memfd_usage([os.getpid()], skip={os.getpid()}) == (0, False)
    finally:
        unmap()
    assert pool_module.memfd_usage([os.getpid()]) == (0, False)


# ------------------------------------ the folder scan holds directory descriptors and a time budget


def test_a_directory_swapped_for_a_link_during_the_scan_is_not_followed(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    outside = tmp_path / "outside"
    (folder / "sub").mkdir(parents=True)
    outside.mkdir()
    (outside / "big").write_bytes(b"x" * (5 * MIB))
    (folder / "sub" / "small").write_bytes(b"y" * 10)
    real_open = os.open
    swapped = []

    def swapping(path, flags, *args, **kwargs):
        # Between the listing that named ``sub`` and the visit to it, an attacker swaps it for a link.
        if os.fspath(path) == "sub" and not swapped:
            swapped.append(True)
            (folder / "sub" / "small").unlink()
            (folder / "sub").rmdir()
            os.symlink(outside, folder / "sub")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping)
    used, truncated = folder_usage(str(folder))
    assert swapped
    # Not followed: nothing of the outside tree is counted, and the swap fails the scan closed.
    assert used < 1 * MIB
    assert truncated is True


def test_a_folder_scan_over_its_time_budget_is_incomplete(tmp_path):
    for index in range(50):
        (tmp_path / ("f%d" % index)).write_bytes(b"x")
    assert folder_usage(str(tmp_path), max_seconds=30) == (folder_usage(str(tmp_path), max_seconds=None))
    used, truncated = folder_usage(str(tmp_path), max_seconds=0.0)
    assert truncated is True


def test_a_slow_tree_cannot_stall_the_scan_of_the_next_budget(make_pool, tmp_path):
    slow = tmp_path / "slow"
    slow.mkdir()
    (slow / "f").write_bytes(b"x")
    pool = make_pool(fast_pool_limits(budget_scan_seconds=0.0), use_clock=False)
    pool.register(definition(budgets=[FolderBudget(str(slow), 100 * MIB, "plugin data")]))
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("tr", "echo")
    assert refused.value.code == "folder_budget"


def test_a_very_deep_tree_is_incomplete_not_a_crash(tmp_path):
    current = tmp_path
    for _ in range(300):
        current = current / "d"
        try:
            current.mkdir()
        except OSError:
            pytest.skip("the filesystem refuses such a deep tree")
    used, truncated = folder_usage(str(tmp_path))
    assert truncated is True

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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


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
    assert pool.state("tr") is WorkerState.STOPPED
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
    assert pool.state("b") is WorkerState.STOPPED
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
    assert pool.state("a") is WorkerState.STOPPED


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
    assert pool.state("tr") is WorkerState.STOPPED
    assert wait_until(lambda: not pid_alive(first))
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
    assert pool.state("a") is WorkerState.STOPPED
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
    with pytest.raises(PoolUnavailable) as full:
        pool.run_once(stuck, "echo")
    assert full.value.code == "pool_full"
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
    monkeypatch.setattr(pool_module, "_children_of", lambda pid: fake if pid == os.getpid() else [])
    sample = read_process_tree_memory(os.getpid(), max_processes=256)
    if sample is not None:
        assert sample.truncated
    members, overflow = pool_module._process_tree(os.getpid(), 256)
    assert overflow and len(members) == 256
    members, overflow = pool_module._process_tree(os.getpid(), 301)
    assert not overflow and len(members) == 301


def test_a_worker_whose_tree_is_too_big_to_measure_is_killed_as_over_its_limit(make_pool):
    pool = make_pool(memory_reader=lambda pid: MemorySample(1 * MIB, 1 * MIB, 256, "test", truncated=True))
    pool.register(definition(memory_limit_bytes=100 * MIB))
    handle = pool.submit("tr", "sleep", {"seconds": 30})
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "memory_limit"
    assert "measured" in str(killed.value)


def test_a_folder_scan_that_cannot_read_a_subtree_is_incomplete(tmp_path, monkeypatch):
    (tmp_path / "sealed").mkdir()
    (tmp_path / "sealed" / "big").write_bytes(b"x" * 100_000)
    real = os.scandir

    def denying(path):
        if os.fspath(path).endswith("sealed"):
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", denying)
    used, truncated = folder_usage(str(tmp_path))
    assert truncated is True
    # A path that is simply not there is empty, not unknown.
    assert folder_usage(str(tmp_path / "missing")) == (0, False)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
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

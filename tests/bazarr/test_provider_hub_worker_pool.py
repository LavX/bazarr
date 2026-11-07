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
    assert wait_until(lambda: (pool.sweep() or pool.state("tr") is WorkerState.BACKOFF))
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
    assert pool.state("tr") is WorkerState.STOPPED
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


def test_the_config_reserve_blocks_starts_and_kills_running_writers(make_pool):
    free = {"bytes": 10 * GIB}
    pool = make_pool(disk_free=lambda: free["bytes"])
    pool.register(definition("writer"))
    pool.register(definition("reader", writes_data=False))
    handle = pool.submit("writer", "sleep", {"seconds": 30})
    pool.run("reader", "echo")
    free["bytes"] = 100 * MIB
    pool.sweep()
    with pytest.raises(WorkerKilled) as killed:
        handle.wait(10)
    assert killed.value.code == "disk_reserve"
    assert pool.state("reader") is WorkerState.READY
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("writer", "echo")
    assert refused.value.code in ("disk_reserve", "backoff")
    pool.reset("writer")
    with pytest.raises(PoolUnavailable) as refused:
        pool.run("writer", "echo")
    assert refused.value.code == "disk_reserve"
    free["bytes"] = 10 * GIB
    pool.run("writer", "echo")


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


def launcher_definition(tmp_path, key="tr"):
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
            env=clean_env(home=data, tmpdir=data, extra=fake_worker_env()),
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

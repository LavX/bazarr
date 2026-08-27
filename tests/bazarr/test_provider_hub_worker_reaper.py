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


def _client(idle_for, alive=True):
    """A client wired to a stand-in process and a real lock, spawning nothing."""
    from provider_hub.worker import ProviderWorkerClient

    client = ProviderWorkerClient.__new__(ProviderWorkerClient)
    client.process = MagicMock()
    client.process.poll.return_value = None if alive else 0
    client._lock = threading.Lock()
    client.last_used = time.monotonic() - idle_for
    return client


def test_an_idle_worker_is_ended():
    from provider_hub import worker as worker_mod

    stale = _client(idle_for=3600)
    worker_mod._live_clients.add(stale)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    stale.process.terminate.assert_called_once()
    assert stale not in worker_mod._live_clients


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


def test_a_worker_that_will_not_terminate_has_its_group_killed(monkeypatch):
    """start_new_session makes the worker a process-group leader, so killing the
    pid alone would leave anything the plugin forked orphaned and resident."""
    from provider_hub import worker as worker_mod

    stubborn = _client(idle_for=3600)
    stubborn.process.pid = 4242
    stubborn.process.wait.side_effect = [Exception('ignored SIGTERM'), 0]
    killed = []
    monkeypatch.setattr(worker_mod.os, 'killpg',
                        lambda pgid, sig: killed.append((pgid, sig)))
    worker_mod._live_clients.add(stubborn)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    assert killed == [(4242, worker_mod.signal.SIGKILL)], (
        f'expected the process group to be killed, got {killed!r}')


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

    client = worker_mod.ProviderWorkerClient.__new__(worker_mod.ProviderWorkerClient)
    client.command = ['true']
    client.cwd = str(tmp_path)
    client.env = None
    client.process = None
    client._lock = threading.Lock()
    client._stdout_queue = None
    client._stdout_thread = None
    client._stderr_thread = None
    client.last_used = 0.0

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

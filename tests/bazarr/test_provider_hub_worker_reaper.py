# coding=utf-8
"""Idle Provider Hub workers have to be reclaimed.

Each Hub provider runs its plugin in a subprocess, spawned lazily the first time
a pool searches with that provider. Nothing stops them afterwards: the pool's own
recycle only fires the next time that pool is searched, so on a quiet install the
fleet sits at full size indefinitely. Measured on a real deployment, a worker
costs 17.6 to 44.6 MiB PSS and a warm install carried roughly 45 of them.

Reaping them is cheap because it is not destructive: `request()` calls `start()`,
and `start()` respawns a process that has exited, so a reaped worker comes back
on the next search for the cost of a cold start, measured at 0.5 to 1.7 s. It is
also safe against a request in flight, because `stop()` goes through `request()`
and therefore waits on the same lock a live request holds.
"""
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
    """A client wired to a stand-in process, without spawning anything."""
    from provider_hub.worker import ProviderWorkerClient

    client = ProviderWorkerClient.__new__(ProviderWorkerClient)
    client.process = MagicMock()
    client.process.poll.return_value = None if alive else 0
    client.last_used = time.monotonic() - idle_for
    client.stop = MagicMock(side_effect=lambda *a, **kw: None)
    return client


def test_a_worker_idle_past_the_ttl_is_stopped():
    from provider_hub import worker as worker_mod

    stale = _client(idle_for=3600)
    worker_mod._live_clients.add(stale)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 1
    stale.stop.assert_called_once()


def test_a_recently_used_worker_is_left_alone():
    from provider_hub import worker as worker_mod

    busy = _client(idle_for=60)
    worker_mod._live_clients.add(busy)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 0
    busy.stop.assert_not_called()


def test_a_worker_that_already_exited_is_not_stopped_again():
    from provider_hub import worker as worker_mod

    dead = _client(idle_for=3600, alive=False)
    worker_mod._live_clients.add(dead)

    assert worker_mod.reap_idle_workers(idle_seconds=1800) == 0
    dead.stop.assert_not_called()


def test_one_failing_worker_does_not_strand_the_rest():
    """A plugin that will not shut down cleanly must not keep the fleet alive."""
    from provider_hub import worker as worker_mod

    angry = _client(idle_for=3600)
    angry.stop = MagicMock(side_effect=OSError('will not die'))
    calm = _client(idle_for=3600)
    worker_mod._live_clients.add(angry)
    worker_mod._live_clients.add(calm)

    reaped = worker_mod.reap_idle_workers(idle_seconds=1800)

    calm.stop.assert_called_once()
    assert reaped == 1, 'the survivor should still be counted'


def test_a_started_worker_registers_and_a_stopped_one_deregisters(monkeypatch, tmp_path):
    """The registry is what the reaper walks, so membership is the contract."""
    from provider_hub import worker as worker_mod

    client = worker_mod.ProviderWorkerClient.__new__(worker_mod.ProviderWorkerClient)
    client.command = ['true']
    client.cwd = str(tmp_path)
    client.env = None
    client.process = None
    client._lock = __import__('threading').Lock()
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

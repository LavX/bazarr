"""Detaching a translator plugin while Bazarr+ runs: a removed or revoked plugin takes no new job, the job its worker holds
finishes (or is cut after the grace), and the worker goes."""
import time

import pytest

from provider_hub.runtime import errors
from provider_hub.runtime.pool import PoolUnavailable, WorkerDefinition
from provider_hub.runtime.testing.fake_translator import fake_translator_command, fake_translator_env
from subtitles.tools.translate.runtime import worker_binding, workers

from test_translator_worker_binding import PLUGIN, _fast_session, _pool, _translate


def _wait(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def port():
    """A port on a real worker pool whose worker is a fake translator that holds its job until it is cancelled."""
    pool = _pool()
    definition = WorkerDefinition(PLUGIN, kind="translator", command=fake_translator_command(),
                                  env=fake_translator_env({"*": [{"wait_cancel": True}]}),
                                  session_limits=_fast_session())
    yield workers.PoolWorkers(pool, lambda plugin_id: definition)
    pool.close()


def _ended(handle):
    try:
        handle.wait(10)
    except Exception as error:  # the way the job ended is what the test reads
        return error
    return None


def test_a_detached_plugin_takes_no_new_job_and_the_job_it_holds_finishes(port):
    held = _translate(port, job="job-1")
    time.sleep(0.3)
    assert not held.done()

    threads = port.detach(PLUGIN)

    # A job that arrives after the detach is refused, and no worker of the release that left is started for it.
    with pytest.raises(PoolUnavailable):
        _translate(port, job="job-2")
    # The job the worker holds is not interrupted, and the detach is not finished while it runs.
    time.sleep(0.4)
    assert not held.done() and all(thread.is_alive() for thread in threads)
    held.cancel("done")
    assert isinstance(_ended(held), (errors.RequestCancelled, errors.RequestFailed))
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads)
    assert port.idle(PLUGIN)


def test_a_detach_with_a_grace_cuts_the_job_that_outlives_it(port):
    held = _translate(port, job="job-1")
    time.sleep(0.3)
    began = time.monotonic()

    threads = port.detach(PLUGIN, grace=0.5)

    # The job is not cut before the grace is over.
    time.sleep(0.2)
    assert not held.done()
    assert _ended(held) is not None
    assert time.monotonic() - began >= 0.5
    for thread in threads:
        thread.join(20)
    assert port.idle(PLUGIN)
    with pytest.raises(PoolUnavailable):
        _translate(port, job="job-2")


def test_a_job_the_pool_took_as_the_plugin_was_detached_is_not_left_running_on_the_release_that_left():
    """The detach lands between the check and the pool's submit: the pool starts a worker for the job, and the port
    cancels that job when it finds the plugin withdrawn."""
    pool = _pool()
    try:
        definition = WorkerDefinition(PLUGIN, kind="translator", command=fake_translator_command(),
                                      env=fake_translator_env({"*": [{"wait_cancel": True}]}),
                                      session_limits=_fast_session())
        ports = []

        class DetachesWhileSubmitting:
            """The real pool, except that the plugin is detached just before the pool takes the job."""

            def __getattr__(self, name):
                return getattr(pool, name)

            def submit(self, *args, **kwargs):
                ports[0].detach(PLUGIN)
                return pool.submit(*args, **kwargs)

        port = workers.PoolWorkers(DetachesWhileSubmitting(), lambda plugin_id: definition)
        ports.append(port)

        with pytest.raises(PoolUnavailable):
            _translate(port, job="job-1")

        assert _wait(lambda: port.idle(PLUGIN))
    finally:
        pool.close()


def test_a_short_call_to_a_detached_plugin_is_refused_before_anything_starts(port):
    port.detach(PLUGIN)

    with pytest.raises(workers.NotSent):
        port.call(PLUGIN, "test", {}, {}, timeout=1.0)


def test_detaching_a_plugin_that_never_had_a_worker_ends_at_once(port):
    threads = port.detach("never_started")

    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)


def test_the_unbound_port_has_nothing_to_detach():
    assert workers.UnboundWorkers().detach(PLUGIN) == []


def test_the_binding_detaches_through_the_port_and_does_nothing_unbound(monkeypatch):
    detached = []

    class Port:
        def detach(self, plugin_id, *, grace=None):
            detached.append((plugin_id, grace))
            return ["thread"]

    monkeypatch.setattr(worker_binding, "_port", None)
    assert worker_binding.plugin_detached(PLUGIN, 3.0) == []
    monkeypatch.setattr(worker_binding, "_port", Port())
    assert worker_binding.plugin_detached(PLUGIN, 3.0) == ["thread"] and detached == [(PLUGIN, 3.0)]


def test_the_translator_kind_detaches_a_removal_but_only_retires_a_switch_off(monkeypatch):
    from provider_hub.kinds import DETACH_LIVE
    from provider_hub.kinds.translator import TranslatorKind

    calls = []
    monkeypatch.setattr(worker_binding, "plugin_detached", lambda plugin_id, grace=None: calls.append(
        ("detached", plugin_id, grace)) or ["thread"])
    monkeypatch.setattr(worker_binding, "plugin_changed", lambda plugin_id: calls.append(("changed", plugin_id)))
    kind = TranslatorKind()

    assert kind.detach_policy == DETACH_LIVE
    assert kind.detach(PLUGIN, withdraw=True, grace=15.0) == ["thread"]
    assert kind.detach(PLUGIN, withdraw=False) == []
    assert calls == [("detached", PLUGIN, 15.0), ("changed", PLUGIN)]


def test_a_finished_removal_clears_the_folders_of_the_plugin_that_is_gone(monkeypatch):
    from provider_hub.kinds.translator import TranslatorKind

    swept = []
    monkeypatch.setattr(worker_binding, "sweep_removed_plugins", lambda: swept.append(True) or [])

    TranslatorKind().removal_finished(PLUGIN)

    assert swept == [True]


@pytest.mark.parametrize("pending_restart, running", [(False, False), (True, True)])
def test_a_removal_detached_live_is_not_a_running_installation_and_one_waiting_for_a_restart_still_is(
        monkeypatch, pending_restart, running):
    """The binding reads the running installations: a removed row that waits for no restart was detached, so a start
    that arrives after the detach has no release to run. A removal that waits for the restart is still running."""
    from provider_hub.state import running_installations

    monkeypatch.setattr("provider_hub.state.load_state", lambda path=None: {"installations": {
        PLUGIN: {"provider_id": PLUGIN, "kind": "translator", "state": "removed", "active_version": "1.0.0",
                 "pending_restart": pending_restart, "manifest": {"provider_id": PLUGIN}}}})

    assert [item.provider_id for item in running_installations(kind="translator")] == ([PLUGIN] if running else [])


def test_a_detach_with_no_grace_still_ends_at_the_ceiling_and_cuts_the_job(port):
    held = _translate(port, job="job-1")
    time.sleep(0.3)
    port.DETACH_CEILING_SECONDS = 0.5

    threads = port.detach(PLUGIN)

    assert _ended(held) is not None
    for thread in threads:
        thread.join(20)
    assert port.idle(PLUGIN)


def test_a_worker_that_cannot_be_stopped_is_reported_once_and_looked_at_less_often_as_it_waits(caplog, monkeypatch):
    import logging

    waits = []

    class Pool:
        def stop(self, key):
            raise RuntimeError("would not stop")

    port = workers.PoolWorkers(Pool(), lambda plugin_id: None)
    polls = iter([False] * 12 + [True])
    monkeypatch.setattr(port, "idle", lambda plugin_id: next(polls))
    monkeypatch.setattr(port, "_worker_status", lambda plugin_id: {"state": "ready", "in_flight": 0})
    monkeypatch.setattr(time, "sleep", waits.append)

    with caplog.at_level(logging.WARNING):
        port._finish_detach(PLUGIN, PLUGIN, None)

    assert len([record for record in caplog.records if "Could not stop the worker" in record.getMessage()]) == 1
    assert waits[0] == 0.1 and waits[-1] == 2.0 and waits == sorted(waits)

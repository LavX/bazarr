# -*- coding: utf-8 -*-
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

import provider_hub.registry as registry
from provider_hub import WORKER_ABI_VERSION
from provider_hub import worker as worker_mod
from provider_hub.registry import HubProxyProvider
from provider_hub.worker import ProviderWorkerClient, RequestNotAdmitted, WorkerBusy


WHISPER_CONFIG = {
    "endpoint": "http://127.0.0.1:9000",
    "ffmpeg_path": "ffmpeg",
    "pass_video_name": False,
    "response_timeout_seconds": 600,
    "transcription_timeout_seconds": 3600,
}


def _pin_global(monkeypatch, value):
    monkeypatch.setattr(registry, "_global_worker_timeout", lambda: float(value))


def test_worker_timeout_validator_default():
    from app.config import settings

    assert int(settings.general.provider_hub_worker_timeout) == 120


def test_declared_transcription_timeout_drives_deadline(monkeypatch):
    _pin_global(monkeypatch, 120)
    provider = HubProxyProvider(**WHISPER_CONFIG)
    # 3600 declared + 30 margin, floored by 120, under the 86400 cap.
    assert provider._request_timeout() == 3630.0


def test_no_declared_timeout_uses_global_default(monkeypatch):
    _pin_global(monkeypatch, 120)
    provider = HubProxyProvider(endpoint="http://x", api_key="secret")
    assert provider._request_timeout() == 120.0


def test_global_default_is_a_floor(monkeypatch):
    _pin_global(monkeypatch, 5000)
    provider = HubProxyProvider(**WHISPER_CONFIG)
    # derived 3630 < global default 5000 -> floor wins.
    assert provider._request_timeout() == 5000.0


def test_declared_timeout_clamped_to_cap(monkeypatch):
    _pin_global(monkeypatch, 120)
    # A plugin timeout above the cap is clamped to _MAX_WORKER_REQUEST_TIMEOUT
    # before the margin is added, so the host wall is cap + margin. Defensive
    # only: the setting validator and plugin manifests already bound configured
    # values to the cap, so a value this high should not reach here in practice.
    provider = HubProxyProvider(endpoint="http://x", transcription_timeout_seconds=90000)
    assert provider._request_timeout() == registry._MAX_WORKER_REQUEST_TIMEOUT + 30.0


def test_margin_preserved_at_the_cap(monkeypatch):
    _pin_global(monkeypatch, 120)
    # Exactly at the manifest maximum, the +30 host margin must not be swallowed
    # by the cap (regression guard: host deadline > worker's own timeout).
    provider = HubProxyProvider(endpoint="http://x", transcription_timeout_seconds=86400)
    assert provider._request_timeout() == 86430.0


def test_explicit_constructor_timeout_override(monkeypatch):
    _pin_global(monkeypatch, 120)
    # An explicit timeout= above the floor drives the deadline (this is the only
    # path exercising the self.timeout contribution to `declared`).
    provider = HubProxyProvider(endpoint="http://x", timeout=5000)
    assert provider._request_timeout() == 5030.0


def test_legacy_worker_timeout_key_still_honored(monkeypatch):
    _pin_global(monkeypatch, 120)
    provider = HubProxyProvider(endpoint="http://x", worker_timeout=900)
    assert provider._request_timeout() == 930.0


def test_legacy_and_suffix_keys_take_the_larger(monkeypatch):
    _pin_global(monkeypatch, 120)
    # Both timeout categories present with the legacy key larger, so a refactor
    # that let the *_timeout_seconds loop overwrite (rather than extend) the
    # declared list would drop to 630 and fail here.
    provider = HubProxyProvider(
        endpoint="http://x", worker_timeout=3600, transcription_timeout_seconds=600
    )
    assert provider._request_timeout() == 3630.0


def test_registry_cap_covers_the_validator_maximum():
    # The host cap must accommodate the largest value the setting/manifest allow
    # (validator lte and whisper manifest maximum are both 86400); otherwise a
    # user's configured timeout would be silently clipped below what they set.
    assert registry._MAX_WORKER_REQUEST_TIMEOUT >= 86400


def test_global_worker_timeout_reads_setting(monkeypatch):
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(general=SimpleNamespace(provider_hub_worker_timeout=500)),
        raising=False,
    )
    assert registry._global_worker_timeout() == 500.0


@pytest.mark.parametrize("bad_value", [None, 0, -5, "oops"])
def test_global_worker_timeout_falls_back(monkeypatch, bad_value):
    # Missing, zero, negative, and non-numeric all fall back to the default via
    # _coerce_timeout; the docstring promises this for "missing/invalid".
    general = SimpleNamespace() if bad_value is None else SimpleNamespace(
        provider_hub_worker_timeout=bad_value
    )
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(general=general),
        raising=False,
    )
    assert registry._global_worker_timeout() == 120.0


# A request that waited for its turn on a worker rechecks whether the provider
# may still be called, and never waits longer than its own timeout.


class _FakeProcess:
    """Stands in for a worker process. Records every request written to it and
    answers at once, or only when answer() is called while held."""

    pid = 4242

    def __init__(self, client):
        self.client = client
        self.written = []
        self.pending = []
        self.held = set()
        self.exited = False
        self.stdin = self
        self.stdout = object()
        client.process = self
        client._stdout_queue = queue.Queue()

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=None):
        return 0

    def write(self, text):
        message = json.loads(text)
        self.written.append(message["op"])
        if message["op"] == "shutdown":
            self.exited = True
        self.pending.append(message)

    def flush(self):
        message = self.pending[-1]
        if message["op"] not in self.held:
            self.answer()

    def answer(self, ok=True):
        message = self.pending.pop(0)
        reply = {"abi": WORKER_ABI_VERSION, "id": message["id"], "ok": ok, "payload": {}, "events": []}
        if not ok:
            reply["error"] = {"code": "timeout", "message": "fixture failure"}
        self.client._stdout_queue.put(json.dumps(reply) + "\n")


class _Call(threading.Thread):
    """A request on its own thread, with the pool's outcome report after it."""

    def __init__(self, client, admit, op="search", timeout=5.0, record=None):
        super().__init__(daemon=True)
        self.client, self.admit, self.op, self.timeout = client, admit, op, timeout
        self.record = record
        self.error = None
        self.result = None
        self.start()

    def run(self):
        try:
            self.result = self.client.request(self.op, {}, timeout=self.timeout, admit=self.admit)
        except Exception as error:
            self.error = error
        finally:
            if self.record is not None:
                self.record.wait(5)
            self.client.outcome_recorded()


@pytest.fixture
def gated(monkeypatch):
    spawned = []
    monkeypatch.setattr(worker_mod.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pgid, sig: None)
    client = ProviderWorkerClient(["unused"])
    process = _FakeProcess(client)
    admitted = []
    allowed = [True]

    def admit():
        admitted.append(threading.current_thread().name)
        return allowed[0]

    return SimpleNamespace(client=client, process=process, admit=admit, admitted=admitted,
                           allowed=allowed, spawned=spawned)


def _until(condition):
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_a_queued_request_is_dropped_once_the_failure_before_it_is_recorded(gated):
    gated.process.held.add("search")
    record = threading.Event()
    first = _Call(gated.client, gated.admit, record=record)
    _until(lambda: gated.process.written == ["search"])
    second = _Call(gated.client, gated.admit)

    gated.allowed[0] = False
    gated.process.answer(ok=False)
    _until(lambda: first.error is not None)
    time.sleep(0.1)
    # The first call's outcome is not recorded yet, so the second one has not
    # run its check; it waits without holding the lock.
    assert second.is_alive()
    assert len(gated.admitted) == 1
    assert not gated.client._lock.locked()

    record.set()
    second.join(5)
    assert isinstance(second.error, RequestNotAdmitted)
    assert gated.process.written == ["search"]


def test_a_request_waiting_its_turn_blocks_neither_archive_selection_nor_stop(gated):
    gated.client.request("download", {}, timeout=5, admit=gated.admit)
    waiting = _Call(gated.client, gated.admit)
    time.sleep(0.1)

    # The download's own member selection is ungated and runs at once.
    gated.client.select_archive_member({}, timeout=5)
    assert waiting.is_alive()
    gated.client.outcome_recorded()
    waiting.join(5)
    assert waiting.error is None
    assert gated.process.written == ["download", "select_archive_member", "search"]

    gated.client.request("download", {}, timeout=5, admit=gated.admit)
    retired = _Call(gated.client, gated.admit)
    time.sleep(0.1)
    gated.client.stop(grace_seconds=0.1)
    retired.join(5)
    gated.client.outcome_recorded()

    # A stopped client admits nothing and respawns nothing.
    assert isinstance(retired.error, WorkerBusy)
    assert len(gated.admitted) == 3
    assert gated.process.written[-2:] == ["download", "shutdown"]
    assert gated.spawned == []


def test_only_the_request_that_cleared_the_latch_sets_it_again(gated):
    gated.client.request("download", {}, timeout=5, admit=gated.admit)

    busy = _Call(gated.client, gated.admit, timeout=0.2)
    busy.join(5)
    assert isinstance(busy.error, WorkerBusy)
    assert not gated.client._outcome_latch.is_set()

    queued = _Call(gated.client, gated.admit)
    time.sleep(0.1)
    assert queued.is_alive()
    gated.allowed[0] = False
    gated.client.outcome_recorded()
    queued.join(5)
    assert isinstance(queued.error, RequestNotAdmitted)
    assert gated.process.written == ["download"]


def test_a_busy_worker_bounds_the_wait_by_the_request_timeout(gated):
    gated.process.held.add("search")
    first = _Call(gated.client, None)
    _until(lambda: gated.process.written == ["search"])

    started = time.monotonic()
    with pytest.raises(WorkerBusy):
        gated.client.request("search", {}, timeout=0.2, admit=gated.admit)
    assert time.monotonic() - started < 2
    assert gated.admitted == []

    gated.process.answer()
    first.join(5)


def test_a_turn_that_comes_at_the_deadline_is_busy_not_an_error(gated, monkeypatch):
    now = [100.0]

    class LatchSetPastTheDeadline:
        def wait(self, timeout):
            now[0] += timeout + 0.5
            return True

    monkeypatch.setattr(worker_mod, "time", SimpleNamespace(monotonic=lambda: now[0]))
    gated.client._outcome_latch = LatchSetPastTheDeadline()

    with pytest.raises(WorkerBusy):
        gated.client.request("search", {}, timeout=5, admit=gated.admit)
    assert gated.admitted == []
    assert gated.process.written == []
    assert not gated.client._lock.locked()


def test_a_client_stopped_after_its_worker_exited_admits_nothing(gated):
    gated.process.exited = True
    gated.client.stop(grace_seconds=0.1)

    with pytest.raises(WorkerBusy):
        gated.client.request("search", {}, timeout=1, admit=gated.admit)
    assert gated.admitted == []
    assert gated.spawned == []
    assert gated.process.written == []


def test_a_request_never_hangs_behind_an_outcome_that_is_never_recorded(gated):
    # The pool-side finally that reports the outcome was stubbed out, so the
    # latch a queued request waits on is never set again. The queued request
    # still gives up within its own timeout instead of waiting forever.
    gated.client.outcome_recorded = lambda: None
    gated.client.request("search", {}, timeout=5, admit=gated.admit)

    started = time.monotonic()
    with pytest.raises(WorkerBusy):
        gated.client.request("search", {}, timeout=0.2, admit=gated.admit)
    assert time.monotonic() - started < 2
    assert not gated.client._outcome_latch.is_set()
    assert gated.process.written == ["search"]


def test_a_request_queued_when_a_rebuild_retires_the_client_is_refused(gated):
    # A gated request queues behind one in flight whose outcome is not
    # recorded yet, and while it waits the pool rebuilds the provider, which
    # retires this client. The pool has nothing against the provider (the
    # admission check would pass), but the retired client refuses the request
    # and never respawns its worker with the configuration that was replaced.
    from subliminal_patch import core

    gated.process.held.add("search")
    record = threading.Event()
    first = _Call(gated.client, gated.admit, record=record)
    _until(lambda: gated.process.written == ["search"])
    gated.process.answer(ok=False)
    _until(lambda: first.error is not None)
    provider = HubProxyProvider(worker_client=gated.client)
    provider.admit = lambda: True
    errors = []

    def queued():
        try:
            provider.download_subtitle(SimpleNamespace(provider_payload={}, language=core.Language("eng")))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=queued, daemon=True)
    thread.start()
    time.sleep(0.1)
    assert thread.is_alive()

    gated.client.stop(grace_seconds=0.1)
    thread.join(5)
    record.set()
    first.join(5)

    assert len(errors) == 1
    assert isinstance(errors[0], core.ProviderBusyError)
    assert gated.spawned == []


def test_stop_if_idle_leaves_a_client_that_was_just_used_open(gated):
    # The idle sweep ends only genuinely idle workers. One that just served a
    # request is left open, stays open for gated requests, and a later
    # request on it still runs instead of respawning or being refused.
    gated.client.request("search", {}, timeout=5, admit=gated.admit)
    gated.client.outcome_recorded()

    assert gated.client.stop_if_idle(3600) is False
    assert gated.client._closed is False

    gated.client.request("search", {}, timeout=5, admit=gated.admit)
    assert gated.process.written == ["search", "search"]
    gated.client.outcome_recorded()


@pytest.mark.parametrize("worker_error, pool_error", [
    (RequestNotAdmitted, "ProviderExcludedWhileQueuedError"),
    (WorkerBusy, "ProviderBusyError"),
])
@pytest.mark.parametrize("operation", ["search", "download"])
def test_the_proxy_turns_a_refused_turn_into_the_pool_errors(worker_error, pool_error, operation):
    from subliminal_patch import core

    def refuse(*args, **kwargs):
        raise worker_error("fixture")

    provider = HubProxyProvider(worker_client=SimpleNamespace(request=refuse))
    provider.admit = lambda: True
    with pytest.raises(getattr(core, pool_error)):
        if operation == "search":
            provider.list_subtitles(SimpleNamespace(), set())
        else:
            provider.download_subtitle(SimpleNamespace(provider_payload={}, language=core.Language("eng")))

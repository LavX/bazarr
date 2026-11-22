# coding=utf-8
"""Detaching a running provider release while Bazarr+ runs: disable, remove, revoke and the replaced release.

The pools, the registry and the state are real. The workers are fakes that record how they were told to stop, and the
calls that are "in flight" are provider calls that wait on an event, so each race below is a real overlap of threads and
not a sequence of mocks.
"""
import threading
import time

import pytest

from test_provider_hub import _manifest

PLUGIN = "alphahub"
# A migrated built-in id: the one a trusted plugin may take the place of.
BUILT_IN = "addic7ed"
WAIT = 15


class FakeWorker:
    """A worker client that records how it was ended. ``killed`` is set when it is cut, which fails the call that waits
    on it the way a worker that went away fails one."""

    def __init__(self):
        self.command = None
        self.process = None
        self.stops = 0
        self.cancels = 0
        self.host_calls_revoked = 0
        self.killed = threading.Event()

    def stop(self):
        self.stops += 1

    def cancel(self):
        self.cancels += 1
        self.killed.set()

    def revoke_host_calls(self):
        self.host_calls_revoked += 1

    def outcome_recorded(self):
        pass


def _provider_class(name, worker, *, gate=None, started=None, record=None):
    """A plugin class whose calls wait on ``gate`` (or are cut), recording each one that ran to its end."""
    from subzero.language import Language
    from provider_hub.registry import HubProxyProvider
    from provider_hub.worker import WorkerCancelled

    def wait_for_gate():
        if started is not None:
            started.set()
        if gate is None:
            return
        while not gate.is_set():
            if worker.killed.is_set():
                raise WorkerCancelled("worker closed stdout")
            time.sleep(0.01)

    class Plugin(HubProxyProvider):
        provider_name = name
        languages = {Language("eng")}
        release_key = "a" * 64

        def __init__(self, **config):
            super().__init__(worker_client=worker, **config)

        def list_subtitles(self, video, languages):
            with self._operation():
                wait_for_gate()
                if record is not None:
                    record.append(("search", self))
                return []

        def download_subtitle(self, subtitle):
            with self._operation():
                wait_for_gate()
                subtitle.content = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
                if record is not None:
                    record.append(("download", self))
                return True

    return Plugin


def _row(plugin_id=PLUGIN, **extra):
    row = {"provider_id": plugin_id, "name": plugin_id.title(), "active_version": "1.0.0", "state": "active",
           "pending_restart": False, "enabled": True, "trusted": True, "source_id": "official", "config": {},
           "manifest": _manifest(provider_id=plugin_id)}
    row.update(extra)
    return row


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A state file with no installations, a restored provider registry, and the settings write recorded."""
    from provider_hub import registry
    from provider_hub.state import load_state, save_state
    from subliminal_patch.extensions import provider_registry

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    before = {name: provider_registry[name] for name in provider_registry}
    hub_ids = set(registry._REGISTERED_PROVIDER_HUB_IDS)
    shadowed = dict(registry._SHADOWED_BUILT_INS)
    withdrawn = set(registry._WITHDRAWN_PROVIDER_IDS)
    enabled = []
    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
                        lambda provider_id, on: enabled.append((provider_id, on)) or True)

    class World:
        settings_writes = enabled
        registry = provider_registry

        def install(self, plugin_id=PLUGIN, **extra):
            state = load_state()
            state["installations"][plugin_id] = _row(plugin_id, **extra)
            save_state(state)

        def row(self, plugin_id=PLUGIN):
            return load_state()["installations"].get(plugin_id)

        def run(self, plugin_id=PLUGIN, *, gate=None, started=None, record=None, worker=None):
            """Register a running plugin and return its class and worker."""
            worker = worker or FakeWorker()
            cls = _provider_class(plugin_id, worker, gate=gate, started=started, record=record)
            provider_registry.register(plugin_id, cls)
            registry._REGISTERED_PROVIDER_HUB_IDS.add(plugin_id)
            return cls, worker

        def pool(self, *names):
            from subliminal_patch.core import SZProviderPool

            return SZProviderPool(providers=list(names or [PLUGIN]), provider_configs={})

    yield World()
    # Before the registry and the settings are put back: a detach that ends late finishes against what the test set up.
    # The registry is put back whatever the wait finds, so one leaked thread does not pollute every test after it.
    from provider_hub import detach

    try:
        assert detach.wait_idle(WAIT)
    finally:
        for name in provider_registry:
            provider_registry.unregister(name)
        for name, provider_class in before.items():
            provider_registry.register(name, provider_class)
        registry._REGISTERED_PROVIDER_HUB_IDS.clear()
        registry._REGISTERED_PROVIDER_HUB_IDS.update(hub_ids)
        registry._SHADOWED_BUILT_INS.clear()
        registry._SHADOWED_BUILT_INS.update(shadowed)
        registry._WITHDRAWN_PROVIDER_IDS.clear()
        registry._WITHDRAWN_PROVIDER_IDS.update(withdrawn)
        registry._REGISTRATION_INCOMPLETE.clear()


def _movie():
    from subliminal.video import Movie

    return Movie("movie.mkv", "Movie", year=2000)


def _search(pool, name=PLUGIN):
    from subzero.language import Language

    return pool.list_subtitles_provider(name, _movie(), {Language("eng")}, detailed=True)


def _in_thread(function, *args, **kwargs):
    outcome = {}

    def work():
        try:
            outcome["value"] = function(*args, **kwargs)
        except BaseException as error:  # the test reads it
            outcome["error"] = error

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, outcome


def _wait_for(condition, seconds=WAIT):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


# ---- remove -----------------------------------------------------------------------------------------------------


def test_removing_a_running_provider_leaves_the_registry_and_the_pool_and_needs_no_restart(world):
    from provider_hub import detach, service

    world.install()
    _, worker = world.run()
    pool = world.pool()
    assert _search(pool).status == "empty"  # an instance is cached, and its worker is the one that will be stopped
    assert PLUGIN in pool.initialized_providers

    assert service.remove_installation(PLUGIN) is True

    # Out of the registry at once, whatever the pools are still doing.
    assert world.registry[PLUGIN] is None
    assert detach.wait_idle(WAIT)
    assert world.row() is None
    assert worker.stops == 1 and PLUGIN not in pool.initialized_providers
    assert (PLUGIN, False) in world.settings_writes
    # A search that starts afterwards finds nothing to run, and says so quietly.
    assert _search(pool).status == "skipped" and _search(pool).reason == "provider_excluded"


def test_a_removal_is_marked_and_asks_for_no_restart_while_its_calls_finish(world):
    from provider_hub import detach, service

    world.install()
    gate, started = threading.Event(), threading.Event()
    _, worker = world.run(gate=gate, started=started)
    pool = world.pool()
    thread, outcome = _in_thread(_search, pool)
    assert started.wait(WAIT)

    service.remove_installation(PLUGIN)

    row = world.row()
    assert row["state"] == "removed" and row["pending_restart"] is False
    assert service.get_provider(PLUGIN)["needs_restart"] is False
    # The call it took is still running: the worker is not stopped under it and the row is not deleted yet.
    time.sleep(0.2)
    assert worker.stops == 0 and world.row() is not None
    gate.set()
    thread.join(WAIT)
    assert outcome["value"].status == "empty"
    assert detach.wait_idle(WAIT)
    assert world.row() is None and worker.stops == 1


def test_a_search_in_flight_across_a_removal_finishes_on_the_class_it_started_with_and_never_sees_none(world):
    from provider_hub import service

    world.install()
    record, gate, started = [], threading.Event(), threading.Event()
    world.run(gate=gate, started=started, record=record)
    pool = world.pool()
    thread, outcome = _in_thread(_search, pool)
    assert started.wait(WAIT)

    service.remove_installation(PLUGIN)
    # The registry has no class now. The search holds the one it started with, and the call it was admitted for ends
    # normally.
    assert world.registry[PLUGIN] is None
    gate.set()
    thread.join(WAIT)

    assert "error" not in outcome
    assert outcome["value"].status == "empty" and [kind for kind, _ in record] == ["search"]


def test_a_download_in_flight_across_a_removal_finishes_and_the_next_one_is_refused_quietly(world):
    from provider_hub import service

    world.install()
    record, gate, started = [], threading.Event(), threading.Event()
    world.run(gate=gate, started=started, record=record)
    pool = world.pool()
    subtitle = _subtitle()
    thread, outcome = _in_thread(pool.download_subtitle, subtitle)
    assert started.wait(WAIT)

    service.remove_installation(PLUGIN)
    gate.set()
    thread.join(WAIT)

    assert outcome.get("value") is True and [kind for kind, _ in record] == ["download"]
    # A download that names the provider afterwards is refused: nothing runs it, and nothing is recorded against it.
    again = _subtitle()
    assert pool.download_subtitle(again) is False
    assert again.skip_reason == "provider_excluded"


def _subtitle(name=PLUGIN):
    from subzero.language import Language
    from subliminal_patch.subtitle import Subtitle

    class Downloadable(Subtitle):
        provider_name = name

        def __init__(self):
            super().__init__(Language("eng"))
            self.provider_name = name
            self.page_link = None

        @property
        def id(self):
            return "s1"

        def get_matches(self, video):
            return set()

        def is_valid(self):
            return True

        def normalize(self):
            pass

    return Downloadable()


def test_a_search_whose_instance_is_detached_before_its_call_is_admitted_is_not_run_on_the_removed_release(world):
    from provider_hub import service

    world.install()
    record = []
    world.run(record=record)
    pool = world.pool()
    stale = pool[PLUGIN]

    service.remove_installation(PLUGIN)

    from subliminal_patch.core import ProviderExcludedError, ProviderRetiredError

    with pytest.raises(ProviderRetiredError):
        stale.list_subtitles(_movie(), set())
    # The pool does not build another instance of the release that is gone.
    with pytest.raises(ProviderExcludedError):
        pool[PLUGIN]
    assert record == []


def test_a_removal_that_a_plugin_installed_again_overtook_leaves_the_new_row_alone(world):
    from provider_hub import detach, service
    from provider_hub.state import load_state, save_state

    world.install()
    gate, started = threading.Event(), threading.Event()
    world.run(gate=gate, started=started)
    pool = world.pool()
    thread, _ = _in_thread(_search, pool)
    assert started.wait(WAIT)
    service.remove_installation(PLUGIN)
    # While the call drains, the plugin is installed again: its row is staged, and the removal must not take it.
    state = load_state()
    state["installations"][PLUGIN] = _row(PLUGIN, active_version=None, staged_version="1.1.0", state="staged",
                                          pending_restart=True)
    save_state(state)

    gate.set()
    thread.join(WAIT)
    assert detach.wait_idle(WAIT)

    assert world.row()["state"] == "staged" and world.row()["staged_version"] == "1.1.0"


def test_a_removal_a_stopped_process_left_marked_is_finished_by_the_next_start(world):
    from provider_hub.service import activate_staged_installations

    world.install(state="removed", pending_restart=False)

    activate_staged_installations()

    assert world.row() is None


def test_a_plugin_that_replaced_a_built_in_gives_it_back_when_it_is_removed(world, monkeypatch):
    from provider_hub import registry, service
    from provider_hub.state import active_installations

    class BuiltIn:
        provider_name = BUILT_IN
        languages = set()

    world.registry.register(BUILT_IN, BuiltIn)
    world.install(BUILT_IN)
    from provider_hub.registry import HubProxyProvider

    class Plugin(HubProxyProvider):
        provider_name = BUILT_IN

    monkeypatch.setattr(registry, "_make_provider_class", lambda manifest, installation=None: Plugin)
    from provider_hub.state import load_state, save_state

    state = load_state()
    state["installations"][BUILT_IN]["manifest"] = _manifest(provider_id=BUILT_IN)
    save_state(state)
    assert registry.register_active_provider_classes(active_installations()) == [BUILT_IN]
    assert world.registry[BUILT_IN] is Plugin and BUILT_IN in registry._REGISTERED_PROVIDER_HUB_IDS

    service.remove_installation(BUILT_IN)

    # One step: the id names the built-in again, and counts as a built-in for the shadow gate.
    assert world.registry[BUILT_IN] is BuiltIn
    assert BUILT_IN not in registry._REGISTERED_PROVIDER_HUB_IDS
    assert not registry.is_withdrawn(BUILT_IN)
    assert BUILT_IN not in registry._SHADOWED_BUILT_INS


def test_a_plugin_that_replaced_nothing_leaves_no_class_behind_and_is_withdrawn(world):
    from provider_hub import registry

    world.run()

    result = registry.unregister_provider_class(PLUGIN)

    assert result.removed and not result.restored_built_in
    assert PLUGIN not in world.registry and registry.is_withdrawn(PLUGIN)
    # Taking out what is not there changes nothing.
    assert registry.unregister_provider_class(PLUGIN).removed is False


def test_the_registry_swaps_and_unregisters_under_one_lock_and_a_missing_name_is_none_not_an_error(world):
    class First:
        pass

    class Second:
        pass

    world.registry.register("one", First)
    assert world.registry["gone"] is None and world.registry.unregister("gone") is None
    # Only the class that was named is taken out.
    assert world.registry.unregister("one", expected=Second) is None and world.registry["one"] is First
    assert world.registry.unregister("one", expected=First) is First and "one" not in world.registry
    world.registry.register("two", Second)
    assert "two" in world.registry.names()


def test_removing_replaces_the_compat_pool_and_the_discover_search_that_holds_the_old_one_is_not_disturbed(world):
    from compat import service as compat

    world.install()
    world.run()
    old = object()
    compat._compat_pool = old
    generation = compat._pool_generation

    from provider_hub import service

    service.remove_installation(PLUGIN)

    assert compat._compat_pool is None and compat._pool_generation == generation + 1
    # The search that holds the old pool still holds it: it is replaced, never edited.
    assert old is not None


# ---- disable ----------------------------------------------------------------------------------------------------


def test_switching_a_provider_off_lets_the_pools_go_of_it_and_ends_its_worker_but_keeps_the_class(world):
    from provider_hub import detach, service

    world.install()
    cls, worker = world.run()
    pool = world.pool()
    _search(pool)
    assert PLUGIN in pool.initialized_providers

    service.update_provider(PLUGIN, enabled=False)

    assert detach.wait_idle(WAIT)
    assert (PLUGIN, False) in world.settings_writes
    assert PLUGIN not in pool.initialized_providers and worker.stops == 1
    # Still the active release, and still registered: switching it on again needs no restart.
    assert world.registry[PLUGIN] is cls and world.row()["state"] == "active"
    assert service.get_provider(PLUGIN)["needs_restart"] is False


def test_a_call_in_flight_when_a_provider_is_switched_off_finishes_before_its_worker_ends(world):
    from provider_hub import detach, service

    world.install()
    gate, started = threading.Event(), threading.Event()
    _, worker = world.run(gate=gate, started=started)
    pool = world.pool()
    thread, outcome = _in_thread(_search, pool)
    assert started.wait(WAIT)

    service.update_provider(PLUGIN, enabled=False)
    time.sleep(0.2)
    assert worker.stops == 0
    gate.set()
    thread.join(WAIT)

    assert outcome["value"].status == "empty"
    assert detach.wait_idle(WAIT) and worker.stops == 1


def test_switching_on_stops_nothing(world):
    from provider_hub import detach, service

    world.install()
    _, worker = world.run()
    pool = world.pool()
    _search(pool)

    service.update_provider(PLUGIN, enabled=True)

    assert detach.wait_idle(WAIT) and worker.stops == 0 and PLUGIN in pool.initialized_providers


# ---- revoke -----------------------------------------------------------------------------------------------------


@pytest.fixture
def revoked(world, monkeypatch):
    """Make the catalog's revocation list name ``PLUGIN``, and a short grace before its calls are cut."""
    from provider_hub import detach, revocation

    monkeypatch.setattr("provider_hub.service._revocations",
                        lambda state: revocation.parse([{"id": PLUGIN, "reason": "It sends credentials away."}]))
    monkeypatch.setattr(detach, "REVOKE_GRACE_SECONDS", 0.3)
    return world


def _mark_revoked(world):
    """What a refresh commits before it stops anything: the row carries the revoked marks."""
    from provider_hub import service
    from provider_hub.state import mutate_state

    mutate_state(lambda state: service._mark_revoked_installations(
        state["installations"], service._revocations(state)))


def test_a_revoked_provider_is_stopped_live_and_leaves_every_consumer(revoked):
    from provider_hub import detach, service
    from provider_hub.state import blocked_installations, running_installations

    revoked.install()
    cls, worker = revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)
    assert revoked.row()["pending_restart"] is False

    assert service.enforce_revocations_live() == [PLUGIN]

    row = revoked.row()
    assert row["blocked"] is True and row["blocked_cause"] == "revoked" and row["pending_restart"] is False
    assert "It was stopped." in row["revoked_reason"] and "Restart" not in row["revoked_reason"]
    assert service.get_provider(PLUGIN)["needs_restart"] is False
    assert running_installations(kind=None) == [] and len(blocked_installations(kind=None)) == 1
    assert detach.wait_idle(WAIT)
    assert revoked.registry[PLUGIN] is None and PLUGIN not in pool.initialized_providers
    assert worker.stops == 1
    # Still installed, so it can be updated or removed; and its release record is the one the boot would hold.
    assert row["active_version"] == "1.0.0" and row["state"] == "active"


def test_the_calls_a_revoked_provider_still_has_are_cut_after_the_grace_and_not_before(revoked):
    from provider_hub import detach, service

    revoked.install()
    gate, started = threading.Event(), threading.Event()
    _, worker = revoked.run(gate=gate, started=started)
    pool = revoked.pool()
    thread, outcome = _in_thread(_search, pool)
    assert started.wait(WAIT)
    _mark_revoked(revoked)

    began = time.monotonic()
    service.enforce_revocations_live()
    thread.join(WAIT)
    assert detach.wait_idle(WAIT)

    # The call ended by the cut: a worker that went away, not a normal result, and not before the grace.
    assert worker.cancels >= 1 and time.monotonic() - began >= 0.3
    assert outcome["value"].status != "empty"
    assert worker.stops == 1


def test_a_revoked_providers_workers_lose_their_host_capabilities_at_once(revoked):
    import weakref

    from provider_hub import detach, registry, service
    from provider_hub.capabilities import live_trust

    revoked.install()
    worker = FakeWorker()
    revoked.run(worker=worker)
    registry._HOST_CALL_CLIENTS.setdefault(PLUGIN, weakref.WeakSet()).add(worker)
    _mark_revoked(revoked)
    # Trusted on the record, but revoked: a worker that is finishing its calls is trusted no more.
    assert live_trust(PLUGIN) is False

    try:
        service.enforce_revocations_live()
        assert detach.wait_idle(WAIT)
        assert worker.host_calls_revoked == 1
    finally:
        registry._HOST_CALL_CLIENTS.pop(PLUGIN, None)


def test_switching_a_provider_off_does_not_take_its_host_capabilities_away(world):
    import weakref

    from provider_hub import detach, registry, service

    world.install()
    worker = FakeWorker()
    world.run(worker=worker)
    registry._HOST_CALL_CLIENTS.setdefault(PLUGIN, weakref.WeakSet()).add(worker)
    try:
        service.update_provider(PLUGIN, enabled=False)
        assert detach.wait_idle(WAIT)
        assert worker.host_calls_revoked == 0
    finally:
        registry._HOST_CALL_CLIENTS.pop(PLUGIN, None)


def test_a_plugin_that_relies_on_a_revoked_one_is_held_back_and_stopped_with_it(revoked, monkeypatch):
    from provider_hub import detach, service

    dependent = "dependenthub"
    revoked.install()
    revoked.install(dependent, supplied_by={"thing.v1": PLUGIN})
    cls, worker = revoked.run()
    _, dependent_worker = revoked.run(dependent)
    pool = revoked.pool(PLUGIN, dependent)
    _search(pool), _search(pool, dependent)
    monkeypatch.setattr("provider_hub.closure.unmet_needs",
                        lambda installations: {dependent: ["thing.v1"]} if installations[PLUGIN].get("revoked") else {})
    _mark_revoked(revoked)

    assert sorted(service.enforce_revocations_live()) == sorted([dependent, PLUGIN])

    assert detach.wait_idle(WAIT)
    assert revoked.row(dependent)["blocked_cause"] == "needs"
    assert revoked.registry[dependent] is None and dependent_worker.stops == 1 and worker.stops == 1


def test_a_release_that_cannot_be_stopped_live_asks_for_the_restart_instead(revoked, monkeypatch):
    from provider_hub import registry, service

    revoked.install()
    revoked.run()
    _mark_revoked(revoked)

    def refuse(plugin_id, grace=None):
        raise registry.RetirementIncomplete("a pool would not let go")

    monkeypatch.setattr(registry, "retire_provider_instances", refuse)

    assert service.enforce_revocations_live() == []

    row = revoked.row()
    assert row["pending_restart"] is True and row["revoked_restart"] is True and row["detach_failed"] is True
    assert "Restart Bazarr+ to stop it" in row["revoked_reason"] and row["last_error"] == row["revoked_reason"]


def test_a_second_refresh_does_not_stop_a_stopped_release_again(revoked):
    from provider_hub import detach, service

    revoked.install()
    _, worker = revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)

    assert service.enforce_revocations_live() == [PLUGIN]
    assert detach.wait_idle(WAIT)
    assert service.enforce_revocations_live() == []
    assert worker.stops == 1


# ---- the host's record of trust ---------------------------------------------------------------------------------


@pytest.mark.parametrize("marks, trusted", [({}, True), ({"revoked": True}, False), ({"blocked": True}, False),
                                            ({"state": "removed"}, False)])
def test_a_release_that_was_detached_holds_no_trust_for_the_host_capabilities(world, marks, trusted):
    from provider_hub.capabilities import live_trust

    world.install(**marks)

    assert live_trust(PLUGIN) is trusted


# ---- a replaced release -----------------------------------------------------------------------------------------


def test_the_provider_kind_stops_live_and_attaches_on_restart():
    from provider_hub.kinds import ATTACH_ON_RESTART, DETACH_LIVE, SUBTITLE_PROVIDER, get_kind

    kind = get_kind(SUBTITLE_PROVIDER)
    assert kind.detach_policy == DETACH_LIVE and kind.attach_policy == ATTACH_ON_RESTART


def test_a_kind_with_an_unknown_detach_policy_is_refused():
    from provider_hub.kinds import Kind, register_kind

    class Odd(Kind):
        name = "odd_detach"
        detach_policy = "sometimes"

    with pytest.raises(ValueError):
        register_kind(Odd())


def test_a_kind_that_stops_on_restart_keeps_the_restart_cue_for_a_removal(world):
    from provider_hub import service
    from provider_hub.kinds import Kind, register_kind, unregister_kind

    class Slow(Kind):
        name = "slow_kind"

        def on_removed(self, plugin_id):
            return True

    register_kind(Slow())
    try:
        world.install("slowplugin", kind="slow_kind")
        service.remove_installation("slowplugin")
        row = world.row("slowplugin")
        assert row["state"] == "removed" and row["pending_restart"] is True
    finally:
        unregister_kind("slow_kind")


# ---- switching off and on again while a call is held ------------------------------------------------------------


def _real_client_class(clients, *, gate=None, started=None):
    """A plugin class whose every instance has a worker client of its own, listed among the live workers the way a
    started client is, so the sweep after a detach can see it."""
    from subzero.language import Language
    from provider_hub import worker
    from provider_hub.registry import HubProxyProvider

    class Plugin(HubProxyProvider):
        provider_name = PLUGIN
        languages = {Language("eng")}

        def __init__(self, **config):
            client = worker.ProviderWorkerClient(["python"], provider_id=PLUGIN)
            with worker._live_clients_lock:
                worker._live_clients.add(client)
            clients.append(client)
            super().__init__(worker_client=client, **config)

        def list_subtitles(self, video, languages):
            with self._operation():
                if started is not None and not started.is_set():
                    started.set()
                    gate.wait(WAIT)
                if self.worker_client._closed:
                    from provider_hub.worker import WorkerBusy

                    raise WorkerBusy("worker client is stopped")
                return []

    return Plugin


def test_switching_off_and_on_while_a_call_is_held_leaves_the_new_instance_with_a_working_worker(world):
    from provider_hub import detach, service
    from provider_hub.registry import HubProxyProvider

    world.install()
    clients, gate, started = [], threading.Event(), threading.Event()
    world.registry.register(PLUGIN, _real_client_class(clients, gate=gate, started=started))
    pool = world.pool()
    held, outcome = _in_thread(_search, pool)
    assert started.wait(WAIT)

    # Off, on, off, on in quick succession while the first call is still held on its instance.
    service.update_provider(PLUGIN, enabled=False)
    service.update_provider(PLUGIN, enabled=True)
    assert _search(pool).status == "empty"  # builds a fresh instance with a worker of its own
    service.update_provider(PLUGIN, enabled=False)
    service.update_provider(PLUGIN, enabled=True)
    assert _search(pool).status == "empty"
    current = pool.initialized_providers[PLUGIN]
    assert isinstance(current, HubProxyProvider) and current.worker_client is clients[-1]

    gate.set()
    held.join(WAIT)
    assert detach.wait_idle(WAIT)

    # What the drain ended is the worker of the instance that was detached: the one the pool holds now is open.
    assert clients[-1]._closed is False
    assert pool.initialized_providers[PLUGIN] is current
    assert _search(pool).status == "empty"


# ---- the cue, the text and the retry across refreshes -----------------------------------------------------------


def test_a_refresh_after_a_failed_stop_keeps_the_cue_and_the_text_and_tries_again(revoked, monkeypatch):
    from provider_hub import detach, registry, service

    revoked.install()
    _, worker = revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)
    failing = {"on": True}
    real = registry.retire_provider_instances

    def retire(plugin_id, grace=None):
        if failing["on"]:
            raise registry.RetirementIncomplete("a pool would not let go")
        return real(plugin_id, grace)

    monkeypatch.setattr(registry, "retire_provider_instances", retire)
    assert service.enforce_revocations_live() == []
    text = revoked.row()["revoked_reason"]
    assert "Restart Bazarr+ to stop it" in text

    # The next refresh marks the row again and tries again: the cue and the words stay while it still fails.
    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == []
    row = revoked.row()
    assert row["pending_restart"] is True and row["revoked_restart"] is True and row["revoked_reason"] == text
    assert row["last_error"] == text

    # Once it can be stopped it is, and the cue goes with the stop.
    failing["on"] = False
    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == [PLUGIN]
    assert detach.wait_idle(WAIT)
    row = revoked.row()
    assert row["pending_restart"] is False and "revoked_restart" not in row and "detach_failed" not in row
    assert "It was stopped." in row["revoked_reason"] and worker.stops == 1


def test_a_stopped_release_keeps_the_same_words_through_the_next_refresh(revoked):
    from provider_hub import detach, service

    revoked.install()
    revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)
    service.enforce_revocations_live()
    assert detach.wait_idle(WAIT)
    before = revoked.row()
    words = (before["revoked_reason"], before["blocked_reason"], before["last_error"])
    assert len(set(words)) == 1 and "It was stopped." in words[0]

    # A refresh marks the row again, and the live pass has nothing new to stop.
    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == []

    after = revoked.row()
    assert (after["revoked_reason"], after["blocked_reason"], after["last_error"]) == words


def test_the_boot_words_a_stopped_release_as_one_that_will_not_start(revoked):
    from provider_hub import detach, service

    revoked.install()
    revoked.run()
    _mark_revoked(revoked)
    service.enforce_revocations_live()
    assert detach.wait_idle(WAIT)

    service.enforce_activation_needs()

    row = revoked.row()
    assert "It will not start." in row["blocked_reason"] and "revoked_stopped" not in row
    # And a refresh after that keeps the words the boot wrote.
    _mark_revoked(revoked)
    assert "It will not start." in revoked.row()["revoked_reason"]


def test_the_live_hold_stops_only_what_relies_on_the_revoked_plugin(revoked, monkeypatch):
    from provider_hub import detach, service

    dependent, other = "dependenthub", "lacksthing"
    revoked.install()
    revoked.install(dependent)
    revoked.install(other)
    for plugin_id in (PLUGIN, dependent, other):
        revoked.run(plugin_id)

    def unmet(installations):
        # ``other`` lacks what it needs whatever happens; ``dependent`` lacks it once the revoked plugin is out.
        found = {other: ["thing.v1"]}
        if installations[PLUGIN].get("revoked"):
            found[dependent] = ["thing.v1"]
        return found

    monkeypatch.setattr("provider_hub.closure.unmet_needs", unmet)
    _mark_revoked(revoked)

    assert sorted(service.enforce_revocations_live()) == sorted([PLUGIN, dependent])

    assert detach.wait_idle(WAIT)
    assert "blocked" not in revoked.row(other) and revoked.registry[other] is not None


# ---- configuration and the registry after a detach --------------------------------------------------------------


@pytest.mark.parametrize("marks, gives_config", [({}, True), ({"blocked": True}, False),
                                                  ({"state": "removed", "pending_restart": False}, False),
                                                  ({"state": "removed", "pending_restart": True}, True)])
def test_a_built_in_that_took_its_id_back_is_not_handed_the_detached_plugins_configuration(world, marks,
                                                                                           gives_config):
    from provider_hub.service import runtime_provider_configs

    world.install(config={"request_delay_ms": 5}, **marks)

    assert (PLUGIN in runtime_provider_configs()) is gives_config


def test_a_candidate_a_detached_plugin_found_is_not_downloaded_by_the_built_in_that_took_its_id(world):
    from provider_hub.protocol import HubWorkerSubtitle

    class BuiltIn:
        hub_proxy = False
        provider_name = PLUGIN
        languages = set()
        called = []

        def __init__(self, **config):
            pass

        def initialize(self):
            pass

        def download_subtitle(self, subtitle):
            BuiltIn.called.append(subtitle)

    world.registry.register(PLUGIN, BuiltIn)
    pool = world.pool()
    subtitle = HubWorkerSubtitle.__new__(HubWorkerSubtitle)
    subtitle.provider_name = PLUGIN

    assert pool.download_subtitle(subtitle) is False

    assert subtitle.skip_reason == "provider_excluded" and BuiltIn.called == []


def test_a_download_for_a_provider_that_left_the_registry_after_the_admission_check_is_refused_quietly(world):
    pool = world.pool("other")
    subtitle = _subtitle()  # names PLUGIN, which this pool was not built with and the registry does not hold

    assert pool.download_subtitle(subtitle) is False

    assert subtitle.skip_reason == "provider_excluded"


# ---- a removal's bound ------------------------------------------------------------------------------------------


def test_a_removal_cuts_the_calls_that_outlive_its_grace(world, monkeypatch):
    from provider_hub import detach, service

    monkeypatch.setattr(detach, "REMOVE_GRACE_SECONDS", 0.3)
    world.install()
    gate, started = threading.Event(), threading.Event()
    _, worker = world.run(gate=gate, started=started)
    pool = world.pool()
    thread, outcome = _in_thread(_search, pool)
    assert started.wait(WAIT)

    service.remove_installation(PLUGIN)

    thread.join(WAIT)
    assert detach.wait_idle(WAIT)
    assert worker.cancels >= 1 and outcome["value"].status != "empty"
    assert world.row() is None


def test_a_removal_that_waits_is_marked_removed_with_no_restart_for_the_interface(world):
    from provider_hub import detach, service

    world.install()
    gate, started = threading.Event(), threading.Event()
    world.run(gate=gate, started=started)
    pool = world.pool()
    thread, _ = _in_thread(_search, pool)
    assert started.wait(WAIT)

    service.remove_installation(PLUGIN)
    shown = service.get_provider(PLUGIN)

    # The state the interface reads: removed, and no restart to wait for.
    assert shown["state"] == "removed" and shown["needs_restart"] is False
    gate.set()
    thread.join(WAIT)
    assert detach.wait_idle(WAIT)


# ---- a cancelled client -----------------------------------------------------------------------------------------


def test_a_request_that_reaches_a_cancelled_client_does_not_start_the_worker_again(monkeypatch):
    from provider_hub import worker

    client = worker.ProviderWorkerClient(["python"], provider_id=PLUGIN)
    spawned = []
    monkeypatch.setattr(client, "_spawn", lambda env: spawned.append(env))

    client.cancel()

    # A request that passed its own check just before the cancel gets this far, and must not respawn.
    with pytest.raises(worker.WorkerError):
        client.start()
    with pytest.raises(worker.WorkerError):
        client.request("search", {})
    assert spawned == []


# ---- a built-in that takes its id back --------------------------------------------------------------------------


def _built_in_class(name):
    """A built-in provider: its constructor takes no settings, as most built-ins that have a catalog plugin do not take
    the plugin's keys."""
    from subzero.language import Language
    from subliminal.video import Episode, Movie

    class BuiltIn:
        provider_name = name
        hub_proxy = False
        languages = {Language("eng")}
        video_types = (Episode, Movie)

        def __init__(self):
            pass

        def initialize(self):
            pass

        def terminate(self):
            pass

        @classmethod
        def check(cls, video):
            return True

        def list_subtitles(self, video, languages):
            return []

    return BuiltIn


def _plugin_over_built_in(world, monkeypatch, plugin_id=BUILT_IN):
    """A built-in class that a plugin of the same id replaced in the registry, as the boot leaves it."""
    from provider_hub import registry
    from provider_hub.registry import HubProxyProvider
    from provider_hub.state import active_installations
    from subzero.language import Language

    built_in = _built_in_class(plugin_id)
    world.registry.register(plugin_id, built_in)
    world.install(plugin_id, config={"request_delay_ms": 5})

    class Plugin(HubProxyProvider):
        provider_name = plugin_id
        languages = {Language("eng")}

        def list_subtitles(self, video, languages):
            return []

    monkeypatch.setattr(registry, "_make_provider_class", lambda manifest, installation=None: Plugin)
    assert registry.register_active_provider_classes(active_installations()) == [plugin_id]
    return built_in, Plugin


def test_a_long_lived_pool_rebuilds_the_built_in_that_took_its_id_back_without_the_plugins_configuration(
        world, monkeypatch):
    from provider_hub import detach, service

    built_in, plugin = _plugin_over_built_in(world, monkeypatch)
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {}})
    # The pool is long lived: it was built with the plugin's configuration under the id, and keeps merging into it.
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    assert type(pool[BUILT_IN]) is plugin

    service.remove_installation(BUILT_IN)
    assert detach.wait_idle(WAIT)
    assert world.registry[BUILT_IN] is built_in
    # What the settings give the built-in now: nothing of the plugin's.
    pool.update([BUILT_IN], {BUILT_IN: {}}, [], {})

    result = _search(pool, BUILT_IN)

    assert (result.status, result.reason) == ("empty", None)
    assert type(pool.initialized_providers[BUILT_IN]) is built_in


def test_a_long_lived_pool_that_gets_no_settings_for_the_built_in_builds_it_with_none(world, monkeypatch):
    from provider_hub import detach, service

    built_in, _ = _plugin_over_built_in(world, monkeypatch)
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {})
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5, "api_key": "k"}
    pool[BUILT_IN]

    service.remove_installation(BUILT_IN)
    assert detach.wait_idle(WAIT)
    pool.update([BUILT_IN], {}, [], {})  # no static entry for the id: nothing arrives for it

    assert _search(pool, BUILT_IN).status == "empty"
    assert BUILT_IN not in pool.provider_configs or pool.provider_configs[BUILT_IN] == {}


@pytest.mark.parametrize("marks", [
    {},
    {"blocked": True},
    {"state": "removed", "pending_restart": False},
    # A revocation lifted without a restart: unblocked, restart pending, the release still not running.
    {"pending_restart": True},
    # Installed again over a row that was being removed: staged, with the old release still named.
    {"state": "staged", "pending_restart": True, "staged_version": "1.1.0"},
])
def test_no_path_hands_the_plugins_configuration_to_a_built_in_that_holds_the_id(world, marks):
    from provider_hub.kinds.subtitle_provider import apply_settings_overlay
    from provider_hub.service import runtime_provider_configs

    world.registry.register(BUILT_IN, _built_in_class(BUILT_IN))
    world.install(BUILT_IN, config={"request_delay_ms": 5}, **marks)

    assert BUILT_IN not in runtime_provider_configs()
    configs = {}
    apply_settings_overlay(configs, {}, set(), set())
    assert BUILT_IN not in configs


def test_the_plugins_configuration_is_handed_to_the_plugin_that_holds_the_id(world, monkeypatch):
    from provider_hub.kinds.subtitle_provider import apply_settings_overlay
    from provider_hub.service import runtime_provider_configs

    _plugin_over_built_in(world, monkeypatch)

    assert runtime_provider_configs()[BUILT_IN]["request_delay_ms"] == 5
    configs = {}
    apply_settings_overlay(configs, {}, set(), set())
    assert BUILT_IN in configs


# ---- what a refresh leaves alone, and what a retry clears -------------------------------------------------------


def test_a_refresh_leaves_the_words_the_boot_wrote_for_a_release_it_held_back(revoked):
    from provider_hub import detach, service

    revoked.install()
    revoked.run()
    _mark_revoked(revoked)
    # A boot held the release back before this process ran: it never started here.
    service.enforce_activation_needs()
    before = revoked.row()
    assert "It will not start." in before["blocked_reason"]
    words = (before["revoked_reason"], before["blocked_reason"], before["last_error"])

    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == []

    after = revoked.row()
    assert (after["revoked_reason"], after["blocked_reason"], after["last_error"]) == words
    assert "revoked_stopped" not in after and detach.wait_idle(WAIT)


def test_a_dependent_whose_stop_failed_and_then_worked_loses_its_restart_cue(revoked, monkeypatch):
    from provider_hub import detach, registry, service

    dependent = "dependenthub"
    revoked.install()
    revoked.install(dependent)
    revoked.run()
    revoked.run(dependent)
    pool = revoked.pool(PLUGIN, dependent)
    _search(pool), _search(pool, dependent)
    monkeypatch.setattr("provider_hub.closure.unmet_needs",
                        lambda installations: {dependent: ["thing.v1"]} if installations[PLUGIN].get("revoked") else {})
    failing = {"on": True}
    real = registry.retire_provider_instances

    def retire(plugin_id, grace=None):
        if failing["on"] and plugin_id == dependent:
            raise registry.RetirementIncomplete("a pool would not let go")
        return real(plugin_id, grace)

    monkeypatch.setattr(registry, "retire_provider_instances", retire)
    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == [PLUGIN]
    assert revoked.row(dependent)["pending_restart"] is True and revoked.row(dependent)["detach_failed"] is True

    failing["on"] = False
    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == [dependent]

    assert detach.wait_idle(WAIT)
    row = revoked.row(dependent)
    assert row["pending_restart"] is False and "revoked_restart" not in row and "detach_failed" not in row


def test_a_refresh_that_cannot_run_the_live_stop_asks_for_the_restart(revoked, monkeypatch):
    from provider_hub import service

    revoked.install()
    revoked.run()
    _mark_revoked(revoked)
    assert revoked.row()["pending_restart"] is False  # the words say stopped, and the stop is still to come

    service.ask_restart_for_unstopped()

    row = revoked.row()
    assert row["pending_restart"] is True and row["detach_failed"] is True
    assert "Restart Bazarr+ to stop it" in row["revoked_reason"]


def test_the_pools_of_every_kind_forget_the_configuration_of_a_provider_that_left(world, monkeypatch):
    from provider_hub.registry import release_provider_id
    from subliminal_patch.core import SZAsyncProviderPool

    world.run()
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {})
    plain = world.pool()
    plain.provider_configs[PLUGIN] = {"request_delay_ms": 5}
    asynchronous = SZAsyncProviderPool(providers=[PLUGIN], provider_configs={PLUGIN: {"request_delay_ms": 5}})

    assert release_provider_id(PLUGIN) >= 2

    assert plain.provider_configs.get(PLUGIN, {}) == {} and asynchronous.provider_configs.get(PLUGIN, {}) == {}


# ---- the built-in that takes its id back ------------------------------------------------------------------------


def _credentialed_built_in(name):
    """A built-in that refuses to be built without its credential, as most credentialed built-ins do."""
    built_in = _built_in_class(name)

    def init(self, password=None):
        if not password:
            raise ValueError("no password")
        self.password = password

    built_in.__init__ = init
    return built_in


def test_a_built_in_that_takes_its_id_back_is_built_with_its_own_saved_settings_at_once(world, monkeypatch):
    """Nothing waits for the next settings read: a call that reaches the id right after the detach finds the built-in's
    settings in the pool, not an empty configuration that the built-in refuses."""
    from provider_hub import detach, service
    from provider_hub.registry import HubProxyProvider
    from provider_hub.state import active_installations
    from subzero.language import Language

    built_in = _credentialed_built_in(BUILT_IN)
    world.registry.register(BUILT_IN, built_in)
    world.install(BUILT_IN, config={"request_delay_ms": 5})

    class Plugin(HubProxyProvider):
        provider_name = BUILT_IN
        languages = {Language("eng")}

    from provider_hub import registry

    monkeypatch.setattr(registry, "_make_provider_class", lambda manifest, installation=None: Plugin)
    registry.register_active_provider_classes(active_installations())
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {"password": "secret"}})
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    assert type(pool[BUILT_IN]) is Plugin

    service.remove_installation(BUILT_IN)
    assert detach.wait_idle(WAIT)

    # No update ran: the built-in is built from what the pool was handed at the detach.
    rebuilt = pool[BUILT_IN]
    assert type(rebuilt) is built_in and rebuilt.password == "secret"


def test_a_built_in_that_takes_its_id_back_inherits_no_throttle_or_discard_the_plugin_earned(world, monkeypatch):
    import datetime as dt

    from app import get_providers
    from provider_hub import detach, service
    from subliminal_patch.provider_health import get_tracker

    _plugin_over_built_in(world, monkeypatch)
    pool = world.pool(BUILT_IN)
    pool.discarded_providers.add(BUILT_IN)
    get_providers.tp[BUILT_IN] = ("ConfigurationError", dt.datetime.now() + dt.timedelta(hours=12), "12 hours")
    monkeypatch.setattr(get_providers, "throttle_count", {BUILT_IN: {"count": 4, "time": dt.datetime.now()}})
    tracker = get_tracker()
    for _ in range(10):
        tracker.record(BUILT_IN, "exception")
    assert tracker.is_discarded(BUILT_IN)

    try:
        service.remove_installation(BUILT_IN)
        assert detach.wait_idle(WAIT)

        assert BUILT_IN not in get_providers.tp and not get_providers.provider_is_throttled(BUILT_IN)
        assert BUILT_IN not in get_providers.throttle_count
        assert not tracker.is_discarded(BUILT_IN) and BUILT_IN not in pool.discarded_providers
    finally:
        get_providers.tp.pop(BUILT_IN, None)
        tracker.forget(BUILT_IN)


def test_a_snapshot_of_the_settings_read_before_the_detach_is_not_merged_back_after_it(world, monkeypatch):
    from provider_hub import detach, service
    from subliminal_patch import core

    _plugin_over_built_in(world, monkeypatch)
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    # A search read the settings (with the plugin's keys) just before the detach, and updates the pool just after.
    stale = core.ConfigSnapshot({BUILT_IN: {"request_delay_ms": 5}})
    stale.ownership = world.registry.ownership_generations()

    service.remove_installation(BUILT_IN)
    assert detach.wait_idle(WAIT)
    pool.update([BUILT_IN], stale, [], {})
    # A pool built from the same snapshot at that moment gets nothing of it either.
    late = core.SZProviderPool(providers=[BUILT_IN], provider_configs=stale)

    assert "request_delay_ms" not in pool.provider_configs.get(BUILT_IN, {})
    assert "request_delay_ms" not in late.provider_configs.get(BUILT_IN, {})


def test_a_removal_of_a_plugin_that_never_held_the_id_leaves_the_built_ins_pool_settings_alone(world):
    from provider_hub import detach, service

    built_in = _built_in_class(BUILT_IN)
    world.registry.register(BUILT_IN, built_in)  # the plugin's registration was refused: the built-in serves the id
    world.install(BUILT_IN)
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"password": "own"}

    service.remove_installation(BUILT_IN)

    assert detach.wait_idle(WAIT)
    assert pool.provider_configs[BUILT_IN] == {"password": "own"}


def test_a_double_failure_leaves_the_release_marked_for_a_retry_and_asks_for_the_restart(revoked, monkeypatch):
    from provider_hub import registry, service

    revoked.install()
    revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)
    real_ask = service._ask_restart_to_stop
    state = {"fail": True}

    def refuse(plugin_id, grace=None):
        raise registry.RetirementIncomplete("a pool would not let go")

    def ask(plugin_ids):
        if state["fail"]:
            raise RuntimeError("the write failed")
        return real_ask(plugin_ids)

    monkeypatch.setattr(registry, "retire_provider_instances", refuse)
    monkeypatch.setattr(service, "_ask_restart_to_stop", ask)
    with pytest.raises(RuntimeError):
        service.enforce_revocations_live()
    # The holds committed and the stop failed, and so did the note of it: the row is marked, not left as stopped.
    assert revoked.row()["detach_pending"] is True

    state["fail"] = False
    assert service.ask_restart_for_unstopped() == [PLUGIN]
    row = revoked.row()
    assert row["pending_restart"] is True and row["detach_failed"] is True
    assert "Restart Bazarr+ to stop it" in row["revoked_reason"]


def test_a_release_still_marked_for_a_stop_is_tried_again_by_the_next_refresh(revoked, monkeypatch):
    from provider_hub import detach, registry, service

    revoked.install()
    revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)
    real = registry.retire_provider_instances
    monkeypatch.setattr(registry, "retire_provider_instances",
                        lambda plugin_id, grace=None: (_ for _ in ()).throw(registry.RetirementIncomplete("no")))
    service.enforce_revocations_live()
    monkeypatch.setattr(registry, "retire_provider_instances", real)

    _mark_revoked(revoked)
    assert service.enforce_revocations_live() == [PLUGIN]

    assert detach.wait_idle(WAIT)
    row = revoked.row()
    assert "detach_pending" not in row and "detach_failed" not in row and row["pending_restart"] is False


def test_a_revocation_lifted_after_a_failed_stop_keeps_the_restart_cue(revoked, monkeypatch):
    from provider_hub import registry, revocation, service

    revoked.install()
    revoked.run()
    pool = revoked.pool()
    _search(pool)
    _mark_revoked(revoked)
    monkeypatch.setattr(registry, "retire_provider_instances",
                        lambda plugin_id, grace=None: (_ for _ in ()).throw(registry.RetirementIncomplete("no")))
    service.enforce_revocations_live()
    assert revoked.row()["pending_restart"] is True

    # The catalog lifts the revocation: the refresh marks the row again and the live pass runs.
    monkeypatch.setattr("provider_hub.service._revocations", lambda state: revocation.parse([]))
    _mark_revoked(revoked)
    service.enforce_revocations_live()

    row = revoked.row()
    assert row["pending_restart"] is True and "blocked" not in row


def test_a_built_ins_ordinary_settings_change_after_a_snapshot_is_still_applied(world):
    """The stamp changes only when an id changes hands: a settings save, which bumps the provider's own generation in
    every pool that sees it, must never make a pool drop the new settings of a built-in."""
    from subliminal_patch import core

    world.registry.register(BUILT_IN, _credentialed_built_in(BUILT_IN))
    pool = world.pool(BUILT_IN)
    snapshot = core.ConfigSnapshot({BUILT_IN: {"password": "new"}})
    snapshot.ownership = world.registry.ownership_generations()
    pool.provider_configs[BUILT_IN] = {"password": "old"}
    # Another pool saw the same change first and bumped the provider's generation, as every settings save does.
    core.bump_provider_generation(BUILT_IN)

    pool.update([BUILT_IN], snapshot, [], {})

    assert pool.provider_configs[BUILT_IN]["password"] == "new"
    late = core.SZProviderPool(providers=[BUILT_IN], provider_configs=snapshot)
    assert late.provider_configs[BUILT_IN]["password"] == "new"


def test_a_snapshot_read_before_the_plugin_left_is_dropped_even_before_the_pools_are_released(world, monkeypatch):
    """The handover is counted by the registry at the moment the class changes, before anything else of the detach
    runs: a snapshot read before it and merged in the gap that follows is dropped too."""
    from provider_hub import registry
    from subliminal_patch import core

    _plugin_over_built_in(world, monkeypatch)
    pool = world.pool(BUILT_IN)
    stale = core.ConfigSnapshot({BUILT_IN: {"request_delay_ms": 5}})
    stale.ownership = world.registry.ownership_generations()

    assert registry.unregister_provider_class(BUILT_IN).removed
    pool.update([BUILT_IN], stale, [], {})  # the gap: the class changed, nothing else has run yet

    assert "request_delay_ms" not in pool.provider_configs.get(BUILT_IN, {})


@pytest.mark.parametrize("enabled", [True, False])
def test_a_settings_merge_rechecks_the_owner_after_another_update(world, monkeypatch, enabled):
    from provider_hub import registry
    from subliminal_patch import core

    _plugin_over_built_in(world, monkeypatch)
    first = "other_built_in"
    world.registry.register(first, _credentialed_built_in(first))
    names = [first, BUILT_IN] if enabled else [first]
    pool = world.pool(*names)
    pool.provider_configs[first] = {"password": "old"}
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    stale = core.ConfigSnapshot({first: {"password": "changed"}, BUILT_IN: {"request_delay_ms": 10}})
    stale.ownership = world.registry.ownership_generations()
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {"password": "own"}})
    entered, resume = threading.Event(), threading.Event()
    retire = pool.retire_provider

    def pause_first_restart(name, *args, **kwargs):
        if name == first:
            entered.set()
            assert resume.wait(WAIT)
        return retire(name, *args, **kwargs)

    monkeypatch.setattr(pool, "retire_provider", pause_first_restart)
    thread, outcome = _in_thread(pool.provider_configs.update, stale)
    try:
        assert entered.wait(WAIT)
        assert registry.unregister_provider_class(BUILT_IN).restored_built_in
        registry.release_provider_id(BUILT_IN)
        fresh = core.ConfigSnapshot({BUILT_IN: {"password": "new"}})
        fresh.ownership = world.registry.ownership_generations()
        pool.provider_configs.update(fresh)
    finally:
        resume.set()
        thread.join(WAIT)

    assert outcome == {"value": None}
    assert pool.provider_configs[first] == {"password": "changed"}
    assert pool.provider_configs[BUILT_IN] == {"password": "new"}


def test_a_settings_restart_started_by_the_plugin_leaves_the_restored_built_in_alone(world, monkeypatch):
    from provider_hub import registry
    from subliminal_patch import core

    _plugin_over_built_in(world, monkeypatch)
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    stale = core.ConfigSnapshot({BUILT_IN: {"request_delay_ms": 10}})
    stale.ownership = world.registry.ownership_generations()
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {}})
    entered, resume = threading.Event(), threading.Event()
    retire = pool.retire_provider

    def pause_restart(name, *args, **kwargs):
        entered.set()
        assert resume.wait(WAIT)
        return retire(name, *args, **kwargs)

    monkeypatch.setattr(pool, "retire_provider", pause_restart)
    thread, result = _in_thread(pool.provider_configs.update, stale)
    try:
        assert entered.wait(WAIT)
        assert registry.unregister_provider_class(BUILT_IN).restored_built_in
        registry.release_provider_id(BUILT_IN)
        restored = pool[BUILT_IN]
    finally:
        resume.set()
        thread.join(WAIT)

    assert result == {"value": None}
    assert pool[BUILT_IN] is restored


def test_a_snapshot_that_carries_no_stamp_is_merged_whole(world):
    from subliminal_patch import core

    world.registry.register(BUILT_IN, _credentialed_built_in(BUILT_IN))
    pool = world.pool(BUILT_IN)

    pool.update([BUILT_IN], core.ConfigSnapshot({BUILT_IN: {"password": "p"}}), [], {})

    assert pool.provider_configs[BUILT_IN]["password"] == "p"


def test_the_registry_counts_a_handover_between_a_plugin_and_a_built_in_and_nothing_else(world):
    from provider_hub.registry import HubProxyProvider

    class Plugin(HubProxyProvider):
        provider_name = "swapped"

    built_in = _built_in_class("swapped")
    start = world.registry.ownership_generations().get("swapped", 0)
    world.registry.register("swapped", built_in)
    world.registry.register("swapped", built_in)  # a built-in registered again is no handover
    assert world.registry.ownership_generations().get("swapped", 0) == start
    world.registry.register("swapped", Plugin)  # the plugin takes the id
    assert world.registry.ownership_generations()["swapped"] == start + 1
    world.registry.unregister("swapped", expected=Plugin)  # and leaves
    assert world.registry.ownership_generations()["swapped"] == start + 2
    world.registry.register("swapped", built_in)  # the built-in is put back over nothing: no plugin involved
    assert world.registry.ownership_generations()["swapped"] == start + 2


def test_deleting_a_plugin_from_the_registry_moves_its_ownership_stamp(world):
    world.run()
    stamp = world.registry.ownership_generations()[PLUGIN]

    del world.registry[PLUGIN]

    assert world.registry.ownership_generations()[PLUGIN] == stamp + 1
    with pytest.raises(KeyError):
        del world.registry[PLUGIN]


def test_a_built_in_built_in_the_window_before_the_pools_are_released_is_retired_and_rebuilt_clean(world, monkeypatch):
    """The class swap and the release of the pools are two steps. A pool that builds the restored built-in between
    them holds an instance made from the plugin's configuration: it is retired right after the release, and the next
    call builds the built-in from its own settings."""
    from provider_hub import detach, registry, service

    built_in, plugin = _plugin_over_built_in(world, monkeypatch)
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    assert type(pool[BUILT_IN]) is plugin
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {"password": "own"}})
    real = registry.release_provider_id
    built = []

    def release_after_a_build(provider_id):
        # The window: the registry already gives the built-in, nothing of the pools has been released.
        pool.initialized_providers.pop(BUILT_IN, None)
        try:
            built.append(pool._build_and_install(BUILT_IN, lambda: pool.provider_configs.get(BUILT_IN, {})))
        except TypeError:
            # The built-in refuses the plugin's keys: the instance is made with what it can take.
            built.append(None)
        return real(provider_id)

    class Takes(built_in):
        def __init__(self, **config):
            self.config = config

    world.registry.register(BUILT_IN, plugin)  # put the plugin back so remove swaps it once more
    registry._SHADOWED_BUILT_INS[BUILT_IN] = Takes
    monkeypatch.setattr(registry, "release_provider_id", release_after_a_build)

    service.remove_installation(BUILT_IN)
    assert detach.wait_idle(WAIT)

    assert built and built[0] is not None and built[0].config == {"request_delay_ms": 5}
    # The instance made in the window is gone from the pool; the next one is built from the built-in's own settings.
    assert pool.initialized_providers.get(BUILT_IN) is not built[0]
    assert pool[BUILT_IN].config == {"password": "own"}


def test_the_release_window_built_in_finishes_before_its_replacement_initializes(world, monkeypatch):
    from provider_hub import registry

    built_in, _ = _plugin_over_built_in(world, monkeypatch)
    pool = world.pool(BUILT_IN)
    entered, resume, terminated = threading.Event(), threading.Event(), threading.Event()
    replacement_waiting = threading.Event()
    wait = pool._built_in_retirement_ended.wait

    def note_waiting(*args):
        replacement_waiting.set()
        return wait(*args)

    monkeypatch.setattr(pool._built_in_retirement_ended, "wait", note_waiting)

    class Searching(built_in):
        def __init__(self, **config):
            self.config = config

        def initialize(self):
            if self.config == {"password": "own"}:
                assert terminated.is_set()

        def list_subtitles(self, video, languages):
            entered.set()
            assert resume.wait(WAIT)
            assert not terminated.is_set()
            return []

        def terminate(self):
            if "request_delay_ms" in self.config:
                terminated.set()

    registry._SHADOWED_BUILT_INS[BUILT_IN] = Searching
    pool.provider_configs[BUILT_IN] = {"request_delay_ms": 5}
    assert registry.unregister_provider_class(BUILT_IN).restored_built_in
    old = pool[BUILT_IN]
    thread, outcome = _in_thread(_search, pool, BUILT_IN)
    replacement = None
    try:
        assert entered.wait(WAIT)
        monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {"password": "own"}})
        registry.release_provider_id(BUILT_IN)

        assert not terminated.is_set()
        replacement, built = _in_thread(pool.__getitem__, BUILT_IN)
        assert replacement_waiting.wait(WAIT)
    finally:
        resume.set()
        thread.join(WAIT)
        if replacement is not None:
            replacement.join(WAIT)

    assert outcome["value"].status == "empty"
    clean = built["value"]
    assert clean is not old and clean.config == {"password": "own"}


def test_an_older_call_cannot_end_a_newer_instances_retirement(world, monkeypatch):
    from provider_hub import registry

    first_started, second_started = threading.Event(), threading.Event()
    first_done, second_done = threading.Event(), threading.Event()
    replacement_waiting = threading.Event()
    terminated = []
    built_in = _built_in_class(BUILT_IN)

    class Searching(built_in):
        def __init__(self, password):
            self.password = password

        def initialize(self):
            if self.password == "own":
                assert "second" in terminated

        def list_subtitles(self, video, languages):
            started, done = {"first": (first_started, first_done), "second": (second_started, second_done)}[self.password]
            started.set()
            assert done.wait(WAIT)
            return []

        def terminate(self):
            terminated.append(self.password)

    world.registry.register(BUILT_IN, Searching)
    pool = world.pool(BUILT_IN)
    pool.provider_configs[BUILT_IN] = {"password": "first"}
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {"password": "own"}})
    wait = pool._built_in_retirement_ended.wait

    def note_waiting(*args):
        replacement_waiting.set()
        return wait(*args)

    monkeypatch.setattr(pool._built_in_retirement_ended, "wait", note_waiting)
    first, first_result = _in_thread(_search, pool, BUILT_IN)
    second = replacement = None
    try:
        assert first_started.wait(WAIT)
        pool.provider_configs.update({BUILT_IN: {"password": "second"}})
        assert terminated == ["first"]
        second, second_result = _in_thread(_search, pool, BUILT_IN)
        assert second_started.wait(WAIT)
        registry.release_provider_id(BUILT_IN)
        replacement, built = _in_thread(pool.__getitem__, BUILT_IN)
        assert replacement_waiting.wait(WAIT)

        first_done.set()
        first.join(WAIT)

        assert first_result["value"].status == "empty"
        assert terminated == ["first"]
    finally:
        first_done.set()
        second_done.set()
        first.join(WAIT)
        if second is not None:
            second.join(WAIT)
        if replacement is not None:
            replacement.join(WAIT)

    assert second_result["value"].status == "empty"
    assert terminated == ["first", "second"]
    assert built["value"].password == "own"


@pytest.mark.parametrize("download", [False, True], ids=["provider-response", "payload-validation"])
def test_an_independent_failure_during_cut_is_visible_without_throttling_the_new_owner(world, monkeypatch, download):
    from app import get_providers
    from provider_hub import detach, service
    from provider_hub.protocol import HubWorkerSubtitle, WorkerProtocolError
    from provider_hub.registry import HubProxyProvider
    from provider_hub.worker import WorkerResult, _raise_worker_error
    from subzero.language import Language
    from subliminal_patch.core_persistent import list_all_subtitles_parallel
    from subliminal_patch.provider_health import ProviderHealthTracker

    started = threading.Event()
    outcomes = []
    health = ProviderHealthTracker()
    monkeypatch.setattr("subliminal_patch.provider_health.get_tracker", lambda: health)

    class RespondingWorker(FakeWorker):
        def request(self, op, payload, **kwargs):
            started.set()
            assert self.killed.wait(WAIT)
            if op == "download":
                return WorkerResult(ok=True, payload={}, events=[])
            _raise_worker_error({"code": "provider", "class_name": "RuntimeError", "message": "provider failed"})

    worker = RespondingWorker()

    class Plugin(HubProxyProvider):
        provider_name = BUILT_IN
        languages = {Language("eng")}

        def __init__(self):
            super().__init__(worker_client=worker)

    built_in, _ = _plugin_over_built_in(world, monkeypatch)
    world.registry.register(BUILT_IN, Plugin)
    pool = world.pool(BUILT_IN)
    pool.throttle_callback = get_providers.provider_throttle
    monkeypatch.setattr(detach, "REMOVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(get_providers, "get_providers_auth", lambda: {BUILT_IN: {}})
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "set_throttled_providers", lambda data: None)
    monkeypatch.setattr(get_providers, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [BUILT_IN])
    subtitle = HubWorkerSubtitle(BUILT_IN, BUILT_IN, "candidate", Language("eng"), {})
    if download:
        thread, result = _in_thread(pool.download_subtitle, subtitle)
    else:
        thread, result = _in_thread(list_all_subtitles_parallel, [_movie()], {Language("eng")}, pool,
                                    on_result=health.record, on_outcome=lambda outcome, elapsed: outcomes.append(outcome))

    try:
        assert started.wait(WAIT)
        service.remove_installation(BUILT_IN)
        thread.join(WAIT)
        assert detach.wait_idle(WAIT)
    finally:
        worker.killed.set()
        thread.join(WAIT)

    assert worker.cancels == 1
    assert world.registry[BUILT_IN] is built_in
    if download:
        assert result == {"value": False}
        assert isinstance(subtitle.download_error, WorkerProtocolError)
    else:
        assert [(result.status, result.reason) for result in outcomes] == [("error", "error")]
    assert BUILT_IN not in get_providers.tp
    assert BUILT_IN not in health.snapshot()
    assert BUILT_IN not in pool.discarded_providers


@pytest.mark.parametrize("listing", ["fanout", "fanout-abandoned", "prioritized", "sequential", "async"])
def test_a_search_of_a_still_cached_plugin_does_not_charge_the_current_built_in(world, monkeypatch, listing):
    from provider_hub import detach, registry, service
    from provider_hub.worker import WorkerError
    from subzero.language import Language
    from subliminal_patch.core import SZAsyncProviderPool
    from subliminal_patch.core_persistent import list_all_subtitles_parallel
    from subliminal_patch.provider_health import ProviderHealthTracker

    built_in, plugin = _plugin_over_built_in(world, monkeypatch)
    errors, outcomes, abandoned = [], [], []
    started, finish = threading.Event(), threading.Event()
    health = ProviderHealthTracker()
    monkeypatch.setattr("subliminal_patch.provider_health.get_tracker", lambda: health)
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {}})

    def fail(self, video, languages):
        errors.append(self)
        if listing == "fanout-abandoned":
            started.set()
            assert finish.wait(WAIT)
        raise WorkerError("cached plugin failed")

    def expire_after_attempt(futures, timeout):
        assert started.wait(WAIT)
        abandoned.extend(futures)
        raise TimeoutError

    if listing == "fanout-abandoned":
        monkeypatch.setattr("subliminal_patch.core_persistent.as_completed", expire_after_attempt)

    monkeypatch.setattr(plugin, "list_subtitles", fail)
    pool = SZAsyncProviderPool(providers=[BUILT_IN]) if listing == "async" else world.pool(BUILT_IN)
    old = pool[BUILT_IN]
    retire = registry.retire_provider_instances

    def search_before_retirement(name, *args, **kwargs):
        assert world.registry[BUILT_IN] is built_in
        assert pool.initialized_providers[BUILT_IN] is old
        video, languages = _movie(), {Language("eng")}
        if listing.startswith("fanout"):
            try:
                list_all_subtitles_parallel([video], languages, pool, on_result=health.record,
                                            on_outcome=lambda outcome, elapsed: outcomes.append(outcome))
            finally:
                finish.set()
            for future in abandoned:
                assert future.result(timeout=WAIT).status == "error"
        elif listing == "prioritized":
            assert pool.list_subtitles_prioritized(video, languages) == []
        else:
            assert pool.list_subtitles(video, languages) == []
        return retire(name, *args, **kwargs)

    monkeypatch.setattr(registry, "retire_provider_instances", search_before_retirement)
    service.remove_installation(BUILT_IN)

    assert detach.wait_idle(WAIT)
    assert errors == [old]
    if listing.startswith("fanout"):
        expected = "error" if listing == "fanout" else "abandoned"
        assert [outcome.status for outcome in outcomes] == [expected]
    assert BUILT_IN not in health.snapshot()
    assert BUILT_IN not in pool.discarded_providers


@pytest.mark.parametrize("during_retry", [False, True], ids=["before-lookup", "before-retry"])
def test_a_plugin_candidate_never_reaches_a_built_in_restored_during_download(world, monkeypatch, during_retry):
    from provider_hub import detach, registry, service
    from provider_hub.protocol import HubWorkerSubtitle
    from subzero.language import Language
    from subliminal_patch.core import ProviderRetiredError

    built_in, plugin = _plugin_over_built_in(world, monkeypatch)
    asked = []

    class Downloading(built_in):
        def download_subtitle(self, subtitle):
            asked.append(subtitle)
            raise AttributeError("a plugin candidate has no built-in download link")

    registry._SHADOWED_BUILT_INS[BUILT_IN] = Downloading
    pool = world.pool(BUILT_IN)
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {}})
    pool[BUILT_IN]

    def remove():
        service.remove_installation(BUILT_IN)
        assert detach.wait_idle(WAIT)

    if during_retry:
        def lost_worker(self, subtitle):
            remove()
            raise ProviderRetiredError(BUILT_IN)

        monkeypatch.setattr(plugin, "download_subtitle", lost_worker)
    else:
        pool.pre_download_hook = lambda subtitle: remove()

    subtitle = HubWorkerSubtitle(BUILT_IN, BUILT_IN, "candidate", Language("eng"), {})
    assert pool.download_subtitle(subtitle) is False
    assert world.registry[BUILT_IN] is Downloading
    assert asked == []
    assert subtitle.skip_reason == "provider_excluded"
    assert BUILT_IN not in pool.discarded_providers


@pytest.mark.parametrize("counted", [False, True])
def test_a_throttle_write_rechecks_ownership_after_classifying_the_error(world, monkeypatch, counted):
    from app import get_providers
    from provider_hub import detach, service
    from provider_hub.worker import WorkerError
    from subliminal_patch.exceptions import APIThrottled

    _, plugin = _plugin_over_built_in(world, monkeypatch)

    def fail(self, video, languages):
        if counted:
            raise APIThrottled(retry_after=1)
        raise WorkerError("provider failed")

    monkeypatch.setattr(plugin, "list_subtitles", fail)
    pool = world.pool(BUILT_IN)
    pool.throttle_callback = get_providers.provider_throttle
    monkeypatch.setattr(get_providers, "get_providers_auth", lambda: {BUILT_IN: {}})
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "throttle_count", {})
    monkeypatch.setattr(get_providers, "set_throttled_providers", lambda data: None)
    monkeypatch.setattr(get_providers, "event_stream", lambda **kwargs: None)
    monkeypatch.setattr(get_providers.settings.general, "enabled_providers", [BUILT_IN])
    classifying, write = threading.Event(), threading.Event()
    throttle_map = get_providers.provider_throttle_map

    def pause_before_write():
        classifying.set()
        assert write.wait(WAIT)
        return throttle_map()

    monkeypatch.setattr(get_providers, "provider_throttle_map", pause_before_write)
    thread, result = _in_thread(_search, pool, BUILT_IN)
    try:
        assert classifying.wait(WAIT)
        service.remove_installation(BUILT_IN)
        assert detach.wait_idle(WAIT)
    finally:
        write.set()
        thread.join(WAIT)

    assert result["value"].status == ("cooldown" if counted else "error")
    assert BUILT_IN not in get_providers.tp
    assert BUILT_IN not in get_providers.throttle_count


@pytest.mark.parametrize("cancel_timing", ["never", "before_eof", "after_eof", "exited_before_eof"])
def test_worker_eof_keeps_its_cause_until_the_request_consumes_it(monkeypatch, cancel_timing):
    from io import StringIO
    from types import SimpleNamespace

    from provider_hub import worker

    close_stdout, queued_eof, consume_eof = threading.Event(), threading.Event(), threading.Event()
    exited = threading.Event()

    class Pipe:
        def readline(self, size):
            assert close_stdout.wait(WAIT)
            return ""

    class ReplyQueue(worker._BoundedChunks):
        def get(self, *args, **kwargs):
            chunk = super().get(*args, **kwargs)
            queued_eof.set()
            assert consume_eof.wait(WAIT)
            return chunk

    client = worker.ProviderWorkerClient(["fake"], provider_id=PLUGIN)
    process = SimpleNamespace(stdin=StringIO(), stdout=Pipe(), stderr=None, pid=8888)
    monkeypatch.setattr(worker, "_BoundedChunks", ReplyQueue)
    monkeypatch.setattr(client, "_spawn", lambda env: process)

    def kill():
        exited.set()
        close_stdout.set()

    monkeypatch.setattr(client, "_has_exited", lambda process: exited.is_set())
    monkeypatch.setattr(client, "_kill_worker", kill)
    thread, result = _in_thread(client.request, "search")
    try:
        assert _wait_for(lambda: client.process is process)
        if cancel_timing == "before_eof":
            client.cancel()
        elif cancel_timing == "exited_before_eof":
            exited.set()
            client.cancel()
        else:
            exited.set()
            close_stdout.set()
        assert queued_eof.wait(WAIT)
        if cancel_timing == "after_eof":
            client.cancel()
    finally:
        close_stdout.set()
        consume_eof.set()
        thread.join(WAIT)

    error_type = worker.WorkerCancelled if cancel_timing == "before_eof" else worker.WorkerError
    assert type(result["error"]) is error_type


@pytest.mark.parametrize("detailed", [True, False])
def test_a_cut_search_does_not_mark_the_restored_built_in_as_failed(world, monkeypatch, detailed):
    from app import get_providers
    from provider_hub import detach, service
    from subzero.language import Language
    from subliminal_patch.core_persistent import list_all_subtitles_parallel
    from subliminal_patch.provider_health import ProviderHealthTracker

    _plugin_over_built_in(world, monkeypatch)
    gate, started = threading.Event(), threading.Event()
    _, worker = world.run(BUILT_IN, gate=gate, started=started)
    pool = world.pool(BUILT_IN)
    monkeypatch.setattr(detach, "REMOVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(get_providers, "get_providers_auth", lambda: {BUILT_IN: {}})
    monkeypatch.setattr(get_providers, "tp", {})
    monkeypatch.setattr(get_providers, "event_stream", lambda **kwargs: None)
    pool.throttle_callback = lambda name, error, **kw: get_providers.provider_throttle(name, error, wait=False, **kw)
    health = ProviderHealthTracker()
    monkeypatch.setattr("subliminal_patch.provider_health.get_tracker", lambda: health)
    outcomes = []
    on_outcome = (lambda result, elapsed: outcomes.append(result)) if detailed else None
    video = _movie()
    thread, result = _in_thread(list_all_subtitles_parallel, [video], {Language("eng")}, pool,
                                on_result=health.record, on_outcome=on_outcome)
    try:
        assert started.wait(WAIT)
        service.remove_installation(BUILT_IN)
        thread.join(WAIT)
        assert detach.wait_idle(WAIT)
    finally:
        gate.set()
        thread.join(WAIT)

    assert worker.cancels == 1
    assert result["value"].get(video, []) == []
    assert BUILT_IN not in get_providers.tp
    assert BUILT_IN not in health.snapshot()
    assert BUILT_IN not in pool.discarded_providers
    assert world.row(BUILT_IN) is None
    if detailed:
        assert [(result.status, result.reason) for result in outcomes] == [("skipped", "provider_excluded")]


def test_a_cut_download_does_not_throttle_or_discard_the_restored_built_in(world, monkeypatch):
    from provider_hub import detach, service

    _plugin_over_built_in(world, monkeypatch)
    gate, started = threading.Event(), threading.Event()
    _, worker = world.run(BUILT_IN, gate=gate, started=started)
    pool = world.pool(BUILT_IN)
    subtitle = _subtitle(BUILT_IN)
    errors = []
    pool.throttle_callback = lambda name, error, **kwargs: errors.append(error)
    monkeypatch.setattr(detach, "REMOVE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr("app.get_providers.get_providers_auth", lambda: {BUILT_IN: {}})

    thread, result = _in_thread(pool.download_subtitle, subtitle)
    try:
        assert started.wait(WAIT)
        service.remove_installation(BUILT_IN)
        thread.join(WAIT)
        assert detach.wait_idle(WAIT)
    finally:
        gate.set()
        thread.join(WAIT)

    assert worker.cancels == 1
    assert result == {"value": False}
    assert subtitle.skip_reason == "provider_excluded"
    assert errors == [] and BUILT_IN not in pool.discarded_providers


def test_the_refusal_of_a_detached_plugins_candidate_logs_from_a_realistic_subtitle_too(world, caplog):
    import logging

    from provider_hub.protocol import HubWorkerSubtitle
    from subzero.language import Language

    class BuiltIn:
        hub_proxy = False

    world.registry.register(PLUGIN, BuiltIn)
    pool = world.pool(PLUGIN)
    subtitle = HubWorkerSubtitle(PLUGIN, PLUGIN, "result", Language("eng"), {})

    with caplog.at_level(logging.INFO):
        assert pool.download_subtitle(subtitle) is False

    assert subtitle.skip_reason == "provider_excluded"
    assert any("no longer serves the plugin" in record.getMessage() and "alphahub:result" in record.getMessage()
               for record in caplog.records)

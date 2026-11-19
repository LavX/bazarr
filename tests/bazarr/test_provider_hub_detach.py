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
    from provider_hub.worker import WorkerError

    def wait_for_gate():
        if started is not None:
            started.set()
        if gate is None:
            return
        while not gate.is_set():
            if worker.killed.is_set():
                raise WorkerError("worker closed stdout")
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
    before = dict(provider_registry.providers)
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
    provider_registry.providers.clear()
    provider_registry.providers.update(before)
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


def _subtitle():
    from subzero.language import Language
    from subliminal_patch.subtitle import Subtitle

    class Downloadable(Subtitle):
        provider_name = PLUGIN

        def __init__(self):
            super().__init__(Language("eng"))
            self.provider_name = PLUGIN
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
    from provider_hub import registry, service

    revoked.install()
    worker = FakeWorker()
    revoked.run(worker=worker)
    registry._HOST_CALL_CLIENTS.setdefault(PLUGIN, __import__("weakref").WeakSet()).add(worker)
    _mark_revoked(revoked)

    # Trusted on the record, but revoked: a worker that is finishing its calls is trusted no more.
    from provider_hub.capabilities import live_trust

    assert live_trust(PLUGIN) is False
    registry.revoke_untrusted_host_calls()
    assert worker.host_calls_revoked == 1
    registry._HOST_CALL_CLIENTS.pop(PLUGIN, None)
    service.enforce_revocations_live()


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
    assert row["pending_restart"] is True and row["revoked_restart"] is True
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

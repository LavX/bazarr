# coding=utf-8
"""The "translation available" gate of the v2 runtime.

Every enqueue path asks whether a translation can run before it keeps a language away from the provider
search. With the runtime on, the answer is about the selected engine and nothing else, and it is closed when:

* no engine is selected, or the selected one is gone;
* the engine is not configured: its plugin is missing or off, it is off itself, it waits for its keys to be
  entered again, none of its back ends can be used, or (Lingarr) it has no address;
* a migrated engine is still waiting to be installed (``GateHolds.hold_for_install``);
* the engine runs in a worker and none is bound yet (``workers.bind``), which is the case until the translator
  worker is set up;
* the engine is paused behind the old sidecar's drain (``GateHolds.pause_for_drain``). Only a paused selected
  engine closes the gate: a paused engine that is merely the fallback is skipped for the jobs routed to it.

The two holds are state the migration and the drain driver set; this module only keeps and reads it. Both live
in memory: the driver sets them again at boot, so nothing needs a table.

Each closed answer carries a cause for the notice that names it. The gate never raises: a configuration that
cannot even be read is just another closed gate with the reason why.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

NO_ENGINE = "no_engine"
ENGINE_MISSING = "engine_missing"
AWAITING_INSTALL = "awaiting_install"
SIDECAR_DRAIN = "sidecar_drain"
NOT_CONFIGURED = "not_configured"
UNREADABLE = "unreadable"
WORKERS_UNBOUND = "workers_unbound"

REASONS = {
    NO_ENGINE: "No translator is configured",
    ENGINE_MISSING: "The selected translator no longer exists",
    AWAITING_INSTALL: "The selected translator is waiting to be installed",
    SIDECAR_DRAIN: "The selected translator is paused until the old translator service has stopped",
    WORKERS_UNBOUND: ("The selected translator is not available yet: the worker that runs translator plugins "
                      "is not set up in this build"),
}


@dataclass(frozen=True)
class GateResult:
    available: bool
    reason: str = ""
    cause: str = ""
    instance_id: int | None = None


class GateHolds:
    """Engines held back by something outside their own configuration. Thread safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._install: set[int] = set()
        self._drain: set[int] = set()

    def hold_for_install(self, instance_id: int) -> None:
        with self._lock:
            self._install.add(instance_id)

    def release_install(self, instance_id: int) -> None:
        with self._lock:
            self._install.discard(instance_id)

    def pause_for_drain(self, instance_id: int) -> None:
        with self._lock:
            self._drain.add(instance_id)

    def resume_after_drain(self, instance_id: int) -> None:
        with self._lock:
            self._drain.discard(instance_id)

    def awaiting_install(self, instance_id: int) -> bool:
        with self._lock:
            return instance_id in self._install

    def paused(self, instance_id: int) -> bool:
        with self._lock:
            return instance_id in self._drain

    def clear(self) -> None:
        with self._lock:
            self._install.clear()
            self._drain.clear()


HOLDS = GateHolds()


def why_not_configured(store, instance) -> str:
    """The reason an engine instance cannot take a job, in words a person can act on."""
    from . import legacy_engines

    if not instance.enabled:
        return "The selected translator is turned off"
    if instance.awaiting_secrets:
        return "The selected translator is waiting for its keys to be entered again"
    if legacy_engines.legacy_engine_for(instance.plugin_id) is not None:
        return "The Lingarr engine has no URL configured"
    if not store.plugin_active(instance.plugin_id):
        return "The selected translator's plugin is not installed or is turned off"
    return "None of the selected translator's back ends can be used right now"


def engine_state(store, instance, holds: GateHolds | None = None, workers=None) -> GateResult:
    """Whether one engine instance can take a job right now, and why not.

    ``workers`` is the worker port the dispatcher will use; the default is the bound one. An engine that runs
    in a worker is not available while none is bound, so no job is made that could only fail. An engine
    served by code in Bazarr+ (Lingarr) needs no worker.
    """
    from . import legacy_engines
    from . import workers as workers_module

    holds = holds or HOLDS
    if holds.awaiting_install(instance.id):
        return GateResult(False, REASONS[AWAITING_INSTALL], AWAITING_INSTALL, instance.id)
    if holds.paused(instance.id):
        return GateResult(False, REASONS[SIDECAR_DRAIN], SIDECAR_DRAIN, instance.id)
    if legacy_engines.legacy_engine_for(instance.plugin_id) is None and not workers_module.is_bound(workers):
        return GateResult(False, REASONS[WORKERS_UNBOUND], WORKERS_UNBOUND, instance.id)
    if not store.instance_configured(instance):
        return GateResult(False, why_not_configured(store, instance), NOT_CONFIGURED, instance.id)
    return GateResult(True, "", "", instance.id)


def evaluate(store=None, holds: GateHolds | None = None, workers=None) -> GateResult:
    """The gate for the selected engine. Never raises."""
    try:
        if store is None:
            from app.database import database

            from .store import TranslatorStore

            store = TranslatorStore(database)
        engine_id, _fallback = store.pointers()
        if engine_id is None:
            return GateResult(False, REASONS[NO_ENGINE], NO_ENGINE)
        instance = store.get_instance(engine_id, fresh=True)
        if instance is None:
            return GateResult(False, REASONS[ENGINE_MISSING], ENGINE_MISSING, engine_id)
        return engine_state(store, instance, holds, workers)
    except Exception as error:
        logger.error("BAZARR cannot read the translator configuration, so nothing can be translated: %s", error)
        return GateResult(False, f"The translator configuration cannot be read: {error}", UNREADABLE)


PICK_MISSING = "The back end that was picked no longer exists"
PICK_UNUSABLE = "The back end that was picked cannot be used right now"
PICK_ENGINE_MISSING = "The engine that was picked no longer exists"
PICK_ENGINE_HAS_BACKENDS = "The engine that was picked has back ends: pick one of them"


def pinned_gate(pin: int, store=None, holds: GateHolds | None = None, workers=None) -> GateResult:
    """The gate for a manual pick: one back end, or one engine with no back ends, whatever engine is selected.

    ``pin`` is what ``pick.pin_value`` made. It asks what the dispatcher asks of a pinned job. For a back end:
    the connection exists, its engine can take a job, and the connection itself is on, has its keys and is served
    by a plugin that runs. For an engine: it exists, can take a job and has no back ends, because a pick of an
    engine that has some says nothing about which to use. Never raises.
    """
    from ..pick import split_pin

    kind, target = split_pin(pin)
    try:
        if store is None:
            from app.database import database

            from .store import TranslatorStore

            store = TranslatorStore(database)
        if kind == "engine":
            instance = store.get_instance(target, fresh=True)
            if instance is None:
                return GateResult(False, PICK_ENGINE_MISSING, ENGINE_MISSING)
            if store.list_connections(instance.id):
                return GateResult(False, PICK_ENGINE_HAS_BACKENDS, NOT_CONFIGURED, instance.id)
            return engine_state(store, instance, holds, workers)
        connection = store.get_connection(target, fresh=True)
        instance = store.get_instance(connection.instance_id, fresh=True) if connection is not None else None
        if connection is None or instance is None:
            return GateResult(False, PICK_MISSING, ENGINE_MISSING)
        state = engine_state(store, instance, holds, workers)
        if not state.available:
            return state
        if not store.connection_usable(connection):
            return GateResult(False, PICK_UNUSABLE, NOT_CONFIGURED, instance.id)
        return GateResult(True, "", "", instance.id)
    except Exception as error:
        logger.error("BAZARR cannot read the translator configuration, so nothing can be translated: %s", error)
        return GateResult(False, f"The translator configuration cannot be read: {error}", UNREADABLE)


def translation_gate(store=None, holds: GateHolds | None = None, workers=None):
    """The gate as the old ``translation_available`` answers it: ``(available, reason)``."""
    from ..availability import TranslationAvailability

    result = evaluate(store, holds, workers)
    return TranslationAvailability(result.available, result.reason)

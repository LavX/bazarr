# coding=utf-8
"""How the dispatcher reaches a translator worker: the port, the pool adapter and the binding point.

The dispatcher only knows ``WorkerPort``: send one ``translate`` request to the worker of a plugin, get a
handle to wait on or cancel. ``PoolWorkers`` is the production port, built on the supervised worker pool and
protocol v2. Which process serves a plugin (its interpreter, bundle, folders and limits) is the binding's
business: it passes a ``definition_for`` that turns a plugin id into a ``WorkerDefinition``, and that
definition's ``host_calls`` should be ``host_calls.host_calls()`` so the worker can reserve, report usage and
checkpoint.

Until a binding exists the module answers with ``UnboundWorkers``: every request is refused as unavailable.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

TRANSLATE_OP = "translate"


class RunningCall(Protocol):
    def wait(self, timeout: float | None = None): ...

    def cancel(self, reason: str = "cancelled") -> None: ...


class WorkerPort(Protocol):
    def submit(self, plugin_id: str, request: dict, secrets: Mapping[str, str] | None, *, context: Any,
               on_progress: Callable[[dict], None] | None) -> RunningCall: ...

    def abandon(self, plugin_id: str) -> None:
        """Take the plugin's worker out of service once the jobs it holds are done: it ignored a cancel."""


class UnboundWorkers:
    """The port before anything is bound: no translator worker exists yet."""

    def submit(self, plugin_id, request, secrets, *, context, on_progress):
        from provider_hub.runtime.pool import PoolCodes, PoolUnavailable

        raise PoolUnavailable("no translator worker is bound for this plugin", code=PoolCodes.UNAVAILABLE)

    def abandon(self, plugin_id):
        return None


class PoolWorkers:
    """The worker pool as a ``WorkerPort``. One pool key per plugin id."""

    def __init__(self, pool, definition_for: Callable[[str], Any], *, key_for: Callable[[str], str] | None = None):
        self._pool = pool
        self._definition_for = definition_for
        self._key_for = key_for or (lambda plugin_id: plugin_id)
        self._lock = threading.Lock()

    def _ensure(self, plugin_id: str) -> str:
        from provider_hub.runtime.pool import PoolCodes, PoolUnavailable, UnknownWorker

        key = self._key_for(plugin_id)
        try:
            self._pool.definition(key)
            return key
        except UnknownWorker:
            pass
        with self._lock:
            try:
                self._pool.definition(key)
                return key
            except UnknownWorker:
                pass
            definition = self._definition_for(plugin_id)
            if definition is None:
                raise PoolUnavailable("no translator worker can be started for this plugin",
                                      code=PoolCodes.UNAVAILABLE)
            try:
                self._pool.register(definition)
            except ValueError:  # another thread registered it first
                pass
        return key

    def submit(self, plugin_id, request, secrets, *, context, on_progress):
        key = self._ensure(plugin_id)
        return self._pool.submit(key, TRANSLATE_OP, {"request": request}, secrets=dict(secrets) or None,
                                 on_progress=on_progress, context=context)

    def abandon(self, plugin_id):
        try:
            self._pool.restart_after_drain(self._key_for(plugin_id))
        except Exception:
            logger.warning("Could not retire the worker of translator %s", plugin_id, exc_info=True)


_port: WorkerPort = UnboundWorkers()
_port_lock = threading.Lock()


def bind(port: WorkerPort) -> None:
    """Bind the worker port the dispatcher uses by default."""
    global _port
    with _port_lock:
        _port = port


def unbind() -> None:
    bind(UnboundWorkers())


def bound_port() -> WorkerPort:
    with _port_lock:
        return _port

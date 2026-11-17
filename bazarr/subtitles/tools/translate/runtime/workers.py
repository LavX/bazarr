# coding=utf-8
"""How the dispatcher reaches a translator worker: the port, the pool adapter and the binding point.

The dispatcher only knows ``WorkerPort``: send one ``translate`` request to the worker of a plugin, get a
handle to wait on or cancel. ``PoolWorkers`` is the production port, built on the supervised worker pool and
protocol v2. Which process serves a plugin (its interpreter, bundle, folders and limits) is the binding's
business: it passes a ``definition_for`` that turns a plugin id into a ``WorkerDefinition``, and that
definition's ``host_calls`` should be ``host_calls.host_calls()`` so the worker can reserve, report usage and
checkpoint.

Until a binding exists the module answers with ``UnboundWorkers``: every request is refused as unavailable, and
the gate (``gate.engine_state``) reports an engine that needs a worker as not available, with that reason, so no
job is made only to fail. The binding is one call, made once when the translator worker is set up::

    from subtitles.tools.translate.runtime import workers
    workers.bind(workers.PoolWorkers(pool, definition_for))

``definition_for(plugin_id)`` returns the ``WorkerDefinition`` of that plugin's worker (its spawn or command, folders and
limits) with ``host_calls=host_calls.host_calls()``, or None when the plugin has no worker. ``workers.unbind()`` undoes it.

Two more hooks, both optional, are what ``worker_binding`` supplies for the real worker:

* ``ports_for(plugin_id)`` is the set of network ports the plugin's worker should be admitted with now. A worker is
  started with the set it had then, so when the set changes the port restarts the worker after the jobs it holds have
  finished (``WorkerPool.restart_after_drain``): nothing running is interrupted and a job that arrives meanwhile is
  served by the new worker.
* ``one_call_for(plugin_id, params)`` is the definition of a one-call worker for a ``test`` or ``options`` request.
  Those requests never go to the long-lived worker. A draft the person has not saved can name an address on a port the
  long-lived worker does not admit, and a request that outlives its deadline against a plugin that ignores the cancel
  ends the worker that holds it: on a worker shared with translations that would end them too. A worker started for
  the one call has nothing else to lose.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

TRANSLATE_OP = "translate"
# The pool key of the one-call worker of a plugin is its key with this added, so a one-call worker never shares a
# key (and so never shares a breaker, a teardown or a start) with the long-lived worker.
ONE_CALL_SUFFIX = ":call"
TEST_OP = "test"
OPTIONS_OP = "options"


class NotSent(Exception):
    """A short request was refused before anything left Bazarr+: no worker, too many one-call workers at once, a
    request the wire could not carry, or a worker that would not start. ``busy`` tells a refusal that only asks the
    caller to wait from one that is not going to change by waiting."""

    def __init__(self, message: str, *, busy: bool = False):
        super().__init__(message)
        self.busy = busy


class RunningCall(Protocol):
    def wait(self, timeout: float | None = None): ...

    def cancel(self, reason: str = "cancelled") -> None: ...


class WorkerPort(Protocol):
    def submit(self, plugin_id: str, request: dict, secrets: Mapping[str, str] | None, *, context: Any,
               on_progress: Callable[[dict], None] | None) -> RunningCall: ...

    def abandon(self, plugin_id: str) -> None:
        """Take the plugin's worker out of service once the jobs it holds are done: it ignored a cancel."""

    def call(self, plugin_id: str, op: str, params: dict, secrets: Mapping[str, str] | None, *,
             timeout: float) -> Any:
        """One short request (``test`` or ``options``), run in a worker of its own.

        Answered, or raised within ``timeout`` (and the time the worker is given to stop), or ``NotSent`` when it was
        refused before anything was sent. It returns or raises only once the request has really ended, and the worker
        that ran it is gone: nothing a translation depends on is ever stopped by a short request that overran.
        """


class UnboundWorkers:
    """The port before anything is bound: no translator worker exists yet."""

    def submit(self, plugin_id, request, secrets, *, context, on_progress):
        from provider_hub.runtime.pool import PoolCodes, PoolUnavailable

        raise PoolUnavailable("no translator worker is bound for this plugin", code=PoolCodes.UNAVAILABLE)

    def abandon(self, plugin_id):
        return None

    def call(self, plugin_id, op, params, secrets, *, timeout):
        raise NotSent("no translator worker is bound for this plugin")


class PoolWorkers:
    """The worker pool as a ``WorkerPort``. One pool key per plugin id."""

    def __init__(self, pool, definition_for: Callable[[str], Any], *, key_for: Callable[[str], str] | None = None,
                 one_call_for: Callable[[str, Mapping[str, Any]], Any] | None = None,
                 ports_for: Callable[[str], frozenset[int] | None] | None = None):
        self._pool = pool
        self._definition_for = definition_for
        self._key_for = key_for or (lambda plugin_id: plugin_id)
        self._one_call_for = one_call_for
        self._ports_for = ports_for
        self._lock = threading.Lock()
        # The port set each long-lived worker was last asked to run with, by pool key.
        self._ports: dict[str, frozenset[int] | None] = {}

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
            self._ports[key] = self._current_ports(plugin_id)
        return key

    def _current_ports(self, plugin_id: str) -> frozenset[int] | None:
        if self._ports_for is None:
            return None
        try:
            ports = self._ports_for(plugin_id)
        except Exception:
            logger.warning("The ports translator %s may reach could not be read", plugin_id, exc_info=True)
            return None
        return None if ports is None else frozenset(ports)

    def _follow_ports(self, plugin_id: str, key: str) -> None:
        """Retire the long-lived worker, once the jobs it holds are done, when the ports it admits are not the ports
        the plugin's connections now need.

        The next start reads the set afresh, so the worker that replaces it admits what is configured. Nothing that
        is running is interrupted. A set that could not be read changes nothing.
        """
        if self._ports_for is None:
            return
        wanted = self._current_ports(plugin_id)
        if wanted is None:
            return
        with self._lock:
            if key not in self._ports:
                self._ports[key] = wanted
                return
            if self._ports[key] == wanted:
                return
            self._ports[key] = wanted
        try:
            self._pool.restart_after_drain(key)
        except Exception:
            logger.warning("Could not restart the worker of translator %s for its new ports", plugin_id,
                           exc_info=True)

    def submit(self, plugin_id, request, secrets, *, context, on_progress):
        key = self._ensure(plugin_id)
        self._follow_ports(plugin_id, key)
        return self._pool.submit(key, TRANSLATE_OP, {"request": request}, secrets=dict(secrets) or None,
                                 on_progress=on_progress, context=context)

    def _one_call_definition(self, plugin_id: str, params: Mapping[str, Any]):
        from dataclasses import replace

        if self._one_call_for is not None:
            definition = self._one_call_for(plugin_id, params)
        else:
            definition = self._definition_for(plugin_id)
            if definition is not None:
                definition = replace(definition, key=self._key_for(plugin_id) + ONE_CALL_SUFFIX)
        if definition is None:
            raise NotSent("no translator worker can be started for this plugin")
        return definition

    def call(self, plugin_id, op, params, secrets, *, timeout):
        from provider_hub.runtime.errors import SessionBusy
        from provider_hub.runtime.frames import FrameError
        from provider_hub.runtime.pool import PoolCodes, PoolError, WorkerKilled

        try:
            definition = self._one_call_definition(plugin_id, params)
            return self._pool.run_once(definition, op, params, secrets=dict(secrets or {}) or None, timeout=timeout)
        except NotSent:
            raise
        except WorkerKilled:
            # Ended for breaking a limit: it may have been after the request was written, which a provider may
            # have acted on, so this is not a refusal.
            raise
        except (PoolError, SessionBusy) as error:
            # The pool turned the request away or the worker would not start: nothing was written.
            busy = isinstance(error, SessionBusy) or getattr(error, "code", None) == PoolCodes.POOL_FULL
            raise NotSent(f"the worker could not take the request ({type(error).__name__})", busy=busy) from None
        except (ValueError, FrameError) as error:
            # What the wire or the pool refuses before it writes anything: a timeout or a secret that is not
            # valid, params that cannot be encoded, a frame over its cap, a launch policy that was refused.
            raise NotSent(f"the request could not be sent ({type(error).__name__})") from None

    def retire(self, plugin_id):
        """Take the plugin's worker out of service once the jobs it holds are done. The next request starts a new
        one from what is installed and configured then. A plugin that never had a worker has nothing to retire."""
        from provider_hub.runtime.pool import UnknownWorker

        key = self._key_for(plugin_id)
        try:
            self._pool.definition(key)
        except UnknownWorker:
            return
        except Exception:
            logger.warning("Could not look up the worker of translator %s", plugin_id, exc_info=True)
            return
        try:
            self._pool.restart_after_drain(key)
        except Exception:
            logger.warning("Could not retire the worker of translator %s", plugin_id, exc_info=True)

    def abandon(self, plugin_id):
        self.retire(plugin_id)


_port: WorkerPort = UnboundWorkers()
_port_lock = threading.Lock()


def bind(port: WorkerPort) -> None:
    """Bind the worker port the dispatcher uses by default."""
    global _port
    with _port_lock:
        _port = port


def unbind() -> None:
    bind(UnboundWorkers())


def is_bound(port: WorkerPort | None = None) -> bool:
    """Whether a real worker port is bound (the given one, or the default)."""
    return not isinstance(port if port is not None else bound_port(), UnboundWorkers)


def bound_port() -> WorkerPort:
    with _port_lock:
        return _port

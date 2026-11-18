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
* ``one_call_for(plugin_id, params, call_id)`` is the definition of a one-call worker for a ``test`` or ``options`` request.
  Those requests never go to the long-lived worker. A draft the person has not saved can name an address on a port the
  long-lived worker does not admit, and a request that outlives its deadline against a plugin that ignores the cancel
  ends the worker that holds it: on a worker shared with translations that would end them too. A worker started for
  the one call has nothing else to lose. Each call has an id of its own, so its pool key and its writable folders are its
  own too: calls never refuse one another because an earlier worker is still being torn down, and what one writes counts
  against its own folder budget, not against a worker that holds translations. ``one_call_done(plugin_id, call_id)``,
  when given, is called once the call has ended, to clear what it left behind.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Callable, Mapping, Protocol

from secret_store.scrub import scrub_text

logger = logging.getLogger(__name__)

TRANSLATE_OP = "translate"
# The pool key of a one-call worker is the plugin's key with this and the call's id added, so a one-call worker never
# shares a key (and so never shares a breaker, a teardown or a start) with the long-lived worker or with another call.
ONE_CALL_SUFFIX = ":call"
TEST_OP = "test"
OPTIONS_OP = "options"


class NotSent(Exception):
    """A short request was refused before anything left Bazarr+: no worker, too many one-call workers at once, a
    request the wire could not carry, or a worker that would not start. ``busy`` tells a refusal that only asks the
    caller to wait from one that is not going to change by waiting."""

    def __init__(self, message: str, *, busy: bool = False, retry_after: float | None = None, code: str | None = None):
        super().__init__(message)
        self.busy = busy
        # How long until asking again can work, and why not now, when the pool knows.
        self.retry_after = retry_after
        self.code = code


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

    def runtime(self, plugin_id):
        return None


class PoolWorkers:
    """The worker pool as a ``WorkerPort``. One pool key per plugin id."""

    def __init__(self, pool, definition_for: Callable[[str], Any], *, key_for: Callable[[str], str] | None = None,
                 one_call_for: Callable[[str, Mapping[str, Any], str], Any] | None = None,
                 one_call_done: Callable[[str, str], None] | None = None,
                 ports_for: Callable[[str], frozenset[int] | None] | None = None):
        self._pool = pool
        self._definition_for = definition_for
        self._key_for = key_for or (lambda plugin_id: plugin_id)
        self._one_call_for = one_call_for
        self._one_call_done = one_call_done
        self._ports_for = ports_for
        self._lock = threading.Lock()
        # Short calls running now, by plugin: a plugin is not idle while one of them has a worker.
        self._calls: dict[str, int] = {}
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
        # The new set is recorded only once the worker has been retired, and both happen under the lock that every
        # submission takes first. A submission that finds the new set recorded therefore finds the retirement done,
        # and is served by a worker that is started with the new set, never by the old one.
        with self._lock:
            if key not in self._ports:
                self._ports[key] = wanted
                return
            if self._ports[key] == wanted:
                return
            try:
                self._pool.restart_after_drain(key)
            except Exception:
                logger.warning("Could not restart the worker of translator %s for its new ports", plugin_id,
                               exc_info=True)
                return  # not recorded: the next submission tries again
            self._ports[key] = wanted

    def submit(self, plugin_id, request, secrets, *, context, on_progress):
        key = self._ensure(plugin_id)
        self._follow_ports(plugin_id, key)
        return self._pool.submit(key, TRANSLATE_OP, {"request": request}, secrets=dict(secrets) or None,
                                 on_progress=on_progress, context=context)

    def _one_call_definition(self, plugin_id: str, params: Mapping[str, Any], call_id: str):
        from dataclasses import replace

        if self._one_call_for is not None:
            definition = self._one_call_for(plugin_id, params, call_id)
        else:
            definition = self._definition_for(plugin_id)
            if definition is not None:
                definition = replace(definition, key=f"{self._key_for(plugin_id)}{ONE_CALL_SUFFIX}:{call_id}")
        if definition is None:
            raise NotSent("no translator worker can be started for this plugin")
        return definition

    @staticmethod
    def _check_encodable(op: str, params: Mapping[str, Any], secrets: Mapping[str, str] | None,
                         timeout: float) -> None:
        """Refuse, as the wire would, what cannot be sent, before a worker is started for it.

        Raises ``ValueError`` or ``FrameError`` for a timeout or a secret that is not valid, params that cannot be
        encoded, or a frame over its cap. Whatever is raised after this passes happened with the request written or
        about to be.
        """
        import math

        from provider_hub.runtime import frames

        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or timeout < 0):
            raise ValueError("timeout must be a finite number of seconds, not negative")
        if secrets and not frames.is_secrets(secrets):
            raise ValueError("secrets must be a flat mapping of text names to non-empty text values")
        # The frame the session writes: its deadline in milliseconds and an id as long as any it hands out.
        frames.pack_control({"t": frames.REQ, "id": 2 ** 53, "op": op, "params": params or {},
                             "max_result_bytes": frames.DEFAULT_MAX_RESULT_BYTES, "deadline_ms": int(timeout * 1000),
                             "secrets": dict(secrets or {})}, sender="host")

    def call(self, plugin_id, op, params, secrets, *, timeout):
        from provider_hub.runtime.errors import SessionBusy
        from provider_hub.runtime.frames import FrameError
        from provider_hub.runtime.pool import PoolCodes, PoolError, WorkerKilled

        call_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._calls[plugin_id] = self._calls.get(plugin_id, 0) + 1
        try:
            # Everything that can refuse the request before a worker exists is settled here, so that what is raised
            # once a worker has the request is never taken for a refusal.
            try:
                definition = self._one_call_definition(plugin_id, params, call_id)
                self._check_encodable(op, params, secrets, timeout)
            except NotSent:
                raise
            except (ValueError, FrameError, OSError, LookupError) as error:
                # A timeout or secret that is not valid, params that cannot be encoded, a frame over its cap, a
                # launch policy or a folder that was refused: all of it before any worker exists.
                raise NotSent(f"the request could not be sent ({type(error).__name__})") from None
            try:
                return self._pool.run_once(definition, op, params, secrets=dict(secrets or {}) or None,
                                           timeout=timeout)
            except WorkerKilled as error:
                if error.before_send:
                    # Over its memory limit as it started, before it was given anything.
                    raise NotSent("the worker was over its limits as it started") from None
                # Ended for breaking a limit after the request was written, which a provider may have acted on:
                # not a refusal.
                raise
            except (PoolError, SessionBusy) as error:
                # The pool turned the request away or the worker would not start: nothing was written.
                busy = isinstance(error, SessionBusy) or getattr(error, "code", None) == PoolCodes.POOL_FULL
                raise NotSent(f"the worker could not take the request ({type(error).__name__})", busy=busy,
                              retry_after=getattr(error, "retry_after", None), code=getattr(error, "code", None)) from None
        finally:
            with self._lock:
                left = self._calls.get(plugin_id, 0) - 1
                if left > 0:
                    self._calls[plugin_id] = left
                else:
                    self._calls.pop(plugin_id, None)
            if self._one_call_done is not None:
                try:
                    self._one_call_done(plugin_id, call_id)
                except Exception:
                    logger.warning("Could not clear what a short request to translator %s left", plugin_id,
                                   exc_info=True)

    def idle(self, plugin_id) -> bool:
        """Whether nothing of the plugin is running: no short call has a worker, and the long-lived worker is not
        there, or is stopped, or is waiting out a failure with no process."""
        from provider_hub.runtime.pool import UnknownWorker, WorkerState

        key = self._key_for(plugin_id)
        with self._lock:
            if self._calls.get(plugin_id):
                return False
        try:
            state = self._pool.state(key)
        except UnknownWorker:
            return self._pool.teardown_confirmed(key)
        if state not in (WorkerState.STOPPED, WorkerState.BACKOFF, WorkerState.BROKEN):
            return False
        # An entry in one of those states can still have a worker that is draining or whose end is not confirmed yet.
        return self._pool.teardown_confirmed(key) and not self._pool.status()["workers"][key]["retiring"]

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

    def runtime(self, plugin_id: str) -> dict | None:
        """How the plugin's long-lived worker is doing, for a card. None when it never had one."""
        worker = self._worker_status(plugin_id)
        return None if worker is None else runtime_summary(worker)

    def stderr_tail(self, plugin_id: str) -> list[str]:
        """The recent stderr lines of the plugin's long-lived worker, scrubbed by its session."""
        worker = self._worker_status(plugin_id)
        return [str(line) for line in (worker or {}).get("recent_stderr") or []]

    def _worker_status(self, plugin_id: str) -> dict | None:
        try:
            return self._pool.status()["workers"].get(self._key_for(plugin_id))
        except Exception:
            logger.warning("Could not read the state of the worker of translator %s", plugin_id, exc_info=True)
            return None


_PHASE_OF_STATE = {
    "starting": "starting",
    "backoff": "backing_off",
    "broken": "broken",
    "draining": "stopping",
    "stopping": "stopping",
    "stopped": "stopped",
}


def runtime_summary(worker: Mapping[str, Any]) -> dict:
    """What a pool entry says about a worker, in the words a card uses.

    ``phase`` is one of starting, healthy (running a request now), idle (up and waiting), backing_off (the
    last start or run failed and requests wait out a delay), broken, stopping and stopped. The stderr tail is
    not part of it: it is read on its own, for the log.
    """
    state = str(worker.get("state") or "stopped")
    in_flight = int(worker.get("in_flight") or 0)
    if state in ("ready", "busy"):
        phase = "healthy" if in_flight > 0 or state == "busy" else "idle"
    else:
        phase = _PHASE_OF_STATE.get(state, "stopped")
    return {
        "phase": phase,
        "in_flight": in_flight,
        "retry_after": worker.get("retry_after"),
        "last_error": scrub_text(str(worker["last_error"])) if worker.get("last_error") else None,
        "starts": int(worker.get("starts") or 0),
        "crashes": int(worker.get("crashes") or 0),
        "memory_bytes": int(worker.get("memory_bytes") or 0),
    }


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


def runtime_of(plugin_id: str) -> dict | None:
    """The state of the plugin's worker under the bound port, or None while nothing is bound or it never ran."""
    reader = getattr(bound_port(), "runtime", None)
    return reader(plugin_id) if callable(reader) else None


def stderr_tail_of(plugin_id: str) -> list[str]:
    """The recent stderr lines of the plugin's worker under the bound port, or none while nothing is bound."""
    reader = getattr(bound_port(), "stderr_tail", None)
    return reader(plugin_id) if callable(reader) else []

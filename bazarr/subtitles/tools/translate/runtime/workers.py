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
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

TRANSLATE_OP = "translate"
# How long a request that was told to stop is waited for: the session gives a worker its cancel grace and then
# its kill grace, so a request that is still running after this is a worker that cannot be stopped.
STOP_SETTLE_SECONDS = 60.0
TEST_OP = "test"
OPTIONS_OP = "options"


class NotSent(Exception):
    """A short request was refused before anything left Bazarr+: no worker, a worker busy with a translation, or
    one that would not start. ``busy`` tells a worker that is only occupied from one that is not there."""

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
        """One short request (``test`` or ``options``) to the plugin's worker.

        Answered, or raised within ``timeout``, or ``NotSent`` when it was refused before anything was sent.
        It returns or raises only once the request has really ended: one that ran out of time is cancelled and
        waited for, so a caller that treats the return as "no longer running" is right.
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

    def call(self, plugin_id, op, params, secrets, *, timeout):
        from provider_hub.runtime.errors import SessionBusy, SessionError
        from provider_hub.runtime.pool import PoolError

        try:
            key = self._ensure(plugin_id)
            handle = self._pool.submit(key, op, params, secrets=dict(secrets or {}) or None, timeout=timeout)
        except SessionBusy:
            raise NotSent("the worker is busy", busy=True) from None
        except (PoolError, SessionError) as error:
            # Refused or failed to start, before a request was written.
            raise NotSent(f"the worker could not take the request ({type(error).__name__})") from None
        try:
            return handle.wait(timeout + 2)
        except BaseException:
            # Out of time, or the caller was interrupted: stop the request and wait until the session confirms it
            # is gone, so nothing is still running when the caller lets go of what it held.
            handle.cancel("timeout")
            self._settle(plugin_id, handle)
            raise

    def _settle(self, plugin_id, handle) -> None:
        deadline = time.monotonic() + STOP_SETTLE_SECONDS
        while time.monotonic() < deadline:
            try:
                handle.wait(1.0)
            except TimeoutError:
                if handle.done():
                    return
                continue
            except BaseException:
                return  # the request ended, cancelled or failed
            return
        logger.warning("A request to translator %s did not stop when cancelled", plugin_id)
        self.abandon(plugin_id)

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


def is_bound(port: WorkerPort | None = None) -> bool:
    """Whether a real worker port is bound (the given one, or the default)."""
    return not isinstance(port if port is not None else bound_port(), UnboundWorkers)


def bound_port() -> WorkerPort:
    with _port_lock:
        return _port

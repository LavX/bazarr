"""The worker side: serves one plugin over the Bazarr+ worker protocol.

Start a worker with ``python -I <sdk>/bazarr_plugin_sdk/_bootstrap.py --entry
package.module:Class [--path DIR ...]``. The runner builds a
:class:`~bazarr_plugin_sdk._protocol.worker_channel.WorkerChannel` and gives it
a handler that maps each request to a method of the plugin:

* the operation name is the method (``describe``, ``translate``, ...), the
  request params are its arguments, the request secrets go where the kind says;
* an ``async def`` method runs on an event loop of its own on the request's
  thread, and a cancel both cancels its task and sets ``ctx.cancelled``; a
  plain method runs on that thread directly and reads ``ctx.is_cancelled``;
* the result is a JSON object. A result that is not an object travels as
  ``{"value": ...}``. One larger than :data:`INLINE_RESULT_BYTES` travels as
  data frames, with ``{"json_in_data": true}`` as its inline part and the
  UTF-8 JSON of the real result as the data;
* a :class:`~bazarr_plugin_sdk.errors.PluginError` becomes a failed response
  that carries its code, retry time, field errors and reason.

The channel keeps the original stdin and stdout for frames and points fd 1 at
stderr and fd 0 at the null device before any plugin code runs.

Exit codes: 0 normal, 3 the host broke the protocol or refused the worker, 64
bad command line or entry, 66 the plugin would not start.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import inspect
import logging
import os
import re
import sys
import threading
import traceback

from collections.abc import Callable
from typing import Any, BinaryIO, Sequence

from . import __version__
from ._protocol import frames
from ._protocol.worker_channel import ChannelClosed, Operation, OperationCancelled, OperationError, OperationResult, WorkerChannel
from .context import PluginContext
from .errors import INTERNAL, PluginError
from ._grace import Grace
from ._scrub import held_secrets, scrub
from .kinds import KindSpec, MethodSpec, get_kind

EXIT_OK = 0
EXIT_PROTOCOL = 3
EXIT_USAGE = 64
EXIT_START = 66

# A result whose JSON is longer than this travels as data frames.
INLINE_RESULT_BYTES = 512 * 1024
# Seconds a finished request's tasks and executor threads get to wind down.
SHUTDOWN_TIMEOUT = 5.0
# Extra seconds the reply waits for the cleanup thread beyond the budget its steps keep to.
CLEANUP_SLACK = 0.25
# A request param the SDK takes for itself: what a paid ``test`` may spend.
BUDGET_PARAM = "budget"

ENTRY_RE = re.compile(r"^(?P<module>[A-Za-z_][\w.]*):(?P<name>[A-Za-z_]\w*)$")

log = logging.getLogger("bazarr_plugin_sdk.runner")


class SdkChannel(WorkerChannel):
    """The reference channel, with failed responses that keep a plugin error's parts."""

    def _respond_error(self, req_id: int, error: BaseException) -> None:
        if isinstance(error, PluginError):
            # Every secret the host has sent this worker, from any request and in every
            # escaped spelling, goes out of the text before it is cut to size.
            secrets = held_secrets(self)

            def scrub_text(text: str) -> str:
                return scrub(text, secrets)

            try:
                payload = error.to_dict(scrub_text)
                self._send({"t": frames.RES, "id": req_id, "ok": False, "error": payload})
                return
            except ChannelClosed:
                raise
            except Exception:
                # An error that cannot be built or encoded gets the base class's fixed reply.
                pass
        super()._respond_error(req_id, error)


class PluginWorker:
    """Serves one plugin instance. Build it, then call :meth:`run`."""

    def __init__(
        self,
        plugin: Any,
        spec: KindSpec,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        redirect_stdout: bool = True,
        heartbeat_interval: float | None = None,
        wait_grace: float = 5.0,
    ) -> None:
        self.plugin = plugin
        self.spec = spec
        kinds = [get_kind(api).offer() for api in plugin.kind_apis if get_kind(api) is not None]  # type: ignore[union-attr]
        declared = getattr(plugin, "max_in_flight", 1)
        self.channel = SdkChannel(
            self.handle,
            kinds=kinds,
            max_in_flight=declared if isinstance(declared, int) and declared > 0 else 1,
            worker={"name": f"{type(plugin).__module__}.{type(plugin).__qualname__}", "sdk_version": __version__},
            stdin=stdin,
            stdout=stdout,
            redirect_stdout=redirect_stdout,
            heartbeat_interval=heartbeat_interval,
            wait_grace=wait_grace,
        )

    def run(self) -> int:
        return self.channel.run()

    # request handling

    def handle(self, operation: Operation) -> dict[str, Any] | OperationResult:
        method = self.spec.methods.get(operation.op)
        if method is None:
            raise OperationError(frames.Codes.UNKNOWN_OP, f"no operation named {operation.op}")
        params = dict(operation.params)
        budget = params.pop(BUDGET_PARAM, None)
        if set(params) != set(method.params):
            raise OperationError(frames.Codes.INVALID_REQUEST, f"{method.name} takes exactly {list(method.params)}")
        if method.embed_secrets_in and not isinstance(params[method.embed_secrets_in], dict):
            raise OperationError(frames.Codes.INVALID_REQUEST, f"{method.embed_secrets_in} must be an object")
        grace = Grace(SHUTDOWN_TIMEOUT)
        ctx = PluginContext(
            operation,
            grace=grace,
            kind_api=self.spec.kind_api,
            heartbeat_interval=self.channel._heartbeat_interval or frames.DEFAULT_HEARTBEAT_INTERVAL,
            budget=float(budget) if isinstance(budget, (int, float)) and not isinstance(budget, bool) else None,
        )
        func = getattr(self.plugin, method.name)
        kwargs = self._kwargs(method, func, params, operation.secrets, ctx)
        if inspect.iscoroutinefunction(func):
            value = _run_coroutine(operation, ctx, func(**kwargs), grace)
        else:
            value = call_bounded(operation, grace, func, kwargs)
            if inspect.isawaitable(value):
                value = _run_coroutine(operation, ctx, value, grace)
        if operation.cancelled.is_set():
            raise OperationCancelled(operation.cancel_reason or "cancelled")
        return pack_result(value)

    @staticmethod
    def _kwargs(method: MethodSpec, func: Any, params: dict[str, Any], secrets: dict[str, Any], ctx: PluginContext) -> dict[str, Any]:
        kwargs = dict(params)
        if method.secrets:
            kwargs["secrets"] = dict(secrets)
        if method.embed_secrets_in:
            kwargs[method.embed_secrets_in] = {**kwargs[method.embed_secrets_in], "secrets": dict(secrets)}
        parameters = inspect.signature(func).parameters
        if "ctx" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            kwargs["ctx"] = ctx
        return kwargs


def pack_result(value: Any) -> dict[str, Any] | OperationResult:
    """The response for a method's return value."""
    payload = value if isinstance(value, dict) else {"value": value}
    try:
        encoded = frames.encode_control(payload)
        if len(encoded) <= INLINE_RESULT_BYTES:
            return payload
        # The data is the JSON of the real result, a list or a scalar as it is: the
        # ``{"value": ...}`` wrapper is only for a result that travels inline.
        data = encoded if isinstance(value, dict) else frames.encode_control(value)
    except (TypeError, ValueError) as error:
        raise PluginError(INTERNAL, f"the result is not JSON: {error}", reason="result_not_json") from None
    return OperationResult({"json_in_data": True}, data)


def _supervise(operation: Operation, grace: Grace, finished: threading.Event, on_cancel: Callable[[], None]) -> bool:
    """Wait for ``finished``. A cancel starts the grace clock; a request still running when it
    runs out is given up on. Returns False for a request that was given up on."""
    sent = False
    while not finished.wait(0.02):
        if operation.cancelled.is_set():
            if not sent:
                sent = True
                grace.start()
                on_cancel()
            if grace.expired():
                return False
    return True


def _abandoned(operation: Operation) -> OperationCancelled:
    log.warning("the request did not stop within %.1f seconds of its cancel and was abandoned", SHUTDOWN_TIMEOUT)
    return OperationCancelled(operation.cancel_reason or "cancelled")


def call_bounded(operation: Operation, grace: Grace, func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    """Run a plain method on a thread of its own. After a cancel it has the grace period to
    return; then the reply goes out without it and the thread is left behind."""
    box: dict[str, Any] = {}
    finished = threading.Event()

    def run() -> None:
        try:
            box["value"] = func(**kwargs)
        except BaseException as error:  # handed to the request thread, which re-raises it
            box["error"] = error
        finally:
            finished.set()

    threading.Thread(target=run, name="bazarr-plugin-call", daemon=True).start()
    if not _supervise(operation, grace, finished, lambda: None):
        raise _abandoned(operation)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _run_coroutine(operation: Operation, ctx: PluginContext | None, awaitable: Any, grace: Grace | None = None) -> Any:
    """Run ``awaitable`` on a private event loop, on a thread of its own, and bound all of it.

    A cancel cancels its task and sets ``ctx.cancelled``. From a cancel, or from the
    moment the request ends, everything left to do (cancelling leftover tasks, closing
    async generators, joining executor threads, closing the loop) shares one
    :class:`Grace` budget. A request that ignores its cancel, a stalled cleanup step and
    a plugin that blocks the loop with a plain call all end with the reply going out when
    the budget is spent; what overran is logged and abandoned.
    """
    grace = grace or Grace(SHUTDOWN_TIMEOUT)
    loop = asyncio.new_event_loop()

    async def wrap() -> Any:
        return await awaitable

    task = loop.create_task(wrap())
    box: dict[str, Any] = {}
    finished = threading.Event()
    cleaned = threading.Event()

    def run() -> None:
        try:
            try:
                box["value"] = loop.run_until_complete(task)
            except BaseException as error:  # includes CancelledError; handed to the request thread
                box["error"] = error
            finished.set()
            grace.start()
            _clean_up(loop, grace)
        finally:
            finished.set()
            cleaned.set()

    def on_cancel() -> None:
        try:
            if ctx is not None:
                loop.call_soon_threadsafe(ctx.cancelled.set)
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # the loop is already closed
            pass

    threading.Thread(target=run, name="bazarr-plugin-loop", daemon=True).start()
    if not _supervise(operation, grace, finished, on_cancel):
        raise _abandoned(operation)
    grace.start()
    # A little over the budget: the steps time themselves out first, this is the backstop for
    # a step that blocks the loop thread where no timeout can reach it.
    if not cleaned.wait(grace.remaining() + CLEANUP_SLACK):
        log.warning("cleanup after the request did not finish within %.1f seconds and was abandoned", SHUTDOWN_TIMEOUT)
    error = box.get("error")
    if isinstance(error, asyncio.CancelledError):
        raise OperationCancelled(operation.cancel_reason or "cancelled") from None
    if error is not None:
        raise error
    return box["value"]


def _clean_up(loop: asyncio.AbstractEventLoop, grace: Grace) -> None:
    """Every step after the request, each within what is left of the shared budget.

    The loop is closed only when every step finished; after an overrun something may
    still be using it.
    """
    steps = (_cancel_remaining_tasks, _shutdown_asyncgens, _shutdown_executor)
    clean = True
    for step in steps:
        try:
            clean = step(loop, grace) and clean
        except Exception:
            log.warning("a cleanup step failed", exc_info=True)
            clean = False
    if clean:
        try:
            loop.close()
        except Exception:
            log.warning("closing the event loop failed", exc_info=True)


def _cancel_remaining_tasks(loop: asyncio.AbstractEventLoop, grace: Grace) -> bool:
    """Cancel what the request left running and let it clean up, as ``asyncio.run`` does."""
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not pending:
        return True
    for task in pending:
        task.cancel()
    # Not wait_for: that waits for a cancelled task to finish, and a task that swallows
    # the cancel never does. Give them what is left of the budget, then leave the rest.
    done, still_running = loop.run_until_complete(asyncio.wait(pending, timeout=grace.remaining()))
    for task in still_running:
        log.warning("a task started by the request ignored its cancel and was abandoned: %r", task)
    for task in done:
        if not task.cancelled() and task.exception() is not None:
            loop.call_exception_handler(
                {"message": "unhandled exception during request shutdown", "exception": task.exception(), "task": task}
            )
    return not still_running


def _shutdown_asyncgens(loop: asyncio.AbstractEventLoop, grace: Grace) -> bool:
    """Close the async generators the request left open; one that stalls in its ``finally`` is abandoned."""
    closing = loop.create_task(loop.shutdown_asyncgens())
    _, still_running = loop.run_until_complete(asyncio.wait({closing}, timeout=grace.remaining()))
    if still_running:
        log.warning("an async generator left open by the request did not close in time and was abandoned")
        return False
    return True


def _shutdown_executor(loop: asyncio.AbstractEventLoop, grace: Grace) -> bool:
    """Wait for the loop's executor threads, but only as long as the budget allows.

    ``shutdown_default_executor`` cannot be bounded from the loop on Python 3.10 and
    3.11: cancelling it still joins the threads without a limit. It runs on a helper
    thread here. Returns False when it did not finish; that thread is then still using
    the loop.
    """

    def run() -> None:
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except BaseException:  # nothing to report: the request is over
            log.debug("the executor shutdown failed", exc_info=True)

    helper = threading.Thread(target=run, name="bazarr-plugin-executor-shutdown", daemon=True)
    helper.start()
    helper.join(grace.remaining())
    if helper.is_alive():
        log.warning("a thread started by the request did not finish in time and was abandoned")
        return False
    return True


# Process entry

def load_plugin(entry: str | None, paths: Sequence[str]) -> tuple[Any, KindSpec]:
    """Import ``module:Class``, build it without arguments and find its kind."""
    entry = entry or os.environ.get("BAZARR_PLUGIN_ENTRY", "")
    match = ENTRY_RE.match(entry)
    if match is None:
        raise PluginError(INTERNAL, f"entry {entry!r} is not module:Class", reason="entry")
    for path in reversed(list(paths)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        module = importlib.import_module(match.group("module"))
        cls = getattr(module, match.group("name"))
    except (ImportError, AttributeError) as error:
        raise PluginError(INTERNAL, f"cannot load {entry}: {error}", reason="entry") from None
    return build_plugin(cls)


def build_plugin(cls: type) -> tuple[Any, KindSpec]:
    served = getattr(cls, "kind_apis", None)
    spec = None
    if isinstance(cls, type) and served:
        for api in served:
            candidate = get_kind(api)
            if candidate is not None and issubclass(cls, candidate.base_class):
                spec = candidate
                break
    if spec is None:
        raise PluginError(INTERNAL, f"{cls!r} is not a plugin of a kind this SDK serves", reason="entry")
    return cls(), spec


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bazarr_plugin_sdk.runner", description="Serve a Bazarr+ plugin over stdio frames.")
    parser.add_argument("--entry", help="module:Class of the plugin (default: BAZARR_PLUGIN_ENTRY)")
    parser.add_argument("--path", action="append", default=[], help="directory to add to sys.path, repeatable")
    # --help writes to stdout, which is still the frame pipe here: send it to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a worker in this process and return its exit code. :func:`run_process` also ends the process."""
    try:
        args = parse_args(argv)
    except SystemExit as stop:
        # argparse exits with 2 for a bad command line and 0 for --help.
        return EXIT_OK if stop.code == 0 else EXIT_USAGE
    paths = list(args.path)
    extra = os.environ.get("BAZARR_PLUGIN_PATH")
    if extra:
        paths.extend(p for p in extra.split(os.pathsep) if p)
    # Take the frame pipe before any plugin code is imported: from here on a print()
    # in a module body or a constructor goes to stderr.
    stdin, stdout = SdkChannel._take_stdio(True)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        plugin, spec = load_plugin(args.entry, paths)
    except PluginError as error:
        print(f"cannot start: {error.message}", file=sys.stderr, flush=True)
        return EXIT_USAGE
    except Exception:
        traceback.print_exc()
        return EXIT_START
    return PluginWorker(plugin, spec, stdin=stdin, stdout=stdout, redirect_stdout=False).run()


def run_process(argv: Sequence[str] | None = None) -> None:
    """Run a worker and end the process, so a plugin thread cannot keep a finished worker alive."""
    code = main(argv)
    grace = Grace(SHUTDOWN_TIMEOUT)
    grace.start()
    for stream in (sys.stdout, sys.stderr):
        _flush(stream, grace)
    os._exit(code)


def _flush(stream: Any, grace: Grace) -> None:
    """Flush a stream, waiting only as long as the budget allows: a full pipe nobody reads blocks."""

    def run() -> None:
        try:
            stream.flush()
        except (OSError, ValueError):
            pass

    flusher = threading.Thread(target=run, name="bazarr-plugin-flush", daemon=True)
    flusher.start()
    flusher.join(grace.remaining())


if __name__ == "__main__":
    run_process()

"""The object a plugin method receives for one request.

Everything a plugin reports to the host goes through it: progress, a
keep-alive that says the request is still making headway, holds on spending,
usage, checkpoints. The host calls (:meth:`reserve`, :meth:`usage`,
:meth:`checkpoint`) return only once the host has stored the record, so a
plugin can send a paid request knowing the hold exists.

Each of the three has two forms with the same behavior: a coroutine for
``async def`` methods, and a ``_sync`` twin for plain methods, which the
runner executes on a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time

from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from ._protocol.worker_channel import HostCallFailed, Operation
from .errors import CANCELLED, ERROR_CODES, INTERNAL, PluginError

T = TypeVar("T")

# Where the launcher puts a plugin's folders. The host sets these in the worker's environment.
ENV_DATA_DIR = "BAZARR_PLUGIN_DATA_DIR"
ENV_TEMP_DIR = "BAZARR_PLUGIN_TEMP_DIR"
ENV_ARTIFACTS_DIR = "BAZARR_PLUGIN_ARTIFACTS_DIR"

# Progress reports closer together than this are dropped, except the final one.
PROGRESS_MIN_INTERVAL = 0.1

# Seconds a host call waits for its acknowledgement.
HOST_CALL_TIMEOUT = 60.0


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


class PluginContext:
    """Per-request context. The runner builds it; a plugin never constructs one."""

    def __init__(
        self,
        operation: Operation,
        *,
        kind_api: str,
        heartbeat_interval: float,
        budget: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.operation = operation
        self.op = operation.op
        self.kind_api = kind_api
        # Seconds between keep-alives the host expects while a request makes progress.
        self.heartbeat_interval = heartbeat_interval
        # What a paid ``test`` may spend. A translate request's budget travels in the request.
        self.budget = budget
        self.data_dir = _env_path(ENV_DATA_DIR)
        self.temp_dir = _env_path(ENV_TEMP_DIR)
        self.artifacts_dir = _env_path(ENV_ARTIFACTS_DIR)
        self.logger = logger or logging.getLogger("bazarr_plugin_sdk.plugin")
        # Set when the host cancels this request. For ``async def`` methods only;
        # plain methods read ``is_cancelled``.
        self.cancelled = asyncio.Event()
        self._last_progress = 0.0

    # cancel

    @property
    def is_cancelled(self) -> bool:
        """True once the host has cancelled this request. Safe to read from any thread."""
        return self.operation.cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise PluginError(CANCELLED, "the host cancelled this request")

    # one-way reports

    def progress(self, done: int, total: int, message: str = "") -> None:
        """Report how far the request is. Dropped after a cancel. Thread-safe."""
        if self.is_cancelled:
            return
        total = max(int(total), 0)
        done = int(done)
        now = time.monotonic()
        if done < total and now - self._last_progress < PROGRESS_MIN_INTERVAL:
            return
        self._last_progress = now
        fraction = min(1.0, done / total) if total else None
        self.operation.progress(fraction=fraction, message=str(message)[:500] or None, data={"done": done, "total": total})

    def heartbeat(self) -> None:
        """Say the request is alive and making headway. Call it at least once per interval."""
        if self.is_cancelled:
            return
        self.operation.progress()

    # host calls, blocking form

    def reserve_sync(self, amount: float, model: str) -> str | None:
        """Record the worst-case charge of a paid call before it is sent.

        Returns the hold's id once the host has stored it. After the host has
        cancelled the request this raises ``PluginError("cancelled")``, so a
        cancelled request starts no new spending.
        """
        self.raise_if_cancelled()
        result = self._call("reserve", {"amount": _money(amount, "amount"), "model": str(model)})
        hold = result.get("hold")
        return hold if isinstance(hold, str) else None

    def usage_sync(self, tokens: int, cost: float, model: str, reservations: Sequence[str] = ()) -> None:
        """Settle ``reservations`` against what a call actually used.

        Still allowed after a cancel: a call already sent has spent its money
        and the host needs the true figure.
        """
        self._call(
            "usage",
            {
                "tokens": int(tokens),
                "cost": _money(cost, "cost"),
                "model": str(model),
                "reservations": [str(r) for r in reservations],
            },
        )

    def checkpoint_sync(self, data: dict[str, Any]) -> None:
        """Store resumable state. Returns once the host has stored it."""
        self.raise_if_cancelled()
        self._call("checkpoint", {"data": data})

    # host calls, awaitable form

    async def reserve(self, amount: float, model: str) -> str | None:
        return await self._offload(self.reserve_sync, amount, model)

    async def usage(self, tokens: int, cost: float, model: str, reservations: Sequence[str] = ()) -> None:
        await self._offload(self.usage_sync, tokens, cost, model, reservations)

    async def checkpoint(self, data: dict[str, Any]) -> None:
        await self._offload(self.checkpoint_sync, data)

    async def _offload(self, func: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def _call(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.operation.call(name, params, timeout=HOST_CALL_TIMEOUT)
        except HostCallFailed as failure:
            code = failure.code if failure.code in ERROR_CODES else INTERNAL
            reason = None if code == failure.code else failure.code
            raise PluginError(code, failure.message, reason=reason) from None


def _money(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite number, zero or more")
    return number


__all__ = ["ENV_ARTIFACTS_DIR", "ENV_DATA_DIR", "ENV_TEMP_DIR", "HOST_CALL_TIMEOUT", "PROGRESS_MIN_INTERVAL", "PluginContext"]

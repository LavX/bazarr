# coding=utf-8
"""Resident worker pool: keeps v2 plugin workers alive, bounded and supervised.

``WorkerPool`` owns the lifecycle of the ``WorkerSession`` objects that talk to
plugin workers. A caller registers a ``WorkerDefinition`` per plugin and then
calls ``run`` or ``submit``; the pool starts the worker on demand, reuses it,
and stops it again when it is idle, when something else needs its slot, when it
misbehaves or when the plugin is removed.

Worker states (``WorkerState``)::

    stopped    no process. The next request starts one.
    starting   spawn and handshake in progress; other callers wait for it.
    ready      a live session. ``busy`` is reported while requests are in flight.
    draining   the session was retired (recycle, or new settings) and finishes
               what it already holds. New requests get a fresh worker.
    stopping   being shut down on purpose.
    backoff    the last start or run failed; requests are refused until the delay ends.
    broken     too many failures in a row (the crash loop breaker); one probe start
               is allowed once the open period ends, and a clean answer closes the breaker.

Limits and what enforces them:

* Resident workers are capped (default 8). Starting one more stops the least
  recently used idle worker; if every resident worker is busy the caller waits
  a little, then gets ``PoolUnavailable`` with code ``pool_full``.
* Memory is read from ``/proc/<pid>/smaps_rollup`` for the launcher's whole
  process tree: when a request starts and ends, on every sweep while requests
  run, and once per heartbeat interval otherwise. Over the limit (translators
  1 GiB, other kinds 512 MiB) the worker is killed. Not available off Linux, where
  the check is skipped.
* Folder budgets (a byte cap per directory the worker may write) and the free
  space reserve on the config volume (1 GiB) are checked before a start, before
  a request and on every sweep. A worker over budget is killed, and nothing
  starts while the reserve is short.
* Heartbeats (15 s, three misses) are enforced by the session; the pool turns
  the session's death into a failure and a restart policy.
* Restarts back off exponentially. Five failures within five minutes open the
  breaker for two minutes.
* ``recycle_needed`` (the session has been handed too many distinct secrets to
  keep redacting them all) retires the worker once its requests finish.
* Standard error is scrubbed by the session for every secret value it ever
  carried. The pool adds its own log rate limit and a short tail for status
  pages. Request parameters and secrets never reach a pool log line.

Threading: all state is guarded by one lock. Slow work (spawn, handshake,
shutdown, reading ``/proc``, measuring folders) runs outside it. Public methods
are safe to call from any thread.
"""

from __future__ import annotations

import enum
import logging
import math
import os
import shutil
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..sandbox import spawn_worker
from .errors import HandshakeError, RequestError, SessionError
from .frames import Codes
from .session import RequestHandle, RequestResult, SessionLimits, WorkerSession

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MEMORY_BYTES",
    "DEFAULT_MAX_RESIDENT",
    "CONFIG_RESERVE_BYTES",
    "TRANSLATOR_MEMORY_BYTES",
    "FolderBudget",
    "MemorySample",
    "PoolCodes",
    "PoolClosed",
    "PoolError",
    "PoolLimits",
    "PoolRequest",
    "PoolUnavailable",
    "UnknownWorker",
    "WorkerDefinition",
    "WorkerKilled",
    "WorkerPool",
    "WorkerState",
    "folder_usage",
    "launcher_spawn",
    "memory_limit_for",
    "read_process_tree_memory",
]

MIB = 1024 * 1024
GIB = 1024 * MIB

DEFAULT_MAX_RESIDENT = 8
TRANSLATOR_MEMORY_BYTES = 1 * GIB
DEFAULT_MEMORY_BYTES = 512 * MIB
CONFIG_RESERVE_BYTES = 1 * GIB

_SIGKILL = getattr(signal, "SIGKILL", None)


class PoolCodes:
    """``code`` values the pool itself raises (see also ``frames.Codes``)."""

    UNAVAILABLE = "worker_unavailable"
    POOL_FULL = "pool_full"
    BACKOFF = "backoff"
    CIRCUIT_OPEN = "circuit_open"
    START_FAILED = "start_failed"
    SANDBOX_REFUSED = "sandbox_refused"
    SANDBOX_BLOCKED = Codes.SANDBOX_BLOCKED
    MEMORY_LIMIT = "memory_limit"
    FOLDER_BUDGET = "folder_budget"
    DISK_RESERVE = "disk_reserve"
    POOL_CLOSED = "pool_closed"
    UNKNOWN_WORKER = "unknown_worker"


class PoolError(Exception):
    code = PoolCodes.UNAVAILABLE

    def __init__(self, message, *, code=None, retry_after=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        # Seconds until trying again can work, when the pool knows.
        self.retry_after = retry_after


class PoolUnavailable(PoolError):
    """No worker can take this request right now. Nothing was sent."""


class PoolClosed(PoolError):
    code = PoolCodes.POOL_CLOSED


class UnknownWorker(PoolError):
    code = PoolCodes.UNKNOWN_WORKER


class WorkerKilled(PoolError):
    """The pool ended the worker for breaking a limit while it held this request."""


class WorkerState(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"  # reported for ready workers with requests in flight; never stored
    DRAINING = "draining"
    STOPPING = "stopping"
    BACKOFF = "backoff"
    BROKEN = "broken"


_S = WorkerState
_TRANSITIONS: dict[WorkerState, frozenset] = {
    _S.STOPPED: frozenset({_S.STARTING}),
    _S.STARTING: frozenset({_S.READY, _S.BACKOFF, _S.BROKEN, _S.STOPPED}),
    _S.READY: frozenset({_S.DRAINING, _S.STOPPING, _S.BACKOFF, _S.BROKEN}),
    _S.DRAINING: frozenset({_S.STARTING, _S.STOPPED}),
    _S.STOPPING: frozenset({_S.STOPPED}),
    _S.BACKOFF: frozenset({_S.STARTING, _S.STOPPED}),
    _S.BROKEN: frozenset({_S.STARTING, _S.STOPPED}),
}
_COUNTS_AS_RESIDENT = (_S.STARTING, _S.READY)


def memory_limit_for(kind: str | None) -> int:
    return TRANSLATOR_MEMORY_BYTES if kind == "translator" else DEFAULT_MEMORY_BYTES


@dataclass(frozen=True)
class FolderBudget:
    """A byte cap on a directory tree a worker may write into."""

    path: str
    max_bytes: int
    label: str = ""


@dataclass(frozen=True)
class WorkerDefinition:
    """How to start and police one plugin worker.

    Give either ``spawn`` (a callable returning a ``Popen`` with binary pipes,
    for example ``launcher_spawn(...)``) or ``command`` (plain ``Popen``, used
    by tests and by hosts without a sandbox). The pool calls ``spawn`` again
    for every start, so a spec built inside it is always fresh.
    """

    key: str
    kind: str | None = None
    kind_versions: Sequence[tuple[int, int]] = ((1, 0),)
    spawn: Callable[[], Any] | None = None
    command: Sequence[str] | None = None
    env: Mapping[str, str] | None = None
    cwd: str | None = None
    host_calls: Mapping[str, Callable] = field(default_factory=dict)
    memory_limit_bytes: int | None = None
    budgets: Sequence[FolderBudget] = ()
    # False for a worker with nowhere to write: the free-space reserve ignores it.
    writes_data: bool = True
    # None uses the pool default; ``math.inf`` keeps the worker until it is evicted.
    idle_timeout: float | None = None
    session_limits: SessionLimits | None = None
    name: str | None = None

    def memory_limit(self) -> int:
        return self.memory_limit_bytes if self.memory_limit_bytes else memory_limit_for(self.kind)

    def check(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError("a worker definition needs a key")
        if self.spawn is None and not self.command:
            raise ValueError("a worker definition needs spawn or command")


@dataclass(frozen=True)
class PoolLimits:
    """Pool tunables. The defaults are the production values."""

    max_resident: int = DEFAULT_MAX_RESIDENT
    idle_timeout: float = 300.0
    heartbeat_interval: float = 15.0
    heartbeat_misses: int = 3
    config_reserve_bytes: int = CONFIG_RESERVE_BYTES
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    breaker_threshold: int = 5
    breaker_window: float = 300.0
    breaker_open: float = 120.0
    # How long a caller may wait for a slot or for another caller's start.
    acquire_timeout: float = 10.0
    # Sweep resolution. Idle workers are sampled once per heartbeat interval.
    tick: float = 1.0
    budget_interval: float = 10.0
    budget_max_entries: int = 100_000
    # Draft ``options`` and ``test`` runs outside the resident set.
    max_oneshot: int = 2
    close_grace: float = 2.0
    stderr_log_per_second: int = 20
    stderr_tail_lines: int = 50


@dataclass(frozen=True)
class MemorySample:
    bytes: int  # proportional set size when the kernel reports it, else RSS
    rss_bytes: int
    processes: int
    source: str


# ----------------------------------------------------------------- /proc readers


def _read_kb(path: str, *names: str) -> dict:
    found: dict = {}
    wanted = set(names)
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        for line in handle:
            name, _, rest = line.partition(":")
            if name in wanted:
                try:
                    found[name] = int(rest.split()[0]) * 1024
                except (ValueError, IndexError):
                    continue
    return found


def _children_of(pid: int) -> list[int] | None:
    """Direct children from the kernel's per-thread children lists, or None."""
    found: list[int] = []
    try:
        threads = os.listdir("/proc/%d/task" % pid)
    except OSError:
        return None
    readable = False
    for tid in threads:
        try:
            with open("/proc/%d/task/%s/children" % (pid, tid), "r", encoding="ascii") as handle:
                readable = True
                found.extend(int(p) for p in handle.read().split())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            continue
    return found if readable else None


def _parent_map(limit: int = 8192) -> dict[int, list[int]]:
    """pid -> children by scanning /proc. The fallback when children lists are off."""
    tree: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return tree
    for name in names[:limit]:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r", encoding="ascii", errors="replace") as handle:
                text = handle.read()
            ppid = int(text.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        tree.setdefault(ppid, []).append(int(name))
    return tree


def _process_tree(root: int, max_processes: int) -> list[int]:
    seen = [root]
    index = 0
    fallback: dict[int, list[int]] | None = None
    while index < len(seen) and len(seen) < max_processes:
        current = seen[index]
        index += 1
        kids = _children_of(current)
        if kids is None:
            if fallback is None:
                fallback = _parent_map()
            kids = fallback.get(current, [])
        for kid in kids:
            if kid not in seen and len(seen) < max_processes:
                seen.append(kid)
    return seen


def read_process_tree_memory(pid: int, max_processes: int = 256) -> MemorySample | None:
    """Memory of a process and every descendant, from ``smaps_rollup``.

    The sum uses ``Pss`` so pages shared between workers are not counted in
    full by each. A process without ``smaps_rollup`` (older kernel, or a
    hardened ``/proc``) falls back to ``VmRSS`` from ``status``. Returns None
    when the root process is gone or ``/proc`` is unavailable.
    """
    if not os.path.isdir("/proc/self"):
        return None
    total = 0
    rss_total = 0
    counted = 0
    source = "smaps_rollup"
    for member in _process_tree(pid, max_processes):
        try:
            values = _read_kb("/proc/%d/smaps_rollup" % member, "Pss", "Rss")
            used = values.get("Pss", values.get("Rss"))
            rss = values.get("Rss", used)
            if used is None:
                raise OSError("no memory figure")
        except OSError:
            try:
                values = _read_kb("/proc/%d/status" % member, "VmRSS")
            except OSError:
                continue
            used = rss = values.get("VmRSS")
            if used is None:
                continue
            source = "status"
        total += used
        rss_total += rss
        counted += 1
    if not counted:
        return None
    return MemorySample(bytes=total, rss_bytes=rss_total, processes=counted, source=source)


def folder_usage(path: str, max_entries: int = 100_000) -> tuple[int, bool]:
    """Allocated bytes under ``path`` without following links.

    Returns ``(bytes, truncated)``; ``truncated`` means the walk stopped at
    ``max_entries`` and the caller should treat the folder as over any budget.
    A missing folder is empty.
    """
    total = 0
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                for entry in scan:
                    seen += 1
                    if seen > max_entries:
                        return total, True
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        total += getattr(info, "st_blocks", 0) * 512 or info.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, False


def launcher_spawn(build_spec: Callable[[], Any], *, probe=None, require_sandbox: bool = False) -> Callable[[], Any]:
    """A ``spawn`` callable that runs the worker through the sandbox launcher.

    ``build_spec`` returns a fresh ``RuntimeSpec`` for every start, so the
    recorded identities of the granted paths are always current and a changed
    port set takes effect on the next start.
    """

    def spawn():
        return spawn_worker(build_spec(), probe=probe, require_sandbox=require_sandbox)

    return spawn


# --------------------------------------------------------------------- internals


class _Slot:
    """One session and what the pool knows about it."""

    def __init__(self, definition: WorkerDefinition, now: float, limits: PoolLimits, ephemeral: bool = False):
        self.definition = definition
        self.session: WorkerSession | None = None
        self.started_at = now
        self.last_used = now
        self.last_sample_at = -math.inf
        self.pending = 0  # submits in progress: the session does not know about them yet
        self.kill_reason: tuple[str, str] | None = None
        self.killed_busy = False
        self.memory_bytes = 0
        self.peak_bytes = 0
        self.ephemeral = ephemeral
        self.closing = False
        self.stderr_tail: deque = deque(maxlen=max(1, limits.stderr_tail_lines))
        self._stderr_window = (0, 0)
        self._stderr_dropped = 0
        self._limits = limits

    def busy(self) -> bool:
        session = self.session
        return self.pending > 0 or (session is not None and session.in_flight > 0)

    def stderr_sink(self, key: str) -> Callable[[str], None]:
        def sink(line: str) -> None:
            self.stderr_tail.append(line)
            second = int(time.monotonic())
            window, count = self._stderr_window
            if second != window:
                if self._stderr_dropped:
                    logger.info("%s: %d stderr lines not logged", key, self._stderr_dropped)
                    self._stderr_dropped = 0
                self._stderr_window = (second, 1)
            elif count >= self._limits.stderr_log_per_second:
                self._stderr_dropped += 1
                return
            else:
                self._stderr_window = (window, count + 1)
            logger.info("%s stderr: %s", key, line)

        return sink


class _Entry:
    def __init__(self, definition: WorkerDefinition):
        self.definition = definition
        self.state = WorkerState.STOPPED
        self.slot: _Slot | None = None
        self.retiring: list[_Slot] = []
        self.failures: deque = deque()
        self.backoff_until = 0.0
        self.open_until = 0.0
        self.last_error: str | None = None
        self.last_budget_check = -math.inf
        self.budget_problem: tuple[str, str] | None = None
        self.starts = 0
        self.crashes = 0
        self.last_peak_bytes = 0


class PoolRequest:
    """A request in flight through the pool. ``wait`` returns the result or raises."""

    def __init__(self, pool, entry, slot, handle: RequestHandle):
        self._pool = pool
        self._entry = entry
        self._slot = slot
        self._handle = handle
        self._finished = False
        self._finish_lock = threading.Lock()

    @property
    def id(self):
        return self._handle.id

    def done(self) -> bool:
        return self._handle.done()

    def cancel(self, reason="cancelled") -> None:
        self._handle.cancel(reason)

    def wait(self, timeout=None) -> RequestResult:
        try:
            result = self._handle.wait(timeout)
        except TimeoutError:
            raise
        except BaseException as error:
            self._finish(error)
            raise self._pool._translate(self._slot, error)
        self._finish(None)
        return result

    def _finish(self, error):
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
        self._pool._after_request(self._entry, self._slot, error)


class WorkerPool:
    """Starts, reuses, polices and stops plugin workers. See the module docstring."""

    def __init__(
        self,
        limits: PoolLimits | None = None,
        *,
        config_dir: str | os.PathLike | None = None,
        event_sink: Callable[[str, str, dict], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        disk_free: Callable[[], int | None] | None = None,
        memory_reader: Callable[[int], MemorySample | None] | None = None,
        autostart: bool = True,
    ):
        self.limits = limits or PoolLimits()
        self._config_dir = os.fspath(config_dir) if config_dir else None
        self._disk_free = disk_free or self._default_disk_free
        self._memory_reader = memory_reader or read_process_tree_memory
        self._event_sink = event_sink
        self._clock = clock
        self._autostart = autostart
        self._max_resident = max(1, int(self.limits.max_resident))

        self._cond = threading.Condition(threading.RLock())
        self._entries: dict[str, _Entry] = {}
        self._ephemeral: set[_Slot] = set()
        self._oneshot = threading.BoundedSemaphore(max(1, self.limits.max_oneshot))
        self._closed = False
        self._closes: list[tuple[_Slot, _Entry | None, str, float]] = []
        self._events: list[tuple[str, str, dict]] = []
        self._sampler: threading.Thread | None = None
        self._sampler_stop = threading.Event()

    # ------------------------------------------------------------ registration

    def register(self, definition: WorkerDefinition) -> None:
        definition.check()
        with self._cond:
            self._check_open()
            if definition.key in self._entries:
                raise ValueError("a worker named %s is already registered" % definition.key)
            self._entries[definition.key] = _Entry(definition)

    def unregister(self, key: str) -> None:
        """Stop the worker (and any retiring one) and forget the definition."""
        with self._cond:
            entry = self._entries.pop(key, None)
            if entry is None:
                return
            if entry.slot is not None:
                self._queue_close(entry.slot, None, "kill")
                entry.slot = None
            for slot in entry.retiring:
                self._queue_close(slot, None, "kill")
            entry.retiring = []
            entry.state = WorkerState.STOPPED
            self._cond.notify_all()
        self._flush()

    def definition(self, key: str) -> WorkerDefinition:
        with self._cond:
            return self._entry(key).definition

    def restart_after_drain(self, key: str, definition: WorkerDefinition | None = None) -> None:
        """Make the next request run on a fresh worker built from ``definition``.

        The current worker is retired: it keeps serving what it already holds,
        and is stopped when that is done. Use it when a setting that the
        sandbox policy bakes in changes, such as a port set. Nothing running
        is interrupted.
        """
        with self._cond:
            entry = self._entry(key)
            if definition is not None:
                definition.check()
                if definition.key != key:
                    raise ValueError("definition key %s does not match %s" % (definition.key, key))
                entry.definition = definition
            if entry.state == WorkerState.READY and entry.slot is not None:
                self._retire(entry, "settings changed")
        self._flush()

    def set_max_resident(self, count: int) -> None:
        """Change the resident cap (the user setting). Idle surplus workers stop now."""
        with self._cond:
            self._max_resident = max(1, int(count))
            surplus = self._resident_count() - self._max_resident
            if surplus > 0:
                for entry in self._idle_by_age(None)[:surplus]:
                    self._begin_stop(entry, "resident cap lowered")
        self._flush()

    @property
    def max_resident(self) -> int:
        return self._max_resident

    # ----------------------------------------------------------------- queries

    def state(self, key: str) -> WorkerState:
        with self._cond:
            return self._public_state(self._entry(key))

    def status(self) -> dict:
        """A snapshot for status pages. No parameters, secrets or paths."""
        now = self._clock()
        with self._cond:
            workers = {}
            for key, entry in self._entries.items():
                slot = entry.slot
                session = slot.session if slot else None
                until = max(entry.backoff_until, entry.open_until)
                workers[key] = {
                    "state": self._public_state(entry).value,
                    "kind": entry.definition.kind,
                    "pid": session.process.pid if session and session.process else None,
                    "in_flight": session.in_flight if session else 0,
                    "memory_bytes": slot.memory_bytes if slot else 0,
                    "peak_bytes": slot.peak_bytes if slot else entry.last_peak_bytes,
                    "memory_limit_bytes": entry.definition.memory_limit(),
                    "idle_seconds": max(0.0, now - slot.last_used) if slot and not slot.busy() else 0.0,
                    "retiring": len(entry.retiring),
                    "failures": len(entry.failures),
                    "retry_after": max(0.0, until - now) if entry.state in (_S.BACKOFF, _S.BROKEN) else None,
                    "last_error": entry.last_error,
                    "starts": entry.starts,
                    "crashes": entry.crashes,
                    "recent_stderr": list(slot.stderr_tail) if slot else [],
                }
            return {
                "max_resident": self._max_resident,
                "resident": self._resident_count(),
                "workers": workers,
            }

    # ---------------------------------------------------------------- requests

    def submit(self, key: str, op: str, params=None, *, timeout=None, max_result_bytes=None,
               secrets=None, on_progress=None) -> PoolRequest:
        """Send a request to the worker for ``key``, starting it if needed.

        ``secrets`` travel in the request frame only; they are never stored in
        the definition or logged. Raises ``PoolUnavailable`` (backoff, open
        breaker, full pool, low disk, budget), ``PoolClosed``, ``UnknownWorker``,
        ``SessionBusy`` (the worker is at its in-flight limit) or a start error.
        """
        last_error: BaseException | None = None
        for _attempt in range(3):
            entry, slot = self._lease(key)
            try:
                self._preflight_running(entry, slot)
                self._sample(slot)
                handle = slot.session.submit(
                    op, params, timeout=timeout, max_result_bytes=max_result_bytes,
                    secrets=secrets, on_progress=on_progress)
            except SessionError as error:
                with self._cond:
                    slot.pending -= 1
                    if (error.code == Codes.RECYCLE_REQUIRED and entry.slot is slot
                            and entry.state == WorkerState.READY):
                        self._retire(entry, "too many distinct secrets")
                        last_error = error
                        retry = True
                    else:
                        retry = False
                    if not retry and not slot.session.alive and entry.slot is slot:
                        self._handle_death(entry, slot, self._death_reason(slot))
                self._flush()
                if retry:
                    continue
                raise self._translate(slot, error)
            except BaseException:
                with self._cond:
                    slot.pending -= 1
                self._flush()
                raise
            with self._cond:
                slot.pending -= 1
                slot.last_used = self._clock()
            return PoolRequest(self, entry, slot, handle)
        raise last_error  # type: ignore[misc]

    def run(self, key: str, op: str, params=None, *, timeout=None, **kwargs) -> RequestResult:
        """Submit and wait. ``timeout`` is the deadline for the whole request."""
        return self.submit(key, op, params, timeout=timeout, **kwargs).wait()

    def warm(self, key: str) -> None:
        """Start the worker without sending anything."""
        entry, slot = self._lease(key)
        with self._cond:
            slot.pending -= 1
        self._flush()

    def run_once(self, definition: WorkerDefinition, op: str, params=None, *, timeout=None,
                 max_result_bytes=None, secrets=None, on_progress=None) -> RequestResult:
        """Run one request on a throwaway worker that is not part of the pool.

        For draft ``options`` and ``test`` calls on settings that are not
        saved yet. The worker obeys the same memory, budget and disk rules and
        is always stopped afterwards. It never touches the breaker. At most
        ``max_oneshot`` run at once.
        """
        definition.check()
        with self._cond:
            self._check_open()
        if not self._oneshot.acquire(timeout=self.limits.acquire_timeout):
            raise PoolUnavailable("too many one-off runs in progress", code=PoolCodes.POOL_FULL, retry_after=1.0)
        slot = _Slot(definition, self._clock(), self.limits, ephemeral=True)
        try:
            problem = self._violation(definition, None)
            if problem:
                raise PoolUnavailable(problem[1], code=problem[0])
            with self._cond:
                self._check_open()
                self._ephemeral.add(slot)
            self._ensure_sampler()
            try:
                self._open_session(slot)
            except PoolError:
                raise
            except BaseException as error:
                raise self._start_error(slot, error)
            try:
                return slot.session.request(
                    op, params, timeout=timeout, max_result_bytes=max_result_bytes,
                    secrets=secrets, on_progress=on_progress)
            except (SessionError, RequestError) as error:
                raise self._translate(slot, error)
        finally:
            if slot.session is not None:
                try:
                    slot.session.close(grace=1.0)
                except Exception:
                    logger.debug("one-off worker close failed", exc_info=True)
            with self._cond:
                self._ephemeral.discard(slot)
            self._oneshot.release()

    # --------------------------------------------------------------- operations

    def stop(self, key: str) -> None:
        """Stop an idle or busy worker on purpose. A worker in backoff stays in backoff."""
        with self._cond:
            entry = self._entry(key)
            if entry.state == WorkerState.READY:
                self._begin_stop(entry, "stopped")
        self._flush()

    def reset(self, key: str) -> None:
        """Forget the failure history and end backoff or an open breaker."""
        with self._cond:
            entry = self._entry(key)
            entry.failures.clear()
            entry.backoff_until = entry.open_until = 0.0
            if entry.state in (WorkerState.BACKOFF, WorkerState.BROKEN):
                self._set_state(entry, WorkerState.STOPPED, "reset")
            self._cond.notify_all()

    def close(self) -> None:
        """Stop every worker. The pool refuses new work afterwards. Idempotent."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._sampler_stop.set()
            for entry in self._entries.values():
                if entry.slot is not None:
                    self._queue_close(entry.slot, None, "close")
                    entry.slot = None
                for slot in entry.retiring:
                    self._queue_close(slot, None, "close")
                entry.retiring = []
                entry.state = WorkerState.STOPPED
            for slot in list(self._ephemeral):
                self._queue_close(slot, None, "kill")
            self._cond.notify_all()
        self._flush(parallel=True)
        sampler = self._sampler
        if sampler is not None and sampler is not threading.current_thread():
            sampler.join(timeout=3.0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------- sweep

    def sweep(self) -> None:
        """One supervision pass. The sampler thread calls this every ``tick``."""
        now = self._clock()
        with self._cond:
            if self._closed:
                return
            current = [(entry, entry.slot) for entry in self._entries.values() if entry.slot is not None]
            retiring = [(entry, slot) for entry in self._entries.values() for slot in entry.retiring]
            ephemeral = list(self._ephemeral)
        free = self._free_space()
        short_on_disk = free is not None and free < self.limits.config_reserve_bytes
        for entry, slot in current:
            self._sweep_current(entry, slot, now, short_on_disk)
        for entry, slot in retiring:
            self._sweep_retiring(entry, slot, now)
        for slot in ephemeral:
            self._sweep_ephemeral(slot, now, short_on_disk)
        with self._cond:
            for entry in self._entries.values():
                if entry.state == WorkerState.DRAINING and not entry.retiring and entry.slot is None:
                    self._set_state(entry, WorkerState.STOPPED, "drained")
            self._cond.notify_all()
        self._flush()

    def _sweep_current(self, entry, slot, now, short_on_disk):
        with self._cond:
            if entry.slot is not slot or entry.state != WorkerState.READY:
                return
            if not slot.session.alive:
                self._handle_death(entry, slot, self._death_reason(slot))
                return
        self._sample_if_due(slot, now)
        definition = entry.definition
        problem = None
        if slot.memory_bytes > definition.memory_limit():
            problem = (PoolCodes.MEMORY_LIMIT, "worker used %d MiB, over its %d MiB limit" % (
                slot.memory_bytes // MIB, definition.memory_limit() // MIB))
        elif short_on_disk and definition.writes_data:
            problem = (PoolCodes.DISK_RESERVE, "free space on the config volume is under the reserve")
        else:
            problem = self._budget_problem(entry, now)
        if problem is not None:
            self._kill_current(entry, slot, problem)
            return
        with self._cond:
            if entry.slot is not slot or entry.state != WorkerState.READY:
                return
            session = slot.session
            if slot.busy():
                slot.last_used = now
                return
            idle_limit = definition.idle_timeout if definition.idle_timeout is not None else self.limits.idle_timeout
            if session.recycle_needed:
                self._begin_stop(entry, "recycled")
            elif now - slot.last_used > idle_limit:
                self._begin_stop(entry, "idle")

    def _sweep_retiring(self, entry, slot, now):
        with self._cond:
            if slot not in entry.retiring:
                return
            session = slot.session
            if session is None or not session.alive:
                entry.retiring.remove(slot)
                self._queue_close(slot, None, "reap")
                return
        self._sample_if_due(slot, now)
        if slot.memory_bytes > entry.definition.memory_limit():
            slot.kill_reason = (PoolCodes.MEMORY_LIMIT, "retiring worker over its memory limit")
            with self._cond:
                if slot in entry.retiring:
                    entry.retiring.remove(slot)
                self._queue_close(slot, None, "kill")
            return
        with self._cond:
            if slot in entry.retiring and not slot.busy():
                entry.retiring.remove(slot)
                self._queue_close(slot, None, "stop")

    def _sweep_ephemeral(self, slot, now, short_on_disk):
        if slot.session is None or not slot.session.alive:
            return
        self._sample_if_due(slot, now)
        if slot.memory_bytes > slot.definition.memory_limit():
            slot.kill_reason = (PoolCodes.MEMORY_LIMIT, "worker used %d MiB, over its %d MiB limit" % (
                slot.memory_bytes // MIB, slot.definition.memory_limit() // MIB))
        elif short_on_disk and slot.definition.writes_data:
            slot.kill_reason = (PoolCodes.DISK_RESERVE, "free space on the config volume is under the reserve")
        else:
            return
        self._terminate_now(slot)

    # ------------------------------------------------------------ lease / start

    def _check_open(self):
        if self._closed:
            raise PoolClosed("the worker pool is closed")

    def _entry(self, key) -> _Entry:
        entry = self._entries.get(key)
        if entry is None:
            raise UnknownWorker("no worker named %s is registered" % key)
        return entry

    def _public_state(self, entry) -> WorkerState:
        if entry.state == WorkerState.READY and entry.slot is not None and entry.slot.busy():
            return WorkerState.BUSY
        return entry.state

    def _lease(self, key) -> tuple[_Entry, _Slot]:
        """A ready slot for ``key`` with ``pending`` raised by one. Starts the worker if needed."""
        self._ensure_sampler()
        deadline = self._clock() + self.limits.acquire_timeout
        try:
            while True:
                with self._cond:
                    self._check_open()
                    entry = self._entry(key)
                    now = self._clock()
                    state = entry.state
                    if state == WorkerState.READY:
                        slot = entry.slot
                        if slot is None or not slot.session.alive:
                            self._handle_death(entry, slot, self._death_reason(slot))
                            continue
                        if slot.session.recycle_needed:
                            self._retire(entry, "too many distinct secrets")
                            continue
                        slot.pending += 1
                        slot.last_used = now
                        return entry, slot
                    if state in (WorkerState.STARTING, WorkerState.STOPPING):
                        self._wait(deadline, "worker is still %s" % state.value)
                        continue
                    if state == WorkerState.BACKOFF and now < entry.backoff_until:
                        raise PoolUnavailable(
                            "worker failed and is backing off: %s" % (entry.last_error or "no detail"),
                            code=PoolCodes.BACKOFF, retry_after=entry.backoff_until - now)
                    if state == WorkerState.BROKEN and now < entry.open_until:
                        raise PoolUnavailable(
                            "worker keeps failing and is paused: %s" % (entry.last_error or "no detail"),
                            code=PoolCodes.CIRCUIT_OPEN, retry_after=entry.open_until - now)
                    # stopped, draining, or a backoff or breaker period that has ended
                    victims = self._claim_capacity(entry)
                    if victims is None:
                        self._wait(deadline, "every resident worker is busy", code=PoolCodes.POOL_FULL)
                        continue
                    self._set_state(entry, WorkerState.STARTING, "request")
                    entry.starts += 1
                    definition = entry.definition
                    slot = _Slot(definition, now, self.limits)
                break
        finally:
            self._flush()
        return self._start_slot(entry, slot)

    def _wait(self, deadline, why, code=PoolCodes.UNAVAILABLE):
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise PoolUnavailable(why, code=code, retry_after=1.0)
        self._cond.wait(min(remaining, 0.5))

    def _start_slot(self, entry, slot) -> tuple[_Entry, _Slot]:
        error: BaseException | None = None
        refusal: tuple[str, str] | None = None
        try:
            refusal = self._violation(entry.definition, entry, fresh=True)
            if refusal is None:
                self._open_session(slot)
        except BaseException as caught:
            error = caught
        with self._cond:
            if self._closed:
                if slot.session is not None:
                    self._queue_close(slot, None, "kill")
                if entry.state == WorkerState.STARTING:
                    self._set_state(entry, WorkerState.STOPPED, "pool closed")
                self._cond.notify_all()
                failed = PoolClosed("the worker pool is closed")
            elif refusal is not None:
                self._set_state(entry, WorkerState.STOPPED, refusal[0])
                self._cond.notify_all()
                failed = PoolUnavailable(refusal[1], code=refusal[0])
            elif error is not None:
                failed = self._fail_start(entry, slot, error)
                self._cond.notify_all()
            else:
                entry.slot = slot
                slot.pending += 1
                slot.last_used = self._clock()
                self._set_state(entry, WorkerState.READY, "started")
                self._cond.notify_all()
                failed = None
        self._flush()
        if failed is not None:
            raise failed
        return entry, slot

    def _open_session(self, slot):
        definition = slot.definition
        key = definition.key
        limits = definition.session_limits or SessionLimits(
            heartbeat_interval=self.limits.heartbeat_interval,
            heartbeat_timeout=self.limits.heartbeat_interval * self.limits.heartbeat_misses,
        )
        session = WorkerSession(
            definition.command,
            spawn=definition.spawn,
            cwd=definition.cwd,
            env=dict(definition.env) if definition.env else None,
            kind=definition.kind,
            kind_versions=definition.kind_versions,
            limits=limits,
            host_calls=dict(definition.host_calls),
            on_event=lambda name, payload, _key=key: self._emit(_key, name, payload),
            on_stderr=slot.stderr_sink(key),
            name=definition.name or key,
        )
        slot.session = session
        session.start()

    def _launcher_failure(self, slot):
        process = slot.session.process if slot.session is not None else None
        getter = getattr(process, "launcher_failure", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _start_error(self, slot, error) -> PoolError:
        """Map whatever a failed start raised to the error a caller sees."""
        if isinstance(error, PoolError):
            return error
        refused = type(error).__name__ == "SandboxRefused"
        if refused:
            return PoolUnavailable(str(error), code=PoolCodes.SANDBOX_REFUSED)
        failure = self._launcher_failure(slot)
        if failure is not None and failure.kind == "sandbox":
            return PoolUnavailable("the sandbox could not be applied: %s" % failure.message,
                                   code=PoolCodes.SANDBOX_BLOCKED)
        if failure is not None:
            return PoolUnavailable("the launcher could not start the worker: %s" % failure.message,
                                   code=PoolCodes.START_FAILED)
        if isinstance(error, HandshakeError):
            return PoolUnavailable("worker handshake failed: %s" % error, code=PoolCodes.START_FAILED)
        return PoolUnavailable("worker could not be started: %s" % (error or type(error).__name__),
                               code=PoolCodes.START_FAILED)

    def _fail_start(self, entry, slot, error) -> PoolError:
        """Record a failed start under the lock and return the error to raise."""
        mapped = self._start_error(slot, error)
        if slot.session is not None:
            self._queue_close(slot, None, "reap")
        if mapped.code == PoolCodes.SANDBOX_REFUSED:
            # A policy answer, not a crash: nothing to back off from.
            self._set_state(entry, WorkerState.STOPPED, mapped.code)
        else:
            self._record_failure(entry, str(mapped))
            mapped.retry_after = max(0.0, max(entry.backoff_until, entry.open_until) - self._clock())
        mapped.__cause__ = error
        return mapped

    # ---------------------------------------------------------- state handling

    def _set_state(self, entry, new, reason=""):
        old = entry.state
        if new == old:
            return
        if new not in _TRANSITIONS[old]:
            raise RuntimeError("illegal worker state change %s -> %s" % (old.value, new.value))
        entry.state = new
        self._events.append((self._key_of(entry), "state", {"from": old.value, "to": new.value, "reason": reason}))
        logger.debug("%s: %s -> %s (%s)", self._key_of(entry), old.value, new.value, reason)

    def _key_of(self, entry):
        return entry.definition.key

    def _resident_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.state in _COUNTS_AS_RESIDENT)

    def _idle_by_age(self, skip) -> list[_Entry]:
        idle = [
            entry for entry in self._entries.values()
            if entry is not skip and entry.state == WorkerState.READY and entry.slot is not None
            and not entry.slot.busy()
        ]
        idle.sort(key=lambda e: e.slot.last_used)
        return idle

    def _claim_capacity(self, entry) -> list | None:
        """Stop idle LRU workers to make room for ``entry``. None: no room yet."""
        over = self._resident_count() + 1 - self._max_resident
        if over <= 0:
            return []
        idle = self._idle_by_age(entry)
        if len(idle) < over:
            return None
        chosen = idle[:over]
        for victim in chosen:
            self._begin_stop(victim, "evicted for %s" % self._key_of(entry))
        return chosen

    def _begin_stop(self, entry, reason):
        slot = entry.slot
        entry.slot = None
        self._set_state(entry, WorkerState.STOPPING, reason)
        if slot is not None:
            entry.last_peak_bytes = slot.peak_bytes
            self._queue_close(slot, entry, "stop")
        else:
            self._set_state(entry, WorkerState.STOPPED, reason)

    def _retire(self, entry, reason):
        slot = entry.slot
        entry.slot = None
        self._set_state(entry, WorkerState.DRAINING, reason)
        if slot is not None:
            entry.retiring.append(slot)

    def _record_failure(self, entry, reason):
        now = self._clock()
        limits = self.limits
        entry.failures.append(now)
        while entry.failures and now - entry.failures[0] > limits.breaker_window:
            entry.failures.popleft()
        entry.last_error = reason
        entry.crashes += 1
        count = len(entry.failures)
        if count >= limits.breaker_threshold:
            entry.open_until = now + limits.breaker_open
            self._set_state(entry, WorkerState.BROKEN, reason)
        else:
            delay = min(limits.backoff_cap, limits.backoff_base * (2 ** (count - 1)))
            entry.backoff_until = now + delay
            self._set_state(entry, WorkerState.BACKOFF, reason)

    def _handle_death(self, entry, slot, reason):
        """The current worker ended without being asked to. Caller holds the lock."""
        if slot is not None and entry.slot is slot:
            entry.slot = None
        if slot is not None:
            entry.last_peak_bytes = slot.peak_bytes
            self._queue_close(slot, None, "reap")
        if entry.state == WorkerState.READY:
            self._record_failure(entry, reason)
        self._cond.notify_all()

    def _death_reason(self, slot) -> str:
        if slot is None:
            return "worker went away"
        if slot.kill_reason:
            return slot.kill_reason[1]
        code = slot.session.exit_code if slot.session is not None else None
        return "worker exited unexpectedly" + (" (exit code %s)" % code if code is not None else "")

    def _after_request(self, entry, slot, error):
        """A request finished. Settle the worker's health and re-sample its memory."""
        self._sample(slot)
        limit = slot.definition.memory_limit()
        with self._cond:
            slot.last_used = self._clock()
            if slot.memory_bytes > limit and slot.kill_reason is None:
                slot.kill_reason = (PoolCodes.MEMORY_LIMIT, "worker used %d MiB, over its %d MiB limit" % (
                    slot.memory_bytes // MIB, limit // MIB))
                over = True
            else:
                over = False
            session = slot.session
            if entry.slot is slot and entry.state == WorkerState.READY:
                if over and session.alive and not slot.busy():
                    # It finished its work and then proved too big: stop it, no failure.
                    self._begin_stop(entry, slot.kill_reason[1])
                elif over or not session.alive:
                    self._handle_death(entry, slot, self._death_reason(slot))
                elif error is None or isinstance(error, RequestError):
                    # The worker answered. A clean answer closes the breaker.
                    entry.failures.clear()
                    entry.backoff_until = entry.open_until = 0.0
            elif slot in entry.retiring and not session.alive:
                entry.retiring.remove(slot)
                self._queue_close(slot, None, "reap")
        if over:
            self._terminate_now(slot)
        self._flush()

    def _translate(self, slot, error):
        """The error a caller sees. A pool kill reads as ``WorkerKilled``, not as a crash."""
        reason = slot.kill_reason
        if reason is not None and isinstance(error, (SessionError, RequestError)):
            killed = WorkerKilled(reason[1], code=reason[0])
            killed.__cause__ = error
            return killed
        return error

    # ------------------------------------------------------------ limits checks

    def _default_disk_free(self):
        if not self._config_dir:
            return None
        try:
            return shutil.disk_usage(self._config_dir).free
        except OSError:
            return None

    def _free_space(self):
        try:
            return self._disk_free()
        except Exception:
            return None

    def _violation(self, definition, entry, fresh=False) -> tuple[str, str] | None:
        """Why this worker must not start or take a request, or None."""
        free = self._free_space()
        if definition.writes_data and free is not None and free < self.limits.config_reserve_bytes:
            return (PoolCodes.DISK_RESERVE,
                    "free space on the config volume (%d MiB) is under the %d MiB reserve" % (
                        free // MIB, self.limits.config_reserve_bytes // MIB))
        return self._budget_problem(entry, self._clock(), definition=definition, force=fresh or entry is None)

    def _budget_problem(self, entry, now, definition=None, force=False):
        definition = definition or entry.definition
        if not definition.budgets:
            return None
        if entry is not None and not force and now - entry.last_budget_check < self.limits.budget_interval:
            return entry.budget_problem
        problem = None
        for budget in definition.budgets:
            used, truncated = folder_usage(budget.path, self.limits.budget_max_entries)
            if truncated or used > budget.max_bytes:
                problem = (PoolCodes.FOLDER_BUDGET, "%s is over its %d MiB budget" % (
                    budget.label or "a worker folder", budget.max_bytes // MIB))
                break
        if entry is not None:
            entry.last_budget_check = now
            entry.budget_problem = problem
        return problem

    def _preflight_running(self, entry, slot):
        """Before a request on a running worker: disk reserve and folder budgets."""
        problem = self._violation(entry.definition, entry)
        if problem is None:
            return
        self._kill_current(entry, slot, problem, requested=True)
        raise PoolUnavailable(problem[1], code=problem[0])

    def _kill_current(self, entry, slot, problem, requested=False):
        with self._cond:
            if slot.kill_reason is None:
                slot.kill_reason = problem
            slot.killed_busy = slot.busy()
            if entry.slot is slot and entry.state == WorkerState.READY:
                # A request is being refused (``requested``): that caller already holds a pending count.
                busy = slot.busy() if not requested else slot.session.in_flight > 0
                if busy:
                    self._handle_death(entry, slot, problem[1])
                else:
                    self._begin_stop(entry, problem[1])
                    # _begin_stop queued a polite stop; a policy kill must not wait for one.
                    self._upgrade_close(slot)
        self._terminate_now(slot)
        self._flush()

    def _upgrade_close(self, slot):
        self._closes = [(s, e, "kill" if s is slot else m, g) for (s, e, m, g) in self._closes]

    # -------------------------------------------------------------- memory

    def _sample(self, slot):
        session = slot.session
        process = session.process if session else None
        if process is None:
            return
        try:
            sample = self._memory_reader(process.pid)
        except Exception:
            sample = None
        now = self._clock()
        with self._cond:
            slot.last_sample_at = now
            if sample is not None:
                slot.memory_bytes = sample.bytes
                slot.peak_bytes = max(slot.peak_bytes, sample.bytes)

    def _sample_if_due(self, slot, now):
        if slot.busy() or now - slot.last_sample_at >= self.limits.heartbeat_interval:
            self._sample(slot)

    # ---------------------------------------------------- closing and killing

    def _queue_close(self, slot, entry, mode, grace=None):
        """Schedule ending a session. Runs in ``_flush``, outside the lock."""
        if slot.closing:
            return
        slot.closing = True
        self._closes.append((slot, entry, mode, self.limits.close_grace if grace is None else grace))

    def _terminate_now(self, slot):
        """End the worker's whole tree without a polite shutdown frame."""
        session = slot.session
        process = session.process if session else None
        if process is None:
            return
        try:
            stop = getattr(process, "stop", None)
            if callable(stop):
                stop(0.5)
            elif process.poll() is None and _SIGKILL is not None and hasattr(os, "killpg"):
                try:
                    os.killpg(process.pid, _SIGKILL)
                except OSError:
                    process.kill()
        except Exception:
            logger.debug("terminate failed", exc_info=True)

    def _run_close(self, job):
        slot, entry, mode, grace = job
        try:
            if slot.session is not None:
                if mode == "kill":
                    self._terminate_now(slot)
                slot.session.close(grace=0 if mode in ("kill", "reap") else grace)
        except Exception:
            logger.debug("worker close failed", exc_info=True)
        finally:
            if entry is not None:
                with self._cond:
                    if entry.state == WorkerState.STOPPING and entry.slot is None:
                        self._set_state(entry, WorkerState.STOPPED, "stopped")
                    self._cond.notify_all()

    def _flush(self, parallel=False):
        """Run queued closes and deliver queued events, outside the lock."""
        with self._cond:
            jobs, self._closes = self._closes, []
        if jobs:
            if parallel and len(jobs) > 1:
                threads = [threading.Thread(target=self._run_close, args=(job,), daemon=True) for job in jobs]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=self.limits.close_grace + 10.0)
            else:
                for job in jobs:
                    self._run_close(job)
        with self._cond:
            events, self._events = self._events, []
        for key, name, payload in events:
            self._emit(key, name, payload)

    def _emit(self, key, name, payload):
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(key, name, payload)
        except Exception:
            logger.debug("pool event sink failed", exc_info=True)

    # ---------------------------------------------------------------- sampler

    def _ensure_sampler(self):
        if not self._autostart:
            return
        with self._cond:
            if self._sampler is not None or self._closed:
                return
            self._sampler = threading.Thread(target=self._sampler_loop, name="worker-pool-sweep", daemon=True)
            self._sampler.start()

    def _sampler_loop(self):
        while not self._sampler_stop.wait(self.limits.tick):
            try:
                self.sweep()
            except Exception:
                logger.exception("worker pool sweep failed")

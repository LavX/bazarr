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

What each public call does to a worker in each state (a worker is ``starting``
while its session is being created; ``ready`` is the entry's current worker;
``draining`` ones were retired and finish what they hold; ``closing`` ones are
being torn down; ``stuck`` ones were torn down but their process is not confirmed
gone; ``detached`` ones are starts whose entry was removed, stopped or closed)::

    call               starting         ready            draining         closing       stuck           detached
    stop(key)          cancel: never    polite stop,     polite stop      wait for it   end it again    wait for it
                       published, torn  kill at grace    (kill at grace)  (bounded)     (once)          to end
                       down, caller
                       gets Unavailable
    restart_after_     start finishes   retire: drains   unchanged        unchanged     unchanged       unchanged
    drain(key)         then retires
    unregister(key)    cancel: never    kill             kill             wait for it   end it again    wait for it
                       published                                                       (once)
    close()            cancel           polite stop      polite stop      wait for it   end it again    wait for it
    set_max_resident   untouched        idle ones are    untouched        untouched     untouched       untouched
                                        stopped
    run_once()         not related: a one-off worker is its own slot, held by a permit until its
                       process is confirmed gone; close() waits for its start and then kills it;
                       stop, unregister and restart_after_drain never touch it
    LRU eviction       only an idle ready worker is ever evicted; the others cannot be, but every
    (internal)         state above counts toward the process cap until it is confirmed gone

Limits and what enforces them:

* Worker processes are capped (default 8). Starting one more stops the least
  recently used idle worker and waits for it to be gone; if every worker is busy
  the caller waits a little, then gets ``PoolUnavailable`` with code
  ``pool_full``. Workers that are stopping or draining still count, so the
  number of live processes never exceeds the cap by more than the retiring
  workers that a settings change leaves behind for a moment.
* Memory is read from ``/proc/<pid>/smaps_rollup`` for the launcher's whole
  process tree: when a request starts and ends, on every sweep while requests
  run, and once per heartbeat interval otherwise. Over the limit (translators
  1 GiB, other kinds 512 MiB) the worker is killed. Not available off Linux, where
  the check is skipped.
* Folder budgets (a byte cap per directory the worker may write) and the free
  space reserve on the config volume (1 GiB) are checked before a start, before
  a request and on every sweep. A worker over its folder budget is killed.
  A short reserve only refuses new starts and requests: it is often the host's
  own doing, so it never kills a worker and never counts toward the breaker.
* Heartbeats (15 s, three misses) are enforced by the session; the pool turns
  the session's death into a failure and a restart policy.
* Restarts back off exponentially. Five failures within five minutes open the
  breaker for two minutes.
* ``recycle_needed`` (the session has been handed too many distinct secrets to
  keep redacting them all) retires the worker once its requests finish.
* Standard error is scrubbed by the session for every secret value it ever
  carried. The pool adds its own log rate limit and a short tail for status
  pages. Request parameters and secrets never reach a pool log line.
* Every stop that is not an instant kill (idle, LRU, recycle, drain, close)
  asks politely first. When the polite window ends, the session hands a
  launcher-started worker to the launcher's own cleanup (its lifeline is cut
  and ``stop()`` runs), which also removes children that moved themselves into
  a new session. A bare group kill would take the launcher down before it
  could do that.
* The pool never signals a process group. Only the session knows whether the
  leader was already reaped (and its number may be someone else's), so a plain
  child is ended through ``session.close`` and a launcher or anchored worker
  through its own ``stop()``. A worker counts toward the process cap, and its
  entry stays ``stopping``, until its process is confirmed gone; one that
  survives a stop is ended again on every sweep.

Evidence, and how each decision fails closed:

* "Process gone" (``_confirmed_gone``): the leader reaped (``returncode``), and for
  an anchored worker its anchor let go, and for a launcher that it exited by itself
  and wrote a ``complete`` cleanup record; a leader or anchor somebody else reaped,
  a missing record or an overflowed status stream is "not confirmed".
* "Sample complete" (``read_process_tree_memory``): every listed process read, the
  listing itself complete (children lists readable, the ``/proc`` scan not capped
  and no record skipped), under ``max_processes``. A live process that denies the
  read, a cut listing or a reader that fails make the sample ``truncated``, which
  counts as over the limit; only a root that vanished reads as no sample.
* "Teardown complete" (``_teardown_confirmed``): only a close job or a stuck pass
  that confirmed "process gone" reaches it, and the entry reads as stopped (and
  starts anything) only when none of its workers is still being torn down.

Hosts without the subreaper launcher (any platform but Linux): a worker that detaches a
child into a new session escapes the group kill, and the pool cannot see it. Making
every such stop permanently stuck would make the pool unusable there, so the group's
end is accepted as evidence, and the gap is kept visible and bounded::

    what                         behaviour
    status()["unverified_       per key: workers ended in the last window without whole-tree
    teardowns"]                  proof, the limit, and the note "detached processes cannot be
                                 tracked on this platform" (the sandbox status says the same)
    log                          one INFO line per key, the first time it happens
    cap                          at ``unverified_limit`` (20) such workers within
                                 ``unverified_window`` (an hour) the key's starts, one-off
                                 runs included, are refused with ``unverified_limit`` and a
                                 retry time, until the oldest leaves the window
    launcher workers, runtimes   never counted: the launcher proves its tree, a runtime
    with no local process        without a process has none to detach from

Nothing forgets a teardown that is not confirmed: the pool keeps, per plugin key and
outside the entry, every worker of that key that is being torn down or is stuck
(``_tearing``), including a start that was cut loose (``_detach``). Unregistering,
registering the key again, restarting, stopping, closing and eviction all leave that
map alone, and no worker or one-off run of the key starts until it is empty. Before a
request is sent, the worker's memory is sampled again and judged, and its folders are
checked against the slot's own definition (cached for ``budget_interval``).

Accounting. A worker's process is counted, in one of these places, from the
moment it can exist until its teardown is confirmed, and it moves between them
inside a single lock hold so the count never dips:

* resident: an entry that is starting or ready (counted once per entry);
* a starting worker whose entry was removed (``_detached``), until its start
  ends and it is queued for teardown;
* retiring: draining workers of an entry;
* closing (``_closing``): every queued or running stop, kill or reap, added when
  the job is queued and taken off when the job ends;
* stuck (``_stuck``): the job ended but the process or a launcher's cleanup was
  not confirmed; ended again on each sweep and removed by the one pass that
  confirms it.

A one-off run holds a permit from before it starts until its process is
confirmed gone, or until the stuck pass that confirms it releases it.

Threading: all state is guarded by one lock. Slow work (spawn, handshake,
shutdown, reading ``/proc``, measuring folders) runs outside it. Shutdowns run
on their own short-lived threads, so ``submit``, ``wait`` and ``sweep`` never
wait for another worker to finish stopping; ``stop``, ``unregister``,
``set_max_resident`` and ``close`` do wait for the stops they started. Public
methods are safe to call from any thread.

Request context: ``submit(..., context=obj)`` attaches ``obj`` to the request
before the worker can see it. A host call handler receives a ``PoolHostCall``
whose ``context`` is that object, even when the worker's first call arrives
before ``submit`` has returned.

Runtime seam: the pool talks to a started worker through ``WorkerRuntime``, the
small surface ``WorkerSession`` already has (``start``, ``submit``, ``request``,
``close``, ``alive``, ``in_flight``, ``recycle_needed``, ``exit_code`` and
``process``). ``WorkerDefinition.runtime`` is a factory returning one; the
default builds a ``WorkerSession``. A managed container runtime (a daemon kind
such as FlareSolverr) is meant to plug in there: it supplies its own start,
health, stop and removal, reports ``process`` as None while it has no local
process (the memory sampler then skips it, and its own memory cap applies), and
reuses the pool's states, breaker, idle stop and cap unchanged. Nothing of that
exists yet.
"""

from __future__ import annotations

import enum
import logging
import math
import os
import re
import shutil
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..sandbox import LauncherProcess, SandboxApplyError, SandboxRefused, WorkerProcess, spawn_worker
from .errors import HandshakeError, RequestError, SessionError
from .frames import Codes, is_secrets
from .session import HostCall, RequestHandle, RequestResult, SessionLimits, WorkerSession

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
    "PoolHostCall",
    "PoolLimits",
    "PoolRequest",
    "PoolUnavailable",
    "UnknownWorker",
    "WorkerDefinition",
    "WorkerKilled",
    "WorkerPool",
    "WorkerRuntime",
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

# Longest polite window a stop may ask for: the shutdown frame cannot carry more.
MAX_CLOSE_GRACE = 600


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
    UNVERIFIED_LIMIT = "unverified_limit"


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
    _S.READY: frozenset({_S.DRAINING, _S.STOPPING, _S.STOPPED, _S.BACKOFF, _S.BROKEN}),
    _S.DRAINING: frozenset({_S.STARTING, _S.STOPPING, _S.STOPPED}),
    _S.STOPPING: frozenset({_S.STOPPED}),
    _S.BACKOFF: frozenset({_S.STARTING, _S.STOPPED}),
    _S.BROKEN: frozenset({_S.STARTING, _S.STOPPED}),
}
_COUNTS_AS_RESIDENT = (_S.STARTING, _S.READY)
_PATH_IN_TEXT = re.compile(r"(?<![\w:/.])/[\w.\-+@%~]+(?:/[\w.\-+@%~]*)*")


def _log_failure(message: str, error: BaseException, level: int = logging.DEBUG) -> None:
    """Log that something failed, with the exception's type and nothing else.

    A callback, a sink or a close can fail with a message (and a traceback with
    frame values) that holds a request's secret or worker output the session has
    not scrubbed. Only the fixed text and the type name are safe to log.
    """
    logger.log(level, "%s (%s)", message, type(error).__name__)


def _public_text(text) -> str:
    """Error text safe for a status page: filesystem paths removed, length bounded."""
    return _PATH_IN_TEXT.sub("<path>", str(text))[:300]


def memory_limit_for(kind: str | None) -> int:
    return TRANSLATOR_MEMORY_BYTES if kind == "translator" else DEFAULT_MEMORY_BYTES


@dataclass(frozen=True)
class FolderBudget:
    """A byte cap on a directory tree a worker may write into."""

    path: str
    max_bytes: int
    label: str = ""


class WorkerRuntime(Protocol):
    """What the pool needs from a started worker. ``WorkerSession`` is one.

    The seam for other runtimes: implement this, return it from
    ``WorkerDefinition.runtime`` and the pool supervises it with the same
    states, limits and restart policy. ``process`` may be None when there is no
    local process to sample.
    """

    process: Any
    exit_code: int | None
    recycle_needed: bool

    @property
    def alive(self) -> bool: ...

    @property
    def in_flight(self) -> int: ...

    def start(self) -> Any: ...

    def submit(self, op, params=None, *, timeout=None, max_result_bytes=None, secrets=None,
               on_progress=None) -> RequestHandle: ...

    def request(self, op, params=None, *, timeout=None, **kwargs) -> RequestResult: ...

    def close(self, grace: float = 5.0) -> None: ...


@dataclass
class PoolHostCall(HostCall):
    """A host call as the pool hands it to a handler: the session's call plus the request context."""

    context: Any = None
    key: str = ""


def _session_runtime(definition, limits, host_calls, on_event, on_stderr):
    return WorkerSession(
        definition.command,
        spawn=definition.spawn,
        cwd=definition.cwd,
        env=dict(definition.env) if definition.env else None,
        kind=definition.kind,
        kind_versions=definition.kind_versions,
        limits=limits,
        host_calls=host_calls,
        on_event=on_event,
        on_stderr=on_stderr,
        name=definition.name or definition.key,
    )


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
    # Builds the runtime object; None means a ``WorkerSession``. Called as
    # ``runtime(definition, session_limits, host_calls, on_event, on_stderr)``.
    runtime: Callable[..., WorkerRuntime] | None = None

    def memory_limit(self) -> int:
        return self.memory_limit_bytes if self.memory_limit_bytes else memory_limit_for(self.kind)

    def check(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError("a worker definition needs a key")
        if self.spawn is None and not self.command and self.runtime is None:
            raise ValueError("a worker definition needs spawn, command or runtime")


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
    # Workers of one plugin that may be ended without proof that their whole tree went
    # (hosts without the launcher) per window, before new starts are refused.
    unverified_limit: int = 20
    unverified_window: float = 3600.0

    def __post_init__(self):
        # The grace goes into a shutdown frame, which only carries whole
        # milliseconds up to a bound; refuse what the frame could not hold.
        grace = self.close_grace
        if (isinstance(grace, bool) or not isinstance(grace, (int, float)) or not math.isfinite(grace)
                or grace < 0 or grace > MAX_CLOSE_GRACE):
            raise ValueError("close_grace must be a number of seconds from 0 to %d" % MAX_CLOSE_GRACE)


@dataclass(frozen=True)
class MemorySample:
    bytes: int  # proportional set size when the kernel reports it, else RSS
    rss_bytes: int
    processes: int
    source: str
    # The tree had more processes than were read: the figure is a lower bound,
    # and the pool treats the worker as over its limit.
    truncated: bool = False


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


PARENT_MAP_LIMIT = 32768


def _children_of(pid: int) -> tuple[list[int] | None, bool]:
    """Direct children from the kernel's per-thread children lists: ``(children or None, incomplete)``.

    None means the lists are not available here (the caller scans ``/proc``
    instead). ``incomplete`` is set when a list that exists could not be read
    or parsed: the children it held are then unknown.
    """
    found: list[int] = []
    incomplete = False
    try:
        threads = os.listdir("/proc/%d/task" % pid)
    except FileNotFoundError:
        return [], False  # gone
    except OSError:
        return None, False
    readable = False
    for tid in threads:
        try:
            with open("/proc/%d/task/%s/children" % (pid, tid), "r", encoding="ascii") as handle:
                text = handle.read()
            readable = True
            found.extend(int(p) for p in text.split())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            incomplete = True
    return (found if readable else None), incomplete


def _parent_map(limit: int = PARENT_MAP_LIMIT) -> tuple[dict[int, list[int]], bool]:
    """pid -> children by scanning /proc: ``(map, complete)``. The fallback when children lists are off.

    Not complete when the listing is cut at ``limit`` entries, cannot be read, or a
    process record that is there cannot be read or understood. A process that
    vanishes during the scan is not an omission.
    """
    tree: dict[int, list[int]] = {}
    complete = True
    try:
        names = os.listdir("/proc")
    except OSError:
        return tree, False
    if len(names) > limit:
        complete = False
    for name in names[:limit]:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "r", encoding="ascii", errors="replace") as handle:
                text = handle.read()
            ppid = int(text.rsplit(")", 1)[1].split()[1])
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            complete = False
            continue
        tree.setdefault(ppid, []).append(int(name))
    return tree, complete


def _process_tree(root: int, max_processes: int) -> tuple[list[int], bool]:
    """The root and its descendants, and whether that list may be incomplete.

    Incomplete when the walk stopped at ``max_processes``, when a children list
    could not be read, or when the ``/proc`` scan used instead was cut short or
    skipped a record: a descendant could be missing from the answer.
    """
    incomplete = False
    seen = [root]
    index = 0
    fallback: dict[int, list[int]] | None = None
    while index < len(seen):
        current = seen[index]
        index += 1
        kids, partial = _children_of(current)
        if partial:
            incomplete = True
        if kids is None:
            if fallback is None:
                fallback, complete = _parent_map()
                if not complete:
                    incomplete = True
            kids = fallback.get(current, [])
        for kid in kids:
            if kid in seen:
                continue
            if len(seen) < max_processes:
                seen.append(kid)
            else:
                incomplete = True
    return seen, incomplete


def read_process_tree_memory(pid: int, max_processes: int = 256) -> MemorySample | None:
    """Memory of a process and every descendant, from ``smaps_rollup``.

    The sum uses ``Pss`` so pages shared between workers are not counted in
    full by each. A process without ``smaps_rollup`` (older kernel, or a
    hardened ``/proc``) falls back to ``VmRSS`` from ``status``. Returns None
    when the root process is gone or ``/proc`` is unavailable. A tree larger
    than ``max_processes``, or with a live process whose memory cannot be
    read, comes back with ``truncated`` set.
    """
    if not os.path.isdir("/proc/self"):
        return None
    total = 0
    rss_total = 0
    counted = 0
    source = "smaps_rollup"
    members, truncated = _process_tree(pid, max_processes)
    for member in members:
        try:
            values = _read_kb("/proc/%d/smaps_rollup" % member, "Pss", "Rss")
            used = values.get("Pss", values.get("Rss"))
            rss = values.get("Rss", used)
            if used is None:
                raise OSError("no memory figure")
        except OSError:
            # No smaps_rollup (an older kernel, or none for this process) says
            # nothing about the process being gone: ask status. Only a process
            # whose own /proc entry has vanished is not part of the tree any more.
            try:
                values = _read_kb("/proc/%d/status" % member, "VmRSS")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                # Alive (it was listed, and its entry is there) but closed to us:
                # its memory is unknown, so the total is only a floor.
                truncated = True
                continue
            used = rss = values.get("VmRSS")
            if used is None:
                # A zombie or a kernel thread has no memory to count.
                continue
            source = "status"
        total += used
        rss_total += rss
        counted += 1
    if not counted:
        # Nothing readable. If nothing was seen at all the process is gone; if
        # live processes denied the read, the answer is "unknown", not "none".
        if not truncated:
            return None
        return MemorySample(bytes=0, rss_bytes=0, processes=0, source=source, truncated=True)
    return MemorySample(bytes=total, rss_bytes=rss_total, processes=counted, source=source, truncated=truncated)


def folder_usage(path: str, max_entries: int = 100_000) -> tuple[int, bool]:
    """Allocated bytes under ``path`` without following links.

    Returns ``(bytes, truncated)``; ``truncated`` means the walk stopped at
    ``max_entries`` and the caller should treat the folder as over any budget.
    So does a subtree that cannot be read (a directory without permission to
    list it or to stat what is in it): its content is unknown, so the scan is
    incomplete. A missing folder is empty.
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
                    except FileNotFoundError:
                        continue
                    except OSError:
                        return total, True
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return total, True
    return total, False


_DELETED = " (deleted)"


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def deleted_open_usage(pids, folders) -> tuple[dict, bool]:
    """Bytes held by files that were unlinked but are still open, per folder: ``({folder: bytes}, incomplete)``.

    A worker can create a file in its folder, unlink it and keep writing through
    the open descriptor: the bytes are on disk and no directory scan finds them.
    For every process given, each descriptor (and each mapping) whose link target
    ends in "(deleted)" and whose original path lies under a folder, or that sits
    on the folder's filesystem, is stat'ed through its ``/proc`` link and counted
    by allocated size, once per (device, inode) however many processes hold it.

    Incomplete when the descriptors of a live process cannot be listed or read, or
    a candidate cannot be stat'ed: what it holds is then unknown. A process that
    vanished, or a descriptor closed meanwhile, is not an omission.
    """
    roots = []
    for folder in folders:
        real = os.path.realpath(folder)
        try:
            device = os.stat(real).st_dev
        except OSError:
            device = None
        roots.append((folder, {folder, real}, device))
    usage = {folder: 0 for folder, _names, _dev in roots}
    seen: set = set()
    incomplete = False

    def account(path, link):
        nonlocal incomplete
        try:
            info = os.stat(link)
        except (FileNotFoundError, ProcessLookupError):
            return
        except OSError:
            incomplete = True
            return
        if not stat.S_ISREG(info.st_mode):
            return
        size = getattr(info, "st_blocks", 0) * 512 or info.st_size
        identity = (info.st_dev, info.st_ino)
        if identity in seen:
            return
        mine = [folder for folder, names, device in roots
                if any(_under(path, name) for name in names) or (device is not None and device == info.st_dev)]
        if mine:
            seen.add(identity)
            for folder in mine:
                usage[folder] += size

    for pid in pids:
        directory = "/proc/%d/fd" % pid
        try:
            descriptors = os.listdir(directory)
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = True
            continue
        for name in descriptors:
            link = "%s/%s" % (directory, name)
            try:
                target = os.readlink(link)
            except FileNotFoundError:
                continue
            except OSError:
                incomplete = True
                continue
            if target.endswith(_DELETED):
                account(target[: -len(_DELETED)], link)
        # Deleted files that are only mapped (and written through the mapping).
        try:
            with open("/proc/%d/maps" % pid, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = True
            continue
        for line in lines:
            if not line.endswith(_DELETED):
                continue
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            path = parts[5][: -len(_DELETED)]
            if any(_under(path, name) for _folder, names, _dev in roots for name in names):
                account(path, "/proc/%d/map_files/%s" % (pid, parts[0]))
    return usage, incomplete


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
        self.session: WorkerRuntime | None = None
        self.started_at = now
        self.last_used = now
        self.last_sample_at = -math.inf
        self.pending = 0  # submits in progress: the session does not know about them yet
        self.active = 0  # requests submitted through the pool and not yet finished
        self.contexts: dict[int, Any] = {}
        self.runtime_error: BaseException | None = None
        self.kill_reason: tuple[str, str] | None = None
        self.killed_busy = False
        self.memory_bytes = 0
        self.memory_truncated = False
        self.peak_bytes = 0
        self.ephemeral = ephemeral
        self.closing = False
        # Why a start was cut short ("stopped"), when it was.
        self.cancel_reason: str | None = None
        # The entry this worker belongs to (None for a one-off run).
        self.entry: _Entry | None = None
        # Only one thread ends the process at a time, and once it has, nobody
        # signals it again unless the pool is retrying a worker it could not confirm gone.
        self.end_lock = threading.Lock()
        self.ended = False
        self.generation = 0
        self.last_budget_at = -math.inf
        # The folder-budget answer for this worker's own definition, and when it was taken.
        self.last_budget_check = -math.inf
        self.budget_problem: tuple[str, str] | None = None
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
        # The slot being started for this entry, until the start finishes.
        self.starting: _Slot | None = None
        self.starts = 0
        self.crashes = 0
        self.last_peak_bytes = 0
        # Raised by every restart_after_drain; a start that began under an older one is stale.
        self.generation = 0


class PoolRequest:
    """A request in flight through the pool. ``wait`` returns the result or raises."""

    def __init__(self, pool, entry, slot, handle: RequestHandle):
        self._pool = pool
        self._entry = entry
        self._slot = slot
        self._handle = handle
        self._finished = False
        self._finish_lock = threading.Lock()
        self._settled_event = threading.Event()
        self.context = None

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

    def _settled(self):
        self._finish(getattr(self._handle, "error", None))

    def _finish(self, error):
        with self._finish_lock:
            first = not self._finished
            self._finished = True
        if not first:
            # The completion callback may be settling the books on another thread:
            # a caller that gets its result back must find them settled.
            self._settled_event.wait(30)
            return
        try:
            self._pool._after_request(self._entry, self._slot, error, self._handle.id)
        finally:
            self._settled_event.set()


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
        self._closing = 0  # polite and hard stops queued or running
        self._close_threads: set = set()
        # Workers whose process could not be confirmed gone after a stop. They
        # keep counting toward the process cap, and the sweep ends them again.
        # Workers being started for an entry that was unregistered or whose pool was
        # closed meanwhile: still a process in the making, counted until the start
        # ends and its teardown is queued.
        self._detached: set = set()
        # Plugin key -> its workers whose teardown has been queued and is not yet
        # confirmed gone (running, or stuck), whatever their generation. It lives
        # here, not on the entry: unregistering and registering the key again must
        # not forget a survivor. Nothing of that key starts, and its entry does not
        # read as stopped, until its set is empty.
        self._tearing: dict[str, set] = {}
        # Plugin key -> when (pool clock) its workers were ended without proof that the
        # whole tree went (no subreaper launcher here): see ``_note_unverified``.
        self._unverified: dict[str, deque] = {}
        self._unverified_logged: set = set()
        # One-off workers whose session is still being created or started. Close
        # waits for them, and none begins once the pool is closing.
        self._ephemeral_starting: set = set()
        self._stuck: list = []
        self._stuck_lock = threading.Lock()
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
            if entry.starting is not None:
                # Its start is still running and will find the entry gone.
                self._detach(entry.starting)
            if entry.slot is not None:
                self._queue_close(entry.slot, None, "kill")
                entry.slot = None
            for slot in entry.retiring:
                self._queue_close(slot, None, "kill")
            entry.retiring = []
            entry.state = WorkerState.STOPPED
            self._cond.notify_all()
        self._flush(wait=True)
        self._wait_detached()
        if self._stuck:
            self._retry_stuck()

    def _detach(self, slot):
        """Cut a start loose from its entry. Caller holds the lock.

        The worker may already be spawning, so from now on its plugin key has an
        unconfirmed teardown: nothing of that key starts beside it, even if the key
        is registered again, until the start has ended and its teardown is confirmed.
        """
        self._detached.add(slot)
        self._tearing.setdefault(slot.definition.key, set()).add(slot)

    def _wait_ephemeral_starts(self):
        """Wait (bounded) for one-off workers that are still being started."""
        deadline = time.monotonic() + self.limits.close_grace + 30.0
        with self._cond:
            while self._ephemeral_starting and time.monotonic() < deadline:
                self._cond.wait(0.2)

    def _wait_start_cancelled(self, slot):
        """Wait (bounded) until a start that was cancelled has ended and queued its teardown."""
        deadline = time.monotonic() + self.limits.close_grace + 30.0
        with self._cond:
            while slot in self._detached and time.monotonic() < deadline:
                self._cond.wait(0.2)
        self._flush(wait=True)

    def _wait_detached(self):
        """Wait (bounded) for starts that were cut off to end, and for the teardown they queued."""
        deadline = time.monotonic() + self.limits.close_grace + 30.0
        with self._cond:
            while self._detached and time.monotonic() < deadline:
                self._cond.wait(0.2)
        self._flush(wait=True)

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
            entry.generation += 1
            if entry.state == WorkerState.READY and entry.slot is not None:
                self._retire(entry, "settings changed")
            # A start under way keeps the definition it began with; _start_slot
            # sees that it is stale and retires it once it is up.
        self._flush()

    def set_max_resident(self, count: int) -> None:
        """Change the resident cap (the user setting). Idle surplus workers stop now."""
        with self._cond:
            self._max_resident = max(1, int(count))
            surplus = self._process_count() - self._max_resident - self._closing
            if surplus > 0:
                for entry in self._idle_by_age(None)[:surplus]:
                    self._begin_stop(entry, "resident cap lowered")
        self._flush(wait=True)

    @property
    def max_resident(self) -> int:
        return self._max_resident

    # ----------------------------------------------------------------- queries

    def state(self, key: str) -> WorkerState:
        with self._cond:
            return self._public_state(self._entry(key))

    def status(self) -> dict:
        """A snapshot for status pages.

        No request parameters or secrets. Filesystem paths are removed from
        ``last_error``. ``recent_stderr`` is the plugin's own output with
        secrets scrubbed by the session; it is the plugin's to keep clean.
        """
        now = self._clock()
        with self._cond:
            workers = {}
            for key, entry in self._entries.items():
                slot = entry.slot
                session = slot.session if slot else None
                until = max(entry.backoff_until, entry.open_until)
                workers[key] = {
                    "state": self._public_state(entry).value,
                    "kind": (slot.definition if slot else entry.definition).kind,
                    "pid": self._live_pid(session),
                    "in_flight": session.in_flight if session else 0,
                    "memory_bytes": slot.memory_bytes if slot else 0,
                    "peak_bytes": slot.peak_bytes if slot else entry.last_peak_bytes,
                    "memory_limit_bytes": (slot.definition if slot else entry.definition).memory_limit(),
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
                "processes": self._process_count(),
                "stuck": len(self._stuck),
                "unverified_teardowns": self._unverified_status(now),
                "workers": workers,
            }

    def _unverified_status(self, now) -> dict:
        """Per key: workers ended in the last window without whole-tree proof. Lock held."""
        result = {}
        for key, stamps in self._unverified.items():
            self._prune_unverified(stamps, now)
            if stamps:
                result[key] = {
                    "count": len(stamps),
                    "limit": self.limits.unverified_limit,
                    "note": "detached processes cannot be tracked on this platform",
                }
        return result

    # ---------------------------------------------------------------- requests

    def submit(self, key: str, op: str, params=None, *, timeout=None, max_result_bytes=None,
               secrets=None, on_progress=None, context=None) -> PoolRequest:
        """Send a request to the worker for ``key``, starting it if needed.

        ``secrets`` travel in the request frame only; they are never stored in
        the definition or logged. Raises ``PoolUnavailable`` (backoff, open
        breaker, full pool, low disk, budget), ``PoolClosed``, ``UnknownWorker``,
        ``SessionBusy`` (the worker is at its in-flight limit) or a start error.

        ``context`` is any object the caller wants its host call handlers to
        see for this request (a job record, say). It is attached before the
        worker can issue a call, so the very first call already carries it as
        ``PoolHostCall.context``. It is dropped when the request finishes.
        """
        self._check_request(timeout, secrets)
        last_error: BaseException | None = None
        for _attempt in range(3):
            entry, slot = self._lease(key)
            try:
                self._preflight_running(entry, slot)
                self._sample(slot)
                problem = self._memory_problem(slot, slot.definition.memory_limit())
                if problem is not None:
                    # Over its limit, or too big to measure, before it has been given
                    # anything: end it and send nothing.
                    self._kill_current(entry, slot, problem, requested=True)
                    raise WorkerKilled(problem[1], code=problem[0])
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
                slot.active += 1
                slot.last_used = self._clock()
                if context is not None:
                    slot.contexts[handle.id] = context
                self._cond.notify_all()
            request = PoolRequest(self, entry, slot, handle)
            register = getattr(handle, "add_done_callback", None)
            if callable(register):
                # The pool's books are settled when the request really finishes,
                # not when somebody happens to call wait().
                register(request._settled)
            return request
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _check_request(timeout, secrets) -> None:
        """Refuse what the session would refuse, before a worker is started for it."""
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be a finite number of seconds, not negative")
        if secrets is not None and not is_secrets(secrets):
            raise ValueError("secrets must be a flat mapping of text names to non-empty text values")

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
                 max_result_bytes=None, secrets=None, on_progress=None, context=None) -> RequestResult:
        """Run one request on a throwaway worker that is not part of the pool.

        For draft ``options`` and ``test`` calls on settings that are not
        saved yet. The worker obeys the same memory, budget and disk rules and
        is always stopped afterwards. It never touches the breaker. At most
        ``max_oneshot`` run at once.
        """
        definition.check()
        self._check_request(timeout, secrets)
        with self._cond:
            self._check_open()
            self._check_key_free(definition.key)
        if not self._oneshot.acquire(timeout=self.limits.acquire_timeout):
            raise PoolUnavailable("too many one-off runs in progress", code=PoolCodes.POOL_FULL, retry_after=1.0)
        slot = _Slot(definition, self._clock(), self.limits, ephemeral=True)
        try:
            problem = self._violation(definition, None)
            if problem:
                raise PoolUnavailable(problem[1], code=problem[0])
            with self._cond:
                self._check_open()
                # The check at the top was made before the permit was waited for; a
                # resident of this key may have become stuck since.
                self._check_key_free(definition.key)
                self._ephemeral.add(slot)
                self._ephemeral_starting.add(slot)
            self._ensure_sampler()
            try:
                self._open_session(slot)
            except PoolError:
                raise
            except BaseException as error:
                raise self._start_error(slot, error)
            finally:
                with self._cond:
                    self._ephemeral_starting.discard(slot)
                    self._cond.notify_all()
            with self._cond:
                self._check_open()
            # Starting the worker can fill its folders or its memory; the checks that
            # held before it started are repeated before it is given any work.
            problem = self._violation(definition, None, tree=slot)
            if problem:
                raise PoolUnavailable(problem[1], code=problem[0])
            self._sample(slot)
            problem = self._memory_problem(slot, definition.memory_limit())
            if problem is not None:
                slot.kill_reason = problem
                raise WorkerKilled(problem[1], code=problem[0])
            try:
                with self._cond:
                    slot.pending += 1
                try:
                    handle = slot.session.submit(
                        op, params, timeout=timeout, max_result_bytes=max_result_bytes,
                        secrets=secrets, on_progress=on_progress)
                except BaseException:
                    with self._cond:
                        slot.pending -= 1
                    raise
                with self._cond:
                    slot.pending -= 1
                    if context is not None:
                        slot.contexts[handle.id] = context
                    self._cond.notify_all()
                result = self._wait_watched(slot, handle)
                # One more reading once it is done, so its peak is not lost. As for a
                # resident worker that finishes and then proves too big, the answer it
                # produced stands; the worker is torn down either way.
                self._sample(slot)
                return result
            except (SessionError, RequestError) as error:
                raise self._translate(slot, error)
        finally:
            gone = True
            if slot.session is not None:
                with self._cond:
                    # Counted as a teardown of this key before anything is done to it, and
                    # until it is confirmed: nothing of the key starts beside a survivor.
                    self._tearing.setdefault(definition.key, set()).add(slot)
                try:
                    self._polite_close(slot, 1.0)
                except Exception as error:
                    _log_failure("one-off worker close failed", error)
                gone = self._confirmed_gone(slot)
            with self._cond:
                self._ephemeral.discard(slot)
                if gone:
                    self._teardown_confirmed(slot, None)
                else:
                    # The permit stays taken until the process is really gone.
                    self._add_stuck(slot, None, self._oneshot.release)
            if gone:
                self._oneshot.release()

    def _check_key_free(self, key):
        """Refuse a start of ``key`` while a worker of it is being torn down, or the cap is spent. Lock held."""
        if self._tearing.get(key):
            raise PoolUnavailable("the previous worker of %s is still being stopped" % key,
                                  code=PoolCodes.UNAVAILABLE, retry_after=1.0)
        wait = self._unverified_wait(key)
        if wait:
            raise PoolUnavailable(
                "%s has had %d workers ended without whole-tree cleanup within the last hour; "
                "detached processes cannot be tracked on this platform, so starts are paused"
                % (key, self.limits.unverified_limit), code=PoolCodes.UNVERIFIED_LIMIT, retry_after=wait)

    def _wait_watched(self, slot, handle):
        """Wait for a one-off request, checking its worker's memory and folders as time passes.

        The sampler thread does the same for resident workers; a one-off run
        carries its own checks so that it is policed even when the pool runs
        without one.
        """
        interval = max(0.05, self.limits.heartbeat_interval)
        while True:
            try:
                return handle.wait(interval)
            except TimeoutError:
                self._sweep_ephemeral(slot, self._clock())

    # --------------------------------------------------------------- operations

    def stop(self, key: str) -> None:
        """Stop the plugin's workers on purpose: the current one, every draining one, a start in progress.

        A start that has not finished is cancelled: its worker is never
        published and its caller gets ``PoolUnavailable``. Busy workers are
        stopped politely and killed when the grace ends. Returns when the
        teardown is over (bounded); a worker in backoff stays in backoff.
        """
        with self._cond:
            entry = self._entry(key)
            starting = entry.starting
            if starting is not None and starting not in self._detached:
                starting.cancel_reason = "stopped"
                self._detach(starting)
            if entry.state == WorkerState.READY and entry.slot is not None:
                self._begin_stop(entry, "stopped")
            retiring, entry.retiring = entry.retiring, []
            for slot in retiring:
                self._queue_close(slot, entry, "stop")
            if retiring and entry.state == WorkerState.DRAINING and entry.slot is None and starting is None:
                self._set_state(entry, WorkerState.STOPPING, "stopped")
            self._cond.notify_all()
        self._flush(wait=True)
        if starting is not None:
            self._wait_start_cancelled(starting)
        if self._stuck:
            self._retry_stuck()

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
                if entry.starting is not None:
                    self._detach(entry.starting)
                if entry.slot is not None:
                    self._queue_close(entry.slot, None, "close")
                    entry.slot = None
                for slot in entry.retiring:
                    self._queue_close(slot, None, "close")
                entry.retiring = []
                entry.state = WorkerState.STOPPED
            self._cond.notify_all()
        self._wait_ephemeral_starts()
        with self._cond:
            # Now that every one-off start has ended, each has its session (or none).
            for slot in list(self._ephemeral):
                self._queue_close(slot, None, "kill")
        self._flush(wait=True)
        self._wait_detached()
        if self._stuck:
            self._retry_stuck()
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
        for entry, slot in current:
            self._sweep_current(entry, slot, now)
        for entry, slot in retiring:
            self._sweep_retiring(entry, slot, now)
        for slot in ephemeral:
            self._sweep_ephemeral(slot, now)
        if self._stuck:
            self._retry_stuck()
        with self._cond:
            for entry in self._entries.values():
                if entry.state == WorkerState.DRAINING and not entry.retiring and entry.slot is None:
                    self._set_state(entry, WorkerState.STOPPED, "drained")
            self._cond.notify_all()
        self._flush()

    def _sweep_current(self, entry, slot, now):
        with self._cond:
            if entry.slot is not slot or entry.state != WorkerState.READY:
                return
            if not slot.session.alive:
                idle = slot.active == 0 and slot.pending == 0
                self._handle_death(entry, slot, self._death_reason(slot), idle=idle)
                return
        self._sample_if_due(slot, now)
        definition = slot.definition
        problem = None
        problem = self._memory_problem(slot, definition.memory_limit())
        if problem is None:
            problem = self._budget_problem(slot, now, definition)
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
        problem = self._memory_problem(slot, slot.definition.memory_limit())
        if problem is not None:
            slot.kill_reason = (problem[0], "retiring worker: " + problem[1])
            with self._cond:
                if slot in entry.retiring:
                    entry.retiring.remove(slot)
                self._queue_close(slot, None, "kill")
            return
        # A draining worker still writes: its own folders, as its own settings named them.
        problem = self._slot_budget_problem(slot, now)
        if problem is not None:
            slot.kill_reason = problem
            with self._cond:
                if slot in entry.retiring:
                    entry.retiring.remove(slot)
                    self._queue_close(slot, None, "kill")
            return
        with self._cond:
            if slot in entry.retiring and not slot.busy():
                entry.retiring.remove(slot)
                self._queue_close(slot, None, "stop")

    def _sweep_ephemeral(self, slot, now):
        if slot.session is None or not slot.session.alive:
            return
        self._sample_if_due(slot, now)
        problem = self._memory_problem(slot, slot.definition.memory_limit())
        if problem is not None:
            slot.kill_reason = problem
        else:
            problem = self._slot_budget_problem(slot, now)
            if problem is None:
                return
            slot.kill_reason = problem
        self._terminate_now(slot)

    def _slot_budget_problem(self, slot, now):
        """The folder budget problem of a worker that is not the entry's current one, or None.

        Checked against the slot's own definition and no more often than
        ``budget_interval``.
        """
        if now - slot.last_budget_at < self.limits.budget_interval:
            return None
        slot.last_budget_at = now
        return self._budget_problem(None, now, definition=slot.definition, force=True, tree=slot)

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
                self._flush()
                with self._cond:
                    self._check_open()
                    entry = self._entry(key)
                    now = self._clock()
                    state = entry.state
                    if state == WorkerState.READY:
                        slot = entry.slot
                        if slot is None or not slot.session.alive:
                            # Found dead before any request used it: restart now, count it once.
                            idle = slot is None or (slot.active == 0 and slot.pending == 0)
                            self._handle_death(entry, slot, self._death_reason(slot), idle=idle)
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
                    if self._tearing.get(key):
                        # A worker of this plugin is still being torn down (or stuck):
                        # a new one does not start beside it.
                        self._wait(deadline, "the previous worker is still being stopped",
                                   code=PoolCodes.UNAVAILABLE)
                        continue
                    self._check_key_free(key)
                    if not self._claim_capacity(entry):
                        self._wait(deadline, "no free worker slot", code=PoolCodes.POOL_FULL)
                        continue
                    self._set_state(entry, WorkerState.STARTING, "request")
                    entry.starts += 1
                    definition = entry.definition
                    slot = _Slot(definition, now, self.limits)
                    slot.entry = entry
                    slot.generation = entry.generation
                    entry.starting = slot
                break
        finally:
            self._flush()
        return self._start_slot(entry, slot)

    def _wait(self, deadline, why, code=PoolCodes.UNAVAILABLE):
        if self._closes:
            return  # stops queued by this very pass have to run before there is anything to wait for
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise PoolUnavailable(why, code=code, retry_after=1.0)
        self._cond.wait(min(remaining, 0.5))

    def _start_slot(self, entry, slot) -> tuple[_Entry, _Slot]:
        error: BaseException | None = None
        refusal: tuple[str, str] | None = None
        try:
            # The definition this worker is being started under, not whatever the
            # entry holds by now.
            refusal = self._violation(slot.definition, None, fresh=True)
            if refusal is None:
                self._open_session(slot)
        except BaseException as caught:
            error = caught
        with self._cond:
            if entry.starting is slot:
                entry.starting = None
            # Taken off the detached list and queued for teardown in this one step,
            # so the process never stops being counted in between.
            detached = slot in self._detached
            self._detached.discard(slot)
            if self._closed or detached:
                if slot.session is not None:
                    self._queue_close(slot, None, "kill")
                else:
                    # Nothing was ever spawned: nothing to wait for.
                    self._teardown_confirmed(slot, None)
                if entry.state == WorkerState.STARTING:
                    self._set_state(entry, WorkerState.STOPPED, "pool closed")
                self._cond.notify_all()
                if self._closed:
                    failed = PoolClosed("the worker pool is closed")
                elif slot.cancel_reason == "stopped":
                    failed = PoolUnavailable("worker %s was stopped while it was starting" % slot.definition.key,
                                             code=PoolCodes.UNAVAILABLE)
                else:
                    failed = UnknownWorker("worker %s was removed while it was starting" % slot.definition.key)
            elif refusal is not None:
                entry.last_error = _public_text(refusal[1])
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
                if slot.generation != entry.generation:
                    # New settings arrived while this worker was starting. It serves
                    # the request that asked for it, then drains; the next one starts fresh.
                    self._retire(entry, "settings changed during start")
                self._cond.notify_all()
                failed = None
        self._flush()
        if failed is not None:
            raise failed
        return entry, slot

    def _open_session(self, slot):
        definition = slot.definition
        key = definition.key
        if slot.ephemeral:
            # Nothing is spawned for a one-off run once the pool is closing; close()
            # waits for the ones that got past this point.
            with self._cond:
                self._check_open()
        limits = definition.session_limits or SessionLimits(
            heartbeat_interval=self.limits.heartbeat_interval,
            heartbeat_timeout=self.limits.heartbeat_interval * self.limits.heartbeat_misses,
        )
        host_calls = {name: self._wrap_host_call(slot, name, handler)
                      for name, handler in definition.host_calls.items()}
        factory = definition.runtime or _session_runtime
        session = factory(
            definition,
            limits,
            host_calls,
            lambda name, payload, _key=key: self._emit(_key, name, payload),
            slot.stderr_sink(key),
        )
        slot.session = session
        session.start()

    def _wrap_host_call(self, slot, key, handler):
        def wrapped(call):
            context = self._context_for(slot, call.request_id)
            return handler(PoolHostCall(call.name, call.params, call.request_id, call.session,
                                        context=context, key=key))

        return wrapped

    def _context_for(self, slot, request_id):
        """The context ``submit`` attached to this request.

        A worker can call the host the moment it gets its request, before
        ``submit`` has returned and the pool has been told the request id, so
        the handler waits (briefly) while a submit is still in progress.
        """
        if request_id is None:
            return None
        deadline = time.monotonic() + 5.0
        with self._cond:
            while request_id not in slot.contexts and slot.pending > 0 and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.2))
            return slot.contexts.get(request_id)

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
        if isinstance(error, SandboxRefused):
            return PoolUnavailable(str(error), code=PoolCodes.SANDBOX_REFUSED)
        if isinstance(error, SandboxApplyError):
            return PoolUnavailable("the sandbox could not be applied: %s" % error, code=PoolCodes.SANDBOX_BLOCKED)
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

    def _process_count(self) -> int:
        """Live worker processes: resident, draining and the ones still being stopped."""
        retiring = sum(len(entry.retiring) for entry in self._entries.values())
        return self._resident_count() + retiring + self._closing + len(self._stuck) + len(self._detached)

    def _idle_by_age(self, skip) -> list[_Entry]:
        idle = [
            entry for entry in self._entries.values()
            if entry is not skip and entry.state == WorkerState.READY and entry.slot is not None
            and not entry.slot.busy()
        ]
        idle.sort(key=lambda e: e.slot.last_used)
        return idle

    def _claim_capacity(self, entry) -> bool:
        """Is there room for one more process? If not, stop idle LRU workers to make some.

        Workers already stopping or draining count: their processes are still
        alive. Stops already under way are not repeated, so the caller just
        waits for them. False means the caller has to wait.
        """
        over = self._process_count() + 1 - self._max_resident
        if over <= 0:
            return True
        evict = over - self._closing
        if evict > 0:
            idle = self._idle_by_age(entry)
            if len(idle) >= evict:
                for victim in idle[:evict]:
                    self._begin_stop(victim, "evicted for %s" % self._key_of(entry))
        return False

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

    def _record_failure(self, entry, reason, idle=False):
        """Count a failure and pick the next state.

        ``idle``: a worker that died while nothing used it. It is counted (a
        worker that keeps dying still opens the breaker) but not backed off, so
        the next request simply starts a new one.
        """
        now = self._clock()
        limits = self.limits
        entry.failures.append(now)
        while entry.failures and now - entry.failures[0] > limits.breaker_window:
            entry.failures.popleft()
        reason = _public_text(reason)
        entry.last_error = reason
        entry.crashes += 1
        count = len(entry.failures)
        if count >= limits.breaker_threshold:
            entry.open_until = now + limits.breaker_open
            self._set_state(entry, WorkerState.BROKEN, reason)
        elif idle and entry.state == WorkerState.READY:
            self._set_state(entry, WorkerState.STOPPED, reason)
        else:
            delay = min(limits.backoff_cap, limits.backoff_base * (2 ** (count - 1)))
            entry.backoff_until = now + delay
            self._set_state(entry, WorkerState.BACKOFF, reason)

    def _handle_death(self, entry, slot, reason, idle=False):
        """The current worker ended without being asked to. Caller holds the lock."""
        if slot is not None and entry.slot is slot:
            entry.slot = None
        if slot is not None:
            entry.last_peak_bytes = slot.peak_bytes
            self._queue_close(slot, None, "reap")
        if entry.state == WorkerState.READY:
            self._record_failure(entry, reason, idle=idle)
        self._cond.notify_all()

    def _death_reason(self, slot) -> str:
        if slot is None:
            return "worker went away"
        if slot.kill_reason:
            return slot.kill_reason[1]
        code = slot.session.exit_code if slot.session is not None else None
        return "worker exited unexpectedly" + (" (exit code %s)" % code if code is not None else "")

    def _after_request(self, entry, slot, error, request_id=None):
        """A request finished. Settle the worker's health and re-sample its memory."""
        self._sample(slot)
        limit = slot.definition.memory_limit()
        with self._cond:
            slot.last_used = self._clock()
            slot.active = max(0, slot.active - 1)
            slot.contexts.pop(request_id, None)
            problem = self._memory_problem(slot, limit)
            if problem is not None and slot.kill_reason is None:
                slot.kill_reason = problem
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

    def _violation(self, definition, holder, fresh=False, tree=None) -> tuple[str, str] | None:
        """Why this worker must not start or take a request, or None."""
        free = self._free_space()
        if definition.writes_data and free is not None and free < self.limits.config_reserve_bytes:
            return (PoolCodes.DISK_RESERVE,
                    "free space on the config volume (%d MiB) is under the %d MiB reserve" % (
                        free // MIB, self.limits.config_reserve_bytes // MIB))
        return self._budget_problem(holder, self._clock(), definition, force=fresh or holder is None, tree=tree)

    def _budget_problem(self, holder, now, definition, force=False, tree=None):
        """The folder-budget problem of ``definition``, cached on ``holder`` (a slot) when given.

        ``tree`` is the slot whose process tree is also searched for files that were
        unlinked but are still held open (and so still use the folder's disk).
        """
        if not definition.budgets:
            return None
        if holder is not None and not force and now - holder.last_budget_check < self.limits.budget_interval:
            return holder.budget_problem
        problem = None
        held, held_incomplete = self._deleted_open_usage(tree or holder, definition.budgets)
        for budget in definition.budgets:
            used, truncated = folder_usage(budget.path, self.limits.budget_max_entries)
            used += held.get(budget.path, 0)
            truncated = truncated or held_incomplete
            if truncated or used > budget.max_bytes:
                problem = (PoolCodes.FOLDER_BUDGET, "%s is over its %d MiB budget" % (
                    budget.label or "a worker folder", budget.max_bytes // MIB))
                break
        if holder is not None:
            holder.last_budget_check = now
            holder.budget_problem = problem
        return problem

    def _deleted_open_usage(self, slot, budgets):
        """``deleted_open_usage`` for the worker's whole tree; ``({}, False)`` when there is no tree to look at."""
        session = getattr(slot, "session", None)
        process = session.process if session is not None else None
        if process is None or process.returncode is not None or not os.path.isdir("/proc/self/fd"):
            return {}, False
        pids, incomplete = _process_tree(process.pid, 256)
        usage, partial = deleted_open_usage(pids, [budget.path for budget in budgets])
        return usage, incomplete or partial

    def _preflight_running(self, entry, slot):
        """Before a request on a running worker: disk reserve and folder budgets."""
        problem = self._violation(slot.definition, slot)
        if problem is None:
            return
        if problem[0] != PoolCodes.DISK_RESERVE:
            # A worker over its folder budget is ended. A short disk only refuses work.
            self._kill_current(entry, slot, problem, requested=True)
        raise PoolUnavailable(problem[1], code=problem[0])

    def _kill_current(self, entry, slot, problem, requested=False):
        with self._cond:
            # The decision was taken outside the lock; the worker may have been
            # retired, replaced or torn down since. Only a slot the pool still
            # holds is killed, and by the path that fits where it is now.
            if not (entry.slot is slot or slot in entry.retiring or slot.closing):
                return
            if slot.kill_reason is None:
                slot.kill_reason = problem
            slot.killed_busy = slot.busy()
            if slot in entry.retiring:
                entry.retiring.remove(slot)
                self._queue_close(slot, None, "kill")
            elif entry.slot is slot and entry.state == WorkerState.READY:
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
        if process is None or process.returncode is not None:
            # Nothing local, or a leader that was reaped: its number may belong to someone else now.
            return
        try:
            sample = self._memory_reader(process.pid)
        except Exception:
            # A reading that failed is not a reading of zero.
            sample = MemorySample(bytes=0, rss_bytes=0, processes=0, source="error", truncated=True)
        now = self._clock()
        with self._cond:
            slot.last_sample_at = now
            if sample is not None:
                slot.memory_truncated = sample.truncated
                slot.memory_bytes = sample.bytes
                if sample.truncated:
                    # Only part of the tree was read: that figure is a floor, not
                    # the total, so the worker counts as over any limit.
                    slot.memory_bytes = max(sample.bytes, slot.definition.memory_limit() + 1)
                slot.peak_bytes = max(slot.peak_bytes, slot.memory_bytes)

    @staticmethod
    def _memory_problem(slot, limit):
        """``(code, text)`` when the last sample puts the worker over ``limit``, else None."""
        if slot.memory_bytes <= limit:
            return None
        if slot.memory_truncated:
            return (PoolCodes.MEMORY_LIMIT,
                    "worker runs more processes than can be measured, counted as over its %d MiB limit" % (
                        limit // MIB))
        return (PoolCodes.MEMORY_LIMIT, "worker used %d MiB, over its %d MiB limit" % (
            slot.memory_bytes // MIB, limit // MIB))

    def _sample_if_due(self, slot, now):
        if slot.busy() or now - slot.last_sample_at >= self.limits.heartbeat_interval:
            self._sample(slot)

    # ---------------------------------------------------- closing and killing

    def _queue_close(self, slot, entry, mode, grace=None):
        """Schedule ending a session. Runs in ``_flush``, outside the lock.

        Modes: ``stop`` and ``close`` ask politely first; ``kill`` does not;
        ``reap`` tidies a worker whose session already failed. Every one counts
        toward the process cap from the moment it is queued, in the same step
        that takes the slot off its entry, until its teardown is confirmed (or
        it moves to the stuck list): a session that failed may still have a
        process, and a launcher that has not finished its cleanup still has a tree.
        """
        if slot.closing:
            return
        slot.closing = True
        self._closing += 1
        if (slot.entry or entry) is not None:
            self._tearing.setdefault(slot.definition.key, set()).add(slot)
        self._closes.append((slot, entry, mode, self.limits.close_grace if grace is None else grace))

    def _live_pid(self, session):
        process = session.process if session is not None else None
        if process is None or process.returncode is not None:
            return None
        return process.pid

    def _confirmed_gone(self, slot) -> bool:
        """Whether the worker's process and everything it started are certainly gone.

        Never polls or signals: a leader the session or ``stop()`` has reaped
        reads as gone, one that is still unreaped does not, and a runtime with
        no local process is gone once it says it is not alive. A sandbox
        launcher is more than its exit: it ends the worker's whole tree, and
        only a launcher that left on its own without reporting a surviving
        descendant says that worked (see ``_cleanup_confirmed``). The pool holds
        on to the worker's slot until this is true.
        """
        session = slot.session
        if session is None:
            return True
        process = session.process
        if process is None:
            return not session.alive
        if process.returncode is None:
            return False
        # The session says whether it ended the group itself; a runtime that does not
        # say is trusted on its process alone.
        if getattr(session, "teardown_confirmed", True) is not True:
            return False
        return self._cleanup_confirmed(process)

    @staticmethod
    def _cleanup_confirmed(process) -> bool:
        """A reaped launcher vouches for its tree only if it exited by itself and said ``complete``.

        The launcher writes a structured cleanup record on its status pipe when
        its tree cleanup ends: ``complete``, or ``survivors`` when it gave up. A
        launcher that was killed from outside (as ``stop()`` does as a last
        resort), or that left without a record, never said its cleanup worked.
        Descendants that moved into new sessions are out of reach of everything
        but that cleanup, so none of those cases may release a worker's place in
        the pool.
        """
        if isinstance(process, WorkerProcess) and (
                process._anchor is not None or process._anchor_lost or process._leader_lost):
            # An anchored worker's group is ended when its anchor is let go; a leader or
            # anchor that somebody else reaped leaves a group nobody can vouch for.
            return False
        if not isinstance(process, LauncherProcess):
            return True
        if process.returncode < 0:
            return False
        try:
            return process.launcher_cleanup() == "complete"
        except Exception:
            return False

    def _end_process(self, slot, grace, again=False):
        """End a launcher or anchored worker's whole tree, once.

        A launcher process first loses its lifeline: the launcher then kills
        the whole worker tree itself, including children that moved into a
        new session, which a signal to the group cannot reach. ``stop()`` then
        waits for it and ends the group. A process without ``stop`` (the
        session's own plain child) is the session's to end: the pool never
        signals a process group itself, because only the session knows whether
        the leader was already reaped and its number could belong to someone else.

        One thread ends a process at a time and a second call finds it done,
        unless ``again`` (the pool retrying a worker it could not confirm gone).
        """
        session = slot.session
        process = session.process if session is not None else None
        stop = getattr(process, "stop", None)
        if not callable(stop):
            return
        with slot.end_lock:
            if slot.ended and not again:
                return
            try:
                cut = getattr(process, "close_lifeline", None)
                if callable(cut):
                    cut()
                stop(grace)
            except Exception as error:
                _log_failure("ending a worker process failed", error)
            finally:
                slot.ended = True

    def _terminate_now(self, slot):
        """End the worker's whole tree without a polite shutdown frame."""
        session = slot.session
        if session is None:
            return
        process = session.process
        if callable(getattr(process, "stop", None)):
            self._end_process(slot, 0.5)
        else:
            try:
                session.close(grace=0)
            except Exception as error:
                _log_failure("ending a worker failed", error)

    def _polite_close(self, slot, grace):
        """Shutdown frame, wait, SIGTERM, and then the process's own cleanup.

        ``WorkerSession.close`` hands a launcher or anchored worker over to its
        ``stop()`` instead of killing the group, so the launcher can reach
        children that moved into a new session. The pool's own call afterwards
        is the backstop and finds the work done.
        """
        slot.session.close(grace=grace)
        self._end_process(slot, 0.5)

    def _run_close(self, job):
        slot, entry, mode, grace = job
        gone = False
        try:
            session = slot.session
            if session is not None:
                if mode == "kill":
                    self._terminate_now(slot)
                    session.close(grace=0)
                elif mode == "reap":
                    session.close(grace=0)
                    self._end_process(slot, 0.5)
                else:
                    self._polite_close(slot, grace)
            gone = self._confirmed_gone(slot)
            if not gone and session is not None:
                # One more, harder attempt before the worker is declared stuck.
                self._end_process(slot, 0.5, again=True)
                session.close(grace=0)
                gone = self._confirmed_gone(slot)
        except Exception as error:
            _log_failure("worker close failed", error)
            try:
                gone = self._confirmed_gone(slot)
            except Exception:
                gone = False
        finally:
            with self._cond:
                self._closing = max(0, self._closing - 1)
                if not gone:
                    # Its process is still there: it keeps its place in the process
                    # cap, and its entry stays stopping, until a later pass ends it.
                    self._add_stuck(slot, entry, None)
                    self._events.append((slot.definition.key, "stuck", {}))
                    logger.warning("%s: worker process could not be confirmed gone", slot.definition.key)
                else:
                    self._teardown_confirmed(slot, entry)
                self._close_threads.discard(threading.current_thread())
                self._cond.notify_all()
            self._deliver_events()

    def _note_unverified(self, slot):
        """Count a worker whose tree could only be ended by its process group. Lock held.

        Where there is no subreaper launcher (a platform without one), a descendant
        that detached itself from the group cannot be seen or ended, so "gone" rests
        on the group alone. That is accepted, but counted, reported once per key and
        capped, so a plugin that keeps leaking cannot do so without limit.
        """
        session = slot.session
        process = session.process if session is not None else None
        if process is None or isinstance(process, LauncherProcess):
            return
        key = slot.definition.key
        now = self._clock()
        stamps = self._unverified.setdefault(key, deque())
        stamps.append(now)
        self._prune_unverified(stamps, now)
        if key not in self._unverified_logged:
            self._unverified_logged.add(key)
            logger.info("%s: a worker was ended by process group only; detached processes cannot be "
                        "tracked on this platform", key)

    def _prune_unverified(self, stamps, now):
        while stamps and now - stamps[0] > self.limits.unverified_window:
            stamps.popleft()

    def _unverified_wait(self, key) -> float:
        """Seconds until a capped key may start again, or 0. Lock held."""
        stamps = self._unverified.get(key)
        if not stamps:
            return 0.0
        now = self._clock()
        self._prune_unverified(stamps, now)
        if len(stamps) < self.limits.unverified_limit:
            return 0.0
        return max(0.0, stamps[0] + self.limits.unverified_window - now)

    def _teardown_confirmed(self, slot, entry):
        """A worker's teardown is confirmed. Caller holds the lock.

        The entry reads as stopped only when no other worker of it is still
        being torn down or stuck, and no new worker starts before that.
        """
        key = slot.definition.key
        self._note_unverified(slot)
        pending = self._tearing.get(key)
        if pending is not None:
            pending.discard(slot)
            if not pending:
                del self._tearing[key]
        owner = slot.entry or entry
        if owner is None:
            return
        if (owner.state == WorkerState.STOPPING and owner.slot is None and not owner.retiring
                and not self._tearing.get(key)):
            self._set_state(owner, WorkerState.STOPPED, "stopped")

    def _add_stuck(self, slot, entry, release):
        """Put a worker on the stuck list once; a second report of it only adds what it must release.

        Caller holds the lock.
        """
        for index, (known, known_entry, known_release) in enumerate(self._stuck):
            if known is slot:
                if known_release is None and release is not None:
                    self._stuck[index] = (known, known_entry, release)
                return
        self._stuck.append((slot, entry, release))

    def _retry_stuck(self):
        """End again the workers that survived a stop, and release what they held once they are gone."""
        # One pass at a time: two sweeps must not end the same process together.
        if not self._stuck_lock.acquire(blocking=False):
            return
        try:
            self._retry_stuck_locked()
        finally:
            self._stuck_lock.release()

    def _retry_stuck_locked(self):
        with self._cond:
            items = list(self._stuck)
        for item in items:
            slot, entry, release = item
            try:
                self._end_process(slot, 0.5, again=True)
                if slot.session is not None:
                    slot.session.close(grace=0)
                gone = self._confirmed_gone(slot)
            except Exception as error:
                _log_failure("retrying a stuck worker failed", error)
                gone = False
            if not gone:
                continue
            with self._cond:
                # Whoever takes the item off the list is the one that releases what it held.
                if item not in self._stuck:
                    continue
                self._stuck.remove(item)
                self._teardown_confirmed(slot, entry)
                self._cond.notify_all()
            if release is not None:
                release()
            self._deliver_events()

    def _flush(self, wait=False):
        """Start queued closes on their own threads and deliver queued events.

        Never waits for a close unless ``wait`` is set, so a request or a sweep
        is not held up by another worker's slow shutdown.
        """
        with self._cond:
            jobs, self._closes = self._closes, []
            for job in jobs:
                thread = threading.Thread(target=self._run_close, args=(job,), name="worker-pool-close", daemon=True)
                # Started before the lock is let go, so another caller that joins
                # the registered threads never meets one that has not started.
                self._close_threads.add(thread)
                thread.start()
        if wait:
            deadline = time.monotonic() + self.limits.close_grace + 30.0
            with self._cond:
                pending = list(self._close_threads)
            for thread in pending:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._deliver_events()

    def _deliver_events(self):
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
        except Exception as error:
            _log_failure("pool event sink failed", error)

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
            except Exception as error:
                _log_failure("worker pool sweep failed", error, level=logging.ERROR)

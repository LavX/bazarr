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

Memory counts each process's Pss, plus the memfds and ``/dev/shm`` files the tree holds
open (by blocks).

``/dev/shm`` is a host-bounded pool, not per-worker memory. It is a size-limited tmpfs
the host sets (64 MiB by default in Docker), so what all workers put there together is
bounded outside any of them, and what one worker keeps there through a mapping alone
(the descriptor closed or the file unlinked: a multiprocessing lock's semaphore, a
sharedctypes array's arena) cannot be attributed to it without privilege. Each such
mapping is charged one page, and nothing more is claimed. A ``/dev/shm`` that is
small and separate (at most 1 GiB, a filesystem of its own) is guarded instead: at each
sweep its free space is read, and below a reserve (``shm_reserve_bytes``; by default
the larger of 8 MiB and a tenth of its size) the pool looks at what each live worker holds
(the allocated size of the objects its descriptors hold, each counted once, plus a page for
each mapping no descriptor measures; ties by mapping count). A mapping whose name still exists is
measured by that name (same inode), at the object's full size however little of it is mapped. One
whose name is gone has no readable size and is charged a page; for those the guard compares the
space in use with what the listed files allocate, and when that unexplained space is itself a
meaningful share, the workers holding such mappings are the candidates, the one with the most
mappings first. Only while some worker holds a meaningful share of the shortfall (at least 1 MiB
and at least a quarter of the space that is missing, ``SHM_MIN_HOLDER_BYTES`` and
``SHM_SHARE_DIVISOR``) or is a candidate for unexplained space does it admit no new start or
request (``shm_low``) and stop the heaviest such worker, one per sweep, until the space
recovers or no worker is to blame. Files a stopped worker left behind and what the
host itself keeps there never recover by stopping anyone, so when no worker is to blame the
pool admits work as it does under a short disk reserve, and logs that once per episode
(``status()["shm_short"]`` still reports the shortfall). Where ``/dev/shm`` is not small and
separate there is nothing to guard, and the sandbox limitations say so: "shared memory
under /dev/shm is not limited per plugin on this host".

The other two kinds of shared memory are refused when they cannot be measured, because
no host limit bounds them. A memfd that is only mapped (its descriptor closed), and a
shared anonymous mapping (MAP_SHARED|MAP_ANONYMOUS, which includes Python's
``mmap.mmap(-1, n)``), make the sample incomplete, so the worker counts as over its
limit. Unmapping every page but one of a big shared anonymous mapping leaves the rest
allocated and invisible: the remaining mapping shows one page in Pss, ``maps`` shows the
same offset and device as for an untouched one, and the object's size is unreachable
without privileges (``map_files``). The rule that can be checked is the strict one; a
plugin that uses ``mmap.mmap(-1, n)`` is stopped for it and should use a private
mapping (``flags=mmap.MAP_PRIVATE``) or a memfd it keeps open (measured by blocks).
Python's multiprocessing Lock, Queue, ProcessPoolExecutor and sharedctypes arrays and
``multiprocessing.shared_memory`` use ``/dev/shm`` objects. Where the sandbox is enforced they are
unavailable: the policy grants a worker no access to ``/dev/shm`` (it is one directory for the whole
host, so a grant would let every worker read and change every other worker's shared memory), and
creating one fails with an ``OSError``. Where nothing is enforced (declared mode, or a host without
the sandbox) they work, and this accounting and the guard above are what bound them.

Folder budgets count what is on disk under the folder, plus files the worker tree
unlinked but still holds open (found through its descriptors, charged by blocks).
A deleted file referenced only by a mapping cannot be measured without privileges
(its mapping's extent says nothing reliable about what it allocates), so that one
case fails closed, after one more look at that process's descriptors for the unmatched
files (the descriptor and mapping passes are separate snapshots, so a file unlinked, or a
memfd created and mapped, in between is not mapped-only): a plugin may not keep deleted
files mapped inside its folders, and doing so makes the folder scan incomplete, which
fails its budget. memfds and
anonymous mappings are not under any folder and are never charged.

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
reuses the pool's states, breaker, idle stop and cap unchanged. ``oci.py`` is that
runtime.
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

from ..sandbox import (
    LauncherProcess,
    SandboxApplyError,
    SandboxRefused,
    WorkerProcess,
    shm_guardable,
    shm_state,
    spawn_worker,
)
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
FOLDER_SCAN_SECONDS = 5.0

# The /dev/shm guard refuses work and stops a worker only while stopping one could help: some live
# worker holds at least this much, and at least this fraction of the space that is missing.
SHM_MIN_HOLDER_BYTES = 1 * MIB
SHM_SHARE_DIVISOR = 4  # a quarter of the shortfall

# Allowances for one sweep of one worker reading its /proc (see ``SampleBudget``).
SAMPLE_SECONDS = 2 * FOLDER_SCAN_SECONDS  # wall time for all of one worker's reading in one sweep
SAMPLE_MAX_ENTRIES = 1_000_000  # records read in all: mappings, descriptors, threads, processes
SAMPLE_MAX_BYTES = 128 * 1024 * 1024  # text read in all from /proc files
SAMPLE_MAX_KEPT = 100_000  # records the pass may hold in memory at once (sets and dicts it builds)

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
    SHM_LOW = "shm_low"


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
    """The pool ended the worker for breaking a limit while it held this request.

    ``before_send`` is True when that happened before the request was given to the worker, for a worker that was over
    its memory limit as it started: nothing was sent, so a caller may treat it like any other refusal.
    """

    before_send = False


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
_PATH_CHARS = r"[\w.\-+@%~]"
# An absolute path inside quotes, spaces and all: the quotes say where it ends.
_QUOTED_PATH = re.compile(r"(?P<q>[\"'`])/(?:(?!(?P=q))[^\n])*(?P=q)")
# An unquoted absolute path. A component may hold spaces (``/tmp/provider files/worker``): the path
# goes on over following words as long as each still has a slash in it, so the rest of such a path
# is not left behind. The limit: a space-containing last component with no slash after it
# (``/tmp/provider files``) cannot be told from prose, and its tail stays visible.
_PATH_IN_TEXT = re.compile(
    r"(?<![\w:/.])/" + _PATH_CHARS + r"+(?:/" + _PATH_CHARS + r"*)*"
    r"(?: +" + _PATH_CHARS + r"+(?:/" + _PATH_CHARS + r"*)+)*")


def _log_failure(message: str, error: BaseException, level: int = logging.DEBUG) -> None:
    """Log that something failed, with the exception's type and nothing else.

    A callback, a sink or a close can fail with a message (and a traceback with
    frame values) that holds a request's secret or worker output the session has
    not scrubbed. Only the fixed text and the type name are safe to log.
    """
    logger.log(level, "%s (%s)", message, type(error).__name__)


def _public_text(text) -> str:
    """Error text safe for a status page: filesystem paths removed, length bounded."""
    text = _QUOTED_PATH.sub(lambda match: match.group("q") + "<path>" + match.group("q"), str(text))
    return _PATH_IN_TEXT.sub("<path>", text)[:300]


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
    # Optional, True when absent: whether an answered request proves the worker is healthy, and so clears the
    # failure history. A runtime that answers locally sets it False; its failures then leave by age alone.

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
    # The name workers ended without whole-tree proof are counted and capped under (default: ``key``). Workers that
    # have a key each, as one-off runs may, and belong to one plugin, share a name here so the cap is the plugin's.
    cap_key: str | None = None

    @property
    def count_key(self) -> str:
        return self.cap_key or self.key

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
    # How long one folder scan may take before it counts as incomplete.
    budget_scan_seconds: float = FOLDER_SCAN_SECONDS
    # What one sweep of one worker may spend reading its /proc, all of it together (see ``SampleBudget``):
    # wall time, records, bytes, and records held in memory. When any runs out the sample is incomplete,
    # which fails closed like an unreadable file. The defaults are far above a legitimate plugin.
    sample_seconds: float = SAMPLE_SECONDS
    sample_max_entries: int = SAMPLE_MAX_ENTRIES
    sample_max_bytes: int = SAMPLE_MAX_BYTES
    sample_max_kept: int = SAMPLE_MAX_KEPT
    # Free space in /dev/shm under which nothing new is admitted and the heaviest user is
    # stopped; None means the larger of 8 MiB and a tenth of its size.
    shm_reserve_bytes: int | None = None
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


class _ProcOverflow(OSError):
    """A /proc file or directory of a worker's process is bigger than any honest one: not read to the end."""


# What one /proc file of a worker's process may cost before it counts as unreadable. A worker
# controls how many mappings, descriptors and threads it has, so none of these listings is
# trusted to be small.
PROC_MAX_BYTES = 8 * 1024 * 1024
PROC_MAX_LINES = 65536
PROC_LINE_LIMIT = 16 * 1024
PROC_MAX_ENTRIES = 65536


# The per-file limits above bound one /proc file. A worker tree can have up to 256 processes, each
# under them, so reading all of it needs an aggregate allowance too. One ``SampleBudget`` is made per
# sweep of a worker and shared by everything that reads its /proc: tree and thread discovery, memory
# sampling, descriptor rechecks and the deleted-open pass. When it runs out the sample is incomplete,
# which fails closed exactly as an unreadable file does.
#
# The defaults are far above what a legitimate plugin reads (a handful of processes with a few
# thousand mappings, a few hundred descriptors and a few dozen threads each is about 100,000 records
# and 10 MB of text), and far below the 16 million mapping records a 256-process tree could feed.


class SampleBudget:
    """One sweep's allowance for reading a worker's /proc: time, records, bytes and kept records.

    ``spend`` charges what was read and answers False once anything ran out, and stays False after
    that; ``keep`` does the same for records held in a growing structure; ``expired`` looks at the
    clock only. Callers stop and report an incomplete result when they are told no.
    """

    __slots__ = ("deadline", "entries", "nbytes", "kept", "exhausted", "_ticks")

    def __init__(self, seconds: "float | None" = SAMPLE_SECONDS, entries: int = SAMPLE_MAX_ENTRIES,
                 nbytes: int = SAMPLE_MAX_BYTES, kept: int = SAMPLE_MAX_KEPT) -> None:
        self.deadline = None if seconds is None else time.monotonic() + seconds
        self.entries = entries
        self.nbytes = nbytes
        self.kept = kept
        self.exhausted = False
        self._ticks = 0

    def expired(self) -> bool:
        if not self.exhausted and self.deadline is not None and time.monotonic() > self.deadline:
            self.exhausted = True
        return self.exhausted

    def spend(self, entries: int = 1, nbytes: int = 0) -> bool:
        if self.exhausted:
            return False
        self.entries -= entries
        self.nbytes -= nbytes
        self._ticks += 1
        if self.entries < 0 or self.nbytes < 0:
            self.exhausted = True
        elif self._ticks % 64 == 0:
            self.expired()
        return not self.exhausted

    def keep(self, count: int = 1) -> bool:
        if self.exhausted:
            return False
        self.kept -= count
        if self.kept < 0:
            self.exhausted = True
        return not self.exhausted


def _bounded_lines(path: str, max_bytes: int = PROC_MAX_BYTES, max_lines: int = PROC_MAX_LINES,
                   max_seconds: float = FOLDER_SCAN_SECONDS, budget: "SampleBudget | None" = None):
    """Lines of a /proc file one at a time, never holding more than a line.

    Raises ``_ProcOverflow`` (an ``OSError``) when the file passes ``max_bytes``,
    ``max_lines`` or ``max_seconds``, or has one line longer than ``PROC_LINE_LIMIT``, or when
    ``budget`` (the sweep's shared allowance) runs out: callers already treat an unreadable file
    of a live process as an incomplete sample.
    """
    deadline = time.monotonic() + max_seconds
    total = 0
    count = 0
    with open(path, "rb") as handle:
        while True:
            raw = handle.readline(PROC_LINE_LIMIT + 1)
            if not raw:
                return
            total += len(raw)
            count += 1
            if (len(raw) > PROC_LINE_LIMIT or total > max_bytes or count > max_lines
                    or time.monotonic() > deadline
                    or (budget is not None and not budget.spend(1, len(raw)))):
                raise _ProcOverflow(path)
            yield raw.decode("utf-8", "surrogateescape").rstrip("\n")


def _read_kb(path: str, *names: str, budget: "SampleBudget | None" = None) -> dict:
    found: dict = {}
    wanted = set(names)
    # status and smaps_rollup are a few kilobytes; an enormous one is not honest.
    for line in _bounded_lines(path, max_bytes=256 * 1024, max_lines=4096, budget=budget):
        name, _, rest = line.partition(":")
        if name in wanted:
            try:
                found[name] = int(rest.split()[0]) * 1024
            except (ValueError, IndexError):
                continue
    return found


PARENT_MAP_LIMIT = 32768


def _children_of(pid: int, budget: "SampleBudget | None" = None) -> tuple[list[int] | None, bool]:
    """Direct children from the kernel's per-thread children lists: ``(children or None, incomplete)``.

    None means the lists are not available here (the caller scans ``/proc``
    instead). ``incomplete`` is set when a list that exists could not be read
    or parsed: the children it held are then unknown.
    """
    found: list[int] = []
    incomplete = False
    readable = False
    count = 0
    try:
        # Streamed: a process with a huge number of threads is never listed whole.
        with os.scandir("/proc/%d/task" % pid) as threads:
            for thread in threads:
                count += 1
                if count > PROC_MAX_ENTRIES:
                    incomplete = True
                    break
                if budget is not None and not budget.spend():
                    # The sweep's allowance is gone: the children of the threads not read are unknown.
                    return found, True
                limit = PROC_MAX_BYTES if budget is None else min(PROC_MAX_BYTES, max(budget.nbytes, 0))
                try:
                    with open("/proc/%d/task/%s/children" % (pid, thread.name), "r", encoding="ascii") as handle:
                        text = handle.read(limit + 1)
                    if budget is not None and not budget.spend(0, len(text)):
                        return found, True
                    readable = True
                    if len(text) > limit:
                        incomplete = True
                        continue
                    kids = [int(p) for p in text.split()]
                    if budget is not None and not budget.keep(len(kids)):
                        # Records of what was found are held too, and they are charged.
                        return found, True
                    found.extend(kids)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    incomplete = True
    except FileNotFoundError:
        return [], False  # gone
    except OSError:
        return None, False
    return (found if readable else None), incomplete


def _parent_map(limit: int = PARENT_MAP_LIMIT, budget: "SampleBudget | None" = None
                ) -> tuple[dict[int, list[int]], bool]:
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
        if budget is not None and not budget.spend():
            return tree, False
        if budget is not None and not budget.keep():
            return tree, False
        try:
            with open("/proc/%s/stat" % name, "r", encoding="ascii", errors="replace") as handle:
                text = handle.read(PROC_LINE_LIMIT + 1)
            if len(text) > PROC_LINE_LIMIT:
                complete = False
                continue
            ppid = int(text.rsplit(")", 1)[1].split()[1])
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            complete = False
            continue
        tree.setdefault(ppid, []).append(int(name))
    return tree, complete


def _process_tree(root: int, max_processes: int, budget: "SampleBudget | None" = None) -> tuple[list[int], bool]:
    """The root and its descendants, and whether that list may be incomplete.

    Incomplete when the walk stopped at ``max_processes``, when a children list
    could not be read, or when the ``/proc`` scan used instead was cut short or
    skipped a record, or when ``budget`` (the sweep's shared allowance) ran out: a descendant
    could be missing from the answer.
    """
    incomplete = False
    seen = [root]
    index = 0
    fallback: dict[int, list[int]] | None = None
    while index < len(seen):
        if budget is not None and not budget.spend():
            return seen, True
        current = seen[index]
        index += 1
        kids, partial = _children_of(current, budget)
        if partial:
            incomplete = True
        if kids is None:
            if fallback is None:
                fallback, complete = _parent_map(budget=budget)
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


def read_process_tree_memory(pid: int, max_processes: int = 256, skip_fds=(),
                             budget: "SampleBudget | None" = None) -> MemorySample | None:
    """Memory of a process and every descendant, from ``smaps_rollup``, plus the memfds they hold open.

    The sum uses ``Pss`` so pages shared between workers are not counted in
    full by each. A process without ``smaps_rollup`` (older kernel, or a
    hardened ``/proc``) falls back to ``VmRSS`` from ``status``. Returns None
    when the root process is gone or ``/proc`` is unavailable. A tree larger
    than ``max_processes``, or with a live process whose memory cannot be
    read, comes back with ``truncated`` set.

    Memory held in open memfds and ``/dev/shm`` files is in no process's Pss or Rss
    until it is mapped, so it is added from the descriptors (``memfd_usage``). One
    that is also mapped is then counted twice, which errs towards stopping the
    worker. ``skip_fds`` names processes whose descriptor directory is not read (the
    pool's own non-dumpable launcher, which hides it from its own user by design).

    ``budget`` is the sweep's shared allowance for reading this worker's /proc (one is made when
    none is given); when it runs out the sample is ``truncated``.
    """
    if not os.path.isdir("/proc/self"):
        return None
    if budget is None:
        budget = SampleBudget()
    total = 0
    rss_total = 0
    counted = 0
    source = "smaps_rollup"
    members, truncated = _process_tree(pid, max_processes, budget)
    for member in members:
        if budget.exhausted:
            truncated = True
            break
        try:
            values = _read_kb("/proc/%d/smaps_rollup" % member, "Pss", "Rss", budget=budget)
            used = values.get("Pss", values.get("Rss"))
            rss = values.get("Rss", used)
            if used is None:
                raise OSError("no memory figure")
        except OSError:
            # No smaps_rollup (an older kernel, or none for this process) says
            # nothing about the process being gone: ask status. Only a process
            # whose own /proc entry has vanished is not part of the tree any more.
            try:
                values = _read_kb("/proc/%d/status" % member, "VmRSS", budget=budget)
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
    held, held_incomplete = memfd_usage(members, skip_fds, budget)
    if held:
        total += held
        rss_total += held
    truncated = truncated or held_incomplete or budget.exhausted
    if not counted:
        # Nothing readable. If nothing was seen at all the process is gone; if
        # live processes denied the read, the answer is "unknown", not "none".
        if not truncated:
            return None
        return MemorySample(bytes=0, rss_bytes=0, processes=0, source=source, truncated=True)
    return MemorySample(bytes=total, rss_bytes=rss_total, processes=counted, source=source, truncated=truncated)


_MAX_SCAN_DEPTH = 256


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: a Windows symlink, junction or mount point


# Decided once, from the real functions: a wrapper put around ``os.open`` later is not a reason to change walks.
_FD_WALK = bool(hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
                and os.open in os.supports_dir_fd and os.scandir in os.supports_fd)


def _fd_walk_available() -> bool:
    """Whether directories can be opened and listed by descriptor here (POSIX; not Windows)."""
    return _FD_WALK


def _is_reparse(entry, info) -> bool:
    """A link of any kind: a symlink, or on Windows a junction or other reparse point."""
    if entry.is_symlink():
        return True
    junction = getattr(entry, "is_junction", None)
    if callable(junction) and junction():
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


class _LinkTally:
    """Charges a file once however many names it has, across one budget's scan and its deleted-open files.

    A file is one set of blocks on disk whatever links point at it, so a 300 MiB file with two
    names under a folder is 300 MiB, not 600. Identity is (device, inode). Only a file with more
    than one link needs remembering (a directory never does: it is charged per entry). A file
    that was unlinked but is still open, and still has a link somewhere (``deleted``: identity to
    size), is recognised too: if the scan reaches it through that surviving link, the scan has
    charged it and the deleted-open pass must not.
    """

    def __init__(self, deleted: "dict | None" = None) -> None:
        self.deleted = deleted or {}
        self.linked: set = set()

    def charge(self, info) -> int:
        size = _allocated(info)
        inode = getattr(info, "st_ino", 0)
        if not inode or stat.S_ISDIR(info.st_mode):
            return size
        identity = (info.st_dev, inode)
        if getattr(info, "st_nlink", 1) > 1 or identity in self.deleted:
            if identity in self.linked:
                return 0
            self.linked.add(identity)
        return size

    def deleted_charged(self) -> int:
        """Bytes of the deleted-open files the scan already charged through a surviving link."""
        return sum(size for identity, size in self.deleted.items() if identity in self.linked)


def folder_usage(path: str, max_entries: int = 100_000, max_seconds: float | None = FOLDER_SCAN_SECONDS,
                 links: "_LinkTally | None" = None) -> tuple[int, bool]:
    """Allocated bytes under ``path`` without following links.

    Returns ``(bytes, truncated)``; ``truncated`` means the scan is incomplete and
    the caller should treat the folder as over any budget. That is so when the
    walk stopped at ``max_entries`` or after ``max_seconds`` (so one slow tree
    cannot hold up the supervision of others), when a subtree cannot be read (a
    directory without permission to list it or to stat what is in it), and when a
    directory was swapped for something else while the scan was under way. A
    missing folder is empty.

    Where directories can be opened by descriptor (POSIX) the walk holds them: every
    directory is opened without following links, relative to the descriptor of its
    parent, and listed and stat'ed through that descriptor. A path is never looked up a
    second time, so swapping a directory for a symlink between the listing and the visit
    cannot send the scan outside the folder. Elsewhere (Windows) it is a bounded
    ``os.scandir(path)`` walk that does not follow or enter symlinks, junctions or other
    reparse points, and looks at a directory once more just before listing it; without a
    descriptor that window cannot be closed completely.

    A file with several hard links is charged once (``links`` remembers which ones the scan has
    met; one is made when none is given).
    """
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    if links is None:
        links = _LinkTally()
    if not _fd_walk_available():
        return _folder_usage_by_path(path, max_entries, deadline, links)
    return _folder_usage_by_descriptor(path, max_entries, deadline, links)


def _folder_usage_by_path(path: str, max_entries: int, deadline, links: "_LinkTally") -> tuple[int, bool]:
    root = os.path.realpath(path)
    try:
        if not stat.S_ISDIR(os.stat(root).st_mode):
            return 0, False
    except FileNotFoundError:
        return 0, False
    except OSError:
        return 0, True
    total = 0
    seen = 0
    stack = [(root, 0)]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            return total, True
        current, depth = stack.pop()
        if depth > _MAX_SCAN_DEPTH:
            return total, True
        try:
            before = os.lstat(current)
            if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & _REPARSE_POINT:
                return total, True  # swapped for a link since it was listed
            scan = os.scandir(current)
        except FileNotFoundError:
            continue
        except OSError:
            return total, True
        with scan:
            for entry in scan:
                seen += 1
                if seen > max_entries or (deadline is not None and time.monotonic() > deadline):
                    return total, True
                try:
                    info = entry.stat(follow_symlinks=False)
                    total += links.charge(info)
                    if _is_reparse(entry, info):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, depth + 1))
                except FileNotFoundError:
                    continue
                except OSError:
                    return total, True
    return total, False


def _folder_usage_by_descriptor(path: str, max_entries: int, deadline, links: "_LinkTally") -> tuple[int, bool]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = flags | getattr(os, "O_NOFOLLOW", 0)
    try:
        # The configured folder itself may be a link the host set up; only what is inside it is untrusted.
        root = os.open(os.path.realpath(path), flags)
    except (FileNotFoundError, NotADirectoryError):
        return 0, False
    except OSError:
        return 0, True
    total = 0
    seen = 0
    frames: list = []  # (directory descriptor, scandir iterator)
    try:
        frames.append((root, os.scandir(root)))
        while frames:
            if deadline is not None and time.monotonic() > deadline:
                return total, True
            descriptor, entries = frames[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                os.close(descriptor)
                frames.pop()
                continue
            seen += 1
            if seen > max_entries:
                return total, True
            try:
                info = entry.stat(follow_symlinks=False)
                total += links.charge(info)
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                return total, True
            if len(frames) >= _MAX_SCAN_DEPTH:
                return total, True
            try:
                child = os.open(entry.name, nofollow, dir_fd=descriptor)
            except FileNotFoundError:
                continue
            except OSError:
                # Swapped for a link or a file since it was listed, or unreadable: not scanned.
                return total, True
            try:
                frames.append((child, os.scandir(child)))
            except OSError:
                os.close(child)
                return total, True
        return total, False
    except OSError:
        return total, True
    finally:
        for descriptor, entries in frames:
            try:
                entries.close()
            except OSError:
                pass
            try:
                os.close(descriptor)
            except OSError:
                pass


_DELETED = " (deleted)"


def _allocated(info) -> int:
    """Bytes a file really allocates: its blocks whenever the platform reports them, even zero.

    A sparse file allocates less than its size, down to nothing; ``st_size`` is the
    answer only where there is no ``st_blocks`` at all.
    """
    blocks = getattr(info, "st_blocks", None)
    return info.st_size if blocks is None else blocks * 512


_SHM_DIR = "/dev/shm/"
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
# How a shared anonymous mapping (MAP_SHARED|MAP_ANONYMOUS, Python's ``mmap.mmap(-1, n)``)
# appears in ``maps``: an unnamed tmpfs file, "/dev/zero (deleted)" on current kernels.


def _holds_memory(target: str) -> bool:
    """Whether an open descriptor's link target is memory the kernel keeps for the holder.

    A memfd, or a file in ``/dev/shm`` (``shm_open``), however it was unlinked: its
    pages are tmpfs pages that no process's Pss or Rss shows until they are mapped.
    """
    if target.endswith(_DELETED):
        target = target[: -len(_DELETED)]
    return target.startswith("/memfd:") or target.startswith(_SHM_DIR)


def _recheck_descriptors(pid: int, wanted: dict, budget: "SampleBudget | None" = None) -> list:
    """One more pass over a process's descriptors, for the mapped files a first pass did not match.

    The descriptor pass and the mapping pass of a sample are two snapshots, so a worker that
    unlinks a file, or creates and maps one, between them leaves a file a descriptor does
    measure looking mapped-only. ``wanted`` maps a name (in the form ``maps`` writes it, without
    " (deleted)") to the set of inodes mapped under it. Returns ``(name, stat result, link target)``
    for each descriptor that holds one of them now: only those targets are stat'ed, and the
    pass reads at most ``PROC_MAX_ENTRIES`` descriptors and charges ``budget`` for each. What cannot
    be read finds nothing, and the caller then fails closed as before.
    """
    found = []
    directory = "/proc/%d/fd" % pid
    try:
        descriptors = os.listdir(directory)[:PROC_MAX_ENTRIES]
    except OSError:
        return found
    for name in descriptors:
        if budget is not None and not budget.spend():
            return found
        link = "%s/%s" % (directory, name)
        try:
            target = os.readlink(link)
        except OSError:
            continue
        path = _maps_form(target[: -len(_DELETED)] if target.endswith(_DELETED) else target)
        if path not in wanted:
            continue
        try:
            info = os.stat(link)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_ino in wanted[path]:
            if budget is not None and not budget.keep():
                return found
            found.append((path, info, target))
    return found


def _maps_device(field: str) -> "int | None":
    """The device a ``maps`` line names (``"fd:01"``, hex major and minor) as ``st_dev`` writes it, or None."""
    try:
        major, minor = field.split(":")
        return os.makedev(int(major, 16), int(minor, 16))
    except (ValueError, OverflowError):
        return None


def _recheck_by_inode(pid: int, wanted: dict, done: set, budget: "SampleBudget") -> "tuple[list, bool]":
    """One more pass over a process's descriptors, for mapped deleted files measured by inode.

    ``wanted`` maps an inode to ``({device: [mapped names]}, {mapped name: None})``, built once for the
    whole tree. A descriptor measures a mapping when it holds the same regular file: same inode and the
    same device (``stat`` and ``maps`` do not always agree on a device number, a btrfs subvolume for one,
    so a descriptor whose own name is a mapped name matches on the inode alone). The descriptor's own
    name does not matter: a hard link to the mapped file, deleted or not, measures it. ``done`` holds
    the (inode, device) groups already resolved, so the work is one step per descriptor plus one per
    mapped name however many descriptors hold the same file. Returns ``([(mapped name, stat result)],
    timed_out)``: ``timed_out`` when ``budget`` (the sweep's shared allowance) ran out first. What cannot
    be read finds nothing, and the caller fails closed as before.
    """
    found: list = []
    directory = "/proc/%d/fd" % pid
    try:
        descriptors = os.listdir(directory)[:PROC_MAX_ENTRIES]
    except OSError:
        return found, False
    for name in descriptors:
        if not budget.spend():
            return found, True
        link = "%s/%s" % (directory, name)
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if not target.startswith(os.sep):
            continue
        try:
            info = os.stat(link)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_ino not in wanted:
            continue
        by_device, names = wanted[info.st_ino]
        group = (info.st_ino, info.st_dev)
        if group not in done:
            matched = by_device.get(info.st_dev, ())
            if not budget.keep(len(matched) + 1):
                return found, True
            done.add(group)
            found.extend((path, info) for path in matched)
        own = _maps_form(target[: -len(_DELETED)] if target.endswith(_DELETED) else target)
        if own in names and (info.st_ino, own) not in done:
            if not budget.keep(2):
                return found, True
            done.add((info.st_ino, own))
            found.append((own, info))
    return found, False


def memfd_usage(pids, skip=(), budget: "SampleBudget | None" = None) -> tuple[int, bool]:
    """Bytes held by memfds and ``/dev/shm`` files that processes have open: ``(bytes, incomplete)``.

    Memory in a memfd that is open but not mapped is in nobody's Pss or Rss, so a
    worker can fill memory past its limit with them. Every descriptor of every
    process given (but not those in ``skip``, the trusted launcher's own) whose
    target is such an object is stat'ed through its ``/proc`` link and counted by
    allocated size, once per (device, inode) across the tree.

    A memfd that is also mapped is in Pss_Shmem as well, so it can be counted twice;
    that errs towards stopping the worker and is accepted. Incomplete when the
    descriptors or mappings of a live process cannot be read, and when a memfd or a
    System V segment is mapped but no descriptor in the tree measures it: a plugin
    may not keep a memfd mapped after closing it. An unmeasured ``/dev/shm`` mapping
    is charged one page instead (see the pool docstring). A shared *anonymous* mapping has no descriptor to measure at all, so
    any in the tree makes the sample incomplete (see the pool docstring for why, and
    for what it costs). Not covered: pipe and socket
    buffers, eventfds and the like (small or bounded by system limits), and tmpfs
    files outside ``/dev/shm`` (the folder budgets cover the folders a plugin has).

    Everything read is charged to ``budget`` (the sweep's shared allowance; one is made when none
    is given), and so is every record the pass keeps in its sets and dicts. When it runs out the
    result is incomplete, as for an unreadable file.
    """
    if budget is None:
        budget = SampleBudget()
    total = 0
    seen: set = set()
    measured: set = set()  # (target path, inode) of every memory object a descriptor measured
    unmatched: dict = {}  # pid -> {name: {inode}} of mappings no descriptor measured when the maps were read
    incomplete = False
    for pid in pids:
        if pid in skip:
            continue
        if budget.expired():
            return total, True
        directory = "/proc/%d/fd" % pid
        try:
            descriptors = os.listdir(directory)
            if len(descriptors) > PROC_MAX_ENTRIES:
                incomplete = True
                descriptors = descriptors[:PROC_MAX_ENTRIES]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            incomplete = True
            continue
        for name in descriptors:
            if not budget.spend():
                return total, True
            link = "%s/%s" % (directory, name)
            try:
                target = os.readlink(link)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                incomplete = True
                continue
            if not _holds_memory(target):
                continue
            try:
                info = os.stat(link)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                incomplete = True
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if not budget.keep(2):
                return total, True
            measured.add((_maps_form(target[: -len(_DELETED)] if target.endswith(_DELETED) else target), info.st_ino))
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            total += _allocated(info)
    # Shared memory that is only mapped: the descriptor was closed, the mapping stays, and
    # nothing unprivileged says how much the object holds (the mapped pages in Pss are
    # only the resident part of the mapping). A mapping of such an object that no
    # descriptor in the tree measures makes the sample incomplete.
    for pid in pids:
        if pid in skip:
            continue
        records = _mapped_objects("/proc/%d/maps" % pid, budget)
        while True:
            # One record at a time: a mapping list is never held whole.
            try:
                parts = next(records)
            except StopIteration:
                break
            except (FileNotFoundError, ProcessLookupError):
                break
            except OSError:
                # Too big, or the sweep's allowance ran out: unread mappings are unknown.
                incomplete = True
                if budget.exhausted:
                    return total, True
                break
            path = parts[5][: -len(_DELETED)] if parts[5].endswith(_DELETED) else parts[5]
            if _is_shared_anonymous(path, parts[1]):
                # MAP_SHARED|MAP_ANONYMOUS: tmpfs pages behind a file nobody has a descriptor
                # to. Unmap all but a page of a big one and the rest stays allocated with
                # no mapping to show it, and nothing unprivileged says how big the object is.
                incomplete = True
                continue
            if not (_holds_memory(path) or path.startswith("/SYSV")):
                continue
            try:
                inode = int(parts[4])
            except ValueError:
                incomplete = True
                continue
            if (path, inode) in measured:
                continue
            if path.startswith(_SHM_DIR):
                # An unlinked or descriptor-less /dev/shm object (a POSIX semaphore of a
                # multiprocessing lock, say). /dev/shm is a size-limited tmpfs the host sets,
                # so what all workers put there together is bounded outside any of them, and
                # what one worker keeps there cannot be attributed to it without privilege.
                # Each such mapping is charged a page, and ``WorkerPool`` guards the total.
                total += _PAGE_SIZE
                continue
            if not budget.keep():
                return total, True
            unmatched.setdefault(pid, {}).setdefault(path, set()).add(inode)
    # The descriptors were read before the mappings, so an object created (or a mapping made)
    # in between looks mapped-only. Look at that process's descriptors once more, for those
    # objects only; what no descriptor holds even now is mapped-only, and fails closed.
    for pid, wanted in unmatched.items():
        for path, info, target in _recheck_descriptors(pid, wanted, budget):
            if not _holds_memory(target):
                continue
            measured.add((path, info.st_ino))
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += _allocated(info)
        if budget.exhausted:
            return total, True
    for wanted in unmatched.values():
        if any((path, inode) not in measured for path, inodes in wanted.items() for inode in inodes):
            incomplete = True
    return total, incomplete


def _maps_form(name: str) -> str:
    """A path as ``/proc/<pid>/maps`` writes it.

    ``readlink`` of a descriptor gives the raw name; ``maps`` writes a newline in it as
    ``\\012`` (and nothing else: the kernel escapes only the characters it is told to, a
    backslash is not one of them, so ``\\012`` there may also be a real backslash and the
    digits). Comparing the two means putting the raw name in the ``maps`` form, never the
    other way round: the reverse is ambiguous. Two names that differ only that way are the
    same name here, which costs nothing, since an inode is compared too.
    """
    return name.replace("\n", "\\012") if "\n" in name else name


def _mapped_objects(path: str, budget: "SampleBudget | None" = None):
    """The mappings of a ``maps`` file that have a name, as ``line.split(None, 5)``: read within bounds.

    The name (the sixth field) is left as ``maps`` wrote it; compare it with ``_maps_form`` of a
    ``readlink`` target.
    """
    for line in _bounded_lines(path, budget=budget):
        parts = line.split(None, 5)
        if len(parts) >= 6:
            yield parts


def _is_shared_anonymous(path: str, perms: str = "rw-s") -> bool:
    """Whether a mapping's name is that of a shared anonymous one: unnamed, or named with ``PR_SET_VMA_ANON_NAME``.

    A private anonymous mapping, named or not, shows no file or ``[anon:name]``; a shared
    one shows ``/dev/zero`` or ``[anon_shmem]`` or ``[anon_shmem:name]``. The name is not
    enough: a ``MAP_PRIVATE`` mapping of an opened ``/dev/zero`` carries the same name and
    is measured like any private memory, so the permissions decide ("rw-s" shared, "rw-p"
    private).
    """
    return (path == "/dev/zero" or path.startswith("[anon_shmem")) and perms[3:4] == "s"


def shm_mapping_count(pids, skip=(), budget: "SampleBudget | None" = None) -> int:
    """How many mappings of ``/dev/shm`` objects these processes hold (the guard's measure of who uses it)."""
    count = 0
    for pid in pids:
        if pid in skip:
            continue
        try:
            for parts in _mapped_objects("/proc/%d/maps" % pid, budget):
                if parts[5].startswith(_SHM_DIR):
                    count += 1
        except _ProcOverflow:
            # More mappings than any honest process has (or the sweep's allowance is gone): the
            # heaviest user by far.
            count += PROC_MAX_LINES
        except OSError:
            continue
    return count


def shm_usage(pids, skip=(), budget: "SampleBudget | None" = None) -> tuple[int, int]:
    """What these processes hold of ``/dev/shm``: ``(bytes, mappings)``. See ``shm_usage_detail``."""
    total, mappings, _blind = shm_usage_detail(pids, skip, budget)
    return total, mappings


def shm_listed_bytes(max_entries: int = 100_000, max_seconds: float = FOLDER_SCAN_SECONDS) -> tuple[int, bool]:
    """Allocated bytes of everything listed under ``/dev/shm``: ``(bytes, truncated)``."""
    return folder_usage(_SHM_DIR.rstrip("/"), max_entries, max_seconds)


def shm_usage_detail(pids, skip=(), budget: "SampleBudget | None" = None) -> tuple[int, int, int]:
    """What these processes hold of ``/dev/shm``: ``(bytes, mappings, blind mappings)``. The guard's measure.

    Bytes are the allocated size (blocks) of every ``/dev/shm`` object a descriptor
    holds open, each counted once across the tree. A mapping no descriptor measures is looked up
    by its name: where the name still exists and is that same object (same inode), its blocks
    are charged, once per object, however little of it is mapped. One whose name is gone (a
    closed and unlinked arena, a semaphore) has a size nothing unprivileged can read: it is
    charged one page and counted as *blind*, and the guard accounts for what blind mappings may
    be hiding from the space that no listed file explains. Mappings are all mappings of
    ``/dev/shm`` objects. A listing too big to read counts as the most an honest process could
    have, and so does what is left unread when ``budget`` (the sweep's shared allowance) runs
    out, so a worker that tries it is the first to go.
    """
    total = 0
    mappings = 0
    blind = 0
    seen: set = set()
    measured: set = set()
    named: dict = {}  # (name, inode) -> identity of the object it still names, or None
    for pid in pids:
        if pid in skip:
            continue
        if budget is not None and budget.expired():
            total += PROC_MAX_ENTRIES * _PAGE_SIZE
            mappings += PROC_MAX_LINES
            blind += PROC_MAX_LINES
            break
        directory = "/proc/%d/fd" % pid
        try:
            descriptors = os.listdir(directory)
            if len(descriptors) > PROC_MAX_ENTRIES:
                total += PROC_MAX_ENTRIES * _PAGE_SIZE
                descriptors = descriptors[:PROC_MAX_ENTRIES]
        except OSError:
            continue
        for name in descriptors:
            if budget is not None and not budget.spend():
                total += PROC_MAX_ENTRIES * _PAGE_SIZE
                break
            link = "%s/%s" % (directory, name)
            try:
                target = os.readlink(link)
                held = target[: -len(_DELETED)] if target.endswith(_DELETED) else target
                if not held.startswith(_SHM_DIR):
                    continue
                info = os.stat(link)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if budget is not None and not budget.keep(2):
                total += PROC_MAX_ENTRIES * _PAGE_SIZE
                break
            measured.add((_maps_form(held), info.st_ino))
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                total += _allocated(info)
    for pid in pids:
        if pid in skip:
            continue
        try:
            for parts in _mapped_objects("/proc/%d/maps" % pid, budget):
                path = parts[5][: -len(_DELETED)] if parts[5].endswith(_DELETED) else parts[5]
                if not path.startswith(_SHM_DIR):
                    continue
                mappings += 1
                try:
                    inode = int(parts[4])
                except ValueError:
                    inode = None
                if (path, inode) in measured:
                    continue
                key = (path, inode)
                if key not in named:
                    # The name may still be there: then the object's blocks are known.
                    identity = None
                    if inode is not None:
                        # ``maps`` writes " (deleted)" after the name of a removed file, but a live file
                        # may really be called that, and it writes a newline in a name as \012: look
                        # the name up in each form, and trust only a hit on the same inode.
                        raw = parts[5]
                        forms = {path, raw, raw.replace("\\012", "\n"), path.replace("\\012", "\n")}
                        for form in forms:
                            if not form.startswith(_SHM_DIR):
                                continue
                            try:
                                info = os.lstat(form)
                            except OSError:
                                continue
                            if stat.S_ISREG(info.st_mode) and info.st_ino == inode:
                                identity = (info.st_dev, info.st_ino, _allocated(info))
                                break
                    if budget is not None and not budget.keep():
                        total += PROC_MAX_ENTRIES * _PAGE_SIZE
                        blind += 1
                        continue
                    named[key] = identity
                identity = named[key]
                if identity is None:
                    total += _PAGE_SIZE
                    blind += 1
                elif identity[:2] not in seen:
                    seen.add(identity[:2])
                    total += identity[2]
        except _ProcOverflow:
            mappings += PROC_MAX_LINES
            blind += PROC_MAX_LINES
            total += PROC_MAX_LINES * _PAGE_SIZE
        except OSError:
            continue
    return total, mappings, blind


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def deleted_open_usage(pids, folders, files: "dict | None" = None,
                       max_seconds: "float | None" = FOLDER_SCAN_SECONDS,
                       budget: "SampleBudget | None" = None) -> tuple[dict, bool]:
    """Bytes held by files that were unlinked but are still open, per folder: ``({folder: bytes}, incomplete)``.

    A worker can create a file in its folder, unlink it and keep writing through
    the open descriptor: the bytes are on disk and no directory scan finds them.

    For every process given, a descriptor whose link target ends in "(deleted)"
    and whose original path lies under a folder is stat'ed through its ``/proc``
    link and counted by allocated size. A file that is only *mapped*, with no
    descriptor anywhere in the tree to stat, cannot be measured without privileges
    an ordinary host lacks (``/proc/<pid>/map_files`` is never touched), and the
    extent of a mapping says nothing reliable about what the file allocates (one
    page of a huge file; a sparse map). So that one case fails closed: the folder
    scan is incomplete and the budget fails. A file that is both open and mapped is
    charged once, by its blocks. Identity is (device, inode) for a descriptor, and
    a mapping is recognised as the same file by its inode and device (or, where
    ``maps`` and ``stat`` disagree on the device, by its path and inode), whatever name
    the descriptor was opened by: a descriptor on another hard link to the mapped file
    measures it too. So a file is counted once however many processes hold it. Hard links to one file that were unlinked
    from folders with different budgets are charged to each of those folders, once each.

    Attribution is by ancestry only: the deleted file's original directory under a
    budgeted folder. Its device plays no part, and a file with no path under any
    folder (an anonymous mapping, ``/memfd:...``) is charged to none.

    Incomplete when the descriptors or mappings of a live process cannot be read,
    or a candidate descriptor cannot be stat'ed. A process that vanished, or a
    descriptor closed meanwhile, is not an omission.

    ``files``, when given, is filled with ``{folder: {(device, inode): bytes}}``: what was
    charged to each folder, file by file, so a caller that also scans the folder can charge a
    file that still has a link under it only once (see ``_LinkTally``).

    The whole pass is bounded by ``budget``, the sweep's shared allowance of time, records, bytes
    and kept records (one is made from ``max_seconds`` when none is given; None is unbounded in
    time), and fails closed the way the folder scan does: when it runs out the result is incomplete,
    so the budget fails. Every per-descriptor and per-mapping step is a constant-time lookup in a set
    or dict, so a worker that maps one file through thousands of hard links costs one step for each.
    """
    if budget is None:
        budget = SampleBudget(seconds=max_seconds)

    def out_of_time():
        return not budget.spend()

    roots = [(folder, {_maps_form(folder), _maps_form(os.path.realpath(folder))}) for folder in folders]
    usage = {folder: 0 for folder, _names in roots}
    incomplete = False
    held: dict = {}  # (device, inode) -> ({every original path seen}, size from a descriptor)
    held_names: set = set()  # (original path, inode): how a mapping recognises a file a descriptor holds
    unmatched: dict = {}  # inode -> {(name, device): None} of mappings no descriptor held when the maps were read

    def folders_of(path):
        if path.startswith("/memfd:") or not path.startswith(os.sep):
            # Not a file in any directory: a memfd, or something with no path at all.
            return []
        return [folder for folder, names in roots if any(_under(path, name) for name in names)]

    for pid in pids:
        if budget.expired():
            return usage, True
        directory = "/proc/%d/fd" % pid
        try:
            descriptors = os.listdir(directory)
            if len(descriptors) > PROC_MAX_ENTRIES:
                incomplete = True
                descriptors = descriptors[:PROC_MAX_ENTRIES]
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = True
            continue
        for name in descriptors:
            if out_of_time():
                return usage, True
            link = "%s/%s" % (directory, name)
            try:
                target = os.readlink(link)
            except FileNotFoundError:
                continue
            except OSError:
                incomplete = True
                continue
            if not target.endswith(_DELETED):
                continue
            path = _maps_form(target[: -len(_DELETED)])
            if not folders_of(path):
                continue
            try:
                info = os.stat(link)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                incomplete = True
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if not budget.keep(2):
                return usage, True
            held.setdefault((info.st_dev, info.st_ino), (set(), _allocated(info)))[0].add(path)
            held_names.add((path, info.st_ino))
    for pid in pids:
        if budget.expired():
            return usage, True
        records = _mapped_objects("/proc/%d/maps" % pid, budget)
        while True:
            # One record at a time: the deleted mappings are never collected into a list first.
            try:
                parts = next(records)
            except StopIteration:
                break
            except FileNotFoundError:
                break
            except OSError:
                incomplete = True
                if budget.exhausted:
                    return usage, True
                break
            if not parts[5].endswith(_DELETED):
                continue
            if out_of_time():
                return usage, True
            path = parts[5][: -len(_DELETED)]
            if not folders_of(path):
                continue
            try:
                inode = int(parts[4])
            except ValueError:
                incomplete = True
                continue
            if inode == 0:
                continue
            # A descriptor's file is recognised by its path and inode (``stat`` and
            # ``maps`` do not always agree on the device number, a btrfs subvolume
            # for one); its size is then known from the descriptor.
            if (path, inode) in held_names:
                continue
            # Measuring is by inode, separately from which folder a name is charged to: a descriptor
            # on a hard link to the mapped file (deleted or not) holds the very same blocks, and the
            # mapped name is kept so the file is charged to every folder it was mapped from.
            device = _maps_device(parts[3])
            if device is not None and (device, inode) in held:
                if not budget.keep(2):
                    return usage, True
                held[(device, inode)][0].add(path)
                held_names.add((path, inode))
                continue
            # No descriptor held it when the descriptors were read. It may have been unlinked since
            # (the passes are separate snapshots), or held through another name: the descriptors
            # are read again below.
            if not budget.keep(2):
                return usage, True
            unmatched.setdefault(inode, {})[(path, device)] = None
    # A descriptor anywhere in the supplied tree measures a mapping anywhere in it: a child may hold
    # the mapping while its parent holds the live hard link (or the other way round).
    if unmatched:
        wanted: dict = {}  # inode -> ({device: [names]}, {name: None})
        for inode, names in unmatched.items():
            by_device: dict = {}
            for path, device in names:
                by_device.setdefault(device, []).append(path)
            wanted[inode] = (by_device, {path: None for path, _device in names})
        done: set = set()
        for pid in pids:
            found, timed_out = _recheck_by_inode(pid, wanted, done, budget)
            for path, info in found:
                if not budget.keep(2):
                    return usage, True
                held.setdefault((info.st_dev, info.st_ino), (set(), _allocated(info)))[0].add(path)
                held_names.add((path, info.st_ino))
            if timed_out or budget.expired():
                return usage, True
    if any((path, inode) not in held_names for inode, names in unmatched.items() for path, _device in names):
        # Referenced by a mapping alone: no descriptor to stat, so its size is unknown.
        incomplete = True
    for identity, (paths, size) in held.items():
        if out_of_time():
            return usage, True
        # Hard links to one file, unlinked from folders with different budgets: every folder that
        # one of the names was in is charged, once each.
        for folder in {folder for path in paths for folder in folders_of(path)}:
            usage[folder] += size
            if files is not None:
                files.setdefault(folder, {})[identity] = size
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
        # Whether this worker holds a unit of its key's unverified-teardown allowance.
        self.unverified_reserved = False
        # Set when its end has been judged, so a second settlement (a close that
        # overlaps a one-off run's own cleanup) cannot count it again.
        self.unverified_noted = False
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
        shm_reader: Callable[[], Any] | None = None,
        shm_lister: Callable[[], Any] | None = None,
        memory_reader: Callable[[int], MemorySample | None] | None = None,
        autostart: bool = True,
    ):
        self.limits = limits or PoolLimits()
        self._config_dir = os.fspath(config_dir) if config_dir else None
        self._disk_free = disk_free or self._default_disk_free
        self._memory_reader = memory_reader or read_process_tree_memory
        self._shm_reader = shm_reader or shm_state
        # ``() -> (bytes, truncated)``: what the files listed in /dev/shm allocate (the guard compares
        # it with the space in use to find what mappings without a name are hiding).
        self._shm_lister = shm_lister or shm_listed_bytes
        configured = self.limits.shm_reserve_bytes
        if configured is not None:
            if isinstance(configured, bool) or not isinstance(configured, int) or configured < 0:
                raise ValueError("shm_reserve_bytes must be a non-negative whole number of bytes")
            try:
                state = self._shm_reader()
            except Exception:
                state = None
            if shm_guardable(state) and configured > state[0]:
                raise ValueError(
                    "shm_reserve_bytes (%d) is more than all of /dev/shm (%d bytes): the guard could never recover"
                    % (configured, state[0]))
        # Set by the sweep while /dev/shm is under its reserve; admissions are refused meanwhile.
        self._shm_low = False
        # Free space is under the reserve (whether or not any worker is to blame), and whether this
        # episode of it has been logged yet.
        self._shm_short = False
        self._shm_logged = False
        # Blind holders (nameless mappings) are blamed for unexplained space only while stopping one
        # has been lowering it: ``pending`` is the unexplained figure when one was last stopped, and
        # ``off`` ends the blame for the rest of the episode once that figure did not go down.
        self._shm_blind_pending = None
        self._shm_blind_off = False
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
        # Plugin key -> workers admitted whose end has not been judged yet. An admission
        # takes one unit of the allowance before the check can be raced, and the unit
        # is turned into a record or given back when the worker's end is known.
        self._unverified_reserved: dict[str, int] = {}
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

    def unregister(self, key: str) -> bool:
        """Stop the worker (and any retiring one) and forget the definition.

        Returns whether every teardown of the key is confirmed by the time this returns. False
        means a process of the key is stuck (or a start is still being cut loose): it is ended
        again on every sweep, and nothing of that key starts until it is gone.
        """
        with self._cond:
            entry = self._entries.pop(key, None)
            if entry is None:
                return not self._tearing.get(key)
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
        return self.teardown_confirmed(key)

    def teardown_confirmed(self, key: str) -> bool:
        """Whether no worker of the key is still being torn down or stuck (its processes are confirmed gone)."""
        with self._cond:
            return not self._tearing.get(key)

    def _detach(self, slot):
        """Cut a start loose from its entry. Caller holds the lock.

        The worker may already be spawning, so from now on its plugin key has an
        unconfirmed teardown: nothing of that key starts beside it, even if the key
        is registered again, until the start has ended and its teardown is confirmed.
        """
        self._detached.add(slot)
        self._tearing.setdefault(slot.definition.key, set()).add(slot)
        # A runtime that can cut its own start short (a long image pull) is asked to, without waiting.
        cancel = getattr(slot.session, "cancel_start", None)
        if callable(cancel):
            try:
                cancel()
            except Exception as error:
                _log_failure("cancelling a start failed", error)

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
                    # Only failures still inside the breaker window: the others no longer count toward it and
                    # are dropped when the next failure is recorded.
                    "failures": sum(1 for stamp in entry.failures if now - stamp <= self.limits.breaker_window),
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
                "shm_low": self._shm_low,
                "shm_short": self._shm_short,
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
                    refused = WorkerKilled(problem[1], code=problem[0])
                    refused.before_send = True
                    raise refused
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
            self._check_key_free(definition.key, cap_key=definition.count_key)
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
                self._check_key_free(definition.key, slot, definition.count_key)
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
                refused = WorkerKilled(problem[1], code=problem[0])
                refused.before_send = True
                raise refused
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

    def _check_key_free(self, key, slot=None, cap_key=None):
        """Refuse a start of ``key`` while a worker of it is being torn down, or the cap is spent. Lock held.

        The cap is counted under ``cap_key`` (default ``key``): the definition's ``count_key``.

        With ``slot``, the check and the reservation of one unit of the allowance for
        it are one step: concurrent admissions cannot all pass a check that only one fits.
        """
        if self._tearing.get(key):
            raise PoolUnavailable("the previous worker of %s is still being stopped" % key,
                                  code=PoolCodes.UNAVAILABLE, retry_after=1.0)
        counted = cap_key or key
        wait = self._unverified_wait(counted)
        if wait:
            raise PoolUnavailable(
                "%s has had %d workers ended without whole-tree cleanup within the last hour; "
                "detached processes cannot be tracked on this platform, so starts are paused"
                % (counted, self.limits.unverified_limit), code=PoolCodes.UNVERIFIED_LIMIT, retry_after=wait)
        if slot is not None:
            self._unverified_reserved[counted] = self._unverified_reserved.get(counted, 0) + 1
            slot.unverified_reserved = True

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

    def stop(self, key: str) -> bool:
        """Stop the plugin's workers on purpose: the current one, every draining one, a start in progress.

        A start that has not finished is cancelled: its worker is never
        published and its caller gets ``PoolUnavailable``. Busy workers are
        stopped politely and killed when the grace ends. Returns when the
        teardown is over (bounded); a worker in backoff stays in backoff. The result is whether
        every teardown of the key is confirmed: False means a process is stuck (it is ended again
        on every sweep) and the plugin reads as stopping until it is gone.
        """
        with self._cond:
            entry = self._entry(key)
            starting = entry.starting
            if starting is not None and starting not in self._detached:
                starting.cancel_reason = "stopped"
                self._detach(starting)
                # The start is counted as a detached one from here on; the entry stops
                # counting it as resident in the same hold, so it is counted once.
                if entry.state == WorkerState.STARTING:
                    self._set_state(entry, WorkerState.STOPPED, "stopped")
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
        return self.teardown_confirmed(key)

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
        self._guard_shm()
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
            for key, entry in self._entries.items():
                if entry.state == WorkerState.DRAINING and not entry.retiring and entry.slot is None:
                    # Its last draining worker has been queued for teardown, not torn down:
                    # the entry reads as stopped only once every teardown of the key is
                    # confirmed (``_teardown_confirmed`` does that).
                    if self._tearing.get(key):
                        self._set_state(entry, WorkerState.STOPPING, "drained")
                    else:
                        self._set_state(entry, WorkerState.STOPPED, "drained")
            self._cond.notify_all()
        self._flush()

    def _shm_reserve(self, total: int) -> int:
        """Free space under which /dev/shm counts as nearly full, never more than half of it.

        The default is the larger of 8 MiB and a tenth of the filesystem, which on a small one
        (a 4 MiB /dev/shm) would be more than it can ever have free: the guard would latch for
        good. So it is held to half the size. A configured value above the size is refused when
        the pool is built (``__init__``); if the filesystem later turns out smaller, it is held
        to half of it as well.
        """
        configured = self.limits.shm_reserve_bytes
        wanted = max(8 * MIB, total // 10) if configured is None else configured
        return min(wanted, total // 2) if (configured is None or configured > total) else wanted

    def _guard_shm(self):
        """Watch the free space of /dev/shm, a size-limited tmpfs shared by every worker.

        Under the reserve, and only while some live worker holds a meaningful share of the
        shortfall (``SHM_MIN_HOLDER_BYTES``, ``SHM_SHARE_DIVISOR``): no new start or request is
        admitted (``_violation``) and the worker holding the most is stopped, one per sweep,
        until the space is back above the reserve or no worker holds a meaningful share. When
        nobody does, stopping anyone cannot help (what a stopped worker left behind and what
        the host keeps there never recover), so work is admitted. Either way the episode is
        logged once. A /dev/shm that is not small and separate cannot be guarded; the sandbox
        limitations say so.
        """
        try:
            state = self._shm_reader()
        except Exception as error:
            _log_failure("reading /dev/shm failed", error)
            state = None
        if not shm_guardable(state):
            with self._cond:
                self._shm_low = False
                self._shm_short = False
                self._shm_logged = False
                self._shm_blind_pending = None
                self._shm_blind_off = False
            return
        total, free, _separate = state
        reserve = self._shm_reserve(total)
        low = free < reserve
        with self._cond:
            self._shm_short = low
            workers = []
            if low:
                workers = [(entry, entry.slot) for entry in self._entries.values() if entry.slot is not None]
                workers += [(entry, slot) for entry in self._entries.values() for slot in entry.retiring]
                workers += [(None, slot) for slot in self._ephemeral]
            else:
                self._shm_low = False
                self._shm_logged = False
                self._shm_blind_pending = None
                self._shm_blind_off = False
        if not low:
            return
        shortfall = reserve - free
        threshold = max(SHM_MIN_HOLDER_BYTES, shortfall // SHM_SHARE_DIVISOR)
        usages = []
        for entry, slot in workers:
            session = slot.session
            process = session.process if session is not None else None
            if process is None or process.returncode is not None:
                continue
            budget = self._new_budget()
            pids, _incomplete = _process_tree(process.pid, 256, budget)
            skip = {process.pid} if isinstance(process, LauncherProcess) else set()
            usages.append((shm_usage_detail(pids, skip, budget), entry, slot))
        unexplained = 0
        blind_blame = False
        with self._cond:
            pending = self._shm_blind_pending
            blind_off = self._shm_blind_off
        if pending is not None or (not blind_off and any(blind for (_h, _c, blind), _e, _s in usages)):
            # Mappings whose name is gone have a size nobody can read. The space in use that no listed
            # file accounts for is what they (or the host's own deleted files) may be hiding: only when
            # that alone is meaningful are the workers holding such mappings to blame. A listing that was
            # cut short only gives a floor for what is listed, so the unexplained figure is an upper bound
            # and nobody is blamed on it.
            try:
                listed, truncated = self._shm_lister()
            except Exception as error:
                _log_failure("listing /dev/shm failed", error)
                listed, truncated = 0, True
            unexplained = max(0, (total - free) - listed)
            blind_blame = not truncated
            if pending is not None:
                # The last blind holder was stopped: if that did not lower the unexplained space, the
                # space is not theirs (the host's, or files that stay) and no more of them are blamed.
                with self._cond:
                    self._shm_blind_pending = None
                    if unexplained >= pending:
                        self._shm_blind_off = True
                        blind_off = True
        blind_blame = blind_blame and not blind_off
        heaviest = None
        for (held, count, blind), entry, slot in usages:
            if held >= threshold:
                rank = (1, held, count)
            elif blind and blind_blame and unexplained >= threshold:
                rank = (0, blind, count)
            else:
                continue
            if heaviest is None or rank > heaviest[0]:
                heaviest = (rank, entry, slot, held, count, blind)
        with self._cond:
            self._shm_low = heaviest is not None
            first = not self._shm_logged
            self._shm_logged = True
        if first:
            if heaviest is not None:
                logger.warning("/dev/shm has %d MiB free, under its %d MiB reserve: nothing new is admitted and "
                               "the heaviest user is stopped", free // MIB, reserve // MIB)
            else:
                logger.warning("/dev/shm has %d MiB free, under its %d MiB reserve, and no worker holds a "
                               "meaningful share of the shortfall: work is still admitted", free // MIB,
                               reserve // MIB)
        if heaviest is None:
            return
        rank, entry, slot, held, count, blind = heaviest
        if not rank[0]:
            with self._cond:
                self._shm_blind_pending = unexplained
        if rank[0]:
            problem = (PoolCodes.SHM_LOW, "worker holds %d MiB (%d mappings) of /dev/shm, which is nearly full" % (
                held // MIB, count))
        else:
            problem = (PoolCodes.SHM_LOW, "worker keeps %d mappings of /dev/shm objects with no name or descriptor, "
                       "and space that no file explains is nearly all that is left" % blind)
        if entry is not None:
            self._kill_current(entry, slot, problem)
        else:
            slot.kill_reason = problem
            self._terminate_now(slot)

    def _sweep_current(self, entry, slot, now):
        with self._cond:
            if entry.slot is not slot or entry.state != WorkerState.READY:
                return
            if not slot.session.alive:
                idle = slot.active == 0 and slot.pending == 0
                self._handle_death(entry, slot, self._death_reason(slot), idle=idle)
                return
        budget = self._new_budget()  # one allowance for all the reading this sweep does for the worker
        self._sample_if_due(slot, now, budget)
        definition = slot.definition
        problem = None
        problem = self._memory_problem(slot, definition.memory_limit())
        if problem is None:
            problem = self._budget_problem(slot, now, definition, budget=budget)
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
        budget = self._new_budget()
        self._sample_if_due(slot, now, budget)
        problem = self._memory_problem(slot, slot.definition.memory_limit())
        if problem is not None:
            slot.kill_reason = (problem[0], "retiring worker: " + problem[1])
            with self._cond:
                if slot in entry.retiring:
                    entry.retiring.remove(slot)
                self._queue_close(slot, None, "kill")
            return
        # A draining worker still writes: its own folders, as its own settings named them.
        problem = self._slot_budget_problem(slot, now, budget)
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
        budget = self._new_budget()
        self._sample_if_due(slot, now, budget)
        problem = self._memory_problem(slot, slot.definition.memory_limit())
        if problem is not None:
            slot.kill_reason = problem
        else:
            problem = self._slot_budget_problem(slot, now, budget)
            if problem is None:
                return
            slot.kill_reason = problem
        self._terminate_now(slot)

    def _new_budget(self) -> SampleBudget:
        """A fresh allowance for one sweep's reading of one worker's /proc."""
        return SampleBudget(self.limits.sample_seconds, self.limits.sample_max_entries,
                            self.limits.sample_max_bytes, self.limits.sample_max_kept)

    def _slot_budget_problem(self, slot, now, budget=None):
        """The folder budget problem of a worker that is not the entry's current one, or None.

        Checked against the slot's own definition and no more often than
        ``budget_interval``.
        """
        if now - slot.last_budget_at < self.limits.budget_interval:
            return None
        slot.last_budget_at = now
        return self._budget_problem(None, now, definition=slot.definition, force=True, tree=slot, budget=budget)

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
                    self._check_key_free(key, cap_key=entry.definition.count_key)
                    if not self._claim_capacity(entry):
                        self._wait(deadline, "no free worker slot", code=PoolCodes.POOL_FULL)
                        continue
                    self._set_state(entry, WorkerState.STARTING, "request")
                    entry.starts += 1
                    definition = entry.definition
                    slot = _Slot(definition, now, self.limits)
                    slot.entry = entry
                    slot.generation = entry.generation
                    # Admitted: the unit is taken here, in the hold that checked the cap.
                    self._reserve_unverified(definition.count_key, slot)
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
                self._settle_unverified(slot)
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
        with self._cond:
            if slot in self._detached:
                # Cut loose before the session existed: tell it before it starts.
                self._detach(slot)
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
        else:
            self._settle_unverified(slot)
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
                elif (error is None or isinstance(error, RequestError)) and getattr(
                        session, "answers_prove_health", True):
                    # The worker answered. A clean answer closes the breaker, unless the runtime says its
                    # answers say nothing about the process (a managed container answers locally).
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
        if self._shm_low:
            return (PoolCodes.SHM_LOW, "shared memory (/dev/shm) is nearly full; nothing new is admitted until it recovers")
        free = self._free_space()
        if definition.writes_data and free is not None and free < self.limits.config_reserve_bytes:
            return (PoolCodes.DISK_RESERVE,
                    "free space on the config volume (%d MiB) is under the %d MiB reserve" % (
                        free // MIB, self.limits.config_reserve_bytes // MIB))
        return self._budget_problem(holder, self._clock(), definition, force=fresh or holder is None, tree=tree)

    def _budget_problem(self, holder, now, definition, force=False, tree=None, budget=None):
        """The folder-budget problem of ``definition``, cached on ``holder`` (a slot) when given.

        ``tree`` is the slot whose process tree is also searched for files that were
        unlinked but are still held open (and so still use the folder's disk).
        """
        if not definition.budgets:
            return None
        if holder is not None and not force and now - holder.last_budget_check < self.limits.budget_interval:
            return holder.budget_problem
        problem = None
        files: dict = {}
        held, held_incomplete = self._deleted_open_usage(tree or holder, definition.budgets, files, budget)
        for budget in definition.budgets:
            links = _LinkTally(files.get(budget.path))
            used, truncated = folder_usage(budget.path, self.limits.budget_max_entries,
                                           self.limits.budget_scan_seconds, links)
            # A deleted file that still has a link in the folder was charged by the scan.
            used += held.get(budget.path, 0) - links.deleted_charged()
            truncated = truncated or held_incomplete
            if truncated or used > budget.max_bytes:
                problem = (PoolCodes.FOLDER_BUDGET, "%s is over its %d MiB budget" % (
                    budget.label or "a worker folder", budget.max_bytes // MIB))
                break
        if holder is not None:
            holder.last_budget_check = now
            holder.budget_problem = problem
        return problem

    def _deleted_open_usage(self, slot, budgets, files=None, budget=None):
        """``deleted_open_usage`` for the worker's whole tree; ``({}, False)`` when there is no tree to look at."""
        session = getattr(slot, "session", None)
        process = session.process if session is not None else None
        if process is None or process.returncode is not None or not os.path.isdir("/proc/self/fd"):
            return {}, False
        if budget is None:
            budget = self._new_budget()
        pids, incomplete = _process_tree(process.pid, 256, budget)
        if isinstance(process, LauncherProcess):
            # The launcher is the pool's own, trusted and non-dumpable: its descriptors
            # are closed to us by design and are not the worker's. Its descendants are.
            pids = [pid for pid in pids if pid != process.pid]
        usage, partial = deleted_open_usage(pids, [folder.path for folder in budgets], files, budget=budget)
        return usage, incomplete or partial or budget.exhausted

    def _preflight_running(self, entry, slot):
        """Before a request on a running worker: disk reserve and folder budgets."""
        problem = self._violation(slot.definition, slot)
        if problem is None:
            return
        if problem[0] not in (PoolCodes.DISK_RESERVE, PoolCodes.SHM_LOW):
            # A worker over its folder budget is ended. A short disk or full shared memory
            # only refuses work (the guard stops the heaviest /dev/shm user on its own).
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

    def _sample(self, slot, budget=None):
        session = slot.session
        process = session.process if session else None
        if process is None or process.returncode is not None:
            # Nothing local, or a leader that was reaped: its number may belong to someone else now.
            return
        try:
            if self._memory_reader is read_process_tree_memory:
                skip = {process.pid} if isinstance(process, LauncherProcess) else ()
                sample = read_process_tree_memory(process.pid, skip_fds=skip,
                                                  budget=budget if budget is not None else self._new_budget())
            else:
                sample = self._memory_reader(process.pid)
        except Exception:
            # A reading that failed is not a reading of zero.
            sample = MemorySample(bytes=0, rss_bytes=0, processes=0, source="error", truncated=True)
        if sample is not None and sample.truncated and not getattr(session, "alive", True):
            # The reading overlapped the end of the worker (the session closes before it kills, and
            # a /proc entry of a process on its way out reads as unreadable). Its tree is not
            # too big to measure, it is gone, and the error that ended it is the one to report.
            # A worker that is still running keeps the fail-closed rule below.
            return
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
                    "worker's memory could not be measured in full (too many processes, or part of its /proc "
                    "was unreadable), counted as over its %d MiB limit" % (limit // MIB))
        return (PoolCodes.MEMORY_LIMIT, "worker used %d MiB, over its %d MiB limit" % (
            slot.memory_bytes // MIB, limit // MIB))

    def _sample_if_due(self, slot, now, budget=None):
        if slot.busy() or now - slot.last_sample_at >= self.limits.heartbeat_interval:
            self._sample(slot, budget)

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
                process._anchor is not None or process._anchor_lost or process._leader_lost
                or process.group_cleaned is False):
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

    def _reserve_unverified(self, key, slot):
        self._unverified_reserved[key] = self._unverified_reserved.get(key, 0) + 1
        slot.unverified_reserved = True

    def _settle_unverified(self, slot):
        """Give back the unit a worker held (it ended without a record, or the record replaces it). Lock held."""
        if not slot.unverified_reserved:
            return
        slot.unverified_reserved = False
        key = slot.definition.count_key
        left = self._unverified_reserved.get(key, 0) - 1
        if left > 0:
            self._unverified_reserved[key] = left
        else:
            self._unverified_reserved.pop(key, None)

    def _note_unverified(self, slot):
        """Count a worker whose tree could only be ended by its process group. Lock held.

        Where there is no subreaper launcher (a platform without one), a descendant
        that detached itself from the group cannot be seen or ended, so "gone" rests
        on the group alone. That is accepted, but counted, reported once per key and
        capped, so a plugin that keeps leaking cannot do so without limit.
        """
        if slot.unverified_noted:
            return
        slot.unverified_noted = True
        session = slot.session
        process = session.process if session is not None else None
        if process is None or isinstance(process, LauncherProcess):
            self._settle_unverified(slot)
            return
        if slot.ephemeral and slot.kill_reason is None and getattr(session, "exit_code", None) == 0:
            # A one-off run whose worker left by itself, with exit code 0, after being asked to and without the pool
            # having ended it: it is the routine end of a short request, not a plugin that leaks. Counting it would turn the cap into a
            # quota on how often a person may test a connection or fetch a model list.
            self._settle_unverified(slot)
            return
        key = slot.definition.count_key
        now = self._clock()
        for other in [name for name, held in self._unverified.items() if not held and name != key]:
            del self._unverified[other]
        stamps = self._unverified.setdefault(key, deque())
        stamps.append(now)
        # The record replaces the unit that was held for it: same place, counted once.
        self._settle_unverified(slot)
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
        reserved = self._unverified_reserved.get(key, 0)
        now = self._clock()
        if stamps:
            self._prune_unverified(stamps, now)
        recorded = len(stamps) if stamps else 0
        if recorded + reserved < self.limits.unverified_limit:
            return 0.0
        if stamps and recorded >= self.limits.unverified_limit:
            return max(0.0, stamps[0] + self.limits.unverified_window - now)
        # Held up by admitted workers whose end is not known yet: try again shortly.
        return 1.0

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
        if self._tearing.get(key):
            return
        # The last teardown of this key is confirmed, whichever worker it was (a one-off
        # run has no entry of its own): the plugin registered under the key, and the
        # entry this worker belonged to, read as stopped if nothing is left of them.
        owner = slot.entry or entry
        for candidate in (self._entries.get(key), owner):
            if (candidate is not None and candidate.state == WorkerState.STOPPING and candidate.slot is None
                    and not candidate.retiring):
                self._set_state(candidate, WorkerState.STOPPED, "stopped")

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

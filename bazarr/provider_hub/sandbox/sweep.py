# coding=utf-8
"""Boot-time sweep of plugin processes a previous run left behind.

When Bazarr+ dies hard (a kill, a crash, an out-of-memory), its launchers see the
lifeline close and clean up on their own. A launcher that is still running but whose
cleanup wedged, or a worker that moved itself into a session of its own, is left
behind, and a detached session is out of every group kill. The sweep finds those at the
next boot and ends them. A session whose leader is gone (a launcher that was killed too)
is refused, not swept: see the last paragraph.

Nothing is signalled by number. A pid or a group id can be handed to a stranger the
moment its holder is gone, so the sweep never calls ``kill`` or ``killpg`` on one. Each
process is pinned first with ``pidfd_open``, identified again through that handle, and
signalled with ``pidfd_send_signal``, which reaches the process the handle pins and no other.
Without ``pidfd_open`` and ``pidfd_send_signal`` (an old kernel, a seccomp profile, a
platform that is not Linux) the sweep does nothing.

Ownership comes from a marker, never from a guess. Once ``configure_owner`` has run,
every worker the host starts through a launcher, and everything that worker starts,
carries one environment variable (the launcher itself is non-dumpable, so its own
environment cannot be read; it is found as the leader of its workers' session)::

    BAZARR_SANDBOX_OWNER=<instance>:<host pid>:<host start time>

``instance`` is a digest of the config folder's real path, so two Bazarr+ installs with
different config folders on one machine never touch each other's workers. It does not tell
apart two installs that share both a config folder and a pid namespace under the same user
(``--pid=host``, or a pod with a shared process namespace): a live run is still protected by
the owner check, but one of them can end the other's real leftovers. The pid and ``/proc``
start time identify the host process that owned the launcher, the same identity mark the pool
uses for a child's pid.

A session is swept only when all of this holds, and a check that cannot be made counts as
failed:

* the scan was complete: the listing was not cut short and every entry was read in time;
* a live process of the same user, in this pid namespace, carries our instance's marker.
  The marker is bound to the process: its ``/proc`` entry must show the same start time
  and session before and after the environment was read, or the pid is skipped;
* its owner is gone: no process has the owner's pid and start time, and the pid answers
  ``ESRCH`` to signal 0. A pid that is taken by anything else is "unknown", not "gone";
* the session has a live leader that is a member of it. A leader with a readable marker is
  ours by it. The launcher never has one (it is non-dumpable, so its environment is
  unreadable, or empty for root), and is accepted instead by the launcher identity rule in
  ``_launcher_identity``: our uid, the launcher's argv, and a pin that re-verifies both;
* the session is not ours: not this process's session or group, not the init session,
  holds none of its processes and no child of it (a child of ours is a live worker of
  this run; the sweep calls ``wait`` on nothing, so nothing is reaped by it);
* each member it will signal still shows, through its own pidfd, the start time and
  session the scan saw, and at least one marked member still carries the same marker.

A live leader gets ``SIGTERM`` first, which makes a launcher end its own tree (setsid
children included) and wait for it. Whatever verified member is still running after the
grace gets ``SIGKILL``, one by one. Processes that were not there when the scan ran are not
touched. A session whose leader is gone, as when a launcher was killed too, is always left
alone, since nothing then ties its members to one another that the sweep can prove; the
members of such a session that are not detached survive every boot.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import select
import signal
import threading
import time
from dataclasses import dataclass, field

from . import spawn

logger = logging.getLogger(__name__)

__all__ = [
    "SweepResult",
    "SweptSession",
    "configure_owner",
    "instance_key",
    "start_sweep_in_background",
    "sweep_leftovers",
]

# Most of an environment the sweep reads, and of /proc it walks. Past either the scan is
# partial, and a partial scan acts on nothing.
ENVIRON_READ_LIMIT = 256 * 1024
MAX_PROC_ENTRIES = 131072
SCAN_BUDGET_SECONDS = 10.0
# How long a live launcher is given to end its own tree before the members are killed. The
# launcher's own cleanup needs up to LAUNCHER_CLEANUP_SECONDS after its worker's grace.
TERM_WAIT_SECONDS = spawn.LAUNCHER_CLEANUP_SECONDS + 5.0
POLL_SECONDS = 0.1
KILL_WAIT_SECONDS = 3.0


@dataclass(frozen=True)
class _Proc:
    pid: int
    state: str
    ppid: int
    pgrp: int
    session: int
    start: str


@dataclass
class SweptSession:
    """What happened to one session. ``outcome`` is ``ended``, ``survivors`` or ``refused``."""

    session: int
    pids: tuple[int, ...]
    outcome: str
    detail: str = ""


@dataclass
class SweepResult:
    supported: bool = True
    reason: str = ""
    scanned: int = 0
    truncated: bool = False
    sessions: list[SweptSession] = field(default_factory=list)
    unreadable: int = 0

    @property
    def ended(self) -> int:
        return sum(1 for item in self.sessions if item.outcome == "ended")


def instance_key(config_dir: "str | os.PathLike") -> str:
    """A short stable name for one config folder."""
    real = os.path.realpath(os.fspath(config_dir))
    return hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _has_proc() -> bool:
    return spawn._has_proc()


def configure_owner(config_dir: "str | os.PathLike") -> "str | None":
    """Start marking the workers this process starts. Returns the marker, or None where it cannot.

    Call once at boot, before any worker starts. Without ``/proc`` (or an unreadable start
    time for this process) nothing can be proved later, so nothing is marked.
    """
    start = spawn._start_mark(os.getpid()) if _has_proc() and _proc_is_ours() else None
    if start is None:
        spawn.set_owner_marker(None)
        return None
    marker = "%s:%d:%s" % (instance_key(config_dir), os.getpid(), start)
    spawn.set_owner_marker(marker)
    return marker


# --------------------------------------------------------------------------- /proc readers


def _read_proc(pid: int) -> "_Proc | None":
    """One process's fields, or None when it is gone or its entry is unreadable."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read(4096)
        rest = data.rsplit(b")", 1)[1].split()
        return _Proc(
            pid=pid,
            state=rest[0].decode("ascii", "replace"),
            ppid=int(rest[1]),
            pgrp=int(rest[2]),
            session=int(rest[3]),
            start=rest[19].decode("ascii", "replace"),
        )
    except (OSError, IndexError, ValueError):
        return None


def _uids_of(pid: int) -> "tuple[int, int] | None":
    """The real and effective uid from ``/proc/<pid>/status``.

    Not the owner of ``/proc/<pid>``: a non-dumpable process (every launcher) has a root-owned
    entry whatever its uid is, while its ``status`` stays readable.
    """
    try:
        with open("/proc/%d/status" % pid, "rb") as handle:
            for line in handle.read(8192).split(b"\n"):
                if line.startswith(b"Uid:"):
                    fields = line.split()
                    return int(fields[1]), int(fields[2])
    except (OSError, IndexError, ValueError):
        pass
    return None


def _ours(pid: int, euid: int) -> bool:
    return _uids_of(pid) == (euid, euid)


def _read_small(path: str, limit: int = 65536) -> "bytes | None":
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _read_cmdline(pid: int) -> "list[str] | None":
    """The argv of a process, None when it cannot be read or is empty."""
    data = _read_small("/proc/%d/cmdline" % pid)
    if not data:
        return None
    return [part.decode("utf-8", "replace") for part in data.split(b"\0") if part]


def _proc_is_ours() -> bool:
    """Whether ``/proc`` is the one of this pid namespace, so that its pids are the ones we signal by.

    ``/proc/self`` must be a link to this process's own pid, ``status`` must list exactly
    one pid for it (``NSpid``, a single value: the namespace of this ``/proc``) equal to
    ``os.getpid()``, and ``self`` and ``thread-self`` must name the same pid namespace. A
    ``/proc`` of another pid namespace fails at least one of them.
    """
    me = os.getpid()
    try:
        if os.readlink("/proc/self") != str(me):
            return False
        with open("/proc/self/status", "rb") as handle:
            lines = handle.read(8192).split(b"\n")
    except OSError:
        return False
    nspid = [line.split()[1:] for line in lines if line.startswith(b"NSpid:")]
    if len(nspid) != 1 or len(nspid[0]) != 1 or nspid[0][0] != str(me).encode("ascii"):
        return False
    mine = _pid_namespace("self")
    return mine is not None and mine == _pid_namespace("thread-self")


def _pid_namespace(pid: "int | str") -> "str | None":
    try:
        return os.readlink("/proc/%s/ns/pid" % pid)
    except OSError:
        return None


def _same_pid_namespace(pid: int) -> bool:
    """Whether the process lives in this process's pid namespace. Unreadable counts as no."""
    mine = _pid_namespace("self")
    return mine is not None and _pid_namespace(pid) == mine


def _read_marker(pid: int) -> "str | None":
    """The owner marker in a process's environment. None for none or cut short.

    ``PermissionError`` is raised, so that a process that hides its environment is told
    apart from one that has no marker; any other failure reads as no marker.
    """
    try:
        with open("/proc/%d/environ" % pid, "rb") as handle:
            data = handle.read(ENVIRON_READ_LIMIT + 1)
    except PermissionError:
        raise
    except OSError:
        return None
    if len(data) > ENVIRON_READ_LIMIT:
        return None
    prefix = (spawn.OWNER_VARIABLE + "=").encode("ascii")
    for entry in data.split(b"\0"):
        if entry.startswith(prefix):
            try:
                return entry[len(prefix):].decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


# The largest pid Linux hands out is 2**22; a start time is a count of clock ticks.
MAX_MARKER_PID = 1 << 22
MAX_MARKER_START_DIGITS = 20


def _parse_marker(value: str) -> "tuple[str, int, str] | None":
    parts = value.split(":")
    if len(parts) != 3 or not parts[0] or len(parts[0]) > 64:
        return None
    if not (parts[1].isascii() and parts[1].isdigit() and parts[2].isascii() and parts[2].isdigit()):
        return None
    if len(parts[1]) > 10 or len(parts[2]) > MAX_MARKER_START_DIGITS:
        return None
    try:
        pid = int(parts[1])
    except (ValueError, OverflowError):
        return None
    if not 0 < pid <= MAX_MARKER_PID:
        return None
    return parts[0], pid, parts[2]


def _same_process(now: "_Proc | None", before: _Proc) -> bool:
    return now is not None and now.start == before.start and now.session == before.session


def _bound_marker(entry: _Proc) -> "str | None":
    """The marker of exactly the process ``entry`` describes, or None when that cannot be said.

    The environment is read between two reads of the process's stat; both must show the same
    start time and session as the table entry. A pid that was handed to another process in
    between shows a different start time, and the result is None.
    """
    value = _read_marker(entry.pid)
    if value is None or not _same_process(_read_proc(entry.pid), entry):
        return None
    return value


def _list_pids() -> "tuple[list[int], bool] | None":
    """Every numeric ``/proc`` entry, and whether the list was cut at ``MAX_PROC_ENTRIES``."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    pids = [int(name) for name in names if name.isdigit()]
    return pids[:MAX_PROC_ENTRIES], len(pids) > MAX_PROC_ENTRIES


def _owner_state(value: str) -> str:
    """``alive``, ``gone`` or ``unknown`` for the host process a marker names.

    ``gone`` needs two things: no process shows the owner's pid and start time, and the pid
    answers ``ESRCH`` to signal 0. A pid that is taken by another process (a reused number)
    is not proof that the owner is gone, so it reads as ``unknown``.
    """
    parsed = _parse_marker(value)
    if parsed is None or not _has_proc():
        return "unknown"
    _instance, pid, start = parsed
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read(4096)
    except FileNotFoundError:
        data = None
    except OSError:
        return "unknown"
    if data is not None:
        try:
            current = data.rsplit(b")", 1)[1].split()[19].decode("ascii", "replace")
        except (IndexError, ValueError):
            return "unknown"
        if current == start:
            return "alive"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except (OSError, OverflowError, ValueError):
        return "unknown"
    return "unknown"


# --------------------------------------------------------------------------- the scan


@dataclass
class _Session:
    session: int
    # Marked processes, with the marker each carried, and every live member the scan saw.
    marked: dict[int, str] = field(default_factory=dict)
    members: dict[int, _Proc] = field(default_factory=dict)
    leader: "_Proc | None" = None


def _scan(instance: str, deadline: float, result: SweepResult) -> "tuple[dict[int, _Session], dict[int, _Proc]]":
    me = os.getpid()
    my_euid = os.geteuid()
    listing = _list_pids()
    if listing is None:
        result.supported = False
        result.reason = "/proc cannot be listed"
        return {}, {}
    pids, cut = listing
    result.truncated = cut
    table: dict[int, _Proc] = {}
    for pid in pids:
        if time.monotonic() > deadline:
            result.truncated = True
            break
        entry = _read_proc(pid)
        if entry is not None:
            table[pid] = entry
    result.scanned = len(table)
    if result.truncated:
        return {}, table
    sessions: dict[int, _Session] = {}
    for pid, entry in table.items():
        if pid == me or entry.state in ("Z", "X"):
            continue
        if time.monotonic() > deadline:
            result.truncated = True
            break
        if not _ours(pid, my_euid) or not _same_pid_namespace(pid):
            continue
        try:
            value = _bound_marker(entry)
        except PermissionError:
            result.unreadable += 1
            continue
        if value is None or value == spawn.owner_marker():
            continue
        parsed = _parse_marker(value)
        if parsed is None or parsed[0] != instance:
            continue
        sessions.setdefault(entry.session, _Session(entry.session)).marked[pid] = value
    if time.monotonic() > deadline:
        result.truncated = True
    if result.truncated:
        return {}, table
    for number, item in sessions.items():
        item.leader = table.get(number)
        for pid, entry in table.items():
            if entry.session == number and entry.state not in ("Z", "X"):
                item.members[pid] = entry
    return sessions, table


def _is_launcher_argv(args: "list[str]") -> bool:
    """Whether an argv is the launcher's: ``python -I -S -B .../sandbox/launcher.py <fds>``."""
    wanted = os.fspath(spawn.LAUNCHER_PATH)
    return any(arg == wanted or arg.replace(os.sep, "/").endswith("/sandbox/launcher.py") for arg in args[:6])


def _launcher_identity(leader: _Proc, euid: int) -> bool:
    """Whether an unreadable or marker-less session leader is a launcher of ours.

    The launcher makes itself non-dumpable, so on a host that is not root its environment
    cannot be read, and as root it reads as empty: it never shows a marker. It is accepted
    only as the leader of a session whose marked workers were already proved to belong to a
    dead owner of this install, and only when all of this holds:

    * it runs as our user (``status``, which a non-dumpable process still shows);
    * its argv is the launcher's. An argv that cannot be read is a refusal: nothing weaker
      (a command name, an age) stands in for it.

    The caller pins the process and checks this again through the pin.
    """
    if not _ours(leader.pid, euid):
        return False
    args = _read_cmdline(leader.pid)
    return args is not None and _is_launcher_argv(args)


def _leader_refusal(item: _Session, euid: int) -> "str | None":
    """Why the session's leader does not prove the session, or None when it does."""
    leader = item.leader
    if leader is None:
        return "its leader is gone"
    if leader.state in ("Z", "X") or leader.pid not in item.members or leader.session != item.session:
        return "its leader is gone"
    if leader.pid in item.marked:
        return None
    try:
        value = _read_marker(leader.pid)
        hidden = False
    except PermissionError:
        value, hidden = None, True
    if not _same_process(_read_proc(leader.pid), leader):
        return "its leader changed while it was read"
    if value is not None:
        return "its leader carries a marker the scan did not accept"
    if _launcher_identity(leader, euid):
        return None
    if hidden:
        return "its leader hides its environment and does not read as a launcher"
    return "its leader's environment carries no marker and does not read as a launcher"


def _refusal(item: _Session, me: int, my_session: int, my_group: int, my_euid: int) -> "str | None":
    """Why this session must not be touched, or None when it may be swept."""
    if item.session <= 1 or item.session in (my_session, my_group):
        return "it is this process's own session"
    if any(entry.pgrp in (my_group, my_session) or entry.pid == me for entry in item.members.values()):
        return "it holds this process's group"
    if any(entry.ppid == me for entry in item.members.values()):
        return "it holds a child of this process, which is a live worker of this run"
    if any(not _ours(pid, my_euid) for pid in item.members):
        return "it holds a process of another user"
    if not item.marked:
        return "no member carries the marker"
    why = _leader_refusal(item, my_euid)
    if why is not None:
        return why
    if not all(_owner_state(value) == "gone" for value in item.marked.values()):
        return "its owner is still running, or cannot be told apart from a running one"
    return None


# --------------------------------------------------------------------------- signalling


def _pidfd_available() -> bool:
    """Whether this host can pin and signal a process by handle, probed with this process."""
    if not (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
        return False
    try:
        os.close(os.pidfd_open(os.getpid()))
    except OSError:
        return False
    return True


def _exited(fd: int) -> bool:
    """Whether the process a pidfd pins has exited."""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


@dataclass
class _Held:
    entry: _Proc
    fd: int
    marker: "str | None"


def _hold(entry: _Proc, marker: "str | None") -> "_Held | None":
    """Pin the process, then check through the pin that it is still the one the scan saw.

    The handle names one process for good, so a number that is handed to another process
    afterwards cannot be reached through it. A process that was replaced before the handle
    was opened shows a different start time or session and is refused here.
    """
    try:
        fd = os.pidfd_open(entry.pid)
    except OSError:
        return None
    try:
        if _exited(fd):
            raise OSError(errno.ESRCH, "exited")
        now = _read_proc(entry.pid)
        if not _same_process(now, entry) or now.state in ("Z", "X"):
            raise OSError(errno.ESRCH, "changed")
        if marker is not None:
            if _read_marker(entry.pid) != marker or not _same_process(_read_proc(entry.pid), entry):
                raise OSError(errno.ESRCH, "marker changed")
        if _exited(fd):
            raise OSError(errno.ESRCH, "exited")
        return _Held(entry, fd, marker)
    except OSError:
        os.close(fd)
        return None


def _send(held: _Held, sig: int) -> bool:
    if _exited(held.fd):
        return False
    try:
        signal.pidfd_send_signal(held.fd, sig)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            logger.warning("Provider Hub: could not signal leftover plugin process %d: %s", held.entry.pid, exc)
        return False
    return True


def _wait_exited(held: "list[_Held]", timeout: float) -> "list[_Held]":
    """The held processes still running after up to ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        left = [item for item in held if not _exited(item.fd)]
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(POLL_SECONDS)


def _end(item: _Session, result: SweepResult, term_wait: float) -> None:
    pids = tuple(sorted(item.members))
    held: list[_Held] = []
    try:
        leader = _hold(item.leader, item.marked.get(item.leader.pid))
        if leader is None:
            result.sessions.append(SweptSession(item.session, pids, "refused", "its leader is gone or changed"))
            return
        held.append(leader)
        if leader.marker is None and not _launcher_identity(leader.entry, os.geteuid()):
            result.sessions.append(SweptSession(item.session, pids, "refused", "its leader no longer reads as a launcher"))
            return
        for pid, entry in item.members.items():
            if pid == leader.entry.pid:
                continue
            member = _hold(entry, item.marked.get(pid))
            if member is not None:
                held.append(member)
        if not any(h.marker is not None for h in held):
            result.sessions.append(SweptSession(item.session, pids, "refused", "no marked member could be confirmed"))
            return
        if _send(leader, signal.SIGTERM):
            _wait_exited([leader], term_wait)
        for member in held:
            _send(member, signal.SIGKILL)
        left = _wait_exited(held, KILL_WAIT_SECONDS)
        if left:
            alive = sorted(h.entry.pid for h in left)
            result.sessions.append(SweptSession(item.session, pids, "survivors", "still running: %s" % alive))
            logger.warning("Provider Hub: leftover plugin processes survived the sweep: %s", alive)
        else:
            result.sessions.append(SweptSession(item.session, pids, "ended"))
    finally:
        for h in held:
            try:
                os.close(h.fd)
            except OSError:
                pass


def sweep_leftovers(
    config_dir: "str | os.PathLike | None" = None,
    *,
    instance: "str | None" = None,
    term_wait: float = TERM_WAIT_SECONDS,
) -> SweepResult:
    """End the processes a previous run of this install left behind. Never raises.

    ``config_dir`` (or ``instance``, an ``instance_key``) names the install; without one
    nothing can be proved and nothing is signalled.
    """
    result = SweepResult()
    try:
        if instance is None and config_dir is not None:
            instance = instance_key(config_dir)
        if not instance:
            result.supported = False
            result.reason = "no instance named"
            return result
        if not _has_proc():
            result.supported = False
            result.reason = "no /proc: leftover processes cannot be told from strangers"
            logger.debug("Provider Hub: leftover process sweep skipped: %s", result.reason)
            return result
        if not _proc_is_ours():
            result.supported = False
            result.reason = "/proc is not the one of this pid namespace, so its entries prove nothing"
            logger.debug("Provider Hub: leftover process sweep skipped: %s", result.reason)
            return result
        if not _pidfd_available():
            result.supported = False
            result.reason = "no pidfd_open and pidfd_send_signal: a process cannot be pinned before it is signalled"
            logger.debug("Provider Hub: leftover process sweep skipped: %s", result.reason)
            return result
        deadline = time.monotonic() + SCAN_BUDGET_SECONDS
        found, _ = _scan(instance, deadline, result)
        if result.truncated:
            result.reason = "the /proc scan was partial, so nothing was signalled"
            logger.warning("Provider Hub: %s", result.reason)
            return result
        me, my_session, my_group, my_euid = os.getpid(), os.getsid(0), os.getpgrp(), os.geteuid()
        for number in sorted(found):
            item = found[number]
            refusal = _refusal(item, me, my_session, my_group, my_euid)
            if refusal is not None:
                result.sessions.append(SweptSession(number, tuple(sorted(item.members)), "refused", refusal))
                # A session of this run, or of an owner that still runs, is the normal case at boot.
                routine = refusal.startswith(("it is this process's", "it holds this process's", "it holds a child", "its owner is still"))
                logger.log(logging.DEBUG if routine else logging.INFO, "Provider Hub: leftover plugin session %d left alone: %s", number, refusal)
                continue
            _end(item, result, term_wait)
        if result.ended:
            logger.warning("Provider Hub: ended %d plugin session(s) left by a previous run", result.ended)
    except Exception:
        result.supported = False
        result.reason = "the leftover process sweep could not finish"
        logger.exception("Provider Hub: the leftover process sweep failed")
    return result


_sweep_thread: "threading.Thread | None" = None
_sweep_lock = threading.Lock()


def start_sweep_in_background(config_dir: "str | os.PathLike") -> bool:
    """Run the sweep once on a daemon thread, so boot does not wait for a launcher's grace.

    Returns whether a thread started; a second call while one runs does nothing.
    """
    global _sweep_thread
    with _sweep_lock:
        if _sweep_thread is not None and _sweep_thread.is_alive():
            return False
        _sweep_thread = threading.Thread(
            target=sweep_leftovers, args=(config_dir,), name="sandbox-sweep", daemon=True
        )
        _sweep_thread.start()
        return True

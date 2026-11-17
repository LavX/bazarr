# coding=utf-8
"""Host side of the sandbox: probe the kernel, start workers through the launcher.

Fork safety: Bazarr+ is heavily threaded, so the host never forks itself. No
``preexec_fn`` and no bare ``os.fork`` anywhere in this module. A worker starts
as ``subprocess.Popen([python, "-I", "-S", "-B", launcher.py, ...])``: a fresh
single-threaded interpreter that applies the limits and the ruleset and then
``execve``s the worker.

Hosts that can tell nothing about a child (no ``waitid`` that leaves an exit unconsumed,
and no ``/proc``): signal 0 proves nothing there, since a pid that answers may be the
child, a zombie, or a stranger that took the number after an outside reap. The one proof
only a parent can get is ``waitpid(pid, WNOHANG)`` on its own direct child (the leader and
the anchor both are). 0 means it is ours, alive and unreaped: the group is signalled, and
the child reaped by us afterwards. The child's own pid means this call collected it (the
status is recorded as ``Popen`` would): its exit is proven, and that is all. Nothing is
signalled after our own reap, because if the leader was the group's last member the group
number is free the moment it is reaped and may already belong to somebody else. The teardown
is taken as complete and recorded as unverified under the pool's cap; what the leader left
behind is not known, and may survive. ``ECHILD`` means somebody else reaped it, so ownership
is lost and nothing is signalled (the teardown stays stuck). The window left, on the live
path: the child exits and is reaped by an outside reaper, and its pid is reused as a group
leader, all between our ``WNOHANG`` and the signal that follows it. The host installs no
``SIGCHLD`` handler and calls no ``waitpid(-1)``, so nothing in this process reaps a worker
behind ``Popen``'s back; only a reaper outside it, or a thread of an embedding application,
could.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import landlock, seccomp
from .policy import (
    PolicyError,
    RuntimeSpec,
    SandboxPolicy,
    SandboxRefused,
    clean_env,
)

LAUNCHER_PATH = Path(__file__).with_name("launcher.py")

__all__ = [
    "LAUNCHER_PATH",
    "LauncherFailure",
    "LauncherProcess",
    "MIN_ENFORCED_ABI",
    "PROVIDER_MIN_ABI",
    "SandboxApplyError",
    "SandboxProbe",
    "WorkerProcess",
    "clear_provider_notice",
    "note_provider_blocked",
    "note_provider_unsandboxed",
    "probe_sandbox",
    "provider_profile_limitations",
    "provider_profile_enabled",
    "provider_profile_gaps",
    "provider_profile_status",
    "reset_probe_cache",
    "sandbox_limitations",
    "sandbox_status",
    "sandbox_status_nowait",
    "spawn_worker",
    "start_probe_in_background",
]


class SandboxApplyError(RuntimeError):
    """The probe said the sandbox works, yet the ruleset failed to apply.

    The worker did not run. Callers treat this as a failed start, never as a
    reason to retry without the sandbox. Raised by
    ``LauncherProcess.raise_for_sandbox_failure``.
    """


@dataclass(frozen=True)
class LauncherFailure:
    """Why the launcher could not start a worker, as the launcher itself says.

    ``kind`` is ``"sandbox"`` (ruleset or limits could not be applied),
    ``"spec"`` (bad spec or working directory) or ``"exec"`` (the interpreter
    could not be executed).
    """

    kind: str
    message: str


# Landlock ABI 3 is the first that can restrict truncate(2). Below it a
# same-user worker could empty any file the user owns, wherever it lives, so
# the filesystem is not enforced there.
MIN_ENFORCED_ABI = 3

# The subtitle provider profile is the one user of Landlock below ABI 3. What it needs is that a
# worker cannot read the configuration or the database and cannot write outside its own folders,
# and ABI 1 already does both. What ABI 1 and 2 leave open (truncate(2), and on ABI 1 moving a
# file between folders) is named in the status notice. Every other worker keeps ``MIN_ENFORCED_ABI``.
PROVIDER_MIN_ABI = 1

# Longest the launcher needs after the worker is gone or the lifeline closed:
# one poll tick, two seconds of kill rounds and five of reaping. The host waits
# this long (on top of the launcher's term_grace) before it kills the group.
LAUNCHER_CLEANUP_SECONDS = 10.0

# What the host keeps of a launcher's status pipe, and the most it reads in one
# call. The launcher writes a few short lines; anything past this is not the
# launcher (the pipe is closed to the worker, but the host does not rely on
# that for its own bounds) and makes the whole stream untrusted.
STATUS_LIMIT = 16 * 1024
STATUS_READ_PER_CALL = 64 * 1024

# Names this host process as the owner of the workers it starts through the launcher: each
# worker, and what it starts, carries it in its environment, so a later run can tell a leftover
# of ours from a stranger's (see ``sweep``). The launcher itself is non-dumpable, so its own
# environment cannot be read and does not carry it. Until ``sweep.configure_owner`` sets it, nothing
# is marked.
OWNER_VARIABLE = "BAZARR_SANDBOX_OWNER"
_owner_marker: "str | None" = None


def set_owner_marker(value: "str | None") -> None:
    global _owner_marker
    _owner_marker = value


def owner_marker() -> "str | None":
    return _owner_marker


# Flipped when the worker spawner starts plugin workers through this launcher.
# Until then the probe describes what the host could do, not what workers get.
WORKERS_SANDBOXED = False


@dataclass(frozen=True)
class SandboxProbe:
    """What this host can enforce, measured and not assumed."""

    abi: int
    # A child applied a test ruleset and a metadata filter and could not read a
    # canary, write or truncate outside its allowed directory, or change the
    # mode, owner, times or extended attributes of any file.
    verified: bool
    network_verified: bool = False
    signal_scope_verified: bool = False
    # The canary held no effective, permitted or ambient capability.
    capabilities_dropped: bool = False
    reason: str = ""
    # A canary applied the ruleset this kernel has and could neither read a file outside its
    # tree nor write there. True on ABI 3 and above whenever ``verified`` is, and also on ABI 1
    # and 2, where ``verified`` stays False because truncate(2) is not covered.
    reads_verified: bool = False
    # The kernel genuinely has no Landlock to give (no syscall, disabled at boot, blocked by a seccomp
    # profile). False for a check that failed or could not finish: that is not the same as absent, and
    # the provider profile refuses to start workers on a host that may enforce but could not confirm it.
    absent: bool = False

    @property
    def enforced(self) -> bool:
        return self.abi >= MIN_ENFORCED_ABI and self.verified

    def enforces(self, minimum_abi: int = MIN_ENFORCED_ABI) -> bool:
        """Whether a ruleset can be mandatory for a worker that accepts ``minimum_abi`` and above."""
        if minimum_abi >= MIN_ENFORCED_ABI:
            return self.enforced
        return self.abi >= max(1, minimum_abi) and (self.reads_verified or self.enforced)

    @property
    def network_enforced(self) -> bool:
        return self.enforced and self.abi >= 4 and self.network_verified

    @property
    def signal_scope_enforced(self) -> bool:
        return self.enforced and self.abi >= 6 and self.signal_scope_verified


# Largest /dev/shm the pool is willing to treat as a host-bounded pool it can guard.
SHM_GUARD_MAX_BYTES = 1024 * 1024 * 1024


def shm_state() -> "tuple[int, int, bool] | None":
    """``(total bytes, free bytes, separate)`` for ``/dev/shm``, or None where there is none.

    ``separate`` is whether it is a filesystem of its own (a tmpfs), not a directory of
    the root filesystem.
    """
    try:
        info = os.statvfs("/dev/shm")
        separate = os.stat("/dev/shm").st_dev != os.stat("/").st_dev
    except (OSError, AttributeError):  # AttributeError: no os.statvfs (Windows)
        return None
    return info.f_blocks * info.f_frsize, info.f_bavail * info.f_frsize, separate


def shm_guardable(state: "tuple[int, int, bool] | None") -> bool:
    """Whether ``/dev/shm`` is small and separate enough for its total use to be bounded outside any worker."""
    return state is not None and state[2] and 0 < state[0] <= SHM_GUARD_MAX_BYTES


def _shm_note() -> str:
    if shm_guardable(shm_state()):
        return ""
    return " Shared memory under /dev/shm is not limited per plugin on this host."


def sandbox_limitations(probe: SandboxProbe) -> list[str]:
    """Plain statements of what the sandbox does not cover on this host."""
    if not landlock.is_supported_platform():
        return [
            "Workers run with a clean environment, but this platform applies no resource limits "
            "and nothing confines their files, network or signals. Detached processes cannot be "
            "tracked on this platform: only a worker's process group is ended, and a worker that "
            "exits by itself (on its shutdown request, say) leaves the processes it started running "
            "where this host cannot see its exit without collecting it. A plugin may not "
            "keep deleted files mapped inside its folders." + _shm_note()
        ]
    if not probe.enforced:
        return [
            "Workers run with resource limits and a clean environment, but nothing confines "
            "their files, network or signals. A plugin may not keep deleted files mapped inside "
            "its folders." + _shm_note()
        ]
    notes = [
        "Workers cannot change file modes, owners, times or extended attributes anywhere. "
        "Subtitle provider plugins are the exception: they run without that filter."
    ]
    if probe.abi < 4 or not probe.network_enforced:
        notes.append("Network connections are not restricted.")
    if probe.abi < 9:
        notes.append("Connections to pathname UNIX sockets (a Docker socket, for example) are not restricted.")
    if probe.abi < 6:
        notes.append("A worker can signal processes outside its sandbox, its launcher included.")
    notes.append(
        "A worker cannot use /dev/shm, so multiprocessing's shared memory (process pools, "
        "cross-process locks and queues, SharedMemory) is unavailable to it: creating one fails "
        "with an error. Hosts where the sandbox is not enforced are unaffected."
    )
    notes.append("A listening TCP socket that was never bound gets a port without a bind check.")
    notes.append("UDP and DNS are not restricted.")
    notes.append("The number of processes is not limited.")
    notes.append(
        "A plugin may not keep deleted files mapped inside its folders: their size cannot be "
        "measured, so the folder counts as over its limit."
    )
    notes.append(
        "A plugin may not keep a memfd mapped after closing it, or use shared anonymous "
        "mappings (MAP_SHARED|MAP_ANONYMOUS, as Python's mmap.mmap(-1, n) makes): their size "
        "cannot be measured, so the worker counts as over its memory limit."
    )
    return notes


# ---------------------------------------------------------------------------------------------
# The subtitle provider profile: whether it is switched on, and which providers started without it.
#
# Provider workers fail open: when the ruleset cannot be applied the worker still starts, and the
# cause is recorded here so System > Status and the Subtitle Hub can say so. New plugin kinds
# never come through here; they fail closed.

_provider_notice_lock = threading.Lock()
# provider -> (what happened, cause). "unsandboxed": it runs without the ruleset. "blocked": its start was refused.
_provider_notices: dict[str, tuple[str, str]] = {}


def provider_profile_enabled() -> bool:
    """The "Sandbox subtitle provider plugins" setting. On unless the setting says otherwise."""
    try:
        from app.config import settings

        return bool(getattr(settings.general, "provider_hub_sandbox", True))
    except Exception:
        return True


def _note(provider: str, kind: str, cause: str) -> None:
    cause = " ".join(str(cause or "unknown reason").split())[:300]
    with _provider_notice_lock:
        first = _provider_notices.get(provider) != (kind, cause)
        _provider_notices[provider] = (kind, cause)
    if first:
        logging.getLogger(__name__).warning(
            "Subtitle provider %s %s: %s",
            provider,
            "runs without the worker sandbox" if kind == "unsandboxed" else "was not started, the worker sandbox could not be applied",
            cause,
        )


def note_provider_unsandboxed(provider: str, cause: str) -> None:
    """Record that a provider worker started without the ruleset, and why. One entry per provider."""
    _note(provider, "unsandboxed", cause)


def note_provider_blocked(provider: str, cause: str) -> None:
    """Record that a provider worker was not started because its ruleset could not be applied."""
    _note(provider, "blocked", cause)


def clear_provider_notice(provider: str) -> None:
    with _provider_notice_lock:
        _provider_notices.pop(provider, None)


def reset_provider_notices() -> None:
    with _provider_notice_lock:
        _provider_notices.clear()


def provider_profile_gaps(abi: int, *, authentication_off: bool = False) -> list[str]:
    """What the provider ruleset does not cover on a kernel with Landlock ``abi``, in plain words."""
    gaps: list[str] = []
    if abi < 3:
        gaps.append(
            "truncating a file is not controlled, so a plugin can still empty a file the Bazarr+ user "
            "can write, config.yaml, the database and the media library included"
        )
    if abi < 2:
        gaps.append(
            "moving or linking a file from one folder to another is refused, so a plugin that does "
            "that inside its own folders fails"
        )
    if abi < 4 and authentication_off:
        gaps.append(
            "the sandbox cannot stop a plugin calling Bazarr+'s own web API on this machine, and with "
            "authentication off that API hands out the API key, the settings and the backups, so turn "
            "authentication on in Settings > General > Security"
        )
    return gaps


def provider_profile_limitations(abi: int) -> list[str]:
    """What the provider profile leaves open on every kernel with Landlock ``abi``."""
    return [
        "the metadata filter (seccomp) is not applied to provider plugins, so a plugin can change the "
        "mode, owner, times or extended attributes of any file the Bazarr+ user owns, media included, "
        "and can lock every provider out by changing the mode of a folder they run from",
        "the file-size and process-count limits are not applied",
        (
            "TCP connections to Bazarr+'s own port are refused and every other connection is allowed"
            if abi >= 4
            else "network connections are not restricted"
        ),
    ]


def provider_profile_status(
    probe: "SandboxProbe | None", *, enabled: bool | None = None, authentication_off: bool | None = None
) -> dict[str, Any]:
    """What confines subtitle provider workers, for the status page and the Subtitle Hub.

    ``state`` is ``enforced`` (the filesystem ruleset applies in full), ``partial`` (it applies, with
    gaps ``notice`` names: an older Landlock ABI, or no network rule while authentication is off),
    ``unsandboxed`` (switched on but the kernel has no Landlock, or a start could not apply it:
    ``notice`` says why), ``blocked`` (the kernel may enforce, but the host check failed or a start
    could not apply its ruleset, and providers are refused), ``off`` (switched off by the user) or
    ``pending`` (the host check is still running). ``probe`` is None while that check runs.
    ``limitations`` is what the provider profile leaves open on every kernel.
    """
    if enabled is None:
        enabled = provider_profile_enabled()
    if authentication_off is None:
        try:
            from provider_hub.provider_roots import authentication_off as read_authentication_off

            authentication_off = read_authentication_off()
        except Exception:
            authentication_off = False
    with _provider_notice_lock:
        recorded = dict(_provider_notices)
    abi = probe.abi if probe is not None else 0
    result: dict[str, Any] = {
        "enabled": bool(enabled),
        "state": "off",
        "notice": None,
        "unsandboxed": sorted(name for name, (kind, _cause) in recorded.items() if kind == "unsandboxed"),
        "blocked": sorted(name for name, (kind, _cause) in recorded.items() if kind == "blocked"),
        "limitations": provider_profile_limitations(abi),
    }
    if not enabled:
        result["unsandboxed"] = result["blocked"] = []
        return result
    if probe is None:
        result["state"] = "pending"
        return result
    if not probe.enforces(PROVIDER_MIN_ABI):
        reason = probe.reason or "this host has no Landlock"
        if not probe.absent:
            result.update(
                state="blocked",
                notice="Subtitle provider plugins are not started: the host could not confirm that the "
                "sandbox holds, and a provider is never run unconfined on a kernel that may enforce it (%s)."
                % reason.rstrip("."),
            )
            return result
        notice = (
            "Subtitle provider plugins run without the sandbox on this host, so nothing keeps them "
            "from reading the configuration or the database: %s." % reason.rstrip(".")
        )
        if authentication_off:
            notice += " Authentication is off, so a plugin can also read the settings through Bazarr+'s own API: turn authentication on."
        result.update(state="unsandboxed", notice=notice)
        return result
    parts: list[str] = []
    if result["blocked"]:
        causes = "; ".join(sorted({c.rstrip(".") for _n, (k, c) in recorded.items() if k == "blocked"}))
        parts.append(
            "The sandbox could not be applied to %s, so it was not started: %s."
            % (", ".join(result["blocked"]), causes)
        )
    if result["unsandboxed"]:
        causes = "; ".join(sorted({c.rstrip(".") for _n, (k, c) in recorded.items() if k == "unsandboxed"}))
        parts.append(
            "The sandbox could not be applied to %s, which runs without it: %s."
            % (", ".join(result["unsandboxed"]), causes)
        )
    if parts:
        result.update(state="blocked" if result["blocked"] else "unsandboxed", notice=" ".join(parts))
        return result
    gaps = provider_profile_gaps(probe.abi, authentication_off=bool(authentication_off))
    if gaps:
        result.update(
            state="partial",
            notice="Subtitle provider plugins are sandboxed, and cannot read the configuration or the "
            "database. This kernel has Landlock ABI %d, which leaves gaps: %s." % (probe.abi, "; ".join(gaps)),
        )
        return result
    result["state"] = "enforced"
    return result


def sandbox_status(probe: SandboxProbe | None = None) -> dict[str, Any]:
    """Status payload for System > Status and plugin cards.

    ``declared`` means nothing confines the worker. The label is neutral on
    purpose: it must never read as protection. ``enforced`` and the labels
    describe what the host can do; ``workers_sandboxed`` says whether plugin
    workers actually run that way yet.
    """
    probe = probe or probe_sandbox()
    if not probe.enforced:
        label = "Not sandboxed on this host"
    elif WORKERS_SANDBOXED:
        label = "Sandboxed (Landlock ABI %d)" % probe.abi
    else:
        label = "Sandbox available: Landlock ABI %d. Not active for plugin workers yet." % probe.abi
    return {
        "abi": probe.abi,
        "enforced": probe.enforced,
        "filesystem": "enforced" if probe.enforced else "declared",
        "network": "enforced" if probe.network_enforced else "declared",
        "signal_scope": "enforced" if probe.signal_scope_enforced else "declared",
        "capabilities": "dropped" if probe.enforced and probe.capabilities_dropped else "declared",
        "workers_sandboxed": bool(probe.enforced and WORKERS_SANDBOXED),
        "label": label,
        "reason": probe.reason,
        "limitations": sandbox_limitations(probe),
        "pending": False,
        "providers": provider_profile_status(probe),
    }


def sandbox_status_nowait() -> dict[str, Any]:
    """The status payload from the cached probe, or a pending one, never waiting.

    The probe runs a canary through the real launcher and can take about twenty
    seconds, so a request handler must not be the one to run it.
    ``start_probe_in_background`` is called at boot; until it has stored its result this
    returns a payload marked ``pending`` (and starts the probe if boot did
    not). The cache is read without taking the probe lock, so a caller never
    waits behind a running probe.
    """
    probe = _probe_cache
    if probe is None:
        start_probe_in_background()
        return {
            "abi": 0,
            "enforced": False,
            "filesystem": "declared",
            "network": "declared",
            "signal_scope": "declared",
            "capabilities": "declared",
            "workers_sandboxed": False,
            "label": "Checking the worker sandbox",
            "reason": "",
            "limitations": [],
            "pending": True,
            "providers": provider_profile_status(None),
        }
    return sandbox_status(probe)


_CANARY_SCRIPT = """
import ctypes, errno, json, os, socket, sys
base, allowed, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
out = {}
def attempt(name, fn):
    try:
        fn()
        out[name] = "allowed"
    except PermissionError:
        out[name] = "denied"
    except OSError as exc:
        out[name] = "error:%s" % exc.errno
def read_outside():
    with open(os.path.join(base, "canary.txt")) as handle:
        handle.read()
def write_outside():
    with open(os.path.join(base, "escape.txt"), "w"):
        pass
def write_inside():
    with open(os.path.join(allowed, "ok.txt"), "w"):
        pass
def trunc_outside():
    os.truncate(os.path.join(base, "trunc.txt"), 0)
def chmod_outside():
    os.chmod(os.path.join(base, "canary.txt"), 0o644)
def chmod_inside():
    os.chmod(os.path.join(allowed, "ok.txt"), 0o600)
def chown_outside():
    os.chown(os.path.join(base, "canary.txt"), -1, -1)
def utime_outside():
    os.utime(os.path.join(base, "canary.txt"), None)
def setxattr_outside():
    os.setxattr(os.path.join(base, "canary.txt"), "user.bazarr", b"1")
def io_uring_setup():
    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    if libc.syscall(425, 0, 0) < 0 and ctypes.get_errno() == errno.EPERM:
        raise PermissionError(errno.EPERM, "denied")
def connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
    finally:
        s.close()
attempt("read_outside", read_outside)
attempt("write_outside", write_outside)
attempt("write_inside", write_inside)
attempt("truncate_outside", trunc_outside)
attempt("chmod_outside", chmod_outside)
attempt("chmod_inside", chmod_inside)
attempt("chown_outside", chown_outside)
attempt("utime_outside", utime_outside)
attempt("setxattr_outside", setxattr_outside)
attempt("io_uring_setup", io_uring_setup)
attempt("connect", connect)
attempt("signal", lambda: os.kill(os.getppid(), 0))
def capabilities_dropped():
    held = {}
    with open("/proc/self/status") as handle:
        for line in handle:
            name, _, value = line.partition(":")
            if name in ("CapEff", "CapPrm", "CapAmb"):
                held[name] = int(value.strip(), 16)
    return "CapEff" in held and "CapPrm" in held and not any(held.values())
out["capabilities"] = "dropped" if capabilities_dropped() else "kept"
print(json.dumps(out))
"""

# Every canary attempt that must come back denied for the host to count as sandboxed.
_CANARY_REQUIRED_DENIED = (
    "read_outside",
    "write_outside",
    "truncate_outside",
    "chmod_outside",
    "chmod_inside",
    "chown_outside",
    "utime_outside",
    "setxattr_outside",
    "io_uring_setup",
)

# The part of the canary the provider profile relies on.
_CANARY_READS_DENIED = ("read_outside", "write_outside")

_probe_lock = threading.Lock()
_probe_cache: SandboxProbe | None = None


def reset_probe_cache() -> None:
    global _probe_cache
    with _probe_lock:
        _probe_cache = None


def probe_sandbox(refresh: bool = False, *, python: str | None = None) -> SandboxProbe:
    """Measure what this host enforces. Cached after the first call.

    Step one asks the kernel for its Landlock ABI. ENOSYS, EOPNOTSUPP and the
    EPERM of a seccomp profile all mean none. Step two, only on a positive ABI,
    runs a canary through the real launcher: the child must be unable to read a
    file outside its tree and unable to write there. A positive ABI whose
    canary escapes is reported as not enforced.
    """
    global _probe_cache
    with _probe_lock:
        if _probe_cache is not None and not refresh:
            return _probe_cache
        _probe_cache = _run_probe(python or sys.executable)
        return _probe_cache


_probe_thread: threading.Thread | None = None
_probe_thread_lock = threading.Lock()


def _probe_in_background() -> None:
    global _probe_cache
    try:
        probe_sandbox()
    except Exception as exc:
        logging.getLogger(__name__).warning("sandbox probe failed: %s", exc)
        # Store an honest "not enforced" answer so the status stops reading pending.
        with _probe_lock:
            if _probe_cache is None:
                _probe_cache = SandboxProbe(abi=0, verified=False, absent=False, reason="the sandbox probe failed: %s" % exc)


def start_probe_in_background() -> bool:
    """Measure the host's sandbox on a daemon thread, once. Returns whether it started.

    Called at boot, outside any request. Does nothing when a result is already
    cached or a probe thread is still running.
    """
    global _probe_thread
    with _probe_thread_lock:
        if _probe_cache is not None or (_probe_thread is not None and _probe_thread.is_alive()):
            return False
        _probe_thread = threading.Thread(target=_probe_in_background, name="sandbox-probe", daemon=True)
        _probe_thread.start()
        return True


def _run_probe(python: str) -> SandboxProbe:
    abi, reason = landlock.probe_abi()
    if abi < 1:
        return SandboxProbe(abi=0, verified=False, absent="probe failed" not in reason, reason=reason)
    # The metadata filter exists for some architectures only. Where it does not, every kind that needs it
    # is not "enforced", but the filesystem canary still runs: the provider profile does not use the filter.
    filter_available = seccomp.supported()
    if getattr(sys, "frozen", False) and python == sys.executable:
        return SandboxProbe(abi=abi, verified=False, reason="frozen build has no stand-alone interpreter for the launcher")
    base = tempfile.mkdtemp(prefix="bazarr-landlock-")
    listener = None
    try:
        allowed = os.path.join(base, "allowed")
        os.mkdir(allowed)
        Path(base, "canary.txt").write_text("canary", encoding="utf-8")
        Path(base, "trunc.txt").write_text("keep me", encoding="utf-8")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        # The canary's interpreter lives under the system paths, so its policy
        # needs nothing beyond the scratch directory.
        policy = SandboxPolicy.for_interpreter(
            python,
            read_write=[allowed],
            connect_ports=[1] if abi >= 4 else None,
        )
        def attempt(use_filter: bool):
            """Run the canary once. ``(output, None)``, or ``(None, why it did not run)``."""
            spec = RuntimeSpec(
                interpreter=python,
                args=("-I", "-c", _CANARY_SCRIPT, base, allowed, str(port)),
                cwd=allowed,
                env=clean_env(home=allowed, tmpdir=allowed),
                policy=policy,
                term_grace=1.0,
                seccomp=use_filter,
            )
            process = _start(spec, enforce=True, min_abi=1, enforcement="enforced", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                out, err = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.stop(0.5)
                return None, "canary timed out"
            finally:
                process.stop(0.5)
            if process.returncode != 0:
                failure = process.launcher_failure()
                detail = failure.message if failure else (err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"])[0]
                return None, "canary could not run: %s" % detail
            return out, None

        out, why = attempt(filter_available)
        if out is None and filter_available:
            # The filter failing to install must not cost the provider profile its filesystem check:
            # run the canary again without it. Every kind that needs the filter stays not enforced.
            retry, _ = attempt(False)
            if retry is not None:
                out, filter_available = retry, False
        if out is None:
            return SandboxProbe(abi=abi, verified=False, reason=why)
        result = json.loads(out.decode("utf-8").strip().splitlines()[-1])
        reads_verified = result.get("write_inside") == "allowed" and all(
            result.get(k) == "denied" for k in _CANARY_READS_DENIED
        )
        if not filter_available:
            if not reads_verified:
                return SandboxProbe(abi=abi, verified=False, reason="canary escaped the sandbox: %s" % result)
            return SandboxProbe(
                abi=abi, verified=False, reads_verified=True, reason="the metadata filter is not available or could not be applied"
            )
        if abi < MIN_ENFORCED_ABI:
            # Below ABI 3 the full canary cannot pass (truncate is not covered), so the host is not
            # "enforced" for any worker that needs that. The reads and writes it did hold back are
            # what the provider profile relies on.
            if not reads_verified:
                return SandboxProbe(abi=abi, verified=False, reason="canary escaped the sandbox: %s" % result)
            return SandboxProbe(
                abi=abi,
                verified=False,
                reads_verified=True,
                reason="Landlock ABI %d cannot restrict truncate(2); ABI %d is the minimum" % (abi, MIN_ENFORCED_ABI),
            )
        verified = reads_verified and all(result.get(k) == "denied" for k in _CANARY_REQUIRED_DENIED)
        if verified and Path(base, "trunc.txt").read_text(encoding="utf-8") != "keep me":
            verified = False
        if not verified:
            return SandboxProbe(abi=abi, verified=False, reads_verified=reads_verified, reason="canary escaped the sandbox: %s" % result)
        return SandboxProbe(
            abi=abi,
            verified=True,
            reads_verified=True,
            network_verified=abi >= 4 and result.get("connect") == "denied",
            signal_scope_verified=abi >= 6 and result.get("signal") == "denied",
            capabilities_dropped=result.get("capabilities") == "dropped",
            reason="kernel reports ABI %d, canary confined" % abi,
        )
    except (OSError, ValueError, PolicyError, json.JSONDecodeError) as exc:
        return SandboxProbe(abi=abi, verified=False, reason="probe failed: %s" % exc)
    finally:
        if listener is not None:
            listener.close()
        shutil.rmtree(base, ignore_errors=True)


def _close_quietly(fd: "int | None") -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _close_fds(holder: dict) -> None:
    for key in ("lifeline", "status"):
        fd = holder.get(key)
        holder[key] = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# The anchor ignores the polite group signals: a group-wide SIGTERM (or SIGINT, SIGHUP) sent to end
# the worker must not take the anchor with it, or the group number loses its holder before the
# group is ended. Only SIGKILL, or the host letting go of its end of the socket, ends it.
_ANCHOR_SCRIPT = (
    "import os, signal, sys\n"
    "for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):\n"
    "    signal.signal(getattr(signal, name), signal.SIG_IGN)\n"
    "sys.stdin.buffer.read()\n"
    "if os.getpgrp() == os.getpid():\n"
    "    os.killpg(os.getpgrp(), 9)\n"
)


def _finish_anchor(anchor: "subprocess.Popen") -> None:
    """Reap the anchor after its group was killed. Only now may its pid be reused."""
    try:
        if anchor.stdin is not None:
            anchor.stdin.close()
    except OSError:
        pass
    try:
        anchor.kill()
    except OSError:
        pass
    try:
        anchor.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _start_anchor() -> "subprocess.Popen | None":
    """A tiny process that leads a fresh process group and never exits by itself.

    The kernel does not reuse a process id while it is still a process group
    id, and a process this object has not waited for (running or zombie) keeps
    its pid reserved. The anchor leads the worker's group and is reaped only by
    ``stop``, so the group stays addressable however many times the worker was
    reaped or its children came and went. The anchor reads its stdin until end
    of file and then kills the group: when the host dies or drops the handle,
    the whole group goes with it. ``None`` where process groups are not
    available or the anchor cannot start (a frozen build has no stand-alone
    interpreter; Python before 3.11 cannot join a group without ``preexec_fn``).
    """
    if not hasattr(os, "killpg") or sys.version_info < (3, 11) or getattr(sys, "frozen", False):
        return None
    try:
        # The anchor reads a socket, not a pipe, so a worker cannot reopen the
        # host's end through /proc and keep the anchor from ever seeing the host go.
        host_end, anchor_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        anchor = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", _ANCHOR_SCRIPT],
            stdin=anchor_end.fileno(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
            process_group=0,
        )
    except OSError:
        host_end.close()
        return None
    finally:
        anchor_end.close()
    anchor.stdin = os.fdopen(host_end.detach(), "wb", buffering=0)
    return anchor


def _start_plain(command: list, **kwargs: Any) -> "WorkerProcess":
    """Start a worker directly (no launcher), in a group an anchor holds.

    The anchor starts first and the worker joins its group, so the group id is
    the anchor's pid, not the worker's. The worker is then free to be reaped by
    ``poll``, ``wait`` or ``communicate`` without the group becoming
    unaddressable. Without an anchor the worker leads its own session instead.
    """
    anchor = _start_anchor()
    try:
        if anchor is not None:
            process = WorkerProcess(command, process_group=anchor.pid, **kwargs)
        else:
            process = WorkerProcess(command, start_new_session=True, **kwargs)
    except BaseException:
        if anchor is not None:
            _finish_anchor(anchor)
        raise
    process._anchor = anchor
    if anchor is not None:
        process._anchor_mark = _start_mark(anchor.pid)
    return process


def _stat_fields(pid: int) -> "tuple[str, str]":
    """``(state, start time)`` of a process from ``/proc/<pid>/stat``. Raises ``FileNotFoundError`` if it is gone."""
    with open("/proc/%d/stat" % pid, "rb") as handle:
        data = handle.read(4096)
    rest = data.rsplit(b")", 1)[1].split()
    return rest[0].decode("ascii", "replace"), rest[19].decode("ascii", "replace")


def _start_mark(pid: int) -> "str | None":
    """When the process started, as ``/proc`` says: what tells a pid's owner from a stranger that reused it."""
    try:
        return _stat_fields(pid)[1]
    except (OSError, IndexError, ValueError):
        return None


def _has_proc() -> bool:
    return os.path.isdir("/proc/self")


def _waitpid_proof(popen: "subprocess.Popen") -> str:
    """Whether a direct child is ours, where neither ``waitid`` nor ``/proc`` can say. Its ``returncode`` is None.

    ``"live"``: ``waitpid(pid, WNOHANG)`` returned 0, so the pid is our own unreaped child.
    ``"reaped"``: it returned the pid, so this call collected the child; its status is
    recorded as ``Popen`` records it. ``"lost"``: ``ECHILD`` (or any other error), so
    somebody else reaped it and the number may be a stranger's. See the module docstring
    for the window this leaves. Taken under the ``Popen``'s own wait lock, so a concurrent
    ``poll`` cannot collect the same exit.
    """
    lock = getattr(popen, "_waitpid_lock", None)
    with (lock if lock is not None else contextlib.nullcontext()):
        if popen.returncode is not None:
            return "reaped"
        try:
            pid, status = os.waitpid(popen.pid, os.WNOHANG)
        except OSError:
            return "lost"
        if pid == 0:
            return "live"
        popen.returncode = os.waitstatus_to_exitcode(status)
        return "reaped"


def _owned_without_waitid(pid: int, started: "str | None") -> "bool | None":
    """Whether a child that has not been reaped by us is still there, where ``waitid`` cannot say.

    True: its entry in ``/proc`` is there with the start time it had (alive, or a zombie,
    which keeps its pid). False: the entry is gone or belongs to a process that started
    later: somebody else reaped it and the pid may be a stranger's. None: nothing can
    tell (no ``/proc``, where signal 0 proves nothing and ``_waitpid_proof`` decides
    instead; an unreadable entry; or no start time saved at spawn to compare with), so
    ownership is unknown and the caller must not signal.
    """
    if _has_proc():
        try:
            _state, start = _stat_fields(pid)
        except FileNotFoundError:
            return False
        except (OSError, IndexError, ValueError):
            return None
        if started is None:
            # No identity was recorded at spawn: a stranger that reused the pid looks the same.
            return None
        if start != started:
            return False
        return True
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
    # Something answers to the pid, but a zombie and a stranger look the same from here.
    return None


class WorkerProcess(subprocess.Popen):
    """What the worker spawner sees, with or without a launcher.

    ``enforcement`` is ``"enforced"`` or ``"declared"``. A plain worker (a
    platform without the launcher) has no failure record, no lifeline and
    nothing to raise, but answers the same calls.

    One lock (``_own_lock``) serialises everything that decides whether the
    process is still ours and then acts on it: the ``returncode`` check and the
    signal that follows, ``poll``, ``wait`` and the reap they perform. A pid may
    be handed out again the moment it is reaped, so a signal sent after a reap
    that raced the check would reach a stranger. Nothing here sleeps or waits
    for the process while holding the lock for more than a short slice, and
    ``stop()`` is the only long operation: concurrent callers share one run.
    """

    enforcement: str = "declared"
    # The process that keeps the worker's group id reserved; see ``_start_plain``.
    _anchor: "subprocess.Popen | None" = None
    _leader_lost = False
    _anchor_lost = False
    # Set when ``_waitpid_proof`` itself collected the leader (or the anchor) on a host with nothing
    # else to go by: its exit is proven, and that is all this says. It grants no signal, ever: once
    # the group's last member is reaped its number may be anybody's.
    _exit_proven = False
    _anchor_exit_proven = False
    _started_mark: "str | None" = None
    _anchor_mark: "str | None" = None
    # None until a ``stop`` has run. True: this object ended the worker's process
    # group itself. False: it could not, because the leader (and with it the group
    # id) was reaped before the group was ended, so what the worker left behind is
    # unknown. A launcher, which proves its tree some other way, leaves it None.
    _group_cleaned: "bool | None" = None

    @property
    def group_cleaned(self) -> "bool | None":
        return self._group_cleaned

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Before the child exists, so every override below is safe to call from
        # Popen's own code.
        self._own_lock = threading.RLock()
        self._stop_running: "threading.Event | None" = None
        # Sticky: set the moment a probe or a wait finds the leader or the anchor
        # was reaped by somebody else. Its pid may belong to a stranger from then
        # on, so nothing is ever signalled by it again.
        self._leader_lost = False
        self._anchor_lost = False
        self._exit_proven = False
        self._anchor_exit_proven = False
        super().__init__(*args, **kwargs)
        self._started_mark = _start_mark(self.pid)

    def launcher_failure(self) -> "LauncherFailure | None":
        return None

    def raise_for_sandbox_failure(self) -> None:
        return None

    def close_lifeline(self) -> None:
        return None

    def __del__(self, *args, **kwargs):
        # Popen.__del__ parks a still-running child on a module list, which
        # keeps this object (and the anchor it holds) alive. Close the anchor's
        # pipe first: it then kills the worker's group and exits.
        anchor = getattr(self, "_anchor", None)
        if anchor is not None and anchor.stdin is not None:
            try:
                anchor.stdin.close()
            except OSError:
                pass
        super().__del__(*args, **kwargs)

    # -- everything below goes through ``_own_lock`` ------------------------------

    def poll(self):
        with self._own_lock:
            return super().poll()

    def send_signal(self, sig):
        # Popen polls first and sends nothing to a reaped child; under the lock
        # no other reap can slip in between that check and the signal.
        with self._own_lock:
            super().send_signal(sig)

    def wait(self, timeout=None):
        """``Popen.wait`` in short slices, so a signaller is never kept out for long."""
        end = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while True:
            with self._own_lock:
                if self.returncode is not None:
                    return self.returncode
                step = 0.05 if end is None else max(0.0, min(0.05, end - time.monotonic()))
                try:
                    return super().wait(timeout=step)
                except subprocess.TimeoutExpired:
                    pass
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.002)

    def _still_ours(self) -> bool:
        """Whether the pid is still this child's. Call with ``_own_lock`` held.

        False once it was reaped, by this object or by anything else (``waitid``
        finds no such child). A zombie is still ours: it keeps its pid.
        """
        if self._leader_lost:
            return False
        if self.returncode is not None:
            return False
        waitid = getattr(os, "waitid", None)
        flag = getattr(os, "WNOWAIT", None)
        if waitid is None or flag is None:
            # No non-reaping wait: ``returncode`` stays None after somebody else's reap, so
            # ask ``/proc``, or where there is none, ``waitpid``; where that cannot tell
            # either, ownership is unknown and refused.
            return self._leader_owned_without_waitid()
        try:
            waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | flag)
        except ChildProcessError:
            self._leader_lost = True
            return False
        except OSError:
            # Any other answer (EPERM, EINVAL, ENOSYS...) says nothing about the pid: the same
            # identity check as without waitid decides, and unknown is refused.
            return self._leader_owned_without_waitid()
        return self.returncode is None

    def _leader_owned_without_waitid(self) -> bool:
        """``_still_ours`` where ``waitid`` cannot say. Call with ``_own_lock`` held.

        Where ``waitpid`` is all there is: 0 means ours and alive (True, and the caller signals
        now). If it collects the child in this very call the answer is False, with ``returncode``
        set and the exit proven (``_exit_proven``), not lost: nothing is signalled after our own
        reap, since the group number may be free the moment its last member is reaped.
        """
        if _has_proc():
            if not _owned_without_waitid(self.pid, self._started_mark):
                self._leader_lost = True
                return False
            return self.returncode is None
        proof = _waitpid_proof(self)
        if proof == "lost":
            self._leader_lost = True
            return False
        if proof == "reaped":
            self._exit_proven = True
            return False
        return True

    def _anchor_owned_without_waitid(self, anchor) -> bool:
        """``_anchor_ours`` where ``waitid`` cannot say. Call with ``_own_lock`` held."""
        if _has_proc():
            if not _owned_without_waitid(anchor.pid, self._anchor_mark):
                self._anchor_lost = True
                return False
            return True
        proof = _waitpid_proof(anchor)
        if proof == "lost":
            self._anchor_lost = True
            return False
        if proof == "reaped":
            # This call collected the anchor: it exited, and nothing is signalled on its number now.
            self._anchor_exit_proven = True
            return False
        return True

    def _anchor_ours(self) -> bool:
        """Whether the group anchor is still unreaped. Call with ``_own_lock`` held.

        The same rule as for the leader: a non-reaping probe immediately before a
        signal, and a sticky flag once anything found it gone. ``returncode`` alone
        is not enough, since a direct ``waitpid`` elsewhere never updates it.
        """
        anchor = self._anchor
        if anchor is None or self._anchor_lost:
            return False
        if anchor.returncode is not None:
            if not self._anchor_exit_proven:
                self._anchor_lost = True
            return False
        waitid = getattr(os, "waitid", None)
        flag = getattr(os, "WNOWAIT", None)
        if waitid is None or flag is None:
            return self._anchor_owned_without_waitid(anchor)
        try:
            waitid(os.P_PID, anchor.pid, os.WEXITED | os.WNOHANG | flag)
        except ChildProcessError:
            self._anchor_lost = True
            return False
        except OSError:
            if not self._anchor_owned_without_waitid(anchor):
                return False
        return True

    def probe_exit(self) -> "str | None":
        """What the child is doing, without reaping it, judged under this object's lock.

        ``"running"``; ``"exited"`` (a zombie, or reaped by this object itself); ``"gone"``
        (reaped by somebody else, so the pid is no longer ours). ``None`` when it cannot
        be told at all: no ``waitid`` that can leave an exit unconsumed and no ``/proc``.

        A caller that reads ``returncode`` and then asks ``waitid`` (or ``/proc``) on its
        own can be overtaken by this object's own reap in between and take it for somebody
        else's. Here the check and the question are one step against every reap this object
        does, ``/proc`` fallback included, so a session asks only this and never repeats the
        question outside the lock.

        On a host with neither ``waitid`` nor ``/proc`` a probe can only see an exit by collecting
        the child. That proves the exit and grants no signal, now or later (see the module
        docstring).
        """
        with self._own_lock:
            if self.returncode is not None:
                return "exited"
            if self._leader_lost:
                return "gone"
            if os.name != "posix":
                return None
            waitid = getattr(os, "waitid", None)
            flag = getattr(os, "WNOWAIT", None)
            if waitid is not None and flag is not None:
                try:
                    found = waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | flag)
                except ChildProcessError:
                    self._leader_lost = True
                    return "gone"
                except OSError:
                    pass  # says nothing about the pid: ask /proc below
                else:
                    return "running" if found is None else "exited"
            return self._proc_answer()

    def _proc_answer(self) -> "str | None":
        """``probe_exit`` from ``/proc`` and the start time. Call with ``_own_lock`` held."""
        if not _has_proc():
            if self._anchor is not None:
                # The anchor keeps the group's number whatever happens to the leader, so the leader
                # can be reaped to see its exit: nothing is lost by it, and nothing else can tell.
                return "exited" if super().poll() is not None else "running"
            # Signal 0 would answer for a zombie and for a stranger alike: only a parent's
            # ``waitpid`` proves the child is ours (see the module docstring).
            proof = _waitpid_proof(self)
            if proof == "lost":
                self._leader_lost = True
                return "gone"
            if proof == "reaped":
                # The exit is proven; nothing is signalled after our own reap.
                self._exit_proven = True
                return "exited"
            return "running"
        try:
            state, start = _stat_fields(self.pid)
        except FileNotFoundError:
            self._leader_lost = True
            return "gone"
        except (OSError, IndexError, ValueError):
            return None
        if self._started_mark is None:
            return None  # no identity saved at spawn: whatever answers to the pid may be a stranger
        if start != self._started_mark:
            self._leader_lost = True
            return "gone"
        return "exited" if state in ("Z", "X") else "running"

    def signal_group(self, sig: int) -> "bool | None":
        """Signal the worker's process group while it is certainly ours.

        True: signalled. None: not ours any more (reaped), nothing was sent.
        False: the signal could not be sent. With an anchor the group is the
        anchor's, which stays unreaped until ``stop`` has used it; otherwise the
        worker leads its own group and its pid names it only while unreaped.
        """
        killpg = getattr(os, "killpg", None)
        if killpg is None:
            return False
        with self._own_lock:
            anchor = self._anchor
            try:
                if anchor is not None:
                    if not self._anchor_ours():
                        # The group id was the anchor's; nobody holds it any more.
                        return None
                    killpg(anchor.pid, sig)
                    return True
                if not self._still_ours():
                    return None
                killpg(self.pid, sig)
                return True
            except OSError:
                return False

    def stop(self, grace: float = 3.0) -> None:
        """End the worker and everything it started. Concurrent callers share one run.

        A caller that arrives while another stop is running waits for it and
        returns; a call after it finished runs again, harmlessly (every step
        checks, under the lock, that the process is still ours).
        """
        with self._own_lock:
            running = self._stop_running
            leader = running is None
            if leader:
                running = self._stop_running = threading.Event()
        if not leader:
            running.wait()
            return
        try:
            self._do_stop(grace)
        finally:
            with self._own_lock:
                self._stop_running = None
            running.set()

    def _terminate_and_wait(self, grace: float) -> bool:
        """SIGTERM the worker and wait. True when it was still running after ``grace``."""
        if self.poll() is None:
            try:
                self.terminate()
            except OSError:
                pass
            try:
                self.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return True
        return False

    def _wait_for_exit_unreaped(self, timeout: float) -> bool:
        """Wait until the worker exits without reaping it. False when ``timeout`` passed first.

        A zombie keeps its pid, so the pid still names the worker's process
        group afterwards. Raises ``ChildProcessError`` when the worker is gone
        (someone else reaped it) and the pid can no longer be trusted.
        """
        end = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                found = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                raise
            except OSError:
                # waitid fails here for some other reason (EPERM, ENOSYS...): it cannot be
                # asked again. Wait out the rest of the grace, seeing the exit through /proc
                # where it can be seen; the caller then checks ownership again and goes on.
                while time.monotonic() < end and not self._zombie():
                    time.sleep(0.01)
                return self._zombie()
            if found is not None:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.01)

    def _reap_quietly(self) -> None:
        try:
            self.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _do_stop(self, grace: float) -> None:
        cleaned = self._end_tree(grace)
        if cleaned:
            self._group_cleaned = True
        elif self._group_cleaned is None:
            self._group_cleaned = False

    def _end_tree(self, grace: float) -> bool:
        """Terminate the worker, then kill its whole process group. True if the group was ended.

        The group is cleaned up whether or not the worker is still running, and
        whether or not something already reaped it (``poll``, ``wait`` and
        ``communicate`` all do): a worker that exited on its own can leave
        children behind in its group. When the worker was started with a group
        anchor (see ``_start_plain``) the group id belongs to the anchor, which
        this object has not reaped, so the kernel cannot hand that pid out
        again and the kill always reaches the right group. Without an anchor
        the group id is the worker's own pid, which is safe to signal only
        while the worker is not reaped: the leader is kept unreaped until the
        group has been signalled, the check and the signal happen under the
        lock, and nothing is sent when something else reaped it first. A leader
        that was reaped before the group was ended leaves the answer False: the
        group number may belong to someone else, so nothing can be said of what
        the worker left behind.
        """
        killpg = getattr(os, "killpg", None)
        if killpg is None:
            # No process groups on this platform: the reap is all there is to end.
            if self._terminate_and_wait(grace):
                try:
                    self.kill()
                except OSError:
                    pass
                self._reap_quietly()
            return True
        anchor = self._anchor
        if anchor is not None:
            timed_out = self._terminate_and_wait(grace)
            cleaned = False
            # Kill the group, reap the anchor and let go of it in one step: until
            # the anchor is reaped its pid cannot be reused, and from then on
            # ``signal_group`` must not find it, so nobody can slip in between.
            with self._own_lock:
                if self._anchor_ours():
                    try:
                        killpg(anchor.pid, signal.SIGKILL)
                        cleaned = True
                    except ProcessLookupError:
                        cleaned = True  # nobody left in it
                    except OSError:
                        pass
                _finish_anchor(anchor)
                self._anchor = None
            if not cleaned and self._anchor_exit_proven:
                # Our own waitpid proved the anchor exited: nothing is signalled on its number, the
                # group is taken as gone, and the leader (still ours and unreaped) is ended directly.
                cleaned = True
                if timed_out:
                    try:
                        self.kill()
                    except OSError:
                        pass
            if timed_out:
                self._reap_quietly()
            return cleaned
        if self.returncode is not None:
            # Reaped by an earlier poll, wait or communicate before the group was
            # ended: the pid may be someone else's now, so nothing is signalled. A host with
            # nothing else to go by proved the exit by its own waitpid: the group is taken as
            # gone (what the leader left behind is not known, and the pool counts the teardown
            # as unverified); anywhere else it is unknown.
            return self._exit_proven
        cleaned = False
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            try:
                # Not self.terminate(): it polls first and would reap an
                # exited worker. The worker is unreaped here (zombies take
                # signals as a no-op), so its pid is still its own.
                with self._own_lock:
                    if self._still_ours():
                        # The first polite signal goes to the whole group, while the leader is
                        # still ours: where our own probe is what reaps a worker that leaves at
                        # once, nothing signals its group afterwards.
                        self._term_group(killpg)
                if self.returncode is None:
                    self._wait_for_exit_unreaped(grace)
                with self._own_lock:
                    if self._still_ours():
                        killpg(self.pid, signal.SIGKILL)
                        cleaned = True
            except ProcessLookupError:
                cleaned = True
            except (ChildProcessError, OSError):
                pass
            self._reap_quietly()
            # ``waitid`` failed here and our own waitpid collected the leader: its exit is proven and
            # nothing is signalled after it (see the module docstring).
            return cleaned or (self.returncode is not None and self._exit_proven)
        # No waitid: the exit cannot be seen without consuming it, so the worker is not
        # waited for. It gets its grace to leave (seen as a zombie where /proc says so;
        # otherwise the whole grace passes), and the group is signalled while the leader
        # is still there, alive or a zombie. Only then is it reaped.
        with self._own_lock:
            if self._still_ours():
                self._term_group(killpg)
        end = time.monotonic() + max(grace, 0.0)
        while self.returncode is None and time.monotonic() < end and not self._zombie():
            time.sleep(0.01)
        with self._own_lock:
            if self._still_ours():
                try:
                    killpg(self.pid, signal.SIGKILL)
                    cleaned = True
                except ProcessLookupError:
                    cleaned = True
                except OSError:
                    pass
        self._reap_quietly()
        # Our own waitpid collected the leader (here or in a probe): its exit is proven and nothing
        # is signalled after it (see the module docstring).
        return cleaned or (self.returncode is not None and self._exit_proven)

    def _term_group(self, killpg) -> None:
        """SIGTERM the leader's whole group. Call with ``_own_lock`` held and the leader proven ours."""
        try:
            killpg(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def _zombie(self) -> bool:
        """Whether ``/proc`` says the child has exited and is waiting to be reaped."""
        try:
            with open("/proc/%d/stat" % self.pid, "rb") as handle:
                return handle.read().rsplit(b")", 1)[1].split()[0] in (b"Z", b"X")
        except (OSError, IndexError):
            return False


class LauncherProcess(WorkerProcess):
    """The launcher process. Its subtree is the worker.

    Holds the write end of the lifeline pipe: when this object is closed or
    garbage collected (a finalizer closes it), when the interpreter exits, or
    when the host dies, the launcher sees end-of-file and kills the whole
    worker tree.
    """

    def __init__(self, *args, lifeline: int | None = None, status: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Set only after the child started, so a failed start leaves both fds
        # to the caller. The finalizer holds the dict, not self.
        self._fds = {"lifeline": lifeline, "status": status}
        self._failure: LauncherFailure | None = None
        self._status_text = b""
        self._status_untrusted = False
        # The launcher's own grace for the worker; set by ``_start``.
        self.term_grace = 3.0
        weakref.finalize(self, _close_fds, self._fds)

    def __del__(self, *args, **kwargs):
        # Popen.__del__ parks a still-running child on a module list, which
        # resurrects this object, so a weakref finalizer alone would never run.
        # Close the lifeline first: the launcher then ends the worker tree.
        fds = getattr(self, "_fds", None)
        if fds is not None:
            _close_fds(fds)
        super().__del__(*args, **kwargs)

    def close_lifeline(self) -> None:
        with self._own_lock:
            fd, self._fds["lifeline"] = self._fds.get("lifeline"), None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _read_status(self) -> None:
        fd = self._fds.get("status")
        if fd is None:
            return
        with self._own_lock:
            left = STATUS_READ_PER_CALL
            try:
                while left > 0:
                    chunk = os.read(fd, min(4096, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    if self._status_untrusted or len(self._status_text) + len(chunk) > STATUS_LIMIT:
                        # Overflow: keep what came before, drop the rest, and
                        # believe nothing in this stream about the cleanup.
                        self._status_untrusted = True
                        continue
                    self._status_text += chunk
            except (BlockingIOError, OSError):
                pass

    def launcher_failure(self) -> LauncherFailure | None:
        """The launcher's own report that the worker could not be started.

        Authoritative: it comes over a pipe only the launcher writes to. A
        worker that exits 78, or prints a line that looks like a launcher
        message, does not produce one. Exact once the process has exited.
        """
        if self._failure is None:
            self._read_status()
            for line in self._status_text.decode("utf-8", "replace").splitlines():
                try:
                    record = json.loads(line)
                    self._failure = LauncherFailure(str(record["kind"]), str(record.get("message", "")))
                    break
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
        return self._failure

    def launcher_cleanup(self) -> str | None:
        """How the launcher says its tree cleanup ended: ``"complete"``, ``"survivors"`` or None.

        None means it wrote no cleanup record (it was killed, or it is still
        running) or its status stream overflowed, which nobody may read as a
        clean tree. The last record wins.
        Exact once the process has exited.
        """
        self._read_status()
        if self._status_untrusted:
            return None
        state = None
        for line in self._status_text.decode("utf-8", "replace").splitlines():
            try:
                value = json.loads(line)["cleanup"]
            except (ValueError, KeyError, TypeError):
                continue
            if value in ("complete", "survivors"):
                state = value
        return state

    def raise_for_sandbox_failure(self) -> None:
        """Raise ``SandboxApplyError`` when the ruleset or limits could not be applied."""
        failure = self.launcher_failure()
        if failure is not None and failure.kind == "sandbox":
            raise SandboxApplyError(failure.message)

    def _do_stop(self, grace: float) -> None:
        """End the worker tree and wait for the launcher to report it gone.

        SIGTERM goes to the launcher, which passes it to the worker and gives it
        its own ``term_grace``. When the launcher is still there after ``grace``
        the lifeline is closed, which makes it kill the whole tree at once,
        ``setsid`` and double-forked descendants included. The launcher is then
        given its configured ``term_grace`` plus the time its tree cleanup can
        take before anything is escalated: a launcher killed first cannot reap
        a descendant that left its group, so the group kill is a last resort
        for a launcher that is stuck, not a timer that races its cleanup.
        """
        if self.poll() is None:
            try:
                # send_signal polls first and sends nothing to a reaped
                # launcher, which os.kill(self.pid) would (pid reuse).
                self.send_signal(signal.SIGTERM)
                # A launcher stopped with SIGSTOP would never see the signal.
                self.send_signal(signal.SIGCONT)
            except OSError:
                pass
            try:
                super().wait(timeout=max(grace, 0.0))
            except subprocess.TimeoutExpired:
                self.close_lifeline()
                try:
                    super().wait(timeout=self.term_grace + LAUNCHER_CLEANUP_SECONDS)
                except subprocess.TimeoutExpired:
                    # Still unreaped (the wait timed out), so the pid is the
                    # group id of the launcher's own session; checked and sent
                    # under the lock that every reap takes.
                    with self._own_lock:
                        if self._still_ours():
                            try:
                                getattr(os, "killpg")(self.pid, signal.SIGKILL)
                            except (AttributeError, OSError):
                                pass
                    try:
                        super().wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        self.close_lifeline()

    def wait(self, timeout=None):
        result = super().wait(timeout=timeout)
        self.close_lifeline()
        return result


def _socket_pair() -> "tuple[int, int]":
    """Two connected stream sockets as plain descriptors (close-on-exec until passed on)."""
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return first.detach(), second.detach()


def _spec_fd(data: bytes) -> int:
    """The launch spec in an anonymous file: no pipe quota to block on."""
    if hasattr(os, "memfd_create"):
        fd = os.memfd_create("bazarr-launch-spec")
    else:  # pragma: no cover - memfd exists on every Linux Python we run
        handle = tempfile.TemporaryFile()
        fd = os.dup(handle.fileno())
        handle.close()
    try:
        os.write(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _start(
    spec: RuntimeSpec,
    *,
    enforce: bool,
    min_abi: int,
    enforcement: str,
    launcher_python: str | None = None,
    **popen_kwargs,
) -> LauncherProcess:
    marker = _owner_marker
    if marker is not None:
        spec = dataclasses.replace(spec, env={**spec.env, OWNER_VARIABLE: marker})
    data = spec.to_launch_json(enforce=enforce, min_abi=min_abi)
    python = launcher_python or sys.executable
    spec_fd = life_r = life_w = status_r = status_w = None
    started = False
    try:
        # Every descriptor is closed on any failure, including a later
        # socketpair running out of descriptors.
        spec_fd = _spec_fd(data)
        # Sockets, not pipes: a same-user worker can reopen the host's end of a
        # pipe through /proc/<host>/fd/<n> and write into it (or hold the
        # lifeline open), but opening a socket that way fails.
        life_w, life_r = _socket_pair()
        status_r, status_w = _socket_pair()
        os.set_blocking(status_r, False)
        # Plain Popen, no preexec_fn. The launcher gets an empty environment:
        # nothing from the host's reaches it, and so nothing reaches the worker
        # except what the spec lists (the owner marker among it).
        process = LauncherProcess(
            [python, "-I", "-S", "-B", str(LAUNCHER_PATH), str(spec_fd), str(life_r), str(status_w)],
            lifeline=life_w,
            status=status_r,
            env={},
            close_fds=True,
            pass_fds=(spec_fd, life_r, status_w),
            start_new_session=True,
            **popen_kwargs,
        )
        started = True
    finally:
        # The child holds its ends now; the process object owns the other two.
        for fd in (spec_fd, life_r, status_w):
            _close_quietly(fd)
        if not started:
            for fd in (life_w, status_r):
                _close_quietly(fd)
    process.enforcement = enforcement
    process.term_grace = float(spec.term_grace)
    return process


def spawn_worker(
    spec: RuntimeSpec,
    *,
    probe: SandboxProbe | None = None,
    require_sandbox: bool = False,
    launcher_python: str | None = None,
    minimum_abi: int = MIN_ENFORCED_ABI,
    stdin: Any = subprocess.PIPE,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    **popen_kwargs: Any,
) -> WorkerProcess:
    """Start one worker through the launcher and return its process handle.

    This is what the worker spawner calls. The decision follows the probe:

    * Probe positive: the ruleset is mandatory. If it cannot be applied the
      launcher exits with code 78 and the worker never runs (fail closed;
      ``process.launcher_failure()`` carries the reason and
      ``process.raise_for_sandbox_failure()`` raises ``SandboxApplyError``).
      There is no silent downgrade.
    * Probe negative (no Landlock, an ABI below 3, an unsupported
      architecture, or a canary that escaped): the worker runs with limits and
      a clean environment but without a ruleset, ``process.enforcement ==
      "declared"``. With ``require_sandbox=True`` this raises
      ``SandboxRefused`` instead.
    * A policy whose ``abi_cap`` is below 3 gets the weaker ruleset it asks
      for and is reported as declared: truncate(2) is not covered. A caller
      that accepts that (the subtitle provider profile passes
      ``minimum_abi=PROVIDER_MIN_ABI``) gets it as ``enforced``.

    "Enforced" means all of: the ruleset (filesystem, plus network and signal
    scope where the ABI has them), every Linux capability dropped, the seccomp
    filter that denies chmod, chown, utime and xattr calls, and the limits. The
    spec's ``seccomp`` flag and limits are how the subtitle provider profile
    leaves the filter and two limits out.

    ``popen_kwargs`` (``text``, ``bufsize`` and so on) go to ``subprocess.Popen``;
    ``preexec_fn`` is refused. On a platform other than Linux the worker starts
    directly with the clean environment and ``enforcement == "declared"``.
    """
    if "preexec_fn" in popen_kwargs:
        raise TypeError("preexec_fn is not allowed: the host never runs code between fork and exec")
    if not landlock.is_supported_platform():
        if require_sandbox:
            raise SandboxRefused("this platform cannot sandbox workers")
        return _start_plain(
            [spec.interpreter, *spec.args],
            cwd=spec.cwd,
            env=dict(spec.env),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **popen_kwargs,
        )
    probe = probe or probe_sandbox()
    cap = spec.policy.abi_cap
    effective = min(probe.abi, cap) if cap else probe.abi
    # Filesystem integrity needs truncate(2) covered, which is ABI 3. A policy
    # capped below that still gets the weaker ruleset, but it is not "enforced".
    # ``minimum_abi`` stays at ``MIN_ENFORCED_ABI`` for every kind but the subtitle provider profile.
    usable = probe.enforces(minimum_abi)
    sandboxed = usable and effective >= max(1, minimum_abi)
    if not sandboxed and require_sandbox:
        raise SandboxRefused("this host cannot sandbox workers: %s" % (probe.reason or "no Landlock"))
    return _start(
        spec,
        enforce=usable,
        min_abi=probe.abi,
        enforcement="enforced" if sandboxed else "declared",
        launcher_python=launcher_python,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        **popen_kwargs,
    )

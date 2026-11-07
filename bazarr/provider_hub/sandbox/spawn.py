# coding=utf-8
"""Host side of the sandbox: probe the kernel, start workers through the launcher.

Fork safety: Bazarr+ is heavily threaded, so the host never forks itself. No
``preexec_fn`` and no bare ``os.fork`` anywhere in this module. A worker starts
as ``subprocess.Popen([python, "-I", "-S", "-B", launcher.py, ...])``: a fresh
single-threaded interpreter that applies the limits and the ruleset and then
``execve``s the worker.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import landlock
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
    "LauncherProcess",
    "SandboxApplyError",
    "SandboxProbe",
    "probe_sandbox",
    "reset_probe_cache",
    "sandbox_status",
    "spawn_worker",
    "LauncherFailure",
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


@dataclass(frozen=True)
class SandboxProbe:
    """What this host can enforce, measured and not assumed."""

    abi: int
    # A child applied a test ruleset and could not read a canary or write
    # outside its allowed directory.
    verified: bool
    network_verified: bool = False
    signal_scope_verified: bool = False
    reason: str = ""

    @property
    def enforced(self) -> bool:
        return self.abi >= 1 and self.verified

    @property
    def network_enforced(self) -> bool:
        return self.enforced and self.abi >= 4 and self.network_verified

    @property
    def signal_scope_enforced(self) -> bool:
        return self.enforced and self.abi >= 6 and self.signal_scope_verified


def sandbox_limitations(probe: SandboxProbe) -> list[str]:
    """Plain statements of what the sandbox does not cover on this host."""
    if not probe.enforced:
        return [
            "Workers run with resource limits and a clean environment, but nothing confines "
            "their files, network or signals."
        ]
    notes = []
    if probe.abi < 4 or not probe.network_enforced:
        notes.append("Network connections are not restricted.")
    if probe.abi < 9:
        notes.append("Connections to pathname UNIX sockets (a Docker socket, for example) are not restricted.")
    if probe.abi < 6:
        notes.append("A worker can signal processes outside its sandbox, its launcher included.")
    notes.append("A listening TCP socket that was never bound gets a port without a bind check.")
    notes.append("UDP and DNS are not restricted.")
    notes.append("The number of processes is not limited.")
    return notes


def sandbox_status(probe: SandboxProbe | None = None) -> dict[str, Any]:
    """Status payload for System > Status and plugin cards.

    ``declared`` means nothing confines the worker. The label is neutral on
    purpose: it must never read as protection.
    """
    probe = probe or probe_sandbox()
    return {
        "abi": probe.abi,
        "enforced": probe.enforced,
        "filesystem": "enforced" if probe.enforced else "declared",
        "network": "enforced" if probe.network_enforced else "declared",
        "signal_scope": "enforced" if probe.signal_scope_enforced else "declared",
        "label": "Sandboxed" if probe.enforced else "Not sandboxed on this host",
        "reason": probe.reason,
        "limitations": sandbox_limitations(probe),
    }


_CANARY_SCRIPT = """
import json, os, socket, sys
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
attempt("connect", connect)
attempt("signal", lambda: os.kill(os.getppid(), 0))
print(json.dumps(out))
"""

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


def _run_probe(python: str) -> SandboxProbe:
    abi, reason = landlock.probe_abi()
    if abi < 1:
        return SandboxProbe(abi=0, verified=False, reason=reason)
    if getattr(sys, "frozen", False) and python == sys.executable:
        return SandboxProbe(abi=abi, verified=False, reason="frozen build has no stand-alone interpreter for the launcher")
    base = tempfile.mkdtemp(prefix="bazarr-landlock-")
    listener = None
    try:
        allowed = os.path.join(base, "allowed")
        os.mkdir(allowed)
        Path(base, "canary.txt").write_text("canary", encoding="utf-8")
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
        spec = RuntimeSpec(
            interpreter=python,
            args=("-I", "-c", _CANARY_SCRIPT, base, allowed, str(port)),
            cwd=allowed,
            env=clean_env(home=allowed, tmpdir=allowed),
            policy=policy,
            term_grace=1.0,
        )
        process = _start(spec, enforce=True, min_abi=1, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.stop(0.5)
            return SandboxProbe(abi=abi, verified=False, reason="canary timed out")
        finally:
            process.stop(0.5)
        if process.returncode != 0:
            failure = process.launcher_failure()
            detail = failure.message if failure else (err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"])[0]
            return SandboxProbe(abi=abi, verified=False, reason="canary could not run: %s" % detail)
        result = json.loads(out.decode("utf-8").strip().splitlines()[-1])
        verified = (
            result.get("read_outside") == "denied"
            and result.get("write_outside") == "denied"
            and result.get("write_inside") == "allowed"
        )
        if not verified:
            return SandboxProbe(abi=abi, verified=False, reason="canary escaped the ruleset: %s" % result)
        return SandboxProbe(
            abi=abi,
            verified=True,
            network_verified=abi >= 4 and result.get("connect") == "denied",
            signal_scope_verified=abi >= 6 and result.get("signal") == "denied",
            reason="kernel reports ABI %d, canary confined" % abi,
        )
    except (OSError, ValueError, PolicyError, json.JSONDecodeError) as exc:
        return SandboxProbe(abi=abi, verified=False, reason="probe failed: %s" % exc)
    finally:
        if listener is not None:
            listener.close()
        shutil.rmtree(base, ignore_errors=True)


def _close_fds(holder: dict) -> None:
    for key in ("lifeline", "status"):
        fd = holder.get(key)
        holder[key] = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class LauncherProcess(subprocess.Popen):
    """The launcher process. Its subtree is the worker.

    Holds the write end of the lifeline pipe: when this object is closed or
    garbage collected (a finalizer closes it), when the interpreter exits, or
    when the host dies, the launcher sees end-of-file and kills the whole
    worker tree.
    """

    enforcement: str = "declared"

    def __init__(self, *args, lifeline: int | None = None, status: int | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Set only after the child started, so a failed start leaves both fds
        # to the caller. The finalizer holds the dict, not self.
        self._fds = {"lifeline": lifeline, "status": status}
        self._failure: LauncherFailure | None = None
        self._status_text = b""
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
        fd, self._fds["lifeline"] = self._fds.get("lifeline"), None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def launcher_failure(self) -> LauncherFailure | None:
        """The launcher's own report that the worker could not be started.

        Authoritative: it comes over a pipe only the launcher writes to. A
        worker that exits 78, or prints a line that looks like a launcher
        message, does not produce one. Exact once the process has exited.
        """
        fd = self._fds.get("status")
        if self._failure is None and fd is not None:
            try:
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    self._status_text += chunk
            except (BlockingIOError, OSError):
                pass
            for line in self._status_text.decode("utf-8", "replace").splitlines():
                try:
                    record = json.loads(line)
                    self._failure = LauncherFailure(str(record["kind"]), str(record.get("message", "")))
                    break
                except (ValueError, KeyError, TypeError):
                    continue
        return self._failure

    def raise_for_sandbox_failure(self) -> None:
        """Raise ``SandboxApplyError`` when the ruleset or limits could not be applied."""
        failure = self.launcher_failure()
        if failure is not None and failure.kind == "sandbox":
            raise SandboxApplyError(failure.message)

    def stop(self, grace: float = 3.0) -> None:
        """SIGTERM the launcher, which stops the tree; SIGKILL the group after ``grace``.

        Prefer this to killing the process group outright: a launcher that is
        killed cannot reap workers that moved themselves into a new session.
        """
        if self.poll() is None:
            try:
                self.terminate()
            except OSError:
                pass
            try:
                self.wait(timeout=grace + 2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    self.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.close_lifeline()

    def wait(self, timeout=None):
        result = super().wait(timeout=timeout)
        self.close_lifeline()
        return result


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


def _start(spec: RuntimeSpec, *, enforce: bool, min_abi: int, launcher_python: str | None = None, **popen_kwargs) -> LauncherProcess:
    data = spec.to_launch_json(enforce=enforce, min_abi=min_abi)
    python = launcher_python or sys.executable
    spec_fd = _spec_fd(data)
    life_r, life_w = os.pipe()
    status_r, status_w = os.pipe()
    os.set_blocking(status_r, False)
    try:
        # Plain Popen, no preexec_fn. The launcher gets an empty environment:
        # nothing from the host's reaches it, and so nothing reaches the worker
        # except what the spec lists.
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
    except BaseException:
        os.close(life_w)
        os.close(status_r)
        raise
    finally:
        os.close(spec_fd)
        os.close(life_r)
        os.close(status_w)
    process.enforcement = "enforced" if enforce else "declared"
    return process


def spawn_worker(
    spec: RuntimeSpec,
    *,
    probe: SandboxProbe | None = None,
    require_sandbox: bool = False,
    launcher_python: str | None = None,
    stdin: Any = subprocess.PIPE,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    **popen_kwargs: Any,
) -> LauncherProcess:
    """Start one worker through the launcher and return its process handle.

    This is what the worker spawner calls. The decision follows the probe:

    * Probe positive: the ruleset is mandatory. If it cannot be applied the
      launcher exits with code 78 and the worker never runs (fail closed;
      ``process.launcher_failure()`` carries the reason and
      ``process.raise_for_sandbox_failure()`` raises ``SandboxApplyError``).
      There is no silent downgrade.
    * Probe negative: the worker runs with limits and a clean environment but
      without a ruleset, ``process.enforcement == "declared"``. With
      ``require_sandbox=True`` this raises ``SandboxRefused`` instead.

    ``popen_kwargs`` (``text``, ``bufsize`` and so on) go to ``subprocess.Popen``;
    ``preexec_fn`` is refused. On a platform other than Linux the worker starts
    directly with the clean environment and ``enforcement == "declared"``.
    """
    if "preexec_fn" in popen_kwargs:
        raise TypeError("preexec_fn is not allowed: the host never runs code between fork and exec")
    if not landlock.is_supported_platform():
        if require_sandbox:
            raise SandboxRefused("this platform cannot sandbox workers")
        process = subprocess.Popen(
            [spec.interpreter, *spec.args],
            cwd=spec.cwd,
            env=dict(spec.env),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            **popen_kwargs,
        )
        return process  # type: ignore[return-value]
    probe = probe or probe_sandbox()
    if not probe.enforced and require_sandbox:
        raise SandboxRefused("this host cannot sandbox workers: %s" % (probe.reason or "no Landlock"))
    return _start(
        spec,
        enforce=probe.enforced,
        min_abi=probe.abi,
        launcher_python=launcher_python,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        **popen_kwargs,
    )

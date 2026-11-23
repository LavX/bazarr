"""Persist worker identities on hosts without the Linux owner sweep."""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

_root: Path | None = None
_owner = uuid.uuid4().hex
_JOB_PREFIX = "Global\\BazarrPluginWorker-"


def _directory(config_dir) -> Path:
    return Path(config_dir) / "provider_hub" / "worker-activity"


def configure(config_dir) -> None:
    global _root
    _root = _directory(config_dir) if sys.platform != "linux" else None


def is_windows() -> bool:
    return sys.platform == "win32"


def registry_enabled() -> bool:
    return _root is not None


def _windows_process(pid: int) -> tuple[int | None, bool]:
    """Read creation time and exit state through one Windows process handle."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    filetime = ctypes.POINTER(wintypes.FILETIME)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, filetime, filetime, filetime, filetime)
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # the recorded PID no longer identifies a process
            return None, False
        raise ctypes.WinError(error)
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in (created, exited, system, user))):
            raise ctypes.WinError(ctypes.get_last_error())
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in {0, 0x102}:  # WAIT_OBJECT_0 or WAIT_TIMEOUT
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.dwHighDateTime << 32) | created.dwLowDateTime, result == 0x102
    finally:
        kernel.CloseHandle(handle)


class _WindowsJob:
    """Keep the exact job object pinned until all its processes have stopped."""

    def __init__(self, handle, api, api_error):
        self._handle = handle
        self._api = api
        self._api_error = api_error
        self._lock = threading.Lock()

    def empty(self):
        try:
            with self._lock:
                return self._handle is None or not self._api.QueryInformationJobObject(
                    self._handle, self._api.JobObjectBasicAccountingInformation)["ActiveProcesses"]
        except self._api_error as error:
            raise OSError(str(error)) from error

    def terminate(self):
        try:
            with self._lock:
                if self._handle is not None:
                    self._api.TerminateJobObject(self._handle, 1)
        except self._api_error as error:
            raise OSError(str(error)) from error

    def close(self):
        with self._lock:
            if self._handle is not None:
                self._handle.Close()
                self._handle = None


def _open_windows_job(name, *, create=False):
    import win32job
    from pywintypes import error as JobError

    if not create:
        try:
            return _WindowsJob(win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, name), win32job, JobError)
        except JobError as error:
            if getattr(error, "winerror", None) == 2:  # ERROR_FILE_NOT_FOUND
                return None
            raise OSError(str(error)) from error
    job = _WindowsJob(win32job.CreateJobObject(None, name), win32job, JobError)
    try:
        info = win32job.QueryInformationJobObject(job._handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job._handle, win32job.JobObjectExtendedLimitInformation, info)
    except BaseException:
        job.close()
        raise
    return job


def _register(path, **identity):
    staging = path.with_suffix(".tmp")
    try:
        with staging.open("x", encoding="ascii") as output:
            json.dump({"owner": _owner, **identity}, output)
            output.flush()
            os.fsync(output.fileno())
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def start_worker(create, command, *, group=None):
    if _root is None:
        return create(command)
    _root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _root / f"{uuid.uuid4().hex}.json"
    if is_windows():
        pid = os.getpid()
        created, running = _windows_process(pid)
        if created is None or not running:
            raise OSError("Could not identify the plugin host process")
        name = _JOB_PREFIX + path.stem
        job = _open_windows_job(name, create=True)
        try:
            _register(path, host=[pid, created])
            launcher = [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()),
                        name, str(pid), str(created), *command]
            process = create(launcher)
        except BaseException:
            # A launcher still starting cannot enter the plugin after its
            # job closes. Keep the complete registration for the next check.
            job.close()
            raise
        process.activity_job = job
    else:
        if group is None:
            raise OSError("Could not start a process group for the plugin worker")
        _register(path, group=group)
        process = create(command)
        process.activity_group = group
    process.activity_record = path
    return process


def is_registered(process) -> bool:
    return isinstance(getattr(process, "activity_record", None), Path)


def tree_gone(process, ceiling=0.0):
    job = getattr(process, "activity_job", None)
    group = getattr(process, "activity_group", None)
    if not is_registered(process):
        return True
    deadline = time.monotonic() + ceiling
    while True:
        try:
            stopped = job.empty() if job is not None else not _group_running(group)
            if stopped:
                return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))


def forget(process) -> None:
    """Drop the registration after the caller confirmed shutdown."""
    if not is_registered(process):
        return
    job = getattr(process, "activity_job", None)
    if job is not None:
        job.close()
    with contextlib.suppress(OSError):
        process.activity_record.unlink(missing_ok=True)


def _still_running(path, record) -> bool:
    if is_windows():
        pid, expected = record["host"]
        if type(pid) is not int or pid <= 0 or type(expected) is not int or expected <= 0:
            return True
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
            return True
        job = _open_windows_job(_JOB_PREFIX + path.stem)
        try:
            created, running = _windows_process(pid)
            # A missing name alone says nothing about a host that has not
            # finished closing its handles yet.
            return (running and created == expected) or (job is not None and not job.empty())
        finally:
            if job is not None:
                job.close()
    return _group_running(record["group"])


def _group_running(group):
    if type(group) is not int or group <= 0 or not hasattr(os, "killpg"):
        return True
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def previous_workers_stopped(config_dir, *, deadline: float | None = None) -> bool:
    folder = _directory(config_dir)
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for path in entries:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if path.suffix != ".json":
            continue
        try:
            if path.is_symlink() or not path.is_file():
                return False
            record = json.loads(path.read_text(encoding="ascii"))
            if record["owner"] == _owner:
                continue
            if _still_running(path, record):
                return False
            path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return deadline is None or time.monotonic() < deadline


def _attach_windows_job(name):
    # This launcher runs with only the standard library. It joins the job
    # before loading the worker interpreter or any of the plugin's code.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.argtypes = ()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenJobObjectW(1, False, name)  # JOB_OBJECT_ASSIGN_PROCESS
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def _run_windows_worker(name, pid, expected, command):
    _attach_windows_job(name)
    created, running = _windows_process(pid)
    # A new host may have briefly opened the same job to check its members.
    # Joining that job cannot grant this launcher permission to start work.
    if not running or created != expected:
        return 1
    return subprocess.call(command, stdin=0, stdout=1, stderr=2)


if __name__ == "__main__":
    raise SystemExit(_run_windows_worker(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))

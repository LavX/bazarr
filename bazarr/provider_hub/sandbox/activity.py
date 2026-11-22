"""Persist worker identities on hosts without the Linux owner sweep."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

_root: Path | None = None
_owner = uuid.uuid4().hex


def _directory(config_dir) -> Path:
    return Path(config_dir) / "provider_hub" / "worker-activity"


def configure(config_dir) -> None:
    global _root
    _root = _directory(config_dir) if sys.platform != "linux" else None


def is_windows() -> bool:
    return sys.platform == "win32"


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


def start_worker(create, *, group=None):
    if _root is None:
        return create()
    _root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _root / f"{uuid.uuid4().hex}.json"
    with path.open("x", encoding="ascii") as output:
        json.dump({"owner": _owner}, output)
    try:
        process = create()
    except (OSError, ValueError):
        # Popen reported that it could not start the child.
        path.unlink(missing_ok=True)
        raise
    try:
        if is_windows():
            created, running = _windows_process(process.pid)
            if process.poll() is not None:
                path.unlink(missing_ok=True)
                return process
            if created is None or not running:
                raise OSError("Could not identify the new plugin worker")
            record = {"pid": process.pid, "created": created}
        else:
            record = {"group": group if group is not None else process.pid}
        record["owner"] = _owner
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(record), encoding="ascii")
        os.replace(staging, path)
        process.activity_record = path
        return process
    except Exception:
        # Keep the record unless the exact new worker can be accounted for.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
            if is_windows():
                path.unlink(missing_ok=True)
        raise


def forget(process) -> None:
    """Drop the registration after the caller confirmed shutdown."""
    path = getattr(process, "activity_record", None)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _still_running(record) -> bool:
    if is_windows():
        pid, expected = record["pid"], record["created"]
        if type(pid) is not int or pid <= 0 or type(expected) is not int or expected <= 0:
            return True
        created, running = _windows_process(pid)
        return running and created == expected
    group = record["group"]
    if type(group) is not int or group <= 0 or not hasattr(os, "killpg"):
        return True
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def previous_workers_stopped(config_dir) -> bool:
    folder = _directory(config_dir)
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            if path.is_symlink() or not path.is_file():
                return False
            record = json.loads(path.read_text(encoding="ascii"))
            if record["owner"] == _owner:
                continue
            if _still_running(record):
                return False
            path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return True

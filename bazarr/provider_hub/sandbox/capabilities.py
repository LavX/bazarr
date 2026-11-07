# coding=utf-8
"""Drop every Linux capability the worker would otherwise inherit (ctypes, standard library only).

``no_new_privs`` stops an exec from gaining privilege, but it does not take away
what the process already holds. A Bazarr+ that runs as root with ``CAP_SYS_ADMIN``
would hand that to a worker, which could then freeze a granted file system
(``FIFREEZE``) or do other things Landlock and the metadata filter never see.

The child calls ``drop_all`` before the seccomp filter goes on. In order:

1. With ``CAP_SETPCAP``: lock the securebits so no exec or ``setuid`` can bring
   capabilities back (no root special-casing, no setuid fix-up, no ambient
   raise, ``KEEP_CAPS`` locked off).
2. Clear the ambient set (needs no capability).
3. With ``CAP_SETPCAP``: drop every capability from the bounding set.
4. ``capset`` effective, permitted and inheritable to empty.

Steps that need ``CAP_SETPCAP`` are skipped without it: a process that lacks it
(the usual unprivileged container user) holds no capability worth locking away,
and ``no_new_privs`` keeps an exec from gaining any. Every other failure raises
``OSError``, and the launcher must not run the worker then. At the end the
result is read back from ``/proc/self/status`` and anything non-zero is an error.

This module is loaded by file path from ``launcher.py`` and also imported by the
host, so it uses no relative imports.
"""
from __future__ import annotations

import ctypes
import errno
import platform

PR_CAPBSET_READ = 23
PR_CAPBSET_DROP = 24
PR_SET_SECUREBITS = 28
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4

SECBIT_NOROOT = 1 << 0
SECBIT_NOROOT_LOCKED = 1 << 1
SECBIT_NO_SETUID_FIXUP = 1 << 2
SECBIT_NO_SETUID_FIXUP_LOCKED = 1 << 3
SECBIT_KEEP_CAPS_LOCKED = 1 << 5
SECBIT_NO_CAP_AMBIENT_RAISE = 1 << 6
SECBIT_NO_CAP_AMBIENT_RAISE_LOCKED = 1 << 7

LOCKED_SECUREBITS = (
    SECBIT_NOROOT
    | SECBIT_NOROOT_LOCKED
    | SECBIT_NO_SETUID_FIXUP
    | SECBIT_NO_SETUID_FIXUP_LOCKED
    | SECBIT_KEEP_CAPS_LOCKED
    | SECBIT_NO_CAP_AMBIENT_RAISE
    | SECBIT_NO_CAP_AMBIENT_RAISE_LOCKED
)

CAP_SETPCAP = 8
LINUX_CAPABILITY_VERSION_3 = 0x20080522
# Bounding set bits above this are never valid; the loop stops at the first EINVAL.
MAX_CAP_PROBE = 64

# capget, capset
_SYSCALLS = {
    "x86_64": (125, 126),
    "amd64": (125, 126),
    "aarch64": (90, 91),
    "arm64": (90, 91),
}

STATUS_PATH = "/proc/self/status"


class _Header(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _Data(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
        _libc.prctl.restype = ctypes.c_int
    return _libc


def _error(what: str, err: int) -> OSError:
    return OSError(err, "%s failed: %s" % (what, errno.errorcode.get(err, err)))


def _prctl(option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> int:
    """Raw prctl: returns the result, or a negative errno."""
    libc = _get_libc()
    ctypes.set_errno(0)
    result = libc.prctl(
        ctypes.c_int(option),
        ctypes.c_ulong(arg2),
        ctypes.c_ulong(arg3),
        ctypes.c_ulong(arg4),
        ctypes.c_ulong(arg5),
    )
    if result < 0:
        return -(ctypes.get_errno() or errno.EPERM)
    return int(result)


def _syscall_numbers() -> tuple[int, int]:
    numbers = _SYSCALLS.get(platform.machine().lower())
    if numbers is None:
        raise OSError(errno.ENOSYS, "no capability syscalls known for this architecture")
    return numbers


def _capget() -> tuple[int, int, int]:
    """Effective, permitted and inheritable sets of this thread, as 64-bit masks."""
    number = _syscall_numbers()[0]
    header = _Header(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_Data * 2)()
    libc = _get_libc()
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(number),
        ctypes.c_void_p(ctypes.addressof(header)),
        ctypes.c_void_p(ctypes.addressof(data)),
    )
    if result != 0:
        raise _error("capget", ctypes.get_errno())
    low, high = data[0], data[1]
    return (
        low.effective | (high.effective << 32),
        low.permitted | (high.permitted << 32),
        low.inheritable | (high.inheritable << 32),
    )


def _capset_empty() -> None:
    number = _syscall_numbers()[1]
    header = _Header(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_Data * 2)()
    libc = _get_libc()
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(number),
        ctypes.c_void_p(ctypes.addressof(header)),
        ctypes.c_void_p(ctypes.addressof(data)),
    )
    if result != 0:
        raise _error("capset", ctypes.get_errno())


def read_status(path: str = STATUS_PATH) -> dict[str, int]:
    """The ``Cap*`` lines of a status file as integers (missing ones are absent)."""
    found: dict[str, int] = {}
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        for line in handle:
            name, sep, value = line.partition(":")
            if sep and name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
                try:
                    found[name] = int(value.strip(), 16)
                except ValueError:
                    raise OSError(errno.EIO, "unreadable %s line in %s" % (name, path))
    return found


def _lock_securebits() -> None:
    result = _prctl(PR_SET_SECUREBITS, LOCKED_SECUREBITS)
    if result < 0:
        raise _error("locking the securebits", -result)


def _clear_ambient() -> None:
    result = _prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL)
    # EINVAL: the kernel predates ambient capabilities, so there is no set to clear.
    if result < 0 and -result != errno.EINVAL:
        raise _error("clearing the ambient capabilities", -result)


def _drop_bounding_set() -> None:
    for cap in range(MAX_CAP_PROBE):
        held = _prctl(PR_CAPBSET_READ, cap)
        if held == -errno.EINVAL:
            return  # past the last capability this kernel knows
        if held < 0:
            raise _error("reading the bounding set", -held)
        if held:
            result = _prctl(PR_CAPBSET_DROP, cap)
            if result < 0:
                raise _error("dropping bounding capability %d" % cap, -result)


def drop_all(status_path: str = STATUS_PATH) -> dict[str, bool]:
    """Remove every capability from the calling thread. Raises ``OSError`` on any failure.

    Returns ``{"bounding_dropped": bool}``: whether the bounding set was emptied
    (it needs ``CAP_SETPCAP``). The effective, permitted, inheritable and ambient
    sets are always empty on success.
    """
    effective, _permitted, _inheritable = _capget()
    can_setpcap = bool(effective & (1 << CAP_SETPCAP))
    if can_setpcap:
        _lock_securebits()
    _clear_ambient()
    if can_setpcap:
        _drop_bounding_set()
    _capset_empty()

    state = read_status(status_path)
    wanted = ("CapEff", "CapPrm", "CapInh") + (("CapBnd",) if can_setpcap else ())
    for name in wanted:
        if name not in state:
            raise OSError(errno.EIO, "%s is missing from %s; cannot confirm the capabilities were dropped" % (name, status_path))
    for name in wanted + ("CapAmb",):
        if state.get(name, 0) != 0:
            raise OSError(errno.EPERM, "%s is still %#x after dropping capabilities" % (name, state[name]))
    return {"bounding_dropped": can_setpcap}


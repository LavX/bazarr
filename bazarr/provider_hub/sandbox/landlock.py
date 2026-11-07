# coding=utf-8
"""Landlock bindings over raw syscalls (ctypes, standard library only).

This module is loaded two ways: as ``provider_hub.sandbox.landlock`` inside the
host, and by file path from ``launcher.py``, which runs under ``python -I -S``
without any Bazarr+ import machinery. It therefore uses no relative imports and
no third-party code.

Landlock ABI levels and what this module enforces at each:

====  =========================================================================
ABI   Enforced here
====  =========================================================================
1     Filesystem rules (read, write, execute, create, remove)
2     Adds REFER: cross-directory rename and link are denied unless granted.
      Kernels at ABI 1 deny every cross-directory rename with EXDEV.
3     Adds TRUNCATE
4     TCP connect and bind rules (only when a port set is given)
5     Adds IOCTL_DEV on device files
6     Scoping: signals and abstract UNIX sockets cannot cross the domain edge
7, 8  Nothing extra (audit logging flags and TSYNC are not used)
9     Adds RESOLVE_UNIX: pathname UNIX sockets outside the domain are denied
      unless granted
10    Nothing extra (UDP rules are left unhandled on purpose: DNS stays open)
====  =========================================================================

Every handled right is masked down to the running kernel's ABI, so a ruleset is
never rejected for asking for a right the kernel does not know.
"""
from __future__ import annotations

import ctypes
import errno
import os
import stat as stat_module
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14
FS_IOCTL_DEV = 1 << 15
FS_RESOLVE_UNIX = 1 << 16

NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1

# Rights that make sense on a regular file or device node. Anything else makes
# landlock_add_rule fail with EINVAL when the rule's object is not a directory.
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

RIGHTS_READ_ONLY = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
# Read-write trees may create and remove entries but never execute files and
# never create device nodes.
RIGHTS_READ_WRITE = (
    FS_READ_FILE
    | FS_READ_DIR
    | FS_WRITE_FILE
    | FS_REMOVE_DIR
    | FS_REMOVE_FILE
    | FS_MAKE_DIR
    | FS_MAKE_REG
    | FS_MAKE_SOCK
    | FS_MAKE_FIFO
    | FS_MAKE_SYM
    | FS_REFER
    | FS_TRUNCATE
    | FS_IOCTL_DEV
)
RIGHTS_DEVICE = FS_READ_FILE | FS_WRITE_FILE | FS_IOCTL_DEV
RIGHTS_RESOLVE_UNIX = FS_RESOLVE_UNIX

PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38
PR_SET_CHILD_SUBREAPER = 36

_FS_BY_ABI = (
    (1, (1 << 13) - 1),  # bits 0..12
    (2, FS_REFER),
    (3, FS_TRUNCATE),
    (5, FS_IOCTL_DEV),
    (9, FS_RESOLVE_UNIX),
)
_NET_BY_ABI = ((4, NET_BIND_TCP | NET_CONNECT_TCP),)
_SCOPE_BY_ABI = ((6, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL),)

class LandlockError(OSError):
    """A Landlock syscall failed while building or applying a ruleset."""


class RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttr(ctypes.Structure):
    # The kernel struct is packed (12 bytes). Its fields sit at offsets 0 and 8
    # either way, and the kernel reads only 12 bytes, so the trailing padding
    # of this natural layout is harmless.
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class NetPortAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("port", ctypes.c_uint64),
    ]


_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
        _libc.prctl.restype = ctypes.c_int
    return _libc


def is_supported_platform() -> bool:
    return sys.platform.startswith("linux")


def _syscall(number: int, *args) -> tuple[int, int]:
    """Return ``(result, errno)``. A single place to stub in tests."""
    libc = _get_libc()
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *args)
    return int(result), ctypes.get_errno()


def prctl(option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> int:
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
        err = ctypes.get_errno()
        raise OSError(err, "prctl(%d) failed: %s" % (option, os.strerror(err)))
    return int(result)


def probe_abi() -> tuple[int, str]:
    """Ask the kernel for its highest supported Landlock ABI.

    Uses ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)``
    directly. The kernel's ``/sys/kernel/security/lsm`` file is normally not
    mounted inside a container, so it is never consulted. Returns ``(0,
    reason)`` when Landlock cannot be used.
    """
    if not is_supported_platform():
        return 0, "not Linux"
    try:
        result, err = _syscall(
            SYS_LANDLOCK_CREATE_RULESET,
            ctypes.c_void_p(None),
            ctypes.c_size_t(0),
            ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
        )
    except (OSError, AttributeError) as exc:
        return 0, "syscall unavailable: %s" % exc
    if result > 0:
        return result, "kernel reports ABI %d" % result
    code = errno.errorcode.get(err, str(err))
    if err == errno.ENOSYS:
        return 0, "kernel has no Landlock (ENOSYS)"
    if err == errno.EOPNOTSUPP:
        return 0, "Landlock is disabled at boot (EOPNOTSUPP)"
    if err == errno.EPERM:
        return 0, "syscall blocked, probably by a seccomp profile (EPERM)"
    return 0, "Landlock probe failed (%s)" % code


def get_abi() -> int:
    return probe_abi()[0]


def _mask_for_abi(table: Sequence[tuple[int, int]], abi: int) -> int:
    mask = 0
    for since, bits in table:
        if abi >= since:
            mask |= bits
    return mask


def handled_fs_for_abi(abi: int) -> int:
    return _mask_for_abi(_FS_BY_ABI, abi)


def handled_net_for_abi(abi: int) -> int:
    return _mask_for_abi(_NET_BY_ABI, abi)


def scoped_for_abi(abi: int) -> int:
    return _mask_for_abi(_SCOPE_BY_ABI, abi)


def attr_size_for_abi(abi: int) -> int:
    """Size of ``landlock_ruleset_attr`` the given ABI understands."""
    if abi >= 6:
        return 24
    if abi >= 4:
        return 16
    return 8


@dataclass
class AppliedRuleset:
    abi: int
    handled_fs: int
    handled_net: int
    scoped: int
    skipped: list[str] = field(default_factory=list)

    @property
    def filesystem_enforced(self) -> bool:
        return self.handled_fs != 0

    @property
    def network_enforced(self) -> bool:
        return bool(self.handled_net)

    @property
    def signal_scope_enforced(self) -> bool:
        return bool(self.scoped & SCOPE_SIGNAL)


def _fail(what: str, err: int) -> LandlockError:
    return LandlockError(err, "%s failed: %s" % (what, os.strerror(err)))


def _add_path_rule(ruleset_fd: int, path: str, rights: int, handled_fs: int) -> bool:
    """Add one path-beneath rule. Returns False when the path does not exist."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False
    allowed = rights & handled_fs
    if not stat_module.S_ISDIR(mode):
        allowed &= FILE_RIGHTS
    if not allowed:
        return True
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attr = PathBeneathAttr(allowed, fd)
        result, err = _syscall(
            SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
            ctypes.c_void_p(ctypes.addressof(attr)),
            ctypes.c_uint32(0),
        )
    finally:
        os.close(fd)
    if result != 0:
        raise _fail("landlock_add_rule(%s)" % path, err)
    return True


def _add_port_rule(ruleset_fd: int, port: int) -> None:
    attr = NetPortAttr(NET_CONNECT_TCP, port)
    result, err = _syscall(
        SYS_LANDLOCK_ADD_RULE,
        ctypes.c_int(ruleset_fd),
        ctypes.c_int(LANDLOCK_RULE_NET_PORT),
        ctypes.c_void_p(ctypes.addressof(attr)),
        ctypes.c_uint32(0),
    )
    if result != 0:
        raise _fail("landlock_add_rule(tcp %d)" % port, err)


def apply_ruleset(
    *,
    read_only: Iterable[str] = (),
    read_write: Iterable[str] = (),
    devices: Iterable[str] = (),
    resolve_unix: Iterable[str] = (),
    connect_ports: Iterable[int] | None = None,
    abi_cap: int | None = None,
    min_abi: int = 1,
) -> AppliedRuleset:
    """Restrict the calling thread (and its future children) irreversibly.

    Call this from a single-threaded process right before ``execve``.
    ``read_write`` paths must exist (``LandlockError`` otherwise: a worker must
    not start with fewer rules than asked); missing ``read_only``, ``devices``
    and ``resolve_unix`` paths are skipped and reported.

    ``connect_ports`` of ``None`` leaves TCP unrestricted. An iterable, even an
    empty one, makes TCP connect and bind default-deny at ABI 4 and above, with
    connect allowed to the listed ports only.

    Raises ``LandlockError`` when the kernel is below ``min_abi`` or when any
    syscall fails: callers that required a sandbox must not run the worker.
    """
    abi, reason = probe_abi()
    if abi < max(1, min_abi):
        raise LandlockError(errno.ENOSYS, "Landlock ABI %d below required %d (%s)" % (abi, min_abi, reason))
    effective = min(abi, abi_cap) if abi_cap else abi

    ports = None if connect_ports is None else sorted({int(p) for p in connect_ports})
    handled_fs = handled_fs_for_abi(effective)
    handled_net = handled_net_for_abi(effective) if ports is not None else 0
    scoped = scoped_for_abi(effective)

    prctl(PR_SET_NO_NEW_PRIVS, 1)

    attr = RulesetAttr(handled_fs, handled_net, scoped)
    ruleset_fd, err = _syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(ctypes.addressof(attr)),
        ctypes.c_size_t(attr_size_for_abi(effective)),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        raise _fail("landlock_create_ruleset", err)

    skipped: list[str] = []
    try:
        skipped.extend(p for p in read_only if not _add_path_rule(ruleset_fd, p, RIGHTS_READ_ONLY, handled_fs))
        skipped.extend(p for p in devices if not _add_path_rule(ruleset_fd, p, RIGHTS_DEVICE, handled_fs))
        if handled_fs & FS_RESOLVE_UNIX:
            skipped.extend(p for p in resolve_unix if not _add_path_rule(ruleset_fd, p, RIGHTS_RESOLVE_UNIX, handled_fs))
        for path in read_write:
            if not _add_path_rule(ruleset_fd, path, RIGHTS_READ_WRITE, handled_fs):
                raise LandlockError(errno.ENOENT, "read-write path does not exist: %s" % path)
        if handled_net:
            for port in ports or ():
                _add_port_rule(ruleset_fd, port)
        result, err = _syscall(
            SYS_LANDLOCK_RESTRICT_SELF,
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(0),
        )
        if result != 0:
            raise _fail("landlock_restrict_self", err)
    finally:
        os.close(ruleset_fd)

    return AppliedRuleset(
        abi=effective,
        handled_fs=handled_fs,
        handled_net=handled_net,
        scoped=scoped,
        skipped=skipped,
    )

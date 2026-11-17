# coding=utf-8
"""A seccomp-bpf filter that denies file metadata changes (ctypes, standard library only).

Landlock does not cover ``chmod``, ``chown``, ``utimes`` or extended
attributes. A worker runs as the host's user, so ``os.chmod(config_dir, 0)``
would lock the host out of its own configuration. Seccomp cannot inspect the
path argument of those calls, so they cannot be scoped to the worker's own
directories: every one of them returns ``EPERM``, everywhere.

File attribute ioctls are denied too: ``FS_IOC_SETFLAGS`` and
``FS_IOC_FSSETXATTR`` (what ``chattr`` uses; it opens the file read-only, so a
read grant is enough), ``FS_IOC_ENABLE_VERITY`` (makes a file permanently
read-only), ``FS_IOC_SET_ENCRYPTION_POLICY``, and ``FIFREEZE``/``FITHAW``
(which also need a capability the launcher has already dropped).

Kernel keyring calls are denied: a same-user worker would share the host's
user keyring. So are the calls that change the scheduling of other processes
(``setpriority``, ``ioprio_set``, ``sched_set*``): Landlock does not scope
them, and a worker could otherwise niceness-starve or pin its launcher or the
host.

The default is to allow, so every call that reaches state outside the worker's
own process tree, or that outlives it, is listed here unless the kernel already
refuses it. System V IPC (``shm*``, ``sem*``, ``msg*``) and POSIX message
queues (``mq_*``) are denied: the IPC namespace is the host's, so a worker could
attach to or create segments, semaphores and queues the host or another service
uses, and they outlive the process. Also denied: ``perf_event_open``, ``bpf``,
``userfaultfd``, ``inotify_init*`` and ``fanotify_init`` (per-user instance
limits are shared with the host), ``process_vm_readv``/``process_vm_writev``,
``kcmp``, ``open_by_handle_at``, ``syslog`` (the kernel log), ``lsm_set_self_attr``
and the mount family (``mount``, ``umount2``, ``pivot_root``, ``chroot``,
``move_mount``, ``open_tree``, ``fsopen``, ``fsconfig``, ``fsmount``, ``fspick``,
``mount_setattr``). Landlock or the dropped capabilities already refuse most of
these; the filter makes that independent of the kernel configuration.

Two further rules: ``io_uring`` is denied outright (it can perform the same
metadata operations from inside the ring), and resource limits are frozen:
``setrlimit`` is denied and ``prlimit64`` may only query (a NULL new limit).
The launcher applies the limits before the filter goes on, so a worker holding
``CAP_SYS_RESOURCE`` cannot raise its own or anyone else's.

Consequences for workers, by design: no ``chmod``, ``chown``, ``utime``,
``setxattr`` or ``removexattr`` in any form. Files get their mode from the
umask when they are created (077 under the launcher). ``shutil.copy2``,
``shutil.copystat`` and ``shutil.move`` across file systems call ``utime`` and
fail; ``shutil.copy``, ``shutil.copyfile`` and a plain rename work.

Supported on x86-64 and AArch64 with a native (64-bit) interpreter. Elsewhere
``supported()`` is false and callers must report the sandbox as not enforced.
Calls made through any other syscall ABI of the same CPU (32-bit compatibility
mode, the x32 ABI) kill the process, since their numbers differ.

This module is loaded by file path from ``launcher.py`` and also imported by the
host, so it uses no relative imports.
"""
from __future__ import annotations

import ctypes
import errno
import platform
import struct
import sys

PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_KILL_PROCESS = 0x80000000

X32_SYSCALL_BIT = 0x40000000

# Offset of seccomp_data.args[0]; each argument is 64 bits, low half first.
_ARG_OFFSET = 16

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JSET_K = 0x45
_BPF_RET_K = 0x06

# Same on both architectures (the mount calls and lsm_set_self_attr are in the
# generic numbering from 428 up). io_uring can issue the same metadata operations
# (IORING_OP_SETXATTR and friends) without going through the syscalls above.
_COMMON = {
    "fchmodat2": 452,
    "setxattrat": 463,
    "removexattrat": 466,
    "io_uring_setup": 425,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "open_tree": 428,
    "move_mount": 429,
    "fsopen": 430,
    "fsconfig": 431,
    "fsmount": 432,
    "fspick": 433,
    "process_madvise": 440,
    "mount_setattr": 442,
    "lsm_set_self_attr": 460,
}

# prlimit64 is allowed only as a query (new_limit is NULL). The launcher sets
# its limits before the filter is installed, so a worker has no reason to set
# any, and one with CAP_SYS_RESOURCE could otherwise raise a hard limit or
# lower its launcher's open-file limit. setrlimit is denied outright.
PRLIMIT64 = {AUDIT_ARCH_X86_64: 302, AUDIT_ARCH_AARCH64: 261}

# ioctl is allowed except for these commands (the low 32 bits of its second
# argument, which is all the kernel reads). The encodings are the same on
# x86-64 and AArch64.
IOCTL = {AUDIT_ARCH_X86_64: 16, AUDIT_ARCH_AARCH64: 29}
DENIED_IOCTLS = {
    "FS_IOC_SETFLAGS": 0x40086602,
    "FS_IOC32_SETFLAGS": 0x40046602,
    "FS_IOC_FSSETXATTR": 0x401C5820,
    "FS_IOC_ENABLE_VERITY": 0x40806685,
    "FS_IOC_SET_ENCRYPTION_POLICY": 0x800C6613,
    "FIFREEZE": 0xC0045877,
    "FITHAW": 0xC0045878,
}

DENIED_SYSCALLS = {
    AUDIT_ARCH_X86_64: {
        "chmod": 90,
        "fchmod": 91,
        "chown": 92,
        "fchown": 93,
        "lchown": 94,
        "utime": 132,
        "setxattr": 188,
        "lsetxattr": 189,
        "fsetxattr": 190,
        "removexattr": 197,
        "lremovexattr": 198,
        "fremovexattr": 199,
        "utimes": 235,
        "fchownat": 260,
        "futimesat": 261,
        "fchmodat": 268,
        "utimensat": 280,
        "setrlimit": 160,
        "setpriority": 141,
        "sched_setparam": 142,
        "sched_setscheduler": 144,
        "sched_setaffinity": 203,
        "add_key": 248,
        "request_key": 249,
        "keyctl": 250,
        "ioprio_set": 251,
        "sched_setattr": 314,
        "shmget": 29,
        "shmat": 30,
        "shmctl": 31,
        "semget": 64,
        "semop": 65,
        "semctl": 66,
        "shmdt": 67,
        "msgget": 68,
        "msgsnd": 69,
        "msgrcv": 70,
        "msgctl": 71,
        "semtimedop": 220,
        "mq_open": 240,
        "mq_unlink": 241,
        "mq_timedsend": 242,
        "mq_timedreceive": 243,
        "mq_notify": 244,
        "mq_getsetattr": 245,
        "perf_event_open": 298,
        "bpf": 321,
        "userfaultfd": 323,
        "inotify_init": 253,
        "inotify_init1": 294,
        "fanotify_init": 300,
        "process_vm_readv": 310,
        "process_vm_writev": 311,
        "kcmp": 312,
        "open_by_handle_at": 304,
        "syslog": 103,
        "mount": 165,
        "umount2": 166,
        "pivot_root": 155,
        "chroot": 161,
        **_COMMON,
    },
    # The generic table has no path-based chmod, chown or utime(s): libc maps
    # them to the *at calls.
    AUDIT_ARCH_AARCH64: {
        "setxattr": 5,
        "lsetxattr": 6,
        "fsetxattr": 7,
        "removexattr": 14,
        "lremovexattr": 15,
        "fremovexattr": 16,
        "fchmod": 52,
        "fchmodat": 53,
        "fchownat": 54,
        "fchown": 55,
        "utimensat": 88,
        "setrlimit": 164,
        "ioprio_set": 30,
        "sched_setparam": 118,
        "sched_setscheduler": 119,
        "sched_setaffinity": 122,
        "setpriority": 140,
        "add_key": 217,
        "request_key": 218,
        "keyctl": 219,
        "sched_setattr": 274,
        "shmget": 194,
        "shmctl": 195,
        "shmat": 196,
        "shmdt": 197,
        "semget": 190,
        "semctl": 191,
        "semtimedop": 192,
        "semop": 193,
        "msgget": 186,
        "msgctl": 187,
        "msgrcv": 188,
        "msgsnd": 189,
        "mq_open": 180,
        "mq_unlink": 181,
        "mq_timedsend": 182,
        "mq_timedreceive": 183,
        "mq_notify": 184,
        "mq_getsetattr": 185,
        "perf_event_open": 241,
        "bpf": 280,
        "userfaultfd": 282,
        "inotify_init1": 26,
        "fanotify_init": 262,
        "process_vm_readv": 270,
        "process_vm_writev": 271,
        "kcmp": 272,
        "open_by_handle_at": 265,
        "syslog": 116,
        "mount": 40,
        "umount2": 39,
        "pivot_root": 41,
        "chroot": 51,
        **_COMMON,
    },
}


def native_audit_arch() -> int | None:
    """The audit arch of this interpreter, or ``None`` when no filter exists for it."""
    if not sys.platform.startswith("linux") or struct.calcsize("P") != 8 or sys.byteorder != "little":
        return None
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return AUDIT_ARCH_X86_64
    if machine in ("aarch64", "arm64"):
        return AUDIT_ARCH_AARCH64
    return None


def supported() -> bool:
    return native_audit_arch() is not None


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def build_filter(arch: int | None = None) -> bytes:
    """Classic BPF program for ``arch`` (default: this interpreter's).

    Layout: arch check (a different arch kills the process), x32 check on
    x86-64, one compare per denied number returning ``EPERM``, the prlimit64
    new-limit check, the ioctl command list, otherwise allow. Little-endian only (the halves of a 64-bit
    argument are read at fixed offsets).
    """
    arch = arch if arch is not None else native_audit_arch()
    if arch not in DENIED_SYSCALLS:
        raise ValueError("no seccomp filter for audit arch %#x" % (arch or 0))
    numbers = sorted(set(DENIED_SYSCALLS[arch].values()))

    program: list[list] = []  # [code, jt, jf, k]; jt and jf are ints or label names
    labels: dict[str, int] = {}

    def emit(code, jt, jf, k):
        program.append([code, jt, jf, k])

    def label(name):
        labels[name] = len(program)

    emit(_BPF_LD_W_ABS, 0, 0, 4)  # seccomp_data.arch
    emit(_BPF_JEQ_K, 0, "kill", arch)
    emit(_BPF_LD_W_ABS, 0, 0, 0)  # seccomp_data.nr
    if arch == AUDIT_ARCH_X86_64:
        emit(_BPF_JSET_K, "kill", 0, X32_SYSCALL_BIT)
    for number in numbers:
        emit(_BPF_JEQ_K, "deny", 0, number)
    emit(_BPF_JEQ_K, "ioctl", 0, IOCTL[arch])
    emit(_BPF_JEQ_K, 0, "allow", PRLIMIT64[arch])
    # prlimit64(pid, resource, new_limit, old_limit): a query when new_limit is
    # NULL. new_limit is a pointer, so both halves must be zero.
    emit(_BPF_LD_W_ABS, 0, 0, _ARG_OFFSET + 8 * 2)
    emit(_BPF_JEQ_K, 0, "deny", 0)
    emit(_BPF_LD_W_ABS, 0, 0, _ARG_OFFSET + 8 * 2 + 4)
    emit(_BPF_JEQ_K, "allow", "deny", 0)
    label("ioctl")
    emit(_BPF_LD_W_ABS, 0, 0, _ARG_OFFSET + 8)
    for command in sorted(DENIED_IOCTLS.values()):
        emit(_BPF_JEQ_K, "deny", 0, command)
    emit(_BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW)
    label("allow")
    emit(_BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW)
    label("deny")
    emit(_BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM)
    label("kill")
    emit(_BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS)

    out = []
    for index, (code, jt, jf, k) in enumerate(program):
        resolved = []
        for target in (jt, jf):
            if isinstance(target, str):
                target = labels[target] - index - 1
            if not 0 <= target <= 255:
                raise ValueError("jump out of range in the seccomp program")
            resolved.append(target)
        out.append(_insn(code, resolved[0], resolved[1], k))
    return b"".join(out)


IPPROTO_MPTCP = 262
SOCKET = {AUDIT_ARCH_X86_64: 41, AUDIT_ARCH_AARCH64: 198}


def build_socket_filter(arch: int | None = None) -> bytes:
    """Classic BPF program that refuses ``socket(..., IPPROTO_MPTCP)`` with ``EPERM`` and allows the rest.

    Landlock's TCP rules (connect and bind by port) do not cover MPTCP sockets, which a worker could
    use to reach a port the rules deny. A different syscall ABI of the same CPU kills the process,
    as in ``build_filter``.
    """
    arch = arch if arch is not None else native_audit_arch()
    if arch not in SOCKET:
        raise ValueError("no seccomp filter for audit arch %#x" % (arch or 0))
    program = [
        (_BPF_LD_W_ABS, 0, 0, 4),  # arch
        (_BPF_JEQ_K, 1, 0, arch),
        (_BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, 0),  # nr
    ]
    if arch == AUDIT_ARCH_X86_64:
        program += [
            (_BPF_JSET_K, 0, 1, X32_SYSCALL_BIT),
            (_BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        ]
    program += [
        (_BPF_JEQ_K, 0, 4, SOCKET[arch]),
        (_BPF_LD_W_ABS, 0, 0, _ARG_OFFSET + 8 * 2),  # protocol, low half
        (_BPF_JEQ_K, 0, 2, IPPROTO_MPTCP),
        (_BPF_LD_W_ABS, 0, 0, _ARG_OFFSET + 8 * 2 + 4),  # protocol, high half (a socket protocol is an int)
        (_BPF_JEQ_K, 1, 0, 0),
        (_BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        (_BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPROTONOSUPPORT),
    ]
    return b"".join(_insn(*instruction) for instruction in program)


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _install(program: bytes) -> None:
    buffer = ctypes.create_string_buffer(program, len(program))
    prog = SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    result = libc.prctl(
        ctypes.c_int(PR_SET_SECCOMP),
        ctypes.c_ulong(SECCOMP_MODE_FILTER),
        ctypes.c_ulong(ctypes.addressof(prog)),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, "seccomp filter install failed: %s" % errno.errorcode.get(err, err))


def apply_socket_filter() -> None:
    """Install the MPTCP filter on the calling thread and its future children (``no_new_privs`` set)."""
    if native_audit_arch() is None:
        raise OSError(errno.ENOSYS, "no seccomp filter for this architecture")
    _install(build_socket_filter())


def apply_filter() -> int:
    """Install the filter on the calling thread and its future children.

    ``no_new_privs`` must already be set (it is what allows an unprivileged
    process to install a filter). Returns the number of denied syscalls.
    Raises ``OSError`` on failure; callers that require the filter must not run
    the worker then.
    """
    arch = native_audit_arch()
    if arch is None:
        raise OSError(errno.ENOSYS, "no seccomp filter for this architecture")
    program = build_filter(arch)
    buffer = ctypes.create_string_buffer(program, len(program))
    prog = SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    result = libc.prctl(
        ctypes.c_int(PR_SET_SECCOMP),
        ctypes.c_ulong(SECCOMP_MODE_FILTER),
        ctypes.c_ulong(ctypes.addressof(prog)),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, "seccomp filter install failed: %s" % errno.errorcode.get(err, err))
    return len(set(DENIED_SYSCALLS[arch].values()))

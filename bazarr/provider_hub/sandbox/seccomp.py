# coding=utf-8
"""A seccomp-bpf filter that denies file metadata changes (ctypes, standard library only).

Landlock does not cover ``chmod``, ``chown``, ``utimes`` or extended
attributes. A worker runs as the host's user, so ``os.chmod(config_dir, 0)``
would lock the host out of its own configuration. Seccomp cannot inspect the
path argument of those calls, so they cannot be scoped to the worker's own
directories: every one of them returns ``EPERM``, everywhere.

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

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JSET_K = 0x45
_BPF_RET_K = 0x06

# Same on both architectures.
_COMMON = {
    "fchmodat2": 452,
    "setxattrat": 463,
    "removexattrat": 466,
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
        **_COMMON,
    },
}


def native_audit_arch() -> int | None:
    """The audit arch of this interpreter, or ``None`` when no filter exists for it."""
    if not sys.platform.startswith("linux") or struct.calcsize("P") != 8:
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
    x86-64, one compare per denied number returning ``EPERM``, otherwise allow.
    """
    arch = arch if arch is not None else native_audit_arch()
    if arch not in DENIED_SYSCALLS:
        raise ValueError("no seccomp filter for audit arch %#x" % (arch or 0))
    numbers = sorted(set(DENIED_SYSCALLS[arch].values()))
    x32 = arch == AUDIT_ARCH_X86_64

    head = 4 if x32 else 3  # arch load, arch jump, nr load, [x32 jump]
    allow_at = head + len(numbers)
    deny_at = allow_at + 1
    kill_at = allow_at + 2

    def rel(from_index: int, to_index: int) -> int:
        return to_index - from_index - 1

    program = [
        _insn(_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        _insn(_BPF_JEQ_K, 0, rel(1, kill_at), arch),
        _insn(_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    if x32:
        program.append(_insn(_BPF_JSET_K, rel(3, kill_at), 0, X32_SYSCALL_BIT))
    for offset, number in enumerate(numbers):
        index = head + offset
        program.append(_insn(_BPF_JEQ_K, rel(index, deny_at), 0, number))
    program.append(_insn(_BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    program.append(_insn(_BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM))
    program.append(_insn(_BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS))
    assert len(program) == kill_at + 1
    return b"".join(program)


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


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

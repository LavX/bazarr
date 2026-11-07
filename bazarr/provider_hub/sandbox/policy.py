# coding=utf-8
"""What a sandboxed worker may touch, and what hosts may run it.

Pure data and decisions: nothing here makes a syscall. ``spawn.py`` turns a
``RuntimeSpec`` into a launcher process.
"""
from __future__ import annotations

import json
import os
import re
import stat as stat_module
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# Read-only (with execute) system locations a Python worker needs: the dynamic
# loader, system libraries, the standard library of a system interpreter, the
# CA bundle, name resolution, time zone and locale data. Symlinks (for example
# /etc/resolv.conf on systemd hosts) are followed and their targets added.
#
# TLS trust data is granted by file and by certificate directory, never as the
# whole of /etc/ssl or /etc/pki: those also hold private keys (/etc/ssl/private,
# /etc/pki/tls/private, /etc/pki/CA/private), and a worker running as root can
# read root-only files. Only public certificates and the OpenSSL configuration
# files are listed; a path that does not exist on a host is skipped.
SYSTEM_READ_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/ssl/certs",
    "/etc/ssl/cert.pem",
    "/etc/ssl/openssl.cnf",
    "/etc/ssl/ct_log_list.cnf",
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/pki/tls/certs",
    "/etc/pki/tls/cert.pem",
    "/etc/pki/tls/openssl.cnf",
    "/etc/pki/tls/ct_log_list.cnf",
    "/etc/pki/ca-trust/extracted",
    "/etc/crypto-policies/back-ends",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/host.conf",
    "/etc/gai.conf",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/passwd",
    "/etc/group",
    "/etc/protocols",
    "/etc/services",
    "/run/systemd/resolve",
    "/proc/self",
)

# Device nodes a Python process opens during normal operation.
DEVICE_PATHS: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
)

# Directories whose UNIX sockets name resolution may legitimately use. Only
# consulted at ABI 9 and above, where connecting to a pathname socket outside
# the domain is otherwise denied.
RESOLVE_UNIX_PATHS: tuple[str, ...] = (
    "/run/systemd/resolve",
    "/run/nscd",
    "/var/run/nscd",
)

DNS_TCP_PORT = 53

# Launcher exit codes and stderr marker. launcher.py cannot import this module
# (it runs standalone), so it repeats these literals; a test keeps them equal.
EXIT_BAD_SPEC = 64
EXIT_SANDBOX_FAILED = 78
EXIT_EXEC_FAILED = 127
MESSAGE_PREFIX = "bazarr-sandbox: "

# Largest spec the host will hand to the launcher. It travels in a memfd, so
# the pipe quota does not matter; the cap only keeps a runaway policy out.
MAX_SPEC_BYTES = 256 * 1024


class PolicyError(ValueError):
    """The requested policy is unsafe or malformed."""


class SandboxRefused(RuntimeError):
    """The host state does not allow this worker to run."""


@dataclass(frozen=True)
class Limits:
    """Process limits applied by the launcher before ``execve``.

    ``nproc`` stays ``None`` by default: ``RLIMIT_NPROC`` counts every process
    of the uid (Bazarr+ itself included), and root ignores it, so a process
    count is declared rather than enforced unless the caller opts in.
    """

    core: int = 0
    nofile: int = 1024
    fsize: int = 512 * 1024 * 1024
    nproc: int | None = None
    nice: int = 10
    oom_score_adj: int = 800
    umask: int = 0o077

    def to_dict(self) -> dict:
        return {
            "core": self.core,
            "nofile": self.nofile,
            "fsize": self.fsize,
            "nproc": self.nproc,
            "nice": self.nice,
            "oom_score_adj": self.oom_score_adj,
            "umask": self.umask,
        }


def _abs(path: str | os.PathLike) -> str:
    text = os.fspath(path)
    if not os.path.isabs(text):
        raise PolicyError("sandbox paths must be absolute: %r" % text)
    # Collapsing ".." lexically is wrong across symlinks, so it is not allowed.
    if ".." in re.split(r"[\\/]", text):
        raise PolicyError("sandbox paths cannot contain '..': %r" % text)
    return os.path.normpath(text)


def _is_proc_self(path: str) -> bool:
    return path == "/proc/self" or path.startswith("/proc/self/")


def _canonical(paths: Iterable[str | os.PathLike]) -> tuple[str, ...]:
    """Absolute, symlink-resolved, de-duplicated, order kept (trusted paths only)."""
    seen: dict[str, None] = {}
    for item in paths:
        path = _abs(item)
        seen.setdefault(path if _is_proc_self(path) else os.path.realpath(path), None)
    return tuple(seen)


def _use_descriptors() -> bool:
    """Whether grants can be walked with descriptors, which only Linux can do.

    ``O_PATH`` does not exist on macOS or Windows. There the sandbox is not
    enforced anyway (the launcher is Linux only), so policy construction falls
    back to plain path checks and a declared launch still works. Enforced
    launches are always on Linux and always use the descriptor walk.
    """
    return (
        sys.platform.startswith("linux")
        and hasattr(os, "O_PATH")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


# Called after each directory step of a walk, with the path reached so far.
# Tests use it to swap a directory mid-walk; production leaves it unset.
_walk_hook = None


def _walk_below(root_real: str, parts: Sequence[str], root_lexical: str) -> tuple[int, int, int] | None:
    """Walk ``parts`` below a writable root with descriptors and no symlink following.

    ``root_real`` is the host-resolved writable root, trusted because the
    worker cannot write above it. Every component below it is opened with
    ``openat(O_PATH|O_NOFOLLOW)`` relative to the descriptor of the previous
    one, so a worker that swaps a directory for a link while this runs either
    gets refused or is held to the object the walk already holds. Returns
    ``(st_dev, st_ino, st_mode)`` of the final object read from its own
    descriptor, or ``None`` when something on the way is missing. Where
    descriptors are not available the same checks are made on paths.
    """
    if not _use_descriptors():
        return _walk_by_path(root_real, parts, root_lexical)
    try:
        fd = os.open(root_real, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return None
    reached = root_real
    try:
        for index, part in enumerate(parts):
            try:
                nxt = os.open(part, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except (FileNotFoundError, NotADirectoryError):
                return None
            os.close(fd)
            fd = nxt
            reached = os.path.join(reached, part)
            info = os.fstat(fd)
            if stat_module.S_ISLNK(info.st_mode):
                raise PolicyError("%s is a symlink inside the writable tree %s" % (reached, root_lexical))
            if index < len(parts) - 1 and not stat_module.S_ISDIR(info.st_mode):
                return None
            if _walk_hook is not None:
                _walk_hook(reached)
        info = os.fstat(fd)
        return info.st_dev, info.st_ino, info.st_mode
    finally:
        os.close(fd)


def _walk_by_path(root_real: str, parts: Sequence[str], root_lexical: str) -> tuple[int, int, int] | None:
    """Portable stand-in for ``_walk_below``: no descriptors, so no race protection."""
    current = root_real
    try:
        for part in parts:
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise PolicyError("%s is a symlink inside the writable tree %s" % (current, root_lexical))
        info = os.stat(current)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return info.st_dev, info.st_ino, info.st_mode


def _inside_any(path: str, roots: Iterable[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


MAX_LINK_HOPS = 40


def _trace(path: str, roots: Iterable[str]) -> tuple[str, bool]:
    """Resolve ``path`` one component at a time and say whether a worker could steer it.

    Returns ``(resolved, tainted)``. ``resolved`` is what ``realpath`` would
    give. ``tainted`` is true when any step of the walk touches a writable
    root: a symlink that lives inside one (the worker can repoint it, and put
    it back before the launch), a directory inside one, or a chain too long to
    follow. Every hop is checked where it sits, not only where the chain ends,
    so a trusted alias that passes through a worker-writable link is caught
    even when its final target is far outside the writable tree.
    """
    roots = tuple(roots)
    resolved = "/"
    pending = path.split("/")[::-1]
    tainted = False
    hops = 0
    while pending:
        part = pending.pop()
        if part in ("", "."):
            continue
        if part == "..":
            resolved = os.path.dirname(resolved) or "/"
            continue
        candidate = os.path.join(resolved, part)
        if _inside_any(candidate, roots):
            tainted = True
        try:
            target = os.readlink(candidate)
        except OSError:
            resolved = candidate
            continue
        hops += 1
        if hops > MAX_LINK_HOPS:
            return candidate, True
        if target.startswith("/"):
            resolved = "/"
        pending.extend(target.split("/")[::-1])
    return resolved, tainted


def _resolve_grants(
    items: Iterable[str | os.PathLike],
    writable: Iterable[str | os.PathLike],
    *,
    required: bool = False,
) -> list[tuple[str, tuple[int, int], int]]:
    """Resolve grants to ``(path, (dev, ino), mode)``; grants that do not exist are left out.

    A grant inside a writable tree is walked with descriptors (see
    ``_walk_below``) and must contain no symlink below the tree's root. Links
    above the root, such as a ``/config`` that points at another disk, are the
    host's own business. A grant outside every writable tree is resolved the
    ordinary way, but it may not resolve into one, nor pass through one on the
    way (a link the worker could repoint and restore): that would be a link
    reaching in. ``/proc/self`` is skipped; the launcher's child resolves it.
    """
    roots = [os.path.normpath(_abs(g)) for g in writable]
    real_roots = [os.path.realpath(r) for r in roots]
    result: list[tuple[str, tuple[int, int], int]] = []
    seen: set[str] = set()
    for item in items:
        lexical = _abs(item)
        if _is_proc_self(lexical):
            continue
        walked = None
        for root, real_root in zip(roots, real_roots):
            if lexical == root or lexical.startswith(root.rstrip("/") + "/"):
                parts = Path(lexical).relative_to(root).parts
                walked = (real_root, parts, root)
                break
        if walked is not None:
            real_root, parts, root = walked
            found = _walk_below(real_root, parts, root)
            if found is None:
                if required:
                    raise PolicyError("read-write path does not exist: %s" % lexical)
                continue
            path = os.path.join(real_root, *parts)
            dev, ino, mode = found
        else:
            path, tainted = _trace(lexical, (*roots, *real_roots))
            if tainted:
                raise PolicyError("%s reaches a writable tree through a symlink" % lexical)
            try:
                info = os.stat(path)
            except OSError:
                if required:
                    raise PolicyError("read-write path does not exist: %s" % lexical) from None
                continue
            dev, ino, mode = info.st_dev, info.st_ino, info.st_mode
        if path not in seen:
            seen.add(path)
            result.append((path, (dev, ino), mode))
    return result


def _check_writable_roots(items: Iterable[str | os.PathLike]) -> tuple[str, ...]:
    """Vet the writable grants as given, before anything is resolved.

    Resolution trusts the first root a path falls under, so two roots that
    overlap as written (``root/sub`` and ``root``, in either order) would let
    a worker repoint ``sub`` and have the next build grant the new target. So
    no root may sit inside another one lexically, and no root's own symlink
    chain may pass through another root, by its written or its resolved form:
    that is a link a worker can repoint. Returns the normalized roots with
    exact repeats dropped.
    """
    roots = tuple(dict.fromkeys(os.path.normpath(_abs(g)) for g in items))
    for outer in roots:
        for inner in roots:
            if inner != outer and inner.startswith(outer.rstrip("/") + "/"):
                raise PolicyError("writable grant %s is nested inside writable grant %s" % (inner, outer))
    for root in roots:
        others: list[str] = []
        for other in roots:
            if other != root:
                others.extend((other, os.path.realpath(other)))
        if others and _trace(root, others)[1]:
            raise PolicyError("writable grant %s reaches another writable grant through a symlink" % root)
    return roots


def interpreter_read_paths(
    interpreter: str | os.PathLike,
    writable: Iterable[str | os.PathLike] = (),
) -> tuple[str, ...]:
    """Directories a worker interpreter needs to start and import its stdlib.

    A virtual environment root, the prefix its ``pyvenv.cfg`` points at, and the
    prefix of the resolved interpreter. A prefix of ``/`` is never granted.

    A worker can rewrite anything inside a writable grant: a ``pyvenv.cfg`` or
    an interpreter symlink there could name any directory the host user can
    read, and the next policy build would grant it. So an interpreter whose
    symlink chain touches a writable grant at any hop (not only at its end:
    a trusted alias may pass through a link the worker can repoint and later
    restore), and a ``pyvenv.cfg`` reached the same way, is never consulted.
    The base interpreter's prefixes then come from the host's own
    ``sys.base_prefix`` and ``sys.base_exec_prefix``, which a worker cannot
    touch.
    """
    roots: list[str] = []
    for item in writable:
        lexical = os.path.normpath(_abs(item))
        roots.extend((lexical, os.path.realpath(lexical)))
    exe = Path(_abs(interpreter))
    real_exe, steerable = _trace(str(exe), roots)
    found: list[Path] = []
    if steerable:
        # Some hop of the chain, or the interpreter itself, sits where a worker
        # can rewrite it. Only the host's own prefixes are trustworthy then, plus
        # the venv root when it lies inside a writable grant (the walk of that
        # grant vets it).
        if _inside_any(str(exe), roots):
            found.append(exe.parent.parent)
        found.extend(Path(p) for p in (sys.base_prefix, sys.base_exec_prefix))
        candidates: list[Path] = []
    else:
        found.append(exe.parent.parent)
        candidates = [exe, Path(real_exe)]
    for candidate in candidates:
        prefix = candidate.parent.parent
        found.append(prefix)
        cfg = prefix / "pyvenv.cfg"
        if _trace(str(cfg), roots)[1]:
            continue
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    found.append(Path(value.strip()).parent)
        except OSError:
            pass
    return tuple(dict.fromkeys(str(p) for p in found if str(p) != "/" and p.is_absolute()))


@dataclass(frozen=True)
class SandboxPolicy:
    """Paths and ports a worker may use.

    Build one with ``for_interpreter`` right before starting the worker: it
    records the device and inode of every granted object, and the launcher
    refuses to start (exit 78) if a grant is by then a symlink or a different
    object, which is what a worker that can write inside its own tree would
    need in order to widen its next launch. A policy that outlives a directory
    being deleted and recreated must be rebuilt.
    """

    read_only: tuple[str, ...]
    read_write: tuple[str, ...]
    devices: tuple[str, ...] = DEVICE_PATHS
    resolve_unix: tuple[str, ...] = RESOLVE_UNIX_PATHS
    # None: TCP unrestricted (declared). A set: connect only to these ports and
    # bind nothing, where the kernel supports it (ABI 4 and above).
    connect_ports: frozenset[int] | None = None
    # Highest Landlock ABI to use, for diagnostics and compatibility pinning.
    abi_cap: int | None = None
    # (device, inode) of each grant when the policy was built. A grant without
    # an entry is checked against its own resolved path at launch instead.
    identities: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def for_interpreter(
        cls,
        interpreter: str | os.PathLike,
        *,
        read_only: Iterable[str | os.PathLike] = (),
        read_write: Iterable[str | os.PathLike] = (),
        connect_ports: Iterable[int] | None = None,
        reserved_ports: Iterable[int] = (),
        protected_paths: Iterable[str | os.PathLike] = (),
        include_dns_tcp: bool = True,
        abi_cap: int | None = None,
    ) -> "SandboxPolicy":
        """Build and validate a policy.

        ``protected_paths`` (``config.yaml``, the database, and so on) must not
        sit inside any granted tree: Landlock cannot carve a hole out of a
        granted directory, so an overlap is refused up front.
        ``reserved_ports`` (Bazarr+'s own port, a front's port) can never be in
        the connect set. Read-write paths must exist and must not nest inside
        one another.
        """
        ports: frozenset[int] | None = None
        if connect_ports is not None:
            values = {int(p) for p in connect_ports}
            if include_dns_tcp:
                values.add(DNS_TCP_PORT)
            for port in values:
                if not 1 <= port <= 65535:
                    raise PolicyError("port out of range: %d" % port)
            clash = values & {int(p) for p in reserved_ports}
            if clash:
                raise PolicyError("reserved ports cannot be allowed: %s" % sorted(clash))
            ports = frozenset(values)
        read_only = tuple(read_only)
        read_write = _check_writable_roots(read_write)
        rw_items = _resolve_grants(read_write, read_write, required=True)
        for path, _ident, mode in rw_items:
            if not stat_module.S_ISDIR(mode):
                raise PolicyError("read-write path is not an existing directory: %s" % path)
        ro_items = _resolve_grants((*SYSTEM_READ_PATHS, *interpreter_read_paths(interpreter, read_write), *read_only), read_write)
        device_items = _resolve_grants(DEVICE_PATHS, read_write)
        unix_items = _resolve_grants(RESOLVE_UNIX_PATHS, read_write)
        every = [*ro_items, *rw_items, *device_items, *unix_items]
        policy = cls(
            read_only=(*(p for p, _i, _m in ro_items), "/proc/self"),
            read_write=tuple(p for p, _i, _m in rw_items),
            devices=tuple(p for p, _i, _m in device_items),
            resolve_unix=tuple(p for p, _i, _m in unix_items),
            connect_ports=ports,
            abi_cap=abi_cap,
            identities={p: ident for p, ident, _m in every},
        )
        policy.validate(protected_paths)
        return policy

    def validate(self, protected_paths: Iterable[str | os.PathLike] = ()) -> None:
        granted = (*self.read_only, *self.read_write)
        if "/" in granted:
            raise PolicyError("the filesystem root cannot be granted")
        for item in protected_paths:
            forms = {_abs(item), os.path.realpath(_abs(item))}
            for form in forms:
                for root in granted:
                    if form == root or form.startswith(root.rstrip("/") + "/"):
                        raise PolicyError("%s lies inside the granted tree %s" % (form, root))
        for outer in self.read_write:
            for inner in self.read_write:
                if inner != outer and inner.startswith(outer.rstrip("/") + "/"):
                    raise PolicyError("writable grant %s is nested inside writable grant %s" % (inner, outer))
        # No system path may be writable, whether granted itself or reached
        # through a larger writable tree such as /etc.
        for system in _canonical(SYSTEM_READ_PATHS):
            for writable in self.read_write:
                if system == writable or system.startswith(writable.rstrip("/") + "/"):
                    raise PolicyError("system path %s would become writable through %s" % (system, writable))

    def grant(self, path: str) -> str | list:
        """A path for the launch spec, with its recorded identity when it has one."""
        identity = self.identities.get(path)
        return [path, identity[0], identity[1]] if identity else path


@dataclass(frozen=True)
class RuntimeSpec:
    """Everything the launcher needs to start one worker."""

    interpreter: str
    args: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    policy: SandboxPolicy
    limits: Limits = field(default_factory=Limits)
    term_grace: float = 3.0

    def to_launch_json(self, *, enforce: bool, min_abi: int) -> bytes:
        payload = {
            "interpreter": os.fspath(self.interpreter),
            "args": [str(a) for a in self.args],
            "cwd": os.fspath(self.cwd),
            "env": dict(self.env),
            "mode": "enforce" if enforce else "declared",
            "min_abi": int(min_abi),
            "abi_cap": self.policy.abi_cap,
            "read_only": [self.policy.grant(p) for p in self.policy.read_only],
            "read_write": [self.policy.grant(p) for p in self.policy.read_write],
            "devices": [self.policy.grant(p) for p in self.policy.devices],
            "resolve_unix": [self.policy.grant(p) for p in self.policy.resolve_unix],
            "connect_ports": None if self.policy.connect_ports is None else sorted(self.policy.connect_ports),
            "limits": self.limits.to_dict(),
            "term_grace": float(self.term_grace),
        }
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_SPEC_BYTES:
            raise PolicyError("launch spec is %d bytes, over the %d limit" % (len(data), MAX_SPEC_BYTES))
        return data


DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def clean_env(
    *,
    home: str | os.PathLike | None = None,
    tmpdir: str | os.PathLike | None = None,
    path_prepend: Sequence[str | os.PathLike] = (),
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A worker environment built from nothing.

    The host environment is never inherited, so tokens, proxy credentials and
    config paths do not reach the worker. ``extra`` is merged last and must be
    strings; per-request secrets belong in the protocol, not here.
    """
    env = {
        "PATH": ":".join([*(os.fspath(p) for p in path_prepend), DEFAULT_PATH]),
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if home is not None:
        env["HOME"] = os.fspath(home)
    if tmpdir is not None:
        env["TMPDIR"] = os.fspath(tmpdir)
    for key, value in (extra or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or "=" in key or "\0" in key + value:
            raise PolicyError("environment entries must be plain strings: %r" % (key,))
        env[key] = value
    return env


@dataclass(frozen=True)
class Admission:
    allowed: bool
    sandboxed: bool
    # Shown to the user when allowed is False.
    reason: str | None = None
    # True: warn when installing, because nothing confines this plugin here.
    warn_at_install: bool = False


def admission(
    *,
    sandbox_enforced: bool,
    official: bool,
    interactive_login: bool = False,
    strict: bool = False,
) -> Admission:
    """Decide whether a Python worker may run on this host.

    No Landlock: Python workers still run (that is today's behavior, and
    Synology and QNAP users depend on it) and are marked not sandboxed, but
    ``interactive_login`` workers are refused, and in strict mode so are all
    non-official plugins. Installing a non-official plugin on such a host
    warns even with strict mode off. The UI must never show the not-sandboxed
    state as protection.
    """
    if sandbox_enforced:
        return Admission(allowed=True, sandboxed=True)
    if interactive_login:
        return Admission(
            allowed=False,
            sandboxed=False,
            reason="Interactive login plugins need a sandbox, and this host has none.",
        )
    if strict and not official:
        return Admission(
            allowed=False,
            sandboxed=False,
            reason="Strict mode is on and this host cannot sandbox non-official plugins.",
        )
    return Admission(allowed=True, sandboxed=False, warn_at_install=not official)

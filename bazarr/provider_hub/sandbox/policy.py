# coding=utf-8
"""What a sandboxed worker may touch, and what hosts may run it.

Pure data and decisions: nothing here makes a syscall. ``spawn.py`` turns a
``RuntimeSpec`` into a launcher process.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# Read-only (with execute) system locations a Python worker needs: the dynamic
# loader, system libraries, the standard library of a system interpreter, the
# CA bundle, name resolution, time zone and locale data. Symlinks (for example
# /etc/resolv.conf on systemd hosts) are followed and their targets added.
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
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
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
    return os.path.normpath(text)


def _is_proc_self(path: str) -> bool:
    return path == "/proc/self" or path.startswith("/proc/self/")


def _canonical(paths: Iterable[str | os.PathLike]) -> tuple[str, ...]:
    """Absolute, symlink-resolved, de-duplicated, order kept.

    Only the resolved form is kept, never the symlink that led to it: the
    launcher opens every grant without following symlinks and compares it with
    what was resolved here. ``/proc/self`` stays symbolic, because the launcher's
    child resolves it and resolving it here would grant the host's own entry.
    """
    seen: dict[str, None] = {}
    for item in paths:
        path = _abs(item)
        seen.setdefault(path if _is_proc_self(path) else os.path.realpath(path), None)
    return tuple(seen)


def _existing(paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(p for p in paths if _is_proc_self(p) or os.path.lexists(p))


def _identities(paths: Iterable[str]) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for path in paths:
        if _is_proc_self(path):
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        result[path] = (info.st_dev, info.st_ino)
    return result


def _reject_links_in_writable_trees(given: Iterable[str | os.PathLike], writable_given: Iterable[str | os.PathLike]) -> None:
    """Refuse grants a worker could redirect by swapping a link into its own tree.

    A grant that resolves into a writable tree must be spelled as a path under
    that tree with no symlink below the tree's root. Links above the root (a
    ``/config`` that points at another disk) are the host's business and fine.
    """
    roots = [os.path.normpath(_abs(g)) for g in writable_given]
    real_roots = [os.path.realpath(r) for r in roots]
    for item in given:
        lexical = os.path.normpath(_abs(item))
        if _is_proc_self(lexical):
            continue
        real = os.path.realpath(lexical)
        for root, real_root in zip(roots, real_roots):
            if lexical == root or lexical.startswith(root.rstrip("/") + "/"):
                current = root
                for part in Path(lexical).relative_to(root).parts:
                    current = os.path.join(current, part)
                    if os.path.islink(current):
                        raise PolicyError("%s is a symlink inside the writable tree %s" % (current, root))
            elif real == real_root or real.startswith(real_root.rstrip("/") + "/"):
                raise PolicyError("%s reaches the writable tree %s through a symlink" % (lexical, root))


def interpreter_read_paths(interpreter: str | os.PathLike) -> tuple[str, ...]:
    """Directories a worker interpreter needs to start and import its stdlib.

    A virtual environment root, the prefix its ``pyvenv.cfg`` points at, and the
    prefix of the resolved interpreter. A prefix of ``/`` is never granted.
    """
    exe = Path(_abs(interpreter))
    found: list[Path] = []
    for candidate in (exe, Path(os.path.realpath(exe))):
        prefix = candidate.parent.parent
        found.append(prefix)
        cfg = prefix / "pyvenv.cfg"
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip().lower() == "home" and value.strip():
                    found.append(Path(value.strip()).parent)
        except OSError:
            pass
    return tuple(str(p) for p in found if str(p) != "/" and p.is_absolute())


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
        read_write = tuple(read_write)
        _reject_links_in_writable_trees((*read_only, *read_write), read_write)
        rw = _canonical(read_write)
        for path in rw:
            if not os.path.isdir(path):
                raise PolicyError("read-write path is not an existing directory: %s" % path)
        ro = _existing(_canonical((*SYSTEM_READ_PATHS, *interpreter_read_paths(interpreter), *read_only)))
        devices = _existing(_canonical(DEVICE_PATHS))
        resolve_unix = _existing(_canonical(RESOLVE_UNIX_PATHS))
        policy = cls(
            read_only=ro,
            read_write=rw,
            devices=devices,
            resolve_unix=resolve_unix,
            connect_ports=ports,
            abi_cap=abi_cap,
            identities=_identities((*ro, *rw, *devices, *resolve_unix)),
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

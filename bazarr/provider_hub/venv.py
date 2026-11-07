# coding=utf-8
from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import stat
import subprocess
import sys
import sysconfig
import threading
import time
import uuid

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from .manifest import ValidatedManifest


# Serialize venv creation process-wide. A large catalog update can fan many provider
# installs across the jobs queue / Flask threads at once; each builds a venv (python -m
# venv + pip install), and a concurrent burst can exceed a container memory limit and trip
# the OOM killer. Holding this lock around the heavy subprocess work caps peak memory to a
# single venv build at a time. Builds are idempotent and cached, so serializing only slows
# the rare bulk-install burst, not normal operation.
_INSTALL_LOCK = threading.Lock()


# Guards the in-flight pins below. An install that reuses or builds a shared
# environment holds a pin on it until its row is recorded, so a cleanup that
# runs on another thread never removes an environment that is about to be used.
_ENV_LOCK = threading.RLock()
_PINS: Counter[str] = Counter()
_SCOPE = threading.local()

SHARED_ENV_DIR = "shared_envs"

# Written into an environment once its packages are installed and consistent. An
# environment no record names is only reused when it carries this file, so the
# leftovers of an interrupted build are never mistaken for a finished one.
COMPLETE_MARKER = ".bazarr-env-complete"

# One lock per fingerprint. It covers choosing the directory for a dependency
# set, building it and the consistency check that follows, so two installs with
# the same dependencies never both build into the same directory.
_FINGERPRINT_LOCKS: dict[str, threading.Lock] = {}
_FINGERPRINT_LOCKS_GUARD = threading.Lock()


def _fingerprint_lock(fingerprint: str) -> threading.Lock:
    with _FINGERPRINT_LOCKS_GUARD:
        return _FINGERPRINT_LOCKS.setdefault(fingerprint, threading.Lock())


class PluginEnvironmentError(RuntimeError):
    """Raised when a Provider Hub environment cannot be built."""


@contextlib.contextmanager
def pin_scope() -> Iterator[None]:
    """Pin every environment ``PluginEnvironment.install`` hands out on this thread.

    The pins are released when the scope ends, which is after the installation
    that uses the environment has been recorded or has failed.
    """
    held: list[str] = []
    previous = getattr(_SCOPE, "held", None)
    _SCOPE.held = held
    try:
        yield
    finally:
        _SCOPE.held = previous
        with _ENV_LOCK:
            for key in held:
                _PINS[key] -= 1
                if _PINS[key] <= 0:
                    del _PINS[key]
            held.clear()


def path_key(path: str | os.PathLike[str]) -> str:
    """One spelling of a directory, so a symlinked or unnormalized path compares equal."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(Path(path))


def _pin(env_path: Path) -> None:
    held = getattr(_SCOPE, "held", None)
    if held is None:
        return
    with _ENV_LOCK:
        key = path_key(env_path)
        _PINS[key] += 1
        held.append(key)


def release_pins() -> None:
    """End this thread's holds early, for a failed or stopped install about to
    decide what to remove. Other threads' holds stay."""
    held = getattr(_SCOPE, "held", None)
    if not held:
        return
    with _ENV_LOCK:
        for key in held:
            _PINS[key] -= 1
            if _PINS[key] <= 0:
                del _PINS[key]
        held.clear()


def is_pinned(env_path: str | os.PathLike[str]) -> bool:
    with _ENV_LOCK:
        return _PINS.get(path_key(env_path), 0) > 0


def hold_environment(env_path: str | os.PathLike[str]):
    """Pin an environment until the returned callable is called, from any thread.

    For workers that outlive the install scope that started them: a cleanup
    leaves the environment alone while any of them may still run from it. The
    release is safe to call more than once.
    """
    key = path_key(env_path)
    with _ENV_LOCK:
        _PINS[key] += 1
    released = threading.Event()

    def release() -> None:
        with _ENV_LOCK:
            if released.is_set():
                return
            released.set()
            _PINS[key] -= 1
            if _PINS[key] <= 0:
                del _PINS[key]

    return release


def pinned_paths() -> set[str]:
    """Every environment an install in flight holds right now."""
    with _ENV_LOCK:
        return {key for key, count in _PINS.items() if count > 0}


def environment_fingerprint(manifest: ValidatedManifest) -> str:
    """The key an environment is shared under.

    It covers the dependency set and what a compiled wheel depends on, the
    platform, the machine architecture and the interpreter's ABI (its cache tag
    and SOABI, which tells a free-threaded build from a regular one), so a config
    directory moved to another architecture never reuses an environment built
    for the first. It does not cover the provider id or the version, so plugins with the same dependencies resolve to one key. The
    requirement lines are sorted, so their order does not matter.
    """
    digest = hashlib.sha256()
    for line in sorted({requirement.pip_line for requirement in manifest.dependency_requirements}):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    digest.update(sys.platform.encode("utf-8"))
    digest.update((sys.implementation.cache_tag or "").encode("utf-8"))
    digest.update((sysconfig.get_config_var("SOABI") or "").encode("utf-8"))
    digest.update(platform.machine().lower().encode("utf-8"))
    return digest.hexdigest()[:16]


def python_executable(env_path: str | os.PathLike[str]) -> Path:
    return Path(env_path) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


_PIP_CHECK_LIMIT = 60.0


def _passes_pip_check(env_path: Path, deadline: float | None = None, provider_id: str = "") -> bool:
    """Whether the environment's installed packages are consistent. Local only.

    ``deadline`` is the install's absolute ``time.monotonic()`` budget. The check
    never runs past it: an exhausted budget raises, and a check cut short by the
    budget raises too instead of reading as a failed check, which would send the
    caller on to a build it has no time for.
    """
    limit = _PIP_CHECK_LIMIT
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise PluginEnvironmentError(f"Provider Hub install for {provider_id} exceeded its time budget")
        limit = min(limit, left)
    try:
        subprocess.run(
            [str(python_executable(env_path)), "-m", "pip", "check"],
            check=True,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
            cwd=str(env_path),
            timeout=limit,
        )
    except subprocess.TimeoutExpired as error:
        if deadline is not None and time.monotonic() >= deadline:
            raise PluginEnvironmentError(f"Provider Hub install for {provider_id} exceeded its time budget") from error
        return False
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def has_parent_reference(path: str | os.PathLike[str]) -> bool:
    """Whether the path, as written, has a ``..`` component.

    Checked on the raw spelling, before any normalisation: collapsing ``a/link/..``
    to ``a`` hides the directory the operating system will actually step into.
    """
    return any(part == ".." for part in Path(path).parts)


BUNDLE_DIR = "bundles"


def _validated_below(root, path, containers: tuple[str, ...], minimum_parts: int) -> Path | None:
    if has_parent_reference(path):
        return None
    candidate = Path(os.path.abspath(path))
    hub = Path(path_key(root))
    for base in (Path(os.path.abspath(root)), hub):
        try:
            parts = candidate.relative_to(base).parts
        except ValueError:
            continue
        break
    else:
        return None
    if len(parts) < minimum_parts or parts[0] not in containers:
        return None
    current = hub
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            return None
    return candidate


def validated_hub_path(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> Path | None:
    """``path`` as an absolute path, if it is below the hub's own ``shared_envs`` or ``envs`` by real directories only.

    The path that comes back is the one to use: the check and whoever runs
    something from it never look at different spellings. A ``..`` anywhere in the
    raw path refuses it. The comparison is against the hub itself, not against
    whatever the container directories resolve to, and a symlink anywhere on the
    way down fails it: a ``shared_envs`` that points outside the hub must not make
    outside paths look like the hub's own.
    """
    return _validated_below(root, path, (SHARED_ENV_DIR, "envs"), 2)


def validated_bundle_path(root: str | os.PathLike[str], path: str | os.PathLike[str] | None) -> Path | None:
    """A plugin bundle directory from the state, if it is below the hub's ``bundles`` area by real directories only.

    The same rules as for environments (no ``..``, no symlinked component), and
    the path returned is the one to use as a working directory, to verify, or to
    clean.
    """
    if not path:
        return None
    return _validated_below(root, path, (BUNDLE_DIR,), 2)


def _read_nul_separated(path: str) -> list[str]:
    with open(path, "rb") as handle:
        return [item.decode("utf-8", "replace") for item in handle.read().split(b"\0") if item]


def processes_using_directory(directory: str | os.PathLike[str], proc: str = "/proc") -> bool | None:
    """Whether any other live process runs from, has open, or maps something under ``directory``.

    Looks at each process's executable, working directory, open files, mapped
    files, command line and environment (Linux ``/proc``). This sees what the worker's process groups
    cannot: a child that started its own session is in none of them. A process
    that cannot be read but belongs to this user and began after this process
    counts as using it. ``None``
    when ``/proc`` is not there to look at, so the caller keeps its other
    safeguards.
    """
    try:
        entries = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return None
    roots = {os.path.abspath(directory), os.path.realpath(directory)}
    prefixes = tuple(root.rstrip(os.sep) + os.sep for root in roots)

    def under(path: str) -> bool:
        path = path.removesuffix(" (deleted)")
        return path in roots or path.startswith(prefixes)

    def started_at(pid: str) -> int | None:
        """The process's start time in clock ticks since boot, readable even when its other files are not."""
        try:
            with open(os.path.join(proc, pid, "stat"), encoding="utf-8", errors="replace") as handle:
                return int(handle.read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return None

    mine = os.getpid()
    my_start = started_at(str(mine))
    uid = os.getuid() if hasattr(os, "getuid") else None
    for name in entries:
        if int(name) == mine:
            continue
        base = os.path.join(proc, name)
        try:
            owned = uid is not None and os.stat(base).st_uid == uid
            for link in ("exe", "cwd"):
                if under(os.readlink(os.path.join(base, link))):
                    return True
            for descriptor in os.listdir(os.path.join(base, "fd")):
                try:
                    if under(os.readlink(os.path.join(base, "fd", descriptor))):
                        return True
                except OSError:
                    continue
            with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as maps:
                for line in maps:
                    fields = line.rstrip("\n").split(None, 5)
                    if len(fields) == 6 and under(fields[5]):
                        return True
            # A symlinked venv's python is the host's python as far as the files
            # above go, so look at how it was started as well: the path it was
            # run by, and the environment it carries.
            if any(under(argument) for argument in _read_nul_separated(os.path.join(base, "cmdline"))):
                return True
            for entry in _read_nul_separated(os.path.join(base, "environ")):
                key, _, value = entry.partition("=")
                if key in ("VIRTUAL_ENV", "PYTHONHOME") and under(value):
                    return True
                if key in ("PATH", "PYTHONPATH") and any(under(item) for item in value.split(os.pathsep) if item):
                    return True
        except PermissionError:
            # Unreadable, but ours: it may be something a worker started. Only a
            # process that began after this one can be (the desktop session, the
            # agents and the like that were already running cannot be).
            began = started_at(name)
            if owned and (began is None or my_start is None or began >= my_start):
                return True
        except OSError:
            continue  # the process ended while it was being looked at
    return False


def path_inside_hub(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> bool:
    return validated_hub_path(root, path) is not None


def _link_target_trusted(env_dir: Path, python_path: Path) -> bool:
    """Follow the interpreter's symlinks hop by hop.

    Every hop must be named inside the environment, or lead into the host's own
    Python tree (the ``python -> python3.x -> /usr/bin/python3.x`` of a normal
    venv; the host tree may itself be reached through directory links such as
    ``/bin``). A hop that detours through anywhere else is refused even if the
    chain comes back, because that place can be repointed after the check.
    """
    env_roots = {Path(os.path.realpath(env_dir)), Path(os.path.abspath(env_dir))}
    host_roots = set()
    for prefix in {sys.base_prefix, sys.base_exec_prefix}:
        if prefix:
            host_roots.add(Path(os.path.realpath(prefix)))
            host_roots.add(Path(os.path.abspath(prefix)))

    def trusted(path: Path) -> bool:
        if any(path.is_relative_to(root) for root in env_roots | host_roots):
            return True
        real = Path(os.path.realpath(path))
        return any(real.is_relative_to(root) for root in host_roots)

    def real_below_the_environment(path: Path) -> bool:
        """No directory between the environment and ``path`` is a symlink (the last component is the hop itself)."""
        for root in env_roots:
            if path.is_relative_to(root):
                parts = path.relative_to(root).parts
                break
        else:
            return True  # in the host tree, which is trusted as a whole
        current = root
        for part in parts[:-1]:
            current = current / part
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    return False
            except FileNotFoundError:
                return True
            except OSError:
                return False
        return True

    current = Path(os.path.abspath(python_path))
    for _ in range(40):
        # A directory link inside the environment would carry a hop outside
        # while every name on the way still reads as the environment's own.
        if not real_below_the_environment(current):
            return False
        if not os.path.islink(current):
            return True
        link = os.readlink(current)
        if has_parent_reference(link):
            return False
        target = Path(os.path.abspath(os.path.join(os.path.dirname(current), link)))
        if not trusted(target):
            return False
        current = target
    return False


def validated_interpreter(root: str | os.PathLike[str], python_path: str | os.PathLike[str]) -> Path | None:
    """The interpreter path to run, if it is one the hub built and may run; otherwise None.

    The environment directory must be the hub's own (see ``validated_hub_path``),
    its ``bin`` or ``Scripts`` directory must be a real directory inside it, not
    a symlink, and the interpreter must be a file there, or a symlink that stays
    inside the environment or leads to the host's own Python tree, as the
    ``python -> python3.x`` links of a normal venv do. No ``..`` may appear in the
    path or in any link on the way. Whatever runs the interpreter uses the path
    returned here, never the string it was given.
    """
    if has_parent_reference(python_path):
        return None
    interpreter = Path(os.path.abspath(python_path))
    env_dir = interpreter.parent.parent
    if validated_hub_path(root, env_dir) is None:
        return None
    try:
        if not stat.S_ISDIR(os.lstat(interpreter.parent).st_mode):
            return None
    except FileNotFoundError:
        # Nothing there to run; whoever uses the path finds that out itself.
        return interpreter
    except OSError:
        return None
    return interpreter if _link_target_trusted(env_dir, interpreter) else None


def interpreter_inside_hub(root: str | os.PathLike[str], python_path: str | os.PathLike[str]) -> bool:
    return validated_interpreter(root, python_path) is not None


class PluginEnvironment:
    """Build and validate an isolated venv for a set of Provider Hub dependencies.

    Plugins whose dependency sets are identical share one environment. A shared
    environment is never changed for one plugin: a different dependency set has a
    different fingerprint and gets its own environment.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    def _fingerprint(self, manifest: ValidatedManifest) -> str:
        return environment_fingerprint(manifest)

    def path_for(self, manifest: ValidatedManifest) -> Path:
        return self.root / SHARED_ENV_DIR / self._fingerprint(manifest)

    def _inside_hub(self, path: Path) -> bool:
        return path_inside_hub(self.root, path)

    def _recorded_path(self, manifest: ValidatedManifest) -> Path | None:
        """The environment recorded for this dependency set, whether or not it is intact.

        A recorded path outside the hub's own environment directories is ignored,
        so a hand-edited state document cannot make an install build somewhere else.
        """
        from .state import load_state

        entry = (load_state().get("plugin_envs") or {}).get(self._fingerprint(manifest))
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return None
        return validated_hub_path(self.root, entry["path"])

    def _fresh_path(self, manifest: ValidatedManifest) -> Path:
        return self.root / SHARED_ENV_DIR / f"{self._fingerprint(manifest)}-{uuid.uuid4().hex[:8]}"

    def install(self, manifest: ValidatedManifest, timeout: float | None = None, *, rebuild: bool = False) -> Path:
        """Return the venv for this dependency set, building it only when needed.

        An environment is immutable once built: nothing is ever installed into a
        directory that already holds one, and nothing is built over it. A recorded
        environment that is intact is reused as it is, with no install and no
        network. When the recorded one fails ``pip check``, has lost its
        interpreter, or ``rebuild`` is set, a new directory is built and the
        caller records it once it passes the worker check; the old one stays for
        a cleanup to remove when nothing references it. With no record, a
        finished environment already sitting in the canonical directory (a
        concurrent install of the same dependencies built it) is reused too.

        The choice of directory and the build happen under a lock per
        fingerprint, so two installs of one dependency set never build into the
        same directory. When ``timeout`` is set it is a hard wall-clock budget for
        the whole install (shared across the lock waits and the venv/pip
        subprocesses): the startup auto-install passes its remaining budget so
        one stuck pip/venv step can't block boot indefinitely. ``None`` (manual
        installs) leaves it unbounded.
        """
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        lock = _fingerprint_lock(self._fingerprint(manifest))
        if deadline is None:
            lock.acquire()
        elif not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise PluginEnvironmentError(
                f"Provider Hub install for {manifest.provider_id} timed out waiting for another install of the same dependencies"
            )
        try:
            return self._install_locked(manifest, deadline, rebuild)
        finally:
            lock.release()

    def _install_locked(self, manifest: ValidatedManifest, deadline: float | None, rebuild: bool) -> Path:
        canonical = self.path_for(manifest)
        # Read the record, look at the canonical directory and pin in one step
        # under the lock a cleanup holds while it decides, so the environment
        # cannot vanish in between.
        with _ENV_LOCK:
            reusable = self._recorded_path(manifest)
            if reusable is None and self._inside_hub(canonical) and self._is_complete(canonical):
                reusable = canonical
            if reusable is not None and python_executable(reusable).exists():
                # An interpreter that leads outside the hub is never reused or run for the check.
                checked = validated_interpreter(self.root, python_executable(reusable))
                reusable = checked.parent.parent if checked is not None else None
            if reusable is not None:
                _pin(reusable)
            canonical_free = not os.path.lexists(canonical)
        if rebuild:
            return self._build(manifest, deadline, self._fresh_path(manifest))
        if reusable is None:
            return self._build(manifest, deadline, canonical if canonical_free else self._fresh_path(manifest))
        if python_executable(reusable).exists() and _passes_pip_check(reusable, deadline, manifest.provider_id):
            return reusable
        return self._build(manifest, deadline, self._fresh_path(manifest))

    @staticmethod
    def _is_complete(env_path: Path) -> bool:
        return python_executable(env_path).exists() and (env_path / COMPLETE_MARKER).is_file()

    def _build(self, manifest: ValidatedManifest, deadline: float | None, env_path: Path) -> Path:
        """Build ``env_path``. ``deadline`` is the install's absolute ``time.monotonic()`` budget."""

        def _remaining() -> float | None:
            if deadline is None:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise PluginEnvironmentError(
                    f"Provider Hub install for {manifest.provider_id} exceeded its time budget"
                )
            return left

        if not self._inside_hub(env_path):
            raise PluginEnvironmentError(f"Provider Hub will not build an environment outside its own directory: {env_path}")
        with _ENV_LOCK:
            _pin(env_path)
        # A directory is only ever built once. One that is already there belongs
        # to an environment something may be running from.
        try:
            env_path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise PluginEnvironmentError(
                f"Provider Hub environment directory already exists: {env_path}"
            ) from error

        python_exe = python_executable(env_path)
        # Serialize the heavy venv/pip subprocess work process-wide (see _INSTALL_LOCK).
        # Bound the wait by the remaining budget so a timed (startup) install can't block
        # forever behind a long-running unbounded (manual) install holding the lock.
        lock_wait = _remaining()
        if lock_wait is None:
            _INSTALL_LOCK.acquire()
        elif not _INSTALL_LOCK.acquire(timeout=lock_wait):
            raise PluginEnvironmentError(
                f"Provider Hub install for {manifest.provider_id} timed out waiting for the install lock"
            )
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(env_path)],
                check=True,
                timeout=_remaining(),
            )

            if manifest.dependency_requirements:
                requirements_path = env_path / "requirements.txt"
                requirements_path.write_text(
                    "\n".join(requirement.pip_line for requirement in manifest.dependency_requirements) + "\n",
                    encoding="utf-8",
                )
                cmd = [
                    str(python_exe),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-warn-script-location",
                    "--require-hashes",
                    "--prefer-binary",
                    "-r",
                    str(requirements_path),
                ]

                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONNOUSERSITE": "1",
                }
                subprocess.run(cmd, check=True, env=env, cwd=str(env_path), timeout=_remaining())

            subprocess.run(
                [str(python_exe), "-m", "pip", "check"],
                check=True,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
                cwd=str(env_path),
                timeout=_remaining(),
            )
            (env_path / COMPLETE_MARKER).write_text("", encoding="utf-8")
        except subprocess.TimeoutExpired as error:
            raise PluginEnvironmentError(
                f"Provider Hub install for {manifest.provider_id} timed out"
            ) from error
        finally:
            _INSTALL_LOCK.release()
        return env_path

# coding=utf-8
from __future__ import annotations

import contextlib
import hashlib
import os
import platform
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
        key = Path(path_key(path))
        return any(
            key != Path(path_key(self.root / name)) and key.is_relative_to(Path(path_key(self.root / name)))
            for name in (SHARED_ENV_DIR, "envs")
        )

    def _recorded_path(self, manifest: ValidatedManifest) -> Path | None:
        """The environment recorded for this dependency set, whether or not it is intact.

        A recorded path outside the hub's own environment directories is ignored,
        so a hand-edited state document cannot make an install build somewhere else.
        """
        from .state import load_state

        entry = (load_state().get("plugin_envs") or {}).get(self._fingerprint(manifest))
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return None
        candidate = Path(entry["path"])
        return candidate if self._inside_hub(candidate) else None

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
            if reusable is None and self._is_complete(canonical):
                reusable = canonical
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

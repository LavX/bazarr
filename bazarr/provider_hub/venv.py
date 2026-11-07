# coding=utf-8
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import threading
import time

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


def _pin(env_path: Path) -> None:
    held = getattr(_SCOPE, "held", None)
    if held is None:
        return
    with _ENV_LOCK:
        key = str(env_path)
        _PINS[key] += 1
        held.append(key)


def is_pinned(env_path: str | os.PathLike[str]) -> bool:
    with _ENV_LOCK:
        return _PINS.get(str(env_path), 0) > 0


def environment_fingerprint(manifest: ValidatedManifest) -> str:
    """The key an environment is shared under.

    It covers the dependency set and what a compiled wheel depends on, the
    platform and the interpreter's ABI tag. It does not cover the provider id or
    the version, so plugins with the same dependencies resolve to one key. The
    requirement lines are sorted, so their order does not matter.
    """
    digest = hashlib.sha256()
    for line in sorted({requirement.pip_line for requirement in manifest.dependency_requirements}):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    digest.update(sys.platform.encode("utf-8"))
    digest.update((sys.implementation.cache_tag or "").encode("utf-8"))
    return digest.hexdigest()[:16]


def python_executable(env_path: str | os.PathLike[str]) -> Path:
    return Path(env_path) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _passes_pip_check(env_path: Path) -> bool:
    """Whether the environment's installed packages are consistent. Local only."""
    try:
        subprocess.run(
            [str(python_executable(env_path)), "-m", "pip", "check"],
            check=True,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
            cwd=str(env_path),
            timeout=60,
        )
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

    def _recorded_path(self, manifest: ValidatedManifest) -> Path | None:
        """The environment recorded for this dependency set, whether or not it is intact."""
        from .state import load_state

        entry = (load_state().get("plugin_envs") or {}).get(self._fingerprint(manifest))
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return None
        return Path(entry["path"])

    def install(self, manifest: ValidatedManifest, timeout: float | None = None) -> Path:
        """Return the venv for this dependency set, building it only when needed.

        A recorded environment that is intact is reused as it is: nothing is
        installed into it and no network is needed. A recorded one that is
        missing or fails ``pip check`` is rebuilt at the same path, which restores
        the same dependency set for every plugin that shares it. When ``timeout``
        is set it is a hard wall-clock budget for the whole install (shared across
        the venv/pip subprocesses): the startup auto-install passes its remaining
        budget so one stuck pip/venv step can't block boot indefinitely. ``None``
        (manual installs) leaves it unbounded.
        """
        recorded = self._recorded_path(manifest)
        if recorded is not None:
            with _ENV_LOCK:
                _pin(recorded)
            if python_executable(recorded).exists() and _passes_pip_check(recorded):
                return recorded
        return self._build(manifest, timeout, recorded or self.path_for(manifest))

    def _build(self, manifest: ValidatedManifest, timeout: float | None, env_path: Path) -> Path:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

        def _remaining() -> float | None:
            if deadline is None:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise PluginEnvironmentError(
                    f"Provider Hub install for {manifest.provider_id} exceeded its time budget"
                )
            return left

        env_path.mkdir(parents=True, exist_ok=True)
        with _ENV_LOCK:
            _pin(env_path)

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
            if not python_exe.exists():
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
        except subprocess.TimeoutExpired as error:
            raise PluginEnvironmentError(
                f"Provider Hub install for {manifest.provider_id} timed out"
            ) from error
        finally:
            _INSTALL_LOCK.release()
        return env_path

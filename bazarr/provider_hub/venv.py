# coding=utf-8
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time

from pathlib import Path

from .manifest import ValidatedManifest


# Serialize venv creation process-wide. A large catalog update can fan many provider
# installs across the jobs queue / Flask threads at once; each builds a venv (python -m
# venv + pip install), and a concurrent burst can exceed a container memory limit and trip
# the OOM killer. Holding this lock around the heavy subprocess work caps peak memory to a
# single venv build at a time. Builds are idempotent and cached, so serializing only slows
# the rare bulk-install burst, not normal operation.
_INSTALL_LOCK = threading.Lock()


class PluginEnvironmentError(RuntimeError):
    """Raised when a Provider Hub environment cannot be built."""


def python_executable(env_path: str | os.PathLike[str]) -> Path:
    return Path(env_path) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class PluginEnvironment:
    """Build and validate an isolated venv for one Provider Hub provider."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    def _fingerprint(self, manifest: ValidatedManifest) -> str:
        digest = hashlib.sha256()
        digest.update(manifest.provider_id.encode("utf-8"))
        digest.update(manifest.version.encode("utf-8"))
        for requirement in manifest.dependency_requirements:
            digest.update(requirement.pip_line.encode("utf-8"))
        digest.update(sys.version.encode("utf-8"))
        digest.update(sys.platform.encode("utf-8"))
        digest.update((sys.implementation.cache_tag or "").encode("utf-8"))
        return digest.hexdigest()[:16]

    def path_for(self, manifest: ValidatedManifest) -> Path:
        return self.root / "envs" / manifest.provider_id / manifest.version / self._fingerprint(manifest)

    def install(self, manifest: ValidatedManifest, timeout: float | None = None) -> Path:
        """Build the provider venv. When ``timeout`` is set it is a hard wall-clock
        budget for the whole install (shared across the venv/pip subprocesses): the
        startup auto-install passes its remaining budget so one stuck pip/venv step
        can't block boot indefinitely. ``None`` (manual installs) leaves it unbounded.
        """
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

        env_path = self.path_for(manifest)
        env_path.mkdir(parents=True, exist_ok=True)

        python_exe = python_executable(env_path)
        # Serialize the heavy venv/pip subprocess work process-wide (see _INSTALL_LOCK).
        _INSTALL_LOCK.acquire()
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

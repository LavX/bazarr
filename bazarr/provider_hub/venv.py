# coding=utf-8
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

from pathlib import Path

from .manifest import ValidatedManifest


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

    def install(self, manifest: ValidatedManifest) -> Path:
        env_path = self.path_for(manifest)
        env_path.mkdir(parents=True, exist_ok=True)

        python_exe = python_executable(env_path)
        if not python_exe.exists():
            subprocess.run(
                [sys.executable, "-m", "venv", str(env_path)],
                check=True,
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
                "--only-binary=:all:",
                "-r",
                str(requirements_path),
            ]

            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONNOUSERSITE": "1",
            }
            subprocess.run(cmd, check=True, env=env, cwd=str(env_path))

        subprocess.run(
            [str(python_exe), "-m", "pip", "check"],
            check=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"},
            cwd=str(env_path),
        )
        return env_path

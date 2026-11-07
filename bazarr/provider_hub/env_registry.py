# coding=utf-8
"""Which environment each dependency set runs in, and what happens to the old ones.

``state.json`` keeps one record, ``plugin_envs``, that maps an environment
fingerprint (see ``venv.environment_fingerprint``) to the directory of the venv
built for it. Installations point at an environment through ``python_path`` and
``staged_python_path``; nothing is counted, an environment is referenced when
some installation row names its interpreter.

Two entry points, both free of pip and of the network:

* ``reconcile_environments`` runs at boot. It records the environments that
  installations already use, in place, and marks an active installation whose
  environment is gone as ``needs_repair``. It never builds anything.
* ``cleanup_environments`` runs outside boot, from the scheduled update check.
  It removes recorded environments that nothing references, and the old
  per-provider environments nothing references any more.
"""
from __future__ import annotations

import logging
import shutil
import sys

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .state import mutate_state
from .venv import SHARED_ENV_DIR, _ENV_LOCK, environment_fingerprint, is_pinned, python_executable

logger = logging.getLogger(__name__)

ENV_REGISTRY_KEY = "plugin_envs"
NEEDS_REPAIR_MESSAGE = (
    "The plugin's Python environment is missing or broken. Reinstall the plugin to rebuild it."
)


def registry(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    envs = state.get(ENV_REGISTRY_KEY)
    if not isinstance(envs, dict):
        envs = {}
        state[ENV_REGISTRY_KEY] = envs
    return envs


def register_environment(state: dict[str, Any], fingerprint: str, env_path: str | Path) -> None:
    """Record ``env_path`` as the environment for ``fingerprint`` unless a working one is recorded."""
    envs = registry(state)
    entry = envs.get(fingerprint)
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        if str(env_path) == entry["path"] or python_executable(entry["path"]).exists():
            return
    envs[fingerprint] = {"path": str(env_path)}


def env_dir_of(python_path: str | Path) -> str:
    """The venv directory an interpreter path belongs to."""
    return str(Path(python_path).parent.parent)


def referenced_env_paths(state: dict[str, Any]) -> set[str]:
    referenced: set[str] = set()
    for row in (state.get("installations") or {}).values():
        if not isinstance(row, dict):
            continue
        for key in ("python_path", "staged_python_path"):
            if row.get(key):
                referenced.add(env_dir_of(row[key]))
    return referenced


def protected_env_paths(state: dict[str, Any]) -> set[str]:
    """Environments a stopped install must not remove: referenced or recorded ones."""
    recorded = {
        entry["path"] for entry in registry(state).values()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    return referenced_env_paths(state) | recorded


def _interpreter_matches(env_dir: Path) -> bool:
    """Whether the venv was made by the running interpreter's minor version."""
    try:
        text = (env_dir / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            parts = value.strip().split(".")
            return parts[:2] == [str(sys.version_info.major), str(sys.version_info.minor)]
    return False


def _requirements_match(env_dir: Path, manifest) -> bool:
    wanted = {requirement.pip_line for requirement in manifest.dependency_requirements}
    if not wanted:
        return True
    try:
        lines = (env_dir / "requirements.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return {line.strip() for line in lines if line.strip()} == wanted


def _adoptable(env_dir: Path, manifest) -> bool:
    return _interpreter_matches(env_dir) and _requirements_match(env_dir, manifest)


def _recorded_dir(envs: dict[str, Any], fingerprint: str) -> Path | None:
    entry = envs.get(fingerprint)
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        recorded = Path(entry["path"])
        if python_executable(recorded).exists():
            return recorded
    return None


def reconcile_environments(
    *,
    validate: Callable[[dict[str, Any], bool], Any],
    probe: Callable[[Any, Path, Path], None],
) -> dict[str, list[str]]:
    """Adopt the environments installations already use. Boot-safe: no pip, no network.

    ``validate(row, staged)`` returns the row's validated manifest (the staged one
    when ``staged``). ``probe(manifest, bundle_path, python_path)`` is the worker
    import check and raises when the interpreter cannot run the plugin.

    * An environment on disk that matches the installation's dependency set is
      recorded where it is. It is not moved, copied or rebuilt.
    * A second installation with the same dependency set is pointed at the
      recorded environment, but only once the probe passes there. When it fails,
      the installation keeps the environment it had.
    * An active installation whose interpreter is gone is marked ``needs_repair``
      and gets the error text installation errors use.
    """
    summary: dict[str, list[str]] = {"adopted": [], "repointed": [], "needs_repair": [], "kept": []}

    def apply(state: dict[str, Any]) -> None:
        envs = registry(state)
        for provider_id, row in (state.get("installations") or {}).items():
            if not isinstance(row, dict):
                continue
            for staged in (False, True):
                python_key = "staged_python_path" if staged else "python_path"
                if staged and not row.get(python_key):
                    continue
                if not staged and not row.get("active_version"):
                    continue
                python_path = row.get(python_key)
                if not python_path or not Path(python_path).exists():
                    if not staged:
                        row["needs_repair"] = True
                        if not row.get("last_error"):
                            row["last_error"] = NEEDS_REPAIR_MESSAGE
                        summary["needs_repair"].append(provider_id)
                    continue
                if not staged and row.get("needs_repair"):
                    row.pop("needs_repair", None)
                    if row.get("last_error") == NEEDS_REPAIR_MESSAGE:
                        row["last_error"] = None
                try:
                    manifest = validate(row, staged)
                except Exception:
                    continue
                fingerprint = environment_fingerprint(manifest)
                env_dir = Path(env_dir_of(python_path))
                recorded = _recorded_dir(envs, fingerprint)
                if recorded is None:
                    if _adoptable(env_dir, manifest):
                        envs[fingerprint] = {"path": str(env_dir)}
                        summary["adopted"].append(provider_id)
                    continue
                if recorded == env_dir or staged or not row.get("active_path"):
                    continue
                shared_python = python_executable(recorded)
                try:
                    probe(manifest, Path(row["active_path"]), shared_python)
                except Exception as error:
                    logger.warning(
                        "Provider Hub: %s stays on its own environment, the shared one failed the check: %s",
                        provider_id, error,
                    )
                    summary["kept"].append(provider_id)
                    continue
                row[python_key] = str(shared_python)
                summary["repointed"].append(provider_id)

    mutate_state(apply)
    return summary


def clear_needs_repair(provider_id: str) -> None:
    """Drop the flag after the plugin's worker ran: its environment works."""

    def clear(state: dict[str, Any]) -> None:
        row = (state.get("installations") or {}).get(provider_id)
        if isinstance(row, dict) and row.pop("needs_repair", None):
            if row.get("last_error") == NEEDS_REPAIR_MESSAGE:
                row["last_error"] = None

    mutate_state(clear)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def cleanup_environments(root: str | Path) -> list[str]:
    """Remove environments no installation references. Never call this during boot.

    Covers the recorded environments and the old per-provider directories
    (``envs/<id>/<version>/<fingerprint>``). An environment an install is using
    right now is pinned and stays. Only directories under ``root`` are removed.
    """
    root = Path(root)
    removed: list[str] = []

    def apply(state: dict[str, Any]) -> None:
        referenced = referenced_env_paths(state)
        envs = registry(state)
        with _ENV_LOCK:
            for fingerprint, entry in list(envs.items()):
                path = entry.get("path") if isinstance(entry, dict) else None
                if not isinstance(path, str):
                    del envs[fingerprint]
                    continue
                if path in referenced or is_pinned(path):
                    continue
                target = Path(path)
                if target.exists():
                    if not (_inside(target, root / SHARED_ENV_DIR) or _inside(target, root / "envs")):
                        continue
                    shutil.rmtree(target, ignore_errors=True)
                    removed.append(path)
                del envs[fingerprint]
            recorded = {
                entry["path"] for entry in envs.values()
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            }
            for old in sorted((root / "envs").glob("*/*/*")):
                key = str(old)
                if not old.is_dir() or key in referenced or key in recorded or is_pinned(key):
                    continue
                shutil.rmtree(old, ignore_errors=True)
                removed.append(key)

    mutate_state(apply)
    return removed

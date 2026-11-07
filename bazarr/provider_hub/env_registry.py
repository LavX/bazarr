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
import os
import platform
import shutil
import stat
import sys

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .state import (
    corrupt_copies, provider_hub_dir, load_state, load_state_strict, mutate_state, mutate_state_checked, read_state_document,
)
from .venv import (
    SHARED_ENV_DIR, _ENV_LOCK, environment_fingerprint, has_parent_reference, is_pinned, path_key, pinned_paths,
    processes_using_directory, python_executable, validated_bundle_path, validated_hub_path, validated_interpreter,
)

logger = logging.getLogger(__name__)

ENV_REGISTRY_KEY = "plugin_envs"
NEEDS_REPAIR_MESSAGE = (
    "The plugin's Python environment is missing or broken. Repair it to rebuild the environment."
)

# ELF e_machine values by platform.machine(), to tell an environment built for
# another architecture from one that runs here.
_ELF_MACHINES = {
    "x86_64": 62, "amd64": 62, "aarch64": 183, "arm64": 183, "armv7l": 40, "armv8l": 40,
    "i686": 3, "i386": 3, "ppc64le": 21, "s390x": 22, "riscv64": 243,
}


def registry(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    envs = state.get(ENV_REGISTRY_KEY)
    if not isinstance(envs, dict):
        envs = {}
        state[ENV_REGISTRY_KEY] = envs
    return envs


def register_environment(state: dict[str, Any], fingerprint: str, env_path: str | Path) -> None:
    """Record ``env_path`` as the environment for ``fingerprint``.

    The environment that just passed the worker check replaces whatever was
    recorded: the install only builds a different directory when the recorded
    one could not be used. The replaced directory stays until nothing
    references it and a cleanup removes it.
    """
    registry(state)[fingerprint] = {"path": str(env_path)}


def env_dir_of(python_path: str | Path) -> str:
    """The venv directory an interpreter path belongs to."""
    return path_key(Path(python_path).parent.parent)


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
        path_key(entry["path"]) for entry in registry(state).values()
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


def _machine_matches(env_dir: Path) -> bool:
    """Whether the venv's interpreter is built for this machine's architecture.

    Read from the ELF header of the interpreter the venv links to. A file that
    is not ELF, or a machine this table does not know, is not judged.
    """
    expected = _ELF_MACHINES.get(platform.machine().lower())
    if expected is None:
        return True
    try:
        with open(python_executable(env_dir).resolve(), "rb") as handle:
            head = handle.read(20)
    except OSError:
        return False
    if head[:4] != b"\x7fELF" or len(head) < 20:
        return True
    return int.from_bytes(head[18:20], "big" if head[5] == 2 else "little") == expected


def _requirements_match(env_dir: Path, manifest) -> bool:
    """Whether the environment was built for exactly this dependency set.

    An environment with no dependencies has no ``requirements.txt`` to read, so
    a missing file only matches the empty set; a file that is there is compared
    either way, so an environment with packages is never adopted as an empty one.
    """
    wanted = {requirement.pip_line for requirement in manifest.dependency_requirements}
    try:
        lines = (env_dir / "requirements.txt").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return not wanted
    except OSError:
        return False
    return {line.strip() for line in lines if line.strip()} == wanted


def _adoptable(env_dir: Path, manifest) -> bool:
    return _interpreter_matches(env_dir) and _machine_matches(env_dir) and _requirements_match(env_dir, manifest)


def _recorded_dir(envs: dict[str, Any], fingerprint: str, root: Path) -> Path | None:
    """The recorded environment for a dependency set, only if it is the hub's own and has an interpreter."""
    entry = envs.get(fingerprint)
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        recorded = validated_hub_path(root, entry["path"])
        if recorded is not None and python_executable(recorded).exists():
            interpreter = validated_interpreter(root, python_executable(recorded))
            if interpreter is not None:
                return interpreter.parent.parent
    return None


def reconcile_environments(
    *,
    validate: Callable[[dict[str, Any], bool], Any],
    probe: Callable[[Any, Path, Path], None],
    root: str | Path | None = None,
    verify_bundle: Callable[[Any, Path], None] | None = None,
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

    The probes run without the state lock; the result is applied in one write
    that skips any row that changed in the meantime.
    """
    summary: dict[str, list[str]] = {"adopted": [], "repointed": [], "needs_repair": [], "kept": []}
    root = Path(root) if root is not None else provider_hub_dir()
    try:
        state = load_state_strict()
    except (OSError, ValueError):
        # An unreadable document is not an empty inventory: reading it as one
        # would record nothing and then write the default over what is stored.
        logger.warning("Provider Hub: the state could not be read, so no environment was reconciled", exc_info=True)
        return summary
    envs = {fp: dict(entry) for fp, entry in registry(state).items() if isinstance(entry, dict)}
    adoptions: dict[str, str] = {}
    flags: list[str] = []
    unflags: list[str] = []
    repoints: list[tuple[str, str, str, str]] = []

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
                    flags.append(provider_id)
                    summary["needs_repair"].append(provider_id)
                continue
            if not staged and validated_bundle_path(root, row.get("active_path")) is None:
                # A bundle the hub did not put there is neither run nor cleaned.
                flags.append(provider_id)
                summary["needs_repair"].append(provider_id)
                continue
            clearing = (
                not staged and bool(row.get("needs_repair")) and row.get("last_error") == NEEDS_REPAIR_MESSAGE
            )
            try:
                manifest = validate(row, staged)
            except Exception:
                continue
            if clearing:
                # The flag goes only when the environment is back and the bundle
                # still matches its manifest; a failed check keeps (or sets) it.
                bundle_for_flag = validated_bundle_path(root, row.get("active_path"))
                if verify_bundle is not None and bundle_for_flag is not None:
                    try:
                        verify_bundle(manifest, bundle_for_flag)
                    except Exception:
                        logger.warning(
                            "Provider Hub: the bundle of %s does not match its manifest", provider_id, exc_info=True
                        )
                        flags.append(provider_id)
                        summary["needs_repair"].append(provider_id)
                        continue
                unflags.append(provider_id)
            fingerprint = environment_fingerprint(manifest)
            interpreter = validated_interpreter(root, python_path)
            if interpreter is None:
                # An interpreter outside the hub's own environments, or reached
                # through a symlink, is neither adopted nor probed nor pointed at.
                logger.warning(
                    "Provider Hub: %s runs from %s, which is not one of the hub's environments; left as it is",
                    provider_id, python_path,
                )
                continue
            env_dir = Path(env_dir_of(interpreter))
            recorded = _recorded_dir(envs, fingerprint, root)
            if recorded is None:
                if _adoptable(env_dir, manifest):
                    envs[fingerprint] = {"path": str(env_dir)}
                    adoptions[fingerprint] = str(env_dir)
                    summary["adopted"].append(provider_id)
                continue
            if path_key(recorded) == str(env_dir) or staged or not row.get("active_path"):
                continue
            shared_python = python_executable(recorded)
            bundle = validated_bundle_path(root, row["active_path"])
            if bundle is None:
                continue
            if verify_bundle is not None:
                try:
                    verify_bundle(manifest, bundle)
                except Exception:
                    # Modified code is not run for the probe; the installation is flagged, nothing else changes.
                    logger.warning("Provider Hub: the bundle of %s does not match its manifest", provider_id, exc_info=True)
                    flags.append(provider_id)
                    summary["needs_repair"].append(provider_id)
                    continue
            try:
                probe(manifest, bundle, shared_python)
            except Exception as error:
                logger.warning(
                    "Provider Hub: %s stays on its own environment, the shared one failed the check: %s",
                    provider_id, error,
                )
                summary["kept"].append(provider_id)
                continue
            repoints.append((provider_id, python_key, str(python_path), str(shared_python)))
            summary["repointed"].append(provider_id)

    def apply(state: dict[str, Any]) -> None:
        installations = state.get("installations") or {}
        stored = registry(state)
        for fingerprint, path in adoptions.items():
            stored[fingerprint] = {"path": path}
        for provider_id in flags:
            row = installations.get(provider_id)
            if isinstance(row, dict):
                row["needs_repair"] = True
                if not row.get("last_error"):
                    row["last_error"] = NEEDS_REPAIR_MESSAGE
        for provider_id in unflags:
            if provider_id in flags:
                continue  # a check in this pass found it broken: the flag stays
            row = installations.get(provider_id)
            if isinstance(row, dict) and row.get("last_error") == NEEDS_REPAIR_MESSAGE:
                row.pop("needs_repair", None)
                row["last_error"] = None
        for provider_id, python_key, old, new in repoints:
            row = installations.get(provider_id)
            if isinstance(row, dict) and row.get(python_key) == old:
                row[python_key] = new

    mutate_state(apply)
    return summary


def _row_uses(row: Any, python_path: str | None) -> bool:
    """Whether an installation row still runs from ``python_path`` (always true when none is named)."""
    if python_path is None:
        return True
    return isinstance(row, dict) and row.get("python_path") == python_path


def mark_needs_repair(provider_id: str, python_path: str | None = None) -> None:
    """Flag an installation whose worker failed its check. The error text stays as the worker gave it.

    With ``python_path``, only when the installation still runs from it: a check
    that ran against an interpreter a repair has since replaced says nothing
    about the installation.
    """

    def mark(state: dict[str, Any]) -> None:
        row = (state.get("installations") or {}).get(provider_id)
        if isinstance(row, dict) and _row_uses(row, python_path):
            row["needs_repair"] = True

    mutate_state(mark)


def clear_needs_repair(provider_id: str, python_path: str | None = None) -> None:
    """Drop the flag after the plugin's worker ran: its environment works."""

    def clear(state: dict[str, Any]) -> None:
        row = (state.get("installations") or {}).get(provider_id)
        if _row_uses(row, python_path) and isinstance(row, dict) and row.pop("needs_repair", None):
            if row.get("last_error") == NEEDS_REPAIR_MESSAGE:
                row["last_error"] = None

    mutate_state(clear)


LEGACY_ENV_DIR = "envs"


def _hub_relative(root: str | Path, target: str | Path) -> tuple[Path, tuple[str, ...]] | None:
    """The resolved hub directory and ``target`` as parts below it, or None.

    The comparison is lexical, so a symlink in the target's own spelling can
    never make it look like it is inside the hub. The caller then walks the
    parts with ``lstat``.
    """
    if has_parent_reference(target):
        return None
    hub = Path(path_key(root))
    candidate = Path(os.path.abspath(target))
    for base in (Path(os.path.abspath(root)), hub):
        try:
            return hub, candidate.relative_to(base).parts
        except ValueError:
            continue
    return None


def _is_env_layout(parts: tuple[str, ...]) -> bool:
    """``shared_envs/<name>`` or the old ``envs/<id>/<version>/<fingerprint>``.

    Anything shallower is a container directory, anything deeper is inside an
    environment: neither is an environment to remove.
    """
    if not parts or any(part in ("", ".", "..") for part in parts):
        return False
    return (parts[0] == SHARED_ENV_DIR and len(parts) == 2) or (parts[0] == LEGACY_ENV_DIR and len(parts) == 4)


def _walk_real_directories(hub: Path, parts: tuple[str, ...]) -> Path | None:
    """``hub / parts`` when every component is a real directory, never a symlink."""
    current = hub
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError:
            return None
        if not stat.S_ISDIR(mode):
            return None
    return current


def _locate(root: str | Path, target: str | Path, *, env_layout: bool = True) -> Path | None:
    """The directory to remove for ``target``, or None when it is not safe to.

    It must be strictly inside the hub (an environment directory, not a
    container), and no component of the way down from the hub may be a symlink.
    """
    located = _hub_relative(root, target)
    if located is None:
        return None
    hub, parts = located
    if env_layout and not _is_env_layout(parts):
        return None
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return _walk_real_directories(hub, parts)


def _overlaps(target: Path, others) -> bool:
    """Whether any of ``others`` is ``target``, inside it, or contains it."""
    for other in others:
        candidate = Path(other)
        if candidate == target or target in candidate.parents or candidate in target.parents:
            return True
    return False


def _remove_environment(root: str | Path, target: str | Path, protected) -> bool:
    """Remove one environment directory if it is safe to. Callers hold ``_ENV_LOCK``.

    Refused when the target is not an environment directory strictly inside the
    hub, when a symlink lies on the way to it, or when a referenced, recorded
    or pinned environment is the target, lies inside it or contains it.
    """
    located = _locate(root, target)
    if located is None:
        return False
    if _overlaps(located, (*protected, *pinned_paths())):
        return False
    if processes_using_directory(located):
        # A process the worker's groups do not cover (one that started its own
        # session) still runs from it: leave it for a later run.
        logger.info("Provider Hub: %s is still in use by a running process; not removed", located)
        return False
    shutil.rmtree(located, ignore_errors=True)
    return True


def is_environment_path(root: str | Path, target: str | Path) -> bool:
    """Whether ``target`` is spelled as an environment directory under the hub."""
    located = _hub_relative(root, target)
    return located is not None and _is_env_layout(located[1])


def remove_inside_hub(root: str | Path, target: str | Path) -> bool:
    """Remove a staged bundle directory, only when it is strictly inside the hub without a symlink on the way."""
    located = _locate(root, target, env_layout=False)
    if located is None:
        return False
    shutil.rmtree(located, ignore_errors=True)
    return True


class StateUnreadable(RuntimeError):
    """The stored installation inventory is missing or malformed, so nothing may be deleted on its say-so."""


def validate_inventory(document: Any) -> None:
    """Refuse a state document whose installation inventory cannot be trusted.

    A cleanup deletes whatever the inventory does not reference, so an inventory
    that is absent, of the wrong type, or has a row it cannot read must stop it:
    treating that as "nothing is referenced" would delete every environment.
    """
    if corrupt_copies():
        raise StateUnreadable(
            "a copy of a damaged state document is kept beside the state; remove it once the state is checked"
        )
    if not isinstance(document, dict):
        raise StateUnreadable("the state document is not an object")
    installations = document.get("installations")
    if not isinstance(installations, dict):
        raise StateUnreadable("the state has no installation inventory")
    for provider_id, row in installations.items():
        if not isinstance(row, dict):
            raise StateUnreadable(f"the installation {provider_id!r} is not readable")
        for key in ("python_path", "staged_python_path"):
            if row.get(key) is not None and not isinstance(row[key], str):
                raise StateUnreadable(f"the installation {provider_id!r} has an unreadable {key}")
    envs = document.get(ENV_REGISTRY_KEY)
    if envs is not None and not isinstance(envs, dict):
        raise StateUnreadable("the environment record is not readable")


def _checked_state() -> dict[str, Any]:
    """The current state, only if its inventory validates. Raises ``StateUnreadable`` or the read error.

    A hub that has never written a state document has nothing recorded, which is
    a fresh install and not a damaged one: it reads as the default.
    """
    if corrupt_copies():
        raise StateUnreadable("a copy of a damaged state document is kept beside the state")
    try:
        document = read_state_document()
    except FileNotFoundError:
        return load_state()
    validate_inventory(document)
    return load_state()


def discard_if_unused(root: str | Path, env_path: str | Path) -> bool:
    """Remove an environment a failed or stopped install built, unless something uses it.

    Referenced, recorded and pinned environments stay. Call it after
    ``venv.release_pins`` so this install's own hold does not count.
    """
    with _ENV_LOCK:
        try:
            state = _checked_state()
        except (StateUnreadable, OSError, ValueError):
            logger.warning("Provider Hub: the state could not be read, so no environment was removed", exc_info=True)
            return False
        return _remove_environment(root, env_path, protected_env_paths(state))


def _legacy_environment_dirs(root: Path) -> list[Path]:
    """Every directory where an unrecorded environment can sit, without following a symlink.

    A container that is a symlink is not entered; a symlink entry is skipped.
    """
    hub = Path(path_key(root))

    def children(parent: Path, parts: tuple[str, ...]) -> list[Path]:
        if _walk_real_directories(hub, parts) is None:
            return []
        try:
            with os.scandir(parent) as entries:
                return sorted(
                    parent / entry.name for entry in entries if entry.is_dir(follow_symlinks=False)
                )
        except OSError:
            return []

    found = children(root / SHARED_ENV_DIR, (SHARED_ENV_DIR,))
    for provider in children(root / LEGACY_ENV_DIR, (LEGACY_ENV_DIR,)):
        for version in children(provider, (LEGACY_ENV_DIR, provider.name)):
            found.extend(children(version, (LEGACY_ENV_DIR, provider.name, version.name)))
    return sorted(found)


def cleanup_environments(root: str | Path) -> list[str]:
    """Remove environments no installation references. Never call this during boot.

    Covers the recorded environments, the unrecorded directories under
    ``shared_envs`` and the old per-provider directories
    (``envs/<id>/<version>/<fingerprint>``). An environment an install is using
    right now is pinned and stays. Only environment directories strictly inside
    ``root`` are removed, reached without crossing a symlink, and never one that
    contains or sits inside a referenced or pinned environment.
    """
    root = Path(root)
    removed: list[str] = []

    def apply(state: dict[str, Any]) -> None:
        referenced = referenced_env_paths(state)
        envs = registry(state)
        for fingerprint, entry in list(envs.items()):
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str):
                del envs[fingerprint]
                continue
            if path_key(path) in referenced or is_pinned(path):
                continue
            if os.path.lexists(path):
                if not _remove_environment(root, path, referenced):
                    continue
                removed.append(path)
            del envs[fingerprint]
        recorded = {
            path_key(entry["path"]) for entry in envs.values()
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        # Environments built into a new directory leave the replaced one behind,
        # and a failed install can leave one nothing recorded. Both are swept
        # here once nothing references or pins them.
        removed.extend(
            str(old) for old in _legacy_environment_dirs(root)
            if _remove_environment(root, old, referenced | recorded)
        )

    # The decision and the state write happen under the lock an install takes
    # to read the record and pin, so an install never sees a record whose
    # directory a cleanup is about to remove.
    with _ENV_LOCK:
        try:
            mutate_state_checked(apply, validate_inventory)
        except (StateUnreadable, OSError, ValueError):
            logger.warning("Provider Hub: the state could not be read, so no environment was removed", exc_info=True)
    return removed

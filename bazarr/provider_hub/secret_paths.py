"""Manifests of releases that reached activation, retained for secret discovery."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

HISTORY_ARCHIVE = "plugin_manifest_history.json"
HISTORY_FIELD = "plugin_manifest_history"
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")


def _archive_folder(root: Path, plugin_id: str) -> Path | None:
    if not _SAFE_ID.fullmatch(plugin_id):
        return None
    parent = root / "manifests" / plugin_id
    if any(path.is_symlink() or path.is_junction() for path in (parent.parent, parent)):
        return None
    return parent


def _history_record(manifest: dict) -> dict:
    """Retain declarations only, without configuration defaults or values."""
    from secret_store.registry import manifest_secret_fields

    record = {key: manifest[key] for key in ("version", "bundle_sha256") if isinstance(manifest.get(key), str)}
    schema = manifest.get("config_schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(properties, dict):
        record["config_schema"] = {"properties": {key: {} for key in properties if isinstance(key, str)}}
    record["secret_fields"] = sorted(manifest_secret_fields(manifest))
    if "permissions" in manifest:
        permissions = manifest["permissions"]
        record["permissions"] = ([item for item in permissions if isinstance(item, str)]
                                 if isinstance(permissions, (list, tuple)) else {"unreadable": True})
    if "login" in manifest:
        login = manifest["login"]
        paths = login.get("paths") if isinstance(login, dict) else None
        record["login"] = {"paths": paths if isinstance(paths, list) and all(isinstance(p, str) for p in paths)
                           else None}
    return record


def protected_history(state, plugin_id: str) -> list[dict]:
    history = state.get(HISTORY_FIELD, {}).get(plugin_id, [])
    return history if isinstance(history, list) else []


def remember_manifest(state, plugin_id: str, manifest: dict | None) -> None:
    """Save a pruned release's exclusions in the same write as the prune."""
    if not _SAFE_ID.fullmatch(plugin_id):
        return
    record = _history_record(manifest) if isinstance(manifest, dict) else {"login": {"paths": None}}
    history = state.setdefault(HISTORY_FIELD, {}).setdefault(plugin_id, [])
    if record not in history:
        history.append(record)


def archive_manifest(root: Path, plugin_id: str, manifest: dict) -> Path | None:
    folder = _archive_folder(root, plugin_id)
    version, digest = manifest.get("version"), manifest.get("bundle_sha256")
    if (folder is None or not isinstance(version, str) or not _VERSION.fullmatch(version)
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        return
    # The manifest may change without changing the bundle. Preserve both declarations in that case.
    content = json.dumps(_history_record(manifest), sort_keys=True).encode("utf-8")
    manifest_digest = hashlib.sha256(content).hexdigest()[:16]
    path = folder / f"{version}-{digest[:16]}-{manifest_digest}.json"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        return path
    fd, temp = tempfile.mkstemp(prefix=".manifest-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
    return path


def archived_manifests(root: Path, plugin_id: str, *, strict: bool = False) -> list[dict]:
    folder = _archive_folder(root, plugin_id)
    if folder is None:
        if strict:
            raise OSError("The plugin manifest archive has an unsafe path")
        return []
    result = []
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return result
    except OSError:
        if strict:
            raise
        return result
    for candidate in entries:
        if candidate.suffix != ".json":
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError("The plugin manifest record is not a regular file")
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or not isinstance(manifest.get("version"), str)
                    or not isinstance(manifest.get("bundle_sha256"), str)):
                raise ValueError("The plugin manifest record could not be read")
            result.append(manifest)
        except (OSError, ValueError):
            if strict:
                raise
    return result


def write_backup_history(archive, root: Path, state) -> None:
    """Carry the historical field classification onto a fresh restore host."""
    records = {}
    history = state.get(HISTORY_FIELD, {})
    ids = set(history) if isinstance(history, dict) else set()
    parent = root / "manifests"
    if parent.is_dir() and not parent.is_symlink() and not parent.is_junction():
        with contextlib.suppress(OSError):
            ids.update(folder.name for folder in parent.iterdir() if folder.is_dir())
    for plugin_id in ids:
        if not _SAFE_ID.fullmatch(plugin_id):
            continue
        manifests = [*protected_history(state, plugin_id), *archived_manifests(root, plugin_id)]
        for manifest in manifests:
            if isinstance(manifest, dict):
                remember_manifest(records, plugin_id, manifest)
    if records:
        archive.writestr(HISTORY_ARCHIVE, json.dumps(records, sort_keys=True))


def restore_backup_history(extracted: Path, root: Path) -> None:
    # Config loads before the hub's DB store on the following boot. Recreate
    # the declaration files now, before settings classifies old fields.
    if not extracted.is_file():
        return
    if extracted.is_symlink():
        raise OSError("The saved plugin manifest history is a link")
    history = json.loads(extracted.read_text(encoding="utf-8")).get(HISTORY_FIELD, {})
    if not isinstance(history, dict):
        raise ValueError("The plugin manifest history is not an object")
    from .datadir import sync_directory, sync_file

    written = set()
    for plugin_id, records in history.items():
        if not isinstance(records, list):
            raise ValueError("A plugin manifest history is not a list")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("A plugin manifest history record is not an object")
            path = archive_manifest(root, plugin_id, record)
            if path is not None:
                if path.is_symlink() or not path.is_file():
                    raise OSError("The restored plugin manifest is not a regular file")
                sync_file(path)
                written.add(path.parent)
    if written:
        for directory in (*written, root / "manifests", root, root.parent):
            sync_directory(directory)

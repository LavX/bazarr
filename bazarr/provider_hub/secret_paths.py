"""Manifests of releases that reached activation, retained for secret discovery."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

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


def archive_manifest(root: Path, plugin_id: str, manifest: dict) -> None:
    folder = _archive_folder(root, plugin_id)
    version, digest = manifest.get("version"), manifest.get("bundle_sha256")
    if (folder is None or not isinstance(version, str) or not _VERSION.fullmatch(version)
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        return
    # The manifest may change without changing the bundle. Preserve both declarations in that case.
    content = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_digest = hashlib.sha256(content).hexdigest()[:16]
    path = folder / f"{version}-{digest[:16]}-{manifest_digest}.json"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        return
    fd, temp = tempfile.mkstemp(prefix=".manifest-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def archived_manifests(root: Path, plugin_id: str) -> list[dict]:
    folder = _archive_folder(root, plugin_id)
    result = []
    if folder is not None:
        for candidate in folder.glob("*.json"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            with contextlib.suppress(OSError, ValueError):
                manifest = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    result.append(manifest)
    return result

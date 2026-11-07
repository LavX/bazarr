# coding=utf-8
"""Reader for the official catalog snapshot shipped in the image.

A release image carries ``catalog.json`` and every provider bundle file it
names, as they stood on the catalog's ``main`` at the release, in a read-only
directory beside the application tree (see ``scripts/release/catalog_floor.py``
for the layout and how it is built). This module is the runtime side of that
snapshot. It does three things:

* It answers the official catalog when the official source has never been
  refreshed successfully (a fresh install with no network, or one a rate limit
  keeps out), so the Marketplace is not empty.
* It serves bundle bytes for an install whose manifest names the snapshot's
  own commit, so that install needs no network either.
* It never offers anything newer than a catalog that was actually fetched: a
  source that has a resolved commit is never seeded, and the first successful
  refresh replaces every seeded entry.

The reader never writes. It opens files read-only without following links,
and it checks every byte it hands out against the sha256 recorded in
``floor.json`` first. The snapshot is trusted as the release's own catalog
under the reason "release floor", for the official source only, and never on a
source that follows a ``dev_ref``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import BundleValidationError
from .state import (
    OFFICIAL_CATALOG_REPO,
    OFFICIAL_CATALOG_SOURCE_ID,
    ORIGIN_BUNDLED,
    RELEASE_FLOOR_TRUST_REASON,
)

logger = logging.getLogger(__name__)

# Points the reader at another directory (tests, a repacked image). An empty
# value turns the floor off.
FLOOR_DIR_ENV = "BAZARR_CATALOG_FLOOR"
CATALOG_FILE = "catalog.json"
META_FILE = "floor.json"
SUPPORTED_LOCK_VERSION = 1
# The largest file the reader will take. The biggest real file is a catalog of a
# few hundred kilobytes; the cap only keeps a damaged snapshot from filling memory.
MAX_FILE_BYTES = 16 * 1024 * 1024

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FloorError(Exception):
    """The snapshot is missing a piece, malformed, or names something it must not."""


class FloorIntegrityError(FloorError, BundleValidationError):
    """A snapshot file does not hash to the value floor.json records for it."""


def floor_root() -> Path | None:
    """The snapshot directory, or None when the floor is switched off."""
    override = os.environ.get(FLOOR_DIR_ENV)
    if override is not None:
        return Path(override) if override else None
    # <app root>/bazarr/provider_hub/floor.py -> <app root>/catalog_floor
    return Path(__file__).resolve().parents[2] / "catalog_floor"


def _safe_path(path: Any) -> str:
    # The same rule the builder applies when it writes the snapshot.
    if (
        not isinstance(path, str) or not path or path.startswith("/")
        or "\\" in path or "\0" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise FloorError(f"unsafe snapshot path: {path!r}")
    return path


def _read_regular_file(root: Path, path: str) -> bytes:
    """Read one regular file under ``root`` without following a link anywhere
    below it and without ever opening anything for writing."""
    parts = _safe_path(path).split("/")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    opened: list[int] = []
    try:
        descriptor = os.open(root, directory_flags)
        opened.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            opened.append(descriptor)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
        opened.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FloorError(f"snapshot entry is not a regular file: {path}")
        if info.st_size > MAX_FILE_BYTES:
            raise FloorError(f"snapshot file is too large: {path}")
        chunks = []
        remaining = info.st_size
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise FloorError(f"snapshot file could not be read: {path}") from error
    finally:
        for descriptor in opened:
            os.close(descriptor)


@dataclass(frozen=True)
class Floor:
    """A snapshot whose floor.json and catalog.json passed their checks."""

    root: Path
    repo: str
    branch: str
    commit: str
    files: dict[str, str]
    catalog_bytes: bytes

    def catalog(self) -> dict[str, Any]:
        """The catalog document, parsed afresh so a caller may edit its copy."""
        return json.loads(self.catalog_bytes)

    def read(self, path: str) -> bytes:
        """The bytes of one snapshot file, checked against floor.json first."""
        expected = self.files.get(path)
        if expected is None:
            raise FloorError(f"{path} is not in the snapshot")
        data = _read_regular_file(self.root, path)
        if hashlib.sha256(data).hexdigest() != expected:
            raise FloorIntegrityError(f"snapshot file {path} does not match floor.json")
        return data


def _parse_meta(raw: bytes) -> tuple[str, str, str, dict[str, str]]:
    try:
        meta = json.loads(raw)
    except ValueError as error:
        raise FloorError("floor.json is not valid JSON") from error
    if not isinstance(meta, dict):
        raise FloorError("floor.json is not an object")
    if meta.get("lock_version") != SUPPORTED_LOCK_VERSION:
        raise FloorError("floor.json has an unsupported lock_version")
    repo, branch, commit = meta.get("repo"), meta.get("branch"), meta.get("commit")
    if repo != OFFICIAL_CATALOG_REPO:
        raise FloorError("floor.json names a repository other than the official catalog")
    if not isinstance(branch, str) or not branch:
        raise FloorError("floor.json has no branch")
    if not isinstance(commit, str) or not _SHA1.match(commit):
        raise FloorError("floor.json has no valid commit")
    files = meta.get("files")
    if not isinstance(files, dict) or CATALOG_FILE not in files:
        raise FloorError("floor.json does not list catalog.json")
    for path, digest in files.items():
        _safe_path(path)
        if path == META_FILE:
            raise FloorError("floor.json lists itself")
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise FloorError(f"floor.json has no valid sha256 for {path}")
    return repo, branch, commit, dict(files)


_cache_lock = threading.Lock()
_cache: tuple[tuple[Any, ...], Floor] | None = None


def _meta_signature(root: Path) -> tuple[Any, ...]:
    info = (root / META_FILE).lstat()
    return (str(root), info.st_ino, info.st_size, info.st_mtime_ns)


def load_floor(root: str | os.PathLike[str] | None = None) -> Floor | None:
    """The snapshot, or None when there is no snapshot directory to read.

    A directory that is there but does not check out raises FloorError. The
    result is cached for as long as floor.json is unchanged; catalog.json is
    hashed once, when it is loaded, and every other file on each read."""
    global _cache
    resolved = Path(root) if root is not None else floor_root()
    if resolved is None:
        return None
    try:
        resolved = resolved.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    try:
        signature = _meta_signature(resolved)
    except OSError:
        raise FloorError("the snapshot has no floor.json") from None
    with _cache_lock:
        if _cache is not None and _cache[0] == signature:
            return _cache[1]
    repo, branch, commit, files = _parse_meta(_read_regular_file(resolved, META_FILE))
    catalog_bytes = _read_regular_file(resolved, CATALOG_FILE)
    if hashlib.sha256(catalog_bytes).hexdigest() != files[CATALOG_FILE]:
        raise FloorIntegrityError("catalog.json does not match floor.json")
    try:
        document = json.loads(catalog_bytes)
    except ValueError as error:
        raise FloorError("catalog.json is not valid JSON") from error
    providers = document.get("providers") if isinstance(document, dict) else None
    if not isinstance(providers, list) or not providers:
        raise FloorError("catalog.json has no providers list")
    floor = Floor(resolved, repo, branch, commit, files, catalog_bytes)
    with _cache_lock:
        _cache = (signature, floor)
    return floor


def _usable_floor() -> Floor | None:
    """The snapshot when it can be used, else None (and a logged reason)."""
    try:
        return load_floor()
    except FloorError as error:
        logger.warning("The bundled provider catalog is not used: %s", error)
        return None


def seed_candidate(source: dict[str, Any], entries: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The catalog to seed the official source with, and its commit, or None.

    Only an official source that has never refreshed successfully qualifies: no
    resolved commit, no successful check, no dev_ref, and nothing stored for it
    that did not come from a snapshot. A catalog that was ever fetched is
    therefore never replaced by the older one baked into the image. The
    plugins list is not seeded: the snapshot carries the providers feed only.
    """
    if source.get("id") != OFFICIAL_CATALOG_SOURCE_ID or source.get("dev_ref"):
        return None
    if source.get("resolved_commit") or source.get("last_checked_at"):
        return None
    for entry in entries.values():
        if (isinstance(entry, dict) and entry.get("source") == OFFICIAL_CATALOG_SOURCE_ID
                and entry.get("origin") != ORIGIN_BUNDLED):
            return None
    floor = _usable_floor()
    if floor is None:
        return None
    document = floor.catalog()
    return {key: value for key, value in document.items() if key != "plugins"}, floor.commit


def trust_source(source: dict[str, Any]) -> None:
    """Trust the official source for the release's own catalog."""
    source["trusted"] = True
    source["trust_reason"] = RELEASE_FLOOR_TRUST_REASON


def mark_seeded(
    source: dict[str, Any], entries: dict[str, Any], entry_keys: set[str], commit: str, now: str
) -> None:
    """Mark the official entries just built from the snapshot, and the source that holds them.

    ``entry_keys`` may hold keys of other sources refreshed in the same pass; only
    the official source's entries are marked."""
    for key in entry_keys:
        entry = entries.get(key)
        if isinstance(entry, dict) and entry.get("source") == OFFICIAL_CATALOG_SOURCE_ID:
            entry["origin"] = ORIGIN_BUNDLED
    source["floor_seed"] = {"commit": commit, "seeded_at": now}


def clear_seed(source: dict[str, Any]) -> None:
    """Forget the seed marker once a fetched catalog has replaced the seeded one."""
    source.pop("floor_seed", None)


def bundle_file(manifest: Any, relative_path: str) -> bytes | None:
    """The bytes of one bundle file from the snapshot, or None when the snapshot
    does not hold this manifest's file (another repository, another commit, a
    file it never carried, or no usable snapshot).

    A file the snapshot does hold but whose bytes no longer match floor.json
    raises FloorIntegrityError, which fails the install like any other
    mismatching bundle file. The caller still checks the result against the
    manifest's own pins."""
    if getattr(manifest, "source_repo", None) != OFFICIAL_CATALOG_REPO:
        return None
    floor = _usable_floor()
    if floor is None or manifest.source_commit != floor.commit:
        return None
    path = "/".join(part for part in (manifest.source_path, relative_path) if part)
    if path in (CATALOG_FILE, META_FILE) or path not in floor.files:
        return None
    return floor.read(path)

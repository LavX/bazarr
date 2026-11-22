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

When the snapshot also carries the signed feed of its channel (the signed
catalog, its detached signature and the root-signed key file), the reader
verifies it in baked mode with the root this release ships: signature, key
chain, revocations and channel, but not the expiry window or the replay
counter. Only a feed that passes is seeded with its ``plugins`` list. A feed
that is missing a piece, does not verify or names another commit seeds the
providers of ``catalog.json`` only, with the reason logged. Seeding from the
snapshot never latches a channel and never raises a stored counter: those move
only on a catalog that was fetched.

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

from . import catalog_feed, revocation
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
# The source status the UI shows while the catalog on screen is the snapshot.
CATALOG_STATUS_BUNDLED = "bundled"
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
    # The signed catalog's bytes, set only when the snapshot's signed feed passed the baked check.
    signed_bytes: bytes | None = None

    def catalog(self) -> dict[str, Any]:
        """The catalog document, parsed afresh so a caller may edit its copy."""
        return json.loads(self.catalog_bytes)

    def signed_catalog(self) -> dict[str, Any] | None:
        """The verified signed catalog, parsed afresh, or None when the snapshot has none that verified."""
        return json.loads(self.signed_bytes) if self.signed_bytes is not None else None

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
    def identity(path: str) -> tuple[int, ...] | None:
        try:
            info = (root / path).lstat()
        except OSError as error:
            if path == META_FILE:
                raise
            return (error.errno,) if error.errno is not None else None
        return info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    # An image should be immutable. If a repacked floor changes after boot,
    # a cached verification must not keep vouching for its signed plugins.
    paths = [META_FILE, CATALOG_FILE, catalog_feed.KEY_FILE_PATH]
    for channel in (catalog_feed.CHANNEL_MAIN, catalog_feed.CHANNEL_BETA):
        paths.extend((catalog_feed.SIGNED_CATALOG_PATH.format(channel=channel),
                      catalog_feed.SIGNED_ENVELOPE_PATH.format(channel=channel)))
    roots = tuple(sorted(catalog_feed.trusted_roots().items()))
    return str(root), roots, tuple(identity(path) for path in paths)


def _checked_read(root: Path, files: dict[str, str], path: str) -> bytes | None:
    """One listed snapshot file, checked against floor.json, or None when floor.json does not list it."""
    expected = files.get(path)
    if expected is None:
        return None
    data = _read_regular_file(root, path)
    if hashlib.sha256(data).hexdigest() != expected:
        raise FloorIntegrityError(f"snapshot file {path} does not match floor.json")
    return data


def _verified_signed_feed(root: Path, branch: str, commit: str, files: dict[str, str]) -> bytes | None:
    """The signed catalog's bytes when the snapshot carries a signed feed that verifies, else None.

    The check is the baked one: signature, key chain, revocations and channel against the roots this
    release ships. Anything short of that, and a feed built from another commit than the snapshot's
    files, leaves the snapshot seeding providers only. The reason is logged."""
    channel = catalog_feed.feed_channel(branch)
    if channel is None:
        return None
    body_path = catalog_feed.SIGNED_CATALOG_PATH.format(channel=channel)
    envelope_path = catalog_feed.SIGNED_ENVELOPE_PATH.format(channel=channel)
    if body_path not in files and envelope_path not in files:
        logger.info("The bundled provider catalog carries no signed feed, so it seeds providers only")
        return None
    try:
        body = _checked_read(root, files, body_path)
        envelope = _checked_read(root, files, envelope_path)
        key_file = _checked_read(root, files, catalog_feed.KEY_FILE_PATH)
    except FloorError as error:
        logger.warning("The bundled signed catalog is not used, so the bundled catalog seeds providers only: %s", error)
        return None
    if body is None:
        logger.warning("The bundled signed catalog is not used, so the bundled catalog seeds providers only: "
                       "its signature is there but the catalog is not")
        return None
    result = catalog_feed.verify_baked(body, envelope, key_file, channel=channel)
    if not result.ok:
        logger.warning(
            "The bundled signed catalog is not used, so the bundled catalog seeds providers only: %s (%s)",
            result.status.value, result.detail)
        return None
    try:
        document = json.loads(body)
    except ValueError:
        document = None
    source_commit = document.get("source_commit") if isinstance(document, dict) else None
    if source_commit != commit:
        logger.warning("The bundled signed catalog is not used, so the bundled catalog seeds providers only: "
                       "it was built from a different commit than the snapshot's files")
        return None
    if not isinstance(document.get("providers"), list) or not document["providers"]:
        logger.warning("The bundled signed catalog is not used, so the bundled catalog seeds providers only: "
                       "it has no providers list")
        return None
    try:
        revocation.parse(document.get(revocation.FIELD_NAME))
    except revocation.RevocationFormatError as error:
        logger.warning("The bundled signed catalog is not used, so the bundled catalog seeds providers only: %s", error)
        return None
    return body


def load_floor(root: str | os.PathLike[str] | None = None) -> Floor | None:
    """The snapshot, or None when there is no snapshot directory to read.

    A directory that is there but does not check out raises FloorError. The
    result is cached for as long as floor.json is unchanged and the trusted roots
    are the same. catalog.json and the signed feed (its files hashed, its
    signature verified in baked mode) are checked once, when the snapshot is
    loaded; every bundle file is hashed on each read."""
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
    signed_bytes = _verified_signed_feed(resolved, branch, commit, files)
    floor = Floor(resolved, repo, branch, commit, files, catalog_bytes, signed_bytes)
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
    therefore never replaced by the older one baked into the image.

    The seed is the snapshot's signed catalog, providers and plugins, when it
    verified (see the module note), and otherwise the providers of
    ``catalog.json`` with any plugins list stripped: an unsigned list is never
    seeded. The commit is the snapshot's, which is the commit the signed
    catalog was built from.
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
    signed = floor.signed_catalog()
    if signed is not None:
        keys = ("schema_version", "name", "providers", "plugins")
        return {key: signed[key] for key in keys if key in signed}, floor.commit
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
    source["catalog_status"] = CATALOG_STATUS_BUNDLED


def clear_seed(source: dict[str, Any]) -> None:
    """Forget the seed marker once a fetched catalog has replaced the seeded one."""
    source.pop("floor_seed", None)
    source.pop("catalog_status", None)


def bundle_file(manifest: Any, relative_path: str) -> bytes | None:
    """The bytes of one bundle file from the snapshot, or None when the snapshot
    does not hold this manifest's file (another repository, another commit, a
    file it never carried, or no usable snapshot).

    A file the snapshot lists but cannot serve (missing, unreadable, too large
    or not a regular file) is logged and answered with None too. A file whose
    bytes no longer match floor.json raises FloorIntegrityError, which fails the
    install like any other mismatching bundle file. The caller still checks the result against the
    manifest's own pins."""
    if getattr(manifest, "source_repo", None) != OFFICIAL_CATALOG_REPO:
        return None
    floor = _usable_floor()
    if floor is None or manifest.source_commit != floor.commit:
        return None
    path = "/".join(part for part in (manifest.source_path, relative_path) if part)
    if path in (CATALOG_FILE, META_FILE) or path not in floor.files:
        return None
    try:
        return floor.read(path)
    except FloorIntegrityError:
        raise
    except FloorError as error:
        logger.warning("The bundled provider catalog does not serve %s: %s", path, error)
        return None

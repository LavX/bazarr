# coding=utf-8
from __future__ import annotations

import hashlib

from pathlib import Path

from .manifest import ValidatedManifest


class BundleValidationError(ValueError):
    """Raised when an extracted provider bundle does not match its manifest."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_sha256(manifest: ValidatedManifest, root: str | Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for relative_path in sorted(manifest.files):
        file_path = root / relative_path
        data = file_path.read_bytes()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def verify_bundle_tree(manifest: ValidatedManifest, root: str | Path) -> None:
    root = Path(root).resolve()

    for relative_path, expected_sha256 in manifest.files.items():
        file_path = root / relative_path
        try:
            resolved = file_path.resolve(strict=False)
        except OSError as error:
            raise BundleValidationError(f"invalid bundle path: {relative_path}") from error

        if root not in (resolved, *resolved.parents):
            raise BundleValidationError(f"path escapes bundle root: {relative_path}")
        if file_path.is_symlink():
            raise BundleValidationError(f"symlink not allowed: {relative_path}")
        if not file_path.is_file():
            raise BundleValidationError(f"missing bundle file: {relative_path}")
        if file_path.suffix != ".py":
            raise BundleValidationError(f"only .py files are allowed: {relative_path}")

        actual = _sha256_file(file_path)
        if actual != expected_sha256:
            raise BundleValidationError(f"SHA256 mismatch for {relative_path}")

    actual_bundle_sha256 = bundle_sha256(manifest, root)
    if actual_bundle_sha256 != manifest.bundle_sha256:
        raise BundleValidationError("bundle SHA256 mismatch")

    declared = set(manifest.files)
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise BundleValidationError(f"symlink not allowed: {relative}")
        if path.is_file() and path.suffix != ".py" and relative != "provider.json":
            raise BundleValidationError(f"unexpected bundle file: {relative}")
        if path.is_file() and path.suffix == ".py" and relative not in declared:
            raise BundleValidationError(f"undeclared bundle file: {relative}")

# coding=utf-8
from __future__ import annotations

import re

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from . import PROVIDER_HUB_API_VERSION

_HEX_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_HEX_COMMIT_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_EXACT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.!+~-]*$")
_CONFIG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENTRY_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_CONFIG_TYPES = {"string", "boolean", "number", "integer"}
_DISALLOWED_SCHEMA_KEYS = {"$ref", "oneOf", "anyOf", "allOf", "not", "items"}


class ManifestValidationError(ValueError):
    """Raised when a Provider Hub manifest violates V1 policy."""


@dataclass(frozen=True)
class DependencyRequirement:
    name: str
    version: str
    hashes: tuple[str, ...]

    @property
    def pip_line(self) -> str:
        hashes = " ".join(f"--hash={item}" for item in self.hashes)
        return f"{self.name}=={self.version} {hashes}".strip()


@dataclass(frozen=True)
class ValidatedManifest:
    raw: dict[str, Any]
    provider_id: str
    name: str
    version: str
    api_version: str
    entry_module: str
    entry_class: str
    config_schema: dict[str, Any]
    secret_fields: tuple[str, ...]
    supported_media: tuple[str, ...]
    languages: tuple[str, ...]
    files: dict[str, str]
    bundle_sha256: str
    source_repo: str
    source_ref: str
    source_commit: str
    source_path: str
    trusted: bool
    dependency_requirements: tuple[DependencyRequirement, ...]


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{field} must be an object")
    return value


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{field} must be a non-empty string")
    return value


def _validate_sha256(value: str, field: str) -> str:
    if not _HEX_SHA256_RE.match(value):
        raise ManifestValidationError(f"{field} must be a SHA256 hex digest")
    return value.lower()


def _validate_provider_id(value: str, built_in_provider_ids: set[str]) -> str:
    if not _PROVIDER_ID_RE.match(value):
        raise ManifestValidationError("provider_id must use lowercase provider id syntax")
    if value in built_in_provider_ids:
        raise ManifestValidationError(f"provider_id {value!r} shadows a built-in provider")
    return value


def _validate_file_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ManifestValidationError("manifest files must use non-empty relative paths")
    if path.startswith("/") or "\\" in path:
        raise ManifestValidationError(f"unsafe file path: {path}")

    parsed = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in parsed.parts):
        raise ManifestValidationError(f"unsafe file path: {path}")
    if parsed.suffix != ".py":
        raise ManifestValidationError(f"only .py files are allowed: {path}")
    return parsed.as_posix()


def _validate_source_path(path: Any) -> str:
    if path in (None, ""):
        return ""
    if not isinstance(path, str):
        raise ManifestValidationError("source.path must be a relative path")
    if path.startswith("/") or "\\" in path:
        raise ManifestValidationError(f"unsafe source path: {path}")

    parsed = PurePosixPath(path)
    if any(part in ("", ".", "..") for part in parsed.parts):
        raise ManifestValidationError(f"unsafe source path: {path}")
    return parsed.as_posix()


def _validate_config_schema(config_schema: dict[str, Any], secret_fields: list[str]) -> None:
    if config_schema.get("type") != "object":
        raise ManifestValidationError("config_schema.type must be object")
    if any(key in config_schema for key in _DISALLOWED_SCHEMA_KEYS):
        raise ManifestValidationError("config_schema contains unsupported composition keys")

    properties = config_schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ManifestValidationError("config_schema.properties must be an object")

    required = config_schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ManifestValidationError("config_schema.required must be a string list")

    for key, field in properties.items():
        if not isinstance(key, str) or not _CONFIG_KEY_RE.match(key):
            raise ManifestValidationError(f"config key is invalid: {key}")
        if not isinstance(field, dict):
            raise ManifestValidationError(f"config field {key} must be an object")
        if any(item in field for item in _DISALLOWED_SCHEMA_KEYS | {"properties"}):
            raise ManifestValidationError(f"config field {key} contains unsupported nested schema")
        if field.get("type") not in _SUPPORTED_CONFIG_TYPES:
            raise ManifestValidationError(f"config field {key} has unsupported type")
        enum = field.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or not enum:
                raise ManifestValidationError(f"config field {key} enum must be a non-empty list")
            if any(not isinstance(item, (str, int, float, bool)) for item in enum):
                raise ManifestValidationError(f"config field {key} enum must use scalar values")

    for key in required:
        if key not in properties:
            raise ManifestValidationError(f"required config field is not declared: {key}")

    for key in secret_fields:
        field = properties.get(key)
        if field is None:
            raise ManifestValidationError(f"secret field is not declared in config_schema: {key}")
        if field.get("type") != "string":
            raise ManifestValidationError(f"secret field must be a string: {key}")


def _validate_dependencies(dependencies: dict[str, Any]) -> tuple[DependencyRequirement, ...]:
    requirements = dependencies.get("requirements", [])
    if requirements is None:
        requirements = []
    if not isinstance(requirements, list):
        raise ManifestValidationError("dependencies.requirements must be a list")

    validated: list[DependencyRequirement] = []
    for item in requirements:
        if not isinstance(item, dict):
            raise ManifestValidationError("dependency must be an object")

        name = _require_text(item.get("name"), "dependency.name")
        version = _require_text(item.get("version"), "dependency.version")
        hashes = item.get("hashes")

        if ";" in name or name.startswith(("git+", "http:", "https:", "file:")):
            raise ManifestValidationError(f"unsafe dependency name: {name}")
        if not _PACKAGE_RE.match(name):
            raise ManifestValidationError(f"unsafe dependency name: {name}")
        if not _EXACT_VERSION_RE.match(version) or any(op in version for op in ("<", ">", "=", "*")):
            raise ManifestValidationError(f"dependency {name} must be pinned to an exact version")
        if not isinstance(hashes, list) or not hashes:
            raise ManifestValidationError(f"dependency {name} must include at least one SHA256 hash")

        normalized_hashes = []
        for digest in hashes:
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise ManifestValidationError(f"dependency {name} has an invalid hash")
            _validate_sha256(digest.removeprefix("sha256:"), f"dependency {name} hash")
            normalized_hashes.append(digest.lower())

        validated.append(
            DependencyRequirement(
                name=name,
                version=version,
                hashes=tuple(normalized_hashes),
            )
        )

    return tuple(validated)


def validate_manifest(manifest: dict[str, Any], built_in_provider_ids: set[str] | None = None) -> ValidatedManifest:
    built_in_provider_ids = built_in_provider_ids or set()
    manifest = _require_mapping(manifest, "manifest")

    schema_version = manifest.get("schema_version")
    if schema_version != 1:
        raise ManifestValidationError("schema_version must be 1")

    provider_id = _validate_provider_id(
        _require_text(manifest.get("provider_id"), "provider_id"),
        built_in_provider_ids,
    )
    name = _require_text(manifest.get("name"), "name")
    version = _require_text(manifest.get("version"), "version")
    # version becomes a filesystem path component (bundle dir, venv dir);
    # reject anything that could escape via "../" or absolute paths before
    # we touch the disk.
    if (
        not _EXACT_VERSION_RE.match(version)
        or version in (".", "..")
        or any(sep in version for sep in ("/", "\\"))
    ):
        raise ManifestValidationError(
            "version must be a safe identifier (letters, digits, '.', '_', '-', '+', '!', '~')"
        )
    api_version = _require_text(manifest.get("api_version"), "api_version")
    if api_version != PROVIDER_HUB_API_VERSION:
        raise ManifestValidationError(f"unsupported Provider Hub API version: {api_version}")

    entry_module = _require_text(manifest.get("entry_module"), "entry_module")
    if not _ENTRY_MODULE_RE.match(entry_module):
        raise ManifestValidationError("entry_module must be a simple Python module name")
    entry_class = _require_text(manifest.get("entry_class"), "entry_class")
    config_schema = _require_mapping(manifest.get("config_schema"), "config_schema")

    secret_fields = manifest.get("secret_fields", [])
    if not isinstance(secret_fields, list) or not all(isinstance(item, str) for item in secret_fields):
        raise ManifestValidationError("secret_fields must be a string list")
    _validate_config_schema(config_schema, secret_fields)

    supported_media = manifest.get("supported_media", [])
    if not isinstance(supported_media, list) or not supported_media:
        raise ManifestValidationError("supported_media must be a non-empty string list")
    if any(item not in ("movie", "episode") for item in supported_media):
        raise ManifestValidationError("supported_media contains unsupported media type")

    languages = manifest.get("languages", [])
    if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
        raise ManifestValidationError("languages must be a string list")

    raw_files = _require_mapping(manifest.get("files"), "files")
    files = {}
    for path, digest in raw_files.items():
        safe_path = _validate_file_path(path)
        files[safe_path] = _validate_sha256(_require_text(digest, f"files.{path}"), f"files.{path}")
    if not files:
        raise ManifestValidationError("files must not be empty")
    entry_file = _validate_file_path(f"{entry_module}.py")
    if entry_file not in files:
        raise ManifestValidationError(f"entry_module must resolve to a declared file: {entry_file}")

    bundle_sha256 = _validate_sha256(
        _require_text(manifest.get("bundle_sha256"), "bundle_sha256"),
        "bundle_sha256",
    )

    source = _require_mapping(manifest.get("source"), "source")
    source_type = source.get("type")
    if source_type != "github":
        raise ManifestValidationError("source.type must be github")
    source_repo = _require_text(source.get("repo"), "source.repo")
    if "/" not in source_repo or source_repo.count("/") != 1:
        raise ManifestValidationError("source.repo must be owner/repo")
    source_ref = _require_text(source.get("ref"), "source.ref")
    source_commit = _require_text(source.get("commit"), "source.commit")
    if not _HEX_COMMIT_RE.match(source_commit):
        raise ManifestValidationError("source.commit must be an immutable commit SHA")
    source_path = _validate_source_path(source.get("path") or source.get("bundle_path") or "")
    trusted = bool(source.get("trusted", False))

    dependencies = _require_mapping(manifest.get("dependencies", {}), "dependencies")
    dependency_requirements = _validate_dependencies(dependencies)

    return ValidatedManifest(
        raw=dict(manifest),
        provider_id=provider_id,
        name=name,
        version=version,
        api_version=api_version,
        entry_module=entry_module,
        entry_class=entry_class,
        config_schema=dict(config_schema),
        secret_fields=tuple(secret_fields),
        supported_media=tuple(supported_media),
        languages=tuple(languages),
        files=files,
        bundle_sha256=bundle_sha256,
        source_repo=source_repo,
        source_ref=source_ref,
        source_commit=source_commit.lower(),
        source_path=source_path,
        trusted=trusted,
        dependency_requirements=dependency_requirements,
    )

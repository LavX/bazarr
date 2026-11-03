# coding=utf-8
"""Manifest v2: the envelope every plugin kind shares.

Schema 2 adds what the platform needs to decide whether, where and how a
plugin can run, and what it depends on. Schema-1 provider manifests stay
valid: they are lifted into the same envelope with the values they have
always meant, a subtitle provider shipped as a Python bundle and run as a
stdio worker on any platform.

Only the envelope is checked here. The fields a kind adds (a provider's
entry module and config schema, for example) are the kind's to validate.
Keys this host does not know are kept in the manifest and ignored.

The compatibility fields (``kind_api``, ``min_host_version``, ``platforms``,
``python_tags`` and ``runtime``) are checked when the manifest is read. Install
and selection do not yet check them against the running host, so declaring
one does not keep a plugin off a host that lacks it.
"""
from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any

from .manifest import ManifestValidationError

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
KIND_API_RE = re.compile(r"^bazarr\.[a-z][a-z0-9_]*\.v(?P<major>[1-9][0-9]*)$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\.v[1-9][0-9]*$")
SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# A fixed origin a plugin's code contacts, written in full as scheme, host and
# port (https://openrouter.ai:443) so that a change in any of the three is a
# new origin. A destination the user types is a settings field instead.
ENDPOINT_RE = re.compile(
    r"^(?P<scheme>https|http)://"
    r"(?P<host>(?=.{1,253}:)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})"
    r":(?P<port>[1-9][0-9]{0,4})$"
)
SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

ANY_PLATFORM = "any"
PLATFORMS = frozenset({
    ANY_PLATFORM,
    "linux-x86_64",
    "linux-aarch64",
    "linux-armv7l",
    "linux-i686",
    "linux-musl-x86_64",
    "linux-musl-aarch64",
    "windows-x86_64",
    "macos-x86_64",
    "macos-arm64",
})

PYTHON_BUNDLE = "python"
STDIO_WORKER = "worker"
RUNTIME_COMBINATIONS = frozenset({
    (PYTHON_BUNDLE, STDIO_WORKER),
    (PYTHON_BUNDLE, "daemon"),
    ("none", "external"),
})
PERMISSIONS = frozenset({"network", "media_read", "data_dir", "binaries", "docker", "interactive_login"})
SETTING_TYPES = frozenset({
    "text", "secret", "number", "integer", "toggle", "choice",
    "list", "key_list", "orderable_list", "options",
})
HOST_CAPABILITY_PREFIX = "host."
# CPython compatibility tags of the wheels an entry's closure ships (cp312,
# cp313, cp314), or py3 when every wheel is pure Python.
PYTHON_TAG_RE = re.compile(r"^(?:cp3[0-9]{1,2}|py3)$")


@dataclass(frozen=True)
class Extends:
    plugin_id: str
    capability: str


@dataclass(frozen=True)
class PluginManifest:
    raw: dict[str, Any]
    schema_version: int
    plugin_id: str
    kind: str
    kind_api: str
    name: str
    version: str
    min_host_version: str | None
    platforms: tuple[str, ...]
    artifact: str
    process: str
    permissions: tuple[str, ...]
    requires: tuple[str, ...]
    provides: tuple[str, ...]
    extends: Extends | None
    settings_version: int | None
    settings_fields: tuple[dict[str, Any], ...]
    endpoints: tuple[str, ...] = ()
    python_tags: tuple[str, ...] = ()
    # Per back-end type, for a plugin that serves several (settings.backend_types):
    # (type id, origins) and (type id, fields), sorted by type id.
    backend_endpoints: tuple[tuple[str, tuple[str, ...]], ...] = ()
    backend_fields: tuple[tuple[str, tuple[dict[str, Any], ...]], ...] = ()

    @property
    def kind_api_major(self) -> int:
        return int(KIND_API_RE.match(self.kind_api).group("major"))

    @property
    def all_endpoints(self) -> tuple[str, ...]:
        """Every fixed origin the plugin's code contacts: the top-level list and each back-end type's."""
        origins = set(self.endpoints)
        for _type, listed in self.backend_endpoints:
            origins.update(listed)
        return tuple(sorted(origins))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{field} must be a non-empty string")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestValidationError(f"{field} must be a list of strings")
    if len(set(value)) != len(value):
        raise ManifestValidationError(f"{field} must not repeat a value")
    return list(value)


def _capabilities(value: Any, field: str) -> tuple[str, ...]:
    items = _string_list(value, field)
    for item in items:
        if not CAPABILITY_RE.match(item):
            raise ManifestValidationError(
                f"{field} entry {item!r} must look like name.v1 (lowercase words joined by dots, then a major)"
            )
    return tuple(items)


def _validate_version_text(value: Any, field: str) -> str:
    text = _text(value, field)
    if not SEMVER_RE.match(text):
        raise ManifestValidationError(f"{field} must be a semantic version such as 2.8.0")
    return text


def _platforms(value: Any) -> tuple[str, ...]:
    if value is None:
        return (ANY_PLATFORM,)
    items = _string_list(value, "platforms")
    if not items:
        raise ManifestValidationError("platforms must not be empty")
    unknown = sorted(set(items) - PLATFORMS)
    if unknown:
        raise ManifestValidationError(f"platforms names an unknown platform: {', '.join(unknown)}")
    if ANY_PLATFORM in items and len(items) > 1:
        raise ManifestValidationError("platforms lists 'any' together with specific platforms")
    return tuple(items)


def _runtime(value: Any) -> tuple[str, str]:
    if value is None:
        return PYTHON_BUNDLE, STDIO_WORKER
    if not isinstance(value, dict):
        raise ManifestValidationError("runtime must be an object")
    artifact = _text(value.get("artifact"), "runtime.artifact")
    process = _text(value.get("process"), "runtime.process")
    if (artifact, process) not in RUNTIME_COMBINATIONS:
        raise ManifestValidationError(f"runtime {artifact} run as {process} is not a supported combination")
    return artifact, process


def _permissions(value: Any) -> tuple[str, ...]:
    items = _string_list(value, "permissions")
    unknown = sorted(set(items) - PERMISSIONS)
    if unknown:
        raise ManifestValidationError(f"permissions names an unknown permission: {', '.join(unknown)}")
    return tuple(sorted(items))


def _python_tags(value: Any) -> tuple[str, ...]:
    items = _string_list(value, "python_tags")
    for item in items:
        if not PYTHON_TAG_RE.match(item):
            raise ManifestValidationError(f"python_tags entry {item!r} must be a tag such as cp314 or py3")
    return tuple(items)


def _endpoints(value: Any) -> tuple[str, ...]:
    items = _string_list(value, "endpoints")
    for item in items:
        match = ENDPOINT_RE.match(item)
        if not match or int(match.group("port")) > 65535:
            raise ManifestValidationError(
                f"endpoints entry {item!r} must be an origin written as scheme://host:port, "
                "such as https://openrouter.ai:443")
    return tuple(sorted(items))


def _extends(value: Any) -> Extends | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManifestValidationError("extends must be an object with id and capability")
    plugin_id = _text(value.get("id"), "extends.id")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ManifestValidationError("extends.id must be a plugin id")
    capability = _text(value.get("capability"), "extends.capability")
    if not CAPABILITY_RE.match(capability) or capability.startswith(HOST_CAPABILITY_PREFIX):
        raise ManifestValidationError("extends.capability must be a plugin capability such as ai_translator.backend.v1")
    return Extends(plugin_id=plugin_id, capability=capability)


def _field_list(fields: Any, where: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(fields, list):
        raise ManifestValidationError(f"{where} must be a list")
    seen = set()
    for index, item in enumerate(fields):
        if not isinstance(item, dict):
            raise ManifestValidationError(f"{where}[{index}] must be an object")
        key = item.get("key")
        if not isinstance(key, str) or not SETTING_KEY_RE.match(key):
            raise ManifestValidationError(f"{where}[{index}].key is invalid")
        if key in seen:
            raise ManifestValidationError(f"settings field {key} is declared twice")
        seen.add(key)
        if item.get("type") not in SETTING_TYPES:
            raise ManifestValidationError(f"settings field {key} has an unsupported type")
    return tuple(dict(item) for item in fields)


def _settings(manifest: dict[str, Any]) -> tuple[
        int | None, tuple[dict[str, Any], ...],
        tuple[tuple[str, tuple[str, ...]], ...], tuple[tuple[str, tuple[dict[str, Any], ...]], ...]]:
    """settings_version, the plugin's fields, and, per back-end type, its origins and fields."""
    settings = manifest.get("settings")
    settings_version = manifest.get("settings_version")
    if settings is None:
        if settings_version is not None:
            raise ManifestValidationError("settings_version is set but there are no settings")
        return None, (), (), ()
    if not isinstance(settings, dict):
        raise ManifestValidationError("settings must be an object")
    if isinstance(settings_version, bool) or not isinstance(settings_version, int) or settings_version < 1:
        raise ManifestValidationError("settings_version must be a positive integer when settings are declared")
    fields = _field_list(settings.get("fields"), "settings.fields")
    backend_endpoints = []
    backend_fields = []
    types = settings.get("backend_types")
    if types is not None:
        if not isinstance(types, dict):
            raise ManifestValidationError("settings.backend_types must be an object")
        for type_id in sorted(types):
            definition = types[type_id]
            if not isinstance(type_id, str) or not SETTING_KEY_RE.match(type_id) or not isinstance(definition, dict):
                raise ManifestValidationError(f"settings.backend_types.{type_id} must be an object under a type id")
            backend_endpoints.append((type_id, _endpoints(definition.get("endpoints"))))
            backend_fields.append((type_id, _field_list(
                definition.get("fields") or [], f"settings.backend_types.{type_id}.fields")))
    return settings_version, fields, tuple(backend_endpoints), tuple(backend_fields)


def parse_plugin_manifest(manifest: Any) -> PluginManifest:
    """Validate the shared envelope of a schema 1 or 2 manifest."""
    from .kinds import SUBTITLE_PROVIDER
    from .kinds.subtitle_provider import LEGACY_PROVIDER_HUB_API, SUBTITLE_PROVIDER_KIND_API

    if not isinstance(manifest, dict):
        raise ManifestValidationError("manifest must be an object")
    schema_version = manifest.get("schema_version")
    if schema_version == 1:
        api_version = manifest.get("api_version")
        if api_version != LEGACY_PROVIDER_HUB_API:
            raise ManifestValidationError(f"unsupported Provider Hub API version: {api_version}")
        plugin_id = _text(manifest.get("provider_id"), "provider_id")
        if not PLUGIN_ID_RE.match(plugin_id):
            raise ManifestValidationError("provider_id must use lowercase provider id syntax")
        return PluginManifest(
            raw=dict(manifest),
            schema_version=1,
            plugin_id=plugin_id,
            kind=SUBTITLE_PROVIDER,
            kind_api=SUBTITLE_PROVIDER_KIND_API,
            name=_text(manifest.get("name"), "name"),
            version=_text(manifest.get("version"), "version"),
            min_host_version=None,
            platforms=(ANY_PLATFORM,),
            artifact=PYTHON_BUNDLE,
            process=STDIO_WORKER,
            permissions=(),
            requires=(),
            provides=(),
            extends=None,
            settings_version=None,
            settings_fields=(),
        )
    if schema_version != 2:
        raise ManifestValidationError("schema_version must be 1 or 2")

    plugin_id = _text(manifest.get("id"), "id")
    if not PLUGIN_ID_RE.match(plugin_id):
        raise ManifestValidationError("id must use lowercase plugin id syntax")
    legacy_id = manifest.get("provider_id")
    if legacy_id is not None and legacy_id != plugin_id:
        raise ManifestValidationError("provider_id, when present, must equal id")
    kind = _text(manifest.get("kind"), "kind")
    if not KIND_RE.match(kind):
        raise ManifestValidationError("kind must use lowercase kind syntax")
    kind_api = _text(manifest.get("kind_api"), "kind_api")
    if not KIND_API_RE.match(kind_api):
        raise ManifestValidationError("kind_api must look like bazarr.<kind>.v<major>")
    min_host_version = manifest.get("min_host_version")
    if min_host_version is not None:
        min_host_version = _validate_version_text(min_host_version, "min_host_version")
    artifact, process = _runtime(manifest.get("runtime"))
    settings_version, settings_fields, backend_endpoints, backend_fields = _settings(manifest)
    provides = _capabilities(manifest.get("provides"), "provides")
    if any(item.startswith(HOST_CAPABILITY_PREFIX) for item in provides):
        raise ManifestValidationError("a plugin cannot provide a host capability")
    extends = _extends(manifest.get("extends"))
    if extends is not None and extends.plugin_id == plugin_id:
        raise ManifestValidationError("a plugin cannot extend itself")
    return PluginManifest(
        raw=dict(manifest),
        schema_version=2,
        plugin_id=plugin_id,
        kind=kind,
        kind_api=kind_api,
        name=_text(manifest.get("name"), "name"),
        version=_text(manifest.get("version"), "version"),
        min_host_version=min_host_version,
        platforms=_platforms(manifest.get("platforms")),
        artifact=artifact,
        process=process,
        permissions=_permissions(manifest.get("permissions")),
        requires=_capabilities(manifest.get("requires"), "requires"),
        provides=provides,
        extends=extends,
        settings_version=settings_version,
        settings_fields=settings_fields,
        endpoints=_endpoints(manifest.get("endpoints")),
        python_tags=_python_tags(manifest.get("python_tags")),
        backend_endpoints=backend_endpoints,
        backend_fields=backend_fields,
    )

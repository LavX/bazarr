# coding=utf-8
"""Plugin kinds: the contracts plugins implement.

A kind names the manifests it accepts (its kind_api majors), says when a new
release attaches (at the next restart, or live), and owns every side effect
that installing, enabling, configuring or removing one of its plugins has on
the rest of Bazarr+. The shared Provider Hub core calls these hooks and never
does a kind's work itself, so a new kind is a new module here, never a change
to the core.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from ..manifest import ValidatedManifest

SUBTITLE_PROVIDER = "subtitle_provider"

ATTACH_ON_RESTART = "restart"
ATTACH_LIVE = "live"


class UnknownKindError(ValueError):
    """Raised when a manifest or a stored row names a kind this host does not define."""


class Kind:
    """Base contract. Subclasses override what their kind needs."""

    name: str = ""
    kind_apis: frozenset[str] = frozenset()
    attach_policy: str = ATTACH_ON_RESTART
    # What the job list calls one of these: "Installing provider X".
    noun: str = "plugin"
    # Capability id to the (config key, value shape) it is delivered under.
    # Empty for kinds that deliver no capability; the host reads it when it
    # merges a resolved capability into a consumer's runtime config.
    capability_delivery: Mapping[str, tuple[str, str]] = MappingProxyType({})

    def on_installed(self, plugin_id: str) -> bool:
        """A first install of this plugin was staged.

        True when the host's state holds what this kind needs, False when it
        could not be saved, which the core reports as a failed action. The
        subtitle provider kind returns what ``set_bazarr_provider_enabled``
        says, as the core's own helper did before kinds existed.
        """
        return True

    def on_removed(self, plugin_id: str) -> bool:
        """This plugin was removed or its removal was staged. Same return rule."""
        return True

    def set_enabled(self, installation: dict[str, Any], enabled: bool) -> bool:
        """Turn the plugin on or off. Same return rule."""
        installation["enabled"] = bool(enabled)
        return True

    def is_enabled(self, installation: dict[str, Any]) -> bool:
        return bool(installation.get("enabled", True))

    def on_config_saved(self, plugin_id: str) -> None:
        """The stored configuration of this plugin changed."""

    def validate_manifest(self, manifest: dict[str, Any], trusted: bool) -> "ValidatedManifest":
        """Validate this kind's manifest and return what the installer stages.

        Raises ManifestValidationError. The envelope itself was checked before
        this is called. A kind with no installer yet refuses, so a catalog
        entry of that kind is listed with the reason and never installs.
        """
        from ..manifest import ManifestValidationError

        raise ManifestValidationError(f"this Bazarr+ cannot install {self.name} plugins yet")

    def smoke_test(self, validated: "ValidatedManifest", bundle_path, python_path) -> None:
        """Start the staged plugin once to prove it loads. The default has nothing to start."""


_KINDS: dict[str, Kind] = {}


def register_kind(kind: Kind) -> Kind:
    if not kind.name:
        raise ValueError("a kind needs a name")
    if kind.attach_policy not in (ATTACH_ON_RESTART, ATTACH_LIVE):
        raise ValueError(f"unknown attach policy for kind {kind.name}: {kind.attach_policy}")
    for kind_api in kind.kind_apis:
        owner = kind_for_api(kind_api)
        if owner is not None and owner.name != kind.name:
            raise ValueError(f"kind_api {kind_api} already belongs to kind {owner.name}")
    _KINDS[kind.name] = kind
    return kind


def unregister_kind(name: str) -> None:
    if name == SUBTITLE_PROVIDER:
        raise ValueError("the subtitle provider kind cannot be removed")
    _KINDS.pop(name, None)


def get_kind(name: str) -> Kind:
    try:
        return _KINDS[name]
    except KeyError:
        raise UnknownKindError(f"unknown plugin kind: {name}") from None


def known_kinds() -> tuple[str, ...]:
    return tuple(sorted(_KINDS))


def kind_for_api(kind_api: str) -> Kind | None:
    for kind in _KINDS.values():
        if kind_api in kind.kind_apis:
            return kind
    return None


def kind_of(item: Any) -> str:
    """The kind of an installation row or installation object.

    Rows written before kinds existed carry no kind and are subtitle providers.
    """
    if isinstance(item, dict):
        value = item.get("kind")
    else:
        value = getattr(item, "kind", None)
    return str(value) if value else SUBTITLE_PROVIDER


def catalog_kind_of(entry: Any) -> str:
    """The kind of a catalog entry, which keeps its kind in its manifest."""
    manifest = entry.get("manifest") if isinstance(entry, dict) else None
    value = manifest.get("kind") if isinstance(manifest, dict) else None
    return str(value) if value else SUBTITLE_PROVIDER


def of_kind(items: Iterable[Any], kind: str | None) -> list[Any]:
    """Filter rows or objects by kind. ``kind=None`` keeps every kind."""
    if kind is None:
        return list(items)
    return [item for item in items if kind_of(item) == kind]


# Registers the built-in kind. Imported last because the module subclasses Kind.
from . import subtitle_provider as _subtitle_provider  # noqa: E402,F401

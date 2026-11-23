# coding=utf-8
"""Plugin kinds: the contracts plugins implement.

A kind names the manifests it accepts (its kind_api majors), says when a new
release attaches (at the next restart, or live) and whether a release it runs
can be stopped without one (``detach_policy``), and owns every side effect
that installing, enabling, configuring or removing one of its plugins has on
the rest of Bazarr+. The shared Provider Hub core calls is_enabled,
set_enabled, on_config_saved, on_installed and on_removed, and validate_manifest
when a catalog refresh reads a plugin. It also calls validate_manifest and
smoke_test when it stages or activates a release, repairs an environment or
reconciles environments at boot.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from ..manifest import ValidatedManifest

SUBTITLE_PROVIDER = "subtitle_provider"
TRANSLATOR = "translator"
SERVICE = "service"

ATTACH_ON_RESTART = "restart"
ATTACH_LIVE = "live"

# Whether a running release of the kind can be stopped and taken out of use while Bazarr+ runs. Separate from the
# attach policy: a kind may attach new releases at the next restart and still stop one now (disable, remove and
# revoke act live, install and update wait for the restart). A kind that detaches on restart keeps the restart cue.
DETACH_ON_RESTART = "restart"
DETACH_LIVE = "live"


class UnknownKindError(ValueError):
    """Raised when a manifest or a stored row names a kind this host does not define."""


class Kind:
    """Base contract. Subclasses override what their kind needs."""

    name: str = ""
    kind_apis: frozenset[str] = frozenset()
    attach_policy: str = ATTACH_ON_RESTART
    detach_policy: str = DETACH_ON_RESTART
    # What the job list calls one of these: "Installing provider X".
    noun: str = "plugin"
    # Capability id to the (config key, value shape) it is delivered under.
    # Empty for kinds that deliver no capability; the host reads it when it
    # merges a resolved capability into a consumer's runtime config.
    capability_delivery: Mapping[str, tuple[str, str]] = MappingProxyType({})

    def on_installed(self, plugin_id: str, missing_required: Sequence[str] = ()) -> bool:
        """A first install of this plugin was staged.

        ``missing_required`` names the ``config_schema.required`` fields that
        hold no value yet, computed once by the install.

        True when the host's state holds what this kind needs, False when it
        could not be saved, which the core reports as a failed action. The
        subtitle provider kind returns what ``set_bazarr_provider_enabled``
        says.
        """
        return True

    def on_removed(self, plugin_id: str) -> bool:
        """This plugin was removed or its removal was staged. Same return rule."""
        return True

    # Whether the kind has anything of a plugin's to keep when it is removed (``after_removed`` with ``keep_data``).
    keeps_data: bool = False

    def kept_data_reclaimed(self, plugin_id: str) -> None:
        """An install took over the data an earlier removal kept of this plugin, so it is in use again."""

    def discard_kept_data(self, plugin_id: str) -> None:
        """Delete what an earlier removal kept of this plugin, because the plugin about to be installed under the
        same id is not the one it was kept for."""

    def after_removed(self, plugin_id: str, keep_data: bool = False) -> None:
        """This plugin's removal has been saved. The place for cleanup that cannot be undone.

        ``keep_data`` is the person's answer to what happens to what the plugin stored: True keeps its saved
        settings and its data folder, so installing it again finds them, and False deletes them.

        ``on_removed`` runs before the removal is saved and may be rolled back by a failure; anything
        that deletes data belongs here instead. It cannot veto the removal, so it reports problems by
        logging them.
        """

    def on_activated(self, plugin_id: str, previous_manifest: dict[str, Any] | None,
                     manifest: dict[str, Any]) -> str | None:
        """A new release of this plugin has just become the active one (``previous_manifest`` is the one it
        replaced, or None for a first activation).

        The place to bring what the kind stores in line with the new manifest. The release is already active,
        so this cannot refuse it; it returns a short message when something needs the person's attention, which
        the host shows as the plugin's last error, or None.
        """
        return None

    def detach(self, plugin_id: str, *, withdraw: bool, grace: float | None = None,
               release_key: str | None = None) -> "list[Any]":
        """Stop serving this plugin's calls now. Only called for a kind whose ``detach_policy`` is live.

        New calls are refused from the moment this returns; the calls the release already took finish (or, with
        ``grace``, are cut once that many seconds have passed). ``withdraw`` is True when the release leaves for
        good (removed, revoked or replaced) and False when the plugin is only switched off and may be switched
        on again. ``release_key`` names the release being stopped, for a kind whose workers are told apart by it
        (an update that keeps the new release running).

        Returns the threads that end when everything the release held is gone, so the caller can finish what
        waits for that (retiring the release, deleting a removed row). A failure is raised: the caller falls
        back to the restart cue.
        """
        return []

    def removal_finished(self, plugin_id: str) -> None:
        """A live removal is complete: the release was detached, its calls are done and its row is gone. The place
        for what had to wait for the plugin to stop existing (a folder that was in use). Never raises."""

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

        Raises ManifestValidationError. A catalog refresh calls this hook for each
        schema 2 plugin it reads and stores nothing the hook refuses. A kind with
        no installer keeps this version, which refuses.
        """
        from ..manifest import ManifestValidationError

        raise ManifestValidationError(f"this Bazarr+ does not install {self.name} plugins")

    def smoke_test(self, validated: "ValidatedManifest", bundle_path, python_path,
                   timeout: float | None = None, *, data_dir=None) -> None:
        """Start the staged plugin once to prove it loads. The default has nothing to start.

        The core calls this hook when it stages a release, activates a staged release, repairs an
        environment and reconciles environments at boot. ``timeout``, when given, is what an install
        that runs on a time budget has left for the check. The subtitle provider kind runs the worker
        health check; a kind with nothing to start keeps this version.
        """


    def settle_data(self, plugin_id: str, timeout: float) -> bool:
        """Wait for the plugin's workers to stop writing to its active data folder."""
        return True


_KINDS: dict[str, Kind] = {}


def register_kind(kind: Kind) -> Kind:
    if not kind.name:
        raise ValueError("a kind needs a name")
    if kind.attach_policy not in (ATTACH_ON_RESTART, ATTACH_LIVE):
        raise ValueError(f"unknown attach policy for kind {kind.name}: {kind.attach_policy}")
    if kind.detach_policy not in (DETACH_ON_RESTART, DETACH_LIVE):
        raise ValueError(f"unknown detach policy for kind {kind.name}: {kind.detach_policy}")
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
    """A catalog entry takes its kind from a schema 2 manifest, else it is a subtitle provider."""
    manifest = entry.get("manifest") if isinstance(entry, dict) else None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        return SUBTITLE_PROVIDER
    value = manifest.get("kind")
    return str(value) if value else SUBTITLE_PROVIDER


def of_kind(items: Iterable[Any], kind: str | None) -> list[Any]:
    """Filter rows or objects by kind. ``kind=None`` keeps every kind."""
    if kind is None:
        return list(items)
    return [item for item in items if kind_of(item) == kind]


# Registers the built-in kinds. Imported last because the modules subclass Kind.
from . import subtitle_provider as _subtitle_provider  # noqa: E402,F401
from . import service as _service  # noqa: E402,F401

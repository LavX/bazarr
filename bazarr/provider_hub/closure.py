# coding=utf-8
"""The dependency closure of a plugin: what installing it also pulls in.

A manifest names what it needs in two ways. ``extends`` names the plugin it is an add-on to, by id and
by the capability that plugin provides. ``requires`` lists capabilities. Resolving a plugin's closure
walks both through the entries one catalog source lists and through what is already installed, and
answers three things: the plugins that would be installed with it (dependencies first, the plugin
last), what is already there and satisfies a need, and what does not resolve.

A capability no plugin provides anywhere is not a plugin's to supply: it names a host capability
(``host.ffmpeg.v1``) or an address or instance the user configures. Those are listed as external and
never block, because this module cannot tell whether the host has them. Binding them is not done here.

The functions read the state document and take what depends on the running host (is an entry usable
here?) as a callback, so this module imports nothing from the service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .manifest import ManifestValidationError
from .plugin_manifest import HOST_CAPABILITY_PREFIX, PluginManifest, parse_plugin_manifest
from .compatibility import select_entry

# A closure deeper than this is not a closure anyone wrote on purpose.
MAX_DEPTH = 12


@dataclass(frozen=True)
class Member:
    """One plugin the install would add: its catalog entry, its parsed manifest and why it is in."""

    entry: dict[str, Any]
    plugin: PluginManifest
    reason: str  # "root", "extends" or "requires"

    @property
    def plugin_id(self) -> str:
        return self.plugin.plugin_id


@dataclass
class Closure:
    members: list[Member] = field(default_factory=list)
    # Installed plugins that already satisfy a need: {"id", "version", "capability"}.
    satisfied_by: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return not self.unresolved

    @property
    def refusal(self) -> str | None:
        return self.unresolved[0] if self.unresolved else None


def _parse(manifest: Any) -> PluginManifest | None:
    try:
        return parse_plugin_manifest(manifest)
    except ManifestValidationError:
        return None
    except Exception:
        return None


def has_needs(manifest: Any) -> bool:
    """Whether a manifest names a dependency at all, which is the only case worth resolving."""
    return isinstance(manifest, dict) and bool(manifest.get("requires") or manifest.get("extends"))


def installed_plugins(state: dict[str, Any]) -> dict[str, tuple[PluginManifest, dict[str, Any]]]:
    """The plugins this host holds, by id: every installation that is not being removed."""
    found: dict[str, tuple[PluginManifest, dict[str, Any]]] = {}
    for plugin_id, row in (state.get("installations") or {}).items():
        if not isinstance(row, dict) or row.get("state") == "removed":
            continue
        manifest = row.get("manifest") if row.get("active_version") else row.get("staged_manifest")
        plugin = _parse(manifest)
        if plugin is not None and (row.get("active_version") or row.get("staged_version")):
            found[str(plugin_id)] = (plugin, row)
    return found


def _source_entries(state: dict[str, Any], source_id: Any) -> list[dict[str, Any]]:
    return [
        entry for entry in (state.get("catalog_entries") or {}).values()
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), dict) and entry.get("source") == source_id
    ]


def resolve_closure(
    state: dict[str, Any], root: dict[str, Any], usable: Callable[[dict[str, Any]], bool],
) -> Closure:
    """The closure of the catalog entry ``root``.

    Dependencies come from the root's own source or are already installed. ``usable`` says whether a
    catalog entry may be installed here (trusted source for its kind, runnable on this host, not
    refused by its kind), so a dependency is chosen by the same rule as the plugin.
    """
    closure = Closure()
    entries = _source_entries(state, root.get("source"))
    installed = installed_plugins(state)
    visiting: list[str] = []
    done: set[str] = set()

    def provides(entry: dict[str, Any], capability: str) -> bool:
        plugin = _parse(entry.get("manifest"))
        return plugin is not None and capability in plugin.provides

    def choose(plugin_id: str, capability: str | None) -> dict[str, Any] | None:
        return select_entry(
            entries, plugin_id,
            usable=lambda entry: usable(entry) and (capability is None or provides(entry, capability)),
        )

    def satisfy(owner: PluginManifest, how: str, needed_id: str | None, capability: str) -> None:
        if how == "extends":
            held = installed.get(needed_id)
            if held is not None:
                if capability not in held[0].provides:
                    newer = choose(needed_id, capability)
                    closure.unresolved.append(
                        f"{owner.name} extends {needed_id}, which is installed but does not provide {capability}"
                        + (f"; version {newer['manifest'].get('version')} does. Update {needed_id} first."
                           if newer else "."))
                elif not _enabled(held[1]):
                    closure.unresolved.append(
                        f"{owner.name} extends {needed_id}, which is switched off. Switch it on first.")
                else:
                    closure.satisfied_by.append(
                        {"id": needed_id, "version": held[0].version, "capability": capability})
                return
            candidate = choose(needed_id, capability)
            if candidate is None:
                closure.unresolved.append(
                    f"{owner.name} extends {needed_id}, and the catalog has no release of it this Bazarr+ can install.")
                return
            visit(candidate, "extends", len(visiting))
            return
        if capability.startswith(HOST_CAPABILITY_PREFIX):
            closure.external.append(capability)
            return
        switched_off = []
        for plugin_id, (held, row) in sorted(installed.items()):
            if plugin_id == owner.plugin_id or capability not in held.provides:
                continue
            # A plugin that is switched off supplies nothing.
            if _enabled(row):
                closure.satisfied_by.append({"id": plugin_id, "version": held.version, "capability": capability})
                return
            switched_off.append(plugin_id)
        providers = sorted({
            str(entry.get("provider_id")) for entry in entries
            if entry.get("provider_id") != owner.plugin_id and entry.get("provider_id") not in installed
            and provides(entry, capability)
        })
        if not providers:
            if switched_off:
                closure.unresolved.append(
                    f"{owner.name} needs {capability}, which only {', '.join(switched_off)} provides, "
                    "and it is switched off. Switch it on first.")
                return
            for plugin_id in sorted(installed):
                newer = choose(plugin_id, capability) if plugin_id != owner.plugin_id else None
                if newer is not None:
                    # The installed release lacks it and a newer one has it: an update comes first.
                    closure.unresolved.append(
                        f"{owner.name} needs {capability}, which {plugin_id} provides from version "
                        f"{newer['manifest'].get('version')}. Update {plugin_id} first.")
                    return
            # No plugin provides it anywhere: a host capability or an address the user configures.
            closure.external.append(capability)
            return
        for plugin_id in providers:
            candidate = choose(plugin_id, capability)
            if candidate is not None:
                visit(candidate, "requires", len(visiting))
                return
        closure.unresolved.append(
            f"{owner.name} needs {capability}, which no plugin this Bazarr+ can install provides.")

    def visit(entry: dict[str, Any], reason: str, depth: int) -> None:
        plugin = _parse(entry.get("manifest"))
        if plugin is None:
            closure.unresolved.append(f"The manifest of {entry.get('provider_id')} could not be read.")
            return
        if plugin.plugin_id in done:
            return
        if plugin.plugin_id in visiting or depth >= MAX_DEPTH:
            closure.unresolved.append(f"{plugin.name} depends on itself through a cycle.")
            return
        visiting.append(plugin.plugin_id)
        try:
            if plugin.extends is not None:
                satisfy(plugin, "extends", plugin.extends.plugin_id, plugin.extends.capability)
            for capability in plugin.requires:
                if capability not in plugin.provides:
                    satisfy(plugin, "requires", None, capability)
        finally:
            visiting.pop()
        done.add(plugin.plugin_id)
        closure.members.append(Member(entry, plugin, reason))

    visit(root, "root", 0)
    # A member added for its dependent comes before it; the root is last by construction.
    closure.external = sorted(set(closure.external))
    return closure


def _manifest_in(row: dict[str, Any], phase: str) -> PluginManifest | None:
    """The manifest an installation runs in ``phase``: ``now`` is its active release, ``next`` the release
    that runs after the next restart (its staged update when it has one, else the active release)."""
    if phase == "next" and isinstance(row.get("staged_manifest"), dict):
        return _parse(row["staged_manifest"])
    if row.get("active_version"):
        return _parse(row.get("manifest"))
    return None


def dependents(state: dict[str, Any], plugin_id: str, *, enabled_only: bool = False) -> list[str]:
    """The ids of installed plugins that depend on ``plugin_id``.

    A plugin depends on another when it extends it, or requires a capability the other provides and
    no other enabled plugin provides. That is judged twice, on what runs now and on what runs after the
    next restart, each with one consistent set of releases: a staged update that drops a capability stops
    supplying it after the restart, and one that adds it does not supply it before. With ``enabled_only``,
    plugins that are switched off do not count, which is the rule for switching a dependency off;
    removing one counts every installed dependent.
    """
    installed = installed_plugins(state)
    if plugin_id not in installed:
        return []
    found = []
    for other_id, (_active, row) in sorted(installed.items()):
        if other_id == plugin_id:
            continue
        if enabled_only and not _enabled(row):
            continue
        for phase in ("now", "next"):
            consumer = _manifest_in(row, phase)
            target = _manifest_in(installed[plugin_id][1], phase)
            if consumer is None or target is None:
                continue
            depends = consumer.extends is not None and consumer.extends.plugin_id == plugin_id
            for capability in consumer.requires:
                if depends or capability not in target.provides or capability in consumer.provides:
                    continue
                # A plugin that is switched off supplies nothing.
                elsewhere = False
                for held_id, (_held, held_row) in installed.items():
                    if held_id in (plugin_id, other_id) or not _enabled(held_row):
                        continue
                    supplier = _manifest_in(held_row, phase)
                    if supplier is not None and capability in supplier.provides:
                        elsewhere = True
                        break
                if not elsewhere:
                    depends = True
            if depends:
                found.append(other_id)
                break
    return found


def _enabled(row: dict[str, Any]) -> bool:
    from .kinds import UnknownKindError, get_kind, kind_of

    try:
        return bool(get_kind(kind_of(row)).is_enabled(row))
    except UnknownKindError:
        return bool(row.get("enabled", True))


def dependents_text(plugin_id: str, names: Iterable[str], action: str) -> str:
    names = sorted(names)
    return f"{plugin_id} cannot be {action} while {', '.join(names)} depend{'s' if len(names) == 1 else ''} on it."

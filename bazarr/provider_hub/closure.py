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
    # The release chosen for each plugin this install adds, so a later need is judged against it.
    selected: dict[str, PluginManifest] = {}

    def provides(entry: dict[str, Any], capability: str) -> bool:
        plugin = _parse(entry.get("manifest"))
        return plugin is not None and capability in plugin.provides

    def choose(plugin_id: str, capability: str | None) -> dict[str, Any] | None:
        return select_entry(
            entries, plugin_id,
            usable=lambda entry: usable(entry) and (capability is None or provides(entry, capability)),
        )

    def upgrade(plugin_id: str, capability: str) -> dict[str, Any] | None:
        """A newer release of an installed plugin that provides ``capability``: from the source the
        install is bound to, and above the installed version, so the hint names a real update."""
        held, row = installed[plugin_id]
        if not row.get("source_bound") or row.get("source_id") != root.get("source"):
            return None
        # Above the release that runs after the next restart, which may already be the staged one.
        running = _manifest_in(row, "next") or held
        return select_entry(
            entries, plugin_id, usable=lambda entry: usable(entry) and provides(entry, capability),
            above_version=running.version)

    def satisfy(owner: PluginManifest, how: str, needed_id: str | None, capability: str) -> None:
        if how == "extends":
            held = installed.get(needed_id)
            if held is not None:
                running = _manifest_in(held[1], "next") or held[0]
                if not _supplies(held[1], capability):
                    newer = upgrade(needed_id, capability)
                    closure.unresolved.append(
                        f"{owner.name} extends {needed_id}, which is installed but does not provide {capability}"
                        + (f"; version {newer['manifest'].get('version')} does. Update {needed_id} first."
                           if newer else "."))
                elif not _enabled(held[1]):
                    closure.unresolved.append(
                        f"{owner.name} extends {needed_id}, which is switched off. Switch it on first.")
                else:
                    closure.satisfied_by.append(
                        {"id": needed_id, "version": running.version, "capability": capability})
                return
            chosen = selected.get(needed_id)
            if chosen is not None:
                if capability not in chosen.provides:
                    closure.unresolved.append(
                        f"{owner.name} extends {needed_id}, but the release of it already chosen for this "
                        f"install ({chosen.version}) does not provide {capability}.")
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
        dropping = []
        for plugin_id, (held, row) in sorted(installed.items()):
            if plugin_id == owner.plugin_id:
                continue
            if not _supplies(row, capability):
                # Provides it now, and the update staged for it drops it.
                if _provides_now(row, capability):
                    dropping.append(plugin_id)
                continue
            # A plugin that is switched off supplies nothing.
            if _enabled(row):
                running = _manifest_in(row, "next") or held
                closure.satisfied_by.append(
                    {"id": plugin_id, "version": running.version, "capability": capability})
                return
            switched_off.append(plugin_id)
        providers = sorted({
            str(entry.get("provider_id")) for entry in entries
            if entry.get("provider_id") != owner.plugin_id and entry.get("provider_id") not in installed
            and provides(entry, capability)
        })
        if not providers:
            if dropping and not switched_off:
                closure.unresolved.append(
                    f"{owner.name} needs {capability}, which {', '.join(dropping)} provides now, but the "
                    "update staged for it no longer does.")
                return
            if switched_off:
                closure.unresolved.append(
                    f"{owner.name} needs {capability}, which only {', '.join(switched_off)} provides, "
                    "and it is switched off. Switch it on first.")
                return
            for plugin_id in sorted(installed):
                newer = upgrade(plugin_id, capability) if plugin_id != owner.plugin_id else None
                if newer is not None:
                    # The installed release lacks it and a newer one has it: an update comes first.
                    closure.unresolved.append(
                        f"{owner.name} needs {capability}, which {plugin_id} provides from version "
                        f"{newer['manifest'].get('version')}. Update {plugin_id} first.")
                    return
            # No plugin provides it anywhere: a host capability or an address the user configures.
            closure.external.append(capability)
            return
        conflicting = []
        for plugin_id in providers:
            chosen = selected.get(plugin_id)
            if chosen is not None:
                if capability in chosen.provides:
                    return
                # Already chosen for another need, in a release that does not provide this one.
                conflicting.append(plugin_id)
                continue
            candidate = choose(plugin_id, capability)
            if candidate is not None:
                visit(candidate, "requires", len(visiting))
                return
        if conflicting:
            closure.unresolved.append(
                f"{owner.name} needs {capability}, which {', '.join(conflicting)} provides only in a release "
                "other than the one already chosen for this install.")
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
        selected[plugin.plugin_id] = plugin
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


def _supplies(row: dict[str, Any], capability: str) -> bool:
    """Whether an installation will provide ``capability`` once the pending restart has activated what is
    staged: judged on the release that runs after the restart, its staged one when it has one.

    A plugin installed now only starts at that same restart (staged installs activate at boot), so it never
    runs beside the release a supplier has today. Removal is stricter and judges both releases.
    """
    plugin = _manifest_in(row, "next")
    return plugin is not None and capability in plugin.provides


def _provides_now(row: dict[str, Any], capability: str) -> bool:
    plugin = _manifest_in(row, "now")
    return plugin is not None and capability in plugin.provides


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


def broken_by(state: dict[str, Any], plugin_id: str, replacement: dict[str, Any] | None) -> list[tuple[str, str]]:
    """The installed or staged dependents a change to ``plugin_id`` would leave without a capability.

    ``replacement`` is the manifest that would become its staged release, and None means its staged release
    goes away and its active one is what runs after the restart. A dependent counts when it relies on a
    capability this plugin supplies after the next restart (or, for ``extends``, on it as the parent), the
    change drops that capability, and no other enabled plugin supplies it. Returns ``(dependent id,
    capability)`` pairs, sorted.
    """
    installed = installed_plugins(state)
    held = installed.get(plugin_id)
    if held is None:
        return []
    row = held[1]
    current = _manifest_in(row, "next")
    if current is None:
        return []
    if replacement is None:
        after = _parse(row.get("manifest")) if row.get("active_version") else None
    else:
        after = _parse(replacement)
    broken: set[tuple[str, str]] = set()
    for other_id, (_active, other_row) in installed.items():
        if other_id == plugin_id:
            continue
        consumer = _manifest_in(other_row, "next")
        if consumer is None:
            continue
        needs = []
        if consumer.extends is not None and consumer.extends.plugin_id == plugin_id:
            needs.append((consumer.extends.capability, True))
        needs.extend((capability, False) for capability in consumer.requires if capability not in consumer.provides)
        for capability, parent in needs:
            if capability not in current.provides or (after is not None and capability in after.provides):
                continue
            if not parent and _supplied_elsewhere(installed, capability, (plugin_id, other_id)):
                continue
            broken.add((other_id, capability))
    return sorted(broken)


def _supplied_elsewhere(installed: dict[str, Any], capability: str, excluding: tuple[str, ...]) -> bool:
    for held_id, (_held, held_row) in installed.items():
        if held_id in excluding or not _enabled(held_row):
            continue
        supplier = _manifest_in(held_row, "next")
        if supplier is not None and capability in supplier.provides:
            return True
    return False


def breaks_text(plugin_id: str, version: str | None, broken: list[tuple[str, str]]) -> str:
    """Why a release of ``plugin_id`` is refused: the dependents it would leave without a capability."""
    names = sorted({other for other, _capability in broken})
    capabilities = sorted({capability for _other, capability in broken})
    return (f"{plugin_id}{' ' + version if version else ''} no longer provides {', '.join(capabilities)}, "
            f"which {', '.join(names)} relies on.")


def supplier_map(state: dict[str, Any], plugin: PluginManifest, owner_id: str) -> dict[str, str]:
    """For each capability ``plugin`` requires that an installed plugin will supply after the next restart,
    which plugin that is. Recorded with the release, so a boot can tell a need a plugin once met from one
    nothing ever supplied."""
    installed = installed_plugins(state)
    found: dict[str, str] = {}
    for capability in plugin.requires:
        if capability in plugin.provides or capability.startswith(HOST_CAPABILITY_PREFIX):
            continue
        for held_id, (_held, held_row) in sorted(installed.items()):
            supplier = _manifest_in(held_row, "next")
            # Recorded whether or not the supplier is switched on (a new provider waits for its settings):
            # which needs are backed by a plugin is a fact about the release, and the boot judges whether
            # the supplier runs.
            if held_id != owner_id and supplier is not None and capability in supplier.provides:
                found[capability] = held_id
                break
    return found


def is_on(row: dict[str, Any]) -> bool:
    """Whether an installation is switched on, by the rule of its own kind."""
    return _enabled(row)


def held_back_error(row: dict[str, Any]) -> str | None:
    """The error a held-back plugin shows: its reason while it is switched on, nothing while it is off."""
    return row.get("blocked_reason") if row.get("blocked") and _enabled(row) else None


def unmet_needs(installations: dict[str, Any], enabled: dict[str, bool] | None = None) -> dict[str, list[str]]:
    """For each installation whose active release relies on a plugin that is not running, what is missing.

    Judged on the set that actually activated and is switched on. The plugin it extends must be that
    plugin, running and providing the capability. A capability it once had an installed supplier for is met
    by any running supplier of it, not only the one recorded, and is unmet only when none runs. A plugin
    that is itself unmet is not running, so what relies on it is unmet too, and a plugin that is switched
    off supplies nothing (it is still judged, so switching it on cannot start it unmet). A plugin whose
    environment is flagged as needing repair does not run either. ``enabled`` overrides, by id, whether a
    plugin counts as switched on, for a change that has not been saved yet.
    """
    rows = {
        str(pid): row for pid, row in installations.items()
        if isinstance(row, dict) and row.get("active_version") and row.get("state") != "removed"
    }
    parsed = {pid: _parse(row.get("manifest")) for pid, row in rows.items()}
    candidates = {pid for pid, plugin in parsed.items() if plugin is not None}
    from .kinds import known_kinds, kind_of

    registered = set(known_kinds())
    # Only a plugin of a kind this Bazarr+ defines, switched on, is running to supply anything.
    def on(pid: str) -> bool:
        return enabled[pid] if enabled is not None and pid in enabled else _enabled(rows[pid])

    suppliers = {
        pid for pid in candidates
        if kind_of(rows[pid]) in registered and on(pid) and not rows[pid].get("needs_repair")
    }
    blocked: dict[str, list[str]] = {}
    while True:
        newly: dict[str, list[str]] = {}
        for pid in sorted(candidates - set(blocked)):
            plugin = parsed[pid]
            missing = []
            if plugin.extends is not None:
                parent_id = plugin.extends.plugin_id
                parent = parsed.get(parent_id) if parent_id in suppliers and parent_id != pid else None
                if parent is None or plugin.extends.capability not in parent.provides:
                    missing.append(f"{parent_id} ({plugin.extends.capability})")
            for capability, recorded in sorted((rows[pid].get("supplied_by") or {}).items()):
                if capability not in plugin.requires:
                    continue
                if not any(capability in parsed[sid].provides for sid in suppliers if sid != pid):
                    missing.append(f"{recorded} ({capability})")
            if missing:
                newly[pid] = missing
        if not newly:
            return blocked
        # What is not running supplies nothing, so judge the rest again without it.
        blocked.update(newly)
        suppliers -= set(newly)


def _enabled(row: dict[str, Any]) -> bool:
    from .kinds import UnknownKindError, get_kind, kind_of

    try:
        return bool(get_kind(kind_of(row)).is_enabled(row))
    except UnknownKindError:
        return bool(row.get("enabled", True))


def dependents_text(plugin_id: str, names: Iterable[str], action: str) -> str:
    names = sorted(names)
    return f"{plugin_id} cannot be {action} while {', '.join(names)} depend{'s' if len(names) == 1 else ''} on it."

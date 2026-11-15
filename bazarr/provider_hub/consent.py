# coding=utf-8
"""What a person agrees to when a plugin is installed or updated.

``install_summary`` describes an install: the plugin and every plugin its closure would add, with
where each comes from and whether Bazarr+ trusts that source, the permissions each asks for (marked
enforced when this host's worker sandbox enforces them and declared otherwise), the fixed origins its
code contacts, any binary or container it runs, and what the closure needs that no plugin supplies.
``update_delta`` describes only what an update adds to what is already installed.

Each summary carries a digest of the facts it shows. The install or update request returns that
digest, and the host refuses the request when the set it would act on no longer matches, so a catalog
refresh between the dialog and the install cannot slip something in that nobody saw. Marks are left out
of the digest: they describe this host's sandbox, which can finish probing in between, and the
facts they describe do not change.
"""
from __future__ import annotations

import hashlib
import json

from typing import Any, Callable, Mapping

from .closure import Closure, Member, installed_plugins
from .plugin_manifest import ARCHIVE, OCI, PluginManifest

DIGEST_PREFIX = "sha256:"
ENFORCED = "enforced"
DECLARED = "declared"
_FILESYSTEM_PERMISSIONS = frozenset({"media_read", "data_dir"})


def permission_mark(permission: str, sandbox: Mapping[str, Any] | None) -> str:
    """``enforced`` when the worker sandbox of this host enforces what the permission names, else ``declared``."""
    sandbox = sandbox or {}
    if not sandbox.get("workers_sandboxed"):
        return DECLARED
    if permission == "network":
        return ENFORCED if sandbox.get("network") == ENFORCED else DECLARED
    if permission in _FILESYSTEM_PERMISSIONS:
        return ENFORCED if sandbox.get("filesystem") == ENFORCED else DECLARED
    return DECLARED


def sandbox_status() -> dict[str, Any]:
    """The cached sandbox probe, never waiting on it. A probe that cannot be read marks everything declared."""
    try:
        from .sandbox.spawn import sandbox_status_nowait

        return sandbox_status_nowait()
    except Exception:
        return {}


def _binaries(plugin: PluginManifest) -> list[dict[str, str]]:
    if plugin.artifact in (ARCHIVE, OCI):
        return [{"artifact": plugin.artifact, "process": plugin.process}]
    return []


def _destination_fields(plugin: PluginManifest) -> set[str]:
    """The settings fields the user points at an address, which are destinations the manifest does not fix."""
    keys = {str(field.get("key")) for field in plugin.settings_fields if field.get("destination") is True}
    for type_id, fields in plugin.backend_fields:
        keys.update(f"{type_id}.{field.get('key')}" for field in fields if field.get("destination") is True)
    return keys


def _member_facts(member: Member) -> dict[str, Any]:
    plugin = member.plugin
    return {
        "id": plugin.plugin_id,
        "version": plugin.version,
        "bundle_sha256": member.entry["manifest"].get("bundle_sha256"),
        "commit": (member.entry["manifest"].get("source") or {}).get("commit"),
        "permissions": sorted(plugin.permissions),
        "destinations": list(plugin.all_endpoints),
        "destination_fields": sorted(_destination_fields(plugin)),
        "binaries": _binaries(plugin),
    }


def digest_of(facts: Any) -> str:
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return DIGEST_PREFIX + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def install_summary(
    closure: Closure,
    describe_source: Callable[[Any], dict[str, Any]],
    sandbox: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The consent summary of installing the plugin that is the last member of ``closure``."""
    members = []
    permissions: dict[str, str] = {}
    destinations: set[str] = set()
    binaries: list[dict[str, str]] = []
    for member in closure.members:
        plugin = member.plugin
        marked = [{"name": name, "mark": permission_mark(name, sandbox)} for name in plugin.permissions]
        permissions.update({item["name"]: item["mark"] for item in marked})
        destinations.update(plugin.all_endpoints)
        for binary in _binaries(plugin):
            if binary not in binaries:
                binaries.append(binary)
        members.append({
            "id": plugin.plugin_id,
            "name": plugin.name,
            "version": plugin.version,
            "kind": plugin.kind,
            "reason": member.reason,
            "source": describe_source(member.entry.get("source")),
            "permissions": marked,
            "destinations": list(plugin.all_endpoints),
            "binaries": _binaries(plugin),
            # The manifest carries a digest per file but no sizes, so no download size is known yet.
            "download_size": None,
            # No manifest field declares a memory request yet.
            "memory_request": None,
        })
    if not closure.members:
        raise ValueError("a consent summary needs a plugin to describe")
    root = closure.members[-1]
    facts = {
        "members": [_member_facts(member) for member in closure.members],
        "satisfied_by": sorted((item["id"], item["capability"]) for item in closure.satisfied_by),
        "external": list(closure.external),
    }
    return {
        "plugin": {"id": root.plugin_id, "name": root.plugin.name, "version": root.plugin.version,
                   "kind": root.plugin.kind},
        "members": members,
        "already_installed": list(closure.satisfied_by),
        "external_requirements": list(closure.external),
        "permissions": [{"name": name, "mark": permissions[name]} for name in sorted(permissions)],
        "destinations": sorted(destinations),
        "binaries": binaries,
        "download_size": None,
        "memory_request": None,
        "digest": digest_of(facts),
    }


def update_delta(
    state: dict[str, Any],
    installed_manifest: Any,
    new_entry: dict[str, Any],
    new_closure: Closure,
    sandbox: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What updating to ``new_entry`` adds to what is installed, and whether that needs the person's consent.

    A new destination (a fixed origin, or a settings field the user points at an address), a new
    permission, a binary or container the plugin did not run before, or a plugin joining its closure
    asks again. A changed package set inside the plugin's own hashed environment, or a new version of a
    binary it already runs, asks nothing.
    """
    from .plugin_manifest import parse_plugin_manifest

    new = parse_plugin_manifest(new_entry["manifest"])
    try:
        old = parse_plugin_manifest(installed_manifest)
    except Exception:
        old = None
    old_destinations = set(old.all_endpoints) if old else set()
    old_permissions = set(old.permissions) if old else set()
    old_fields = _destination_fields(old) if old else set()
    added_destinations = sorted(set(new.all_endpoints) - old_destinations)
    added_fields = sorted(_destination_fields(new) - old_fields)
    added_permissions = sorted(set(new.permissions) - old_permissions)
    added_binaries = [binary for binary in _binaries(new) if not old or binary["artifact"] != old.artifact]
    held = set(installed_plugins(state))
    added_plugins = sorted(
        member.plugin_id for member in new_closure.members
        if member.reason != "root" and member.plugin_id not in held)
    added = {
        "destinations": added_destinations,
        "destination_fields": added_fields,
        "permissions": [{"name": name, "mark": permission_mark(name, sandbox)} for name in added_permissions],
        "binaries": added_binaries,
        "plugins": added_plugins,
    }
    needs = any(added[key] for key in added)
    facts = {
        "id": new.plugin_id, "from": old.version if old else None, "to": new.version,
        "bundle_sha256": new_entry["manifest"].get("bundle_sha256"),
        "destinations": added_destinations, "destination_fields": added_fields,
        "permissions": added_permissions, "binaries": added_binaries, "plugins": added_plugins,
    }
    return {
        "plugin": {"id": new.plugin_id, "name": new.name, "from_version": old.version if old else None,
                   "to_version": new.version, "kind": new.kind},
        "needs_consent": needs,
        "added": added,
        "digest": digest_of(facts),
    }


def delta_text(delta: Mapping[str, Any]) -> str:
    """One line naming what an update adds, for a refusal that asks the person to confirm it."""
    added = delta["added"]
    parts = []
    if added["destinations"]:
        parts.append("it contacts " + ", ".join(added["destinations"]))
    if added["destination_fields"]:
        parts.append("it adds the address setting " + ", ".join(added["destination_fields"]))
    if added["permissions"]:
        parts.append("it asks for " + ", ".join(item["name"] for item in added["permissions"]))
    if added["binaries"]:
        parts.append("it runs " + ", ".join(item["artifact"] for item in added["binaries"]))
    if added["plugins"]:
        parts.append("it installs " + ", ".join(added["plugins"]))
    return "This update needs your consent first: " + "; ".join(parts) + "."

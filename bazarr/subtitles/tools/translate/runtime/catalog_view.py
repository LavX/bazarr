# coding=utf-8
"""What the catalog says that the Translation page shows: engines it could install, and a newer release.

Read-only. The install itself is the Provider Hub's (the page posts to its installation route and follows
the job), and so is every update; this module only describes entries the hub already holds.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping
from urllib.parse import urlsplit

from .destinations import DestinationSchema, effective_destinations
from .field_schema import is_secret_field
from .legacy_engines import LEGACY_ENGINES
from .store import OFFICIAL_ENGINE_PLUGIN_ID

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}
DESCRIPTION_LIMIT = 300


def available_engines() -> list[dict[str, Any]]:
    """The translator engines the catalog offers that have no installation, the official one first."""
    from provider_hub.kinds import TRANSLATOR
    from provider_hub.service import installable_catalog_plugins

    try:
        entries = installable_catalog_plugins(TRANSLATOR)
    except Exception:
        logger.exception("Could not read the catalog for translator engines")
        return []
    items = []
    for entry in entries:
        # An engine Bazarr+ runs itself is already in the engine list, and a catalog entry must not shadow it.
        if str(entry.get("provider_id") or "") in LEGACY_ENGINES:
            continue
        item = describe_entry(entry)
        if item is not None:
            items.append(item)
    items.sort(key=lambda item: (not item["official"], item["name"].casefold()))
    return items


def update_version(plugin_id: str) -> str | None:
    """The release the Provider Hub offers as an update to the installed plugin, or None."""
    from provider_hub.service import offered_update_version

    try:
        return offered_update_version(plugin_id)
    except Exception:
        logger.exception("Could not check %s for an update", plugin_id)
        return None


def describe_entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """One catalog entry as the page lists it, or None when its manifest cannot be read."""
    from provider_hub.plugin_manifest import parse_plugin_manifest
    from provider_hub.service import OFFICIAL_CATALOG_SOURCE_ID

    try:
        manifest = parse_plugin_manifest(dict(entry["manifest"]))
    except Exception:
        logger.warning("The catalog entry for translator %s does not parse", entry.get("provider_id"), exc_info=True)
        return None
    scopes = [DestinationSchema(fields=manifest.settings_fields, endpoints=manifest.endpoints)]
    scopes += [DestinationSchema(fields=fields, endpoints=dict(manifest.backend_endpoints).get(type_id, ()))
               for type_id, fields in manifest.backend_fields]
    destinations = sorted({_shown(item) for scope in scopes for item in effective_destinations(scope, {})
                           if not item.startswith("invalid:")})
    every_field = list(manifest.settings_fields) + [field for _type, fields in manifest.backend_fields
                                                    for field in fields]
    description = manifest.raw.get("description")
    item: dict[str, Any] = {
        "plugin_id": manifest.plugin_id,
        "name": manifest.name or manifest.plugin_id,
        "version": manifest.version,
        "source": str(entry.get("source") or ""),
        # The official engine is the one the official source lists under its id: another source's entry with the
        # same id is not it, however it is named.
        "official": (manifest.plugin_id == OFFICIAL_ENGINE_PLUGIN_ID
                     and entry.get("source") == OFFICIAL_CATALOG_SOURCE_ID),
        # No field asks for a key, so it needs no account and has no bill to cap.
        "free": not any(is_secret_field(field) for field in every_field),
        "destinations": destinations,
    }
    if isinstance(description, str) and description.strip():
        item["description"] = description.strip()[:DESCRIPTION_LIMIT]
    return item


def _shown(origin: str) -> str:
    """An address as a person reads it: the port is left off when it is the scheme's own."""
    parts = urlsplit(origin)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is None or parts.port == _DEFAULT_PORTS.get(parts.scheme):
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{parts.port}"

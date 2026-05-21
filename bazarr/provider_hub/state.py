# coding=utf-8
from __future__ import annotations

import json
import os

from dataclasses import dataclass
from pathlib import Path
from typing import Any


OFFICIAL_CATALOG_SOURCE_ID = "official"
OFFICIAL_CATALOG_URL = "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json"


@dataclass(frozen=True)
class ProviderHubInstallation:
    provider_id: str
    name: str
    active_version: str | None
    state: str
    pending_restart: bool
    manifest: dict[str, Any]
    active_path: str | None = None
    python_path: str | None = None
    staged_version: str | None = None
    staged_path: str | None = None
    staged_python_path: str | None = None
    last_error: str | None = None


def official_catalog_source() -> dict[str, Any]:
    return {
        "id": OFFICIAL_CATALOG_SOURCE_ID,
        "name": "Official Bazarr Provider Catalog",
        "type": "github",
        "url": OFFICIAL_CATALOG_URL,
        "enabled": True,
        "official": True,
        "trusted": True,
        "dev_ref": None,
        "last_checked_at": None,
        "last_error": None,
    }


def default_state() -> dict[str, Any]:
    return {
        "catalog_sources": {
            OFFICIAL_CATALOG_SOURCE_ID: official_catalog_source(),
        },
        "catalog_entries": {},
        "installations": {},
        "jobs": [],
    }


def _default_state_file() -> Path:
    try:
        from app.get_args import args
        return Path(args.config_dir) / "provider_hub" / "state.json"
    except Exception:
        return Path("provider_hub") / "state.json"


def state_file() -> Path:
    override = os.environ.get("BAZARR_PROVIDER_HUB_STATE")
    if override:
        return Path(override)
    return _default_state_file()


def provider_hub_dir() -> Path:
    return state_file().parent


def load_state(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    current = Path(path) if path is not None else state_file()
    if not current.exists():
        return default_state()
    with current.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return default_state()
    sources = data.setdefault("catalog_sources", {})
    if not isinstance(sources, dict):
        sources = {}
        data["catalog_sources"] = sources
    if OFFICIAL_CATALOG_SOURCE_ID not in sources:
        sources[OFFICIAL_CATALOG_SOURCE_ID] = official_catalog_source()
    for source_id, source in list(sources.items()):
        if not isinstance(source, dict):
            continue
        is_official = source_id == OFFICIAL_CATALOG_SOURCE_ID
        if is_official:
            official = official_catalog_source()
            official.update(
                {
                    key: value
                    for key, value in source.items()
                    if key not in ("official", "trusted", "url", "type")
                }
            )
            official["id"] = OFFICIAL_CATALOG_SOURCE_ID
            official["name"] = source.get("name") or official["name"]
            sources[source_id] = official
        else:
            source["official"] = False
            source["trusted"] = False
        if "dev_ref" not in source:
            source["dev_ref"] = None
    entries = data.setdefault("catalog_entries", {})
    if isinstance(entries, dict):
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            source_ref = entry.get("source") or entry.get("source_name")
            source = next(
                (
                    item
                    for item in sources.values()
                    if isinstance(item, dict)
                    and source_ref in (item.get("id"), item.get("name"))
                ),
                {},
            )
            entry["trusted"] = bool(source.get("trusted", False))
    data.setdefault("installations", {})
    data.setdefault("jobs", [])
    return data


def save_state(data: dict[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    current = Path(path) if path is not None else state_file()
    current.parent.mkdir(parents=True, exist_ok=True)
    tmp = current.with_suffix(current.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.replace(tmp, current)


def active_installations(path: str | os.PathLike[str] | None = None) -> list[ProviderHubInstallation]:
    data = load_state(path)
    installations = []
    for provider_id, item in (data.get("installations") or {}).items():
        if not isinstance(item, dict):
            continue
        if item.get("state") != "active" or item.get("pending_restart"):
            continue
        manifest = item.get("manifest")
        if not isinstance(manifest, dict):
            continue
        installations.append(
            ProviderHubInstallation(
                provider_id=str(item.get("provider_id") or provider_id),
                name=str(item.get("name") or manifest.get("name") or provider_id),
                active_version=item.get("active_version"),
                state=str(item.get("state")),
                pending_restart=bool(item.get("pending_restart", False)),
                manifest=manifest,
                active_path=item.get("active_path"),
                python_path=item.get("python_path"),
                staged_version=item.get("staged_version"),
                staged_path=item.get("staged_path"),
                staged_python_path=item.get("staged_python_path"),
                last_error=item.get("last_error"),
            )
        )
    return installations

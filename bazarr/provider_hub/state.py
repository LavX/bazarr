# coding=utf-8
from __future__ import annotations

import json
import os
import threading
import contextlib

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any


OFFICIAL_CATALOG_SOURCE_ID = "official"
OFFICIAL_CATALOG_SOURCE_NAME = "Official Bazarr Provider Catalog"
OFFICIAL_CATALOG_URL = "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json"
OFFICIAL_CATALOG_REPO = "LavX/bazarr-provider-catalog"
_STATE_LOCK = threading.RLock()


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
    trusted: bool = False
    origin: str = "catalog"
    source_id: str | None = None


def official_catalog_source() -> dict[str, Any]:
    return {
        "id": OFFICIAL_CATALOG_SOURCE_ID,
        "name": OFFICIAL_CATALOG_SOURCE_NAME,
        "type": "github",
        "url": OFFICIAL_CATALOG_URL,
        "enabled": True,
        "official": True,
        "trusted": True,
        "dev_ref": None,
        "last_checked_at": None,
        "last_error": None,
    }


def spoofs_official_catalog_source(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return normalized in {
        OFFICIAL_CATALOG_SOURCE_ID.casefold(),
        OFFICIAL_CATALOG_SOURCE_NAME.casefold(),
    }


def catalog_source_for_entry(
    sources: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    source_id = entry.get("source")
    if isinstance(source_id, str):
        source = sources.get(source_id)
        if isinstance(source, dict):
            return source
    source_name = entry.get("source_name")
    if isinstance(source_name, str):
        for key, source in sources.items():
            if key == OFFICIAL_CATALOG_SOURCE_ID:
                continue
            if isinstance(source, dict) and source.get("name") == source_name:
                return source
    return {}


def default_state() -> dict[str, Any]:
    return {
        "catalog_sources": {
            OFFICIAL_CATALOG_SOURCE_ID: official_catalog_source(),
        },
        "catalog_entries": {},
        "installations": {},
        "jobs": [],
        # `provider_id.field` paths of plugin secret fields. Persisted so a
        # removed plugin keeps its registrations: the row goes at the next
        # restart, the path never leaves.
        "plugin_secret_paths": [],
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


@contextlib.contextmanager
def state_write_lock():
    with _STATE_LOCK:
        yield


# Bumped by every successful state write, so caches derived from state.json
# (the decrypt-blocked provider set in the Hub service) can key on it and be
# invalidated by mutate_state deterministically, even where the filesystem's
# mtime granularity would leave a rewritten file looking unchanged.
_STATE_VERSION = 0


def state_version() -> int:
    """How many times state.json has been written by this process."""
    return _STATE_VERSION


def mutate_state(
    mutator: Callable[[dict[str, Any]], Any],
    path: str | os.PathLike[str] | None = None,
) -> Any:
    with state_write_lock():
        data = load_state(path)
        result = mutator(data)
        save_state(data, path)
        return result


def load_state(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    current = Path(path) if path is not None else state_file()
    if not current.exists():
        return default_state()
    with current.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return default_state()
    return _normalized_state(data)


def load_state_strict(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The state as stored, refusing a document whose root is not an object.

    ``load_state`` answers a default for a malformed document, which is the
    resilient answer for every reader that only wants the state there now. A
    rule that refuses work when the state cannot be read needs the
    distinction: a document whose root is not an object is the state being
    unreadable, not an empty installation inventory, so this reader raises
    and the caller fails closed instead of reading no installations.
    """
    current = Path(path) if path is not None else state_file()
    if not current.exists():
        return default_state()
    with current.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("the Provider Hub state document is not an object")
    return _normalized_state(data)


def _normalized_state(data: dict[str, Any]) -> dict[str, Any]:
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
            # Trust is kept as the last refresh computed it from the commit it
            # resolved; only the record's identity is rebuilt here.
            official = official_catalog_source()
            official.update(
                {
                    key: value
                    for key, value in source.items()
                    if key not in ("official", "url", "type")
                }
            )
            official["trusted"] = bool(official.get("trusted"))
            official["id"] = OFFICIAL_CATALOG_SOURCE_ID
            official["name"] = OFFICIAL_CATALOG_SOURCE_NAME
            sources[source_id] = official
        else:
            source["official"] = False
            source["trusted"] = False
            if spoofs_official_catalog_source(source.get("id")) or spoofs_official_catalog_source(source.get("name")):
                source["name"] = source_id
        if "dev_ref" not in source:
            source["dev_ref"] = None
    entries = data.setdefault("catalog_entries", {})
    if isinstance(entries, dict):
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            source = catalog_source_for_entry(sources, entry)
            entry["trusted"] = bool(source.get("trusted", False))
    data.setdefault("installations", {})
    data.setdefault("jobs", [])
    data.setdefault("plugin_secret_paths", [])
    return data


def save_state(data: dict[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    global _STATE_VERSION
    with state_write_lock():
        current = Path(path) if path is not None else state_file()
        current.parent.mkdir(parents=True, exist_ok=True)
        tmp = current.with_suffix(current.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, current)
        _STATE_VERSION += 1


def _installation_from_row(provider_id: str, item: dict[str, Any],
                          manifest: dict[str, Any]) -> ProviderHubInstallation:
    """One installation row as the typed record the callers walk."""
    return ProviderHubInstallation(
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
        trusted=bool(item.get("trusted", False)),
        origin=str(item.get("origin") or "catalog"),
        source_id=item.get("source_id"),
    )


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
        installations.append(_installation_from_row(provider_id, item, manifest))
    return installations


def running_installations(path: str | os.PathLike[str] | None = None) -> list[ProviderHubInstallation]:
    """Installations whose active code is running, keyed off active_version.

    The active provider class stays registered until restart, even while an
    update or a removal is staged, so the population the worker uses is
    every row with an active_version, whatever its state and restart
    flag. The settings overlay walks exactly this population: cutting it to
    the non-pending rows would let a provider be admitted on a
    settings-section credential that never reaches its worker config."""
    data = load_state(path)
    installations = []
    for provider_id, item in (data.get("installations") or {}).items():
        if not isinstance(item, dict):
            continue
        if not item.get("active_version"):
            continue
        manifest = item.get("manifest")
        if not isinstance(manifest, dict):
            continue
        installations.append(_installation_from_row(provider_id, item, manifest))
    return installations

# coding=utf-8
from __future__ import annotations

import json
import shutil
import tempfile
import uuid

import requests

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from subliminal_patch.extensions import provider_registry

from .manifest import validate_manifest
from .bundle import verify_bundle_tree
from .state import OFFICIAL_CATALOG_SOURCE_ID, OFFICIAL_CATALOG_URL, load_state, provider_hub_dir, save_state
from .venv import PluginEnvironment, python_executable
from .worker import ProviderWorkerClient, worker_command


class CatalogSourceError(ValueError):
    """Raised when a Provider Hub catalog source is not allowed."""


class ProviderHubInstallError(RuntimeError):
    """Raised when a Provider Hub install could not be staged."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_github_catalog_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise CatalogSourceError("Provider Hub V1 only supports GitHub.com HTTPS catalog sources")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[2] not in ("blob", "raw"):
        raise CatalogSourceError("Catalog source must be a GitHub file URL")
    return url


def _parse_github_file_url(url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(_validate_github_catalog_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    owner, repo, _kind, ref, *path_parts = parts
    if not path_parts:
        raise CatalogSourceError("Catalog source must include a file path")
    return owner, repo, ref, "/".join(path_parts)


def _resolve_github_ref(owner: str, repo: str, ref: str) -> str:
    if len(ref) == 40 and all(char in "0123456789abcdefABCDEF" for char in ref):
        return ref.lower()
    response = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}",
        timeout=20,
    )
    response.raise_for_status()
    commit = response.json().get("sha")
    if not isinstance(commit, str) or len(commit) != 40:
        raise CatalogSourceError("GitHub did not return an immutable commit SHA")
    return commit.lower()


def _fetch_github_catalog(url: str) -> tuple[dict[str, Any], str]:
    owner, repo, ref, path = _parse_github_file_url(url)
    commit = _resolve_github_ref(owner, repo, ref)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}"
    response = requests.get(raw_url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise CatalogSourceError("Catalog payload must be a JSON object")
    return payload, commit


def add_catalog_source(name: str, url: str, trusted: bool = False) -> dict[str, Any]:
    if not name or not isinstance(name, str):
        raise CatalogSourceError("Catalog source name is required")
    url = _validate_github_catalog_url(url)
    if name == OFFICIAL_CATALOG_SOURCE_ID and url != OFFICIAL_CATALOG_URL:
        raise CatalogSourceError("The official catalog source id is reserved")
    is_official = name == OFFICIAL_CATALOG_SOURCE_ID and url == OFFICIAL_CATALOG_URL

    state = load_state()
    sources = state.setdefault("catalog_sources", {})
    source = {
        "id": name,
        "name": name,
        "type": "github",
        "url": url,
        "enabled": True,
        "official": is_official,
        "trusted": is_official,
        "last_checked_at": None,
        "last_error": None,
    }
    sources[name] = source
    save_state(state)
    return source


def remove_catalog_source(name: str) -> bool:
    if name == OFFICIAL_CATALOG_SOURCE_ID:
        return False
    state = load_state()
    sources = state.setdefault("catalog_sources", {})
    if name not in sources:
        return False
    del sources[name]
    save_state(state)
    return True


def list_catalog() -> dict[str, Any]:
    state = load_state()
    return {
        "sources": list((state.get("catalog_sources") or {}).values()),
        "entries": list((state.get("catalog_entries") or {}).values()),
    }


def _normalize_catalog_manifest(manifest: dict[str, Any], source: dict[str, Any], commit: str) -> dict[str, Any]:
    normalized = dict(manifest)
    manifest_source = dict(normalized.get("source") or {})
    manifest_source.setdefault("type", "github")
    manifest_source.setdefault("catalog_url", source.get("url"))
    manifest_source["commit"] = commit
    manifest_source["trusted"] = bool(source.get("trusted", False))
    normalized["source"] = manifest_source
    return normalized


def refresh_catalog() -> dict[str, Any]:
    state = load_state()
    now = utcnow_iso()
    entries = state.setdefault("catalog_entries", {})
    refreshed_sources = set()
    refreshed_entry_keys = set()
    entries_count = 0
    sources_count = 0
    for source in (state.get("catalog_sources") or {}).values():
        if isinstance(source, dict):
            sources_count += 1
            source["last_checked_at"] = now
            try:
                catalog, commit = _fetch_github_catalog(source["url"])
                source["resolved_commit"] = commit
                source["last_error"] = None
                refreshed_sources.add(source.get("id") or source["name"])
            except Exception as error:
                source["last_error"] = str(error)
                continue

            for item in catalog.get("providers", []):
                if not isinstance(item, dict):
                    continue
                manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else item
                if not isinstance(manifest, dict):
                    continue
                manifest = _normalize_catalog_manifest(manifest, source, commit)
                provider_id = manifest.get("provider_id") or item.get("provider_id")
                version = manifest.get("version") or item.get("version")
                if not provider_id or not version:
                    continue
                key = f"{source['name']}:{provider_id}:{version}"
                source_id = source.get("id") or source["name"]
                entries[key] = {
                    "source": source_id,
                    "source_name": source["name"],
                    "provider_id": provider_id,
                    "name": manifest.get("name") or item.get("name") or provider_id,
                    "version": version,
                    "trusted": bool(source.get("trusted", False)),
                    "manifest": manifest,
                    "resolved_commit": commit,
                }
                refreshed_entry_keys.add(key)
                entries_count += 1
    for key, entry in list(entries.items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("source") in refreshed_sources:
            if key not in refreshed_entry_keys:
                del entries[key]
    save_state(state)
    return {"refreshed_at": now, "sources": sources_count, "entries": entries_count}


def list_providers() -> list[dict[str, Any]]:
    state = load_state()
    return list((state.get("installations") or {}).values())


def get_provider(provider_id: str) -> dict[str, Any] | None:
    state = load_state()
    provider = (state.get("installations") or {}).get(provider_id)
    return provider if isinstance(provider, dict) else None


def _built_in_provider_ids() -> set[str]:
    provider_ids = set(provider_registry.names())
    try:
        from .registry import _REGISTERED_PROVIDER_HUB_IDS
        return provider_ids - _REGISTERED_PROVIDER_HUB_IDS
    except Exception:
        return provider_ids


def _bundle_path_for(manifest) -> Path:
    return (
        provider_hub_dir()
        / "bundles"
        / manifest.provider_id
        / manifest.version
        / manifest.source_commit
    )


def _github_raw_url(manifest, relative_path: str) -> str:
    raw_path = "/".join(part for part in (manifest.source_path, relative_path) if part)
    return f"https://raw.githubusercontent.com/{manifest.source_repo}/{manifest.source_commit}/{raw_path}"


def _fetch_github_bundle_file(manifest, relative_path: str) -> bytes:
    response = requests.get(_github_raw_url(manifest, relative_path), timeout=30)
    response.raise_for_status()
    content = response.content
    if not isinstance(content, bytes):
        raise ProviderHubInstallError(f"GitHub returned invalid content for {relative_path}")
    return content


def _write_manifest_file(manifest, root: Path) -> None:
    (root / "provider.json").write_text(
        json.dumps(manifest.raw, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _fetch_bundle(manifest) -> Path:
    target = _bundle_path_for(manifest)
    if target.exists():
        verify_bundle_tree(manifest, target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=str(target.parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        for relative_path in manifest.files:
            destination = tmp_path / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_fetch_github_bundle_file(manifest, relative_path))

        _write_manifest_file(manifest, tmp_path)
        verify_bundle_tree(manifest, tmp_path)
        shutil.move(str(tmp_path), str(target))

    verify_bundle_tree(manifest, target)
    return target


def _trust_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(manifest)
    if isinstance(normalized.get("source"), dict):
        source = dict(normalized["source"])
        source.pop("trusted", None)
        normalized["source"] = source
    return normalized


def _catalog_manifest_trusted(manifest: dict[str, Any], state: dict[str, Any]) -> bool:
    provider_id = manifest.get("provider_id")
    version = manifest.get("version")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    commit = source.get("commit") if isinstance(source, dict) else None
    sources = state.get("catalog_sources") or {}

    for entry in (state.get("catalog_entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        source_ref = entry.get("source") or entry.get("source_name")
        catalog_source = next(
            (
                item
                for item in sources.values()
                if isinstance(item, dict)
                and source_ref in (item.get("id"), item.get("name"))
            ),
            {},
        )
        if not bool(catalog_source.get("trusted", False)):
            continue
        if entry.get("provider_id") != provider_id or entry.get("version") != version:
            continue
        entry_manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
        if _trust_manifest(entry_manifest) == _trust_manifest(manifest):
            return bool(entry.get("trusted", False))
    return False


def _smoke_validate_worker(manifest, bundle_path: Path, python_path: Path) -> None:
    runner = Path(__file__).with_name("worker_runner.py")
    client = ProviderWorkerClient(
        worker_command(python_path, runner),
        cwd=bundle_path,
        env={
            "BAZARR_PROVIDER_HUB_BUNDLE": str(bundle_path),
            "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(manifest.raw),
        },
    )
    try:
        client.request("health", {}, timeout=10)
    finally:
        client.stop()
        shutil.rmtree(bundle_path / "__pycache__", ignore_errors=True)


def _staged_installation(validated, existing, bundle_path: Path, staged_python_path: Path, source_trusted: bool):
    existing = existing if isinstance(existing, dict) else {}
    return {
        "provider_id": validated.provider_id,
        "name": validated.name,
        "active_version": existing.get("active_version"),
        "staged_version": validated.version,
        "active_path": existing.get("active_path"),
        "python_path": existing.get("python_path"),
        "staged_path": str(bundle_path),
        "staged_python_path": str(staged_python_path),
        "staged_manifest": validated.raw,
        "state": "staged",
        "pending_restart": True,
        "installed_at": utcnow_iso(),
        "activated_at": existing.get("activated_at"),
        "last_error": None,
        "trusted": bool(source_trusted),
        "manifest": existing.get("manifest") if existing.get("active_version") else validated.raw,
    }


def _failed_installation(validated, existing, error: Exception, source_trusted: bool):
    existing = existing if isinstance(existing, dict) else {}
    message = str(error)
    if existing.get("active_version"):
        installation = dict(existing)
        installation["last_error"] = message
        installation["staged_version"] = None
        installation["staged_path"] = None
        installation["staged_python_path"] = None
        installation["staged_manifest"] = None
        installation["pending_restart"] = False
        return installation

    return {
        "provider_id": validated.provider_id,
        "name": validated.name,
        "active_version": None,
        "staged_version": None,
        "active_path": None,
        "python_path": None,
        "staged_path": None,
        "staged_python_path": None,
        "staged_manifest": None,
        "state": "failed",
        "pending_restart": False,
        "installed_at": utcnow_iso(),
        "activated_at": None,
        "last_error": message,
        "trusted": bool(source_trusted),
        "manifest": validated.raw,
    }


def stage_install(manifest: dict[str, Any]) -> dict[str, Any]:
    validated = validate_manifest(manifest, built_in_provider_ids=_built_in_provider_ids())
    state = load_state()
    source_trusted = _catalog_manifest_trusted(validated.raw, state)
    existing = (state.get("installations") or {}).get(validated.provider_id)

    try:
        bundle_path = _fetch_bundle(validated)
        env_path = PluginEnvironment(provider_hub_dir()).install(validated)
        staged_python_path = python_executable(env_path)
        _smoke_validate_worker(validated, bundle_path, staged_python_path)
    except Exception as error:
        state = load_state()
        installations = state.setdefault("installations", {})
        current = installations.get(validated.provider_id, existing)
        installation = _failed_installation(validated, current, error, source_trusted)
        installations[validated.provider_id] = installation
        save_state(state)
        raise ProviderHubInstallError(str(error)) from error

    state = load_state()
    installations = state.setdefault("installations", {})
    current = installations.get(validated.provider_id, existing)
    installation = _staged_installation(validated, current, bundle_path, staged_python_path, source_trusted)
    installations[validated.provider_id] = installation
    save_state(state)
    return installation


def activate_staged_installations() -> list[str]:
    state = load_state()
    activated = []
    changed = False
    for provider_id, installation in (state.get("installations") or {}).items():
        if not isinstance(installation, dict):
            continue
        if not installation.get("pending_restart") or installation.get("state") != "staged":
            continue
        try:
            manifest = validate_manifest(installation.get("manifest") or {}, built_in_provider_ids=_built_in_provider_ids())
            if isinstance(installation.get("staged_manifest"), dict):
                manifest = validate_manifest(
                    installation.get("staged_manifest"),
                    built_in_provider_ids=_built_in_provider_ids(),
                )
            staged_path = installation.get("staged_path")
            staged_python_path = installation.get("staged_python_path")
            if not staged_path or not staged_python_path:
                raise ProviderHubInstallError("staged bundle or python path is missing")
            verify_bundle_tree(manifest, staged_path)
            _smoke_validate_worker(manifest, Path(staged_path), Path(staged_python_path))
        except Exception as error:
            installation["last_error"] = str(error)
            installation["staged_version"] = None
            installation["staged_path"] = None
            installation["staged_python_path"] = None
            installation["staged_manifest"] = None
            installation["pending_restart"] = False
            if installation.get("active_version"):
                installation["state"] = "active"
            else:
                installation["state"] = "failed"
            changed = True
            continue
        installation["active_version"] = installation.get("staged_version")
        if installation.get("staged_path"):
            installation["active_path"] = installation.get("staged_path")
        if installation.get("staged_python_path"):
            installation["python_path"] = installation.get("staged_python_path")
        installation["staged_version"] = None
        installation["staged_path"] = None
        installation["staged_python_path"] = None
        installation["staged_manifest"] = None
        installation["manifest"] = manifest.raw
        installation["state"] = "active"
        installation["pending_restart"] = False
        installation["last_error"] = None
        installation["activated_at"] = utcnow_iso()
        activated.append(provider_id)
        changed = True
    if changed:
        save_state(state)
    return activated


def remove_installation(provider_id: str) -> bool:
    state = load_state()
    installations = state.setdefault("installations", {})
    if provider_id not in installations:
        return False
    item = installations[provider_id]
    if isinstance(item, dict):
        item["state"] = "removed"
        item["pending_restart"] = True
    save_state(state)
    return True


def check_updates() -> dict[str, Any]:
    state = load_state()
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "action": "check_updates",
        "state": "completed",
        "message": "Catalog metadata checked. Untrusted updates require manual staging.",
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
    state.setdefault("jobs", []).append(job)
    save_state(state)
    return job


def apply_update(provider_id: str) -> dict[str, Any] | None:
    provider = get_provider(provider_id)
    if not provider:
        return None
    manifest = provider.get("available_manifest")
    if not isinstance(manifest, dict):
        provider["last_error"] = "No update manifest is available"
        state = load_state()
        state.setdefault("installations", {})[provider_id] = provider
        save_state(state)
        return provider
    try:
        return stage_install(manifest)
    except ProviderHubInstallError:
        return get_provider(provider_id)


def list_jobs() -> list[dict[str, Any]]:
    return list(load_state().get("jobs") or [])


def get_job(job_id: str) -> dict[str, Any] | None:
    for job in list_jobs():
        if job.get("id") == job_id:
            return job
    return None

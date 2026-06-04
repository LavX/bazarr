# coding=utf-8
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import time
import uuid
import zipfile

import requests

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from subliminal_patch.extensions import provider_registry

from .manifest import validate_manifest
from .bundle import verify_bundle_tree
from .migration import MIGRATED_BUILT_IN_PROVIDER_IDS, validation_built_in_provider_ids
from .state import (
    OFFICIAL_CATALOG_SOURCE_ID,
    OFFICIAL_CATALOG_URL,
    catalog_source_for_entry,
    load_state,
    mutate_state,
    provider_hub_dir,
    spoofs_official_catalog_source,
)
from .venv import PluginEnvironment, python_executable
from .worker import ProviderWorkerClient, worker_command


class CatalogSourceError(ValueError):
    """Raised when a Provider Hub catalog source is not allowed."""


class ProviderHubInstallError(RuntimeError):
    """Raised when a Provider Hub install could not be staged."""


SECRET_PLACEHOLDER = "********"
_VERSION_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")
_SEMVER_RE = re.compile(
    r"^\s*v?(?P<core>\d+(?:\.\d+)*)(?:-(?P<prerelease>[0-9A-Za-z.-]+))?\s*$"
)
_JOB_LOG_LIMIT = 200


def _bazarr_enabled_providers() -> list[str]:
    """Return Bazarr's current enabled-providers list, or [] if unavailable.

    ``settings.general.enabled_providers`` is the actual gate that decides
    which providers run during a search. The hub installation row's stored
    ``enabled`` flag is a local copy that historically drifted out of sync
    with this list. Reading the canonical value lazily avoids a circular
    import (service.py is imported during init before settings finishes
    loading on some code paths) and keeps the hub view honest.
    """
    try:
        from app.config import settings
    except Exception:
        return []
    raw = getattr(getattr(settings, "general", None), "enabled_providers", None)
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        trimmed = raw.strip().strip("[]")
        return [item.strip().strip("'\"") for item in trimmed.split(",") if item.strip()]
    return []


def _set_bazarr_provider_enabled(provider_id: str, enabled: bool) -> bool:
    """Add ``provider_id`` to (or remove it from) Bazarr's enabled_providers.

    Returns True when the on-disk config changed. Logs and swallows
    failures so a hub action never aborts on a settings hiccup.
    """
    try:
        from app.config import settings, write_config
    except Exception:
        return False
    current = list(_bazarr_enabled_providers())
    if enabled and provider_id not in current:
        current.append(provider_id)
    elif not enabled and provider_id in current:
        current = [item for item in current if item != provider_id]
    else:
        return False
    try:
        settings.general.enabled_providers = current
        write_config()
        return True
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "Failed to sync enabled_providers for %s", provider_id
        )
        return False


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _persist_job(job: dict[str, Any]) -> None:
    def persist(state: dict[str, Any]) -> None:
        jobs = state.setdefault("jobs", [])
        if not isinstance(jobs, list):
            jobs = []
            state["jobs"] = jobs
        job_id = job.get("id")
        replaced = False
        for index, existing in enumerate(jobs):
            if isinstance(existing, dict) and existing.get("id") == job_id:
                jobs[index] = dict(job)
                replaced = True
                break
        if not replaced:
            jobs.append(dict(job))
        if len(jobs) > _JOB_LOG_LIMIT:
            del jobs[: len(jobs) - _JOB_LOG_LIMIT]

    mutate_state(persist)


class _JobHandle:
    """Mutable wrapper for the dict callers receive inside ``record_job``."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    def update(self, **fields: Any) -> None:
        """Merge ``fields`` into the job payload and persist immediately.

        ``details`` is shallow-merged so callers can extend the metadata bag
        across multiple stages without clobbering earlier keys.
        """
        details = fields.pop("details", None)
        if details is not None:
            current = self.data.get("details") or {}
            if not isinstance(current, dict):
                current = {}
            current.update(details)
            self.data["details"] = current
        for key, value in fields.items():
            self.data[key] = value
        self.data["updated_at"] = utcnow_iso()
        _persist_job(self.data)


@contextlib.contextmanager
def record_job(
    action: str,
    *,
    target_kind: str = "system",
    target_id: str | None = None,
    target_name: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    from_version: str | None = None,
    to_version: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
):
    """Record the full lifecycle of a Provider Hub action.

    Writes one job row that transitions pending -> running -> completed/failed,
    capturing duration and any exception trace. The yielded handle exposes
    ``data`` (the live job dict) and ``update(**fields)`` so callers can fill
    in fields they only learn mid-flight (e.g. resolved ``to_version``).
    """
    job_id = str(uuid.uuid4())
    now = utcnow_iso()
    job = {
        "id": job_id,
        "action": action,
        "state": "pending",
        "target_kind": target_kind,
        "target_id": target_id,
        "target_name": target_name,
        "source_id": source_id,
        "source_name": source_name,
        "from_version": from_version,
        "to_version": to_version,
        "message": message,
        "error": None,
        "details": dict(details) if isinstance(details, dict) else {},
        "duration_ms": None,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
        "updated_at": now,
    }
    handle = _JobHandle(job)
    _persist_job(job)
    started_perf = time.perf_counter()
    job["state"] = "running"
    job["started_at"] = utcnow_iso()
    job["updated_at"] = job["started_at"]
    _persist_job(job)
    try:
        yield handle
    except BaseException as error:
        job["state"] = "failed"
        job["error"] = str(error) or error.__class__.__name__
        job["completed_at"] = utcnow_iso()
        job["updated_at"] = job["completed_at"]
        job["duration_ms"] = int((time.perf_counter() - started_perf) * 1000)
        _persist_job(job)
        raise
    else:
        if job.get("state") != "failed":
            job["state"] = "completed"
        job["completed_at"] = utcnow_iso()
        job["updated_at"] = job["completed_at"]
        job["duration_ms"] = int((time.perf_counter() - started_perf) * 1000)
        _persist_job(job)


_DEV_REF_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,200}$")


def _validate_dev_ref(dev_ref):
    if dev_ref is None:
        return None
    if not isinstance(dev_ref, str):
        raise CatalogSourceError("dev_ref must be a string or null")
    trimmed = dev_ref.strip()
    if not trimmed:
        return None
    if not _DEV_REF_RE.match(trimmed):
        raise CatalogSourceError(
            "dev_ref contains characters not allowed in a git ref"
        )
    return trimmed


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


def _fetch_github_catalog(
    url: str, override_ref: str | None = None
) -> tuple[dict[str, Any], str]:
    owner, repo, ref, path = _parse_github_file_url(url)
    if override_ref:
        ref = override_ref
    commit = _resolve_github_ref(owner, repo, ref)
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}"
    response = requests.get(raw_url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise CatalogSourceError("Catalog payload must be a JSON object")
    return payload, commit


def _catalog_source_error_message(error: Exception) -> str:
    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return (
            "Could not reach GitHub while refreshing this catalog source. "
            "Check network or DNS and try again."
        )
    if isinstance(error, requests.exceptions.HTTPError):
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code:
            return f"GitHub returned HTTP {status_code} while refreshing this catalog source."
        return "GitHub returned an error while refreshing this catalog source."
    return str(error)


def add_catalog_source(
    name: str, url: str, trusted: bool = False, dev_ref: str | None = None
) -> dict[str, Any]:
    with record_job(
        "add_source",
        target_kind="source",
        target_id=name,
        target_name=name,
    ) as job:
        if not name or not isinstance(name, str):
            raise CatalogSourceError("Catalog source name is required")
        name = name.strip()
        if not name:
            raise CatalogSourceError("Catalog source name is required")
        url = _validate_github_catalog_url(url)
        if spoofs_official_catalog_source(name):
            raise CatalogSourceError("The official catalog source name is reserved")
        is_official = name == OFFICIAL_CATALOG_SOURCE_ID and url == OFFICIAL_CATALOG_URL
        dev_ref_value = _validate_dev_ref(dev_ref)

        source = {
            "id": name,
            "name": name,
            "type": "github",
            "url": url,
            "enabled": True,
            "official": is_official,
            "trusted": is_official,
            "dev_ref": dev_ref_value,
            "last_checked_at": None,
            "last_error": None,
        }
        def store_source(state: dict[str, Any]) -> dict[str, Any]:
            state.setdefault("catalog_sources", {})[name] = source
            return dict(source)

        source = mutate_state(store_source)
        job.update(
            source_id=name,
            source_name=name,
            message=f"Added catalog source '{name}'",
            details={"url": url, "trusted": is_official, "dev_ref": dev_ref_value},
        )
        return source


def remove_catalog_source(name: str) -> bool:
    with record_job(
        "remove_source",
        target_kind="source",
        target_id=name,
        target_name=name,
    ) as job:
        if name == OFFICIAL_CATALOG_SOURCE_ID:
            job.update(message="Refused to remove the official catalog source")
            return False

        def remove_source(state: dict[str, Any]) -> tuple[bool, str]:
            sources = state.setdefault("catalog_sources", {})
            if name not in sources:
                return False, name
            removed_name = sources[name].get("name", name) if isinstance(sources[name], dict) else name
            del sources[name]
            # Drop catalog_entries that came from this source. Without this purge
            # the marketplace keeps offering stale entries and update checks may
            # match them as available upgrades, which surprises the user.
            entries = state.setdefault("catalog_entries", {})
            for key, entry in list(entries.items()):
                if not isinstance(entry, dict):
                    continue
                if entry.get("source") == name or entry.get("source_name") == removed_name:
                    del entries[key]
            return True, removed_name

        removed, _removed_name = mutate_state(remove_source)
        if not removed:
            job.update(message=f"Catalog source '{name}' not found")
            return False
        job.update(message=f"Removed catalog source '{name}'")
        return True


_UNSET = object()


def _find_catalog_source(state: dict[str, Any], identifier: str):
    """Return ``(key, source_dict)`` matching ``identifier`` by id or name.

    Catalog sources are stored under their ``id`` (or the explicit name the
    caller passed to add_catalog_source). The official source keys on
    ``"official"`` but exposes the long display name to the UI, so callers
    can hit either; we look up by exact key first and fall back to matching
    the ``name`` field.
    """
    sources = state.get("catalog_sources") or {}
    source = sources.get(identifier)
    if isinstance(source, dict):
        return identifier, source
    for key, candidate in sources.items():
        if isinstance(candidate, dict) and candidate.get("name") == identifier:
            return key, candidate
    return None, None


def update_catalog_source(name: str, dev_ref=_UNSET) -> dict[str, Any] | None:
    """Update a catalog source in place.

    Currently only ``dev_ref`` is mutable. Leave it unset to no-op; pass
    ``None`` to clear it; pass a string to set it. The validated ref is
    stored on the source dict. Returns the updated source, or ``None`` if no
    source matches the identifier (by id or by display name).
    """
    with record_job(
        "update_source",
        target_kind="source",
        target_id=name,
        target_name=name,
    ) as job:
        validated_dev_ref = _validate_dev_ref(dev_ref) if dev_ref is not _UNSET else _UNSET

        def update_source(state: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
            _key, source = _find_catalog_source(state, name)
            if source is None:
                return None, None
            previous_dev_ref = source.get("dev_ref")
            if dev_ref is not _UNSET:
                source["dev_ref"] = validated_dev_ref
            return dict(source), previous_dev_ref

        source, previous_dev_ref = mutate_state(update_source)
        if source is None:
            job.update(message=f"Catalog source '{name}' not found")
            return None
        job.update(
            source_id=source.get("id"),
            source_name=source.get("name"),
            from_version=previous_dev_ref,
            to_version=source.get("dev_ref"),
            message=f"Updated catalog source '{source.get('name') or name}'",
            details={"dev_ref": source.get("dev_ref")},
        )
        return source


def get_catalog_source(name: str) -> dict[str, Any] | None:
    state = load_state()
    _key, source = _find_catalog_source(state, name)
    return source


def _catalog_needs_auto_refresh(state: dict[str, Any]) -> bool:
    for source in (state.get("catalog_sources") or {}).values():
        if not isinstance(source, dict):
            continue
        if source.get("enabled", True) and source.get("last_checked_at") is None:
            return True
    return False


def list_catalog(auto_refresh: bool = False) -> dict[str, Any]:
    state = load_state()
    if auto_refresh and _catalog_needs_auto_refresh(state):
        refresh_catalog()
        state = load_state()
    return {
        "sources": list((state.get("catalog_sources") or {}).values()),
        "entries": list((state.get("catalog_entries") or {}).values()),
    }


def _latest_official_catalog_manifest(state: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
    """Latest manifest for ``provider_id`` from the OFFICIAL (trusted) catalog source."""
    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for entry in (state.get("catalog_entries") or {}).values():
        if not isinstance(entry, dict) or entry.get("provider_id") != provider_id:
            continue
        if entry.get("source") != OFFICIAL_CATALOG_SOURCE_ID or not entry.get("trusted", False):
            continue
        manifest = entry.get("manifest")
        if not isinstance(manifest, dict):
            continue
        version_key = _version_key(manifest.get("version") or entry.get("version"))
        if version_key is None:
            continue
        candidates.append((version_key, manifest))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def autoinstall_enabled_builtins() -> list[str]:
    """Stage-install official-catalog versions of enabled providers not yet installed.

    For each provider in Bazarr's ``enabled_providers`` that (a) has no Provider Hub
    installation row yet and (b) has a matching entry in the OFFICIAL catalog, stage
    the latest official manifest. The caller's ``activate_staged_installations()`` +
    ``register_active_provider_classes()`` then activate and shadow the built-in in the
    same boot (catalog plugins reuse the built-in id). Idempotent and best-effort:
    never raises, never enables/disables/uninstalls anything.
    """
    import logging
    import os

    logger = logging.getLogger(__name__)
    if os.environ.get("BAZARR_DISABLE_PROVIDER_AUTOINSTALL"):
        return []

    logger.info("Provider Hub startup auto-install: reconciling enabled providers against the official catalog")

    try:
        state = load_state()
        # Skip rows that are live or mid-lifecycle, but NOT "failed": a transient
        # bundle/venv error should be retried on a later boot rather than blocking
        # the migration forever.
        existing = {
            provider_id
            for provider_id, row in (state.get("installations") or {}).items()
            if isinstance(row, dict) and row.get("state") != "failed"
        }
        enabled = _bazarr_enabled_providers()
    except Exception:
        logger.exception("Provider Hub startup auto-install: could not load state; skipping")
        return []

    candidates = [provider_id for provider_id in enabled if provider_id not in existing]
    if not candidates:
        # Everything enabled is already installed: no migration work, so don't touch
        # the network at all. Keeps the common boot fast and independent of catalog
        # source health.
        return []

    # There is potential work, so force a fresh OFFICIAL catalog (only) to pick up a
    # newly-merged provider id. Best-effort: refresh_catalog isolates per-source
    # failures and the call is guarded, so a slow/broken catalog can never block or
    # crash boot, and unrelated third-party sources are not fetched on the boot path.
    try:
        refresh_catalog(source_ids={OFFICIAL_CATALOG_SOURCE_ID})
        state = load_state()
    except Exception:
        logger.exception("Provider Hub startup auto-install: catalog refresh failed; using cached catalog")

    budget_seconds = 180.0
    started = time.monotonic()
    staged: list[str] = []
    for provider_id in candidates:
        manifest = _latest_official_catalog_manifest(state, provider_id)
        if manifest is None:
            continue
        remaining = budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            logger.warning(
                "Provider Hub startup auto-install: time budget exceeded at %s; deferring remaining to next start",
                provider_id,
            )
            break
        try:
            # Bound the install itself by the remaining budget so one stuck pip/venv
            # step can't block boot indefinitely; failures are swallowed and the
            # built-in keeps serving until the next start.
            stage_install(manifest, install_timeout=remaining)
            staged.append(provider_id)
            logger.info("Provider Hub startup auto-install: staged %s from the official catalog", provider_id)
        except Exception:
            logger.exception("Provider Hub startup auto-install: failed to stage %s; keeping built-in", provider_id)
    return staged


def _normalize_catalog_manifest(manifest: dict[str, Any], source: dict[str, Any], commit: str) -> dict[str, Any]:
    normalized = dict(manifest)
    manifest_source = dict(normalized.get("source") or {})
    manifest_source.setdefault("type", "github")
    manifest_source.setdefault("catalog_url", source.get("url"))
    manifest_source["commit"] = commit
    manifest_source["trusted"] = bool(source.get("trusted", False))
    normalized["source"] = manifest_source
    return normalized


def refresh_catalog(source_ids: set[str] | None = None) -> dict[str, Any]:
    """Refresh catalog sources. When ``source_ids`` is given, only those source ids
    are fetched (e.g. the startup migration refreshes only the official source so an
    unrelated slow/broken third-party source can't delay boot); otherwise all are."""
    with record_job("refresh_catalog", target_kind="system") as job:
        def refresh(state: dict[str, Any]) -> dict[str, Any]:
            now = utcnow_iso()
            entries = state.setdefault("catalog_entries", {})
            refreshed_sources = set()
            refreshed_entry_keys = set()
            entries_count = 0
            sources_count = 0
            failed_sources: list[str] = []
            for source in (state.get("catalog_sources") or {}).values():
                if not isinstance(source, dict):
                    continue
                if source_ids is not None and source.get("id") not in source_ids:
                    continue
                sources_count += 1
                source["last_checked_at"] = now
                try:
                    catalog, commit = _fetch_github_catalog(
                        source["url"], override_ref=source.get("dev_ref")
                    )
                    source["resolved_commit"] = commit
                    source["last_error"] = None
                    refreshed_sources.add(source.get("id") or source["name"])
                except Exception as error:
                    source["last_error"] = _catalog_source_error_message(error)
                    failed_sources.append(source.get("name") or source.get("id") or "?")
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
            ok_sources = sources_count - len(failed_sources)
            return {
                "refreshed_at": now,
                "sources": sources_count,
                "entries": entries_count,
                "sources_ok": ok_sources,
                "failed_sources": failed_sources,
            }

        result = mutate_state(refresh)
        sources_count = result["sources"]
        entries_count = result["entries"]
        ok_sources = result["sources_ok"]
        failed_sources = result["failed_sources"]
        if failed_sources:
            summary = (
                f"Refreshed {ok_sources}/{sources_count} sources, "
                f"{entries_count} entries. Failed: {', '.join(failed_sources)}"
            )
        else:
            summary = f"Refreshed {sources_count} source(s), {entries_count} entries"
        job.update(
            message=summary,
            details={
                "sources_total": sources_count,
                "sources_ok": ok_sources,
                "sources_failed": failed_sources,
                "entries": entries_count,
            },
        )
        return {
            "refreshed_at": result["refreshed_at"],
            "sources": sources_count,
            "entries": entries_count,
        }


def _manifest_secret_fields(installation: dict[str, Any]) -> set[str]:
    manifest = installation.get("manifest")
    if not isinstance(manifest, dict):
        manifest = installation.get("staged_manifest")
    if not isinstance(manifest, dict):
        return set()
    secret_fields = manifest.get("secret_fields") or []
    if not isinstance(secret_fields, list):
        return set()
    return {item for item in secret_fields if isinstance(item, str)}


def _encrypt_secret_value(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        from secret_store import encrypt_secret
        return encrypt_secret(value)
    except Exception:
        return value


def _decrypt_secret_value(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        from secret_store import decrypt_secret
        return decrypt_secret(value)
    except Exception:
        return value


def _redact_installation(installation: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(installation)
    provider_id = redacted.get("provider_id")
    if provider_id:
        # enabled_providers in config.yaml is the source of truth; the stored
        # field on the hub row is a stale shadow we don't trust on read.
        redacted["enabled"] = str(provider_id) in _bazarr_enabled_providers()
    config = redacted.get("config")
    if not isinstance(config, dict):
        return redacted
    secret_fields = _manifest_secret_fields(redacted)
    redacted_config = dict(config)
    for field in secret_fields:
        if redacted_config.get(field):
            redacted_config[field] = SECRET_PLACEHOLDER
    redacted["config"] = redacted_config
    return redacted


def _effective_installation_config(installation: dict[str, Any]) -> dict[str, Any]:
    config = installation.get("config")
    if not isinstance(config, dict):
        return {}
    secret_fields = _manifest_secret_fields(installation)
    effective = dict(config)
    for field in secret_fields:
        if field in effective:
            effective[field] = _decrypt_secret_value(effective[field])
    return effective


def update_provider(provider_id: str, enabled: bool | None = None, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError("config must be an object")

    def update_installation(state: dict[str, Any]) -> dict[str, Any] | None:
        provider = (state.get("installations") or {}).get(provider_id)
        if not isinstance(provider, dict):
            return None

        if enabled is not None:
            provider["enabled"] = bool(enabled)
            _set_bazarr_provider_enabled(provider_id, bool(enabled))

        if config is not None:
            secret_fields = _manifest_secret_fields(provider)
            current = provider.get("config")
            next_config = dict(current if isinstance(current, dict) else {})
            for key, value in config.items():
                if key in secret_fields and value == SECRET_PLACEHOLDER:
                    continue
                next_config[key] = _encrypt_secret_value(value) if key in secret_fields else value
            provider["config"] = next_config

        state.setdefault("installations", {})[provider_id] = provider
        return dict(provider)

    provider = mutate_state(update_installation)
    if provider is None:
        return None
    return _redact_installation(provider)


def runtime_provider_configs() -> dict[str, dict[str, Any]]:
    state = load_state()
    configs = {}
    for provider_id, installation in (state.get("installations") or {}).items():
        if not isinstance(installation, dict):
            continue
        # The active provider class is keyed off ``active_version`` and stays
        # registered until restart, even while an update or removal is staged.
        # Filtering on ``state == active`` would strip the config from a
        # pending-restart row and the live search would run without
        # credentials. Use ``active_version`` as the gate instead.
        if not installation.get("active_version"):
            continue
        configs[provider_id] = _effective_installation_config(installation)
    return configs


def list_providers(redact: bool = True) -> list[dict[str, Any]]:
    state = load_state()
    providers = [
        _with_origin(provider, state) if isinstance(provider, dict) else provider
        for provider in (state.get("installations") or {}).values()
    ]
    if not redact:
        return providers
    return [
        _redact_installation(provider) if isinstance(provider, dict) else provider
        for provider in providers
    ]


def get_provider(provider_id: str, redact: bool = True) -> dict[str, Any] | None:
    state = load_state()
    provider = (state.get("installations") or {}).get(provider_id)
    if not isinstance(provider, dict):
        return None
    provider = _with_origin(provider, state)
    return _redact_installation(provider) if redact else provider


def _record_provider_last_error(provider_id: str, message: str | None) -> None:
    def update_error(state: dict[str, Any]) -> None:
        provider = state.setdefault("installations", {}).get(provider_id)
        if not isinstance(provider, dict):
            return
        provider["last_error"] = message

    mutate_state(update_error)


def _remove_bundle_runtime_artifacts(bundle_path: Path) -> None:
    for cache_dir in list(bundle_path.rglob("__pycache__")):
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_provider_connection(provider_id: str) -> dict[str, Any] | None:
    provider = get_provider(provider_id, redact=False)
    if not provider:
        return None

    target_name = provider.get("name") or provider_id

    with record_job(
        "test_connection",
        target_kind="provider",
        target_id=provider_id,
        target_name=target_name,
    ) as job:
        if provider.get("pending_restart") or provider.get("state") != "active":
            job.update(
                message="Restart Bazarr+ before testing this staged plugin",
                details={"status": "pending_restart"},
            )
            return {
                "provider_id": provider_id,
                "ok": False,
                "status": "pending_restart",
                "message": "Restart Bazarr+ before testing this staged plugin",
            }

        manifest_data = provider.get("manifest")
        active_path = provider.get("active_path")
        python_path = provider.get("python_path")

        try:
            if not isinstance(manifest_data, dict):
                raise ProviderHubInstallError("active manifest is missing")
            if not active_path:
                raise ProviderHubInstallError("active bundle path is missing")
            if not python_path:
                raise ProviderHubInstallError("provider Python path is missing")

            trusted = bool(provider.get("trusted", False))
            manifest = validate_manifest(
                manifest_data,
                built_in_provider_ids=_validation_built_in_provider_ids(
                    manifest_data,
                    trusted,
                ),
            )
            bundle_path = Path(active_path)
            _remove_bundle_runtime_artifacts(bundle_path)
            verify_bundle_tree(manifest, bundle_path)

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
                result = client.request("health", {}, timeout=10)
            finally:
                client.stop()
                _remove_bundle_runtime_artifacts(bundle_path)

            _record_provider_last_error(provider_id, None)
            job.update(
                message="Worker health check passed",
                details={"status": "ready"},
            )
            return {
                "provider_id": provider_id,
                "ok": True,
                "status": "ready",
                "message": "Worker health check passed",
                "details": result.payload,
            }
        except Exception as error:
            message = str(error) or error.__class__.__name__
            _record_provider_last_error(provider_id, message)
            job.data["state"] = "failed"
            job.update(
                error=message,
                message=message,
                details={"status": "failed"},
            )
            return {
                "provider_id": provider_id,
                "ok": False,
                "status": "failed",
                "message": message,
            }


def _built_in_provider_ids() -> set[str]:
    provider_ids = set(provider_registry.names())
    try:
        from .registry import _REGISTERED_PROVIDER_HUB_IDS
        provider_ids = provider_ids - _REGISTERED_PROVIDER_HUB_IDS
    except Exception:
        pass
    # Keep migrated built-in ids in the denylist even after a hub provider has
    # registered one, so an untrusted install of the same id can never shadow it.
    return provider_ids | MIGRATED_BUILT_IN_PROVIDER_IDS


def _manifest_provider_id(manifest: dict[str, Any]) -> str:
    if not isinstance(manifest, dict):
        return ""
    return str(manifest.get("provider_id") or "")


def _validation_built_in_provider_ids(manifest: dict[str, Any], trusted: bool) -> set[str]:
    return validation_built_in_provider_ids(
        _manifest_provider_id(manifest),
        _built_in_provider_ids(),
        trusted,
    )


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


def _deadline_request_timeout(deadline: float | None, default: float = 30.0) -> float:
    """Per-request timeout bounded by an optional wall-clock deadline (monotonic).

    Raises ``ProviderHubInstallError`` when the deadline has already passed so a
    slow bundle fetch can't run the startup auto-install past its time budget.
    """
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderHubInstallError("bundle fetch exceeded the install time budget")
    return max(1.0, min(default, remaining))


def _fetch_github_bundle_file(manifest, relative_path: str, deadline: float | None = None) -> bytes:
    response = requests.get(
        _github_raw_url(manifest, relative_path),
        timeout=_deadline_request_timeout(deadline),
    )
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


def _fetch_bundle(manifest, deadline: float | None = None) -> Path:
    target = _bundle_path_for(manifest)
    if target.exists():
        _remove_bundle_runtime_artifacts(target)
        verify_bundle_tree(manifest, target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=str(target.parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        for relative_path in manifest.files:
            destination = tmp_path / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_fetch_github_bundle_file(manifest, relative_path, deadline=deadline))

        _write_manifest_file(manifest, tmp_path)
        verify_bundle_tree(manifest, tmp_path)
        shutil.move(str(tmp_path), str(target))

    verify_bundle_tree(manifest, target)
    return target


_LOCAL_PACKAGE_MAX_MEMBERS = 5000
_LOCAL_PACKAGE_MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MB uncompressed


def _safe_extract_zip(archive_bytes: bytes, dest: Path) -> None:
    """Extract a zip into ``dest`` with zip-slip, size and read-error guards."""
    dest_root = dest.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as error:
        raise ProviderHubInstallError("uploaded package is not a valid .zip archive") from error
    with archive:
        infos = archive.infolist()
        # Bound the work before extracting so an oversized or zip-bomb upload
        # cannot fill the disk or stall the install.
        if len(infos) > _LOCAL_PACKAGE_MAX_MEMBERS:
            raise ProviderHubInstallError("package contains too many files")
        if sum(max(0, info.file_size) for info in infos) > _LOCAL_PACKAGE_MAX_TOTAL_BYTES:
            raise ProviderHubInstallError("package is too large")
        for member in infos:
            name = member.filename
            if not name or name.endswith("/"):
                continue
            target = (dest / name).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise ProviderHubInstallError(f"unsafe path in package: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
            except (RuntimeError, zipfile.BadZipFile, OSError) as error:
                # Encrypted/corrupt members raise here, outside the open() guard
                # above; surface them as a 400 invalid-package, not a 500.
                raise ProviderHubInstallError(
                    f"could not read package entry {name}: {error}"
                ) from error


def _find_local_manifest(extracted: Path) -> tuple[dict[str, Any], Path]:
    """Locate the manifest inside an extracted local package and its bundle root."""
    candidates = sorted(
        list(extracted.rglob("provider.json")) + list(extracted.rglob("manifest.json")),
        key=lambda path: len(path.parts),
    )
    if not candidates:
        raise ProviderHubInstallError("package must contain a provider.json manifest")
    manifest_file = candidates[0]
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProviderHubInstallError(f"invalid manifest in package: {error}") from error
    if not isinstance(manifest, dict):
        raise ProviderHubInstallError("package manifest must be a JSON object")
    return manifest, manifest_file.parent


def _stage_local_bundle(validated, source_dir: Path) -> Path:
    """Stage a bundle from already-extracted local files (no network fetch)."""
    target = _bundle_path_for(validated)
    if target.exists():
        _remove_bundle_runtime_artifacts(target)
        verify_bundle_tree(validated, target)
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=str(target.parent)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        for relative_path in validated.files:
            source_file = source_dir / relative_path
            if not source_file.is_file():
                raise ProviderHubInstallError(f"package is missing declared file: {relative_path}")
            destination = tmp_path / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source_file.read_bytes())

        _write_manifest_file(validated, tmp_path)
        verify_bundle_tree(validated, tmp_path)
        shutil.move(str(tmp_path), str(target))

    verify_bundle_tree(validated, target)
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
    sources = state.get("catalog_sources") or {}

    for entry in (state.get("catalog_entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        catalog_source = catalog_source_for_entry(sources, entry)
        if not bool(catalog_source.get("trusted", False)):
            continue
        if entry.get("provider_id") != provider_id or entry.get("version") != version:
            continue
        entry_manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
        if _trust_manifest(entry_manifest) == _trust_manifest(manifest):
            return bool(entry.get("trusted", False))
    return False


def _catalog_source_id_for_manifest(manifest: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Return the configured catalog source id whose url matches the manifest's
    catalog_url, or None when no configured source matches."""
    if not isinstance(manifest, dict):
        return None
    source = manifest.get("source")
    catalog_url = source.get("catalog_url") if isinstance(source, dict) else None
    if not catalog_url:
        return None
    for source_id, configured in (state.get("catalog_sources") or {}).items():
        if isinstance(configured, dict) and configured.get("url") == catalog_url:
            return source_id
    return None


def _install_origin(manifest: dict[str, Any], state: dict[str, Any]) -> tuple[str, str | None]:
    """Resolve (origin, source_id) for a catalog install.

    A catalog install is always origin="catalog"; the source_id is the configured
    source matching the manifest's catalog_url, or None if none is configured (the
    source may have been removed, or this is an older install). "local" is reserved
    for uploads, which stage_install_local records explicitly. This is for
    display/grouping only and is independent of the trust gate.
    """
    return ("catalog", _catalog_source_id_for_manifest(manifest, state))


def _with_origin(provider: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Ensure a serialized installation carries origin/source_id.

    A stored origin of "local" is authoritative (a local upload is never
    relabelled as a catalog provider). Otherwise it is a catalog install and the
    source_id is resolved from the manifest's catalog_url against the current
    sources, so source-config changes are reflected on read.
    """
    result = dict(provider)
    if result.get("origin") == "local":
        result.setdefault("source_id", result.get("source_id"))
        return result
    result["origin"] = "catalog"
    result["source_id"] = _catalog_source_id_for_manifest(
        result.get("manifest") or {}, state
    )
    return result


def _smoke_validate_worker(manifest, bundle_path: Path, python_path: Path) -> None:
    _remove_bundle_runtime_artifacts(bundle_path)
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
        _remove_bundle_runtime_artifacts(bundle_path)


def _staged_installation(
    validated,
    existing,
    bundle_path: Path,
    staged_python_path: Path,
    source_trusted: bool,
    origin: str = "catalog",
    source_id: str | None = None,
):
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
        "origin": origin,
        "source_id": source_id,
        "manifest": existing.get("manifest") if existing.get("active_version") else validated.raw,
        "enabled": existing.get("enabled", True),
        "config": existing.get("config", {}),
    }


def _failed_installation(
    validated,
    existing,
    error: Exception,
    source_trusted: bool,
    origin: str = "catalog",
    source_id: str | None = None,
):
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
        "origin": origin,
        "source_id": source_id,
        "manifest": validated.raw,
        "enabled": existing.get("enabled", True),
        "config": existing.get("config", {}),
    }


def _stage_validated(
    validated,
    source_trusted: bool,
    origin: str,
    source_id: str | None,
    bundle_stager,
    catalog_url: str | None = None,
    install_timeout: float | None = None,
) -> dict[str, Any]:
    """Shared install core: stage the bundle, build the venv, smoke-test, record.

    ``bundle_stager(validated, deadline) -> Path`` decides where the bundle comes
    from (catalog fetch vs already-extracted local files). ``install_timeout``, when
    set, is a single wall-clock budget for the whole stage (bundle fetch + venv
    build) shared via ``deadline``, so the startup auto-install can't run past it;
    ``None`` (manual installs) leaves it unbounded.
    """
    state = load_state()
    existing = (state.get("installations") or {}).get(validated.provider_id)
    existing_version = (
        existing.get("active_version") if isinstance(existing, dict) else None
    )
    is_update = bool(existing_version)
    action = "stage_update" if is_update else "install"

    with record_job(
        action,
        target_kind="provider",
        target_id=validated.provider_id,
        target_name=validated.raw.get("name") or validated.provider_id,
        from_version=existing_version,
        to_version=validated.version,
        details={"catalog_url": catalog_url, "trusted": source_trusted, "origin": origin},
    ) as job:
        try:
            deadline = None if install_timeout is None else time.monotonic() + max(0.0, install_timeout)
            bundle_path = bundle_stager(validated, deadline)
            install_remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            env_path = PluginEnvironment(provider_hub_dir()).install(validated, timeout=install_remaining)
            staged_python_path = python_executable(env_path)
            _smoke_validate_worker(validated, bundle_path, staged_python_path)
        except Exception as error:
            install_error = error

            def record_failed_install(state: dict[str, Any]) -> dict[str, Any]:
                installations = state.setdefault("installations", {})
                current = installations.get(validated.provider_id, existing)
                installation = _failed_installation(
                    validated, current, install_error, source_trusted, origin, source_id
                )
                installations[validated.provider_id] = installation
                return dict(installation)

            mutate_state(record_failed_install)
            raise ProviderHubInstallError(str(install_error)) from install_error

        def record_staged_install(state: dict[str, Any]) -> dict[str, Any]:
            installations = state.setdefault("installations", {})
            current = installations.get(validated.provider_id, existing)
            installation = _staged_installation(
                validated,
                current,
                bundle_path,
                staged_python_path,
                source_trusted,
                origin,
                source_id,
            )
            installations[validated.provider_id] = installation
            return dict(installation)

        installation = mutate_state(record_staged_install)
        if not is_update:
            # First install: opt the provider into Bazarr's enabled_providers so
            # the new plugin is search-eligible without a separate UI toggle.
            _set_bazarr_provider_enabled(validated.provider_id, True)
        if is_update:
            message = (
                f"Staged update {existing_version} -> {validated.version} "
                f"(restart Bazarr+ to activate)"
            )
        else:
            message = f"Staged install of v{validated.version} (restart Bazarr+ to activate)"
        job.update(message=message)
        return _redact_installation(installation)


def stage_install(manifest: dict[str, Any], install_timeout: float | None = None) -> dict[str, Any]:
    state = load_state()
    source_trusted = _catalog_manifest_trusted(manifest, state) if isinstance(manifest, dict) else False
    origin, source_id = _install_origin(manifest, state) if isinstance(manifest, dict) else ("local", None)
    validated = validate_manifest(
        manifest,
        built_in_provider_ids=_validation_built_in_provider_ids(
            manifest,
            source_trusted,
        ),
    )
    manifest_source = validated.raw.get("source") if isinstance(validated.raw, dict) else None
    catalog_url = manifest_source.get("catalog_url") if isinstance(manifest_source, dict) else None
    return _stage_validated(
        validated,
        source_trusted,
        origin,
        source_id,
        _fetch_bundle,
        catalog_url=catalog_url,
        install_timeout=install_timeout,
    )


def stage_install_local(archive_bytes: bytes) -> dict[str, Any]:
    """Install a Provider Hub provider from an uploaded .zip package.

    A local package has no catalog vouching for it, so it is always recorded as
    origin="local" and forced untrusted: it can never shadow a built-in provider.
    Its files are still hash-verified against the manifest and smoke-tested before
    activation, exactly like a catalog install.
    """
    if not isinstance(archive_bytes, (bytes, bytearray)) or not archive_bytes:
        raise ProviderHubInstallError("uploaded package is empty")

    # A local upload can be the very first Provider Hub write, before any catalog
    # refresh/job has created the directory, so make sure it exists.
    hub_dir = provider_hub_dir()
    hub_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="phub-local-", dir=str(hub_dir)))
    try:
        _safe_extract_zip(bytes(archive_bytes), work_dir)
        manifest, bundle_root = _find_local_manifest(work_dir)
        # Untrusted (full built-in set, no replacements): a local package can
        # never shadow a built-in provider.
        validated = validate_manifest(
            manifest,
            built_in_provider_ids=_built_in_provider_ids(),
        )
        return _stage_validated(
            validated,
            source_trusted=False,
            origin="local",
            source_id=None,
            bundle_stager=lambda candidate, deadline=None: _stage_local_bundle(candidate, bundle_root),
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def activate_staged_installations() -> list[str]:
    state = load_state()
    installations = state.get("installations") or {}
    activated = []
    changed = False
    for provider_id, installation in list(installations.items()):
        if not isinstance(installation, dict):
            continue
        if installation.get("pending_restart") and installation.get("state") == "removed":
            target_name = installation.get("name") or provider_id
            with record_job(
                "uninstall",
                target_kind="provider",
                target_id=provider_id,
                target_name=target_name,
                from_version=installation.get("active_version"),
            ) as job:
                del installations[provider_id]
                changed = True
                job.update(message=f"Removed plugin '{target_name}' on restart")
            continue
        if not installation.get("pending_restart") or installation.get("state") != "staged":
            continue

        previous_version = installation.get("active_version")
        target_version = installation.get("staged_version")
        target_name = installation.get("name") or provider_id

        with record_job(
            "activate",
            target_kind="provider",
            target_id=provider_id,
            target_name=target_name,
            from_version=previous_version,
            to_version=target_version,
        ) as job:
            try:
                trusted = bool(installation.get("trusted", False))
                manifest_data = installation.get("manifest") or {}
                manifest = validate_manifest(
                    manifest_data,
                    built_in_provider_ids=_validation_built_in_provider_ids(
                        manifest_data,
                        trusted,
                    ),
                )
                if isinstance(installation.get("staged_manifest"), dict):
                    staged_manifest = installation.get("staged_manifest")
                    manifest = validate_manifest(
                        staged_manifest,
                        built_in_provider_ids=_validation_built_in_provider_ids(
                            staged_manifest,
                            trusted,
                        ),
                    )
                staged_path = installation.get("staged_path")
                staged_python_path = installation.get("staged_python_path")
                if not staged_path or not staged_python_path:
                    raise ProviderHubInstallError("staged bundle or python path is missing")
                staged_bundle_path = Path(staged_path)
                _remove_bundle_runtime_artifacts(staged_bundle_path)
                verify_bundle_tree(manifest, staged_bundle_path)
                _smoke_validate_worker(manifest, staged_bundle_path, Path(staged_python_path))
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
                job.data["state"] = "failed"
                job.update(
                    error=str(error) or error.__class__.__name__,
                    message=f"Activation failed: {error}",
                )
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
            if previous_version:
                msg = f"Activated update {previous_version} -> {target_version}"
            else:
                msg = f"Activated plugin '{target_name}' v{target_version}"
            job.update(message=msg)
    if changed:
        updated_installations = state.get("installations") or {}

        def persist_activated(state: dict[str, Any]) -> None:
            state["installations"] = updated_installations

        mutate_state(persist_activated)
    return activated


def remove_installation(provider_id: str) -> bool:
    state = load_state()
    installations = state.setdefault("installations", {})
    if provider_id not in installations:
        return False
    item = installations[provider_id]
    target_name = (
        item.get("name") if isinstance(item, dict) else None
    ) or provider_id
    active_version = item.get("active_version") if isinstance(item, dict) else None

    with record_job(
        "uninstall",
        target_kind="provider",
        target_id=provider_id,
        target_name=target_name,
        from_version=active_version,
    ) as job:
        def remove_or_stage(state: dict[str, Any]) -> str:
            installations = state.setdefault("installations", {})
            item = installations.get(provider_id)
            if not isinstance(item, dict):
                return "missing"
            if not item.get("active_version"):
                del installations[provider_id]
                return "removed_pending"
            item["state"] = "removed"
            item["pending_restart"] = True
            item["staged_version"] = None
            item["staged_path"] = None
            item["staged_python_path"] = None
            item["staged_manifest"] = None
            item["last_error"] = None
            return "staged"

        result = mutate_state(remove_or_stage)
        if result == "missing":
            job.update(message=f"Plugin '{target_name}' not found")
            return False
        _set_bazarr_provider_enabled(provider_id, False)
        if result == "removed_pending":
            job.update(message=f"Removed pending install of '{target_name}'")
            return True
        job.update(message=f"Staged removal of '{target_name}' (restart Bazarr+ to finalize)")
        return True


def check_updates() -> dict[str, Any]:
    job_id: str | None = None
    with record_job("check_updates", target_kind="system") as job:
        job_id = job.data["id"]
        state = load_state()
        available = 0
        provider_summaries: list[str] = []
        for provider_id, installation in (state.get("installations") or {}).items():
            if not isinstance(installation, dict):
                continue
            if installation.get("origin") == "local":
                # Local packages are owned by their uploaded file, not the catalog.
                continue
            active_version = installation.get("active_version")
            if not active_version:
                continue
            latest = _latest_catalog_manifest(state, provider_id, active_version)
            if not isinstance(latest, dict):
                continue
            latest_version = latest.get("version")
            if not latest_version:
                continue
            available += 1
            display_name = installation.get("name") or provider_id
            provider_summaries.append(
                f"{display_name}: {active_version} -> {latest_version}"
            )
        if available:
            message = (
                f"{available} update(s) available: {', '.join(provider_summaries)}"
            )
        else:
            message = "Catalog metadata checked. No new updates available."
        job.update(
            message=message,
            details={"updates_available": available, "providers": provider_summaries},
        )
    persisted = get_job(job_id) if job_id else None
    return persisted or {}


def _version_key(version: Any) -> tuple[Any, ...] | None:
    raw_version = str(version or "").strip()
    if not raw_version:
        return None
    comparable_version = raw_version.split("+", 1)[0]
    match = _SEMVER_RE.match(comparable_version)
    if match:
        core_parts = [int(part) for part in match.group("core").split(".")]
        while len(core_parts) < 3:
            core_parts.append(0)
        prerelease = match.group("prerelease")
        if prerelease is None:
            prerelease_key = (1,)
        else:
            prerelease_tokens: list[tuple[int, int | str]] = []
            for token in prerelease.split("."):
                if token.isdigit():
                    prerelease_tokens.append((0, int(token)))
                else:
                    prerelease_tokens.append((1, token.lower()))
            prerelease_key = (0, tuple(prerelease_tokens))
        return (1, tuple(core_parts), prerelease_key)

    tokens: list[tuple[int, int | str]] = []
    for token in _VERSION_TOKEN_RE.findall(comparable_version):
        if token.isdigit():
            tokens.append((0, int(token)))
        else:
            tokens.append((1, token.lower()))
    return (0, tuple(tokens))


def _latest_catalog_manifest(state: dict[str, Any], provider_id: str, active_version: Any) -> dict[str, Any] | None:
    active_key = _version_key(active_version)
    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for entry in (state.get("catalog_entries") or {}).values():
        if not isinstance(entry, dict) or entry.get("provider_id") != provider_id:
            continue
        manifest = entry.get("manifest")
        if not isinstance(manifest, dict):
            continue
        version_key = _version_key(manifest.get("version") or entry.get("version"))
        if version_key is None:
            continue
        if active_key is not None and version_key <= active_key:
            continue
        candidates.append((version_key, manifest))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def apply_update(provider_id: str) -> dict[str, Any] | None:
    provider = get_provider(provider_id, redact=False)
    if not provider:
        return None
    if provider.get("origin") == "local":
        # A local package is never replaced by a catalog update; ignore the request.
        return _redact_installation(provider)
    state = load_state()
    manifest = provider.get("available_manifest") or _latest_catalog_manifest(
        state,
        provider_id,
        provider.get("active_version") or provider.get("staged_version"),
    )
    if not isinstance(manifest, dict):
        target_name = provider.get("name") or provider_id
        with record_job(
            "stage_update",
            target_kind="provider",
            target_id=provider_id,
            target_name=target_name,
            from_version=provider.get("active_version") or provider.get("staged_version"),
        ) as job:
            provider["last_error"] = "No update manifest is available"

            def record_missing_manifest(state: dict[str, Any]) -> None:
                state.setdefault("installations", {})[provider_id] = provider

            mutate_state(record_missing_manifest)
            job.data["state"] = "failed"
            job.update(
                error="No update manifest is available",
                message="No update manifest is available",
            )
        return _redact_installation(provider)
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

# coding=utf-8
"""Private settings copies attached to an exact retained release."""
from __future__ import annotations

import json
from typing import Any

RETAINED = "retained_release_settings"
PENDING_HOOK = "pending_rollback_hook"


def provider_restore_pending(row: dict) -> bool:
    marker = row.get(PENDING_HOOK)
    return isinstance(marker, dict) and bool(marker.get("provider_settings_copy"))


def _matches(record: Any, release) -> bool:
    return bool(isinstance(record, dict) and release is not None and record.get("version") == 1
                and (record.get("release_id"), record.get("created_at"), record.get("digest"))
                == (release.id, release.created_at, release.bundle_digest)
                and isinstance(record.get("content"), str))


def record_for(installation: dict, release) -> dict | None:
    record = installation.get(RETAINED)
    return record if _matches(record, release) else None


def _declared(manifest: dict) -> set[str]:
    from secret_store.registry import _manifest_field_names

    return {field.lower() for field in _manifest_field_names(manifest)}


def _private_fields(state, plugin_id, installation) -> set[str]:
    from secret_store.registry import installation_secret_fields

    return installation_secret_fields(plugin_id, installation, state.get("plugin_secret_paths"))


def capture(state: dict, plugin_id: str, installation: dict, release, translator_owners: dict,
            *, section: dict | None = None) -> dict | None:
    """Bind public plugin settings and scoped runtime owners to the exact outgoing release."""
    if release is None:
        return None
    manifest = installation.get("manifest")
    if not isinstance(manifest, dict):
        return None
    private = _private_fields(state, plugin_id, installation)
    allowed = _declared(manifest) - private
    future = _declared(installation.get("staged_manifest")) - private
    if not allowed and not future and not translator_owners:
        return None
    config = installation.get("config")
    config = config if isinstance(config, dict) else {}
    payload = {"config_fields": sorted(allowed), "config": {key: config[key] for key in allowed if key in config},
               "owners": translator_owners}
    if section is not None:
        payload["section"] = {key: section[key] for key in allowed if key in section}
    content = seal(payload)
    return {"version": 1, "release_id": release.id, "created_at": release.created_at,
            "digest": release.bundle_digest, "content": content}


def seal(payload: dict) -> str:
    from secret_store.crypto import encrypt_secret, persist_master_key

    persist_master_key()
    return encrypt_secret(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def read(record: dict | None) -> dict:
    return unseal(record["content"]) if record else {}


def unseal(content: str) -> dict:
    from secret_store.crypto import decrypt_secret, is_encrypted

    if not is_encrypted(content):
        raise ValueError("The retained settings copy is invalid")
    try:
        result = json.loads(decrypt_secret(content))
    except (TypeError, ValueError):
        raise ValueError("The retained settings copy could not be read") from None
    if not isinstance(result, dict):
        raise ValueError("The retained settings copy is invalid")
    return result


def restore_config(state: dict, plugin_id: str, row: dict, snapshot: dict) -> None:
    """Restore declared public settings into this plugin's own configuration only."""
    if not snapshot:
        return
    private = _private_fields(state, plugin_id, row)
    previous = (_declared(row.get("staged_manifest")) & set(snapshot["config_fields"])) - private
    current = _declared(row.get("manifest")) - private
    owned = current | previous
    existing = row.get("config")
    existing = existing if isinstance(existing, dict) else {}
    updated = {key: value for key, value in existing.items() if key not in owned}
    updated.update((key, value) for key, value in snapshot["config"].items() if key in previous)
    row["config"] = updated


def provider_section(plugin_id: str) -> dict:
    from app.config import settings
    from .manifest import reserved_settings_sections

    if plugin_id in reserved_settings_sections():
        raise ValueError("The plugin does not own this settings section")
    section = settings.get(plugin_id, {})
    return dict(section) if hasattr(section, "items") else {}


def _provider_credentials_present(plugin_id, names, section, hub_config) -> bool:
    from app.config import _credential_present
    from secret_store.migration import decrypt_failure_paths

    failed = decrypt_failure_paths()
    return any(f"{plugin_id}.{key}" in failed or _credential_present(section.get(key))
               or _credential_present(hub_config.get(key)) for key in names)


def provider_restore_plan(state, plugin_id, row, target_manifest, snapshot, *, section=None):
    """Project the two stores without copying or decrypting a retained credential."""
    from app.config import _plugin_destination_reach, _plugin_destination_fields

    current = row.get("manifest") or {}
    restoring = {**row, "staged_manifest": target_manifest}
    private = _private_fields(state, plugin_id, restoring)
    owned = (_declared(current) | _declared(target_manifest)) - private
    section = provider_section(plugin_id) if section is None else section
    prior = snapshot.get("section", {})
    desired = {key: prior[key] for key in owned if key in prior}
    before = {key: section[key] for key in owned if key in section}
    projected_section = {key: value for key, value in section.items() if key not in owned} | desired
    current_hub = row.get("config") if isinstance(row.get("config"), dict) else {}
    proposed = dict(restoring)
    restore_config(state, plugin_id, proposed, snapshot)
    next_hub = proposed.get("config") or {}

    current_uris, current_secrets, current_defaults = _plugin_destination_fields(current)
    target_uris, target_secrets, target_defaults = _plugin_destination_fields(target_manifest)
    destinations = current_uris | target_uris
    destinations |= {key for key in owned if key in ("url", "host", "endpoint") or key.endswith(("_url", "_host"))}
    changed = {}
    for key in sorted(destinations):
        try:
            before_reach = _plugin_destination_reach(section.get(key), key in current_hub, current_hub.get(key),
                                                     current_defaults.get(key, ()))
            after_reach = _plugin_destination_reach(projected_section.get(key), key in next_hub, next_hub.get(key),
                                                    target_defaults.get(key, ()))
        except TypeError:
            raise ValueError("The previous provider address could not be verified") from None
        if before_reach != after_reach:
            changed[key] = sorted(str(value) for value in after_reach)
    secret_fields = private | current_secrets | target_secrets
    if changed and _provider_credentials_present(plugin_id, secret_fields, section, current_hub):
        raise ValueError("Remove the saved provider credential before rolling back to a different address")
    return {"fields": sorted(owned), "before": before, "after": desired,
            "changed": changed, "secret_fields": sorted(secret_fields)}


def restore_provider_section(plugin_id: str, plan: dict, hub_config: dict) -> None:
    from app.config import settings, write_config
    from discover.metadata import CONFIG_LOCK

    with CONFIG_LOCK:
        current = provider_section(plugin_id)
        if plan["changed"] and _provider_credentials_present(plugin_id, plan["secret_fields"], current, hub_config):
            raise ValueError("Remove the saved provider credential before restoring its previous address")
        owned = set(plan["fields"])
        existing = {key: current[key] for key in owned if key in current}
        if existing == plan["after"]:
            return
        if existing != plan["before"]:
            raise ValueError("The provider settings section changed before it could be restored")
        settings[plugin_id] = {key: value for key, value in current.items() if key not in owned} | plan["after"]
        try:
            if write_config() is not True:
                raise ValueError("The previous provider settings could not be saved")
        except BaseException:
            settings[plugin_id] = current
            raise

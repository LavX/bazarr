# coding=utf-8
"""Private settings copies attached to an exact retained release."""
from __future__ import annotations

import json
from typing import Any

RETAINED = "retained_release_settings"


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


def capture(state: dict, plugin_id: str, installation: dict, release, translator_owners: dict) -> dict | None:
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

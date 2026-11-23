# coding=utf-8
"""First-start selection of the Google Translate engine shipped with the image."""
from __future__ import annotations

from . import runtime_v2_enabled

_GOOGLE_SEED_SELECTED = "google_seed_selected"


def _verified_google_seed_release(*, staged: bool):
    """Identify an exact signed image release without choosing or staging one."""
    from provider_hub import seed
    from provider_hub.kinds import TRANSLATOR, get_kind, kind_of
    from provider_hub.manifest import manifest_provider_id
    from provider_hub.state import OFFICIAL_CATALOG_SOURCE_ID, load_state_strict, provider_hub_dir

    state = load_state_strict()
    official = (state.get("catalog_sources") or {}).get(OFFICIAL_CATALOG_SOURCE_ID) or {}
    row = (state.get("installations") or {}).get(seed.GOOGLE_PLUGIN_ID)
    if (official.get("dev_ref") or official.get("enabled") is False
            or not isinstance(row, dict) or row.get("state") != ("staged" if staged else "active")
            or kind_of(row) != TRANSLATOR or not get_kind(TRANSLATOR).is_enabled(row)
            or row.get("source_id") != OFFICIAL_CATALOG_SOURCE_ID or not row.get("trusted")
            or (row.get("origin") or "catalog") != "catalog"):
        return None
    version = row.get("staged_version" if staged else "active_version")
    bundle = row.get("staged_path" if staged else "active_path")
    python = row.get("staged_python_path" if staged else "python_path")
    manifest = row.get("staged_manifest" if staged else "manifest")
    if (not all(isinstance(value, str) and value for value in (version, bundle, python))
            or not isinstance(manifest, dict) or manifest_provider_id(manifest) != seed.GOOGLE_PLUGIN_ID
            or manifest.get("version") != version or not seed.is_runtime_path(bundle)
            or not seed.is_runtime_path(python)
            or seed.resolve_paths(provider_hub_dir(), manifest, bundle, python) is None):
        return None
    return version, bundle, python, manifest


def pending_google_seed_release():
    """Remember only a verified image release waiting for the boot activation pass."""
    return _verified_google_seed_release(staged=True)


def start_google_from_seed(*, activated_release=None) -> bool:
    """Initialize the legacy Google default, or finish selection after exact activation.

    Supplying the previously staged identity only finishes selection. It never
    ranks releases or stages an update over what the boot pass just activated.
    """
    if not runtime_v2_enabled():
        return False

    from app.config import settings
    from app.database import database
    from provider_hub.seed import GOOGLE_PLUGIN_ID
    from provider_hub.service import activate_google_seed
    from provider_hub.state import load_state_strict, mutate_state

    from .store import TranslatorStore

    store = TranslatorStore(database)
    selected_id, _ = store.pointers()
    if selected_id is not None:
        selected = store.get_instance(selected_id)
        if selected is None or selected.plugin_id != GOOGLE_PLUGIN_ID or not selected.enabled:
            return False
    else:
        if load_state_strict().get(_GOOGLE_SEED_SELECTED):
            return False
        if getattr(settings.translator, "translator_type", None) != GOOGLE_PLUGIN_ID:
            return False
        google_instances = store.list_instances(GOOGLE_PLUGIN_ID)
        if google_instances and not any(instance.enabled for instance in google_instances):
            return False

    if activated_release is not None:
        if _verified_google_seed_release(staged=False) != activated_release:
            return False
    elif not activate_google_seed():
        return False
    if selected_id is None:
        for instance in store.list_instances(GOOGLE_PLUGIN_ID):
            if store.instance_configured(instance):
                store.set_engine(instance.id)
                break
        else:
            return False
    if not load_state_strict().get(_GOOGLE_SEED_SELECTED):
        mutate_state(lambda state: state.update({_GOOGLE_SEED_SELECTED: True}))
    return True

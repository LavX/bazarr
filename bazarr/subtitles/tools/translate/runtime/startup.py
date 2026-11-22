# coding=utf-8
"""First-start selection of the Google Translate engine shipped with the image."""
from __future__ import annotations

from . import runtime_v2_enabled

_GOOGLE_SEED_SELECTED = "google_seed_selected"


def start_google_from_seed() -> bool:
    """Honor an existing engine choice, or initialize the legacy Google default once."""
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

    if not activate_google_seed():
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

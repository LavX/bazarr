# coding=utf-8
"""The translator kind: plugins that translate subtitle lines.

The kind owns what installing, switching on or off and removing a translator
plugin means for the rest of Bazarr+: an engine instance appears with the plugin,
and the engine pointers let go of it when it goes. The tables, the secrets and
the pointer rules live in ``subtitles.tools.translate.runtime.store``.

This kind is dark. It is registered only when the ``translator.runtime_v2``
switch is on (``register_when_enabled``, called once from init.py), so with the
switch off a translator manifest is as unsupported as it is today. The
hooks need the runtime tables; with them missing they report failure rather than
guess.
"""
from __future__ import annotations

import logging

from typing import Any

from . import ATTACH_ON_RESTART, TRANSLATOR, Kind, register_kind

logger = logging.getLogger(__name__)

TRANSLATOR_KIND_API = "bazarr.translator.v1"


def _store():
    from app.database import database
    from subtitles.tools.translate.runtime.store import TranslatorStore

    return TranslatorStore(database)


class TranslatorKind(Kind):
    name = TRANSLATOR
    kind_apis = frozenset({TRANSLATOR_KIND_API})
    attach_policy = ATTACH_ON_RESTART
    noun = "translator"

    def on_installed(self, plugin_id: str) -> bool:
        try:
            _store().ensure_instance(plugin_id)
        except Exception:
            logger.exception("Could not create the engine instance for translator %s", plugin_id)
            return False
        return True

    def on_removed(self, plugin_id: str) -> bool:
        try:
            for notice in _store().remove_plugin(plugin_id).notices:
                logger.info("%s", notice.message)
        except Exception:
            logger.exception("Could not remove the engine instances of translator %s", plugin_id)
            return False
        return True

    def set_enabled(self, installation: dict[str, Any], enabled: bool) -> bool:
        installation["enabled"] = bool(enabled)
        if enabled:
            return True
        try:
            for notice in _store().plugin_disabled(str(installation.get("provider_id") or "")).notices:
                logger.info("%s", notice.message)
        except Exception:
            logger.exception("Could not release the engine pointers of translator %s",
                             installation.get("provider_id"))
            return False
        return True

    def validate_manifest(self, manifest: dict[str, Any], trusted: bool):
        from ..manifest import ManifestValidationError, validate_plugin_bundle

        validated = validate_plugin_bundle(manifest)
        if validated.kind != TRANSLATOR or validated.api_version != TRANSLATOR_KIND_API:
            raise ManifestValidationError(f"this is not a {TRANSLATOR_KIND_API} manifest")
        return validated


def register_translator_kind() -> Kind:
    """Register the kind. Idempotent."""
    from . import get_kind

    try:
        return get_kind(TRANSLATOR)
    except Exception:
        return register_kind(TranslatorKind())


def register_when_enabled() -> bool:
    """Register the kind when the runtime switch is on. True when the kind is registered.

    Reads the setting itself, on purpose: ``runtime_v2_flag`` lives in the ``subtitles`` package, and
    importing that package this early in startup would load a great deal that a switched-off boot has
    never loaded at this point.
    """
    try:
        from app.config import settings

        if getattr(settings.translator, "runtime_v2", False) is not True:
            return False
    except Exception:
        return False
    register_translator_kind()
    return True

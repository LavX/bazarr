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

from typing import Any, Sequence

from . import ATTACH_ON_RESTART, DETACH_LIVE, TRANSLATOR, Kind, register_kind

logger = logging.getLogger(__name__)

TRANSLATOR_KIND_API = "bazarr.translator.v1"


def _store():
    from app.database import database
    from subtitles.tools.translate.runtime.store import TranslatorStore

    return TranslatorStore(database)


def _retire_worker(plugin_id: str) -> None:
    """Let the plugin's worker go once the jobs it holds are done, so the next one starts from what is installed
    and switched on then. Nothing happens while the runtime has bound no worker, and a failure here never undoes the
    change that asked for it."""
    try:
        from subtitles.tools.translate.runtime import worker_binding

        worker_binding.plugin_changed(plugin_id)
    except Exception:
        logger.warning("Could not retire the worker of translator %s", plugin_id, exc_info=True)


class TranslatorKind(Kind):
    name = TRANSLATOR
    kind_apis = frozenset({TRANSLATOR_KIND_API})
    attach_policy = ATTACH_ON_RESTART
    # A removed or revoked translator stops taking jobs now and its worker goes once its jobs are done.
    detach_policy = DETACH_LIVE
    noun = "translator"
    keeps_data = True

    def detach(self, plugin_id: str, *, withdraw: bool, grace: float | None = None,
               release_key: str | None = None):
        from subtitles.tools.translate.runtime import worker_binding

        if not withdraw:
            # Switched off: the worker is retired as the switch always did, and the gate refuses new jobs.
            worker_binding.plugin_changed(plugin_id)
            return []
        return worker_binding.plugin_detached(plugin_id, grace)

    def kept_data_reclaimed(self, plugin_id: str) -> None:
        try:
            from subtitles.tools.translate.runtime import worker_binding

            worker_binding.plugin_installed(plugin_id)
        except Exception:
            logger.warning("Could not clear the kept mark of translator %s", plugin_id, exc_info=True)

    def discard_kept_data(self, plugin_id: str) -> None:
        """Delete what an earlier removal kept, for a plugin that is about to be installed under the same id by
        another owner. Unlike cleanup after a removal this does not swallow failures: if anything of the old data
        survives, the install is refused, so the new package never finds the old one's keys or folders."""
        _store().delete_plugin_data(plugin_id)
        from subtitles.tools.translate.runtime import worker_binding

        worker_binding.discard_folders(plugin_id)

    def on_installed(self, plugin_id: str, missing_required: Sequence[str] = ()) -> bool:
        try:
            _store().ensure_instance(plugin_id)
        except Exception:
            logger.exception("Could not create the engine instance for translator %s", plugin_id)
            return False
        try:
            from subtitles.tools.translate.runtime import worker_binding

            # Data kept when the plugin was removed is in use again, so the mark that kept it goes.
            worker_binding.plugin_installed(plugin_id)
        except Exception:
            logger.warning("Could not clear the kept mark of translator %s", plugin_id, exc_info=True)
        return True

    def on_removed(self, plugin_id: str) -> bool:
        """Let the engine pointers go of the plugin's instances. The instances themselves stay until the
        removal is saved (``after_removed``), so a removal that fails part way loses no configuration."""
        try:
            for notice in _store().release_plugin(plugin_id).notices:
                logger.info("%s", notice.message)
        except Exception:
            logger.exception("Could not release the engine pointers of translator %s", plugin_id)
            return False
        _retire_worker(plugin_id)
        return True

    def removal_finished(self, plugin_id: str) -> None:
        # The folders of a plugin that is not installed any more go now that nothing of it runs, as the sweep after
        # a restart would clear them.
        try:
            from subtitles.tools.translate.runtime import worker_binding

            worker_binding.sweep_removed_plugins()
        except Exception:
            logger.warning("Could not clear the folders of translator %s", plugin_id, exc_info=True)

    def after_removed(self, plugin_id: str, keep_data: bool = False) -> None:
        if keep_data:
            # The engine instances, their connections and secrets stay, and so do the plugin's folders, marked so
            # that no sweep takes them for leftovers. Installing the plugin again picks them up.
            try:
                from subtitles.tools.translate.runtime import worker_binding

                worker_binding.plugin_kept(plugin_id)
            except Exception:
                logger.warning("Could not mark the folders of translator %s as kept", plugin_id, exc_info=True)
            return
        try:
            _store().delete_plugin_data(plugin_id)
        except Exception:
            logger.exception("Could not remove the engine instances of translator %s", plugin_id)
        try:
            from subtitles.tools.translate.runtime import worker_binding

            worker_binding.plugin_removed(plugin_id)
        except Exception:
            logger.warning("Could not clear what translator %s left in its folders", plugin_id, exc_info=True)

    def on_activated(self, plugin_id: str, previous_manifest, manifest) -> str | None:
        """Bring the plugin's saved engines and back ends in line with the release that just became active."""
        from subtitles.tools.translate.runtime.plugin_update import bring_up_to_date

        message = bring_up_to_date(_store(), plugin_id, manifest)
        # A worker that is running was started from the release this one replaces.
        _retire_worker(plugin_id)
        return message

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
        _retire_worker(str(installation.get("provider_id") or ""))
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

# coding=utf-8
"""The subtitle provider kind: catalog providers that join Bazarr's search pool.

Everything here is specific to providers and used to live in the shared core:
the ``enabled_providers`` list, registration as a subliminal provider (see
``provider_hub.registry``), the provider pool and credential-throttle resets
when a provider's configuration changes, and the settings-section overlay the
pool reads a plugin's credentials from.
"""
from __future__ import annotations

import logging

from typing import Any

from . import ATTACH_ON_RESTART, SUBTITLE_PROVIDER, Kind, kind_of, register_kind

logger = logging.getLogger(__name__)

# The contract providers implement. Schema-1 manifests name it by the older
# Provider Hub API string, which means the same contract.
SUBTITLE_PROVIDER_KIND_API = "bazarr.subtitle_provider.v1"
LEGACY_PROVIDER_HUB_API = "bazarr.provider-hub.v1"


def bazarr_enabled_providers() -> list[str]:
    """Return Bazarr's current enabled-providers list, or [] if unavailable.

    ``settings.general.enabled_providers`` is the actual gate that decides
    which providers run during a search. The hub installation row's stored
    ``enabled`` flag is a local copy that historically drifted out of sync
    with this list. Reading the canonical value lazily avoids a circular
    import (the hub is imported during init before settings finishes
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


def set_bazarr_provider_enabled(provider_id: str, enabled: bool) -> bool:
    """Add ``provider_id`` to (or remove it from) Bazarr's enabled_providers.

    Returns True when the list holds the requested state on disk, whether
    this call changed it or it already did. Returns False when the change
    could not be saved, with the live list put back as it was: a change that
    reached no file would make the provider look enabled, or removed, until
    the next restart quietly undid it. Callers report that as a failure.

    The read, the edit and the write are one critical section. Installing
    several providers at once reaches this from several threads: the venv
    lock in venv.py serializes only the pip work and is released before the
    caller gets here, and write_config takes CONFIG_LOCK around the file
    write alone, not around the read. Two installs finishing in the same
    window would each read the list before the other wrote it, and the
    second write would drop the first provider: a successful install that
    vanishes. CONFIG_LOCK is reentrant, so the write_config below still
    takes it on the same thread.
    """
    try:
        from app.config import settings, write_config
        from discover.metadata import CONFIG_LOCK
    except Exception:
        logger.exception("Failed to sync enabled_providers for %s", provider_id)
        return False
    with CONFIG_LOCK:
        current = list(bazarr_enabled_providers())
        if enabled and provider_id not in current:
            current.append(provider_id)
        elif not enabled and provider_id in current:
            current = [item for item in current if item != provider_id]
        elif not enabled or provider_id not in getattr(settings.general, "disabled_providers", []):
            return True
        previous = settings.general.enabled_providers
        previous_disabled = getattr(settings.general, "disabled_providers", [])
        try:
            settings.general.enabled_providers = current
            if enabled:
                settings.general.disabled_providers = [item for item in previous_disabled if item != provider_id]
            saved = write_config()
        except Exception:
            logger.exception("Failed to sync enabled_providers for %s", provider_id)
            saved = False
        if saved is not True:
            settings.general.enabled_providers = previous
            if enabled:
                settings.general.disabled_providers = previous_disabled
            return False
        return True


def forget_credential_throttle(provider_id: str) -> None:
    """Hand a re-configured provider its next attempt back.

    An AuthenticationError parks a provider in throttled_providers.dat for
    twelve hours, and the only thing that fixes it is the user correcting what
    they typed. The settings page already clears those entries on save, but a
    Provider Hub plugin is configured through the hub instead, so the
    corrected provider stayed skipped by every search, and reported as cooling
    down on Discover, until the backoff ran out on its own.

    The compat pool holds provider instances built with the old credentials, so
    it is dropped here too, exactly as the settings path does. Both steps are
    best-effort: saving a plugin's configuration must not fail because the
    throttle table or the pool is unavailable.
    """
    try:
        from app.get_providers import reset_throttled_provider
        reset_throttled_provider(provider_id)
    except Exception:
        logger.exception("Failed to clear the throttle for %s after a configuration change", provider_id)
    try:
        from compat.service import reset_compat_pool
        reset_compat_pool()
    except Exception:
        logger.exception("Failed to reset the compat pool after configuring %s", provider_id)


def settings_section_plugin_ids() -> set[str]:
    """Ids of installed provider plugins, whose config.yaml section the pool reads.

    ``save_settings`` resets the compat pool when one of these sections is
    saved. This is the body of ``config._active_provider_hub_provider_ids`` as
    it is on ``development`` (rows with an active version that are not
    removed, else the active installations), reached through ``sys.modules``
    first for the same reason as before the move: config.py runs before the hub
    is importable on some paths, and ``test_config_save.py`` replaces the
    module there. Only subtitle providers count.
    """
    import sys

    try:
        state_module = sys.modules.get("provider_hub.state")
        if state_module is None:
            from provider_hub import state as state_module
        load_state = getattr(state_module, "load_state", None)
        if callable(load_state):
            state = load_state()
            return {
                str(item.get("provider_id") or provider_id)
                for provider_id, item in (state.get("installations") or {}).items()
                if isinstance(item, dict)
                and kind_of(item) == SUBTITLE_PROVIDER
                and item.get("active_version")
                and item.get("state") != "removed"
            }
        return {
            str(provider_id)
            for provider_id in (
                getattr(installation, "provider_id", None)
                for installation in state_module.active_installations()
            )
            if provider_id
        }
    except Exception:
        logger.exception("Unable to inspect active Provider Hub providers")
        return set()


def apply_settings_overlay(provider_configs: dict[str, dict[str, Any]], settings, reported: set[str],
                           blocked: set[str]) -> None:
    """Overlay each running provider plugin's config with its settings section.

    ``reported`` is the caller's set of plugin ids already logged for claiming
    a Bazarr settings section, so the same install writes that line once per
    process rather than on every search. ``blocked`` is the caller's set of
    providers whose stored credential will not decrypt: they get no overlay,
    so the pool cannot rebuild a config for them from the settings section.
    The two hub modules are read through their module attributes at call time,
    because the tests of the pool overlay patch them there.
    """
    from provider_hub import service as hub_service
    from provider_hub import state as hub_state
    from provider_hub.manifest import reserved_settings_sections

    try:
        provider_configs.update(hub_service.runtime_provider_configs())
    except Exception:
        logger.exception("Unable to load Provider Hub provider config")
    try:
        reserved_sections = reserved_settings_sections()
        # The same active-version population the runtime configs admit, with
        # the active manifest: the class stays keyed off active_version until
        # a restart, so a pending update must not strip the section overlay.
        for installation in hub_state.running_installations():
            if installation.provider_id in blocked:
                continue
            manifest = installation.manifest
            schema = manifest.get("config_schema") if isinstance(manifest, dict) else {}
            properties = schema.get("properties") if isinstance(schema, dict) else {}
            if not isinstance(properties, dict):
                continue
            if installation.provider_id in reserved_sections:
                # The section named after this plugin is one of Bazarr's own, not
                # the plugin's. validate_manifest refuses such an id at install and
                # at every boot, so getting here means a manifest slipped past it,
                # and copying the section's keys into the worker config would hand
                # the plugin that section's credentials (sonarr.apikey, plex.token,
                # auth.password) on every search. Its own stored config and the
                # schema defaults still apply.
                if installation.provider_id not in reported:
                    reported.add(installation.provider_id)
                    logger.error("Refusing to read the %s settings section for Provider Hub plugin %s: "
                                 "that section belongs to Bazarr, not to the plugin",
                                 installation.provider_id, installation.provider_id)
                section = {}
            else:
                section = settings.get(installation.provider_id, {}) if hasattr(settings, "get") else {}
            config = dict(provider_configs.get(installation.provider_id, {}))
            for key, field in properties.items():
                if not isinstance(field, dict):
                    continue
                if "default" in field and key not in config:
                    config[key] = field["default"]
                value = section.get(key) if hasattr(section, "get") else getattr(section, key, None)
                if value not in (None, "", [], {}):
                    config[key] = value
            provider_configs[installation.provider_id] = config
    except Exception:
        logger.exception("Unable to load Provider Hub settings config")


class SubtitleProviderKind(Kind):
    name = SUBTITLE_PROVIDER
    kind_apis = frozenset({SUBTITLE_PROVIDER_KIND_API})
    attach_policy = ATTACH_ON_RESTART
    noun = "provider"

    def on_installed(self, plugin_id: str) -> bool:
        # A first install opts the provider into enabled_providers, so the new
        # plugin is search-eligible without a separate toggle. False means the
        # list could not be saved, which the core reports.
        return set_bazarr_provider_enabled(plugin_id, True)

    def on_removed(self, plugin_id: str) -> bool:
        return set_bazarr_provider_enabled(plugin_id, False)

    def set_enabled(self, installation: dict[str, Any], enabled: bool) -> bool:
        installation["enabled"] = bool(enabled)
        return set_bazarr_provider_enabled(str(installation.get("provider_id") or ""), bool(enabled))

    def is_enabled(self, installation: dict[str, Any]) -> bool:
        # enabled_providers in config.yaml is the source of truth; the stored
        # field on the hub row is a stale shadow that is not trusted on read.
        return str(installation.get("provider_id") or "") in bazarr_enabled_providers()

    def on_config_saved(self, plugin_id: str) -> None:
        forget_credential_throttle(plugin_id)

    def validate_manifest(self, manifest: dict[str, Any], trusted: bool):
        # The provider validator, with the built-in shadow rule that depends
        # on whether the catalog is trusted. It returns what is staged.
        from provider_hub import service
        from provider_hub.manifest import validate_manifest

        return validate_manifest(
            manifest, built_in_provider_ids=service._validation_built_in_provider_ids(manifest, trusted))

    def smoke_test(self, validated, bundle_path, python_path) -> None:
        # Looked up at call time, so a test that patches the service function reaches it.
        from provider_hub import service

        service._smoke_validate_worker(validated, bundle_path, python_path)


SUBTITLE_PROVIDER_KIND = register_kind(SubtitleProviderKind())

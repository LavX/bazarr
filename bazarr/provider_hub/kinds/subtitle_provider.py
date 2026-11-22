# coding=utf-8
"""The subtitle provider kind: catalog providers that join Bazarr's search pool.

Everything here is specific to providers:
the ``enabled_providers`` list, registration as a subliminal provider (see
``provider_hub.registry``), the provider pool and credential-throttle resets
when a provider's configuration changes, and the settings-section overlay the
pool reads a plugin's credentials from.
"""
from __future__ import annotations

import logging

from types import MappingProxyType
from typing import Any, Sequence

from . import ATTACH_ON_RESTART, DETACH_LIVE, SUBTITLE_PROVIDER, Kind, kind_of, register_kind

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
    saved. The ids are the rows with an active version that are not removed,
    else the active installations. The state module is reached through
    ``sys.modules`` first because config.py runs before the hub is importable
    on some paths, and ``test_config_save.py`` replaces the module there. Only
    subtitle providers count.
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
    from provider_hub import registry
    from provider_hub import service as hub_service
    from provider_hub import state as hub_state
    from provider_hub.capabilities import deliver
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
            if not registry.is_plugin_class(installation.provider_id):
                # A built-in holds the id: it is built from its own settings, not from this plugin's.
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
            # Reserve any still-empty service field here. The worker gets the leased address per call.
            deliver(config, properties, installation, settings)
            provider_configs[installation.provider_id] = config
    except Exception:
        logger.exception("Unable to load Provider Hub settings config")


def own_settings_section_fields(provider_id: str) -> dict[str, Any]:
    """The fields of the plugin's own settings section in the configuration, with the value each has by default.

    Empty when the section is not the plugin's alone: a built-in provider module of that id is still there and
    reads the same section, so a plugin's removal must not clear what it needs. Also empty for an id that has no
    section at all, which is every plugin that keeps its settings only on its own installation row.
    """
    from app.config import validators
    from provider_hub.migration import MIGRATED_BUILT_IN_PROVIDER_IDS
    from provider_hub.registry import _REGISTERED_PROVIDER_HUB_IDS
    from subliminal_patch.extensions import provider_registry

    # Only the sections of the providers that moved to the catalog are a provider's. Any other id could name a
    # section of the application itself, which is nobody's to clear.
    if provider_id not in MIGRATED_BUILT_IN_PROVIDER_IDS:
        return {}
    if provider_id in set(provider_registry.names()) - _REGISTERED_PROVIDER_HUB_IDS:
        return {}
    fields: dict[str, Any] = {}
    for validator in validators:
        for name in getattr(validator, "names", ()) or ():
            section, _, field = str(name).partition(".")
            if section != provider_id or not field or "." in field:
                continue
            default = getattr(validator, "default", None)
            if default is None or isinstance(default, (str, int, float, bool, list, dict)):
                fields[str(name)] = default
    return fields


def reset_own_settings_section(provider_id: str) -> bool:
    """Put the plugin's own settings section back to its defaults, credentials included, and save it. False when
    it could not be saved, with the live values put back as they were. Nothing shared is touched."""
    fields = own_settings_section_fields(provider_id)
    if not fields:
        return True
    from app.config import settings, write_config
    from discover.metadata import CONFIG_LOCK

    with CONFIG_LOCK:
        previous = {name: settings.get(name) for name in fields}
        try:
            for name, default in fields.items():
                settings[name] = default
            saved = write_config()
        except Exception:
            logger.exception("Could not clear the settings of %s", provider_id)
            saved = False
        if saved is not True:
            for name, value in previous.items():
                settings[name] = value
            return False
        return True


class SubtitleProviderKind(Kind):
    name = SUBTITLE_PROVIDER
    kind_apis = frozenset({SUBTITLE_PROVIDER_KIND_API})
    # FlareSolverr's address arrives in the provider's own ``flaresolverr_url`` field.
    capability_delivery = MappingProxyType({"flaresolverr.v1": ("flaresolverr_url", "url")})
    attach_policy = ATTACH_ON_RESTART
    # A new release waits for the restart, but a provider that is switched off, removed or revoked is taken out
    # of every pool and its worker stopped at once.
    detach_policy = DETACH_LIVE
    noun = "provider"
    keeps_data = True

    def detach(self, plugin_id: str, *, withdraw: bool, grace: float | None = None,
               release_key: str | None = None):
        from provider_hub import registry

        if withdraw:
            # First: a pool that builds an instance from here on finds no class, and the built-in a plugin had
            # replaced is registered again in the same step.
            if registry.unregister_provider_class(plugin_id).removed:
                # The pools merge settings into what they hold, so what they hold for the id is replaced: the
                # built-in that may take the id back is built from its own settings, never from the plugin's, and
                # starts without the backoff the plugin earned. (When the plugin's class was never in the registry
                # a built-in already serves the id, and what the pools hold for it is its own.)
                registry.release_provider_id(plugin_id)
        try:
            from compat.service import reset_compat_pool

            # The persistent pool of the compat endpoint and Discover is replaced, never edited: a search that
            # already holds it finishes on it, the next one gets a pool built without the provider.
            reset_compat_pool()
        except Exception:
            logger.exception("Failed to replace the compat pool after detaching %s", plugin_id)
        if withdraw:
            # The release is gone for good: a worker still finishing its calls asks the host for nothing more.
            registry.revoke_host_calls_of(plugin_id)
        drains = registry.retire_provider_instances(plugin_id, grace)
        return drains

    def after_removed(self, plugin_id: str, keep_data: bool = False) -> None:
        # A provider keeps nothing but its settings: its own section of the configuration, and the settings on its
        # installation row, which the removal has already cleared when the data is not kept.
        if keep_data:
            return
        if not reset_own_settings_section(plugin_id):
            logger.warning("The settings of %s could not be cleared", plugin_id)

    def discard_kept_data(self, plugin_id: str) -> None:
        if not reset_own_settings_section(plugin_id):
            raise RuntimeError(f"the settings of {plugin_id} could not be cleared")

    def on_installed(self, plugin_id: str, missing_required: Sequence[str] = ()) -> bool:
        # A first install opts the provider into enabled_providers, so the new
        # plugin is search-eligible without a separate toggle. False means the
        # list could not be saved, which the core reports.
        #
        # A provider whose manifest requires settings that are still empty
        # (an API key, a login) is left out: enabled, it would search without
        # credentials, fail and get throttled. It stays installed and the user
        # adds it to the enabled list once the settings are filled in. A
        # provider the user already had enabled (a built-in this install
        # replaces) is never taken off the list.
        if missing_required:
            logger.info("Not enabling %s after install: required settings are empty (%s)",
                        plugin_id, ", ".join(missing_required))
            return True
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

    def smoke_test(self, validated, bundle_path, python_path, timeout=None, *, data_dir=None) -> None:
        # Looked up at call time, so a test that patches the service function reaches it.
        from provider_hub import service

        arguments = {}
        if timeout is not None:
            arguments["timeout"] = timeout
        if data_dir is not None:
            arguments["data_dir"] = data_dir
        service._smoke_validate_worker(validated, bundle_path, python_path, **arguments)

    def settle_data(self, plugin_id: str, timeout: float) -> bool:
        import time

        from provider_hub.worker import _client_gone, live_workers

        deadline = time.monotonic() + timeout
        for client in live_workers(plugin_id):
            # Admission has closed and requests have drained. The idle stop
            # permits the existing client to start again when admission opens.
            client.stop_if_idle(0, grace_seconds=min(1.0, max(0.0, deadline - time.monotonic())))
            if not _client_gone(client, max(0.0, deadline - time.monotonic())):
                return False
        return True


SUBTITLE_PROVIDER_KIND = register_kind(SubtitleProviderKind())

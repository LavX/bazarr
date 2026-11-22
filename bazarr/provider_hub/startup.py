# coding=utf-8
"""Provider Hub startup once the application database has migrated."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def start_after_migrations() -> bool:
    """Activate and register providers before pruning the configured provider lists."""
    from app.config import remove_settings_section, settings, write_config
    from subliminal_patch.extensions import provider_registry

    registration_ok = True
    try:
        from provider_hub.service import autoinstall_enabled_builtins
        auto_staged = autoinstall_enabled_builtins()
        if auto_staged:
            logger.info("Provider Hub auto-installed from official catalog: %s", auto_staged)
    except Exception:  # pragma: no cover - hub failures must not prevent startup
        logger.exception("Unable to auto-install Provider Hub providers on startup")
    try:
        from provider_hub.service import activate_staged_installations
        activated = activate_staged_installations()
        if activated:
            logger.info("Activated staged Provider Hub installations on startup: %s", activated)
    except Exception:  # pragma: no cover - hub failures must not prevent startup
        logger.exception("Unable to activate staged Provider Hub installations on startup")
    try:
        # Records the environments installations already use and flags the ones
        # that went missing. It runs no pip: repairing is never a boot step.
        from provider_hub.service import enforce_activation_needs, reconcile_plugin_environments
        reconcile_plugin_environments()
        # A supplier whose environment just turned out to be missing does not run, so what relies on it is held
        # back too.
        enforce_activation_needs()
    except Exception:  # pragma: no cover - hub failures must not prevent startup
        logger.exception("Unable to reconcile Provider Hub environments on startup")
    try:
        # A package uploaded for an install that never ran: its job did not
        # survive the restart, so nothing will install or remove it now.
        from provider_hub.service import discard_local_package_uploads
        discard_local_package_uploads()
    except Exception:  # pragma: no cover - hub failures must not prevent startup
        logger.exception("Unable to remove leftover Provider Hub package uploads on startup")
    try:
        from provider_hub.registry import register_active_provider_classes
        registered = register_active_provider_classes()
        if registered:
            logger.info("Registered Provider Hub plugins into provider registry: %s", registered)
    except Exception:  # pragma: no cover - hub failures must not prevent startup
        registration_ok = False
        logger.exception("Unable to register active Provider Hub providers on startup")
    existing_providers = provider_registry.names()
    enabled_providers = settings.general.enabled_providers
    disabled_providers = list(getattr(settings.general, 'disabled_providers', None) or [])
    # The providers whose installation exists but whose registration was refused. They
    # are not "gone": nothing below may drop their ids or their settings. When the set
    # cannot be read, nothing is dropped at all.
    try:
        from provider_hub.registry import registration_incomplete, settings_cleanup_plan
        registration_incomplete_ids = registration_incomplete()
    except Exception:  # pragma: no cover - hub failures must not prevent startup
        registration_ok = False
        logger.exception("Unable to read which Provider Hub providers failed to register")
    if registration_ok:
        if registration_incomplete_ids:
            logger.warning("Provider Hub providers kept in the settings, their registration was refused: %s",
                            ", ".join(sorted(registration_incomplete_ids)))
        from provider_hub.migration import RETIRED_BUILT_IN_PROVIDER_IDS
        settings.general.enabled_providers, settings.general.disabled_providers, stale_provider_sections = \
            settings_cleanup_plan(existing_providers, enabled_providers, disabled_providers,
                                  RETIRED_BUILT_IN_PROVIDER_IDS, lambda provider_id: hasattr(settings, provider_id),
                                  registration_incomplete_ids)

        # Drop the config section a retired built-in provider left behind.
        #
        # A retired id has no provider class, no validators and no secret_store
        # paths left, so its section is unreachable settings: the Settings card went
        # with the provider and nothing reads the values. It is still serialized
        # into config.yaml and into every /api/system/settings response though, and
        # a credential written there by a build older than the secret store stays in
        # clear text forever, because the encrypt-at-rest walk only visits
        # registered paths. Unsetting the section is what closes that, and it is the
        # same treatment app.config already gives series_scores / movie_scores.
        #
        # Skipped for any retired id a trusted catalog plugin has adopted: Provider
        # Hub reads that plugin's credentials out of exactly this section, and the
        # registration above is what puts the id back in existing_providers.
        for stale_provider_section in stale_provider_sections:
            remove_settings_section(stale_provider_section)
        if stale_provider_sections:
            logger.info("Removed leftover config sections of retired providers: %s",
                         ", ".join(stale_provider_sections))

        write_config()
    else:
        logger.warning("Skipping provider list cleanup because Provider Hub registration failed")


    # Initialize provider_priorities if not exists
    if not hasattr(settings.general, 'provider_priorities') or not settings.general.provider_priorities:
        settings.general.provider_priorities = {}
        # Set default priorities based on current order in enabled_providers
        for idx, provider in enumerate(settings.general.enabled_providers):
            settings.general.provider_priorities[provider] = (idx + 1) * 10
        write_config()
    return registration_ok

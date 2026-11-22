# coding=utf-8
"""Prepare the database and plugin runtimes before the server starts."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def prepare_database_and_plugins(app) -> bool:
    """Upgrade the schema before starting workers and return registration status."""
    from app.config import settings
    from app.database import fix_languages_profiles_with_duplicate_ids, migrate_db, upgrade_languages_profile_values
    from provider_hub.startup import start_after_migrations

    migrate_db(app)
    try:
        # The first start after an upgrade has no translation runtime tables until now.
        if getattr(settings.translator, "runtime_v2", False) is True:
            from subtitles.tools.translate.runtime import run_startup_tasks
            run_startup_tasks()
    except Exception:  # pragma: no cover - must not prevent startup
        logger.exception("Unable to start the translation runtime after the migrations")
    upgrade_languages_profile_values()
    fix_languages_profiles_with_duplicate_ids()
    try:
        if getattr(settings.translator, "runtime_v2", False) is True:
            from subtitles.tools.translate.runtime.startup import start_google_from_seed
            start_google_from_seed()
    except Exception:  # pragma: no cover - the engine must not prevent startup
        logger.exception("Unable to start the seeded Google Translate engine")
    registration_ok = start_after_migrations()
    try:
        # A removal staged in the last run is finished by now: clear what the removed translators left.
        if getattr(settings.translator, "runtime_v2", False) is True:
            from subtitles.tools.translate.runtime import worker_binding
            worker_binding.sweep_removed_plugins()
    except Exception:  # pragma: no cover - must not prevent startup
        logger.exception("Unable to clear the folders of removed translators")
    return registration_ok

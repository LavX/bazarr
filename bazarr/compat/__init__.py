"""Bazarr+ OpenSubtitles-compatible REST endpoint subpackage.

DO NOT import from bazarr.subtitles.manual, bazarr.subtitles.indexer, or
TableEpisodes/TableMovies. The compat surface is isolated from DB-backed
media objects by design.
"""
from __future__ import annotations

compat_active: bool = False


def register(app, base_url: str) -> None:
    """Register the compat blueprint (real or stub) with the Flask app.

    MUST be called BEFORE api_bp registration (B3 precedence).
    When enabled=True, auto-generates any missing secrets before running the
    boot HMAC self-test. Then the self-test FAILS CLOSED if anything is still
    wrong (B6).
    """
    # Use the same `app.config` import path as the rest of bazarr. Importing
    # via `bazarr.app.config` would resolve to a SECOND module instance with
    # its own Dynaconf state, so settings written by /api/system/settings
    # would not be visible here and vice versa. Codex flagged this as
    # producing UI/runtime divergence on /system/compat/regenerate writes.
    from app.config import settings
    enabled = bool(settings.compat_endpoint.enabled)
    prefix = base_url.rstrip("/") + "/api/v1"
    global compat_active
    if enabled:
        from api.system.compat_admin import ensure_secrets
        ensure_secrets()  # idempotent; auto-generates token/jwt_secret/file_id_secret if missing
        from .auth import boot_hmac_selftest
        boot_hmac_selftest()  # fail-closed if any secret is still invalid
        from . import keyring
        keyring.seed_legacy_key()  # migrate shared token -> Default key (idempotent)
        from .routes import compat_bp
        app.register_blueprint(compat_bp, url_prefix=prefix)
        compat_active = True
    else:
        from .routes import compat_stub_bp
        app.register_blueprint(compat_stub_bp, url_prefix=prefix)
        compat_active = False

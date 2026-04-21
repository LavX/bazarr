"""Bazarr+ OpenSubtitles-compatible REST endpoint subpackage.

DO NOT import from bazarr.subtitles.manual, bazarr.subtitles.indexer, or
TableEpisodes/TableMovies. The compat surface is isolated from DB-backed
media objects by design.
"""
from __future__ import annotations


def register(app, base_url: str) -> None:
    """Register the compat blueprint (real or stub) with the Flask app.

    MUST be called BEFORE api_bp registration (B3 precedence).
    When enabled=True, auto-generates any missing secrets before running the
    boot HMAC self-test. Then the self-test FAILS CLOSED if anything is still
    wrong (B6).
    """
    from bazarr.app.config import settings
    enabled = bool(settings.compat_endpoint.enabled)
    prefix = base_url.rstrip("/") + "/api/v1"
    if enabled:
        from bazarr.api.system.compat_admin import ensure_secrets
        ensure_secrets()  # idempotent; auto-generates token/jwt_secret/file_id_secret if missing
        from .auth import boot_hmac_selftest
        boot_hmac_selftest()  # fail-closed if any secret is still invalid
        from .routes import compat_bp
        app.register_blueprint(compat_bp, url_prefix=prefix)
    else:
        from .routes import compat_stub_bp
        app.register_blueprint(compat_stub_bp, url_prefix=prefix)

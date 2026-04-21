from flask import Flask
import pytest


def test_disabled_state_404s_json(monkeypatch):
    """enabled=False: all /api/v1/* return stub JSON 404."""
    from bazarr.compat import register
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.enabled", False)
    app = Flask(__name__)
    register(app, base_url="")
    r = app.test_client().get("/api/v1/subtitles")
    assert r.status_code == 404
    assert r.headers["x-reason"] == "compat-disabled"


def test_enabled_state_auto_generates_missing_secrets(monkeypatch):
    """enabled=True with empty secrets -> ensure_secrets() auto-generates them
    at blueprint registration, then boot_hmac_selftest passes.

    The old fail-closed behavior (raise CompatBootError when secrets are
    empty) was replaced because it blocked the first-enable flow: the user
    ticks Enable + Save before any secret exists, and a fail-closed register()
    would crash the Flask app on next boot. Auto-generation is the recovery
    path.
    """
    import bazarr.api.system.compat_admin as admin
    from bazarr.app.config import settings
    from bazarr.compat import register

    monkeypatch.setattr(settings.compat_endpoint, "enabled", True)
    monkeypatch.setattr(settings.compat_endpoint, "token", "")
    monkeypatch.setattr(settings.compat_endpoint, "jwt_secret", "")
    monkeypatch.setattr(settings.compat_endpoint, "file_id_secret", "")
    # Suppress the real write_config; we only care that ensure_secrets
    # fills in-memory values so boot_hmac_selftest passes.
    monkeypatch.setattr(admin, "write_config", lambda: None)

    app = Flask(__name__)
    register(app, base_url="")  # must NOT raise

    for name in ("token", "jwt_secret", "file_id_secret"):
        assert len(getattr(settings.compat_endpoint, name)) >= 32

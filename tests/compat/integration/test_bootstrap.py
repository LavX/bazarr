def test_first_enable_autogenerates_secrets(monkeypatch):
    """When compat_endpoint.enabled flips True and secrets are missing/short,
    ensure_secrets() auto-generates them via secrets.token_urlsafe(32)."""
    from bazarr.api.system.compat_admin import ensure_secrets
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.enabled", True)
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.token", "")
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.jwt_secret", "")
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.file_id_secret", "")
    writes = {}
    ensure_secrets(write_fn=lambda k, v: writes.__setitem__(k, v))
    assert len(writes["compat_endpoint.token"]) >= 32
    assert len(writes["compat_endpoint.jwt_secret"]) >= 32
    assert len(writes["compat_endpoint.file_id_secret"]) >= 32


def test_ensure_secrets_idempotent_when_secrets_present(monkeypatch):
    """If all 3 secrets already >=32 chars, ensure_secrets is a no-op."""
    from bazarr.api.system.compat_admin import ensure_secrets
    from bazarr.app.config import settings
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.enabled", True)
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.token", "a" * 32)
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.jwt_secret", "b" * 32)
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.file_id_secret", "c" * 32)
    writes = {}
    ensure_secrets(write_fn=lambda k, v: writes.__setitem__(k, v))
    assert writes == {}, "no regeneration when secrets already valid"

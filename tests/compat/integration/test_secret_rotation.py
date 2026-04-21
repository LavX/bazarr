def test_regenerate_rotates_all_three_and_invalidates_cache(monkeypatch):
    from bazarr.api.system.compat_admin import regenerate_all_secrets
    from bazarr.compat.cache import compat_region
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.token", "o" * 32)
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.jwt_secret", "o" * 32)
    monkeypatch.setattr("bazarr.app.config.settings.compat_endpoint.file_id_secret", "o" * 32)
    compat_region.set("somekey", {"cached": True})
    assert compat_region.get("somekey") == {"cached": True}
    new_token = regenerate_all_secrets(write_fn=lambda k, v: None)
    assert len(new_token) >= 32
    # After rotation, the cached value must be gone (hard invalidate)
    from dogpile.cache.api import NO_VALUE
    assert compat_region.get("somekey") is NO_VALUE

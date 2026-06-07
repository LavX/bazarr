"""Provider Hub startup auto-install is opt-in (default off).

Auto-installing official-catalog versions of enabled built-in providers at startup
used to run unconditionally (only an undocumented env var could stop it). It is now
gated behind `settings.general.provider_hub_auto_install`, default False, so a fresh
install uses the built-ins until the user opts in. The behavior is non-destructive:
the gate only prevents *new* staging; it never uninstalls/reverts anything.
"""
import provider_hub.service as svc
from app.config import settings


def test_provider_hub_auto_install_defaults_false():
    """The new opt-in config key exists and defaults to False."""
    assert settings.general.provider_hub_auto_install is False


def test_autoinstall_skipped_when_optin_off(monkeypatch):
    """With the opt-in off, the function returns early without touching state."""
    monkeypatch.delenv("BAZARR_DISABLE_PROVIDER_AUTOINSTALL", raising=False)
    monkeypatch.setattr(settings.general, "provider_hub_auto_install", False, raising=False)

    calls = []
    monkeypatch.setattr(svc, "load_state", lambda *a, **k: calls.append("load_state") or {})

    result = svc.autoinstall_enabled_builtins()

    assert result == []
    assert calls == [], "auto-install must not reach load_state when opt-in is off"


def test_autoinstall_proceeds_when_optin_on(monkeypatch):
    """With the opt-in on (and no env kill-switch), the gate passes and work begins."""
    monkeypatch.delenv("BAZARR_DISABLE_PROVIDER_AUTOINSTALL", raising=False)
    monkeypatch.setattr(settings.general, "provider_hub_auto_install", True, raising=False)

    reached = []
    # Return no installations + no enabled providers so the function exits quickly after
    # the gate without hitting the network; we only care that the gate let it through.
    monkeypatch.setattr(svc, "load_state", lambda *a, **k: reached.append("load_state") or {"installations": {}})
    monkeypatch.setattr(svc, "_bazarr_enabled_providers", lambda *a, **k: [])

    svc.autoinstall_enabled_builtins()

    assert reached == ["load_state"], "gate should let execution reach load_state when opt-in is on"


def test_env_killswitch_overrides_optin(monkeypatch):
    """The legacy env kill-switch force-disables even when the opt-in is on."""
    monkeypatch.setenv("BAZARR_DISABLE_PROVIDER_AUTOINSTALL", "1")
    monkeypatch.setattr(settings.general, "provider_hub_auto_install", True, raising=False)

    calls = []
    monkeypatch.setattr(svc, "load_state", lambda *a, **k: calls.append("load_state") or {})

    result = svc.autoinstall_enabled_builtins()

    assert result == []
    assert calls == []

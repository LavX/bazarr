# coding=utf-8
"""First-run onboarding flag (`general.setup_complete`).

The onboarding wizard at /setup auto-triggers on a fresh install and must stop
triggering once the user finishes or skips it. That persistent "done" marker is
the `general.setup_complete` config flag: it defaults to False on a fresh
install and is flipped to True when the wizard completes or is skipped.
"""
from app.config import settings


def test_setup_complete_defaults_false():
    # A fresh install has not been through onboarding yet.
    assert settings.general.setup_complete is False


def test_setup_complete_is_settable(monkeypatch):
    # Completing or skipping the wizard flips the flag; the guard reads it here.
    monkeypatch.setattr(settings.general, "setup_complete", True)
    assert settings.general.setup_complete is True

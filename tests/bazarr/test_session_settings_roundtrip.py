# coding=utf-8
"""
Round-trip tests for the session-hardening settings.

general.trusted_proxies is a list, and the settings writer unwraps a
single-element list into a scalar unless the key's last segment is named in
array_keys. Getting that wrong is invisible until someone configures exactly
one proxy, which is the common case: the value then fails validation as "not a
list", and the save is rejected.
"""
import sys
from types import SimpleNamespace

import pytest


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


@pytest.fixture
def saveable(monkeypatch):
    """save_settings with the disk write and the database stubbed out."""
    from app import config

    monkeypatch.setattr(config, "write_config", lambda: True)
    monkeypatch.setattr(config, "validate_log_regex", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "app.database",
        SimpleNamespace(
            database=SimpleNamespace(execute=lambda statement: None),
            update=lambda _model: _FakeUpdate(),
            System=object,
        ),
    )
    return config


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        (["10.0.0.5"], ["10.0.0.5"]),
        (["10.0.0.5", "172.18.0.2"], ["10.0.0.5", "172.18.0.2"]),
        ([""], []),
    ],
)
def test_trusted_proxies_stays_a_list(saveable, submitted, expected):
    original = list(saveable.settings.general.trusted_proxies)
    try:
        saveable.save_settings([("settings-general-trusted_proxies", submitted)])

        assert saveable.settings.general.trusted_proxies == expected
    finally:
        saveable.settings.general.trusted_proxies = original


def test_trusted_proxies_survives_validation(saveable):
    """The validator types this as a list, so an unwrapped scalar is rejected."""
    original = list(saveable.settings.general.trusted_proxies)
    try:
        saveable.save_settings([("settings-general-trusted_proxies", ["10.0.0.5"])])
        saveable.settings.validators.validate()
    finally:
        saveable.settings.general.trusted_proxies = original


def test_cookie_policy_settings_round_trip(saveable):
    original = (
        saveable.settings.auth.session_lifetime_days,
        saveable.settings.auth.cookie_secure,
    )
    try:
        saveable.save_settings(
            [
                ("settings-auth-session_lifetime_days", ["7"]),
                ("settings-auth-cookie_secure", ["always"]),
            ]
        )

        # The writer casts numeric strings, so the lifetime must arrive as an int.
        assert saveable.settings.auth.session_lifetime_days == 7
        assert saveable.settings.auth.cookie_secure == "always"
        saveable.settings.validators.validate()
    finally:
        (
            saveable.settings.auth.session_lifetime_days,
            saveable.settings.auth.cookie_secure,
        ) = original


def test_the_new_settings_are_exposed_to_the_ui(saveable):
    """The settings page cannot render a field the API does not return."""
    served = saveable.get_settings()

    assert "session_lifetime_days" in served["auth"]
    assert "cookie_secure" in served["auth"]
    assert "trusted_proxies" in served["general"]


def test_the_api_key_and_password_are_not_newly_exposed(saveable):
    """Guard against widening the auth payload while editing it."""
    served = saveable.get_settings()

    # Unchanged by this work, asserted so a future edit to the auth section
    # has to look at it deliberately.
    assert set(served["auth"]) == {
        "apikey",
        "type",
        "username",
        "password",
        "session_lifetime_days",
        "cookie_secure",
    }

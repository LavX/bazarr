# coding=utf-8
"""
Round-trip tests for the session-hardening settings.

general.trusted_proxy is a single address because waitress compares the peer
against it exactly; a list would match nobody. These pin the round trip through
the settings writer, which casts and coerces values on the way in.
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
        (["127.0.0.1"], "127.0.0.1"),
        (["10.0.0.5"], "10.0.0.5"),
        # Cleared in the UI: stored as an empty string, which the helper that
        # feeds waitress turns into "trust nothing".
        ([""], ""),
    ],
)
def test_trusted_proxy_round_trips(saveable, submitted, expected):
    original = saveable.settings.general.trusted_proxy
    try:
        saveable.save_settings([("settings-general-trusted_proxy", submitted)])

        assert saveable.settings.general.trusted_proxy == expected
        saveable.settings.validators.validate()
    finally:
        saveable.settings.general.trusted_proxy = original


def test_a_cleared_trusted_proxy_cannot_break_startup(saveable):
    """An empty value must mean "trust nothing", never a server that refuses to build."""
    from app.app import trusted_proxy_value

    original = saveable.settings.general.trusted_proxy
    try:
        saveable.save_settings([("settings-general-trusted_proxy", [""])])

        assert trusted_proxy_value(saveable.settings) is None
    finally:
        saveable.settings.general.trusted_proxy = original


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
    assert "trusted_proxy" in served["general"]


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

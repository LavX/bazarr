"""Checking a settings payload against the fields a manifest declares."""
import pytest

from subtitles.tools.translate.runtime.field_schema import (field_visible, is_finite_number, is_secret_field,
                                                            validate_values)

FIELDS = (
    {"key": "base_url", "type": "text", "destination": True, "max_length": 40},
    {"key": "api_key", "type": "secret", "required": True},
    {"key": "api_keys", "type": "key_list", "secret": True, "max_items": 2},
    {"key": "temperature", "type": "number", "min": 0, "max": 2},
    {"key": "batch", "type": "integer", "min": 1, "max": 500, "default": 100},
    {"key": "thinking", "type": "toggle"},
    {"key": "preset", "type": "choice", "choices": ["openai", {"value": "custom", "label": "Custom"}]},
    {"key": "tags", "type": "list"},
    {"key": "order", "type": "orderable_list", "items": ["a", {"value": "b", "label": "B"}, "c"]},
    {"key": "model", "type": "options"},
    {"key": "region", "type": "text", "required": True, "show_if": {"key": "preset", "equals": "custom"}},
)


def check(values, **kwargs):
    return validate_values(FIELDS, values, **kwargs)


def test_a_valid_payload_is_split_into_settings_and_secrets():
    out = check({"base_url": "http://x", "api_key": "k", "api_keys": ["a", "b"], "temperature": 0.5, "batch": 10,
                 "thinking": False, "preset": "openai", "tags": ["x"], "order": ["c", "a"], "model": "free text"})
    assert out.errors == {}
    assert out.secrets == {"api_key": "k", "api_keys": ["a", "b"]}
    assert out.settings["thinking"] is False and out.settings["order"] == ["c", "a"]
    assert "api_key" not in out.settings and "api_keys" not in out.settings


@pytest.mark.parametrize("values,key", [
    ({"api_keys": "['key']"}, "api_keys"),          # no coercion of a string into a list
    ({"api_keys": ["a", ""]}, "api_keys"),
    ({"api_keys": ["a", "b", "c"]}, "api_keys"),
    ({"tags": "x"}, "tags"),
    ({"batch": "5"}, "batch"),
    ({"batch": 5.5}, "batch"),
    ({"batch": True}, "batch"),
    ({"batch": 0}, "batch"),
    ({"temperature": 3}, "temperature"),
    ({"temperature": "1"}, "temperature"),
    ({"thinking": "yes"}, "thinking"),
    ({"preset": "nope"}, "preset"),
    ({"base_url": 5}, "base_url"),
    ({"base_url": "x" * 41}, "base_url"),
    ({"order": ["a", "a"]}, "order"),
    ({"order": ["z"]}, "order"),
    ({"mystery": 1}, "mystery"),
])
def test_a_value_of_the_wrong_type_or_range_is_refused_for_that_field(values, key):
    out = check({"api_key": "k", **values})
    assert key in out.errors


def test_a_required_secret_is_met_by_a_stored_one():
    assert "api_key" in check({}).errors
    assert "api_key" not in check({}, stored_secrets=frozenset({"api_key"})).errors
    # An empty secret keeps what is stored; it does not clear it and it does not count as entered.
    out = check({"api_key": ""}, stored_secrets=frozenset({"api_key"}))
    assert "api_key" not in out.secrets and "api_key" not in out.errors


def test_a_hidden_required_field_is_not_required_and_a_shown_one_is():
    assert "region" not in check({"api_key": "k"}).errors
    assert check({"api_key": "k", "preset": "custom"}).errors["region"] == "is required"
    assert check({"api_key": "k", "preset": "custom", "region": "eu"}).errors == {}


def test_an_empty_value_is_dropped_so_the_default_applies_and_a_false_toggle_is_kept():
    out = check({"api_key": "k", "batch": None, "preset": "", "thinking": False})
    assert out.errors == {}
    assert "batch" not in out.settings and "preset" not in out.settings
    assert out.settings["thinking"] is False


def test_host_keys_are_not_unknown():
    out = check({"api_key": "k", "price": 1}, extra_keys=frozenset({"price"}))
    assert out.errors == {}


def test_secret_and_visibility_helpers():
    assert is_secret_field({"type": "secret"}) and is_secret_field({"type": "key_list", "secret": True})
    assert not is_secret_field({"type": "key_list"})
    field = {"key": "a", "show_if": {"key": "m", "in": ["x", "y"]}}
    assert field_visible(field, {"m": "y"}) and not field_visible(field, {"m": "z"})
    assert field_visible({"key": "a"}, {})


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), True, "1", 10 ** 400])
def test_a_number_must_be_real_and_finite(value):
    assert is_finite_number(value) is False
    assert "temperature" in check({"api_key": "k", "temperature": value}).errors


def test_finite_numbers_pass():
    assert is_finite_number(0) and is_finite_number(1.5) and is_finite_number(-3)


def test_an_address_may_not_carry_a_user_name_or_password():
    for url in ("https://u:p@x.test/v1", "http://u@x.test"):
        assert check({"api_key": "k", "base_url": url}).errors == {
            "base_url": "must not contain a user name or password"}
    assert check({"api_key": "k", "base_url": "http://x.test:1234/v1"}).errors == {}


def test_every_entry_of_a_list_of_addresses_is_checked_for_a_user_name_or_password():
    fields = [{"key": "mirrors", "type": "list", "destination": True}]
    refused = {"mirrors": "must not contain a user name or password"}
    assert validate_values(fields, {"mirrors": ["https://x.test", "https://user:password@example.test"]}).errors == refused
    assert validate_values(fields, {"mirrors": ["http://u@x.test"]}).errors == refused
    clean = validate_values(fields, {"mirrors": ["https://x.test", "http://y.test:1234/v1"]})
    assert clean.errors == {} and clean.settings["mirrors"] == ["https://x.test", "http://y.test:1234/v1"]
    # A list that is not a destination is not an address.
    plain = [{"key": "notes", "type": "list"}]
    assert validate_values(plain, {"notes": ["user:password@host"]}).errors == {}


MALFORMED_WITH_CREDENTIALS = [
    "https://user:password@[invalid/v1",
    "https://user:password@host:notaport/v1",
    "https://user:password@exa mple.test/v1",
    "https://user:password@/v1",
    "user:password@example.test",
    "ftp://user:password@example.test/v1",
    "javascript://user:password@example.test",
    "https://user:password@exa\tmple.test",
    "//user:password@example.test",
]


@pytest.mark.parametrize("url", MALFORMED_WITH_CREDENTIALS)
def test_a_malformed_address_with_a_password_in_it_is_refused_without_repeating_it(url):
    for fields, values in (
            ([{"key": "base_url", "type": "text", "destination": True}], {"base_url": url}),
            ([{"key": "mirrors", "type": "list", "destination": True}], {"mirrors": ["https://ok.test", url]})):
        result = validate_values(fields, values)
        assert list(result.errors) == [fields[0]["key"]] and result.settings == {}
        assert "password" not in repr(result.errors)
        assert result.errors[fields[0]["key"]] in (
            "must not contain a user name or password", "must be a web address that starts with http:// or https://")


def test_a_clean_address_is_accepted_and_one_in_a_list_is_checked_like_a_single_one():
    fields = [{"key": "base_url", "type": "text", "destination": True}]
    for url in ("https://x.test", "http://lm.lan:1234/v1", "HTTPS://X.Test/V1", "http://[::1]:8080/v1"):
        assert validate_values(fields, {"base_url": url}).errors == {}
    assert validate_values(fields, {"base_url": "not a url"}).errors == {
        "base_url": "must be a web address that starts with http:// or https://"}


def test_a_condition_sees_the_default_for_a_field_sent_empty_as_it_will_be_stored():
    fields = [{"key": "mode", "type": "text", "default": "custom"},
              {"key": "api_key", "type": "secret", "required": True, "show_if": {"key": "mode", "equals": "custom"}},
              {"key": "flag", "type": "toggle", "default": True},
              {"key": "extra", "type": "text", "required": True, "show_if": {"key": "flag", "equals": False}}]
    # An empty mode is stored as no mode, so "custom" applies and the key is required.
    assert validate_values(fields, {"mode": ""}).errors == {"api_key": "is required"}
    assert validate_values(fields, {"mode": None}).errors == {"api_key": "is required"}
    assert validate_values(fields, {"mode": "other"}).errors == {}
    # A toggle sent off is a value, not an empty one.
    assert validate_values(fields, {"flag": False, "api_key": "k"}).errors == {"extra": "is required"}


def test_one_function_checks_a_connection_with_its_model_and_prices():
    from subtitles.tools.translate.runtime.host_fields import validate_connection

    fields = [{"key": "extra", "type": "text", "required": True, "show_if": {"key": "model", "equals": "special"}},
              {"key": "model", "type": "options", "required": True},
              {"key": "price_input_per_million", "type": "number", "max": 2}]
    host = {"model": "plain", "price_input_per_million": 1, "price_output_per_million": 0}
    assert validate_connection(fields, {}, host).errors == {}
    assert validate_connection(fields, {}, {**host, "model": "special"}).errors == {"extra": "is required"}
    assert validate_connection(fields, {"extra": "x"}, {**host, "model": "special"}).errors == {}
    assert validate_connection(fields, {}, {**host, "model": ""}).errors == {"model": "is required"}
    assert validate_connection(fields, {}, {**host, "price_input_per_million": 3}).errors == {
        "price_input_per_million": "must be at most 2"}
    # The host's own rule applies when the manifest does not declare the field: a price is never negative.
    assert validate_connection(fields, {}, {**host, "price_output_per_million": -1}).errors == {
        "price_output_per_million": "must be at least 0"}
    # The settings side holds only the JSON fields.
    assert validate_connection(fields, {"extra": "x"}, host).settings == {"extra": "x"}


def test_a_stored_secret_that_stays_is_checked_against_the_present_limits_but_never_returned():
    fields = [{"key": "keys", "type": "key_list", "secret": True, "max_items": 1},
              {"key": "token", "type": "secret", "max_length": 4}]
    stored = {"keys": ["a-key", "b-key"], "token": "toolong"}
    result = validate_values(fields, {}, stored_secrets=frozenset(stored), stored_secret_values=stored)
    assert result.errors == {"keys": "must hold at most 1 entries", "token": "must be at most 4 characters"}
    assert result.secrets == {} and "a-key" not in repr(result.errors)
    # A secret the save is replacing is judged by the new value, not the stored one.
    replaced = validate_values(fields, {"token": "ok"}, stored_secrets=frozenset(stored),
                               stored_secret_values={"keys": ["a-key"]})
    assert replaced.errors == {} and replaced.secrets == {"token": "ok"}

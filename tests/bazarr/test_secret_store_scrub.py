"""The scrub module: what masks a credential in text, a payload and state.

The two layers have separate tests. The pattern layer must recognise the
token shapes a third-party library echoes without knowing the value; the
value layer must mask an exact credential no pattern would recognise,
wherever it appears. The collectors are tested against the shapes the boot
feeds them: a settings tree and a Provider Hub state document.
"""

import json
import time

import pytest

from secret_store.crypto import SECRET_MARKER_PREFIX, encrypt_secret
from secret_store.scrub import (MASK, clear_secret_values, collect_hub_secret_values,
                                collect_settings_secret_values, refresh_secret_values,
                                register_secret_values, registered_secret_values, scrub_persisted_secrets,
                                scrub_state_secrets, scrub_text, scrub_value)

FAKE_KEY = "sk-fake-provider-key-0123456789"  # pragma: allowlist secret
ANOTHER_FAKE_KEY = "another-fake-value-9876"  # pragma: allowlist secret
GOOGLE_KEY = "AIza" + "0123456789abcdefghijklmnopqrstuvwxy"  # pragma: allowlist secret
OPENROUTER_KEY = "sk-or-v1-" + "0123456789abcdef" * 4  # pragma: allowlist secret
BEARER_TOKEN = "fake-bearer-token-9876"  # pragma: allowlist secret

MANIFEST = {
    "secret_fields": ["api_key"],
    "config_schema": {"properties": {"token": {"type": "string", "secret": True}}},
}


@pytest.fixture
def scrub_registry():
    """The live value set saved, emptied and restored: a test registers its
    own values without taking the boot's out of any later test."""
    previous = registered_secret_values()
    clear_secret_values()
    yield register_secret_values
    register_secret_values(previous)


def test_a_bearer_token_is_masked_and_the_scheme_is_kept():
    line = f"GET /v1/models HTTP/1.1 - Authorization: Bearer {BEARER_TOKEN}"
    assert scrub_text(line) == "GET /v1/models HTTP/1.1 - Authorization: Bearer ***"


def test_bearer_prose_is_left_alone():
    line = "Authorization: Bearer authentication is required"
    assert scrub_text(line) == line


def test_a_goog_api_key_header_is_masked_and_its_name_is_kept():
    assert scrub_text(f"x-goog-api-key: {GOOGLE_KEY} was rejected") == "x-goog-api-key: *** was rejected"


def test_a_key_carried_without_a_label_is_masked_anyway():
    assert scrub_text(f"400 Bad Request for key {GOOGLE_KEY}") == "400 Bad Request for key ***"
    assert scrub_text(f"OpenRouter says {OPENROUTER_KEY} is invalid") == "OpenRouter says *** is invalid"


def test_ordinary_text_comes_back_unchanged():
    line = "Downloaded subtitle for Some.Show.S01E01.1080p.WEB-DL from provider opensubtitlescom"
    assert scrub_text(line) == line
    assert scrub_text("") == ""
    assert scrub_text(None) is None


def test_a_registered_value_is_masked_without_a_label(scrub_registry):
    scrub_registry([FAKE_KEY])
    assert scrub_text(f"provider said no to {FAKE_KEY}") == "provider said no to ***"


def test_a_value_contained_in_another_is_masked_whole(scrub_registry):
    scrub_registry([FAKE_KEY, FAKE_KEY[-12:]])
    scrubbed = scrub_text(f"provider said no to {FAKE_KEY}")
    assert scrubbed == "provider said no to ***"
    # Neither the whole value nor the fragment it contains survives.
    assert FAKE_KEY not in scrubbed
    assert FAKE_KEY[-12:] not in scrubbed


def test_registering_replaces_the_previous_set(scrub_registry):
    scrub_registry([FAKE_KEY])
    assert scrub_registry([ANOTHER_FAKE_KEY]) == 1
    assert scrub_text(FAKE_KEY) == FAKE_KEY
    assert scrub_text(ANOTHER_FAKE_KEY) == MASK


def test_a_value_shorter_than_the_floor_is_not_registered(scrub_registry):
    assert scrub_registry(["abcde"]) == 0
    assert scrub_text("plain words like abcde stay readable") == "plain words like abcde stay readable"


def test_the_mask_and_ciphertext_are_not_registered(scrub_registry):
    assert scrub_registry([MASK, SECRET_MARKER_PREFIX + "ZmFrZQ==", "", 17]) == 0


def test_a_payload_is_scrubbed_as_a_whole(scrub_registry):
    scrub_registry([FAKE_KEY])
    payload = {
        "message": f"boom {FAKE_KEY}",
        "details": {"url": f"https://host.example/{FAKE_KEY}", "status": "failed"},
        "rows": [FAKE_KEY, 7],
    }
    assert scrub_value(payload) == {
        "message": "boom ***",
        "details": {"url": "https://host.example/***", "status": "failed"},
        "rows": ["***", 7],
    }
    assert scrub_value((5, FAKE_KEY)) == (5, MASK)
    assert scrub_value(7) == 7


def test_settings_values_are_collected_from_the_secret_paths():
    settings_doc = {
        "sonarr": {"apikey": FAKE_KEY},
        "translator": {"gemini_keys": [GOOGLE_KEY, OPENROUTER_KEY]},
    }
    values = collect_settings_secret_values(settings_doc)
    assert FAKE_KEY in values
    assert GOOGLE_KEY in values
    assert OPENROUTER_KEY in values


def test_settings_values_are_collected_from_either_casing():
    assert FAKE_KEY in collect_settings_secret_values({"SONARR": {"APIKEY": FAKE_KEY}})


def test_a_settings_tree_without_the_sections_collects_nothing():
    assert collect_settings_secret_values({}) == set()


def test_hub_credentials_are_collected_decrypted():
    state = {
        "installations": {
            "example": {
                "manifest": MANIFEST,
                "config": {"api_key": encrypt_secret(FAKE_KEY), "region": "eu"},
            }
        }
    }
    assert collect_hub_secret_values(state) == {FAKE_KEY}


def test_a_hub_credential_that_will_not_decrypt_is_skipped():
    state = {
        "installations": {
            "example": {
                "manifest": MANIFEST,
                "config": {"api_key": SECRET_MARKER_PREFIX + "not-a-payload"},
            }
        }
    }
    assert collect_hub_secret_values(state) == set()


def test_refresh_registers_the_settings_and_the_hub_values(scrub_registry):
    state = {
        "installations": {
            "example": {"manifest": MANIFEST, "config": {"api_key": encrypt_secret(FAKE_KEY)}}
        }
    }
    count = refresh_secret_values({"sonarr": {"apikey": ANOTHER_FAKE_KEY}}, state=state)
    assert count == 2
    assert scrub_text(f"{FAKE_KEY} and {ANOTHER_FAKE_KEY}") == f"{MASK} and {MASK}"


def test_state_diagnostics_are_masked_and_credential_containers_are_not(scrub_registry):
    scrub_registry([FAKE_KEY])
    state = {
        "installations": {
            "example": {
                "last_error": f"login failed: {FAKE_KEY}",
                "config": {"api_key": SECRET_MARKER_PREFIX + FAKE_KEY},
            }
        },
        "jobs": [{"message": f"update failed for {FAKE_KEY}", "details": {"token": FAKE_KEY}}],
        "catalog_sources": {"s": {"last_error": f"Bearer {BEARER_TOKEN}"}},
    }
    assert scrub_state_secrets(state) is True
    assert state["installations"]["example"]["last_error"] == "login failed: ***"
    # The credential container holds the plugin's own stored value: it is
    # rewritten by the credential migration, never by a text scrub.
    assert state["installations"]["example"]["config"]["api_key"] == SECRET_MARKER_PREFIX + FAKE_KEY
    assert state["jobs"][0]["message"] == "update failed for ***"
    assert state["jobs"][0]["details"]["token"] == MASK
    assert state["catalog_sources"]["s"]["last_error"] == "Bearer ***"


def test_a_clean_state_document_reports_no_change():
    state = {"installations": {}, "jobs": [{"message": "Worker health check passed"}]}
    assert scrub_state_secrets(state) is False


def test_a_settings_write_rebuilds_the_registered_values(monkeypatch):
    """A settings write is where a rotated credential enters the store, so the
    scrub set follows it there rather than waiting for the next boot."""
    import app.config
    import secret_store.scrub as scrub

    calls = []
    monkeypatch.setattr(scrub, "refresh_secret_values", lambda *args, **kwargs: calls.append(True) or 0)
    app.config.write_config()
    assert calls


def test_a_stored_leak_is_rewritten_at_rest(scrub_registry, tmp_path, monkeypatch):
    scrub_registry([FAKE_KEY])
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "installations": {"example": {"last_error": f"login failed: {FAKE_KEY}"}},
                "jobs": [{"message": f"spawn failed: {FAKE_KEY}"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))
    assert scrub_persisted_secrets() is True
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["installations"]["example"]["last_error"] == "login failed: ***"
    assert stored["jobs"][0]["message"] == "spawn failed: ***"
    # A second pass has nothing left to rewrite.
    assert scrub_persisted_secrets() is False


def test_ordinary_lines_are_scrubbed_without_a_regression(scrub_registry):
    """The formatter runs this on every logged line, so a line with nothing
    to mask has to stay cheap with the live credentials registered."""
    scrub_registry([FAKE_KEY, GOOGLE_KEY, OPENROUTER_KEY, ANOTHER_FAKE_KEY])
    line = ("2026-10-18 12:00:00|INFO    |subliminal.core                   |"
            "Downloaded subtitle for Some.Show.S01E01.1080p.WEB-DL from provider|")
    start = time.monotonic()
    for _ in range(20_000):
        scrub_text(line)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0
    # Nothing to mask: the same object comes back, with no copy to leave
    # behind in a buffer or a payload.
    assert scrub_text(line) is line

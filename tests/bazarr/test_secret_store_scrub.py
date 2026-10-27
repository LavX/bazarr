"""The scrub module: what masks a credential in text, a payload and state.

The two layers have separate tests. The pattern layer must recognise the
token shapes a third-party library echoes without knowing the value; the
value layer must mask an exact credential no pattern would recognise,
wherever it appears. The collectors are tested against the shapes the boot
feeds them: a settings tree and a Provider Hub state document.
"""

import json
import threading
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
STORED_CIPHERTEXT = SECRET_MARKER_PREFIX + "gAAAAAB" + "A" * 64  # pragma: allowlist secret
SUBMITTED_CIPHERTEXT = "enc:" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="  # pragma: allowlist secret

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


def test_ciphertext_is_masked_in_both_the_stored_and_submitted_forms():
    assert scrub_text(f"stored value {STORED_CIPHERTEXT} was echoed") == "stored value *** was echoed"
    assert scrub_text(f"request carried {SUBMITTED_CIPHERTEXT}") == "request carried ***"


def test_a_bare_ciphertext_marker_is_left_alone():
    line = "values at rest carry the enc:v1: marker"
    assert scrub_text(line) == line


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


def test_a_bearer_prose_is_left_alone_and_a_long_letter_token_is_masked():
    prose = "Authorization: Bearer authentication is required"
    assert scrub_text(prose) == prose
    # A real token can be all letters. Sixteen characters is past any prose
    # word that follows "Bearer", so length alone decides there.
    assert scrub_text("Authorization: Bearer abcdefghijklmnop") == "Authorization: Bearer ***"


def test_a_state_identifier_that_equals_a_credential_is_not_masked(scrub_registry):
    """A username can equal a provider id ("opensubtitles"). The exact-value
    layer masks what a worker said, never the document's structure: masking
    there renamed the installation and its manifest to the mask on the next
    boot."""
    scrub_registry(["opensubtitles"])
    state = {
        "installations": {
            "opensubtitles": {
                "provider_id": "opensubtitles",
                "manifest": {"id": "opensubtitles"},
                "last_error": "login refused for opensubtitles",
            }
        },
        "plugin_secret_paths": ["opensubtitles.api_key"],
        "catalog_sources": {"catalog": {"url": "https://github.com/opensubtitles/catalog"}},
    }
    assert scrub_state_secrets(state) is True
    assert state["installations"]["opensubtitles"]["provider_id"] == "opensubtitles"
    assert state["installations"]["opensubtitles"]["manifest"]["id"] == "opensubtitles"
    assert state["plugin_secret_paths"] == ["opensubtitles.api_key"]
    assert state["catalog_sources"]["catalog"]["url"] == "https://github.com/opensubtitles/catalog"
    # The diagnostic field still masks it.
    assert state["installations"]["opensubtitles"]["last_error"] == "login refused for ***"


def test_a_state_shape_is_masked_even_outside_a_text_field(scrub_registry):
    """Pattern shapes never occur in legitimate identifiers, so they are
    masked everywhere in the document; only exact values are field-limited."""
    scrub_registry(["opensubtitles"])
    state = {"catalog_sources": {"s": {"resolved_commit": "Bearer abcdefghijklmnop"}}}
    assert scrub_state_secrets(state) is True
    assert state["catalog_sources"]["s"]["resolved_commit"] == "Bearer ***"


def test_a_failed_state_read_keeps_the_previous_values(scrub_registry, monkeypatch):
    """An unreadable state document must not publish a set missing every Hub
    credential: the refresh answers with the set it already had."""
    import provider_hub.state

    scrub_registry([FAKE_KEY])

    def unavailable(*args, **kwargs):
        raise OSError("state unreadable")

    monkeypatch.setattr(provider_hub.state, "load_state_strict", unavailable)
    count = refresh_secret_values({"sonarr": {"apikey": ANOTHER_FAKE_KEY}})

    assert count == 1
    assert scrub_text(FAKE_KEY) == MASK


def test_a_refresh_waits_for_the_one_in_progress_instead_of_skipping(scrub_registry, monkeypatch):
    """A refresh that starts while another is collecting must wait and then
    collect for real. Answering with the earlier set let a credential saved in
    between stay unmasked until some later refresh."""
    import secret_store.scrub as scrub

    started = threading.Event()
    release = threading.Event()

    def slow_collect(state=None):
        started.set()
        release.wait(2)
        return set()

    scrub_registry([FAKE_KEY])
    monkeypatch.setattr(scrub, "collect_hub_secret_values", slow_collect)
    first = threading.Thread(target=scrub.refresh_secret_values,
                             kwargs={"settings_obj": {"sonarr": {"apikey": FAKE_KEY}}})
    first.start()
    assert started.wait(2)

    entered = threading.Event()
    results = []

    def second_refresh():
        entered.set()
        results.append(scrub.refresh_secret_values(
            {"sonarr": {"apikey": ANOTHER_FAKE_KEY}}))

    second = threading.Thread(target=second_refresh)
    second.start()
    assert entered.wait(2)
    time.sleep(0.05)
    assert second.is_alive(), "the second refresh returned the in-progress set instead of waiting"

    release.set()
    first.join(5)
    second.join(5)
    assert results == [1]
    assert scrub_text(ANOTHER_FAKE_KEY) == MASK


def test_the_registry_publishes_values_and_their_regex_together(scrub_registry):
    """A reader that sees a new value must see the regex that masks it: the
    set and the pattern are one assignment, so there is no window where the
    value is registered and the pattern does not cover it yet."""
    import secret_store.scrub as scrub

    scrub_registry([FAKE_KEY, ANOTHER_FAKE_KEY])
    values, regex = scrub._VALUE_STATE
    assert values == {FAKE_KEY, ANOTHER_FAKE_KEY}
    assert regex is not None
    assert all(regex.search(value) for value in values)


def test_a_non_object_state_document_keeps_the_previous_values(scrub_registry, tmp_path, monkeypatch):
    """A valid JSON document with a non-object root is unreadable state, not
    an empty inventory: the refresh must keep the set it already had."""
    scrub_registry([FAKE_KEY])
    state_path = tmp_path / "state.json"
    state_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    count = refresh_secret_values({"sonarr": {"apikey": ANOTHER_FAKE_KEY}})

    assert count == 1
    assert scrub_text(FAKE_KEY) == MASK


def test_a_rolled_back_save_refreshes_the_registered_values(monkeypatch):
    """A refused save registers the credential it proposed; the caller then
    restores the saved settings. The registry has to follow that restore, or
    the active credential stops being masked."""
    import app.config

    calls = []
    monkeypatch.setattr("secret_store.scrub.refresh_secret_values",
                        lambda *args, **kwargs: calls.append(True) or 0)
    monkeypatch.setattr(app.config.settings, "reload", lambda: None)
    monkeypatch.setattr(app.config, "migrate_legacy_plex_encryption", lambda settings: None)
    monkeypatch.setattr(app.config, "decrypt_settings_in_place", lambda settings: None)

    app.config.restore_persisted_settings()

    assert calls


def test_a_settings_write_refreshes_even_when_it_raises(monkeypatch):
    """A save whose follow-up failed has still written the credential to
    disk, and a failed one is rolled back after the call: the refresh cannot
    hang off the return value."""
    import app.config

    calls = []
    monkeypatch.setattr("secret_store.scrub.refresh_secret_values",
                        lambda *args, **kwargs: calls.append(True) or 0)
    monkeypatch.setattr(app.config, "_write_config",
                        lambda **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        app.config.write_config()

    assert calls


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


def test_a_hub_job_row_masks_its_text_and_keeps_its_structure(scrub_registry, tmp_path, monkeypatch):
    """A credential that equals a structural value (a password of "install", or
    one matching a provider id) must not rewrite the row's action or ids, while
    the rest of the same row is still masked."""
    from provider_hub.service import list_jobs, record_job

    scrub_registry([FAKE_KEY, "install", "examplehub"])
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
                          encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    with pytest.raises(RuntimeError):
        with record_job("install", target_kind="provider", target_id="examplehub",
                        source_id="examplehub", from_version="1.0.0", to_version="1.1.0",
                        details={"note": f"using {FAKE_KEY}"}) as job:
            job.update(message=f"login with {FAKE_KEY}")
            raise RuntimeError(f"rejected {FAKE_KEY}")

    row = list_jobs()[-1]
    assert row["action"] == "install"
    assert row["target_id"] == "examplehub"
    assert row["source_id"] == "examplehub"
    assert row["state"] == "failed"
    assert row["message"] == "login with ***"
    assert row["error"] == "rejected ***"
    assert row["details"]["note"] == "using ***"


def test_a_hub_job_row_masks_a_credential_in_every_free_text_place(scrub_registry, tmp_path, monkeypatch):
    """The row's display names, a config echo under the details and a tuple
    inside them are masked too. The keys stay as they are, and a
    structural field that equals a registered value is not rewritten."""
    from provider_hub.service import list_jobs, record_job

    scrub_registry([FAKE_KEY, "install", "examplehub"])
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}),
                          encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_path))

    with record_job("install", target_kind="provider", target_id="examplehub",
                    target_name=f"Example {FAKE_KEY}", source_id="examplehub",
                    source_name=f"Catalog {FAKE_KEY}", from_version="1.0.0", to_version="1.1.0",
                    details={"config": {"api_key": FAKE_KEY, "mode": "install", "kept": "plain"},
                             "tried": (f"a {FAKE_KEY}", "b"),
                             "nested": [("c", {"x": FAKE_KEY})]}):
        pass

    row = list_jobs()[-1]
    assert row["action"] == "install"
    assert row["target_id"] == "examplehub"
    assert row["source_id"] == "examplehub"
    assert row["target_name"] == "Example ***"
    assert row["source_name"] == "Catalog ***"
    assert row["details"]["config"]["api_key"] == MASK
    # The keys stay; only the values are scrubbed.
    assert row["details"]["config"]["mode"] == MASK
    assert row["details"]["config"]["kept"] == "plain"
    assert row["details"]["tried"] == ["a ***", "b"]
    assert row["details"]["nested"] == [["c", {"x": MASK}]]
    assert FAKE_KEY not in state_path.read_text(encoding="utf-8")


def test_a_job_row_field_nobody_listed_still_takes_the_exact_credential(scrub_registry):
    """The structural fields are the ones spared, not the text fields the ones
    scrubbed: a free-text field a later writer adds to the row (here ``note``)
    is masked without anyone listing it, while the structural fields, the keys
    and the non-string values stay as they are."""
    from secret_store.scrub import scrub_job_secrets

    scrub_registry([FAKE_KEY, "install", "examplehub"])
    row = {
        "id": "examplehub", "action": "install", "state": "install",
        "target_kind": "provider", "target_id": "examplehub", "source_id": "examplehub",
        "from_version": "install", "to_version": "examplehub",
        "created_at": "2026-10-27T00:00:00Z", "duration_ms": 12,
        "note": f"seen {FAKE_KEY}",
        "extra": {FAKE_KEY: {"inner": [f"x {FAKE_KEY}", ("install", 3)]}},
        "retries": 3, "flag": None,
    }

    assert scrub_job_secrets(row) is True

    assert row["note"] == "seen ***"
    assert row["extra"] == {FAKE_KEY: {"inner": ["x ***", [MASK, 3]]}}
    assert row["retries"] == 3 and row["flag"] is None
    for field in ("id", "action", "state", "target_id", "source_id", "from_version", "to_version"):
        assert row[field] in ("install", "examplehub")
    assert row["target_kind"] == "provider"
    assert row["duration_ms"] == 12
    assert row["created_at"] == "2026-10-27T00:00:00Z"


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

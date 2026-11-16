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
                                scrub_job_secrets, scrub_state_secrets, scrub_text, scrub_value)

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


def test_a_value_shorter_than_the_floor_is_registered_but_not_masked_in_plain_text(scrub_registry):
    assert scrub_registry(["abcde"]) == 1
    assert scrub_text("plain words like abcde stay readable") == "plain words like abcde stay readable"
    assert scrub_text("abcde") == "abcde"


@pytest.mark.parametrize("text, masked", [
    ("password=abc", "password=***"),
    ("password = abc;", "password = ***;"),
    ("login failed for pass: abc", "login failed for pass: ***"),
    ('{"pass": "abc", "user": "name"}', '{"pass": "***", "user": "name"}'),
    ("{'pass': 'abc'}", "{'pass': '***'}"),
    ("pass: 'abc'", "pass: '***'"),
    ("GET /search?apikey=abc&page=2", "GET /search?apikey=***&page=2"),
    ("https://user:abc@host.example/path", "https://user:***@host.example/path"),
    ("https://abc:secret-pw@host.example/path", "https://***:secret-pw@host.example/path"),
    ("Authorization: Basic abc", "Authorization: Basic ***"),
    ("Authorization: abc", "Authorization: ***"),
    ('run --password "abc" now', 'run --password "***" now'),
    ("key=abc", "key=***"),
])
def test_a_short_value_is_masked_as_a_whole_token_in_context(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("text", [
    "an abcdef word and abc in prose",
    "cabbage abcdef",
    "password=abcdef",
    "password=xabc",
    "token=abc.example",
    "https://host.example/abc/path",
    "the abc.txt file",
    '{"abcd": "abcd"}',
])
def test_an_unrelated_word_with_the_same_letters_is_not_masked(scrub_registry, text):
    scrub_registry(["abc"])
    assert scrub_text(text) == text


def test_short_and_long_values_mask_together(scrub_registry):
    scrub_registry(["abc", FAKE_KEY, "ab"])
    text = f"user=ab pass=abc key={FAKE_KEY} and {FAKE_KEY} alone, abc alone"
    assert scrub_text(text) == "user=*** pass=*** key=*** and *** alone, abc alone"


def test_a_long_value_is_masked_anywhere_as_before(scrub_registry):
    scrub_registry([FAKE_KEY, "abc"])
    assert scrub_text(f"x{FAKE_KEY}y {FAKE_KEY}") == "x***y ***"


def test_a_short_captcha_key_and_a_short_username_are_collected_and_masked(scrub_registry):
    from secret_store.scrub import collect_settings_secret_values

    settings = {"anticaptcha": {"anti_captcha_key": "abcde"}, "hdbits": {"username": "ab"}}
    assert scrub_registry(collect_settings_secret_values(settings)) == 2
    assert scrub_text('anti_captcha_key=abcde "hdbits": "ab" user=ab') == \
        'anti_captcha_key=*** "hdbits": "***" user=***'
    assert scrub_text("a cabbage of abcdefg") == "a cabbage of abcdefg"


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


def test_the_repr_form_of_a_credential_survives_a_refresh(scrub_registry):
    """A refresh replaces the whole value set, so every registration carries the ``%r`` spelling itself."""
    value = "refresh'quote\\back-ünï"
    refresh_secret_values({"sonarr": {"apikey": value}})
    assert scrub_text(f"key {value}") == f"key {MASK}"
    assert scrub_text(f"key {value!r}") == f'key "{MASK}"'
    refresh_secret_values({"sonarr": {"apikey": value}})
    assert scrub_text(f"key {value!r}") == f'key "{MASK}"'
    assert registered_secret_values() == {value}


def test_the_registry_publishes_values_and_their_regex_together(scrub_registry):
    """A reader that sees a new value must see the regex that masks it: the
    set and the pattern are one assignment, so there is no window where the
    value is registered and the pattern does not cover it yet."""
    import secret_store.scrub as scrub

    scrub_registry([FAKE_KEY, ANOTHER_FAKE_KEY])
    values, regex, _short, _long = scrub._VALUE_STATE
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


@pytest.mark.parametrize("text, masked", [
    ("password=abc.", "password=***."),
    ("password=abc! next", "password=***! next"),
    ("password=abc?", "password=***?"),
    ("password=abc: failed", "password=***: failed"),
    ("/search?apikey=abc#top", "/search?apikey=***#top"),
    ("a|password=abc|b", "a|password=***|b"),
    ("password=abc/path", "password=***/path"),
    ("password=abc\\next", "password=***\\next"),
    ("first line password=abc\nsecond line", "first line password=***\nsecond line"),
    ("password=abc", "password=***"),
    ('{\\"pass\\": \\"abc\\"}', '{\\"pass\\": \\"***\\"}'),
    ('log: "{\\"user\\": \\"abc\\"}"', 'log: "{\\"user\\": \\"***\\"}"'),
])
def test_trailing_punctuation_still_ends_a_short_token(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("text, masked", [
    ("authorization: BASIC abc", "authorization: BASIC ***"),
    ("Authorization: bearer abc", "Authorization: bearer ***"),
    ("Authorization: DIGEST abc", "Authorization: DIGEST ***"),
])
def test_an_authorization_scheme_name_is_matched_in_any_case(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


def test_the_credential_itself_stays_case_sensitive(scrub_registry):
    scrub_registry(["abc"])
    assert scrub_text("Authorization: Basic ABC") == "Authorization: Basic ABC"
    assert scrub_text("password=ABC") == "password=ABC"


@pytest.mark.parametrize("short, long, text, masked", [
    ("abc", "abc@example", "mail=abc@example done", "mail=*** done"),
    ("bob", "bob@example.com", "user: bob@example.com", "user: ***"),
    ("bob", "bob@example.com", "https://bob@example.com/x and bob@example.com", "https://***/x and ***"),
])
def test_a_short_value_never_pre_empts_a_longer_credential(scrub_registry, short, long, text, masked):
    scrub_registry([short, long])
    assert scrub_text(text) == masked


def test_a_short_value_beside_a_longer_one_is_still_masked_on_its_own(scrub_registry):
    scrub_registry(["bob", "bob@example.com"])
    assert scrub_text("user=bob mail=bob@example.com") == "user=*** mail=***"


def test_a_structured_leaf_equal_to_a_short_credential_is_masked(scrub_registry):
    scrub_registry(["abc", FAKE_KEY])
    payload = {"api_key": "abc", "rows": ["abc", " abc ", "abcd", 7], "nested": ({"token": "abc"},),
               "text": "prose with abc in it"}
    assert scrub_value(payload) == {
        "api_key": "***", "rows": ["***", "***", "abcd", 7], "nested": ({"token": "***"},),
        "text": "prose with abc in it",
    }


def test_a_job_row_masks_a_long_leaf_whole_and_a_short_one_only_in_context(scrub_registry):
    # A job row is labels and ids as much as it is text, so a short credential is not wiped from a
    # leaf that merely equals it (as in a job action); a long one is masked wherever it appears.
    scrub_registry(["abc", FAKE_KEY])
    job = {"id": "abc", "action": "install", "message": "failed",
           "details": {"config": {"api_key": FAKE_KEY, "region": "eu", "note": "abc"},
                       "args": [FAKE_KEY, "other", "x=abc"]}}
    assert scrub_job_secrets(job) is True
    assert job == {"id": "abc", "action": "install", "message": "failed",
                   "details": {"config": {"api_key": "***", "region": "eu", "note": "abc"},
                               "args": ["***", "other", "x=***"]}}


def test_a_state_document_leaf_in_a_text_field_equal_to_a_short_credential_is_masked(scrub_registry):
    scrub_registry(["abc"])
    state = {"installations": {"p": {"last_error": "abc", "details": {"api_key": "abc"}, "name": "abc"}}}
    assert scrub_state_secrets(state) is True
    assert state == {"installations": {"p": {"last_error": "***", "details": {"api_key": "***"},
                                             "name": "abc"}}}


def _best_of(runs, function):
    import time

    best = None
    for _ in range(runs):
        started = time.perf_counter()
        function()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


LONG_VALUES = ["long-credential-00001-value", "long-credential-00002-value", "long-credential-00003-value"]
WORKLOAD = "GET /x?page=1&size=20&token=zz9 user=ab note: ok \"quoted\" 10:30:45 " * 4000


def test_substitution_cost_does_not_grow_with_the_number_of_registered_short_values(scrub_registry):
    # The same text, the same matches, a registry of 2 short values and then 370: the cost of a
    # scan is set by the text, so the ratio stays near one. A scan that tried every registered
    # value at every context would be two orders of magnitude slower on the large registry.
    small = ["ab", "1"]
    large = small + [f"{index:03x}"[-3:] + chr(97 + index % 26) for index in range(368)]
    assert len(set(large)) == 370

    scrub_registry(small + LONG_VALUES)
    expected = scrub_text(WORKLOAD)
    small_cost = _best_of(5, lambda: scrub_text(WORKLOAD))

    scrub_registry(large + LONG_VALUES)
    assert scrub_text(WORKLOAD) == expected
    large_cost = _best_of(5, lambda: scrub_text(WORKLOAD))

    assert large_cost < small_cost * 3


def test_the_scan_is_linear_in_the_text(scrub_registry):
    scrub_registry(["a", "ab"] + LONG_VALUES)
    small = "key=zz " * 5000
    large = small * 8

    small_cost = _best_of(5, lambda: scrub_text(small))
    large_cost = _best_of(5, lambda: scrub_text(large))

    # Eight times the text takes about eight times as long, far from its square (64).
    assert large_cost < small_cost * 20


@pytest.mark.parametrize("text, masked", [
    ("password=abc<tag>", "password=***<tag>"),
    ("password=abc(x)", "password=***(x)"),
    ("[password=abc]", "[password=***]"),
    ("{password=abc}", "{password=***}"),
    ("password=abc%20more", "password=***%20more"),
    ("password=abc+more", "password=***+more"),
    ("password=abc=more", "password=***=more"),
    ("password=abc\x01more", "password=***\x01more"),
    ("password=abc\tmore", "password=***\tmore"),
])
def test_more_characters_end_a_short_token(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("secret, text, masked", [
    ('"abc', 'password="abc', "password=***"),
    ('"abc', 'password = "abc and more', "password = *** and more"),
    ("'ab", "pass='ab;", "pass=***;"),
    ('"abc', '{"pass": "abc}', '{"pass": ***}'),
    ("abc", 'password="abc"', 'password="***"'),
])
def test_a_credential_that_begins_with_a_quote_is_found_from_the_quote(scrub_registry, secret, text, masked):
    scrub_registry([secret])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("text, masked", [
    ("http://plex:32400", "http://plex:32400"),
    ("http://plex:32400/web", "http://plex:32400/web"),
    ("http://plex:32400 and more", "http://plex:32400 and more"),
    ("http://plex@host/web", "http://***@host/web"),
    ("http://plex:pw@host/web", "http://***:pw@host/web"),
    ("http://plex:p:w@host:8080/", "http://***:p:w@host:8080/"),
])
def test_a_url_user_needs_an_at_sign_after_a_colon(scrub_registry, text, masked):
    scrub_registry(["plex"])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("text", ["at 10:30:45 today", "2026-11-15 10:30:45,123 - msg", "ratio 1:30:5"])
def test_a_short_numeric_secret_leaves_a_time_of_day_intact(scrub_registry, text):
    # A colon followed by a digit does not end a token, so the minutes of a time are not a
    # standalone value. (Seconds at the very end of a time are, as any value after a colon.)
    scrub_registry(["30", "10"])
    assert scrub_text(text) == text


def test_the_log_formatter_scrubs_the_message_and_not_the_timestamp(scrub_registry):
    import logging
    import time

    from app.logger import FileHandlerFormatter

    scrub_registry(["30", "45"])
    formatter = FileHandlerFormatter('%(asctime)s|%(levelname)-8s|%(name)-32s|%(message)s|', '%Y-%m-%d %H:%M:%S')
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "retry count=30 for user=45", None, None)
    record.created = time.mktime((2026, 11, 15, 10, 30, 45, 0, 0, -1))
    record.msecs = 0
    line = formatter.format(record)
    stamp = line.split("|", 1)[0]
    assert stamp == "2026-11-15 10:30:45"
    assert stamp == formatter.formatTime(record, formatter.datefmt)
    assert "count=*** for user=***" in line


@pytest.mark.parametrize("text, masked", [
    ("api_keys:\n  - abc\n  - other", "api_keys:\n  - ***\n  - other"),
    ("- abc", "- ***"),
    ("list:\n\t-   abc  \nnext: x", "list:\n\t-   ***  \nnext: x"),
    ('keys:\n  - "abc"', 'keys:\n  - "***"'),
    ("keys:\n  - abcd\n  - xabc", "keys:\n  - abcd\n  - xabc"),
    ("a sentence - abc in prose", "a sentence - abc in prose"),
    ("range 1 - abc", "range 1 - abc"),
])
def test_an_unquoted_yaml_list_item_is_a_value_context(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("password", ["p" * 300, "p" * 5000, "word:" * 400 + "x"])
def test_a_short_username_is_masked_whatever_the_length_of_the_password(scrub_registry, password):
    scrub_registry(["bob"])
    assert scrub_text(f"http://bob:{password}@host.example/path") == f"http://***:{password}@host.example/path"
    assert scrub_text(f"http://bob:{password} host") == f"http://bob:{password} host"


def test_the_url_user_scan_stays_linear_with_many_colons(scrub_registry):
    scrub_registry(["bob"])
    small = "http://bob:" + "p:" * 2000
    large = small * 8

    small_cost = _best_of(5, lambda: scrub_text(small))
    large_cost = _best_of(5, lambda: scrub_text(large))

    assert large_cost < max(small_cost, 0.002) * 40


def test_the_long_value_precheck_keeps_the_alternation_off_text_that_holds_none(scrub_registry):
    # About 100 long values. A text that holds none of them is cleared by one substring search per
    # value, and the alternation is never run over it; a text that holds one runs it once. Counted,
    # not timed, so the check does not depend on the machine.
    import secret_store.scrub as scrub

    many = [f"{chr(97 + index % 26)}{index:05d}-credential-value-{index}" for index in range(100)]
    text = ("2026-11-15 10:30:45 worker retrying request to https://host.example/api/v1/items?page=1 "
            "after a timeout of thirty seconds and no further detail\n") * 2500

    scrub_registry(many)
    values, regex, short_values, long_values = scrub._VALUE_STATE

    class Counting:
        calls = 0

        def finditer(self, subject):
            Counting.calls += 1
            return regex.finditer(subject)

    scrub._VALUE_STATE = (values, Counting(), short_values, long_values)

    assert scrub_text(text) == text
    assert Counting.calls == 0

    assert scrub_text("x " + many[57] + " y") == "x *** y"
    assert Counting.calls == 1


def test_the_console_format_scrubs_only_the_message(scrub_registry):
    import logging

    from app.logger import FileHandlerFormatter

    scrub_registry(["45", "7"])
    formatter = FileHandlerFormatter(
        '%(asctime)-15s - %(name)-32s (%(thread)x) :  %(levelname)s (%(module)s:%(lineno)d) - %(message)s')
    record = logging.LogRecord("name7", logging.INFO, "/x/module7.py", 45, "retry count=45 for user=7", None, None)
    line = formatter.format(record)

    assert "(module7:45)" in line
    assert "name7" in line
    assert line.endswith("retry count=*** for user=***")


def test_an_exception_and_a_stack_in_a_log_record_are_scrubbed(scrub_registry):
    import logging

    from app.logger import FileHandlerFormatter

    scrub_registry(["abc"])
    formatter = FileHandlerFormatter('%(levelname)s|%(message)s|')
    try:
        raise ValueError("password=abc")
    except ValueError:
        import sys
        record = logging.LogRecord("n", logging.ERROR, "/x/m.py", 1, "failed %s", ("user=abc",), sys.exc_info())
    record.stack_info = "stack password=abc"
    line = formatter.format(record)

    assert "abc" not in line
    assert "failed user=***" in line
    assert "password=***" in line


@pytest.mark.parametrize("text, masked", [
    ("api_keys: [abc, other]", "api_keys: [***, other]"),
    ("api_keys: [other, abc]", "api_keys: [other, ***]"),
    ("api_keys: [ other ,abc ,  x ]", "api_keys: [ other ,*** ,  x ]"),
    ('api_keys: ["abc", "other"]', 'api_keys: ["***", "other"]'),
    ("api_keys=[x,abc]", "api_keys=[x,***]"),
    ("keys: [[abc, x], [y, abc]]", "keys: [[***, x], [y, ***]]"),
    ("keys:\n  - - abc\n    - x\n  - - - abc", "keys:\n  - - ***\n    - x\n  - - - ***"),
    ("keys: [abcd, xabc, abc.x]", "keys: [abcd, xabc, abc.x]"),
    ("indexes a[abc] and b[x, abc] and (1, abc) and abc, abc", "indexes a[abc] and b[x, abc] and (1, abc) and abc, abc"),
    ("keys: [x]\nnext, abc", "keys: [x]\nnext, abc"),
])
def test_a_yaml_flow_list_and_a_nested_block_list_are_value_contexts(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


@pytest.mark.parametrize("text, masked", [
    # "Bearer" is itself registered here, so it is masked as the long value it is, and the
    # short value after it is still found: the scheme it needs was in the text it was found in.
    ("Authorization: Bearer abc", "Authorization: *** ***"),
    ("Authorization: bearer abc and Bearer abc", "Authorization: bearer *** and *** ***"),
    ("Basic abc", "Basic ***"),
])
def test_a_long_value_cannot_erase_the_context_a_short_value_needs(scrub_registry, text, masked):
    scrub_registry(["Bearer", "Basic", "abc"])
    assert scrub_text(text) == masked


def test_a_long_match_wins_where_it_overlaps_a_short_one(scrub_registry):
    scrub_registry(["abc", "abc@example.com", "pass=abcdef"])
    assert scrub_text("mail=abc@example.com and pass=abcdef and user=abc") == "mail=*** and *** and user=***"


@pytest.mark.parametrize("text, masked", [
    ("password=abc$HOME", "password=***$HOME"),
    ("password=abc^x", "password=***^x"),
])
def test_dollar_and_caret_end_a_short_token(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


def test_a_key_added_while_a_database_refresh_collects_survives_it(scrub_registry, monkeypatch):
    import secret_store.scrub as scrub

    scrub_registry([])
    scrub.add_database_secret_values(["old"])

    def collect(session):
        # A writer commits and registers its key while the collection is reading.
        scrub.add_database_secret_values(["abc"])
        return {"stored"}

    monkeypatch.setattr(scrub, "collect_database_secret_values", collect)
    scrub.refresh_database_secret_values(object())

    # "old" was added before the refresh started collecting and the database does not hold it.
    assert scrub_text("a=abc b=stored c=old") == "a=*** b=*** c=old"

    scrub.refresh_secret_values({"omdb": {"apikey": FAKE_KEY}}, state={"installations": {}})
    assert scrub_text(f"a=abc {FAKE_KEY} b=stored") == "a=*** *** b=***"

    scrub.clear_secret_values()
    assert scrub_text("a=abc") == "a=abc"


def test_adding_a_key_never_raises(scrub_registry, monkeypatch):
    import secret_store.scrub as scrub

    def broken(values):
        raise RuntimeError("boom")

    monkeypatch.setattr(scrub, "register_secret_values", broken)
    scrub.add_database_secret_values(["abc"])


def test_adding_a_key_takes_no_lock_but_its_own(scrub_registry):
    # Safe under the account, config and destination locks: a refresh that holds the
    # collection lock (and may be waiting on those) does not block an addition.
    import secret_store.scrub as scrub

    with scrub._refresh_lock:
        done = []
        import threading

        thread = threading.Thread(target=lambda: done.append(scrub.add_database_secret_values(["abc"])))
        thread.start()
        thread.join(5)
        assert done, "the addition waited on the refresh lock"
    assert scrub_text("a=abc") == "a=***"


@pytest.mark.parametrize("text, masked", [
    ('keys: ["]", abc]', 'keys: ["]", ***]'),
    ('keys: ["[", abc, "]"]', 'keys: ["[", ***, "]"]'),
    ('keys: ["a,b", abc]', 'keys: ["a,b", ***]'),
    ("keys: ['],', abc]", "keys: ['],', ***]"),
    ('keys: ["\\"]", abc]', 'keys: ["\\"]", ***]'),
    ('keys: ["a", ["]", abc], x]', 'keys: ["a", ["]", ***], x]'),
    ('keys: ["]", abcd, x]', 'keys: ["]", abcd, x]'),
    ('keys: ["]"] then abc, abc', 'keys: ["]"] then abc, abc'),
])
def test_brackets_and_commas_inside_a_quoted_flow_item_are_not_structure(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


def test_an_unterminated_quote_in_a_flow_list_does_not_loop_or_swallow_the_line(scrub_registry):
    scrub_registry(["abc"])
    assert scrub_text('keys: ["unterminated, abc]') == 'keys: ["unterminated, ***]'
    text = 'keys: [" ' + ', "' * 3000
    assert scrub_text(text) == text


def test_a_short_value_that_is_also_the_scheme_of_the_next_context_is_masked_in_both_places(scrub_registry):
    scrub_registry(["Basic", "abc"])
    assert scrub_text("Authorization: Basic abc") == "Authorization: *** ***"
    assert scrub_text("a: Basic abc, b=Basic") == "a: *** ***, b=***"


@pytest.mark.parametrize("text, masked", [
    ("keys: ['it''s ]', abc]", "keys: ['it''s ]', ***]"),
    ("keys: ['a''', abc]", "keys: ['a''', ***]"),
    ("keys: ['x''y,z''', abc]", "keys: ['x''y,z''', ***]"),
    # A quote not followed by a comma or a bracket did not close an item: nothing after it is hidden.
    ('keys: ["a, abc, b"c, abc]', 'keys: ["a, ***, b"c, ***]'),
    ('keys: ["a" x, abc]', 'keys: ["a" x, ***]'),
    # Blanks between the closing quote and the comma are fine.
    ('keys: ["]"  , abc]', 'keys: ["]"  , ***]'),
])
def test_doubled_single_quotes_escape_and_an_unbalanced_quote_hides_nothing(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


def test_a_database_refresh_that_finds_nothing_drops_the_earlier_additions(scrub_registry, monkeypatch):
    import secret_store.scrub as scrub

    scrub_registry([])
    scrub.add_database_secret_values(["abc"])
    assert scrub_text("a=abc") == "a=***"

    monkeypatch.setattr(scrub, "collect_database_secret_values", lambda session: set())
    scrub.refresh_database_secret_values(object())
    assert scrub_text("a=abc") == "a=abc"

    # Added after that refresh, so a refresh that finds it in the database keeps it.
    scrub.add_database_secret_values(["xyz"])
    assert scrub_text("a=xyz") == "a=***"
    monkeypatch.setattr(scrub, "collect_database_secret_values", lambda session: {"xyz"})
    scrub.refresh_database_secret_values(object())
    assert scrub_text("a=xyz") == "a=***"

    # And one that no longer finds it drops it, as it dropped "abc".
    monkeypatch.setattr(scrub, "collect_database_secret_values", lambda session: set())
    scrub.refresh_database_secret_values(object())
    assert scrub_text("a=xyz") == "a=xyz"


def test_overlapping_spans_are_joined_to_the_furthest_end():
    from secret_store.scrub import _merge_spans

    assert _merge_spans([(5, 8), (3, 6), (10, 12), (11, 15), (20, 21)]) == [(3, 8), (10, 15), (20, 21)]
    assert _merge_spans([(0, 10), (2, 4)]) == [(0, 10)]
    assert _merge_spans([(0, 2), (2, 4)]) == [(0, 2), (2, 4)]


def test_a_short_match_that_overlaps_a_long_one_leaves_no_part_of_either_readable(scrub_registry):
    scrub_registry(["abc", "bc@example"])
    assert scrub_text("x=abc@example") == "x=***"
    assert scrub_text("x=abc and y=bc@example") == "x=*** and y=***"


def test_a_job_action_masks_a_long_leaf_and_leaves_a_short_label_alone(scrub_registry):
    from secret_store.scrub import scrub_value

    scrub_registry(["Save", "abc", FAKE_KEY])
    action = {"kind": "discover.save", "label": "Save", "ticket": "abc", "file": f"x {FAKE_KEY}", "note": "a=abc"}

    assert scrub_value(action, short_leaves=False) == {
        "kind": "discover.save", "label": "Save", "ticket": "abc", "file": "x ***", "note": "a=***"}
    assert scrub_value({"label": FAKE_KEY, "x": "Save"}, short_leaves=False) == {"label": "***", "x": "Save"}
    # The default still masks a whole short leaf.
    assert scrub_value(action)["ticket"] == "***"


def test_the_repr_form_of_a_short_credential_is_masked_as_a_whole_token(scrub_registry):
    value = "a'b"
    scrub_registry([value])
    assert scrub_text(f"password={value}") == "password=***"
    assert scrub_text(f"password={value!r}") == 'password="***"'
    assert scrub_text(f"args={['x', value]!r}") == 'args=[\'x\', "***"]'


def test_every_registration_path_carries_the_repr_forms(scrub_registry):
    import secret_store.scrub as scrub

    long_value = "back\\slash'quote-0123"
    short_value = "x\\y"
    scrub_registry([])

    scrub.add_database_secret_values([long_value, short_value])
    assert scrub_text(f"k {long_value!r}") == f'k "{MASK}"'
    assert scrub_text(f"pass={short_value!r}") == "pass='***'"

    scrub.clear_secret_values()
    scrub.add_declared_secret_values([long_value, short_value])
    assert scrub_text(f"k {long_value!r}") == f'k "{MASK}"'
    assert scrub_text(f"pass={short_value!r}") == "pass='***'"

    scrub.clear_secret_values()
    scrub.refresh_secret_values({"sonarr": {"apikey": long_value}, "hdbits": {"passkey": short_value}},
                                state={"installations": {}})
    assert scrub_text(f"k {long_value!r}") == f'k "{MASK}"'
    assert scrub_text(f"pass={short_value!r}") == "pass='***'"


def test_a_declared_hub_secret_survives_every_refresh_and_addition(scrub_registry, monkeypatch):
    import secret_store.scrub as scrub

    scrub_registry([])
    scrub.add_declared_secret_values(["declared-secret-1", "dcl"])
    assert scrub_text("a=declared-secret-1 b=dcl") == "a=*** b=***"

    monkeypatch.setattr(scrub, "collect_database_secret_values", lambda session: {"stored"})
    scrub.refresh_database_secret_values(object())
    scrub.add_database_secret_values(["added"])
    scrub.refresh_secret_values({"omdb": {"apikey": FAKE_KEY}}, state={"installations": {}})

    assert scrub_text(f"a=declared-secret-1 b=dcl c=stored d=added e={FAKE_KEY}") == "a=*** b=*** c=*** d=*** e=***"


@pytest.mark.parametrize("text, masked", [
    ('{\\\\\\"pass\\\\\\": \\\\\\"abc\\\\\\"}', '{\\\\\\"pass\\\\\\": \\\\\\"***\\\\\\"}'),
    ('\\\\\\"abc\\\\\\"', '\\\\\\"***\\\\\\"'),
    ('x=\\\\\\"abc', 'x=\\\\\\"***'),
    ('\\"abc\\"', '\\"***\\"'),
    ('\\\\\\"abcd\\\\\\"', '\\\\\\"abcd\\\\\\"'),
])
def test_a_doubly_escaped_quote_is_a_quote_too(scrub_registry, text, masked):
    scrub_registry(["abc"])
    assert scrub_text(text) == masked


def test_a_job_row_leaf_equal_to_a_short_credential_is_left_to_the_text_scrub(scrub_registry):
    scrub_registry(["abc", "Save", FAKE_KEY])
    job = {"message": "Save", "details": {"label": "abc", "file": "m.srt password=abc", "key": FAKE_KEY}}

    assert scrub_job_secrets(job) is True
    assert job == {"message": "Save", "details": {"label": "abc", "file": "m.srt password=***", "key": "***"}}

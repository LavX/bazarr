# coding=utf-8
"""The official catalog is read through its signature, and what that costs and keeps.

Every key here is generated for the test and the host trusts only the test's own root.
"""
import json
from datetime import timedelta

import pytest

import provider_hub_state_engines as engines
from plugin_platform_helpers import service_manifest, write_state
from signed_feed_helpers import HEAD, OLDER, SignedWorld
from test_provider_hub import _manifest

# Fixtures live in the helper module; binding them here makes pytest find them.
hub_engine = engines.hub_engine
hub_store = engines.hub_store

PROVIDER = "feedprovider"
SERVICE = "feedsvc"


@pytest.fixture
def world(tmp_path, monkeypatch, service_kind):
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json")))
    return SignedWorld(monkeypatch)


def _refresh():
    from provider_hub.service import refresh_catalog

    refresh_catalog(source_ids={"official"})


def _state():
    from provider_hub.state import load_state

    return load_state()


def _official():
    return _state()["catalog_sources"]["official"]


def _entries():
    return {key: entry for key, entry in _state()["catalog_entries"].items()}


def _counter_file(tmp_path):
    path = tmp_path / "plugins" / "feed-counter.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _plugin(version="1.0.0"):
    return service_manifest(SERVICE, version)


def _provider(version="1.0.0"):
    return _manifest(provider_id=PROVIDER, version=version)


def _publish_signed(world, **kwargs):
    kwargs.setdefault("plugins", [_plugin()])
    kwargs.setdefault("providers", [_provider()])
    return world.publish(**kwargs)


# --- the cutover -------------------------------------------------------------------------


def test_a_commit_with_no_signed_file_is_read_as_the_legacy_catalog(world, tmp_path):
    world.legacy(providers=[_provider()], plugins=[_plugin()])

    _refresh()

    source = _official()
    assert source["catalog_signature"] == "unsigned (legacy)" and source["signature_verified"] is False
    # The providers list is read as it always was, and the plugins list of an unsigned catalog is ignored.
    assert {entry["provider_id"] for entry in _entries().values()} == {PROVIDER}
    assert "catalog_feeds" not in _state() or _state()["catalog_feeds"] == {}
    assert _counter_file(tmp_path) is None


def test_a_build_with_no_root_never_asks_for_a_signed_file(world, monkeypatch):
    from provider_hub import signing_roots

    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})
    world.legacy(providers=[_provider()])
    _publish_signed(world)

    _refresh()

    assert world.fetched("catalog.json.sig") == [] and world.fetched("catalog-signing-keys.json") == []
    assert _official()["catalog_signature"] == "unsigned (legacy)"


def test_an_official_branch_other_than_beta_reads_the_legacy_catalog_only(world):
    from provider_hub.service import update_catalog_source

    world.heads["main"] = "c" * 40
    world.heads["beta"] = "c" * 40
    world.legacy(commit="c" * 40, providers=[_provider()], plugins=[_plugin()])
    update_catalog_source("official", dev_ref="feature-x")
    world.monkeypatch.setattr(
        "provider_hub.service._resolve_github_ref", lambda owner, repo, ref, advertisement_sink=None: "c" * 40)

    _refresh()

    assert world.fetched("catalog.json.sig") == []
    assert {entry["provider_id"] for entry in _entries().values()} == {PROVIDER}


# --- a verified catalog ----------------------------------------------------------------------


def test_a_signed_catalog_latches_the_channel_and_stores_its_plugins(world, tmp_path):
    _publish_signed(world, version=100)

    _refresh()

    source = _official()
    assert source["catalog_signature"] == "verified" and source["signature_verified"] is True
    assert source["catalog_channel"] == "main" and source["last_error"] is None
    assert {entry["provider_id"] for entry in _entries().values()} == {PROVIDER, SERVICE}
    feed = _state()["catalog_feeds"]["main"]
    assert feed["signed_version"] == 100 and feed["key_file_version"] == 1 and feed["source_commit"] == HEAD
    # The same two numbers are mirrored to a file outside the database.
    assert _counter_file(tmp_path)["channels"]["main"] == {"signed_version": 100, "key_file_version": 1}


def test_manifests_and_bundles_come_from_the_signed_source_commit_not_the_head(world):
    from provider_hub.service import _github_raw_url
    from provider_hub.manifest import validate_plugin_bundle

    world.ancestors.add(OLDER)
    _publish_signed(world, source_commit=OLDER)

    _refresh()

    entry = next(entry for entry in _entries().values() if entry["provider_id"] == SERVICE)
    assert entry["manifest"]["source"]["commit"] == OLDER and entry["resolved_commit"] == OLDER
    provider = next(entry for entry in _entries().values() if entry["provider_id"] == PROVIDER)
    assert provider["manifest"]["source"]["commit"] == OLDER
    assert OLDER in _github_raw_url(validate_plugin_bundle(entry["manifest"]), "service.py")
    # The source still records the head it resolved, which is what trust is judged on.
    assert _official()["resolved_commit"] == HEAD and _official()["trusted"] is True


def test_a_source_commit_that_is_not_on_main_or_beta_is_refused(world):
    _publish_signed(world, source_commit=OLDER)

    _refresh()

    assert _official()["last_error"].startswith("The signed catalog names a commit that is not on")
    assert _entries() == {}


@pytest.mark.parametrize("bad", ["", "A" * 40, "abc123", "d" * 39, 7])
def test_a_source_commit_that_is_not_forty_lowercase_hex_digits_is_refused(world, bad):
    world.publish(body=world.catalog_body(source_commit=bad))

    _refresh()

    assert "source_commit" in _official()["last_error"]
    assert _entries() == {}


def test_the_unchanged_head_is_skipped_and_asks_for_nothing_more(world):
    _publish_signed(world)
    _refresh()
    before = len(world.fetched("catalog.json.sig"))

    _refresh()

    assert len(world.fetched("catalog.json.sig")) == before
    assert {entry["provider_id"] for entry in _entries().values()} == {PROVIDER, SERVICE}


# --- what a later refusal keeps ------------------------------------------------------------


def _latch(world):
    _publish_signed(world, version=100)
    _refresh()
    assert _official()["catalog_signature"] == "verified"


def _move_head(world, commit):
    # The old head is now an ancestor of the new one, which is where a signed body built from it points.
    world.ancestors.add(HEAD)
    world.heads["main"] = commit
    world.heads["beta"] = commit


def test_a_later_catalog_without_a_signature_is_refused_and_the_last_good_one_kept(world):
    _latch(world)
    _move_head(world, "e" * 40)
    world.legacy(commit="e" * 40, providers=[_provider("9.9.9")])

    _refresh()

    assert "only accepts a signed catalog" in _official()["last_error"]
    assert {entry["version"] for entry in _entries().values()} == {"1.0.0"}
    assert _state()["catalog_feeds"]["main"]["last_refusal"]["status"] == "unsigned"
    assert _official()["resolved_commit"] == HEAD


def test_a_tampered_catalog_is_refused_as_a_bad_signature(world):
    _latch(world)
    _move_head(world, "e" * 40)
    body = world.catalog_body(version=101, plugins=[_plugin("2.0.0")])
    world.publish(commit="e" * 40, body=body, envelope=world.sign(body))
    tampered = body.replace(b"2.0.0", b"2.0.1")
    world.files[("e" * 40, "signed/main/catalog.json")] = tampered

    _refresh()

    assert "bad signature" in _official()["last_error"]
    assert {entry["version"] for entry in _entries().values()} == {"1.0.0"}


def test_a_key_the_root_revoked_is_refused(world):
    _latch(world)
    _move_head(world, "e" * 40)
    key_file = world.key_file(version=2, revocations=[world.keys.revocation(world.now)])
    world.publish(commit="e" * 40, version=101, key_file=key_file)

    _refresh()

    assert "revoked key" in _official()["last_error"]


def test_an_older_signed_catalog_is_refused_as_replayed_and_channels_count_apart(world, tmp_path):
    from provider_hub.service import update_catalog_source

    _latch(world)
    _move_head(world, "e" * 40)
    world.publish(commit="e" * 40, version=99)

    _refresh()

    assert "replayed" in _official()["last_error"]
    # The beta channel has its own counter: a lower number than main's is fine there.
    world.publish(commit="e" * 40, channel="beta", version=5, plugins=[_plugin("3.0.0")])
    update_catalog_source("official", dev_ref="beta")

    _refresh()

    assert _official()["last_error"] is None and _official()["catalog_channel"] == "beta"
    feeds = _state()["catalog_feeds"]
    assert feeds["beta"]["signed_version"] == 5 and feeds["main"]["signed_version"] == 100
    assert _counter_file(tmp_path)["channels"] == {
        "beta": {"signed_version": 5, "key_file_version": 1},
        "main": {"signed_version": 100, "key_file_version": 1},
    }


def test_a_database_restore_cannot_take_the_counter_back(world, tmp_path):
    from provider_hub.state import mutate_state

    _latch(world)
    # An old backup of the database: the channel looks as if it was never latched.
    mutate_state(lambda state: state.pop("catalog_feeds"))
    _move_head(world, "e" * 40)
    world.publish(commit="e" * 40, version=90)

    _refresh()

    assert "replayed" in _official()["last_error"]
    world.publish(commit="e" * 40, version=100)
    _refresh()
    assert _official()["last_error"] is None


def test_a_wrong_host_clock_is_named_instead_of_a_signature_error(world):
    world.publish(not_before=world.now + timedelta(days=30), expires=world.now + timedelta(days=120))

    _refresh()

    error = _official()["last_error"]
    assert "clock" in error and "bad signature" not in error and "host time" in error


def test_a_host_clock_before_the_build_date_accepts_by_the_counter_and_says_so(world, monkeypatch):
    from provider_hub import catalog_feed

    monkeypatch.setattr(catalog_feed, "build_time", lambda: world.now + timedelta(days=400))
    _publish_signed(world, expires=world.now - timedelta(days=10), not_before=world.now - timedelta(days=99))

    _refresh()

    assert _official()["last_error"] is None
    assert _state()["catalog_feeds"]["main"]["clock_wrong"] is True


# --- expiry blocks installs, never running plugins ------------------------------------------


def test_an_expired_catalog_blocks_installs_and_updates_from_the_channel(world):
    from provider_hub.service import list_catalog, resolve_catalog_install, ProviderHubInstallError

    _publish_signed(world, version=100, expires=world.now + timedelta(days=89))
    _refresh()
    world.now = world.now + timedelta(days=91)

    blocked = {entry["provider_id"]: entry.get("blocked_reason") for entry in list_catalog()["entries"]}
    assert all("expired" in (reason or "") for reason in blocked.values()), blocked
    with pytest.raises(ProviderHubInstallError, match="expired"):
        resolve_catalog_install("official", SERVICE, "1.0.0", catalog_list="plugins")


def test_a_catalog_inside_its_validity_does_not_block_anything(world):
    from provider_hub.service import list_catalog

    _publish_signed(world, version=100)
    _refresh()

    assert all(not entry.get("blocked_reason") for entry in list_catalog()["entries"])


# --- a cached catalog that the host has since moved past ---------------------------------------------


def _blocked():
    from provider_hub.service import list_catalog

    return {entry["provider_id"]: entry.get("blocked_reason") for entry in list_catalog()["entries"]}


def test_a_restored_database_with_a_cache_older_than_the_counter_file_installs_nothing(world):
    from provider_hub.catalog_feed import write_counter_file, ChannelRecord
    from provider_hub.service import ProviderHubInstallError, resolve_catalog_install

    _latch(world)
    assert all(reason is None for reason in _blocked().values())
    # The file a later refresh wrote holds a higher counter than the restored database's cached catalog.
    write_counter_file({"main": ChannelRecord(150, 1)})

    assert set(_blocked()) == {PROVIDER, SERVICE}
    assert all("older than one it already accepted" in reason for reason in _blocked().values())
    with pytest.raises(ProviderHubInstallError, match="Refresh the catalog"):
        resolve_catalog_install("official", PROVIDER, "1.0.0")


def test_a_restored_database_that_never_saw_the_latch_installs_nothing_until_a_verified_refresh(world):
    from provider_hub.state import mutate_state

    _latch(world)

    def restore(state):
        # An old backup: the cached catalog is the legacy one and the channel looks never latched.
        state.pop("catalog_feeds")
        source = state["catalog_sources"]["official"]
        source.update(catalog_signature="unsigned (legacy)", signature_verified=False, catalog_expires_at=None)
        source.pop("catalog_signed_version")

    mutate_state(restore)

    assert all("older than one it already accepted" in reason for reason in _blocked().values())
    world.publish(commit=HEAD, version=100)
    _move_head(world, "e" * 40)
    world.publish(commit="e" * 40, version=101, plugins=[_plugin()], providers=[_provider()])
    _refresh()
    assert all(reason is None for reason in _blocked().values())


def test_a_legacy_cache_on_a_channel_that_never_latched_installs_as_it_always_did(world):
    world.legacy(providers=[_provider()])
    _refresh()

    assert _blocked() == {PROVIDER: None}


def test_an_explicit_main_ref_still_reads_the_signed_channel(world):
    from provider_hub.catalog_feed import feed_channel
    from provider_hub.service import update_catalog_source

    assert feed_channel("main") == "main" and feed_channel(None) == "main" and feed_channel("feature-x") is None
    _latch(world)
    update_catalog_source("official", dev_ref="main")
    _move_head(world, "e" * 40)
    world.legacy(commit="e" * 40, providers=[_provider("9.9.9")])

    _refresh()

    assert "only accepts a signed catalog" in _official()["last_error"]
    assert {entry["version"] for entry in _entries().values()} == {"1.0.0"}


def test_expiry_is_judged_on_the_channel_that_supplied_the_cached_entries(world):
    from provider_hub.state import mutate_state

    _publish_signed(world, version=100, expires=world.now + timedelta(days=89))
    _refresh()
    # The source now asks for beta, which never accepted anything, but the cached entries are main's.
    mutate_state(lambda state: state["catalog_sources"]["official"].update(dev_ref="beta"))
    world.now = world.now + timedelta(days=91)

    reasons = _blocked()

    assert reasons and all("signed main catalog expired" in reason for reason in reasons.values()), reasons


def test_the_key_file_version_counts_across_both_channels(world):
    from provider_hub.service import update_catalog_source

    world.publish(version=100, key_file=world.key_file(version=2))
    _refresh()
    update_catalog_source("official", dev_ref="beta")
    world.publish(channel="beta", version=5, key_file=world.key_file(version=1))

    _refresh()

    assert "key file is older" in _official()["last_error"]


# --- an authenticated key file counts even when its catalog is refused -------------------------------------


def test_a_root_signed_key_file_that_revokes_the_cached_signer_is_remembered_with_its_refusal(world, tmp_path):
    _latch(world)
    _move_head(world, "e" * 40)
    revoking = world.key_file(version=2, revocations=[world.keys.revocation(world.now)])
    world.publish(commit="e" * 40, version=101, key_file=revoking, plugins=[_plugin()], providers=[_provider()])

    _refresh()

    assert "revoked key" in _official()["last_error"]
    assert _state()["catalog_feeds"]["main"]["key_file_version"] == 2
    assert _counter_file(tmp_path)["channels"]["main"]["key_file_version"] == 2
    # The cache the old key file vouched for no longer installs, and the old key file is not taken back.
    assert set(_blocked()) == {PROVIDER, SERVICE}
    assert all("older than one it already accepted" in reason for reason in _blocked().values())
    _move_head(world, "f" * 40)
    world.ancestors.add("e" * 40)
    world.publish(commit="f" * 40, version=102, plugins=[_plugin()], providers=[_provider()])

    _refresh()

    assert "key file is older" in _official()["last_error"]


def test_a_key_file_seen_before_the_first_latch_is_remembered_too(world, tmp_path):
    revoking = world.key_file(version=3, revocations=[world.keys.revocation(world.now)])
    world.publish(version=100, key_file=revoking)

    _refresh()

    assert "revoked key" in _official()["last_error"]
    assert _counter_file(tmp_path)["channels"]["main"] == {"signed_version": None, "key_file_version": 3}
    assert _state()["catalog_feeds"]["main"]["key_file_version"] == 3
    world.publish(commit=HEAD, version=101, key_file=world.key_file(version=1))
    _refresh()
    assert "key file is older" in _official()["last_error"]


def test_a_cache_without_the_key_file_record_is_repaired_by_a_refresh_with_an_unchanged_head(world):
    from provider_hub.state import mutate_state

    _latch(world)
    # A cache from a release that did not record the key file version: the commit has not moved.
    mutate_state(lambda state: state["catalog_sources"]["official"].pop("catalog_key_file_version"))
    assert all("older than one it already accepted" in reason for reason in _blocked().values())

    _refresh()

    assert _official()["catalog_key_file_version"] == 1
    assert _blocked() == {PROVIDER: None, SERVICE: None}


# --- a ref that reads no signed channel ----------------------------------------------------------------


def test_nothing_installs_from_a_pinned_ref_once_a_channel_has_latched(world):
    from provider_hub.service import ProviderHubInstallError, resolve_catalog_install, update_catalog_source

    _latch(world)
    update_catalog_source("official", dev_ref=HEAD)
    world.legacy(commit=HEAD, providers=[_provider("9.9.9")])

    _refresh()

    assert _official()["trusted"] is False and "signed catalogs" in _official()["trust_reason"]
    reasons = _blocked()
    assert set(reasons) == {PROVIDER}
    assert "ref that is not signed" in reasons[PROVIDER] and "main or beta" in reasons[PROVIDER]
    assert "BAZARR_DEV_PLUGIN_SOURCE" not in reasons[PROVIDER]
    with pytest.raises(ProviderHubInstallError, match="ref that is not signed"):
        resolve_catalog_install("official", PROVIDER, "9.9.9")


def test_a_pinned_ref_on_main_is_trusted_as_before_while_nothing_has_latched(world):
    from provider_hub.service import update_catalog_source

    world.legacy(commit=HEAD, providers=[_provider()])
    update_catalog_source("official", dev_ref=HEAD)

    _refresh()

    assert _official()["trusted"] is True and _blocked() == {PROVIDER: None}


# --- the counter file is read once until something writes it -----------------------------------------------


def test_the_counter_file_is_parsed_once_for_many_checks(tmp_path, monkeypatch):
    from provider_hub import catalog_feed
    from provider_hub.catalog_feed import ChannelRecord

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "state.json"))
    catalog_feed.write_counter_file({"main": ChannelRecord(10, 2)})
    parsed = []
    real = catalog_feed._parse_counter_file
    monkeypatch.setattr(catalog_feed, "_parse_counter_file", lambda path: parsed.append(path) or real(path))

    for _ in range(5):
        assert catalog_feed.stored_record({}, "main") == ChannelRecord(10, 2)
    assert len(parsed) == 1
    catalog_feed.write_counter_file({"main": ChannelRecord(11, 2)})
    assert catalog_feed.stored_record({}, "main") == ChannelRecord(11, 2)
    assert len(parsed) == 2


# --- a refusal after the signature checked out still keeps the key file -------------------------------------


@pytest.mark.parametrize("what,message", [
    ("malformed_commit", "malformed source_commit"),
    ("off_branch_commit", "not on the official catalog's main or beta"),
])
def test_a_refusal_for_what_the_body_says_still_remembers_the_authenticated_key_file(
        world, tmp_path, what, message):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _latch(world)
    _move_head(world, "e" * 40)
    replacement = Ed25519PrivateKey.generate()
    # A key file that revokes the cached signer and lists a replacement, which signs the next catalog.
    key_file = world.key_file(version=2, revocations=[world.keys.revocation(world.now)],
                              extra=[world.keys.entry(replacement)])
    commit = "bad" if what == "malformed_commit" else "9" * 40
    body = world.catalog_body(version=101, plugins=[_plugin()], providers=[_provider()], source_commit=commit)
    world.publish(commit="e" * 40, body=body, envelope=world.sign(body, replacement), key_file=key_file)

    _refresh()

    assert message in _official()["last_error"]
    assert _state()["catalog_feeds"]["main"]["key_file_version"] == 2
    assert _counter_file(tmp_path)["channels"]["main"]["key_file_version"] == 2
    assert all("older than one it already accepted" in reason for reason in _blocked().values())


# --- one verification point ---------------------------------------------------------------------------


@pytest.fixture
def fetched_feed(world):
    from provider_hub.catalog_feed import FetchedFeed

    body = world.catalog_body(version=7)
    return FetchedFeed(body, world.sign(body), world.key_file())


def _verify(world, fetched, **kwargs):
    from provider_hub import catalog_feed

    options = {"channel": "main", "stored": catalog_feed.ChannelRecord(), "now": world.now}
    options.update(kwargs)
    return catalog_feed.verify_fetched(fetched, **options)


def test_a_verified_catalog_may_advance_state(world, fetched_feed):
    verified = _verify(world, fetched_feed)

    assert verified.result.ok and verified.may_advance_state and verified.source_commit == HEAD


def test_a_flipped_byte_is_refused(world, fetched_feed):
    from provider_hub.catalog_feed import FeedRefused, FetchedFeed

    flipped = FetchedFeed(fetched_feed.catalog.replace(b"Official", b"Offical!"), fetched_feed.envelope,
                          fetched_feed.key_file)

    with pytest.raises(FeedRefused, match="bad signature"):
        _verify(world, flipped)


def test_a_signed_file_with_no_envelope_is_refused_even_before_the_first_latch(world):
    from provider_hub.catalog_feed import FeedRefused, FetchedFeed

    with pytest.raises(FeedRefused, match="not signed"):
        _verify(world, FetchedFeed(world.catalog_body(), None, world.key_file()))


def test_a_channel_that_has_never_latched_reads_the_legacy_catalog_and_a_latched_one_does_not(world):
    from provider_hub import catalog_feed
    from provider_hub.catalog_feed import ChannelRecord, FeedRefused

    assert _verify(world, None) is None
    with pytest.raises(FeedRefused, match="only accepts a signed"):
        _verify(world, None, stored=ChannelRecord(5, 1))
    assert catalog_feed.stored_record({}, "main") == ChannelRecord()


# --- a host that cannot verify any more ----------------------------------------------------------------


def test_a_latched_host_with_no_root_refuses_the_refresh_and_names_the_cause(world, monkeypatch):
    from provider_hub import signing_roots

    _latch(world)
    _move_head(world, "e" * 40)
    world.legacy(commit="e" * 40, providers=[_provider("9.9.9")])
    # A downgrade: the database and the counter file remember the latch, this release trusts no root.
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})
    requests_before = len(world.requests)

    _refresh()

    error = _official()["last_error"]
    assert "no signing key" in error and "Update Bazarr+" in error
    assert {entry["version"] for entry in _entries().values()} == {"1.0.0"}
    assert not [url for url in world.requests[requests_before:] if url.endswith("catalog.json")]


def test_a_stale_cache_on_a_host_with_no_root_says_to_update_rather_than_to_refresh(world, monkeypatch):
    from provider_hub import signing_roots
    from provider_hub.state import mutate_state

    _latch(world)
    mutate_state(lambda state: state["catalog_sources"]["official"].update(
        catalog_signature="unsigned (legacy)", signature_verified=False))
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})

    assert all("Update Bazarr+" in reason for reason in _blocked().values())


# --- the stored key file version -----------------------------------------------------------------------


def test_a_cache_checked_with_an_older_key_file_than_the_host_accepted_installs_nothing(world):
    from provider_hub.catalog_feed import ChannelRecord, write_counter_file

    _publish_signed(world, version=100, key_file=world.key_file(version=1))
    _refresh()
    assert _blocked() == {PROVIDER: None, SERVICE: None}
    # A later refresh accepted a newer key file, which a restored database has not seen.
    write_counter_file({"main": ChannelRecord(100, 2)})

    assert set(_blocked()) == {PROVIDER, SERVICE}
    assert all("older than one it already accepted" in reason for reason in _blocked().values())


# --- the counter file ---------------------------------------------------------------------


def test_the_counter_file_keeps_the_larger_value_and_ignores_damage(tmp_path, monkeypatch):
    from provider_hub import catalog_feed
    from provider_hub.catalog_feed import ChannelRecord

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(tmp_path / "hub" / "state.json"))
    assert catalog_feed.counter_file() == tmp_path / "hub" / "plugins" / "feed-counter.json"
    catalog_feed.write_counter_file({"main": ChannelRecord(10, 2)})
    catalog_feed.write_counter_file({"main": ChannelRecord(7, 3), "beta": ChannelRecord(1, 1)})

    assert catalog_feed.read_counter_file() == {"main": ChannelRecord(10, 3), "beta": ChannelRecord(1, 1)}
    catalog_feed.counter_file().write_text("{not json", encoding="utf-8")
    assert catalog_feed.read_counter_file() == {}
    assert catalog_feed.stored_record({}, "main") == ChannelRecord()


# --- the same flows on the database the app stores its state in ----------------------------------

@pytest.fixture
def db_world(hub_store, tmp_path, monkeypatch, service_kind):
    from provider_hub import catalog_feed

    monkeypatch.setattr(catalog_feed, "counter_file", lambda: tmp_path / "plugins" / "feed-counter.json")
    return SignedWorld(monkeypatch)


def test_the_latch_and_the_last_good_catalog_survive_a_refusal_on_each_database(db_world, hub_engine):
    from provider_hub_state_engines import raw_row

    _latch(db_world)
    _move_head(db_world, "e" * 40)
    body = db_world.catalog_body(version=101, plugins=[_plugin("2.0.0")])
    db_world.publish(commit="e" * 40, body=body)
    db_world.files[("e" * 40, "signed/main/catalog.json")] = body.replace(b"2.0.0", b"2.0.1")

    _refresh()

    assert "bad signature" in _official()["last_error"]
    assert {entry["version"] for entry in _entries().values()} == {"1.0.0"}
    document = json.loads(raw_row(hub_engine)[1])
    assert document["catalog_feeds"]["main"]["signed_version"] == 100
    assert document["catalog_feeds"]["main"]["last_refusal"]["status"] == "bad_signature"


def test_a_restore_that_drops_the_row_counter_is_held_by_the_file_on_each_database(db_world):
    from provider_hub.state import mutate_state

    _latch(db_world)
    mutate_state(lambda state: state.pop("catalog_feeds"))
    _move_head(db_world, "e" * 40)
    db_world.publish(commit="e" * 40, version=99)

    _refresh()

    assert "replayed" in _official()["last_error"]
    assert "catalog_feeds" not in _state() or "signed_version" not in _state()["catalog_feeds"].get("main", {})


def test_an_update_is_refused_with_the_cause_on_a_latched_host_with_an_unsigned_ref(world):
    from provider_hub.service import apply_update, update_catalog_source
    from provider_hub.state import mutate_state

    _latch(world)
    mutate_state(lambda state: state["installations"].update({PROVIDER: {
        "provider_id": PROVIDER, "name": "Feed Provider", "kind": "subtitle_provider", "active_version": "1.0.0",
        "state": "active", "pending_restart": False, "source_id": "official", "source_bound": True,
        "origin": "catalog", "catalog_list": "providers", "manifest": _provider("1.0.0")}}))
    update_catalog_source("official", dev_ref=HEAD)
    world.legacy(commit=HEAD, providers=[_provider("1.1.0")])
    _refresh()

    refused = apply_update(PROVIDER)

    assert "ref that is not signed" in refused["last_error"] and refused["active_version"] == "1.0.0"


@pytest.mark.parametrize("body,message", [(b"{", "could not be read"), (b"[]", "could not be read")])
def test_a_verified_body_that_is_not_a_json_object_is_refused_and_keeps_the_key_file(
        world, monkeypatch, body, message):
    from provider_hub import catalog_feed, signing
    from provider_hub.catalog_feed import FeedRefused, FetchedFeed

    result = signing.VerificationResult(
        signing.Status.OK, root_key_id="r" * 16, key_file_version=4, signed_version=9, channel="main",
        host_time=world.now)
    monkeypatch.setattr(catalog_feed.signing, "verify_catalog", lambda *args, **kwargs: result)

    with pytest.raises(FeedRefused, match=message) as refused:
        _verify(world, FetchedFeed(body, b"{}", b"{}"))

    assert refused.value.key_file_version == 4

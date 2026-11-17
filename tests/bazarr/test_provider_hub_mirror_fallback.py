# coding=utf-8
"""The official catalog is read through its mirror when GitHub refuses, and only then.

Every flow here runs on SQLite and on PostgreSQL (the latter when BAZARR_PG_TEST_URL names a server). The
keys are generated for the test. GitHub is stubbed per origin, so each case fails exactly the origins it
names: the ref advertisement, raw and the Pages mirror.
"""
import hashlib
import json

import pytest
import requests

import provider_hub_state_engines as engines
from plugin_platform_helpers import service_manifest
from signed_feed_helpers import HEAD, FeedResponse, SignedWorld
from test_provider_hub import _manifest

hub_engine = engines.hub_engine
hub_store = engines.hub_store

PAGES = "https://lavx.github.io/bazarr-provider-catalog/v1/"
NEW = "1" * 40
NEWER = "2" * 40
OLD = "3" * 40
STRANGER = "4" * 40
PROVIDER = "mirrorprovider"
SERVICE = "mirrorsvc"
STAMP = "2026-11-17T10:00:00Z"
LATER_STAMP = "2026-11-17T11:00:00Z"


def _plugin(version="1.0.0"):
    return service_manifest(SERVICE, version)


def _provider(version="1.0.0"):
    return _manifest(provider_id=PROVIDER, version=version)


def pointer_bytes(commit, history, published_at=STAMP, channel="main"):
    return json.dumps({
        "channel": channel, "commit": commit, "catalog_sha256": "0" * 64,
        "published_at": published_at, "history": history,
    }).encode("utf-8")


class MirrorWorld(SignedWorld):
    """The official repository with a Pages mirror beside it, each of which can be made to refuse."""

    def __init__(self, monkeypatch):
        super().__init__(monkeypatch)
        self.pages = {}
        self.pointers = {}
        self.raw_status = None
        self.advert_status = None
        self.pages_status = None
        self.raw_headers = {}
        self.advert_headers = {}
        # How many advertisement requests are refused before it answers again; None means all of them.
        self.advert_refusals = None

    def mirror(self, commit):
        """Serve what the repository holds at ``commit`` from the mirror too."""
        for (held, path), content in list(self.files.items()):
            if held == commit:
                self.pages[(commit, path)] = content

    def pages_requests(self):
        return [url for url in self.requests if url.startswith(PAGES)]

    @staticmethod
    def _refusal(status, headers):
        response = FeedResponse(b"", status)
        response.headers = requests.structures.CaseInsensitiveDict(headers)
        return response

    def _get(self, url, timeout=None, **kwargs):
        if url.startswith(PAGES):
            self.requests.append(url)
            if self.pages_status is not None:
                return self._refusal(self.pages_status, {})
            channel, _, rest = url[len(PAGES):].partition("/")
            if rest == "current":
                content = self.pointers.get(channel)
            else:
                commit, _, path = rest.partition("/")
                content = self.pages.get((commit, path))
            return FeedResponse(b"", 404) if content is None else FeedResponse(content)
        if url.endswith("/info/refs?service=git-upload-pack") and self.advert_status is not None:
            if self.advert_refusals is None or self.advert_refusals > 0:
                if self.advert_refusals is not None:
                    self.advert_refusals -= 1
                self.requests.append(url)
                return self._refusal(self.advert_status, self.advert_headers)
        if url.startswith("https://raw.githubusercontent.com/") and self.raw_status is not None:
            self.requests.append(url)
            return self._refusal(self.raw_status, self.raw_headers)
        return super()._get(url, timeout, **kwargs)


@pytest.fixture(autouse=True)
def _forget_origins():
    from provider_hub import transport

    transport.reset_state()
    yield
    transport.reset_state()


@pytest.fixture
def world(hub_store, tmp_path, monkeypatch, service_kind):
    from provider_hub import catalog_feed

    monkeypatch.setattr(catalog_feed, "counter_file", lambda: tmp_path / "plugins" / "feed-counter.json")
    return MirrorWorld(monkeypatch)


def _refresh():
    from provider_hub.service import refresh_catalog

    refresh_catalog(source_ids={"official"})


def _state():
    from provider_hub.state import load_state

    return load_state()


def _official():
    return _state()["catalog_sources"]["official"]


def _versions():
    return {entry["provider_id"]: entry["version"] for entry in _state()["catalog_entries"].values()}


def _latch(world):
    """One ordinary refresh from raw, so the channel is latched at signed version 100."""
    world.publish(version=100, plugins=[_plugin()], providers=[_provider()])
    _refresh()
    assert _official()["catalog_signature"] == "verified"
    assert _official()["resolved_commit"] == HEAD


def _publish_new(world, commit=NEW, version=101, **kwargs):
    kwargs.setdefault("plugins", [_plugin("2.0.0")])
    kwargs.setdefault("providers", [_provider("2.0.0")])
    world.publish(commit=commit, version=version, source_commit=commit, **kwargs)
    world.mirror(commit)


def _github_down(world, pointer=None, *, status=503, raw=True):
    world.advert_status = status
    if raw:
        world.raw_status = status
    if pointer is not None:
        world.pointers["main"] = pointer


# --- GitHub refuses, the mirror serves a newer catalog ---------------------------------------------


def test_a_refused_advertisement_is_replaced_by_the_mirror_pointer_and_the_mirror_serves_the_files(world):
    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]))

    _refresh()

    source = _official()
    assert source["last_error"] is None and source["resolved_commit"] == NEW
    assert _versions() == {PROVIDER: "2.0.0", SERVICE: "2.0.0"}
    assert source["last_origin"] == "pages"
    assert source["last_failure"]["origin"] == "github" and source["last_failure"]["status"] == 503
    assert source["last_failure"]["surface"] == "pointer" and source["last_failure"]["kind"] == "server_error"
    assert source["mirror_pointer"] == {"commit": NEW, "published_at": STAMP}
    # The channel's counter moved on the mirror's signed bytes, the way it does on raw's.
    assert _state()["catalog_feeds"]["main"]["signed_version"] == 101
    assert _state()["catalog_feeds"]["main"]["source_commit"] == NEW


def test_raw_refusing_while_the_advertisement_answers_serves_the_files_from_the_mirror(world):
    _latch(world)
    world.heads["main"] = world.heads["beta"] = NEW
    world.ancestors.add(HEAD)
    _publish_new(world)
    world.raw_status = 503

    _refresh()

    source = _official()
    assert source["last_error"] is None and _versions()[SERVICE] == "2.0.0"
    assert source["last_origin"] == "pages" and source["last_failure"]["surface"] == "catalog"
    # The head came from GitHub, so the mirror's pointer was never read.
    assert not any(url.endswith("/current") for url in world.pages_requests())


def test_what_the_transport_learnt_is_kept_with_the_state_and_survives_a_restart(world):
    from provider_hub import transport

    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]), status=429)
    world.raw_headers = {"Retry-After": "600"}

    _refresh()

    saved = _state()["transport"]
    assert saved["retry_after"]["raw"]["until"] > 0
    transport.reset_state()
    transport.restore_state(saved)
    assert transport.export_state()["retry_after"]["raw"]["until"] == saved["retry_after"]["raw"]["until"]


def test_a_flipped_byte_from_the_mirror_is_refused_and_the_held_catalog_kept(world):
    _latch(world)
    _publish_new(world)
    key = (NEW, "signed/main/catalog.json")
    world.pages[key] = world.pages[key].replace(b"2.0.0", b"2.0.1")
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]))

    _refresh()

    source = _official()
    assert "bad signature" in source["last_error"]
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}
    assert source["resolved_commit"] == HEAD
    assert _state()["catalog_feeds"]["main"]["signed_version"] == 100


def test_a_catalog_the_host_already_accepted_is_refused_when_the_mirror_serves_it_again(world):
    _latch(world)
    _publish_new(world, version=99)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]))

    _refresh()

    assert "replayed" in _official()["last_error"]
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


# --- no signed file on the mirror is never a reason to read an unsigned catalog -------------------------


def test_a_mirror_without_a_signed_file_never_supplies_the_legacy_catalog(world):
    # No channel has latched yet, so the raw path would read the legacy catalog on a 404 of the primary.
    world.legacy(commit=NEW, providers=[_provider()])
    world.mirror(NEW)
    _github_down(world, pointer_bytes(NEW, [NEW]), raw=False)

    _refresh()

    assert "no signed file" in _official()["last_error"]
    assert _state()["catalog_entries"] == {}
    # The commit was named by the mirror, so the unsigned catalog is not read from raw at it either.
    assert world.fetched(f"{NEW}/catalog.json") == []


def test_a_404_from_the_mirror_after_raw_failed_is_not_a_missing_signed_file(world):
    # Advertisement answers, raw is refusing, and the mirror has nothing at the commit: the legacy catalog
    # at raw would be the answer to a primary 404, but this is not one.
    world.legacy(providers=[_provider()])
    world.raw_status = 503

    _refresh()

    source = _official()
    assert source["last_error"] and _state()["catalog_entries"] == {}
    assert source.get("catalog_signature") is None


def test_only_the_primarys_404_means_there_is_no_signed_file(world):
    world.legacy(providers=[_provider()])

    _refresh()

    source = _official()
    assert source["catalog_signature"] == "unsigned (legacy)" and _versions() == {PROVIDER: "1.0.0"}
    assert world.pages_requests() == []


def test_a_latched_host_refuses_an_unsigned_mirror_catalog_as_unsigned(world):
    _latch(world)
    world.legacy(commit=NEW, providers=[_provider("9.9.9")])
    world.mirror(NEW)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]), raw=False)

    _refresh()

    assert "not signed" in _official()["last_error"]
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


# --- the pointer must prove forward progress ------------------------------------------------------


def test_a_pointer_at_an_older_commit_is_refused_and_nothing_changes(world):
    _latch(world)
    _publish_new(world, commit=OLD, version=99)
    _github_down(world, pointer_bytes(OLD, [OLD, STRANGER]))

    _refresh()

    source = _official()
    assert "cannot prove" in source["last_error"] and source["resolved_commit"] == HEAD
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}
    assert _state()["catalog_feeds"]["main"]["signed_version"] == 100
    # The signed files were never asked for: the pointer was refused first.
    assert not any("/signed/" in url for url in world.pages_requests())


def test_a_stored_commit_outside_the_pointers_history_is_refused(world):
    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, STRANGER, OLD]))

    _refresh()

    assert "cannot prove" in _official()["last_error"]
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


def test_a_pointer_listing_the_stored_commit_as_its_own_head_only_is_the_stored_commit(world):
    # history[0] is the pointer's own commit, so a history of [HEAD] is the unchanged head, not progress.
    _latch(world)
    _github_down(world, pointer_bytes(HEAD, [HEAD]))

    _refresh()

    source = _official()
    assert source["last_error"] is None and source["resolved_commit"] == HEAD
    # Nothing was read, so the held catalog is still the one raw served.
    assert source["last_origin"] == "raw"
    assert not any("/signed/" in url for url in world.pages_requests())


def test_a_pointer_that_is_not_newer_than_the_last_one_accepted_is_refused(world):
    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]))
    _refresh()
    assert _official()["resolved_commit"] == NEW
    _publish_new(world, commit=NEWER, version=102, plugins=[_plugin("3.0.0")], providers=[_provider("3.0.0")])
    world.pointers["main"] = pointer_bytes(NEWER, [NEWER, NEW, HEAD], published_at=STAMP)
    from provider_hub import transport

    transport.reset_state()
    _refresh()

    assert "not newer" in _official()["last_error"]
    assert _versions() == {PROVIDER: "2.0.0", SERVICE: "2.0.0"}

    world.pointers["main"] = pointer_bytes(NEWER, [NEWER, NEW, HEAD], published_at=LATER_STAMP)
    transport.reset_state()
    _refresh()

    assert _official()["last_error"] is None and _versions()[SERVICE] == "3.0.0"
    assert _official()["mirror_pointer"] == {"commit": NEWER, "published_at": LATER_STAMP}


@pytest.mark.parametrize("document", [
    b"not json", b"[]",
    json.dumps({"channel": "beta", "commit": NEW, "published_at": STAMP, "history": [NEW]}).encode(),
    json.dumps({"channel": "main", "commit": "x" * 40, "published_at": STAMP, "history": ["x" * 40]}).encode(),
    json.dumps({"channel": "main", "commit": NEW, "published_at": "yesterday", "history": [NEW]}).encode(),
    json.dumps({"channel": "main", "commit": NEW, "published_at": STAMP, "history": [HEAD, NEW]}).encode(),
    json.dumps({"channel": "main", "commit": NEW, "published_at": STAMP, "history": [NEW, NEW]}).encode(),
])
def test_a_pointer_that_is_not_one_is_not_used_and_githubs_failure_is_shown(world, document):
    _latch(world)
    _publish_new(world)
    _github_down(world, document, status=429)
    world.advert_headers = {"Retry-After": "120"}

    _refresh()

    source = _official()
    assert "temporarily limited" in source["last_error"] and source["resolved_commit"] == HEAD
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


# --- an install with no stored commit, and the order of preference ----------------------------------


def test_with_nothing_stored_a_pointer_whose_catalog_verifies_is_accepted_and_recorded(world):
    world.publish(commit=NEW, version=100, source_commit=NEW, plugins=[_plugin()], providers=[_provider()])
    world.mirror(NEW)
    _github_down(world, pointer_bytes(NEW, [NEW]))

    _refresh()

    source = _official()
    assert source["last_error"] is None and source["resolved_commit"] == NEW
    assert source["mirror_pointer"] == {"commit": NEW, "published_at": STAMP}
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}
    assert _state()["catalog_feeds"]["main"]["signed_version"] == 100


def test_with_nothing_stored_a_catalog_that_fails_its_signature_is_refused_and_the_install_stays_empty(world):
    world.publish(commit=NEW, version=100, source_commit=NEW, plugins=[_plugin()], providers=[_provider()])
    key = (NEW, "signed/main/catalog.json")
    world.files[key] = world.files[key].replace(b"1.0.0", b"1.0.1")
    world.mirror(NEW)
    _github_down(world, pointer_bytes(NEW, [NEW]))

    _refresh()

    assert "bad signature" in _official()["last_error"]
    assert _state()["catalog_entries"] == {} and _official().get("resolved_commit") is None
    assert "catalog_feeds" not in _state() or "main" not in _state()["catalog_feeds"] or (
        "signed_version" not in _state()["catalog_feeds"]["main"])


def test_when_github_answers_the_mirror_is_never_asked(world):
    world.publish(version=100, plugins=[_plugin()], providers=[_provider()])
    world.mirror(HEAD)
    world.pointers["main"] = pointer_bytes(HEAD, [HEAD])

    _refresh()

    assert _official()["last_origin"] == "raw" and _official()["last_failure"] is None
    assert world.pages_requests() == []


# --- who may use the mirror -----------------------------------------------------------------------


def test_a_source_on_a_development_ref_never_reads_the_mirror(world):
    from provider_hub.service import update_catalog_source

    _latch(world)
    _publish_new(world)
    update_catalog_source("official", dev_ref="beta")
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]))

    _refresh()

    assert _official()["last_error"]
    assert world.pages_requests() == []
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


def test_a_community_source_never_reads_the_mirror(world):
    from provider_hub.service import add_catalog_source, refresh_catalog

    add_catalog_source("Community", "https://github.com/someone/catalog/blob/main/catalog.json")
    world.advert_status = 503
    world.pointers["main"] = pointer_bytes(NEW, [NEW])
    world.pages[(NEW, "catalog.json")] = b"{}"
    world.community = lambda url, timeout: FeedResponse(b"", 503)

    refresh_catalog(source_ids={"Community"})

    assert world.pages_requests() == []
    assert _state()["catalog_sources"]["Community"]["last_error"]
    assert "last_origin" not in _state()["catalog_sources"]["Community"]


def test_with_the_setting_off_a_refused_advertisement_fails_as_it_always_did(world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "provider_hub_allow_fallback", False, raising=False)
    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]), status=429)
    world.advert_headers = {"Retry-After": "120"}

    _refresh()

    source = _official()
    assert "temporarily limited" in source["last_error"] and source["resolved_commit"] == HEAD
    assert world.pages_requests() == []
    assert "last_origin" not in source and "last_failure" not in source and "mirror_pointer" not in source
    assert "transport" not in _state()
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


def test_with_the_setting_off_a_refused_raw_fails_and_the_mirror_is_not_asked(world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "provider_hub_allow_fallback", False, raising=False)
    _latch(world)
    world.heads["main"] = world.heads["beta"] = NEW
    world.ancestors.add(HEAD)
    _publish_new(world)
    world.raw_status = 503

    _refresh()

    assert _official()["last_error"] and world.pages_requests() == []
    assert _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}


def test_with_the_setting_off_a_healthy_refresh_matches_the_old_one(world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.general, "provider_hub_allow_fallback", False, raising=False)
    world.publish(version=100, plugins=[_plugin()], providers=[_provider()])

    _refresh()

    source = _official()
    assert source["catalog_signature"] == "verified" and _versions() == {PROVIDER: "1.0.0", SERVICE: "1.0.0"}
    assert "last_origin" not in source and "last_failure" not in source and "transport" not in _state()


def test_a_build_with_no_root_never_reads_the_mirror(world, monkeypatch):
    from provider_hub import signing_roots

    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW]))

    _refresh()

    assert _official()["last_error"] and world.pages_requests() == []


# --- bundles --------------------------------------------------------------------------------------

BUNDLE_COMMIT = "5" * 40
BUNDLE_FILE = b"class ExampleProvider: pass\n"


def _bundle_manifest(content=BUNDLE_FILE):
    files = {"provider.py": content}
    sha = hashlib.sha256(content).hexdigest()
    manifest = _manifest(provider_id="mirrorbundle", version="1.0.0")
    digest = hashlib.sha256()
    digest.update(b"provider.py\0" + str(len(content)).encode() + b"\0" + content + b"\0")
    manifest["files"] = {name: sha for name in files}
    manifest["bundle_sha256"] = digest.hexdigest()
    manifest["source"] = {
        "type": "github", "repo": "LavX/bazarr-provider-catalog", "ref": "main", "commit": BUNDLE_COMMIT,
        "path": "providers/mirrorbundle",
    }
    return manifest


@pytest.fixture
def bundle_env(tmp_path, monkeypatch):
    from provider_hub import service

    state_file = tmp_path / "bundle-hub" / "state.json"
    state_file.parent.mkdir()
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}))
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(tmp_path / "no-floor"))
    served = {}
    calls = []

    def fake_get(url, timeout=None, **kwargs):
        calls.append(url)
        if url.startswith("https://raw.githubusercontent.com/"):
            return FeedResponse(b"", 503)
        if url.startswith(PAGES):
            content = served.get(url[len(PAGES):])
            return FeedResponse(b"", 404) if content is None else FeedResponse(content)
        raise AssertionError(url)

    monkeypatch.setattr(service.requests, "get", fake_get)
    served[f"main/{BUNDLE_COMMIT}/providers/mirrorbundle/provider.py"] = BUNDLE_FILE
    return service, served, calls


def _fetch(service, manifest, channel="main"):
    from provider_hub.manifest import validate_manifest

    token = service._BUNDLE_MIRROR_CHANNEL.set(channel)
    try:
        return service._fetch_bundle(validate_manifest(manifest))
    finally:
        service._BUNDLE_MIRROR_CHANNEL.reset(token)


def test_a_bundle_is_read_from_the_mirror_when_raw_refuses_and_checked_against_its_pins(bundle_env):
    service, _served, calls = bundle_env

    path = _fetch(service, _bundle_manifest())

    assert (path / "provider.py").read_bytes() == BUNDLE_FILE
    assert any(url.startswith(PAGES) for url in calls)


def test_a_flipped_byte_in_a_mirrored_bundle_file_is_refused(bundle_env):
    from provider_hub.bundle import BundleValidationError

    service, served, _calls = bundle_env
    served[f"main/{BUNDLE_COMMIT}/providers/mirrorbundle/provider.py"] = b"class ExampleProvider: pass\n#"

    with pytest.raises(BundleValidationError):
        _fetch(service, _bundle_manifest())


def test_an_install_that_does_not_ask_for_the_mirror_reads_github_only(bundle_env):
    service, _served, calls = bundle_env
    from provider_hub.manifest import validate_manifest

    with pytest.raises(requests.exceptions.HTTPError):
        service._fetch_bundle(validate_manifest(_bundle_manifest()))

    assert not any(url.startswith(PAGES) for url in calls)


def test_the_mirror_missing_the_file_reports_the_failure_of_raw(bundle_env):
    service, served, _calls = bundle_env
    served.clear()

    with pytest.raises(requests.exceptions.HTTPError) as caught:
        _fetch(service, _bundle_manifest())

    assert caught.value.response.status_code == 503


def test_a_foreign_repository_bundle_is_never_asked_of_the_mirror(bundle_env):
    service, _served, calls = bundle_env
    manifest = _bundle_manifest()
    manifest["source"]["repo"] = "someone/else"

    with pytest.raises(requests.exceptions.HTTPError):
        _fetch(service, manifest)

    assert not any(url.startswith(PAGES) for url in calls)


def _state_with(tmp_path, monkeypatch, official=None):
    source = {"id": "official", "name": "Official", "official": True, "url": "x", "dev_ref": None}
    source.update(official or {})
    return {"catalog_sources": {"official": source, "community": {"id": "community", "name": "Community"}}}


def test_only_the_official_source_on_its_default_branch_may_read_bundles_from_the_mirror(tmp_path, monkeypatch):
    from provider_hub.service import _bundle_mirror_channel

    state = _state_with(tmp_path, monkeypatch)
    assert _bundle_mirror_channel(state, "official") == "main"
    assert _bundle_mirror_channel(state, "community") is None
    assert _bundle_mirror_channel(state, None) is None
    assert _bundle_mirror_channel(_state_with(tmp_path, monkeypatch, {"dev_ref": "beta"}), "official") is None
    assert _bundle_mirror_channel(_state_with(tmp_path, monkeypatch, {"dev_ref": "feature-x"}), "official") is None


def test_with_the_setting_off_no_bundle_may_read_the_mirror(tmp_path, monkeypatch):
    from app.config import settings
    from provider_hub.service import _bundle_mirror_channel

    monkeypatch.setattr(settings.general, "provider_hub_allow_fallback", False, raising=False)

    assert _bundle_mirror_channel(_state_with(tmp_path, monkeypatch), "official") is None


def test_stage_install_sets_the_mirror_channel_for_the_bound_source_only(tmp_path, monkeypatch):
    from provider_hub import service

    seen = []
    state = {"catalog_sources": {"official": {"id": "official", "official": True, "dev_ref": None}}}
    monkeypatch.setattr(service, "load_state", lambda: state)
    monkeypatch.setattr(service, "_catalog_binding", lambda manifest, held, source_id=None: (source_id, True))
    monkeypatch.setattr(service, "_manifest_catalog_list", lambda *a, **k: "providers")
    monkeypatch.setattr(service, "_install_refusal", lambda *a, **k: None)
    monkeypatch.setattr(service, "_host_refusal", lambda *a, **k: None)
    monkeypatch.setattr(service, "_refuse_a_release_that_breaks_dependents", lambda *a, **k: None)
    monkeypatch.setattr(
        service, "_stage_validated",
        lambda *a, **k: seen.append(service._BUNDLE_MIRROR_CHANNEL.get()) or {})
    manifest = _provider()

    service.stage_install(manifest, source_id="official")
    service.stage_install(manifest, source_id="community")

    assert seen == ["main", None]
    assert service._BUNDLE_MIRROR_CHANNEL.get() is None


# --- a parked primary and a mirror that has nothing -----------------------------------------------


def _park_raw(seconds=600):
    import time

    from provider_hub import transport

    now = time.time()
    transport.restore_state({"retry_after": {"raw": {"until": now + seconds, "at": now, "kind": "rate_limited"}}})
    return now + seconds


def test_a_parked_primary_does_not_stop_a_host_that_never_latched_from_reading_the_legacy_catalog(world):
    until = _park_raw()
    world.legacy(providers=[_provider()])

    _refresh()

    # Nothing here was ever accepted as signed, so the signed file that cannot be asked for is as good as
    # missing: the legacy catalog is read exactly as it was before a root was embedded.
    source = _official()
    assert source["last_error"] is None and source["catalog_signature"] == "unsigned (legacy)"
    assert {entry["provider_id"] for entry in _state()["catalog_entries"].values()} == {PROVIDER}
    assert until > 0


def test_a_mirror_that_has_the_catalog_but_not_its_envelope_is_not_an_unsigned_catalog(world):
    world.publish(version=100, plugins=[_plugin()], providers=[_provider()])
    world.mirror(HEAD)
    del world.pages[(HEAD, "signed/main/catalog.json.sig")]
    # The first file fails over, so the rest of the job stays on the mirror.
    world.raw_status = 503

    _refresh()

    source = _official()
    assert source["last_error"] and _state()["catalog_entries"] == {}
    assert source.get("catalog_signature") is None
    assert "catalog_feeds" not in _state() or "main" not in _state()["catalog_feeds"]


def test_a_signed_source_commit_outside_the_pointers_history_is_checked_against_the_branches(world):
    # The pointer is not signed, so its history is no proof that a commit is on main or beta.
    world.legacy(commit=OLD)
    world.publish(commit=NEW, version=101, source_commit=OLD, plugins=[_plugin()], providers=[_provider()])
    world.mirror(NEW)
    # The advertisement refuses the refresh once and answers the trust check that follows.
    _github_down(world, pointer_bytes(NEW, [NEW, OLD]), raw=False)
    world.advert_refusals = 1

    _refresh()

    assert "not on the official catalog's main or beta" in _official()["last_error"]
    assert _state()["catalog_entries"] == {}


def test_a_pointer_for_the_stored_commit_with_an_older_stamp_never_lowers_the_floor(world):
    from provider_hub import transport

    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD], published_at=LATER_STAMP))
    _refresh()
    assert _official()["mirror_pointer"]["published_at"] == LATER_STAMP

    world.pointers["main"] = pointer_bytes(NEW, [NEW, HEAD], published_at=STAMP)
    transport.reset_state()
    _refresh()
    assert _official()["mirror_pointer"] == {"commit": NEW, "published_at": LATER_STAMP}

    _publish_new(world, commit=NEWER, version=102, plugins=[_plugin("3.0.0")], providers=[_provider("3.0.0")])
    world.pointers["main"] = pointer_bytes(NEWER, [NEWER, NEW, HEAD], published_at="2026-11-17T10:30:00Z")
    transport.reset_state()
    _refresh()

    assert "not newer" in _official()["last_error"] and _versions()[SERVICE] == "2.0.0"


def test_a_restriction_lifted_by_a_success_stays_lifted_after_a_restart(world):
    import time

    from provider_hub import transport
    from provider_hub.state import mutate_state

    now = time.time()
    mutate_state(lambda state: state.__setitem__("transport", {
        "cooldowns": {}, "retry_after": {"raw": {"until": now - 5, "at": now - 100, "kind": "rate_limited"}},
        "probes": {"raw": {"at": now - 100}}}))
    world.publish(version=100, plugins=[_plugin()], providers=[_provider()])

    _refresh()

    assert _official()["last_origin"] == "raw"
    saved = _state()["transport"]
    assert saved["retry_after"] == {} and saved["probes"] == {}
    transport.reset_state()
    transport.restore_state(saved)
    assert transport.export_state()["retry_after"] == {}


def test_an_unchanged_check_leaves_where_the_held_catalog_came_from(world):
    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]))
    _refresh()
    assert _official()["last_origin"] == "pages"
    failure = _official()["last_failure"]

    world.advert_status = world.raw_status = None
    world.heads["main"] = world.heads["beta"] = NEW
    _refresh()

    source = _official()
    assert source["last_error"] is None and source["last_origin"] == "pages"
    assert source["last_failure"] == failure


def test_a_failed_advertisement_is_not_recorded_as_a_failure_of_raw(world):
    _latch(world)
    _publish_new(world)
    _github_down(world, pointer_bytes(NEW, [NEW, HEAD]), raw=False)

    _refresh()

    assert _official()["last_failure"]["origin"] == "github"


def test_a_parked_primary_and_a_mirror_without_a_bundle_file_fail_with_that_state(bundle_env):
    from provider_hub import transport

    service, served, calls = bundle_env
    served.clear()
    _park_raw()

    with pytest.raises(transport.NoOriginAvailable) as caught:
        _fetch(service, _bundle_manifest())

    assert "not answering requests right now" in str(caught.value)
    assert caught.value.failure.retry_at is not None
    assert all(url.startswith(PAGES) for url in calls)


def test_a_bundle_path_the_origins_cannot_address_is_read_from_github_only(bundle_env):
    from provider_hub.manifest import validate_manifest

    service, _served, calls = bundle_env
    token = service._BUNDLE_MIRROR_CHANNEL.set("main")
    try:
        with pytest.raises(requests.exceptions.HTTPError):
            service._fetch_github_bundle_file(validate_manifest(_bundle_manifest()), "my file.py")
    finally:
        service._BUNDLE_MIRROR_CHANNEL.reset(token)

    assert calls and all(url.startswith("https://raw.githubusercontent.com/") for url in calls)


def test_a_failed_store_of_the_origin_record_does_not_fail_the_install(monkeypatch):
    from provider_hub import service

    monkeypatch.setattr(service, "load_state", lambda: (_ for _ in ()).throw(RuntimeError("unreadable")))

    service._persist_transport()

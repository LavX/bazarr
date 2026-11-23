# coding=utf-8
"""The runtime reader of the catalog snapshot shipped in the image.

The official catalog source falls back to the snapshot when it has never
refreshed successfully, marks what it seeds, trusts it only as the release's own
catalog, serves bundle bytes from it, and never lets it stand in for, or replace,
a catalog that was fetched. The snapshot directory is read-only to the reader.
"""
import copy
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest
import requests

from provider_hub import signing_roots as _signing_roots

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "release" / "catalog_floor.py"
_SHIPPED_ROOTS = dict(_signing_roots.SIGNING_ROOTS)
OFFICIAL_REPO = "LavX/bazarr-provider-catalog"
FLOOR_COMMIT = "a" * 40
LATER_COMMIT = "c" * 40
PROVIDER_FILE = b"class ExampleProvider: pass\n"


def _builder():
    spec = importlib.util.spec_from_file_location("_floor_builder_for_reader_tests", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _bundle_sha(files):
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(str(len(data)).encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest(provider_id="examplehub", version="1.0.0", commit=FLOOR_COMMIT, repo=OFFICIAL_REPO,
              content=PROVIDER_FILE):
    files = {"provider.py": content}
    return {
        "schema_version": 1,
        "provider_id": provider_id,
        "name": "Example Hub Provider",
        "version": version,
        "api_version": "bazarr.provider-hub.v1",
        "entry_module": "provider",
        "entry_class": "ExampleProvider",
        "config_schema": {"type": "object", "properties": {}},
        "secret_fields": [],
        "supported_media": ["movie", "episode"],
        "languages": ["eng"],
        "files": {name: _sha(data) for name, data in files.items()},
        "bundle_sha256": _bundle_sha(files),
        "source": {
            "type": "github", "repo": repo, "ref": "main", "commit": commit,
            "path": f"providers/{provider_id}",
            "catalog_url": f"https://github.com/{repo}/blob/main/catalog.json",
            "trusted": False,
        },
        "dependencies": {"python": ">=3.12", "requirements": []},
    }


def _catalog(version="1.0.0", with_plugin=True):
    document = {"schema_version": 1, "providers": [{"manifest": _manifest(version=version)}]}
    if with_plugin:
        document["plugins"] = [{"manifest": {"provider_id": "someplugin", "version": "1.0.0"}}]
    return document


def _write_snapshot(dest, catalog=None, commit=FLOOR_COMMIT, extra=None, read_only=True):
    """A snapshot written by the real builder, so the reader sees the real layout."""
    builder = _builder()
    document = catalog if catalog is not None else _catalog()
    data = {
        "catalog.json": json.dumps(document, indent=1).encode(),
        "providers/examplehub/provider.py": PROVIDER_FILE,
    }
    data.update(extra or {})
    lock = {"lock_version": 1, "repo": OFFICIAL_REPO, "branch": "main", "commit": commit}
    builder.write_floor(lock, data, dest)
    return Path(dest)


def _unlock(root):
    for dirpath, dirs, names in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in names:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                os.chmod(path, 0o644)


@pytest.fixture(autouse=True)
def _legacy_catalog_host(monkeypatch):
    # These tests stub the legacy catalog only. They model a host that trusts no root, so the refresh
    # never asks for the signed files; the signed feed and the shipped root are covered in
    # test_provider_hub_signed_feed.py and test_provider_hub_signing.py.
    from provider_hub import signing_roots
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})


@pytest.fixture(autouse=True)
def _cleanup(tmp_path):
    yield
    _unlock(tmp_path)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    root = _write_snapshot(tmp_path / "catalog_floor")
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(root))
    return root


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from provider_hub import service
    state_file = tmp_path / "hub" / "state.json"
    state_file.parent.mkdir()
    state_file.write_text(json.dumps({"catalog_sources": {}, "installations": {}, "jobs": []}))
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))
    return service


def _offline(monkeypatch, service):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(service, "_fetch_github_catalog", refuse)


def _online(monkeypatch, service, catalog, commit):
    monkeypatch.setattr(service, "_fetch_github_catalog", lambda *a, **k: (copy.deepcopy(catalog), commit))


def _official(state):
    return state["catalog_sources"]["official"]


# ---- a host that trusts the shipped root ---------------------------------

@pytest.fixture
def shipped_root(monkeypatch):
    from provider_hub import signing_roots, transport
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", dict(_SHIPPED_ROOTS))
    # A refusal reaches the transport through the mirror pointer, which leaves a process-wide cooldown.
    transport.reset_state()
    yield
    transport.reset_state()


def _unsigned_origin(monkeypatch, commit, catalog):
    """GitHub as it is today: the heads advertise ``commit``, the catalog is the legacy file, nothing is signed."""
    from signed_feed_helpers import FeedResponse
    from test_provider_hub import _AdvertisementResponse, _git_advertisement

    def get(url, timeout=None, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement(
                [(commit, "refs/heads/main"), (commit, "refs/heads/beta")]))
        if url.endswith("/catalog.json") and "/signed/" not in url:
            return FeedResponse(json.dumps(catalog).encode("utf-8"))
        return FeedResponse(b"", 404)

    monkeypatch.setattr("provider_hub.service.requests.get", get)


def _unreachable(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr("provider_hub.service.requests.get", refuse)


def test_an_offline_first_refresh_still_seeds_from_the_floor_with_the_shipped_root(
        snapshot, hub, shipped_root, monkeypatch):
    from provider_hub.state import load_state
    _unreachable(monkeypatch)

    hub.refresh_catalog()

    source = _official(load_state())
    assert source["trust_reason"] == "release floor" and source["floor_seed"]["commit"] == FLOOR_COMMIT
    assert [entry["provider_id"] for entry in load_state()["catalog_entries"].values()] == ["examplehub"]


def test_a_refresh_with_the_shipped_root_replaces_the_seed_with_the_legacy_catalog(
        snapshot, hub, shipped_root, monkeypatch):
    from provider_hub.state import load_state
    _unreachable(monkeypatch)
    hub.refresh_catalog()
    _unsigned_origin(monkeypatch, LATER_COMMIT, _catalog(version="2.0.0", with_plugin=False))

    hub.refresh_catalog()

    state = load_state()
    source = _official(state)
    assert source["last_error"] is None and "floor_seed" not in source
    assert source["resolved_commit"] == LATER_COMMIT and source["trust_reason"] == "default branch"
    assert source["catalog_signature"] == "unsigned (legacy)"
    assert [entry["version"] for entry in state["catalog_entries"].values()] == ["2.0.0"]


def test_a_cache_from_before_the_signature_check_is_fetched_once_then_skipped_with_the_shipped_root(
        snapshot, hub, shipped_root, monkeypatch):
    from provider_hub.state import mutate_state
    calls = []
    _unsigned_origin(monkeypatch, LATER_COMMIT, _catalog(version="2.0.0", with_plugin=False))
    hub.refresh_catalog()
    # What a release without the signature check left behind.
    mutate_state(lambda state: _official(state).pop("catalog_signature", None))
    served = hub.requests.get

    def counting(url, *args, **kwargs):
        calls.append(url)
        return served(url, *args, **kwargs)

    monkeypatch.setattr("provider_hub.service.requests.get", counting)

    hub.refresh_catalog()
    assert any(url.endswith("/catalog.json") and "/signed/" not in url for url in calls)
    calls.clear()
    hub.refresh_catalog()

    # The second refresh sees the same head and asks for nothing but the advertisement.
    assert [url for url in calls if not url.endswith("/info/refs?service=git-upload-pack")] == []


# ---- reading the snapshot ------------------------------------------------

def test_load_floor_verifies_and_returns_the_snapshot(snapshot):
    from provider_hub.floor import load_floor
    floor = load_floor()
    assert floor.commit == FLOOR_COMMIT and floor.repo == OFFICIAL_REPO
    assert floor.catalog()["providers"][0]["manifest"]["provider_id"] == "examplehub"
    assert floor.read("providers/examplehub/provider.py") == PROVIDER_FILE


def test_no_snapshot_directory_means_no_floor(tmp_path, monkeypatch):
    from provider_hub.floor import load_floor
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(tmp_path / "missing"))
    assert load_floor() is None
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", "")
    assert load_floor() is None


def test_a_changed_catalog_is_refused(snapshot):
    from provider_hub.floor import FloorIntegrityError, load_floor
    _unlock(snapshot)
    (snapshot / "catalog.json").write_bytes(b'{"providers": [{"manifest": {}}]}')
    with pytest.raises(FloorIntegrityError):
        load_floor()


def test_a_changed_bundle_file_is_refused_on_read(snapshot):
    from provider_hub.floor import FloorIntegrityError, load_floor
    floor = load_floor()
    _unlock(snapshot)
    (snapshot / "providers/examplehub/provider.py").write_bytes(b"import os\n")
    with pytest.raises(FloorIntegrityError):
        floor.read("providers/examplehub/provider.py")


def test_a_file_floor_json_does_not_list_is_not_served(snapshot):
    from provider_hub.floor import FloorError, load_floor
    floor = load_floor()
    _unlock(snapshot)
    (snapshot / "providers/examplehub/extra.py").write_bytes(b"x = 1\n")
    with pytest.raises(FloorError):
        floor.read("providers/examplehub/extra.py")
    with pytest.raises(FloorError):
        floor.read("../escape.py")


def test_a_linked_file_is_not_followed(snapshot, tmp_path):
    from provider_hub.floor import FloorError, load_floor
    floor = load_floor()
    outside = tmp_path / "outside.py"
    outside.write_bytes(PROVIDER_FILE)
    _unlock(snapshot)
    target = snapshot / "providers/examplehub/provider.py"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(FloorError):
        floor.read("providers/examplehub/provider.py")


@pytest.mark.parametrize("change", [
    lambda meta: meta.update(repo="someone/else"),
    lambda meta: meta.update(commit="abc"),
    lambda meta: meta.update(lock_version=2),
    lambda meta: meta["files"].pop("catalog.json"),
    lambda meta: meta["files"].update({"../escape": "0" * 64}),
    lambda meta: meta["files"].update({"providers/x.py": "nothex"}),
])
def test_a_floor_json_that_does_not_check_out_is_refused(snapshot, change):
    from provider_hub.floor import FloorError, load_floor
    _unlock(snapshot)
    meta = json.loads((snapshot / "floor.json").read_text())
    change(meta)
    (snapshot / "floor.json").write_text(json.dumps(meta))
    with pytest.raises(FloorError):
        load_floor()


def test_the_reader_only_reads(snapshot, monkeypatch, hub):
    """Open flags stay read-only, nothing is created or removed, and the tree's
    modes and times are the same afterwards."""
    from provider_hub import floor as floor_module

    def listing():
        out = {}
        for dirpath, dirs, names in os.walk(snapshot):
            for name in [*dirs, *names]:
                path = os.path.join(dirpath, name)
                info = os.lstat(path)
                out[path] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_size)
        return out

    before = listing()
    real_open = os.open
    flags_seen = []

    def spy(path, flags, *args, **kwargs):
        flags_seen.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    for name in ("write_bytes", "write_text", "mkdir", "touch", "unlink", "rename"):
        monkeypatch.setattr(Path, name, lambda *a, _n=name, **k: pytest.fail(f"Path.{_n} called"))

    floor_module.load_floor().read("providers/examplehub/provider.py")
    assert floor_module.seed_candidate({"id": "official"}, {}) is not None
    monkeypatch.undo()
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(snapshot))

    assert flags_seen
    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    assert not any(flag & writing for flag in flags_seen)
    assert listing() == before


# ---- seeding an empty official source ------------------------------------

def test_an_offline_first_refresh_seeds_the_official_source_from_the_floor(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    _offline(monkeypatch, hub)

    hub.refresh_catalog()

    state = load_state()
    source = _official(state)
    assert source["trusted"] is True and source["trust_reason"] == "release floor"
    assert source["floor_seed"]["commit"] == FLOOR_COMMIT and source["floor_seed"]["seeded_at"]
    assert source["catalog_status"] == "bundled"
    # A seed is not a successful refresh.
    assert not source.get("resolved_commit") and source["last_checked_at"] is None
    assert source["last_error"]
    entries = list(state["catalog_entries"].values())
    assert [entry["provider_id"] for entry in entries] == ["examplehub"]
    entry = entries[0]
    assert entry["origin"] == "bundled" and entry["trusted"] is True
    assert entry["resolved_commit"] == FLOOR_COMMIT
    assert entry["manifest"]["source"]["commit"] == FLOOR_COMMIT
    assert entry["catalog_list"] == "providers"


def test_the_plugins_feed_is_never_seeded(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    _offline(monkeypatch, hub)
    hub.refresh_catalog()
    assert all(entry["catalog_list"] == "providers" for entry in load_state()["catalog_entries"].values())


def test_listing_the_catalog_offline_shows_the_floor(snapshot, hub, monkeypatch):
    _offline(monkeypatch, hub)
    listed = hub.list_catalog(auto_refresh=True)
    assert [entry["provider_id"] for entry in listed["entries"]] == ["examplehub"]
    assert listed["entries"][0]["origin"] == "bundled"


def test_a_successful_refresh_replaces_the_seed(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    _offline(monkeypatch, hub)
    hub.refresh_catalog()
    _online(monkeypatch, hub, _catalog(version="2.0.0", with_plugin=False), LATER_COMMIT)

    hub.refresh_catalog()

    state = load_state()
    source = _official(state)
    assert "floor_seed" not in source and "catalog_status" not in source
    assert source["resolved_commit"] == LATER_COMMIT and source["trust_reason"] == "default branch"
    entries = list(state["catalog_entries"].values())
    assert [entry["version"] for entry in entries] == ["2.0.0"]
    assert "origin" not in entries[0]


def test_a_fetch_at_the_floor_commit_still_replaces_the_seed(snapshot, hub, monkeypatch):
    """A release cut from the catalog's current head fetches the very commit the
    floor holds. The entries must still turn into fetched ones."""
    from provider_hub.state import load_state
    _offline(monkeypatch, hub)
    hub.refresh_catalog()
    _online(monkeypatch, hub, _catalog(with_plugin=False), FLOOR_COMMIT)

    hub.refresh_catalog()

    state = load_state()
    assert "floor_seed" not in _official(state)
    assert all("origin" not in entry for entry in state["catalog_entries"].values())
    assert _official(state)["trust_reason"] == "default branch"


def test_a_refreshed_catalog_is_never_replaced_by_the_floor(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    _online(monkeypatch, hub, _catalog(version="2.0.0", with_plugin=False), LATER_COMMIT)
    hub.refresh_catalog()
    _offline(monkeypatch, hub)

    hub.refresh_catalog()

    state = load_state()
    source = _official(state)
    assert "floor_seed" not in source
    assert source["resolved_commit"] == LATER_COMMIT and source["trust_reason"] == "default branch"
    assert [entry["version"] for entry in state["catalog_entries"].values()] == ["2.0.0"]
    assert source["last_error"]


def test_a_source_that_checked_once_is_not_seeded(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state, mutate_state
    mutate_state(lambda state: _official(state).update(last_checked_at="2026-11-01T00:00:00+00:00"))
    _offline(monkeypatch, hub)

    hub.refresh_catalog()

    state = load_state()
    assert state["catalog_entries"] == {}
    assert "floor_seed" not in _official(state)


def test_stored_fetched_entries_block_the_seed(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state, mutate_state
    kept = {"source": "official", "provider_id": "kept", "version": "1.0.0", "manifest": {}}
    mutate_state(lambda state: state["catalog_entries"].update({"official:providers:kept:1.0.0": kept}))
    _offline(monkeypatch, hub)

    hub.refresh_catalog()

    state = load_state()
    assert list(state["catalog_entries"]) == ["official:providers:kept:1.0.0"]
    assert "floor_seed" not in _official(state)


def test_a_new_image_floor_replaces_an_older_seed(tmp_path, snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    _offline(monkeypatch, hub)
    hub.refresh_catalog()
    newer = _write_snapshot(
        tmp_path / "newer_floor", catalog=_catalog(version="1.1.0"), commit=LATER_COMMIT)
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(newer))

    hub.refresh_catalog()

    state = load_state()
    assert _official(state)["floor_seed"]["commit"] == LATER_COMMIT
    assert [entry["version"] for entry in state["catalog_entries"].values()] == ["1.1.0"]


def test_a_floor_that_fails_its_checks_seeds_nothing(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    _unlock(snapshot)
    (snapshot / "catalog.json").write_bytes(b'{"providers": [{"manifest": {}}]}')
    _offline(monkeypatch, hub)

    hub.refresh_catalog()

    state = load_state()
    assert state["catalog_entries"] == {}
    assert _official(state)["last_error"]
    assert "floor_seed" not in _official(state)


def test_a_user_source_never_gets_the_floor(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state, mutate_state
    community = {
        "id": "community", "name": "community", "type": "github",
        "url": "https://github.com/example/providers/blob/main/catalog.json",
        "enabled": True,
    }
    mutate_state(lambda state: state["catalog_sources"].update({"community": community}))

    def fetch(url, *args, **kwargs):
        if "example/providers" in url:
            raise requests.ConnectionError("down")
        return _catalog(with_plugin=False), LATER_COMMIT

    monkeypatch.setattr(hub, "_fetch_github_catalog", fetch)

    hub.refresh_catalog()

    state = load_state()
    assert "floor_seed" not in state["catalog_sources"]["community"]
    assert not any(entry.get("source") == "community" for entry in state["catalog_entries"].values())
    assert all("origin" not in entry for entry in state["catalog_entries"].values())


def test_an_official_source_on_a_dev_ref_is_not_seeded(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    hub.update_catalog_source("official", dev_ref="feature/x")
    _offline(monkeypatch, hub)

    hub.refresh_catalog()

    state = load_state()
    assert state["catalog_entries"] == {}
    assert "floor_seed" not in _official(state)


def test_the_floor_reason_does_not_survive_on_a_dev_ref_source(snapshot, hub, monkeypatch):
    """The keep-the-earlier-verdict branch of the trust update (no answer from
    GitHub) must never carry the floor's reason over to a dev_ref source."""
    from provider_hub import service
    from provider_hub.state import RELEASE_FLOOR_TRUST_REASON

    assert RELEASE_FLOOR_TRUST_REASON not in service._TRUST_REASONS

    class NoAnswer:
        def check(self, commit):
            return None, "no answer"

    source = {"dev_ref": "feature/x", "trusted": True, "trust_reason": RELEASE_FLOOR_TRUST_REASON}
    service._set_official_trust(source, FLOOR_COMMIT, FLOOR_COMMIT, NoAnswer())
    assert source["trusted"] is False


def test_the_floor_reason_is_dropped_from_a_dev_ref_source_in_stored_state(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state, mutate_state
    _offline(monkeypatch, hub)
    hub.refresh_catalog()
    assert _official(load_state())["trusted"] is True

    hub.update_catalog_source("official", dev_ref="feature/x")

    state = load_state()
    assert _official(state)["trusted"] is False
    assert all(entry["trusted"] is False for entry in state["catalog_entries"].values())

    # A reason with no seed marker is not honoured either.
    hub.update_catalog_source("official", dev_ref=None)
    mutate_state(lambda s: _official(s).update(
        trusted=True, trust_reason="release floor", floor_seed=None))
    assert _official(load_state())["trusted"] is False


def test_the_marker_only_touches_official_entries(snapshot):
    from provider_hub.floor import mark_seeded
    entries = {
        "a": {"source": "official"},
        "b": {"source": "community"},
    }
    source = {}
    mark_seeded(source, entries, {"a", "b"}, FLOOR_COMMIT, "now")
    assert entries["a"]["origin"] == "bundled" and "origin" not in entries["b"]
    assert source["floor_seed"] == {"commit": FLOOR_COMMIT, "seeded_at": "now"}


# ---- updates -------------------------------------------------------------

def _update_state(origin):
    entry = {
        "source": "official", "source_name": "Official Bazarr Provider Catalog",
        "provider_id": "examplehub", "version": "1.0.0", "trusted": True,
        "manifest": _manifest(), "catalog_list": "providers",
    }
    if origin:
        entry["origin"] = origin
    return {
        "catalog_sources": {"official": {"id": "official", "name": "Official Bazarr Provider Catalog",
                                         "trusted": True, "official": True}},
        "catalog_entries": {"official:providers:examplehub:1.0.0": entry},
        "installations": {},
    }


def test_a_bundled_entry_is_never_offered_as_an_update(snapshot, hub):
    assert hub._latest_catalog_manifest(
        _update_state("bundled"), "examplehub", "0.9.0", "official", "providers") is None


def test_a_bundled_entry_this_host_cannot_run_is_not_reported_as_refused(snapshot, hub, monkeypatch):
    """Without the origin check the entry would be reported as a refused newer
    version: the host lacks its runtime, so only the origin keeps it quiet."""
    from provider_hub import compatibility
    monkeypatch.setattr(compatibility, "AVAILABLE_RUNTIMES", frozenset())

    assert hub._refused_update_reason(
        _update_state(None), "examplehub", "0.9.0", "official", "providers") is not None
    assert hub._refused_update_reason(
        _update_state("bundled"), "examplehub", "0.9.0", "official", "providers") is None


def test_a_fetched_entry_is_offered_as_an_update(snapshot, hub):
    latest = hub._latest_catalog_manifest(
        _update_state(None), "examplehub", "0.9.0", "official", "providers")
    assert latest and latest["version"] == "1.0.0"


# ---- installing from the floor ------------------------------------------

def _validated(**kwargs):
    from provider_hub.manifest import validate_manifest
    return validate_manifest(_manifest(**kwargs))


def _no_network(monkeypatch, hub):
    def blocked(*args, **kwargs):
        raise AssertionError("the network was used")
    monkeypatch.setattr(hub.requests, "get", blocked)


def test_a_bundle_at_the_floor_commit_installs_from_the_floor(snapshot, hub, monkeypatch):
    _no_network(monkeypatch, hub)
    manifest = _validated()

    path = hub._fetch_bundle(manifest)

    assert (path / "provider.py").read_bytes() == PROVIDER_FILE


def test_a_bundle_at_another_commit_still_uses_the_network(snapshot, hub, monkeypatch):
    calls = []

    class Response:
        content = PROVIDER_FILE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(hub.requests, "get", lambda url, **k: calls.append(url) or Response())

    hub._fetch_bundle(_validated(commit=LATER_COMMIT))

    assert calls and LATER_COMMIT in calls[0]


@pytest.mark.parametrize("damage", ["missing", "directory", "oversized"])
def test_a_floor_file_that_cannot_be_read_falls_back_to_the_network(snapshot, hub, monkeypatch, damage):
    """floor.json lists the file but it cannot be served: that is not an
    integrity failure, so the reader answers None and the fetch goes on."""
    from provider_hub import floor as floor_module
    from provider_hub.floor import bundle_file, load_floor
    load_floor()  # cache the snapshot so the size limit below only applies to the bundle file
    _unlock(snapshot)
    target = snapshot / "providers/examplehub/provider.py"
    target.unlink()
    if damage == "directory":
        target.mkdir()
    elif damage == "oversized":
        target.write_bytes(b"x" * 1024)
        monkeypatch.setattr(floor_module, "MAX_FILE_BYTES", 16)
    manifest = _validated()

    assert bundle_file(manifest, "provider.py") is None

    class Response:
        content = PROVIDER_FILE

        def raise_for_status(self):
            pass

    calls = []
    monkeypatch.setattr(hub.requests, "get", lambda url, **k: calls.append(url) or Response())
    assert hub._fetch_github_bundle_file(manifest, "provider.py") == PROVIDER_FILE
    assert calls


def test_a_foreign_repository_never_reads_the_floor(snapshot, hub, monkeypatch):
    from provider_hub.floor import bundle_file
    assert bundle_file(_validated(repo="someone/else"), "provider.py") is None


def test_a_tampered_floor_file_fails_the_install(snapshot, hub, monkeypatch):
    from provider_hub.bundle import BundleValidationError
    _no_network(monkeypatch, hub)
    _unlock(snapshot)
    (snapshot / "providers/examplehub/provider.py").write_bytes(b"import os\n")

    with pytest.raises(BundleValidationError):
        hub._fetch_bundle(_validated())


def test_floor_bytes_must_still_match_the_manifest_pins(snapshot, hub, monkeypatch):
    """The floor's own hashes agree with its files, but the manifest being
    installed pins different bytes: verify_bundle_tree refuses it."""
    from provider_hub.bundle import BundleValidationError
    _no_network(monkeypatch, hub)

    with pytest.raises(BundleValidationError):
        hub._fetch_bundle(_validated(content=b"class Other: pass\n"))


def test_a_floor_seeded_entry_installs_without_network(snapshot, hub, monkeypatch):
    from provider_hub.state import load_state
    from provider_hub.manifest import validate_manifest
    _offline(monkeypatch, hub)
    _no_network(monkeypatch, hub)
    hub.refresh_catalog()
    entry = next(iter(load_state()["catalog_entries"].values()))

    path = hub._fetch_bundle(validate_manifest(entry["manifest"]))

    assert (path / "provider.py").read_bytes() == PROVIDER_FILE


def test_an_offline_install_stages_a_bundle_from_the_floor(tmp_path, snapshot, hub, monkeypatch):
    """Refresh falls back to the floor, then the install path (binding, refusal
    checks, staging) runs with the network blocked. Environment creation and
    the worker smoke check are mocked, so this proves staging from the floor,
    not a full install."""
    from provider_hub.state import load_state

    _offline(monkeypatch, hub)
    _no_network(monkeypatch, hub)
    hub.refresh_catalog()

    class FakeEnvironment:
        def __init__(self, root):
            self.root = Path(root)

        def install(self, validated, timeout=None):
            env_path = self.root / "envs" / validated.provider_id / validated.version / "test"
            python_path = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("", encoding="utf-8")
            return env_path

    monkeypatch.setattr(hub, "PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(hub, "_smoke_validate_worker", lambda manifest, bundle_path, python_path: None)
    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled",
                        lambda provider_id, enabled: True)

    hub.install_catalog_entry("official", "examplehub", "1.0.0")

    row = load_state()["installations"]["examplehub"]
    assert row["state"] == "staged" and row["staged_version"] == "1.0.0"
    assert row["source_id"] == "official" and row["source_bound"] and row["trusted"] is True
    assert row["origin"] == "catalog"
    bundle = Path(row["staged_path"])
    assert (bundle / "provider.py").read_bytes() == PROVIDER_FILE


# ---- the signed feed in the snapshot -------------------------------------

SERVICE_ID = "floorsvc"
SERVICE_FILE = b"class Service: pass\n"
SIGNED_FILES = ("signed/main/catalog.json", "signed/main/catalog.json.sig", "keys/catalog-signing-keys.json")


def _service_manifest():
    from plugin_platform_helpers import service_manifest
    manifest = service_manifest(SERVICE_ID, "1.0.0", file_payloads={"service.py": SERVICE_FILE})
    manifest["source"] = {**manifest["source"], "commit": FLOOR_COMMIT, "path": f"plugins/{SERVICE_ID}"}
    return manifest


def _signed_files(keys, *, source_commit=FLOOR_COMMIT, channel="main", providers=None, plugins=None, revocations=None):
    """The three signed files the lock pins, signed with the throwaway ``keys``."""
    from datetime import datetime, timedelta, timezone

    from test_provider_hub_signing import envelope_for, stamp

    now = datetime.now(timezone.utc).replace(microsecond=0)
    body = json.dumps({
        "schema_version": 2, "name": "Official", "channel": channel, "signed_version": 100,
        # An expired window on purpose: the baked check does not look at it.
        "not_before": stamp(now - timedelta(days=200)), "expires": stamp(now - timedelta(days=100)),
        "source_commit": source_commit,
        "providers": [{"manifest": _manifest()}] if providers is None else providers,
        "plugins": [{"manifest": _service_manifest()}] if plugins is None else plugins,
        **({"revocations": revocations} if revocations is not None else {}),
    }, sort_keys=True).encode()
    return {
        SIGNED_FILES[0]: body,
        SIGNED_FILES[1]: json.dumps(envelope_for(body, keys.ci)).encode(),
        SIGNED_FILES[2]: json.dumps(keys.key_file()).encode(),
    }


@pytest.fixture
def keys(monkeypatch):
    """Throwaway keys, and a host that trusts only their root."""
    from test_provider_hub_signing import Keys
    from provider_hub import signing_roots, transport
    made = Keys()
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", dict(made.roots))
    transport.reset_state()
    yield made
    transport.reset_state()


def _signed_snapshot(tmp_path, monkeypatch, signed, *, drop=(), commit=FLOOR_COMMIT):
    extra = {path: data for path, data in signed.items() if path not in drop}
    extra[f"plugins/{SERVICE_ID}/service.py"] = SERVICE_FILE
    root = _write_snapshot(tmp_path / "catalog_floor", commit=commit, extra=extra)
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(root))
    return root


def _tamper(data):
    raw = bytearray(data)
    raw[len(raw) // 2] ^= 0x01
    return bytes(raw)


def _seeded(source=None, entries=None):
    from provider_hub import floor as floor_module
    return floor_module.seed_candidate(source or {"id": "official"}, entries or {})


def test_a_fixture_snapshot_with_a_valid_signed_feed_seeds_plugins(tmp_path, monkeypatch, keys):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))

    catalog, commit = _seeded()

    assert commit == FLOOR_COMMIT
    assert [item["manifest"]["provider_id"] for item in catalog["providers"]] == ["examplehub"]
    assert [item["manifest"]["id"] for item in catalog["plugins"]] == [SERVICE_ID]


def test_a_signed_feed_seeds_through_a_refresh_and_never_latches_the_channel(
        tmp_path, monkeypatch, hub, keys, service_kind):
    from provider_hub import catalog_feed
    from provider_hub.state import load_state
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    _unreachable(monkeypatch)

    hub.refresh_catalog()

    state = load_state()
    source = _official(state)
    assert source["trust_reason"] == "release floor" and source["catalog_status"] == "bundled"
    assert source["floor_seed"]["commit"] == FLOOR_COMMIT
    entries = {entry["provider_id"]: entry for entry in state["catalog_entries"].values()}
    assert set(entries) == {"examplehub", SERVICE_ID}
    assert entries[SERVICE_ID]["catalog_list"] == "plugins" and entries[SERVICE_ID]["origin"] == "bundled"
    assert entries[SERVICE_ID]["resolved_commit"] == FLOOR_COMMIT
    assert entries[SERVICE_ID]["manifest"]["source"]["commit"] == FLOOR_COMMIT
    # A baked catalog is not a fetched one: nothing latched, no counter stored, no signature claimed.
    assert state.get("catalog_feeds", {}) == {}
    assert not catalog_feed.counter_file().exists()
    assert catalog_feed.stored_record(state, "main") == catalog_feed.ChannelRecord()
    assert source.get("catalog_signature") != catalog_feed.SIGNATURE_VERIFIED
    assert source.get("signature_verified") in (None, False)
    assert not source.get("resolved_commit") and source["last_checked_at"] is None


def test_baked_seeding_never_raises_a_stored_counter(tmp_path, monkeypatch, hub, keys, service_kind):
    from provider_hub import catalog_feed
    from provider_hub.state import load_state
    # The snapshot's signed_version is 100. A host that holds a higher counter keeps it, and one that
    # holds none stays without one.
    catalog_feed.write_counter_file({"main": catalog_feed.ChannelRecord(500, 1)})
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    _unreachable(monkeypatch)

    hub.refresh_catalog()

    assert catalog_feed.read_counter_file()["main"] == catalog_feed.ChannelRecord(500, 1)
    assert load_state().get("catalog_feeds", {}) == {}


def test_the_baked_check_is_the_one_that_never_advances_state(tmp_path, monkeypatch, keys):
    from provider_hub import catalog_feed
    signed = _signed_files(keys)

    result = catalog_feed.verify_baked(
        signed[SIGNED_FILES[0]], signed[SIGNED_FILES[1]], signed[SIGNED_FILES[2]], channel="main")

    assert result.ok and result.baked and not result.may_advance_state


def _providers_only(catalog):
    assert [item["manifest"]["provider_id"] for item in catalog["providers"]] == ["examplehub"]
    assert "plugins" not in catalog


def test_a_tampered_signature_seeds_providers_only(tmp_path, monkeypatch, keys, caplog):
    signed = _signed_files(keys)
    signed[SIGNED_FILES[1]] = _tamper(signed[SIGNED_FILES[1]])
    _signed_snapshot(tmp_path, monkeypatch, signed)

    with caplog.at_level("WARNING"):
        catalog, commit = _seeded()

    _providers_only(catalog)
    assert commit == FLOOR_COMMIT
    assert "seeds providers only" in caplog.text


def test_a_tampered_signed_catalog_seeds_providers_only(tmp_path, monkeypatch, keys):
    signed = _signed_files(keys)
    signed[SIGNED_FILES[0]] = _tamper(signed[SIGNED_FILES[0]])
    _signed_snapshot(tmp_path, monkeypatch, signed)

    _providers_only(_seeded()[0])


def test_a_wrong_root_seeds_providers_only(tmp_path, monkeypatch, keys):
    from test_provider_hub_signing import Keys
    from provider_hub import signing_roots
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", dict(Keys().roots))

    _providers_only(_seeded()[0])


def test_a_build_with_no_root_seeds_providers_only(tmp_path, monkeypatch, keys):
    from provider_hub import signing_roots
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", {})

    _providers_only(_seeded()[0])


def test_a_missing_key_file_seeds_providers_only(tmp_path, monkeypatch, keys, caplog):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys), drop=(SIGNED_FILES[2],))

    with caplog.at_level("WARNING"):
        catalog, _commit = _seeded()

    _providers_only(catalog)
    assert "seeds providers only" in caplog.text


def test_a_missing_signature_seeds_providers_only(tmp_path, monkeypatch, keys):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys), drop=(SIGNED_FILES[1],))

    _providers_only(_seeded()[0])


def test_a_snapshot_with_no_signed_files_seeds_providers_only_and_says_so(tmp_path, monkeypatch, keys, caplog):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys), drop=SIGNED_FILES)

    with caplog.at_level("INFO"):
        catalog, _commit = _seeded()

    _providers_only(catalog)
    assert "carries no signed feed" in caplog.text


def test_a_signed_feed_for_another_channel_seeds_providers_only(tmp_path, monkeypatch, keys):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys, channel="beta"))

    _providers_only(_seeded()[0])


def test_a_signed_feed_built_from_another_commit_seeds_providers_only(tmp_path, monkeypatch, keys, caplog):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys, source_commit=LATER_COMMIT))

    with caplog.at_level("WARNING"):
        catalog, _commit = _seeded()

    _providers_only(catalog)
    assert "different commit" in caplog.text


def test_a_signed_file_changed_after_the_build_seeds_providers_only(tmp_path, monkeypatch, keys, caplog):
    root = _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    _unlock(root)
    path = root / SIGNED_FILES[0]
    path.write_bytes(_tamper(path.read_bytes()))

    with caplog.at_level("WARNING"):
        catalog, _commit = _seeded()

    _providers_only(catalog)
    assert "does not match floor.json" in caplog.text


def test_a_tampered_signature_seeds_providers_only_through_a_refresh(tmp_path, monkeypatch, hub, keys, service_kind):
    from provider_hub.state import load_state
    signed = _signed_files(keys)
    signed[SIGNED_FILES[1]] = _tamper(signed[SIGNED_FILES[1]])
    _signed_snapshot(tmp_path, monkeypatch, signed)
    _unreachable(monkeypatch)

    hub.refresh_catalog()

    entries = list(load_state()["catalog_entries"].values())
    assert [entry["provider_id"] for entry in entries] == ["examplehub"]
    assert all(entry["catalog_list"] == "providers" for entry in entries)


def test_the_signed_feed_never_leaks_an_unsigned_plugins_list(tmp_path, monkeypatch, keys):
    # The legacy catalog.json in this snapshot carries a plugins list (see _catalog); it is never seeded.
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys), drop=SIGNED_FILES)
    from provider_hub import floor as floor_module
    assert "plugins" in floor_module.load_floor().catalog()

    _providers_only(_seeded()[0])


def test_a_plugin_bundle_installs_from_the_snapshot(tmp_path, monkeypatch, keys):
    from provider_hub import floor as floor_module
    from provider_hub.manifest import validate_plugin_bundle
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    manifest = _service_manifest()

    validated = validate_plugin_bundle(manifest)

    assert floor_module.bundle_file(validated, "service.py") == SERVICE_FILE


def test_swapping_the_trusted_roots_between_loads_verifies_the_feed_again(tmp_path, monkeypatch, keys):
    from test_provider_hub_signing import Keys
    from provider_hub import floor as floor_module
    from provider_hub import signing_roots
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys))
    first = floor_module.load_floor()
    assert first.signed_catalog() is not None
    assert [item["manifest"]["id"] for item in _seeded()[0]["plugins"]] == [SERVICE_ID]

    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", dict(Keys().roots))

    second = floor_module.load_floor()
    assert second is not first and second.signed_catalog() is None
    _providers_only(_seeded()[0])

    monkeypatch.setattr(signing_roots, "SIGNING_ROOTS", dict(keys.roots))
    assert floor_module.load_floor().signed_catalog() is not None


def test_a_signature_without_the_signed_catalog_seeds_providers_only(tmp_path, monkeypatch, keys, caplog):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys), drop=(SIGNED_FILES[0],))

    with caplog.at_level("WARNING"):
        catalog, _commit = _seeded()

    _providers_only(catalog)
    assert "its signature is there but the catalog is not" in caplog.text


def test_a_signed_catalog_with_no_providers_seeds_providers_only(tmp_path, monkeypatch, keys, caplog):
    _signed_snapshot(tmp_path, monkeypatch, _signed_files(keys, providers=[]))

    with caplog.at_level("WARNING"):
        catalog, _commit = _seeded()

    _providers_only(catalog)
    assert "no providers list" in caplog.text


@pytest.fixture
def register_seed_translator():
    from provider_hub.kinds import TRANSLATOR, kind_for_api, unregister_kind
    from provider_hub.kinds.translator import TRANSLATOR_KIND_API, register_translator_kind

    already_registered = kind_for_api(TRANSLATOR_KIND_API) is not None
    register_translator_kind()
    yield
    if not already_registered:
        unregister_kind(TRANSLATOR)


def _build_runtime_snapshot(
    tmp_path, monkeypatch, keys, *, tag="py3-none-any", bad_bytes=False, missing_provider_wheel=False,
    google_fields=None, unsigned=False, revocations=None,
):
    from provider_hub import seed
    from plugin_platform_helpers import service_manifest
    from test_provider_hub_bundled_wheels import _make_wheel

    wheel = _make_wheel(tmp_path / "wheels", "seedfixture", "1.0", tag=tag)
    wheel_name = f"wheels/{wheel.name}"
    plugin_files = {
        "translator.py": (
            b"from bazarr_plugin_sdk.translator.v1 import TranslatorPlugin\n"
            b"from seedfixture import VALUE\n\n"
            b"class FixtureTranslator(TranslatorPlugin):\n"
            b"    def describe(self):\n"
            b"        return {'name': VALUE}\n\n"
            b"    async def test(self, config, secrets, ctx=None):\n"
            b"        return {'ok': True, 'message': VALUE}\n"
        ),
        wheel_name: wheel.read_bytes(),
    }
    google = service_manifest(
        seed.GOOGLE_PLUGIN_ID,
        kind="translator",
        kind_api="bazarr.translator.v1",
        entry_module="translator",
        entry_class="FixtureTranslator",
        file_payloads=plugin_files,
        dependencies={"requirements": [{"name": "seedfixture", "version": "1.0",
                                        "hashes": ["sha256:" + _sha(plugin_files[wheel_name])]}]},
        source={"type": "github", "repo": OFFICIAL_REPO, "ref": "main", "commit": FLOOR_COMMIT,
                "path": "plugins/google_translate"},
        **(google_fields or {}),
    )
    provider_manifest = _manifest()
    if missing_provider_wheel:
        provider_manifest["dependencies"]["requirements"] = [
            {"name": "missingfixture", "version": "1.0", "hashes": ["sha256:" + "0" * 64]}
        ]
    providers = [{"manifest": provider_manifest}, {"manifest": _manifest(provider_id="exampleother")}]
    signed = (_signed_files(keys, providers=providers, plugins=[{"manifest": google}, {"manifest": _service_manifest()}],
                            revocations=revocations) if not unsigned else {})
    data = {
        "catalog.json": json.dumps({"schema_version": 1, "providers": providers}).encode(),
        "providers/examplehub/provider.py": PROVIDER_FILE,
        "providers/exampleother/provider.py": PROVIDER_FILE,
        **signed,
        **{f"plugins/google_translate/{name}": content for name, content in plugin_files.items()},
    }
    if bad_bytes:
        data[f"plugins/google_translate/{wheel_name}"] = _tamper(data[f"plugins/google_translate/{wheel_name}"])
    lock = {"lock_version": 1, "repo": OFFICIAL_REPO, "branch": "main", "commit": FLOOR_COMMIT}
    root = tmp_path / "catalog_floor"
    builder = _builder()
    builder.write_floor(lock, data, root, build_runtime=lambda path: seed.build(
        path, repo=OFFICIAL_REPO, commit=FLOOR_COMMIT, catalog=data["catalog.json"],
        signed_catalog=signed.get(SIGNED_FILES[0]), files=data, offline=True))
    builder.check_modes(root)
    monkeypatch.setenv("BAZARR_CATALOG_FLOOR", str(root))
    return root


@pytest.fixture
def runtime_snapshot(tmp_path, monkeypatch, keys, register_seed_translator):
    return _build_runtime_snapshot(tmp_path, monkeypatch, keys)


def test_runtime_index_uses_exact_catalog_bytes_and_shares_environments(runtime_snapshot, tmp_path):
    from provider_hub import floor, seed

    seed.check_image_interpreter(runtime_snapshot)
    snapshot = floor.load_floor(runtime_snapshot)
    index = json.loads(snapshot.read(seed.INDEX_FILE))
    expected = {"examplehub", "exampleother", seed.GOOGLE_PLUGIN_ID}
    assert {entry["manifest"].get("provider_id", entry["manifest"].get("id")) for entry in index["releases"]} == expected
    assert index["source_commit"] == FLOOR_COMMIT
    assert len(index["environments"]) == 2
    first = seed.select_release(tmp_path / "hub", "examplehub")
    other = seed.select_release(tmp_path / "hub", "exampleother")
    google = seed.select_release(tmp_path / "hub", seed.GOOGLE_PLUGIN_ID)
    assert first.interpreter == other.interpreter
    assert first.interpreter != google.interpreter
    assert {entry["bundle_sha256"] for entry in index["releases"]} == {first.manifest.bundle_sha256,
                                                                    google.manifest.bundle_sha256}
    assert seed.select_release(tmp_path / "hub", SERVICE_ID) is None


@pytest.mark.parametrize(("version", "config_wins"), [("0.9.0", False), ("1.0.0", True), ("1.1.0", True)])
def test_runtime_seed_picks_the_highest_valid_release_and_config_wins_a_tie(runtime_snapshot, tmp_path, version, config_wins):
    import sys

    from provider_hub import seed

    root = tmp_path / "hub"
    config_manifest = _validated(version=version)
    bundle = root / "bundles" / "examplehub" / version / FLOOR_COMMIT
    bundle.mkdir(parents=True)
    (bundle / "provider.py").write_bytes(PROVIDER_FILE)
    interpreter = root / "shared_envs" / "fixture" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable).resolve())
    (interpreter.parent.parent / 'pyvenv.cfg').write_text(f'version = {sys.version.split()[0]}\n')
    candidate = (config_manifest, str(bundle), str(interpreter))

    chosen = seed.select_release(root, "examplehub", config_candidate=candidate)
    assert chosen.seeded is not config_wins
    assert chosen.manifest.version == (version if config_wins else "1.0.0")

    (bundle / "provider.py").write_bytes(b"not the bundle we validated")
    assert seed.select_release(root, "examplehub", config_candidate=candidate).seeded


def test_tampered_signature_with_cached_seed_releases_leaves_providers_only(runtime_snapshot, tmp_path):
    from provider_hub import seed

    root = tmp_path / "hub"
    assert seed.select_release(root, seed.GOOGLE_PLUGIN_ID).seeded
    existing = runtime_snapshot / SIGNED_FILES[1]
    existing.chmod(0o644)
    existing.write_bytes(_tamper(existing.read_bytes()))
    existing.chmod(0o444)

    assert seed.select_release(root, seed.GOOGLE_PLUGIN_ID) is None
    assert seed.select_release(root, "examplehub").seeded
    assert "plugins" not in _seeded()[0]


def test_wrong_runtime_architecture_ignores_the_seed(runtime_snapshot, tmp_path, monkeypatch, caplog):
    from provider_hub import seed

    monkeypatch.setattr(seed.platform, "machine", lambda: "different-architecture")
    with pytest.raises(seed.SeedError, match="another Python interpreter or architecture"):
        seed.check_image_interpreter(runtime_snapshot)
    assert seed.select_release(tmp_path / "hub", "examplehub") is None
    assert "another Python interpreter or architecture" in caplog.text


def test_build_rejects_modified_runtime_wheel(tmp_path, monkeypatch, keys):
    from provider_hub.bundle import BundleValidationError

    with pytest.raises(BundleValidationError, match="SHA256 mismatch for wheels/"):
        _build_runtime_snapshot(tmp_path, monkeypatch, keys, bad_bytes=True)


def test_signed_build_rejects_a_wheel_for_another_architecture(tmp_path, monkeypatch, keys):
    from provider_hub.seed import SeedError

    with pytest.raises(SeedError, match="could not install wheels for google_translate"):
        _build_runtime_snapshot(tmp_path, monkeypatch, keys, tag="cp38-cp38-win32")


@pytest.mark.parametrize("damage", ["bundle", "wheel", "environment"])
def test_seeded_worker_resolves_verified_paths_again_each_time(runtime_snapshot, tmp_path, damage):
    from types import SimpleNamespace

    from provider_hub import seed
    from provider_hub.sandbox.policy import PolicyError
    from subtitles.tools.translate.runtime.worker_binding import TranslatorWorkers

    root = tmp_path / "hub"
    google = seed.select_release(root, seed.GOOGLE_PLUGIN_ID)
    installation = SimpleNamespace(active_path=str(google.bundle), python_path=str(google.interpreter),
                                   manifest=google.manifest.raw)
    definitions = TranslatorWorkers(
        installation_for=lambda _plugin: installation, hub_dir=lambda: root,
        config_dir=lambda: str(tmp_path / "config"), reserved_ports=lambda: (),
    )
    spec = definitions._spec(seed.GOOGLE_PLUGIN_ID, {443})
    assert spec.interpreter == str(google.interpreter)
    assert spec.cwd == str(google.bundle)
    assert (root / "run" / seed.GOOGLE_PLUGIN_ID / "data").is_dir()
    if damage == "bundle":
        target = google.bundle / "translator.py"
    elif damage == "wheel":
        target, = (google.bundle / "wheels").glob("*.whl")
    else:
        target, = google.interpreter.parent.parent.glob("lib/python*/site-packages/seedfixture/__init__.py")
    mode = stat.S_IMODE(target.stat().st_mode)
    target.chmod(mode | 0o200)
    target.write_bytes(_tamper(target.read_bytes()))
    target.chmod(mode)
    with pytest.raises(PolicyError, match="no release that can run"):
        definitions._spec(seed.GOOGLE_PLUGIN_ID, {443})


@pytest.fixture
def google_startup(monkeypatch, schema_session):
    import sys
    from types import SimpleNamespace

    from app.config import settings
    from subtitles.tools.translate import runtime
    from subtitles.tools.translate.runtime import startup, store

    class Pointers:
        engine = fallback = None

        def get(self):
            return self.engine, self.fallback

        def set(self, engine, fallback):
            self.engine, self.fallback = engine, fallback
            return True

    pointers = Pointers()
    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr(settings.translator, "translator_type", "google_translate")
    monkeypatch.setattr(settings.general, "provider_hub_auto_install", False)
    monkeypatch.setattr(runtime, "_runtime_tables_exist", lambda: True)
    monkeypatch.setattr(store, "ConfigPointers", lambda: pointers)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(database=schema_session))
    return startup.start_google_from_seed, store.TranslatorStore(schema_session)


def test_google_first_start_launches_the_seeded_translator_worker(
    runtime_snapshot, tmp_path, hub, google_startup, monkeypatch,
):
    from provider_hub import env_registry, seed
    from provider_hub.runtime.pool import WorkerPool
    from subtitles.tools.translate.runtime import worker_binding, workers

    config = tmp_path / "config"
    root = hub.provider_hub_dir()
    start, store = google_startup
    source_paths = {p.relative_to(runtime_snapshot): (p.stat().st_mode, p.stat().st_mtime_ns)
                    for p in runtime_snapshot.rglob("*")}

    def fail_install(*args, **kwargs):
        pytest.fail("looking up an image seed should not run the installer")

    monkeypatch.setattr(hub.PluginEnvironment, "install", fail_install)
    monkeypatch.setattr(hub, "refresh_catalog", fail_install)
    _no_network(monkeypatch, hub)
    assert start()
    row, = hub.load_state()["installations"].values()
    assert row["provider_id"] == seed.GOOGLE_PLUGIN_ID
    assert row["state"] == "active" and row["origin"] == "catalog"
    assert Path(row["active_path"]).is_relative_to(runtime_snapshot)
    instance, = store.list_instances(seed.GOOGLE_PLUGIN_ID)
    assert instance.enabled and store.pointers() == (instance.id, None)
    assert hub.reconcile_plugin_environments()["needs_repair"] == []
    assert env_registry.cleanup_environments(root) == []
    assert not (root / "bundles").exists()
    assert not (root / "shared_envs").exists()

    definitions = worker_binding.TranslatorWorkers(
        hub_dir=lambda: root, config_dir=lambda: str(config), reserved_ports=lambda: (),
        store_factory=lambda: type("Store", (), {"list_instances": lambda self: []})(),
    )
    pool = WorkerPool(autostart=True)
    try:
        port = workers.PoolWorkers(pool, definitions.definition_for, one_call_for=definitions.one_call_for,
                                   ports_for=definitions.ports_for)
        response = port.call(seed.GOOGLE_PLUGIN_ID, workers.TEST_OP, {"config": {}}, {}, timeout=20)
        assert response.result == {"ok": True, "message": "seedfixture"}
    finally:
        pool.close()
    assert {p.relative_to(runtime_snapshot): (p.stat().st_mode, p.stat().st_mtime_ns)
            for p in runtime_snapshot.rglob("*")} == source_paths
    store.set_engine(None)
    assert not start()
    assert store.pointers() == (None, None)


def test_seeded_provider_proxy_reverifies_its_runtime_before_respawn(runtime_snapshot, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from provider_hub import registry, seed
    from provider_hub.worker import WorkerError

    root = tmp_path / "hub"
    release = seed.select_release(root, "examplehub")
    installation = SimpleNamespace(active_path=str(release.bundle), python_path=str(release.interpreter))
    monkeypatch.setattr(registry, "provider_hub_dir", lambda: root)
    provider = registry._make_provider_class(release.manifest, installation=installation)()
    provider._verify_bundle_before_start()

    target = release.bundle / "provider.py"
    target.chmod(0o644)
    target.write_bytes(b"tampered")
    target.chmod(0o444)
    with pytest.raises(WorkerError, match="catalog runtime seed no longer verifies"):
        provider._verify_bundle_before_start()


def test_signed_build_refuses_a_missing_google_release(tmp_path, keys):
    from provider_hub import seed

    signed = _signed_files(keys)
    with pytest.raises(seed.SeedError, match="no usable google_translate release"):
        seed.build(tmp_path, repo=OFFICIAL_REPO, commit=FLOOR_COMMIT, catalog=b"",
                   signed_catalog=signed[SIGNED_FILES[0]], files={}, offline=True)


def test_signed_build_refuses_an_unavailable_legacy_provider_wheel(tmp_path, monkeypatch, keys):
    from provider_hub.seed import SeedError

    with pytest.raises(SeedError, match="could not install wheels for examplehub"):
        _build_runtime_snapshot(tmp_path, monkeypatch, keys, missing_provider_wheel=True)
    assert not (tmp_path / "catalog_floor" / "floor.json").exists()


@pytest.fixture
def provider_sandbox_probes(monkeypatch, schema_session):
    """Exercise the normal profile with an empty, migrated media root inventory."""
    from app import database as app_database
    from provider_hub import sandbox

    monkeypatch.setattr(app_database, "database", schema_session)
    host = sandbox.probe_sandbox()
    real_spawn = sandbox.spawn_worker
    confined = host.enforces(sandbox.PROVIDER_MIN_ABI)
    if not confined and not host.absent:
        pytest.skip(f"Host provider sandbox probe failed: {host.reason}")
    probes, launches = [], []

    def probe(*_args, **_kwargs):
        probes.append(True)
        return host

    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        launches.append(process.pid)
        return process

    monkeypatch.setattr(sandbox, "provider_profile_enabled", lambda: True)
    monkeypatch.setattr(sandbox, "probe_sandbox", probe)
    monkeypatch.setattr(sandbox, "spawn_worker", spawn)
    return probes, launches, confined


def test_marketplace_installs_and_tests_a_signed_seeded_provider(
    runtime_snapshot, hub, monkeypatch, provider_sandbox_probes,
):
    _unreachable(monkeypatch)
    _no_network(monkeypatch, hub)
    hub.refresh_catalog()
    monkeypatch.setattr(hub.PluginEnvironment, "install", lambda *_args, **_kw: pytest.fail("unexpected pip"))
    monkeypatch.setattr("provider_hub.kinds.subtitle_provider.set_bazarr_provider_enabled", lambda *_: True)

    hub.install_catalog_entry("official", "examplehub", "1.0.0")
    assert hub.activate_staged_installations() == ["examplehub"]
    root = hub.provider_hub_dir()
    row = hub.load_state()["installations"]["examplehub"]
    assert Path(row["active_path"]).is_relative_to(runtime_snapshot)
    assert not (root / "bundles").exists() and not (root / "shared_envs").exists()
    result = hub.test_provider_connection("examplehub")
    assert result["status"] == "ready" and result["details"]["initialized"] is True
    probes, launches, confined = provider_sandbox_probes
    assert len(probes) == 3  # install smoke, activation smoke and Test Connection
    assert len(launches) == (3 if confined else 0)
    print(f"Provider launches through verified Landlock: {len(launches)}")

    provider_file = Path(row["active_path"]) / "provider.py"
    provider_file.chmod(0o644)
    provider_file.write_bytes(b"raise RuntimeError('tampered code must never load')\n")
    provider_file.chmod(0o444)
    monkeypatch.setattr(hub, "ProviderWorkerClient", lambda *_args, **_kw: pytest.fail("seed started after damage"))
    result = hub.test_provider_connection("examplehub")
    assert result["status"] == "failed" and "could not be verified" in result["message"]


def test_opt_in_provider_activates_after_the_real_application_migration(
    runtime_snapshot, hub, tmp_path, monkeypatch, provider_sandbox_probes,
):
    from alembic.migration import MigrationContext
    from app import database as db
    from app.config import settings
    from app.startup import prepare_database_and_plugins
    from flask import Flask
    from provider_hub import registry, releases, sandbox, state
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import scoped_session, sessionmaker
    from subliminal_patch import extensions

    _unreachable(monkeypatch)
    _no_network(monkeypatch, hub)
    monkeypatch.delenv("BAZARR_DISABLE_PROVIDER_AUTOINSTALL", raising=False)
    monkeypatch.delenv("BAZARR_PROVIDER_HUB_STATE", raising=False)
    state_snapshot = tmp_path / "database-hub" / "state.json"
    monkeypatch.setattr(state, "state_file", lambda: state_snapshot)
    monkeypatch.setattr(settings.general, "provider_hub_auto_install", True)
    monkeypatch.setattr(settings.general, "enabled_providers", ["exampleother"])
    monkeypatch.setattr(settings.general, "disabled_providers", ["embeddedsubtitles"])
    monkeypatch.setattr(settings.general, "provider_priorities", {"exampleother": 60})
    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr("app.config.write_config", lambda: True)
    monkeypatch.setattr(hub.PluginEnvironment, "install", lambda *_args, **_kw: pytest.fail("unexpected pip"))
    original_registry = extensions.provider_registry
    provider_registry = extensions.ProviderRegistry()
    for name in original_registry.names():
        if name != "exampleother":
            provider_registry.register(name, original_registry[name])
    monkeypatch.setattr(extensions, "provider_registry", provider_registry)
    monkeypatch.setattr(registry, "provider_registry", provider_registry)
    monkeypatch.setattr(hub, "provider_registry", provider_registry)
    monkeypatch.setattr(registry, "_REGISTERED_PROVIDER_HUB_IDS", set())
    monkeypatch.setattr(registry, "_REGISTRATION_INCOMPLETE", set())
    monkeypatch.setattr(registry, "_WITHDRAWN_PROVIDER_IDS", set())

    url = f"sqlite:///{tmp_path / 'boot.db'}"
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(db, "url", url)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "database", session)
    monkeypatch.setenv("BAZARR_DB_MIGRATION_VERSION", "fixture-unmigrated")
    checked = []
    real_probe = sandbox.probe_sandbox

    def check_migration(*args, **kwargs):
        with engine.connect() as connection:
            checked.append(MigrationContext.configure(connection).get_current_heads())
        assert checked[-1] and checked[-1] == (os.environ["BAZARR_DB_MIGRATION_VERSION"],)
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(sandbox, "probe_sandbox", check_migration)
    try:
        db.init_db()
        with engine.connect() as connection:
            assert not MigrationContext.configure(connection).get_current_heads()
        assert not state_snapshot.exists()
        assert prepare_database_and_plugins(Flask(__name__))
        assert len(checked) == 2  # install smoke and activation smoke
        _, launches, confined = provider_sandbox_probes
        assert len(launches) == (2 if confined else 0)
        print(f"Provider startup launches after migrations through verified Landlock: {len(launches)}")
        row, = hub.load_state()["installations"].values()
        assert row["state"] == "active" and Path(row["active_path"]).is_relative_to(runtime_snapshot)
        with engine.connect() as connection:
            release = connection.execute(select(releases.releases_table).where(
                releases.releases_table.c.plugin_id == "exampleother",
            )).mappings().one()
        assert release["state"] == "active" and release["bundle_path"] == row["active_path"]
        assert state_snapshot.is_file()
        assert "exampleother" in provider_registry.names()
        assert settings.general.enabled_providers == ["exampleother"]
        assert settings.general.disabled_providers == ["embeddedsubtitles"]
        assert settings.general.provider_priorities == {"exampleother": 60}
    finally:
        session.remove()
        engine.dispose()


def test_invalid_runtime_seed_marketplace_falls_back_to_the_verified_floor_copy(
    runtime_snapshot, hub, monkeypatch, provider_sandbox_probes,
):
    from provider_hub import seed

    root = hub.provider_hub_dir()
    seeded = seed.select_release(root, "examplehub")
    altered = seeded.bundle / "provider.py"
    altered.chmod(0o644)
    altered.write_bytes(b"raise RuntimeError('tampered code must never load')\n")
    altered.chmod(0o444)
    _unreachable(monkeypatch)
    _no_network(monkeypatch, hub)
    hub.refresh_catalog()
    monkeypatch.setattr(hub.provider_kind, "set_bazarr_provider_enabled", lambda *_: True)

    hub.install_catalog_entry("official", "examplehub", "1.0.0")
    assert hub.activate_staged_installations() == ["examplehub"]
    row = hub.load_state()["installations"]["examplehub"]
    bundle = Path(row["active_path"])
    assert bundle.is_relative_to(root)
    assert (bundle / "provider.py").read_bytes() == PROVIDER_FILE
    assert hub.test_provider_connection("examplehub")["status"] == "ready"


def test_google_first_start_ignores_an_invalid_signature(runtime_snapshot, hub, google_startup):
    start, store = google_startup
    target = runtime_snapshot / SIGNED_FILES[1]
    target.chmod(0o644)
    target.write_bytes(_tamper(target.read_bytes()))
    target.chmod(0o444)

    assert not start()
    assert hub.load_state()["installations"] == {}
    assert store.pointers() == (None, None)


@pytest.mark.parametrize("field", ["pyvenv.cfg", "requirements.txt"])
def test_invalid_config_environment_metadata_falls_back_to_seed(runtime_snapshot, tmp_path, field):
    import sys

    from provider_hub import seed

    root = tmp_path / "hub"
    release = seed.select_release(root, "examplehub")
    bundle = root / "bundles" / "examplehub" / release.manifest.version / FLOOR_COMMIT
    bundle.mkdir(parents=True)
    (bundle / "provider.py").write_bytes(PROVIDER_FILE)
    env = root / "shared_envs" / "fixture"
    python = env / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    (env / "pyvenv.cfg").write_text(f"version = {sys.version.split()[0]}\n")
    (env / field).write_bytes(b"\xff")

    chosen = seed.select_release(root, "examplehub", config_candidate=(release.manifest, str(bundle), str(python)))
    assert chosen == release


@pytest.mark.parametrize("fields", [{"platforms": ["windows-x86_64"]}, {"python_tags": ["cp310"]}])
def test_signed_seed_requires_google_to_support_the_build_target(tmp_path, monkeypatch, keys, fields):
    from provider_hub.seed import SeedError

    with pytest.raises(SeedError, match="no usable google_translate release"):
        _build_runtime_snapshot(tmp_path, monkeypatch, keys, google_fields=fields)


def test_signed_seed_checks_google_against_the_stamped_image_version(tmp_path, monkeypatch, keys):
    from provider_hub import seed

    monkeypatch.delenv("BAZARR_VERSION", raising=False)
    root = _build_runtime_snapshot(tmp_path, monkeypatch, keys, google_fields={"min_host_version": "2.8.0"})
    seed.check_image_interpreter(root, "2.8.0")
    with pytest.raises(seed.SeedError, match="no google_translate release compatible"):
        seed.check_image_interpreter(root, "2.7.1")


@pytest.mark.parametrize("mode", ["disabled", "prior-fallback", "source-scoped"])
def test_invalid_seed_worker_cannot_reach_the_unsandboxed_launcher(runtime_snapshot, hub, tmp_path, monkeypatch, mode):
    from provider_hub import seed
    from provider_hub.provider_roots import SourceMediaFile
    from provider_hub.worker import ProviderWorkerClient, SourceMediaRefused, WorkerError, worker_command

    root = hub.provider_hub_dir()
    release = seed.select_release(root, "examplehub")
    env = {
        "BAZARR_PROVIDER_HUB_BUNDLE": str(release.bundle),
        "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(release.manifest.raw),
    }
    runner = ROOT / "bazarr" / "provider_hub" / "worker_runner.py"
    env["BAZARR_PROVIDER_HUB_MANIFEST"] = "{}"
    monkeypatch.setattr("provider_hub.sandbox.provider_profile_enabled", lambda: mode != "disabled")
    monkeypatch.setattr("provider_hub.worker.subprocess.Popen",
                        lambda *_args, **_kwargs: pytest.fail("unverified seed reached the direct launcher"))
    worker = ProviderWorkerClient(worker_command(release.interpreter, runner), cwd=release.bundle, env=env)
    worker._sandbox_failed_open = mode != "disabled"
    refusal = "seeded provider bundle and interpreter could not be verified"
    if mode == "source-scoped":
        media = tmp_path / "media"
        media.mkdir()
        path = media / "movie.mkv"
        path.write_bytes(b"fixture movie")
        info = path.stat()
        source_file = SourceMediaFile(path, (info.st_dev, info.st_ino), media)
        with pytest.raises(SourceMediaRefused, match=refusal):
            worker.request("search", source_media=lambda: source_file)
    else:
        with pytest.raises(WorkerError, match=refusal):
            worker.start()
    assert worker.process is None


def test_unsigned_wheel_failure_leaves_the_provider_in_the_catalog(tmp_path, monkeypatch, keys, caplog):
    from provider_hub import seed

    _build_runtime_snapshot(tmp_path, monkeypatch, keys, unsigned=True, missing_provider_wheel=True)
    assert {item["manifest"]["provider_id"] for item in _seeded()[0]["providers"]} == {"examplehub", "exampleother"}
    assert seed.select_release(tmp_path / "hub", "examplehub") is None
    assert seed.select_release(tmp_path / "hub", "exampleother").seeded
    assert "no runnable wheel closure for: examplehub" in caplog.text


def test_signed_revocation_refuses_seed_selection_and_marketplace_install(
    tmp_path, monkeypatch, keys, register_seed_translator, hub,
):
    from provider_hub import seed

    _build_runtime_snapshot(tmp_path, monkeypatch, keys, revocations=[{"id": "examplehub", "version": "1.0.0"}])
    _unreachable(monkeypatch)
    hub.refresh_catalog()
    assert seed.select_release(tmp_path / "hub", "examplehub") is None
    with pytest.raises(hub.ProviderHubInstallError, match="revoked by the official catalog"):
        hub.install_catalog_entry("official", "examplehub", "1.0.0")


def test_a_cached_bundled_google_entry_cannot_install_after_signature_damage(runtime_snapshot, hub, monkeypatch):
    from provider_hub import seed

    _unreachable(monkeypatch)
    hub.refresh_catalog()
    source = runtime_snapshot / SIGNED_FILES[1]
    source.chmod(0o644)
    source.write_bytes(_tamper(source.read_bytes()))
    source.chmod(0o444)
    assert {entry["provider_id"] for entry in hub.list_catalog()["entries"]} == {"examplehub", "exampleother"}
    monkeypatch.setattr(hub, "_fetch_bundle", lambda *_a, **_k: pytest.fail("cannot trust the cached seed manifest"))
    with pytest.raises(hub.ProviderHubInstallError, match="bundled plugin catalog no longer verifies"):
        hub.stage_install(
            next(entry["manifest"] for entry in hub.load_state()["catalog_entries"].values()
                 if entry["provider_id"] == seed.GOOGLE_PLUGIN_ID),
            source_id="official",
        )


def test_google_first_start_prefers_an_existing_config_release_on_a_tie(
    runtime_snapshot, hub, google_startup, monkeypatch,
):
    from provider_hub import seed

    _unreachable(monkeypatch)
    _no_network(monkeypatch, hub)
    root = hub.provider_hub_dir()
    previous = runtime_snapshot / "without-runtime"
    runtime = runtime_snapshot / seed.RUNTIME_DIR
    runtime_snapshot.chmod(0o755)
    runtime.rename(previous)
    runtime_snapshot.chmod(0o555)
    try:
        # The raw floor remains signed and usable. In the absence of the derived
        # runtime this first install stages the official bundle in config.
        hub.refresh_catalog()
        digest = hub.install_consent("official", seed.GOOGLE_PLUGIN_ID, "1.0.0")["digest"]
        staged = hub.install_catalog_entry("official", seed.GOOGLE_PLUGIN_ID, "1.0.0", consent_digest=digest)
        assert Path(staged["staged_path"]).is_relative_to(root)
        assert hub.activate_staged_installations() == [seed.GOOGLE_PLUGIN_ID]
    finally:
        runtime_snapshot.chmod(0o755)
        previous.rename(runtime)
        runtime_snapshot.chmod(0o555)

    monkeypatch.setattr(hub.PluginEnvironment, "install", lambda *_a, **_k: pytest.fail("unexpected pip"))
    start, store = google_startup
    assert start()
    row = hub.load_state()["installations"][seed.GOOGLE_PLUGIN_ID]
    assert row["active_path"] == staged["staged_path"]
    instance, = store.list_instances(seed.GOOGLE_PLUGIN_ID)
    assert store.pointers() == (instance.id, None)


@pytest.mark.parametrize("reason", ["host_version", "signed_revocation"])
def test_google_startup_and_worker_reject_a_config_release_before_ranking_it(
    reason, tmp_path, monkeypatch, keys, register_seed_translator, hub, google_startup,
):
    from provider_hub import revocation, seed
    from subtitles.tools.translate.runtime.worker_binding import TranslatorWorkers

    fields = {"version": "2.0.0"}
    if reason == "host_version":
        fields["min_host_version"] = "3.0.0"
    monkeypatch.setenv("BAZARR_VERSION", "3.0.0")
    old = _build_runtime_snapshot(tmp_path / "installed-image", monkeypatch, keys, google_fields=fields)
    # Install into config through the regular signed Marketplace pathway.
    old.chmod(0o755)
    (old / seed.RUNTIME_DIR).rename(old / "without-runtime")
    old.chmod(0o555)
    _unreachable(monkeypatch)
    _no_network(monkeypatch, hub)
    hub.refresh_catalog()
    digest = hub.install_consent("official", seed.GOOGLE_PLUGIN_ID, "2.0.0")["digest"]
    hub.install_catalog_entry("official", seed.GOOGLE_PLUGIN_ID, "2.0.0", consent_digest=digest)
    assert hub.activate_staged_installations() == [seed.GOOGLE_PLUGIN_ID]
    root = hub.provider_hub_dir()
    active = hub.load_state()["installations"][seed.GOOGLE_PLUGIN_ID]
    assert active["active_version"] == "2.0.0" and Path(active["active_path"]).is_relative_to(root)
    definitions = TranslatorWorkers(hub_dir=lambda: root)
    assert definitions.resolve(seed.GOOGLE_PLUGIN_ID) is not None

    monkeypatch.setenv("BAZARR_VERSION", "2.8.0")
    revoked = [{"id": seed.GOOGLE_PLUGIN_ID, "version": "2.0.0"}] if reason == "signed_revocation" else None
    current = _build_runtime_snapshot(tmp_path / "boot-image", monkeypatch, keys, revocations=revoked)
    assert revocation.stored_lists(hub.load_state()) == {}
    assert definitions.resolve(seed.GOOGLE_PLUGIN_ID) is None

    monkeypatch.setattr(hub.PluginEnvironment, "install", lambda *_a, **_k: pytest.fail("unexpected pip"))
    start, store = google_startup
    assert start()
    row = hub.load_state()["installations"][seed.GOOGLE_PLUGIN_ID]
    assert row["active_version"] == "1.0.0" and Path(row["active_path"]).is_relative_to(current)
    instance, = store.list_instances(seed.GOOGLE_PLUGIN_ID)
    assert store.pointers() == (instance.id, None)
    assert revocation.stored_lists(hub.load_state()) == {}
    assert definitions.resolve(seed.GOOGLE_PLUGIN_ID).bundle == Path(row["active_path"])


def test_a_failed_probe_refuses_even_a_verified_seed_instead_of_claiming_landlock_is_absent(
    runtime_snapshot, hub, monkeypatch,
):
    from provider_hub import sandbox, seed
    from provider_hub.worker import ProviderWorkerClient, WorkerError, worker_command

    release = seed.select_release(hub.provider_hub_dir(), "examplehub")
    path = ROOT / "bazarr" / "provider_hub" / "worker_runner.py"
    client = ProviderWorkerClient(worker_command(release.interpreter, path), cwd=release.bundle, env={
        "BAZARR_PROVIDER_HUB_BUNDLE": str(release.bundle),
        "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps(release.manifest.raw),
    })
    failed = sandbox.SandboxProbe(abi=11, verified=False, absent=False, reason="fixture canary could not verify")
    monkeypatch.setattr(sandbox, "provider_profile_enabled", lambda: True)
    monkeypatch.setattr(sandbox, "probe_sandbox", lambda *_a, **_k: failed)
    with pytest.raises(WorkerError, match="the sandbox could not be confirmed"):
        client.start()
    assert client.process is None


def test_runtime_checks_keep_host_supported_config_service_pairs_available(tmp_path, monkeypatch):
    from plugin_platform_helpers import oci_runtime, service_manifest
    from provider_hub import seed
    from provider_hub.compatibility import HostFacts

    root = tmp_path / "hub"
    bundle = root / "bundles" / "configservice" / "1.0.0"
    bundle.mkdir(parents=True)
    interpreter = root / "shared_envs" / "fixture" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    manifest = service_manifest("configservice", **oci_runtime())
    host = HostFacts("2.8.0", "linux-x86_64", frozenset({("oci", "daemon")}), in_container=True)
    monkeypatch.setattr(seed, "current_host", lambda: host)
    assert seed.resolve_paths(root, manifest, bundle, interpreter) == (bundle, interpreter)


@pytest.mark.parametrize("denial", ["host_version", "baked_revocation", None])
def test_reconciliation_applies_release_policy_before_smoke_testing_shared_config_env(
    denial, hub, tmp_path, monkeypatch, keys,
):
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub.manifest import validate_manifest
    from provider_hub.state import save_state
    from provider_hub.venv import environment_fingerprint

    monkeypatch.setenv("BAZARR_VERSION", "2.8.0")
    revoked = [{"id": "examplehub", "version": "1.0.0"}] if denial == "baked_revocation" else None
    _build_runtime_snapshot(tmp_path, monkeypatch, keys, revocations=revoked)
    raw = provider_manifest_v2("examplehub", file_payloads={"provider.py": PROVIDER_FILE})
    if denial == "host_version":
        raw["min_host_version"] = "3.0.0"
    root = hub.provider_hub_dir()
    original_python = root / "envs" / "examplehub" / "1.0.0" / "legacy" / "bin" / "python"
    shared_python = root / "shared_envs" / "fixture" / "bin" / "python"
    for interpreter in (original_python, shared_python):
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
    bundle = root / "bundles" / "examplehub" / "1.0.0" / FLOOR_COMMIT
    bundle.mkdir(parents=True)
    (bundle / "provider.py").write_bytes(PROVIDER_FILE)
    state = hub.load_state()
    state["installations"]["examplehub"] = {
        "provider_id": "examplehub", "state": "active", "active_version": "1.0.0",
        "manifest": raw, "active_path": str(bundle), "python_path": str(original_python),
    }
    key = environment_fingerprint(validate_manifest(raw))
    state["plugin_envs"] = {key: {"path": str(shared_python.parent.parent)}}
    save_state(state)
    probes = []
    monkeypatch.setattr(hub, "_smoke_validate_worker", lambda *_args: probes.append(True))

    assert hub.reconcile_plugin_environments()["repointed"] == ([] if denial else ["examplehub"])
    assert probes == ([] if denial else [True])
    if denial == "host_version":
        result = hub.test_provider_connection("examplehub")
        assert not result["ok"] and "needs Bazarr+ 3.0.0" in result["message"]
        assert not hub.load_state()["installations"]["examplehub"].get("needs_repair")


@pytest.mark.parametrize("enabled", [True, False])
def test_refused_first_activation_preserves_owner_settings_for_a_retired_provider(
    enabled, hub, monkeypatch,
):
    from app.config import settings
    from plugin_platform_helpers import provider_manifest_v2
    from provider_hub import registry
    from provider_hub.manifest import validate_manifest
    from provider_hub.startup import start_after_migrations
    from provider_hub.state import save_state

    provider_id = "argenteamdump"
    monkeypatch.setenv("BAZARR_DISABLE_PROVIDER_AUTOINSTALL", "true")
    monkeypatch.setenv("BAZARR_VERSION", "2.8.0")
    monkeypatch.setattr(settings.general, "enabled_providers", [provider_id] if enabled else [])
    monkeypatch.setattr(settings.general, "disabled_providers", [] if enabled else [provider_id])
    monkeypatch.setattr(settings.general, "provider_priorities", {provider_id: 70})
    saved_section = {"username": "fixture-owner"}
    monkeypatch.setattr(settings, provider_id, saved_section, raising=False)
    monkeypatch.setattr(registry, "_REGISTRATION_INCOMPLETE", set())
    monkeypatch.setattr("app.config.write_config", lambda: True)
    removed = []
    monkeypatch.setattr("app.config.remove_settings_section", removed.append)
    monkeypatch.setattr(hub, "_smoke_validate_worker", lambda *_a: pytest.fail("incompatible provider was started"))

    raw = provider_manifest_v2(provider_id, min_host_version="3.0.0")
    root = hub.provider_hub_dir()
    bundle = root / "bundles" / provider_id / "1.0.0" / FLOOR_COMMIT
    interpreter = root / "shared_envs" / "fixture" / "bin" / "python"
    installation = hub._staged_installation(
        validate_manifest(raw), {}, bundle, interpreter, True, source_id="official", catalog_list="plugins",
    )
    state = hub.load_state()
    state["installations"][provider_id] = installation
    save_state(state)

    assert start_after_migrations()
    failed = hub.load_state()["installations"][provider_id]
    assert failed["state"] == "failed" and not failed.get("active_version")
    assert "needs Bazarr+ 3.0.0" in failed["last_error"]
    assert settings.general.enabled_providers == ([provider_id] if enabled else [])
    assert settings.general.disabled_providers == ([] if enabled else [provider_id])
    assert settings.general.provider_priorities == {provider_id: 70}
    assert getattr(settings, provider_id) == saved_section and provider_id not in removed

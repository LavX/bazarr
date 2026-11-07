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

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "release" / "catalog_floor.py"
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
    assert "floor_seed" not in source
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

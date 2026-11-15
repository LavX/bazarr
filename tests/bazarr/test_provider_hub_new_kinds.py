# coding=utf-8
"""A plugin of a kind other than the subtitle provider comes from the official catalog only."""
import pytest

from plugin_platform_helpers import provider_manifest_v2, service_manifest, verified_official_source, write_state
from signed_feed_helpers import SignedWorld
from test_provider_hub import _AdvertisementResponse, _FakeResponse, _git_advertisement

PLUGIN = "newkindsvc"
REASON = "Only the official catalog can install this kind of plugin."


def _plugin(version="1.0.0"):
    return service_manifest(PLUGIN, version)


def _community_source():
    return {"id": "community", "name": "community", "type": "github", "enabled": True, "trusted": False,
            "url": "https://github.com/example/providers/blob/main/catalog.json", "resolved_commit": "c" * 40}


def _entry(source, manifest, catalog_list="plugins", trusted=None):
    return {"source": source, "source_name": source, "provider_id": manifest.get("id") or manifest["provider_id"],
            "name": manifest["name"], "version": manifest["version"],
            "trusted": source == "official" if trusted is None else trusted,
            "manifest": manifest, "catalog_list": catalog_list}


def _state(tmp_path, monkeypatch, **collections):
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", **collections)))


@pytest.fixture
def community_feed(monkeypatch):
    """A community catalog that lists a plugin of a new kind, beside an official catalog that lists nothing."""

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("d" * 40, "refs/heads/main")]))
        if "LavX/bazarr-provider-catalog" in url:
            return _FakeResponse({"providers": []})
        return _FakeResponse({"providers": [], "plugins": [{"manifest": _plugin()},
                                                           {"manifest": provider_manifest_v2("commsub")}]})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)


def test_a_community_catalog_cannot_store_a_plugin_of_a_new_kind(tmp_path, monkeypatch, service_kind, community_feed):
    from provider_hub.service import add_catalog_source, list_jobs, refresh_catalog
    from provider_hub.state import load_state

    _state(tmp_path, monkeypatch)
    add_catalog_source("community", "https://github.com/example/providers/blob/main/catalog.json")
    refresh_catalog()

    # The new kind is rejected with its reason, and the subtitle provider in the same list is kept.
    assert [entry["provider_id"] for entry in load_state()["catalog_entries"].values()] == ["commsub"]
    rejected = list_jobs()[-1]["details"]["rejected_plugins"]
    assert [(record["provider_id"], record["reason"]) for record in rejected] == [(PLUGIN, REASON)]


def test_the_official_catalog_stores_a_plugin_of_a_new_kind(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    _state(tmp_path, monkeypatch)
    SignedWorld(monkeypatch).publish(plugins=[_plugin()])

    refresh_catalog()

    assert [entry["provider_id"] for entry in load_state()["catalog_entries"].values()] == [PLUGIN]


def test_a_legacy_official_catalog_cannot_carry_a_plugin_of_a_new_kind(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import refresh_catalog
    from provider_hub.state import load_state

    _state(tmp_path, monkeypatch)
    world = SignedWorld(monkeypatch)
    world.legacy(plugins=[_plugin()])

    refresh_catalog()

    assert load_state()["catalog_entries"] == {}


def test_an_untrusted_official_source_cannot_store_one_either(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import list_jobs, refresh_catalog
    from provider_hub.state import load_state

    world = SignedWorld(monkeypatch)
    world.publish(plugins=[_plugin()])
    _state(tmp_path, monkeypatch)
    # A dev_ref on a commit that is not on main or beta leaves the official source untrusted.
    monkeypatch.setattr("provider_hub.service._set_official_trust",
                        lambda source, *args: source.update(trusted=False, trust_reason="outside main and beta"))

    refresh_catalog()

    assert load_state()["catalog_entries"] == {}
    assert [record["reason"] for record in list_jobs()[-1]["details"]["rejected_plugins"]] == [REASON]


def test_an_entry_a_community_source_holds_is_blocked_and_cannot_be_installed(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, list_catalog, resolve_catalog_install

    entry = _entry("community", _plugin(), trusted=False)
    _state(tmp_path, monkeypatch, catalog_sources={"community": _community_source()},
           catalog_entries={f"community:plugins:{PLUGIN}:1.0.0": entry})

    blocked = {item["provider_id"]: item.get("blocked_reason") for item in list_catalog()["entries"]}
    assert blocked == {PLUGIN: REASON}
    with pytest.raises(ProviderHubInstallError, match="Only the official catalog"):
        resolve_catalog_install("community", PLUGIN, "1.0.0", catalog_list="plugins")


def test_a_manifest_no_trusted_entry_holds_cannot_be_staged_as_a_new_kind(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import ProviderHubInstallError, stage_install

    _state(tmp_path, monkeypatch, catalog_sources={"official": verified_official_source()})

    with pytest.raises(ProviderHubInstallError, match="Only the official catalog"):
        stage_install(_plugin())


def test_an_installed_plugin_bound_to_a_community_source_is_offered_no_update(tmp_path, monkeypatch, service_kind):
    from provider_hub.service import _latest_catalog_manifest
    from provider_hub.state import load_state

    _state(tmp_path, monkeypatch, catalog_sources={"community": _community_source()},
           catalog_entries={f"community:plugins:{PLUGIN}:2.0.0": _entry("community", _plugin("2.0.0"), trusted=False)})

    assert _latest_catalog_manifest(load_state(), PLUGIN, "1.0.0", "community", "plugins",
                                    installed_kind="test_service") is None


def test_the_official_entry_of_a_new_kind_installs_and_a_subtitle_provider_still_comes_from_anywhere(
        tmp_path, monkeypatch, service_kind):
    from provider_hub.service import resolve_catalog_install

    sub = provider_manifest_v2("commsub")
    _state(tmp_path, monkeypatch,
           catalog_sources={"official": verified_official_source(), "community": _community_source()},
           catalog_entries={f"official:plugins:{PLUGIN}:1.0.0": _entry("official", _plugin()),
                            "community:plugins:commsub:1.0.0": _entry("community", sub, trusted=False)})

    assert resolve_catalog_install("official", PLUGIN, "1.0.0", catalog_list="plugins")["provider_id"] == PLUGIN
    assert resolve_catalog_install("community", "commsub", "1.0.0", catalog_list="plugins")["provider_id"] == "commsub"

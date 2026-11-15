# coding=utf-8
"""The development install path: one named community source may carry plugins of a new kind, untrusted."""
from types import SimpleNamespace

import pytest

from plugin_platform_helpers import service_manifest, write_state
from test_provider_hub import _AdvertisementResponse, _FakeResponse, _git_advertisement

PLUGIN = "devtranslator"
REASON = "Only the official catalog can install this kind of plugin."
SOURCE = "branch-catalog"
SOURCE_URL = "https://github.com/example/providers/blob/main/catalog.json"


def _plugin(version="1.0.0"):
    return service_manifest(PLUGIN, version)


@pytest.fixture
def served(monkeypatch):
    """A community catalog on a branch that lists a plugin of a new kind, and an official one that lists nothing."""

    def fake_get(url, timeout, **_kwargs):
        if url.endswith("/info/refs?service=git-upload-pack"):
            return _AdvertisementResponse(_git_advertisement([("d" * 40, "refs/heads/main")]))
        if "LavX/bazarr-provider-catalog" in url:
            return _FakeResponse({"providers": []})
        return _FakeResponse({"providers": [], "plugins": [{"manifest": _plugin()}]})

    monkeypatch.setattr("provider_hub.service.requests.get", fake_get)


@pytest.fixture
def hub(tmp_path, monkeypatch, service_kind, served):
    from provider_hub.service import add_catalog_source

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json")))
    monkeypatch.delenv("BAZARR_DEV_PLUGIN_SOURCE", raising=False)
    add_catalog_source(SOURCE, SOURCE_URL)
    add_catalog_source("another", "https://github.com/example/other/blob/main/catalog.json")
    return SimpleNamespace(tmp_path=tmp_path)


def _refresh():
    from provider_hub.service import refresh_catalog

    refresh_catalog()


def _stored():
    from provider_hub.state import load_state

    return {entry["source"]: entry for entry in load_state()["catalog_entries"].values()}


def test_with_the_variable_unset_the_source_is_refused_like_any_community_source(hub):
    from provider_hub.service import list_jobs

    _refresh()

    assert _stored() == {}
    rejected = list_jobs()[-1]["details"]["rejected_plugins"]
    assert {record["reason"] for record in rejected} == {REASON}


def test_with_the_variable_set_only_the_named_source_may_carry_a_new_kind(hub, monkeypatch):
    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)

    _refresh()

    entries = _stored()
    assert set(entries) == {SOURCE}
    assert entries[SOURCE]["trusted"] is False and entries[SOURCE]["provider_id"] == PLUGIN


def test_the_entry_and_the_source_are_marked_as_development(hub, monkeypatch):
    from provider_hub.service import list_catalog

    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)
    _refresh()

    catalog = list_catalog()
    assert [(e["provider_id"], e["development_source"]) for e in catalog["entries"]] == [(PLUGIN, True)]
    flags = {source["id"]: source["development_source"] for source in catalog["sources"]}
    assert flags == {"official": False, SOURCE: True, "another": False}
    assert all(not e.get("blocked_reason") for e in catalog["entries"])


def test_the_official_source_is_untouched_whether_or_not_the_variable_is_set(hub, monkeypatch):
    from provider_hub.state import load_state

    _refresh()
    unset = {key: value for key, value in load_state()["catalog_sources"]["official"].items()
             if key not in ("last_attempted_at", "last_checked_at")}
    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)
    _refresh()
    state = load_state()
    official = {key: value for key, value in state["catalog_sources"]["official"].items()
                if key not in ("last_attempted_at", "last_checked_at")}

    assert official == unset
    # No latch and no counter, which only the official source has.
    assert not state.get("catalog_feeds")
    assert not (hub.tmp_path / "plugins" / "feed-counter.json").exists()


def test_the_official_source_cannot_be_named_as_the_development_source(hub, monkeypatch):
    from provider_hub import dev_source

    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", "official")

    assert dev_source.is_dev_source("official", {"official": True}) is False
    assert dev_source.is_dev_source("lookalike", {"name": "lookalike", "official": True}) is False
    _refresh()
    assert _stored() == {}


def _digest():
    from provider_hub.service import install_consent

    return install_consent(SOURCE, PLUGIN, "1.0.0", catalog_list="plugins")["digest"]


@pytest.fixture
def staging(hub, monkeypatch):
    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})
    from provider_hub import service

    tmp_path = hub.tmp_path

    class FakeEnvironment:
        def __init__(self, root):
            self.root = root

        def install(self, validated, timeout=None):
            (tmp_path / "env" / "bin").mkdir(parents=True, exist_ok=True)
            (tmp_path / "env" / "bin" / "python").write_text("", encoding="utf-8")
            return tmp_path / "env"

    monkeypatch.setattr(service, "_fetch_bundle", lambda manifest, deadline=None: tmp_path / "bundle")
    monkeypatch.setattr(service, "PluginEnvironment", FakeEnvironment)
    monkeypatch.setattr(service, "python_executable", lambda env_path: tmp_path / "python")


def test_an_install_from_the_development_source_is_untrusted_and_badged(staging, monkeypatch):
    from provider_hub.service import install_catalog_entry, list_installations

    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)
    _refresh()

    row = install_catalog_entry(SOURCE, PLUGIN, "1.0.0", consent_digest=_digest())

    assert row["state"] == "staged" and row["trusted"] is False
    [listed] = list_installations()
    assert listed["trusted"] is False and listed["source_id"] == SOURCE
    assert listed["development_source"] is True and listed["source_name"] == SOURCE


def test_after_the_variable_is_unset_the_install_stays_and_gets_no_update(staging, monkeypatch):
    from provider_hub.service import _latest_catalog_manifest, install_catalog_entry, list_installations
    from provider_hub.state import load_state

    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)
    _refresh()
    install_catalog_entry(SOURCE, PLUGIN, "1.0.0", consent_digest=_digest())
    monkeypatch.delenv("BAZARR_DEV_PLUGIN_SOURCE")

    [listed] = list_installations()
    assert listed["development_source"] is False and listed["trusted"] is False
    assert _latest_catalog_manifest(load_state(), PLUGIN, "0.9.0", SOURCE, "plugins",
                                    installed_kind="test_service") is None


def test_the_consent_summary_names_the_development_source(hub, monkeypatch):
    from provider_hub.service import install_consent

    monkeypatch.setattr("provider_hub.consent.sandbox_status", lambda: {})
    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)
    _refresh()

    [member] = install_consent(SOURCE, PLUGIN, "1.0.0", catalog_list="plugins")["members"]

    assert member["source"]["development"] is True
    assert member["source"]["official"] is False and member["source"]["trusted"] is False


def test_the_status_page_reports_the_source_while_it_is_set(monkeypatch):
    from api.system import status

    monkeypatch.setenv("BAZARR_VERSION", "2.8.0")
    monkeypatch.setattr(status, "get_sonarr_info", SimpleNamespace(version=lambda: ""))
    monkeypatch.setattr(status, "get_radarr_info", SimpleNamespace(version=lambda: ""))
    monkeypatch.setattr(status, "get_sportarr_info", SimpleNamespace(version=lambda: ""))
    monkeypatch.setattr(status, "media_server_statuses", lambda: [])
    handler = status.SystemStatus.get
    while hasattr(handler, "__wrapped__"):
        handler = handler.__wrapped__

    monkeypatch.delenv("BAZARR_DEV_PLUGIN_SOURCE", raising=False)
    assert handler(status.SystemStatus())["data"]["dev_plugin_source"] is None
    monkeypatch.setenv("BAZARR_DEV_PLUGIN_SOURCE", SOURCE)
    assert handler(status.SystemStatus())["data"]["dev_plugin_source"] == SOURCE

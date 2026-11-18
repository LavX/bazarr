# coding=utf-8
"""Kinds on the shared Provider Hub core.

Every installation row carries a kind that never changes, every iteration
filters by it, and the side effects that belong to subtitle providers run
only through the subtitle provider kind.
"""
from types import SimpleNamespace

import pytest

from plugin_platform_helpers import (
    SERVICE_KIND,
    provider_manifest_v1,
    write_state,
)


def test_kind_registry_refuses_conflicts():
    from provider_hub.kinds import (
        SUBTITLE_PROVIDER, Kind, UnknownKindError, get_kind, register_kind, unregister_kind,
    )

    class Squatter(Kind):
        name = "squatter"
        kind_apis = frozenset({"bazarr.subtitle_provider.v1"})

    with pytest.raises(ValueError, match="already belongs"):
        register_kind(Squatter())
    with pytest.raises(UnknownKindError):
        get_kind("no_such_kind")
    with pytest.raises(ValueError):
        unregister_kind(SUBTITLE_PROVIDER)


def test_the_subtitle_provider_kind_attaches_on_restart():
    from provider_hub.kinds import ATTACH_ON_RESTART, SUBTITLE_PROVIDER, get_kind, kind_for_api, kind_of

    kind = get_kind(SUBTITLE_PROVIDER)
    assert kind.attach_policy == ATTACH_ON_RESTART
    assert kind_for_api("bazarr.subtitle_provider.v1") is kind
    assert kind_of({"provider_id": "legacyhub"}) == SUBTITLE_PROVIDER


def _active_row(provider_id, kind=None, manifest=None):
    row = {
        "provider_id": provider_id,
        "name": provider_id,
        "active_version": "1.0.0",
        "state": "active",
        "pending_restart": False,
        "manifest": manifest or provider_manifest_v1(provider_id),
        "config": {"api_key": "plain"},
    }
    if kind:
        row["kind"] = kind
    return row


def test_rows_written_before_kinds_load_as_subtitle_providers(tmp_path, monkeypatch):
    from provider_hub.state import load_state

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(
        tmp_path / "state.json", installations={"legacyhub": _active_row("legacyhub")})))

    assert load_state()["installations"]["legacyhub"]["kind"] == "subtitle_provider"


def test_iterations_see_only_their_own_kind(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.kinds import subtitle_provider
    from provider_hub.state import active_installations

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "providerhub": _active_row("providerhub"),
        "exampleservice": _active_row("exampleservice", kind=SERVICE_KIND),
    })))
    monkeypatch.setattr(subtitle_provider, "bazarr_enabled_providers", lambda: ["providerhub"])

    assert [item.provider_id for item in active_installations()] == ["providerhub"]
    assert [item.provider_id for item in active_installations(kind=SERVICE_KIND)] == ["exampleservice"]
    assert {item.provider_id for item in active_installations(kind=None)} == {"providerhub", "exampleservice"}
    assert set(service.runtime_provider_configs()) == {"providerhub"}
    assert [item["provider_id"] for item in service.list_providers()] == ["providerhub"]
    assert {item["provider_id"] for item in service.list_installations()} == {"providerhub", "exampleservice"}
    assert subtitle_provider.settings_section_plugin_ids() == {"providerhub"}


def test_a_service_plugin_never_joins_the_provider_pool(tmp_path, monkeypatch, service_kind):
    from provider_hub import registry
    from subliminal_patch.extensions import provider_registry

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "exampleservice": _active_row("exampleservice", kind=SERVICE_KIND),
    })))

    assert registry.register_active_provider_classes() == []
    assert "exampleservice" not in provider_registry.names()


def test_a_service_plugin_never_reads_a_settings_section(tmp_path, monkeypatch, service_kind):
    from app import get_providers

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "exampleservice": _active_row("exampleservice", kind=SERVICE_KIND),
    })))

    assert "exampleservice" not in get_providers.get_providers_auth()


def test_an_installation_never_changes_kind(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "examplehub": _active_row("examplehub", kind=SERVICE_KIND),
    })))
    staged = []
    monkeypatch.setattr(service, "_fetch_bundle", lambda *args, **kwargs: staged.append("bundle"))

    with pytest.raises(service.ProviderHubInstallError, match="installed as a test_service plugin"):
        service.stage_install(provider_manifest_v1("examplehub", "2.0.0"))
    assert staged == []


def test_provider_side_effects_run_only_for_providers(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.kinds import subtitle_provider

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "exampleservice": _active_row("exampleservice", kind=SERVICE_KIND),
    })))
    enabled_calls = []
    monkeypatch.setattr(subtitle_provider, "set_bazarr_provider_enabled",
                        lambda provider_id, enabled: enabled_calls.append((provider_id, enabled)))
    monkeypatch.setattr(subtitle_provider, "forget_credential_throttle",
                        lambda provider_id: enabled_calls.append(("throttle", provider_id)))

    service.update_provider("exampleservice", enabled=False)
    service.update_provider("exampleservice", config={"api_key": "new"})
    service.remove_installation("exampleservice")

    assert enabled_calls == []
    assert service_kind.calls == [
        ("enabled", "exampleservice", False),
        ("configured", "exampleservice"),
        ("removed", "exampleservice"),
        ("after_removed", "exampleservice"),
    ]


def test_cleanup_that_deletes_data_runs_only_after_the_removal_is_saved(tmp_path, monkeypatch, service_kind):
    from provider_hub import service

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "exampleservice": _active_row("exampleservice", kind=SERVICE_KIND),
    })))

    real = service.mutate_state

    def cannot_save_the_removal(mutation):
        if mutation.__name__ == "remove_or_stage":
            raise OSError("the state file cannot be written")
        return real(mutation)

    monkeypatch.setattr(service, "mutate_state", cannot_save_the_removal)
    with pytest.raises(OSError):
        service.remove_installation("exampleservice")
    assert ("after_removed", "exampleservice") not in service_kind.calls
    assert ("removed", "exampleservice") in service_kind.calls


def test_an_update_check_covers_only_the_kind_it_is_asked_for(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.kinds import SUBTITLE_PROVIDER

    def bound(row):
        return dict(row, source_bound=True, source_id="official")

    # Only the subtitle kind is offered updates today, so the offer rule is lifted here to leave the
    # kind filter of the update check as the one thing that keeps the other installation out of the
    # subtitle check. The catalog stores both newer versions as schema 1 manifests.
    monkeypatch.setattr(service, "_offerable_as_update", lambda entry, installed_kind: True)
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", catalog_entries={
        "official:providerhub:1.1.0": {"source": "official", "provider_id": "providerhub", "version": "1.1.0",
                                       "manifest": provider_manifest_v1("providerhub", "1.1.0")},
        "official:exampleservice:1.1.0": {"source": "official", "provider_id": "exampleservice",
                                          "version": "1.1.0",
                                          "manifest": provider_manifest_v1("exampleservice", "1.1.0")},
    }, installations={
        "providerhub": bound(_active_row("providerhub")),
        "exampleservice": bound(_active_row("exampleservice", kind=SERVICE_KIND)),
    })))

    everything = service.check_updates()
    subtitle = service.check_updates(kind=SUBTITLE_PROVIDER)

    assert sorted(everything["details"]["providers"]) == [
        "exampleservice: 1.0.0 -> 1.1.0",
        "providerhub: 1.0.0 -> 1.1.0",
    ]
    assert subtitle["details"]["providers"] == ["providerhub: 1.0.0 -> 1.1.0"]


def test_no_kind_keeps_every_entry():
    from provider_hub.kinds import SUBTITLE_PROVIDER, of_kind

    items = [{"provider_id": "providerhub", "kind": SUBTITLE_PROVIDER},
             SimpleNamespace(provider_id="exampleservice", kind=SERVICE_KIND)]

    assert of_kind(items, None) == items


def test_a_kind_keeps_only_the_entries_whose_kind_matches():
    from provider_hub.kinds import SUBTITLE_PROVIDER, of_kind

    provider = {"provider_id": "providerhub", "kind": SUBTITLE_PROVIDER}
    service_row = {"provider_id": "examplehub", "kind": SERVICE_KIND}
    service = SimpleNamespace(provider_id="exampleservice", kind=SERVICE_KIND)

    assert of_kind([provider, service_row, service], SERVICE_KIND) == [service_row, service]


def test_an_entry_with_no_kind_is_a_subtitle_provider():
    from provider_hub.kinds import SUBTITLE_PROVIDER, of_kind

    legacy = {"provider_id": "legacyhub"}
    service = SimpleNamespace(provider_id="exampleservice", kind=SERVICE_KIND)

    assert legacy in of_kind([legacy, service], SUBTITLE_PROVIDER)


def test_an_entry_of_another_kind_is_left_out_of_the_subtitle_filter():
    from provider_hub.kinds import SUBTITLE_PROVIDER, of_kind

    legacy = {"provider_id": "legacyhub"}
    service = SimpleNamespace(provider_id="exampleservice", kind=SERVICE_KIND)

    assert service not in of_kind([legacy, service], SUBTITLE_PROVIDER)


def test_a_row_without_a_kind_is_a_subtitle_provider_even_when_its_manifest_names_another_kind():
    from provider_hub.kinds import SUBTITLE_PROVIDER, kind_of

    assert kind_of({"provider_id": "legacyhub", "manifest": {"kind": "other_kind"}}) == SUBTITLE_PROVIDER


def test_a_catalog_entry_takes_its_kind_from_its_manifest():
    from provider_hub.kinds import SUBTITLE_PROVIDER, catalog_kind_of

    assert catalog_kind_of({"provider_id": "x", "manifest": {"schema_version": 2, "kind": "other_kind"}}) == "other_kind"
    assert catalog_kind_of({"provider_id": "x", "manifest": {}}) == SUBTITLE_PROVIDER


def test_a_catalog_entry_counts_a_kind_only_from_a_schema_2_manifest():
    from provider_hub.kinds import SUBTITLE_PROVIDER, catalog_kind_of

    assert catalog_kind_of({"kind": "other_kind", "manifest": {}}) == SUBTITLE_PROVIDER
    assert catalog_kind_of({"manifest": {"schema_version": 1, "kind": "other_kind"}}) == SUBTITLE_PROVIDER
    assert catalog_kind_of({"manifest": {"schema_version": 2, "kind": "other_kind"}}) == "other_kind"


@pytest.mark.parametrize("keep", [True, False])
def test_a_removal_hands_the_choice_about_its_data_to_the_kind(tmp_path, monkeypatch, service_kind, keep):
    from provider_hub import service

    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(write_state(tmp_path / "state.json", installations={
        "exampleservice": _active_row("exampleservice", kind=SERVICE_KIND),
    })))

    service.remove_installation("exampleservice", keep_data=keep)
    assert ("after_removed", "exampleservice") in service_kind.calls
    assert service_kind.kept is keep


# --- a subtitle provider's own settings, and nothing shared --------------------------------------------------


class _Section:
    """The provider's section read fresh each time, since a dotted write replaces the box it holds."""

    def __init__(self, config):
        self._config = config

    def read(self):
        section = self._config.settings.opensubtitlescom
        return section.username, section.password


@pytest.fixture
def provider_section(monkeypatch):
    """The real settings with one provider's section filled in and saving stubbed, put back afterwards."""
    from app import config
    from subliminal_patch.extensions import provider_registry

    before = _Section(config).read()
    config.settings["opensubtitlescom.username"] = "someone"
    config.settings["opensubtitlescom.password"] = "secret-value"  # pragma: allowlist secret
    saved = []
    monkeypatch.setattr(config, "write_config", lambda *a, **k: saved.append(1) or True)
    monkeypatch.setattr(provider_registry, "names", lambda: [])
    try:
        yield config, _Section(config), saved
    finally:
        config.settings["opensubtitlescom.username"], config.settings["opensubtitlescom.password"] = before


def test_deleting_a_providers_data_resets_its_own_settings_section(provider_section):
    from provider_hub.kinds.subtitle_provider import SubtitleProviderKind

    _config, section, saved = provider_section

    SubtitleProviderKind().after_removed("opensubtitlescom", keep_data=False)

    assert section.read() == ("", "") and saved == [1]


def test_keeping_a_providers_data_leaves_its_settings_alone(provider_section):
    from provider_hub.kinds.subtitle_provider import SubtitleProviderKind

    _config, section, saved = provider_section

    SubtitleProviderKind().after_removed("opensubtitlescom", keep_data=True)

    assert section.read() == ("someone", "secret-value") and saved == []  # pragma: allowlist secret


def test_a_section_a_built_in_provider_still_reads_is_never_cleared(provider_section, monkeypatch):
    from provider_hub.kinds.subtitle_provider import SubtitleProviderKind
    from subliminal_patch.extensions import provider_registry

    _config, section, saved = provider_section
    monkeypatch.setattr(provider_registry, "names", lambda: ["opensubtitlescom"])

    SubtitleProviderKind().after_removed("opensubtitlescom", keep_data=False)

    assert section.read()[0] == "someone" and saved == []


@pytest.mark.parametrize("plugin_id", ["general", "translator", "auth", "unknownplugin", "exampleservice"])
def test_only_a_providers_own_section_is_ever_cleared(provider_section, plugin_id):
    from provider_hub.kinds.subtitle_provider import own_settings_section_fields

    assert own_settings_section_fields(plugin_id) == {}


def test_the_fields_cleared_are_all_in_the_providers_own_section(provider_section):
    from provider_hub.kinds.subtitle_provider import own_settings_section_fields

    fields = own_settings_section_fields("opensubtitlescom")
    assert fields and all(name.startswith("opensubtitlescom.") for name in fields)


def test_a_settings_save_that_fails_puts_the_values_back(provider_section, monkeypatch):
    from provider_hub.kinds.subtitle_provider import reset_own_settings_section

    config, section, _saved = provider_section
    monkeypatch.setattr(config, "write_config", lambda *a, **k: False)

    assert reset_own_settings_section("opensubtitlescom") is False
    assert section.read() == ("someone", "secret-value")  # pragma: allowlist secret

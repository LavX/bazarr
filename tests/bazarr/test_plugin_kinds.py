# coding=utf-8
"""Kinds on the shared Provider Hub core.

Every installation row carries a kind that never changes, every iteration
filters by it, and the side effects that belong to subtitle providers run
only through the subtitle provider kind.
"""
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
    ]


def test_an_update_check_covers_only_the_kind_it_is_asked_for(tmp_path, monkeypatch, service_kind):
    from provider_hub import service
    from provider_hub.kinds import SUBTITLE_PROVIDER

    def bound(row):
        return dict(row, source_bound=True, source_id="official")

    # Catalog validation accepts only subtitle provider manifests, so the newer
    # version of the other installation is carried as one. The kind filter is
    # what keeps that installation out of the subtitle check.
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

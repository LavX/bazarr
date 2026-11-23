"""Review notices and retained plugin data persist on SQLite and PostgreSQL."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import provider_hub_state_engines as engines
from provider_hub_state_engines import raw_row

hub_engine = engines.hub_engine
hub_store = engines.hub_store


def _installation(provider_id, **extra):
    return {"provider_id": provider_id, "name": provider_id, "active_version": "1.0.0", "state": "active",
            "pending_restart": False, "manifest": {"name": provider_id}, **extra}


def test_dismissal_clears_the_flag_in_the_stored_document_and_leaves_the_others(hub_store, hub_engine):
    from provider_hub import service
    from provider_hub.state import load_state, mutate_state

    mutate_state(lambda state: state["installations"].update(
        flagged=_installation("flagged", review_pending=True), other=_installation("other", review_pending=True)))
    revision = raw_row(hub_engine)[0]

    assert service.dismiss_review_notice("flagged") is True

    installations = load_state()["installations"]
    assert installations["flagged"]["review_pending"] is False
    assert installations["other"]["review_pending"] is True
    assert raw_row(hub_engine)[0] == revision + 1


def test_dismissing_the_notice_of_a_plugin_that_is_not_there_writes_nothing_it_did_not_have(hub_store, hub_engine):
    from provider_hub import service
    from provider_hub.state import load_state

    assert service.dismiss_review_notice("ghost") is False
    assert "ghost" not in load_state()["installations"]


class _Kind:
    keeps_data = True

    def __init__(self):
        self.discarded = []
        self.reclaimed = []
        self.fail_discard = False

    def discard_kept_data(self, plugin_id):
        if self.fail_discard:
            raise RuntimeError("cannot clear")
        self.discarded.append(plugin_id)

    def kept_data_reclaimed(self, plugin_id):
        self.reclaimed.append(plugin_id)


def _removal(service, kind_hooks, plugin_id, keep, **row):
    from provider_hub.state import mutate_state

    installation = {"provider_id": plugin_id, "kind": "translator", "source_id": "official", **row}
    mutate_state(lambda state: service._record_kept_data(state, plugin_id, installation, kind_hooks, keep))


def _stage_row(plugin_id, **row):
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["installations"].update(
        {plugin_id: {"provider_id": plugin_id, "state": "staged", "kind": "translator", "staged_version": "1.0.0",
                     **row}}))


@pytest.mark.parametrize("plugin_kind", ["subtitle_provider", "translator", "service"])
@pytest.mark.parametrize("claim", ["same_owner", "other_source", "upload", "other_kind"])
def test_kept_data_goes_back_only_to_the_same_plugin_from_the_same_source_and_kind(
        hub_store, hub_engine, monkeypatch, tmp_path, plugin_kind, claim):
    from compat import service as compat_service
    from provider_hub import datadir, detach, kinds, leases, registry, service
    from provider_hub.kinds import subtitle_provider, translator
    from provider_hub.kinds.service import ServiceKind
    from provider_hub.state import load_state, mutate_state
    from subtitles.tools.translate.runtime import worker_binding

    # Keep the actual lifecycle hooks; isolate their settings, runtime and worker dependencies.
    monkeypatch.setattr(subtitle_provider, "set_bazarr_provider_enabled", lambda plugin_id, enabled: True)
    monkeypatch.setattr(subtitle_provider, "bazarr_enabled_providers", lambda: ["engine"])
    monkeypatch.setattr(subtitle_provider, "reset_own_settings_section", lambda plugin_id: True)
    monkeypatch.setattr(registry, "unregister_provider_class", lambda plugin_id: SimpleNamespace(removed=False))
    monkeypatch.setattr(registry, "revoke_host_calls_of", lambda plugin_id: None)
    monkeypatch.setattr(registry, "retire_provider_instances", lambda plugin_id, grace: [])
    monkeypatch.setattr(compat_service, "reset_compat_pool", lambda: None)
    monkeypatch.setattr(leases, "detach_service", lambda plugin_id: [])
    monkeypatch.setattr(translator, "_store", lambda: SimpleNamespace(
        release_plugin=lambda plugin_id: SimpleNamespace(notices=[]), delete_plugin_data=lambda plugin_id: None))
    for name in ("plugin_changed", "plugin_kept", "plugin_installed", "discard_folders"):
        monkeypatch.setattr(worker_binding, name, lambda plugin_id: None)
    monkeypatch.setattr(worker_binding, "plugin_detached", lambda plugin_id, grace: [])
    monkeypatch.setattr(worker_binding, "sweep_removed_plugins", lambda: None)
    monkeypatch.setattr(detach, "snapshot_workers", lambda plugin_id: [])
    hooks = {
        "subtitle_provider": subtitle_provider.SubtitleProviderKind(),
        "translator": translator.TranslatorKind(), "service": ServiceKind(),
    }
    observed_hooks = ("on_removed", "after_removed", "removal_finished", "discard_kept_data", "kept_data_reclaimed")
    for name, hook in hooks.items():
        for method in observed_hooks:
            monkeypatch.setattr(hook, method, Mock(wraps=getattr(hook, method)))
        monkeypatch.setitem(kinds._KINDS, name, hook)
    kind = hooks[plugin_kind]
    monkeypatch.setattr(service, "provider_hub_dir", lambda: tmp_path)
    manifest = {"schema_version": 2, "kind": plugin_kind, "permissions": ["data_dir"], "version": "1.0.0"}
    active = datadir.release_path(tmp_path, "engine", "a" * 32)
    active.mkdir(parents=True)
    (active / "saved.txt").write_text("saved state")
    retained = datadir.plugin_root(tmp_path, "engine") / "kept"
    marker = retained / "saved.txt"
    owner = {
        "source_id": "official", "origin": "catalog", "kind": plugin_kind, "config": {"region": "eu"},
    }
    mutate_state(lambda state: state["installations"].update(engine=_installation(
        "engine", kind=plugin_kind, source_id="official", origin="catalog", manifest=manifest,
        active_data_key=active.name, config={"region": "eu"})))

    assert service.remove_installation("engine", keep_data=True) is True
    assert detach.wait_idle(timeout=5)

    stored = json.loads(raw_row(hub_engine)[1])
    assert "engine" not in stored["installations"]
    assert stored["kept_data"]["engine"] == owner
    kind.on_removed.assert_called_once_with("engine")
    kind.after_removed.assert_called_once_with("engine", keep_data=True)
    kind.removal_finished.assert_called_once_with("engine")
    assert not active.exists()
    assert marker.read_text() == "saved state"

    source = "community" if claim == "other_source" else None if claim == "upload" else "official"
    incoming_kind = ("translator" if plugin_kind == "service" else "service") if claim == "other_kind" else plugin_kind
    origin = "local" if claim == "upload" else "catalog"
    _stage_row("engine", kind=incoming_kind, source_id=source, origin=origin)

    assert service.claim_kept_data("engine", source, incoming_kind, origin) is (claim == "same_owner")

    state = load_state()
    if claim == "same_owner":
        kind.discard_kept_data.assert_not_called()
        kind.kept_data_reclaimed.assert_called_once_with("engine")
        assert state["kept_data"]["engine"] == {**owner, "config": None}
        assert state["installations"]["engine"]["config"] == {"region": "eu"}
        assert marker.read_text() == "saved state"
    else:
        kind.discard_kept_data.assert_called_once_with("engine")
        kind.kept_data_reclaimed.assert_not_called()
        assert "engine" not in state["kept_data"]
        assert not state["installations"]["engine"].get("config")
        assert not retained.exists()
    for name, hook in hooks.items():
        if name != plugin_kind:
            for method in observed_hooks:
                getattr(hook, method).assert_not_called()


def test_a_plugins_saved_settings_come_back_with_its_kept_data_once(hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True, config={"base_url": "https://x.test", "api_key": "enc:abc"})
    _stage_row("engine")

    assert service.claim_kept_data("engine", "official", "translator") is True

    state = load_state()
    assert state["installations"]["engine"]["config"] == {"base_url": "https://x.test", "api_key": "enc:abc"}
    # The copy of the settings in the record is dropped once they are back on the row.
    assert state["kept_data"]["engine"]["config"] is None


def test_a_claim_that_cannot_clear_what_was_kept_refuses_the_install_and_keeps_the_record(
        hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    kind.fail_discard = True
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True)

    with pytest.raises(service.ProviderHubInstallError, match="could not be cleared"):
        service.claim_kept_data("engine", "community", "translator")

    assert "engine" in load_state()["kept_data"]


def test_a_removal_that_deletes_the_data_forgets_what_was_kept_and_a_kind_with_nothing_to_keep_records_nothing(
        hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True)
    _removal(service, kind, "engine", False)
    assert "engine" not in load_state()["kept_data"]

    kind.keeps_data = False
    _removal(service, kind, "service", True, kind="service")
    assert "service" not in load_state()["kept_data"]
    # Nothing was kept for it, so a first install has nothing to discard either.
    assert service.claim_kept_data("service", "official", "service") is False
    assert kind.discarded == []

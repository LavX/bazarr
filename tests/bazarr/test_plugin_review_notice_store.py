"""Dismissing a plugin's review notice writes the installation record, on SQLite and on PostgreSQL."""
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


def _removal(service, kind, plugin_id, keep, **row):
    service._record_kept_data(plugin_id, {"provider_id": plugin_id, "kind": "translator", "source_id": "official",
                                           **row}, kind, keep)


def _stage_row(plugin_id, **row):
    from provider_hub.state import mutate_state

    mutate_state(lambda state: state["installations"].update(
        {plugin_id: {"provider_id": plugin_id, "state": "staged", "kind": "translator", "staged_version": "1.0.0",
                     **row}}))


def test_kept_data_goes_back_only_to_the_same_plugin_from_the_same_source_and_kind(hub_store, hub_engine, monkeypatch):
    from provider_hub import service
    from provider_hub.state import load_state

    kind = _Kind()
    monkeypatch.setattr(service, "get_kind", lambda name: kind)
    _removal(service, kind, "engine", True)
    assert load_state()["kept_data"]["engine"] == {"source_id": "official", "origin": None, "kind": "translator", "config": None}

    assert service.claim_kept_data("engine", "official", "translator") is True
    # Handed over: nothing discarded, the owner stays on record until a removal rewrites it.
    assert kind.discarded == [] and kind.reclaimed == ["engine"]
    assert load_state()["kept_data"]["engine"]["source_id"] == "official"

    # Another source, an upload over a catalog install, and another kind each start clean.
    for source, kind_name in (("community", "translator"), (None, "translator"), ("official", "service")):
        _removal(service, kind, "engine", True)
        assert service.claim_kept_data("engine", source, kind_name) is False, (source, kind_name)
        assert "engine" not in load_state()["kept_data"], (source, kind_name)
    assert kind.discarded == ["engine"] * 3


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
    _removal(service, kind, "provider", True)
    assert "provider" not in load_state()["kept_data"]
    # Nothing was kept for it, so a first install has nothing to discard either.
    assert service.claim_kept_data("provider", "official", "subtitle_provider") is False
    assert kind.discarded == []

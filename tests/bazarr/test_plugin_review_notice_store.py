"""Dismissing a plugin's review notice writes the installation record, on SQLite and on PostgreSQL."""
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

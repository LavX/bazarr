"""A translator release becoming active brings the saved engines and back ends in line with its manifest.

These go through the real activation of a staged installation (``activate_staged_installations``) with the
translator kind registered, not through the store helper.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from plugin_platform_helpers import service_manifest

PLUGIN = "lingarr"
SECRET = "plain-token-0123456789-abcdef"  # pragma: allowlist secret


def _manifest(version, fields, backend_fields=()):
    settings = {"fields": list(fields)}
    if backend_fields:
        settings["backend_types"] = {"openrouter": {"label": "OpenRouter", "fields": list(backend_fields)}}
    return service_manifest(PLUGIN, version, kind="translator", kind_api="bazarr.translator.v1",
                            settings_version=1, settings=settings)


OLD_FIELDS = [{"key": "label", "type": "text"}, {"key": "token", "type": "text"}]


@pytest.fixture
def lifecycle(monkeypatch, tmp_path, schema_session):
    from provider_hub import kinds
    from provider_hub.kinds.translator import register_translator_kind
    from secret_store import scrub
    from subtitles.tools.translate.runtime import store as store_module

    class Pointers:
        engine = fallback = None

        def get(self):
            return self.engine, self.fallback

        def set(self, engine, fallback):
            self.engine, self.fallback = engine, fallback
            return True

    pointers = Pointers()
    before = kinds._KINDS.pop("translator", None)
    register_translator_kind()
    monkeypatch.setattr(store_module, "ConfigPointers", lambda: pointers)
    monkeypatch.setattr(store_module, "default_plugin_active", lambda plugin_id: True)
    monkeypatch.setitem(sys.modules, "app.database", SimpleNamespace(database=schema_session))
    monkeypatch.setattr("provider_hub.service.verify_bundle_tree", lambda manifest, root: None)
    monkeypatch.setattr("provider_hub.service._smoke_validate_worker", lambda manifest, bundle, python: None)
    scrubbed = []
    monkeypatch.setattr(scrub, "refresh_secret_values",
                        lambda *args, **kwargs: scrubbed.append(store_module.collect_secret_values()))
    store = store_module.TranslatorStore(schema_session, pointers=pointers)

    def stage(old, new):
        state = {"installations": {PLUGIN: {
            "provider_id": PLUGIN, "name": "Lingarr", "kind": "translator", "active_version": "1.0.0",
            "staged_version": "2.0.0", "active_path": "/old", "python_path": "/old/python",
            "staged_path": str(tmp_path / "bundle"), "staged_python_path": str(tmp_path / "python"),
            "state": "staged", "pending_restart": True, "manifest": old, "staged_manifest": new}}}
        path = tmp_path / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(path))

    world = SimpleNamespace(store=store, pointers=pointers, stage=stage, scrubbed=scrubbed)
    try:
        yield world
    finally:
        kinds._KINDS.pop("translator", None)
        if before is not None:
            kinds._KINDS["translator"] = before


def _activate():
    from provider_hub.service import activate_staged_installations
    from provider_hub.state import load_state

    activated = activate_staged_installations()
    installation = load_state()["installations"][PLUGIN]
    assert activated == [PLUGIN], installation.get("last_error")
    return installation


def test_activating_a_release_that_makes_a_field_secret_moves_the_plaintext_into_the_secret_store(lifecycle):
    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine", "token": SECRET})
    new_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"}]
    lifecycle.stage(_manifest("1.0.0", OLD_FIELDS), _manifest("2.0.0", new_fields))
    installation = _activate()
    assert installation["state"] == "active" and installation["active_version"] == "2.0.0"
    assert installation["last_error"] is None
    store._session.expire_all()
    row = store.get_instance(instance.id)
    assert store.settings_of(row) == {"label": "mine"} and row.revision == 2 and row.enabled == 1
    assert store.secrets_for_request(("instance", instance.id), frozenset()) == {"token": SECRET}
    # And the scrubber knows it as soon as the activation is done.
    assert lifecycle.scrubbed and SECRET in lifecycle.scrubbed[-1]


def test_activating_a_release_that_drops_a_field_removes_it_from_what_is_stored(lifecycle):
    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine", "token": "gone"})
    lifecycle.stage(_manifest("1.0.0", OLD_FIELDS), _manifest("2.0.0", [{"key": "label", "type": "text"}]))
    assert _activate()["last_error"] is None
    store._session.expire_all()
    assert store.settings_of(store.get_instance(instance.id)) == {"label": "mine"}


def test_a_release_whose_new_rules_the_saved_settings_do_not_meet_holds_them_back_and_switches_them_off(lifecycle):
    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine", "token": SECRET})
    connection = store.add_connection(instance.id, "openrouter", plugin_id=PLUGIN, settings={"label": "c"})
    store.set_engine(instance.id)
    new_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"},
                  {"key": "region", "type": "text", "required": True}]
    lifecycle.stage(_manifest("1.0.0", OLD_FIELDS, [{"key": "label", "type": "text"}]),
                    _manifest("2.0.0", new_fields, [{"key": "label", "type": "text"}]))
    installation = _activate()
    # The release is active, but what could not be carried over is switched off and flagged.
    assert installation["state"] == "active" and installation["active_version"] == "2.0.0"
    assert "Settings > Translation" in installation["last_error"] and SECRET not in installation["last_error"]
    store._session.expire_all()
    held = store.get_instance(instance.id)
    assert held.enabled == 0 and held.awaiting_secrets == 1
    assert store.settings_of(held) == {"label": "mine", "token": SECRET}  # nothing was changed or moved
    assert store.secret_summary(("instance", instance.id), frozenset()) == {}
    assert lifecycle.pointers.get() == (None, None)
    # The back end met the new rules and was migrated normally.
    assert store.get_connection(connection.id).enabled == 1
    assert store.connection_usable(store.get_connection(connection.id)) is True

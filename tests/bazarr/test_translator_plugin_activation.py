"""A translator release becoming active brings the saved engines and back ends in line with its manifest.

These go through the real activation of a staged installation (``activate_staged_installations``) with the
translator kind registered, not through the store helper.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from plugin_platform_helpers import service_manifest

PLUGIN = "example_engine"
SECRET = "plain-token-0123456789-abcdef"  # pragma: allowlist secret


def _manifest(version, fields, backend_fields=(), **extra):
    settings = {"fields": list(fields)}
    if backend_fields:
        settings["backend_types"] = {"openrouter": {"label": "OpenRouter", "fields": list(backend_fields)}}
    return service_manifest(PLUGIN, version, kind="translator", kind_api="bazarr.translator.v1",
                            settings_version=1, settings=settings, **extra)


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
            "provider_id": PLUGIN, "name": "Example Engine", "kind": "translator", "active_version": "1.0.0",
            "staged_version": "2.0.0", "active_path": "/old", "python_path": "/old/python",
            "staged_path": str(tmp_path / "bundles" / PLUGIN / "2.0.0" / ("b" * 40)),
            "staged_python_path": str(tmp_path / "envs" / PLUGIN / "2.0.0" / "test" / "bin" / "python"),
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


def test_a_secret_of_a_field_the_release_removed_does_not_keep_the_owner_waiting(lifecycle):
    store = lifecycle.store
    origin = "https://api.example:443"
    old_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"},
                  {"key": "oldkey", "type": "secret"}]
    new_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"}]
    instance = store.create_instance(PLUGIN, settings={"label": "mine"})
    owner = ("instance", instance.id)
    store.set_secret(owner, "token", "current-token-0123456789", destinations=frozenset({origin}))
    # Bound to no address, the way a key entered before the engine named one is.
    store.set_secret(owner, "oldkey", "obsolete-key-0123456789", destinations=frozenset())
    lifecycle.stage(_manifest("1.0.0", old_fields, endpoints=[origin]),
                    _manifest("2.0.0", new_fields, endpoints=[origin]))
    assert _activate()["last_error"] is None
    store._session.expire_all()
    assert store.get_instance(instance.id).awaiting_secrets == 0
    assert set(store.secret_summary(owner, frozenset({origin}))) == {"token"}


def test_a_held_back_engine_with_a_removed_secret_can_be_repaired_and_turned_back_on(lifecycle):
    from subtitles.tools.translate.runtime.destinations import DestinationSchema, effective_destinations

    store = lifecycle.store
    old_origin, new_origin = "https://api.example:443", "https://api.example.net:443"
    old_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"},
                  {"key": "oldkey", "type": "secret"}]
    new_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"},
                  {"key": "region", "type": "text", "required": True}]
    instance = store.create_instance(PLUGIN, settings={"label": "mine"})
    owner = ("instance", instance.id)
    for name in ("token", "oldkey"):
        store.set_secret(owner, name, f"{name}-value-0123456789", destinations=frozenset({old_origin}))
    lifecycle.stage(_manifest("1.0.0", old_fields, endpoints=[old_origin]),
                    _manifest("2.0.0", new_fields, endpoints=[new_origin]))
    # The release drops a secret, requires a field the engine does not have and moves the address.
    assert "Translation in the main menu" in _activate()["last_error"]
    store._session.expire_all()
    held = store.get_instance(instance.id)
    assert held.enabled == 0 and held.awaiting_secrets == 1

    # The person gives the missing field and the key for the new address, saves, and turns the engine on.
    schema = DestinationSchema(fields=tuple(new_fields), endpoints=(new_origin,))
    destinations = effective_destinations(schema, {"label": "mine", "region": "eu"})
    store.set_secret(owner, "token", "fresh-token-0123456789", destinations=destinations)
    saved = store.save_instance_settings(instance.id, {"label": "mine", "region": "eu"}, schema=schema,
                                         expected_revision=held.revision)
    assert saved.awaiting_secrets is False
    store.set_instance_enabled(instance.id, True, expected_revision=saved.revision)
    store._session.expire_all()
    engine = store.get_instance(instance.id)
    assert engine.enabled == 1 and engine.awaiting_secrets == 0
    assert store.instance_configured(engine) is True
    assert set(store.secret_summary(owner, destinations)) == {"token"}


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
    assert "Translation in the main menu" in installation["last_error"] and SECRET not in installation["last_error"]
    store._session.expire_all()
    held = store.get_instance(instance.id)
    assert held.enabled == 0 and held.awaiting_secrets == 1
    # Held back is not left in the clear: the token that is a secret now has moved into the secret store, and
    # the log scrubber knows it. Everything else it held is as it was.
    assert store.settings_of(held) == {"label": "mine"}
    assert store.secrets_for_request(("instance", instance.id), frozenset()) == {"token": SECRET}
    assert lifecycle.scrubbed and SECRET in lifecycle.scrubbed[-1]
    assert lifecycle.pointers.get() == (None, None)
    # The back end met the new rules and was migrated normally.
    assert store.get_connection(connection.id).enabled == 1
    assert store.connection_usable(store.get_connection(connection.id)) is True


def _activate_with_connections(lifecycle, connections, new_backend_fields):
    """One engine with the given connections (name -> keyword arguments), then a release that changes the rules."""
    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine"})
    made = {name: store.add_connection(instance.id, "openrouter", plugin_id=PLUGIN, **kwargs)
            for name, kwargs in connections.items()}
    keep = [{"key": "label", "type": "text"}]
    lifecycle.stage(_manifest("1.0.0", keep, [{"key": "label", "type": "text"}]),
                    _manifest("2.0.0", keep, new_backend_fields))
    installation = _activate()
    store._session.expire_all()
    return installation, {name: store.get_connection(row.id) for name, row in made.items()}, store.get_instance(
        instance.id)


def test_a_release_that_requires_a_model_holds_back_the_connections_that_have_none(lifecycle):
    installation, rows, engine = _activate_with_connections(
        lifecycle, {"bare": {"model": ""}, "named": {"model": "vendor/m"}},
        [{"key": "label", "type": "text"}, {"key": "model", "type": "options", "required": True}])
    assert rows["bare"].enabled == 0 and rows["bare"].awaiting_secrets == 1
    assert rows["named"].enabled == 1 and rows["named"].awaiting_secrets == 0
    assert engine.enabled == 1 and "Translation in the main menu" in installation["last_error"]


def test_a_release_that_tightens_a_price_limit_holds_back_the_connections_over_it(lifecycle):
    _installation, rows, _engine = _activate_with_connections(
        lifecycle, {"dear": {"price_input_per_million": 5}, "cheap": {"price_input_per_million": 1}},
        [{"key": "label", "type": "text"}, {"key": "price_input_per_million", "type": "number", "max": 2}])
    assert rows["dear"].enabled == 0 and rows["dear"].awaiting_secrets == 1
    assert rows["cheap"].enabled == 1
    assert rows["dear"].price_input_per_million == 5  # held back as it was, not rewritten


def test_a_field_required_only_for_one_model_holds_back_the_connections_that_use_it(lifecycle):
    _installation, rows, _engine = _activate_with_connections(
        lifecycle,
        {"special": {"model": "special", "settings": {"label": "c"}},
         "special_ok": {"model": "special", "settings": {"label": "c", "extra": "given"}},
         "plain": {"model": "plain", "settings": {"label": "c"}}},
        [{"key": "label", "type": "text"},
         {"key": "extra", "type": "text", "required": True, "show_if": {"key": "model", "equals": "special"}}])
    assert rows["special"].enabled == 0 and rows["special"].awaiting_secrets == 1
    assert rows["special_ok"].enabled == 1 and rows["plain"].enabled == 1


def test_an_engine_that_declares_its_own_model_field_keeps_it_through_activation(lifecycle):
    store = lifecycle.store
    optional = store.create_instance(PLUGIN, name="optional", settings={"label": "a", "model": "mine"})
    required = store.create_instance(PLUGIN, name="required", settings={"label": "b", "model": "needed"})
    both = [{"key": "label", "type": "text"}, {"key": "model", "type": "text"}]
    lifecycle.stage(_manifest("1.0.0", both), _manifest("2.0.0", [
        {"key": "label", "type": "text"}, {"key": "model", "type": "text", "required": True}]))
    installation = _activate()
    store._session.expire_all()
    # The value of an engine's own "model" setting is kept, and a required one it already holds is satisfied.
    assert store.settings_of(store.get_instance(optional.id)) == {"label": "a", "model": "mine"}
    assert store.get_instance(optional.id).enabled == 1 and store.get_instance(required.id).enabled == 1
    assert installation["last_error"] is None


def test_a_failure_while_checking_a_release_still_moves_the_newly_secret_values(lifecycle, monkeypatch):
    from subtitles.tools.translate.runtime import plugin_update

    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine", "token": SECRET})

    def broken(*_args, **_kwargs):
        raise RuntimeError("the check failed")

    monkeypatch.setattr(plugin_update, "_fits", broken)
    new_fields = [{"key": "label", "type": "text"}, {"key": "token", "type": "secret"}]
    lifecycle.stage(_manifest("1.0.0", OLD_FIELDS), _manifest("2.0.0", new_fields))
    installation = _activate()
    assert "could not be brought up to date" in installation["last_error"]
    store._session.expire_all()
    assert store.settings_of(store.get_instance(instance.id)) == {"label": "mine"}
    assert store.secrets_for_request(("instance", instance.id), frozenset()) == {"token": SECRET}
    assert lifecycle.scrubbed and SECRET in lifecycle.scrubbed[-1]


def test_a_connection_whose_back_end_type_is_gone_shows_none_of_its_stored_settings(lifecycle):
    from subtitles.tools.translate.runtime.settings_service import TranslatorSettingsService

    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine"})
    store.add_connection(instance.id, "retired", plugin_id=PLUGIN, settings={"token": SECRET, "label": "c"})
    lifecycle.stage(_manifest("1.0.0", OLD_FIELDS), _manifest("2.0.0", [{"key": "label", "type": "text"}]))
    _activate()
    store._session.expire_all()
    service = TranslatorSettingsService(store, plugin_lookup=lambda plugin_id: SimpleNamespace(
        plugin_id=plugin_id, name="Example Engine", version="2.0.0", active=True,
        manifest=_manifest("2.0.0", [{"key": "label", "type": "text"}])))
    view = service.instance_view(store.get_instance(instance.id))
    assert SECRET not in json.dumps(view) and view["connections"][0]["values"].get("token") is None


def test_a_held_back_engine_with_a_removed_plain_field_can_be_repaired_and_turned_back_on(lifecycle):
    from subtitles.tools.translate.runtime.settings_service import TranslatorSettingsService

    store = lifecycle.store
    old_fields = [{"key": "label", "type": "text"}, {"key": "legacy", "type": "text"}]
    new_fields = [{"key": "label", "type": "text"}, {"key": "region", "type": "text", "required": True}]
    instance = store.create_instance(PLUGIN, settings={"label": "mine", "legacy": "old"})
    lifecycle.stage(_manifest("1.0.0", old_fields), _manifest("2.0.0", new_fields))
    # The release drops a populated field and requires one the engine does not have: it is held back as it was.
    assert "Translation in the main menu" in _activate()["last_error"]
    store._session.expire_all()
    held = store.get_instance(instance.id)
    assert held.enabled == 0 and store.settings_of(held) == {"label": "mine", "legacy": "old"}

    service = TranslatorSettingsService(store, plugin_lookup=lambda plugin_id: SimpleNamespace(
        plugin_id=plugin_id, name="Example Engine", version="2.0.0", active=True, manifest=_manifest("2.0.0", new_fields)))
    saved = service.save_engine(instance.id, {"expected_revision": held.revision, "values": {"region": "eu"}})
    assert saved["engine"]["values"] == {"label": "mine", "region": "eu"}
    turned_on = service.save_engine(instance.id, {"expected_revision": saved["engine"]["revision"], "enabled": True})
    assert turned_on["engine"]["enabled"] is True
    store._session.expire_all()
    engine = store.get_instance(instance.id)
    assert store.settings_of(engine) == {"label": "mine", "region": "eu"}
    assert engine.enabled == 1 and store.instance_configured(engine) is True


def test_a_release_that_tightens_a_secret_limit_holds_back_the_owners_whose_stored_secret_breaks_it(lifecycle):
    store = lifecycle.store
    instance = store.create_instance(PLUGIN, settings={"label": "mine"})
    many = store.add_connection(instance.id, "openrouter", plugin_id=PLUGIN)
    long_one = store.add_connection(instance.id, "openrouter", plugin_id=PLUGIN)
    fine = store.add_connection(instance.id, "openrouter", plugin_id=PLUGIN)
    keys = ["key-one-0123456789", "key-two-0123456789", "key-three-0123456789"]
    store.set_secret(("connection", many.id), "keys", json.dumps(keys), destinations=frozenset())
    store.set_secret(("connection", long_one.id), "token", "t" * 30, destinations=frozenset())
    store.set_secret(("connection", fine.id), "keys", json.dumps(keys[:1]), destinations=frozenset())
    store.set_secret(("connection", fine.id), "token", "short", destinations=frozenset())
    old = [{"key": "keys", "type": "key_list", "secret": True, "max_items": 5},
           {"key": "token", "type": "secret", "max_length": 50}]
    new = [{"key": "keys", "type": "key_list", "secret": True, "max_items": 1},
           {"key": "token", "type": "secret", "max_length": 8}]
    lifecycle.stage(_manifest("1.0.0", [{"key": "label", "type": "text"}], old),
                    _manifest("2.0.0", [{"key": "label", "type": "text"}], new))
    installation = _activate()
    store._session.expire_all()
    assert store.get_connection(many.id).enabled == 0 and store.get_connection(many.id).awaiting_secrets == 1
    assert store.get_connection(long_one.id).enabled == 0
    assert store.get_connection(fine.id).enabled == 1
    # Held back as it was: the secrets stay where they are, and nothing about them appears in the message.
    assert json.loads(store.secrets_for_request(("connection", many.id), frozenset())["keys"]) == keys
    assert "Translation in the main menu" in installation["last_error"]
    assert not any(value in installation["last_error"] for value in keys + ["t" * 30])

"""The typed translation API: dark while the switch is off, CRUD with it on, and secrets that never come back."""
import json

import pytest
from flask import Flask

from plugin_platform_helpers import service_manifest
from subtitles.tools.translate.runtime.settings_service import InstalledPlugin, TranslatorSettingsService
from subtitles.tools.translate.runtime.store import OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore

API_KEY = "translation-api-test-key"  # pragma: allowlist secret
SECRET = "sk-live-0123456789abcdef-WXYZ"  # pragma: allowlist secret
KEYS = ["gem-key-one-0123456789", "gem-key-two-0123456789"]  # pragma: allowlist secret
PLUGIN = OFFICIAL_ENGINE_PLUGIN_ID


def _manifest():
    return service_manifest(
        PLUGIN, settings_version=2, kind="translator", kind_api="bazarr.translator.v1",
        endpoints=["https://updates.example.test:443"],
        settings={
            "fields": [
                {"key": "credit_line", "type": "text", "max_length": 80},
                {"key": "concurrency", "type": "integer", "min": 1, "max": 8, "default": 2},
                {"key": "service_token", "type": "secret"},
            ],
            "backend_types": {
                "openrouter": {"label": "OpenRouter", "endpoints": ["https://openrouter.ai:443"],
                               "fields": [{"key": "api_key", "type": "secret", "required": True}]},
                "gemini": {"label": "Gemini", "endpoints": ["https://generativelanguage.googleapis.com:443"],
                           "fields": [{"key": "api_keys", "type": "key_list", "secret": True}]},
                "openai_compatible": {"label": "OpenAI compatible", "fields": [
                    {"key": "preset", "type": "choice", "default": "custom", "choices": [
                        {"value": "lmstudio", "label": "LM Studio", "sets": {"base_url": "http://lms:1234/v1"}},
                        {"value": "custom", "label": "Custom"}]},
                    {"key": "base_url", "type": "text", "destination": True, "default": "https://api.openai.com/v1"},
                    {"key": "api_key", "type": "secret"}]},
            }})


class MemoryPointers:
    def __init__(self):
        self.engine = self.fallback = None

    def get(self):
        return self.engine, self.fallback

    def set(self, engine, fallback):
        self.engine, self.fallback = engine, fallback
        return True


@pytest.fixture
def world(monkeypatch, schema_session):
    """The API on an in-memory database with the switch on and one installed engine plugin."""
    from subtitles.tools.translate import runtime

    import api.translation.translation as module
    from api import api_bp
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)
    plugins = {PLUGIN: InstalledPlugin(PLUGIN, "AI Subtitle Translator", "2.2.0", True, _manifest())}
    store = TranslatorStore(schema_session, pointers=MemoryPointers(), plugin_active=lambda pid: pid in plugins)
    validations = []
    sources = []
    monkeypatch.setattr(module, "get_service", lambda: TranslatorSettingsService(
        store, plugin_lookup=plugins.get, plugin_validator=lambda *args: validations.append(args) or {},
        options_source=(lambda *args: sources[0](*args)) if sources else None))
    # The real blueprint registers these routes only when the switch is on at import, which a test cannot rely
    # on, so the routes get a blueprint of their own. The real one stays registered for the status route.
    from flask import Blueprint
    from flask_restx import Api

    blueprint = Blueprint("translation_test", __name__, url_prefix="/api")
    Api(blueprint, authorizations={"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}},
        security="apikey").add_namespace(module.api_ns_translation, "/")
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.register_blueprint(api_bp)
    client = app.test_client()
    instance = store.create_instance(PLUGIN, name="AI", settings_version=2, plugin_version="2.2.0")

    class World:
        pass

    w = World()
    w.client, w.store, w.instance, w.plugins, w.runtime, w.validations = client, store, instance, plugins, runtime, validations
    w.sources = sources

    def call(method, path, body=None, current=True):
        """Send a request. A save carries the revision the caller loaded, unless ``current`` is False."""
        if current and method == "patch" and isinstance(body, dict) and "expected_revision" not in body:
            kind, _, row_id = path.partition("/")
            row = (store.get_instance if kind == "engines" else store.get_connection)(int(row_id))
            if (kind == "connections" or body.get("values") is not None or body.get("clear_secrets")
                    or body.get("enabled") is not None):
                body = {**body, "expected_revision": row.revision}
        if current and method == "delete" and path.startswith("connections/") and "?" not in path:
            row = store.get_connection(int(path.split("/")[1]))
            path += f"?expected_revision={row.revision}" if row is not None else ""
        response = getattr(client, method)(
            "/api/translation/" + path, headers={"X-API-KEY": API_KEY},
            data=None if body is None else json.dumps(body), content_type="application/json")
        w.last_text = response.get_data(as_text=True)
        return response

    w.call = call
    return w


def _everything_said(w):
    return w.last_text


# ------------------------------------------------------------------- the switch

ROUTES = [
    ("get", "state", None), ("put", "pointers", {"engine": 1}), ("patch", "engines/1", {"values": {}}),
    ("post", "engines/1/connections", {"backend_type": "openrouter"}),
    ("put", "engines/1/connections/order", {"order": []}), ("post", "engines/1/options", {"field": "model"}),
    ("patch", "connections/1", {"enabled": False}), ("delete", "connections/1", None)]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_route_is_404_while_the_switch_is_off(world, monkeypatch, method, path, body):
    monkeypatch.setattr(world.runtime, "runtime_v2_enabled", lambda: False)
    response = world.call(method, path, body, current=False)
    assert response.status_code == 404
    # Nothing was read or written behind the refusal.
    assert world.store.list_connections(world.instance.id) == []
    assert world.store.pointers() == (None, None)


def test_a_request_without_the_key_is_refused_before_the_switch_is_looked_at(world):
    assert world.client.get("/api/translation/state").status_code == 401


def test_the_switch_really_off_answers_404_without_touching_the_database(world, monkeypatch):
    monkeypatch.undo()  # the default switch: off
    from app.config import settings
    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    assert world.call("get", "state").status_code == 404


# ------------------------------------------------------------------- reading

def test_state_lists_the_engine_with_its_schema_and_host_fields(world):
    state = world.call("get", "state").json
    assert state["enabled"] is True and (state["engine"], state["fallback_engine"]) == (None, None)
    (engine,) = state["instances"]
    assert engine["plugin_id"] == PLUGIN and engine["official"] is True and engine["revision"] == 1
    types = engine["schema"]["backend_types"]
    assert set(types) == {"openrouter", "gemini", "openai_compatible"}
    keys = [item["key"] for item in types["openrouter"]["fields"]]
    assert keys[:3] == ["model", "price_input_per_million", "price_output_per_million"] and "api_key" in keys
    assert types["openai_compatible"]["fields"][3]["choices"][0]["sets"] == {"base_url": "http://lms:1234/v1"}


# ---------------------------------------------------------------- engine settings

def test_saving_engine_settings_bumps_the_revision_and_keeps_secrets_out_of_the_response(world):
    response = world.call("patch", f"engines/{world.instance.id}", {
        "expected_revision": 1,
        "values": {"credit_line": "Translated by Bazarr+", "concurrency": 3, "service_token": SECRET}})
    assert response.status_code == 200, response.json
    engine = response.json["engine"]
    assert engine["revision"] == 2
    assert engine["values"] == {"credit_line": "Translated by Bazarr+", "concurrency": 3}
    assert engine["secrets"]["service_token"] == {"configured": True, "stale": False, "last4": SECRET[-4:]}
    assert SECRET not in _everything_said(world)
    assert SECRET not in world.call("get", "state").get_data(as_text=True)
    stored = world.store.get_instance(world.instance.id)
    assert SECRET not in stored.settings_json
    assert world.store.secrets_for_request(("instance", stored.id), frozenset()) == {"service_token": SECRET}


def test_an_empty_secret_keeps_the_stored_one_and_clear_removes_it(world):
    path = f"engines/{world.instance.id}"
    world.call("patch", path, {"values": {"service_token": SECRET}})
    kept = world.call("patch", path, {"values": {"service_token": "", "concurrency": 4}}).json["engine"]
    assert kept["secrets"]["service_token"]["configured"] is True and kept["values"]["concurrency"] == 4
    cleared = world.call("patch", path, {"clear_secrets": ["service_token"]}).json["engine"]
    assert cleared["secrets"] == {}
    assert world.call("patch", path, {"clear_secrets": ["concurrency"]}).status_code == 400


def test_an_invalid_value_is_refused_by_field_and_nothing_is_saved(world):
    response = world.call("patch", f"engines/{world.instance.id}", {"values": {"concurrency": "3", "nope": 1}})
    assert response.status_code == 400
    assert set(response.json["field_errors"]) == {"concurrency", "nope"}
    assert world.store.get_instance(world.instance.id).revision == 1


def test_the_plugins_own_validate_runs_after_the_schema_and_its_field_errors_are_returned(world, monkeypatch):
    import api.translation.translation as module
    from api.translation.translation import get_service as real

    service = real()
    service._validator = lambda *args: {"credit_line": "is too forward"}
    monkeypatch.setattr(module, "get_service", lambda: service)
    response = world.call("patch", f"engines/{world.instance.id}", {"values": {"credit_line": "x"}})
    assert response.status_code == 400 and response.json["field_errors"] == {"credit_line": "is too forward"}
    # A schema failure never reaches the plugin.
    world.validations.clear()
    world.call("patch", f"engines/{world.instance.id}", {"values": {"concurrency": "x"}})
    assert world.validations == []


def test_a_stale_revision_is_a_conflict(world):
    path = f"engines/{world.instance.id}"
    assert world.call("patch", path, {"expected_revision": 1, "values": {}}).status_code == 200
    stale = world.call("patch", path, {"expected_revision": 1, "values": {"concurrency": 5}})
    assert stale.status_code == 409 and stale.json["error"] == "stale_revision"


def test_an_engine_whose_plugin_is_gone_cannot_be_saved(world):
    world.plugins.clear()
    response = world.call("patch", f"engines/{world.instance.id}", {"values": {"concurrency": 2}})
    assert response.status_code == 409 and response.json["error"] == "plugin_unavailable"


def test_the_engine_switch_resets_a_pointer_that_named_it(world):
    world.call("put", "pointers", {"engine": world.instance.id})
    response = world.call("patch", f"engines/{world.instance.id}", {"enabled": False})
    assert response.status_code == 200
    assert [n["code"] for n in response.json["notices"]] == ["engine_reset"]
    assert world.call("get", "state").json["engine"] is None


# ------------------------------------------------------------------- pointers

def test_pointers_round_trip_and_never_name_the_same_instance(world):
    other = world.store.create_instance(PLUGIN, name="Second")
    assert world.call("put", "pointers", {"engine": world.instance.id, "fallback_engine": other.id}).json == {
        "engine": world.instance.id, "fallback_engine": other.id, "notices": []}
    refused = world.call("put", "pointers", {"fallback_engine": world.instance.id})
    assert refused.status_code == 400
    moved = world.call("put", "pointers", {"engine": other.id}).json
    assert moved["engine"] == other.id and [n["code"] for n in moved["notices"]] == ["fallback_reset"]
    assert world.call("put", "pointers", {"engine": 999}).status_code == 400
    assert world.call("put", "pointers", {"engine": "1"}).status_code == 400
    assert world.call("put", "pointers", {"engine": None}).json["engine"] is None


def test_a_pointer_request_that_fails_on_one_part_saves_neither(world, monkeypatch):
    other = world.store.create_instance(PLUGIN, name="Second")
    world.call("put", "pointers", {"engine": world.instance.id})
    writes = []
    real_set = world.store._pointers.set
    monkeypatch.setattr(world.store._pointers, "set", lambda *args: writes.append(args) or real_set(*args))

    unknown_fallback = world.call("put", "pointers", {"engine": other.id, "fallback_engine": 999})
    assert unknown_fallback.status_code == 400
    same_instance = world.call("put", "pointers", {"engine": other.id, "fallback_engine": other.id})
    assert same_instance.status_code == 400
    assert world.store.pointers() == (world.instance.id, None) and writes == []

    both = world.call("put", "pointers", {"engine": other.id, "fallback_engine": world.instance.id})
    assert both.json == {"engine": other.id, "fallback_engine": world.instance.id, "notices": []}
    assert len(writes) == 1  # the pair was written once


# ----------------------------------------------------------------- connections

def _new_connection(world, **body):
    body.setdefault("backend_type", "openrouter")
    return world.call("post", f"engines/{world.instance.id}/connections", body)


def test_a_connection_is_created_listed_edited_reordered_and_deleted(world):
    first = _new_connection(world, values={"api_key": SECRET, "model": "vendor/free", "price_input_per_million": 0.5})
    assert first.status_code == 201, first.json
    connection = first.json["connection"]
    assert (connection["position"], connection["model"], connection["enabled"]) == (0, "vendor/free", True)
    assert connection["values"]["price_input_per_million"] == 0.5 and connection["values"]["price_output_per_million"] == 0
    assert connection["secrets"]["api_key"]["configured"] is True and "api_key" not in connection["values"]
    assert SECRET not in _everything_said(world)
    second = _new_connection(world, backend_type="gemini", values={"api_keys": KEYS}).json["connection"]
    assert second["position"] == 1
    assert second["secrets"]["api_keys"] == {"configured": True, "stale": False, "count": 2}
    assert not any(key in _everything_said(world) for key in KEYS)

    edited = world.call("patch", f"connections/{connection['id']}", {
        "enabled": False, "values": {"model": "vendor/paid", "price_output_per_million": 2}}).json["connection"]
    assert (edited["enabled"], edited["model"], edited["values"]["price_output_per_million"]) == (False, "vendor/paid", 2)
    assert edited["secrets"]["api_key"]["configured"] is True  # not re-entered, still there
    assert world.store.get_instance(world.instance.id).revision == 1  # a connection edit never bumps it

    order = world.call("put", f"engines/{world.instance.id}/connections/order", {"order": [second["id"], connection["id"]]})
    assert [item["id"] for item in order.json["connections"]] == [second["id"], connection["id"]]
    assert world.call("put", f"engines/{world.instance.id}/connections/order", {"order": [connection["id"]]}).status_code == 400

    assert world.call("delete", f"connections/{connection['id']}").status_code == 204
    state = world.call("get", "state").json
    assert [item["id"] for item in state["instances"][0]["connections"]] == [second["id"]]
    assert world.call("delete", f"connections/{connection['id']}").status_code == 404
    assert world.store.secrets_for_request(("connection", connection["id"]), frozenset()) == {}


def test_an_emptied_price_goes_back_to_its_default_and_other_bad_prices_are_still_refused(world):
    created = _new_connection(world, values={"api_key": SECRET, "price_input_per_million": 0.5,
                                             "price_output_per_million": 1.5}).json["connection"]
    path = f"connections/{created['id']}"
    reset = world.call("patch", path, {"values": {"price_input_per_million": None}})
    assert reset.status_code == 200, reset.json
    values = reset.json["connection"]["values"]
    assert values["price_input_per_million"] == 0 and values["price_output_per_million"] == 1.5
    assert _new_connection(world, values={"api_key": "k", "price_input_per_million": None}).status_code == 201
    assert world.call("patch", path, {"values": {"price_output_per_million": "x"}}).status_code == 400
    assert world.call("patch", path, {"values": {"price_output_per_million": -1}}).status_code == 400


def test_a_connection_of_an_unknown_type_or_with_bad_values_is_refused(world):
    assert _new_connection(world, backend_type="nope").status_code == 400
    assert _new_connection(world, backend_type="").status_code == 400
    assert _new_connection(world, values={}).json["field_errors"] == {"api_key": "is required"}
    assert _new_connection(world, values={"api_key": "k", "price_input_per_million": -1}).status_code == 400
    assert _new_connection(world, values={"api_key": "k", "model": 5}).status_code == 400
    assert _new_connection(world, backend_type="gemini", values={"api_keys": "['key']"}).status_code == 400
    assert world.store.list_connections(world.instance.id) == []


def test_a_user_entered_base_url_needs_consent_before_anything_is_saved(world):
    body = {"backend_type": "openai_compatible", "values": {"preset": "lmstudio", "base_url": "http://lms:1234/v1"}}
    refused = _new_connection(world, **body)
    assert refused.status_code == 409 and refused.json["error"] == "consent_required"
    assert refused.json["destinations"] == ["http://lms:1234/v1"]
    assert world.store.list_connections(world.instance.id) == []
    accepted = _new_connection(world, **body, accept_destinations=["http://lms:1234/v1"])
    assert accepted.status_code == 201
    # The manifest's own default address is covered by the plugin's consent, not asked again.
    assert _new_connection(world, backend_type="openai_compatible", values={}).status_code == 201


def test_a_changed_base_url_asks_again_and_never_keeps_the_stored_key(world):
    url = "http://lms:1234/v1"
    created = _new_connection(world, backend_type="openai_compatible", accept_destinations=[url],
                              values={"base_url": url, "api_key": SECRET}).json["connection"]
    path = f"connections/{created['id']}"
    assert created["awaiting_secrets"] is False
    moved = {"values": {"base_url": "http://elsewhere:9/v1"}}
    assert world.call("patch", path, moved).status_code == 409
    saved = world.call("patch", path, {**moved, "accept_destinations": ["http://elsewhere:9/v1"]})
    assert saved.status_code == 200
    connection = saved.json["connection"]
    assert connection["awaiting_secrets"] is True and connection["usable"] is False
    assert connection["secrets"]["api_key"]["stale"] is True
    # The old key is not handed to a request for the new address.
    assert world.store.secrets_for_request(("connection", created["id"]), frozenset({"http://elsewhere:9/v1"})) == {}
    # Entering the key again clears the wait.
    again = world.call("patch", path, {"values": {"base_url": "http://elsewhere:9/v1", "api_key": SECRET + "2"}})
    assert again.json["connection"]["awaiting_secrets"] is False
    assert SECRET not in _everything_said(world)


def test_the_options_route_falls_back_to_free_text_without_a_source(world):
    path = f"engines/{world.instance.id}/options"
    answer = world.call("post", path, {"field": "model", "backend_type": "openrouter"}).json
    assert answer["available"] is False and answer["items"] == []
    assert world.call("post", "engines/999/options", {"field": "model", "backend_type": "openrouter"}).status_code == 404
    assert world.call("post", path, {"field": 5}).status_code == 400


def test_a_body_that_is_not_an_object_is_refused(world):
    response = world.client.patch(f"/api/translation/engines/{world.instance.id}", headers={"X-API-KEY": API_KEY},
                                  data="[1]", content_type="application/json")
    assert response.status_code == 400


def test_the_system_status_names_the_switch_only_while_it_is_on(world, monkeypatch):
    from app.config import settings

    def status():
        response = world.client.get("/api/system/status", headers={"X-API-KEY": API_KEY})
        return response.json["data"]

    monkeypatch.setitem(settings.translator, "runtime_v2", True)
    assert status()["translation_runtime_v2"] is True
    monkeypatch.setattr(world.runtime, "runtime_v2_enabled", lambda: False)
    assert "translation_runtime_v2" not in status()


def test_the_status_reads_the_setting_before_it_touches_the_runtime(world, monkeypatch):
    from app.config import settings

    def explode():
        raise AssertionError("the runtime was asked with the switch off")

    monkeypatch.setitem(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(world.runtime, "runtime_v2_enabled", explode)
    response = world.client.get("/api/system/status", headers={"X-API-KEY": API_KEY})
    assert response.status_code == 200 and "translation_runtime_v2" not in response.json["data"]


def test_the_package_lists_the_typed_api_beside_the_history_routes():
    from api.translation import api_ns_list_translation

    assert "Translation" in {namespace.name for namespace in api_ns_list_translation}


def test_a_boot_with_the_switch_on_registers_the_typed_api_and_one_with_it_off_does_not():
    import os
    import subprocess
    import sys
    from pathlib import Path

    bazarr = str(Path(__file__).parents[2] / "bazarr")

    def names_registered(switch):
        code = ("from app.config import settings; "
                f"settings.translator.runtime_v2 = {switch}; "
                "import api; "
                "print(sorted({namespace.name for group in api.api_ns_list for namespace in "
                "(group if isinstance(group, (list, tuple)) else [group])}))")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                                cwd=bazarr, env={**os.environ, "NO_CLI": "true",
                                                 "PYTHONPATH": os.pathsep.join([bazarr, *sys.path])})
        assert result.returncode == 0, result.stderr[-2000:]
        return result.stdout.strip().splitlines()[-1]

    assert "'Translation'" in names_registered(True)
    assert "'Translation'" not in names_registered(False)


# ------------------------------------------------------------ review fixes

def _strict_json(text):
    """The body as a strict JSON parser reads it: NaN and Infinity are errors."""
    def refuse(name):
        raise ValueError(name)
    return json.loads(text, parse_constant=refuse)


def test_a_key_list_save_keeps_every_other_credential_masked_and_masks_each_key(world, monkeypatch):
    from app.config import settings
    from secret_store import scrub

    other = "sonarr-key-0123456789abcdef"  # pragma: allowlist secret
    monkeypatch.setitem(settings.sonarr, "apikey", other)
    monkeypatch.setitem(settings.translator, "runtime_v2", True)
    monkeypatch.setattr("app.database.database", world.store._session)
    scrub.refresh_secret_values(settings)
    assert other not in scrub.scrub_text(f"key {other}")
    response = _new_connection(world, backend_type="gemini", values={"api_keys": KEYS})
    assert response.status_code == 201
    text = scrub.scrub_text(f"a {other} b {KEYS[0]} c {KEYS[1]}")
    assert other not in text and KEYS[0] not in text and KEYS[1] not in text


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN", "1e400"])
def test_a_non_finite_number_is_refused_everywhere_a_number_is_taken(world, literal):
    path = f"engines/{world.instance.id}/connections"
    body = '{"backend_type":"openrouter","values":{"api_key":"k","price_input_per_million":%s}}' % literal
    response = world.client.post("/api/translation/" + path, headers={"X-API-KEY": API_KEY}, data=body,
                                 content_type="application/json")
    assert response.status_code == 400
    patch = world.client.patch(
        f"/api/translation/engines/{world.instance.id}", headers={"X-API-KEY": API_KEY},
        data='{"expected_revision":1,"values":{"concurrency":%s}}' % literal, content_type="application/json")
    assert patch.status_code == 400
    assert world.store.list_connections(world.instance.id) == []


def test_the_state_stays_valid_json_when_a_stored_price_is_not_finite(world):
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    row = world.store.get_connection(created["id"])
    row.price_input_per_million = float("inf")
    row.settings_json = '{"x": NaN}'
    world.store._session.flush()
    response = world.call("get", "state")
    assert response.status_code == 200
    connection = _strict_json(world.last_text)["instances"][0]["connections"][0]
    assert connection["values"]["price_input_per_million"] == 0


def test_a_save_without_the_revision_is_refused(world):
    path = f"engines/{world.instance.id}"
    response = world.call("patch", path, {"values": {"concurrency": 2}}, current=False)
    assert response.status_code == 400 and response.json["error"] == "expected_revision_required"
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    response = world.call("patch", f"connections/{created['id']}", {"enabled": False}, current=False)
    assert response.status_code == 400 and response.json["error"] == "expected_revision_required"


def test_the_store_swaps_the_revision_in_the_database_not_in_memory(world):
    from sqlalchemy import update

    from app.database import TableTranslatorEngineInstance
    from subtitles.tools.translate.runtime.destinations import DestinationSchema
    from subtitles.tools.translate.runtime.store import StaleRevisionError

    instance = world.store.get_instance(world.instance.id)
    assert instance.revision == 1
    # Another writer got in after this one read the row: the in-memory revision still says 1.
    world.store._session.execute(update(TableTranslatorEngineInstance).where(
        TableTranslatorEngineInstance.id == instance.id).values(revision=5).execution_options(
        synchronize_session=False))
    with pytest.raises(StaleRevisionError):
        world.store.save_instance_settings(instance.id, {"a": 1}, schema=DestinationSchema())
    assert world.store.settings_of(instance) == {}


def test_a_connection_has_its_own_revision_and_a_stale_save_is_a_conflict(world):
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    assert created["revision"] == 1
    path = f"connections/{created['id']}"
    first = world.call("patch", path, {"enabled": False})
    assert first.status_code == 200 and first.json["connection"]["revision"] == 2
    stale = world.call("patch", path, {"enabled": True, "expected_revision": 1})
    assert stale.status_code == 409 and stale.json["error"] == "stale_revision" and stale.json["revision"] == 2
    assert world.store.get_instance(world.instance.id).revision == 1


def test_clearing_a_secret_and_typing_a_new_one_in_one_save_keeps_the_new_one(world):
    created = _new_connection(world, backend_type="gemini", values={"api_keys": KEYS}).json["connection"]
    new = ["gem-key-new-0123456789"]  # pragma: allowlist secret
    response = world.call("patch", f"connections/{created['id']}", {
        "values": {"api_keys": new}, "clear_secrets": ["api_keys"]})
    assert response.status_code == 200
    assert response.json["connection"]["secrets"]["api_keys"]["count"] == 1
    held = world.store.secrets_for_request(("connection", created["id"]), frozenset())
    assert json.loads(held["api_keys"]) == new
    # Clearing alone still removes it.
    cleared = world.call("patch", f"connections/{created['id']}", {"clear_secrets": ["api_keys"]})
    assert cleared.json["connection"]["secrets"] == {}


def test_a_connection_save_merges_onto_what_is_stored(world):
    url = "http://lms:1234/v1"
    created = _new_connection(world, backend_type="openai_compatible", accept_destinations=[url],
                              values={"base_url": url, "preset": "lmstudio", "model": "m1"}).json["connection"]
    path = f"connections/{created['id']}"
    merged = world.call("patch", path, {"values": {"model": "m2"}}).json["connection"]
    assert merged["values"]["base_url"] == url and merged["values"]["preset"] == "lmstudio"
    assert merged["values"]["model"] == "m2"
    # An empty value takes a key back to its default.
    reset = world.call("patch", path, {"values": {"preset": ""}}).json["connection"]
    assert "preset" not in reset["values"] and reset["values"]["base_url"] == url


def test_a_manifest_that_declares_the_model_field_keeps_its_limits_and_saves_work(world):
    fields = world.plugins[PLUGIN].manifest["settings"]["backend_types"]["openrouter"]["fields"]
    fields.append({"key": "model", "type": "options", "required": True, "max_length": 10})
    assert _new_connection(world, values={"api_key": "k"}).json["field_errors"] == {"model": "is required"}
    assert _new_connection(world, values={"api_key": "k", "model": "x" * 11}).status_code == 400
    created = _new_connection(world, values={"api_key": "k", "model": "ok"})
    assert created.status_code == 201
    saved = world.call("patch", f"connections/{created.json['connection']['id']}", {"values": {"api_key": "n"}})
    assert saved.status_code == 200 and saved.json["connection"]["model"] == "ok"


def test_a_required_model_with_a_manifest_default_stores_the_default_when_left_out_or_emptied(world):
    fields = world.plugins[PLUGIN].manifest["settings"]["backend_types"]["openrouter"]["fields"]
    fields.append({"key": "model", "type": "options", "required": True, "default": "vendor/default"})
    left_out = _new_connection(world, values={"api_key": "k"})
    assert left_out.status_code == 201 and left_out.json["connection"]["model"] == "vendor/default"
    assert world.store.get_connection(left_out.json["connection"]["id"]).model == "vendor/default"
    emptied = _new_connection(world, values={"api_key": "k", "model": ""})
    assert emptied.status_code == 201 and emptied.json["connection"]["model"] == "vendor/default"
    chosen = _new_connection(world, values={"api_key": "k", "model": "vendor/mine"}).json["connection"]
    assert chosen["model"] == "vendor/mine"
    reset = world.call("patch", f"connections/{chosen['id']}", {"values": {"model": ""}})
    assert reset.json["connection"]["model"] == "vendor/default"
    kept = world.call("patch", f"connections/{chosen['id']}", {"values": {"api_key": "n"}})
    assert kept.json["connection"]["model"] == "vendor/default"


def test_the_options_route_refuses_a_draft_address_that_is_malformed_or_holds_a_password(world):
    seen = []
    world.sources.append(lambda *args: seen.append(args) or {"items": [{"id": "m"}]})
    path = f"engines/{world.instance.id}/options"
    for url in ("https://user:hunter2@[bad/v1", "https://user:hunter2@a.example/v1", "x.test:1234", "ftp://a.test"):
        response = world.call("post", path, {"field": "model", "backend_type": "openai_compatible",
                                              "values": {"base_url": url}})
        assert response.status_code == 400 and list(response.json["field_errors"]) == ["base_url"]
        assert "hunter2" not in world.last_text
    assert seen == []


def test_the_options_route_checks_the_request_against_the_manifest_and_strips_secrets(world):
    seen = []
    world.sources.append(lambda *args: seen.append(args) or {"items": [{"id": "m"}]})
    path = f"engines/{world.instance.id}/options"
    answer = world.call("post", path, {
        "field": "model", "backend_type": "openai_compatible",
        "values": {"base_url": "http://x:1/v1", "api_key": SECRET, "invented": 1}}).json
    assert answer["available"] is True
    (call,) = seen
    assert call == (world.instance.id, "openai_compatible", "model", {"base_url": "http://x:1/v1"})
    assert world.call("post", path, {"field": "model", "backend_type": "nope"}).status_code == 400
    assert world.call("post", path, {"field": "base_url", "backend_type": "openai_compatible"}).status_code == 400
    assert world.call("post", path, {"field": "model"}).status_code == 400  # the engine itself has no model field
    assert len(seen) == 1


def test_a_user_name_or_password_in_an_address_is_refused_and_never_echoed(world):
    response = _new_connection(world, backend_type="openai_compatible",
                               values={"base_url": "https://user:hunter2@a.example/v1"},
                               accept_destinations=["https://user:hunter2@a.example/v1"])
    assert response.status_code == 400
    assert response.json["field_errors"] == {"base_url": "must not contain a user name or password"}
    assert "hunter2" not in world.last_text
    assert world.store.list_connections(world.instance.id) == []


def test_a_malformed_address_with_a_password_is_refused_before_consent_or_storage(world):
    url = "https://user:hunter2@[invalid/v1"
    response = _new_connection(world, backend_type="openai_compatible", values={"base_url": url})
    assert response.status_code == 400 and list(response.json["field_errors"]) == ["base_url"]
    assert "hunter2" not in world.last_text and response.json.get("destinations") is None
    assert world.store.list_connections(world.instance.id) == []


def test_the_options_route_checks_every_draft_value_like_a_save_does(world):
    seen = []
    world.sources.append(lambda *args: seen.append(args) or {"items": [{"id": "m"}]})
    path = f"engines/{world.instance.id}/options"
    body = {"field": "model", "backend_type": "openai_compatible"}
    for values, field in (({"price_input_per_million": -1}, "price_input_per_million"),
                          ({"price_input_per_million": {"x": 1}}, "price_input_per_million"),
                          ({"preset": "not-offered"}, "preset"), ({"base_url": 5}, "base_url")):
        refused = world.call("post", path, {**body, "values": values})
        assert refused.status_code == 400 and list(refused.json["field_errors"]) == [field]
    assert world.call("post", path, {**body, "values": "text"}).status_code == 400
    assert seen == []
    assert world.call("post", path, {**body, "values": {"base_url": "http://x:1/v1", "preset": "custom"}}).status_code == 200


def test_a_field_the_plugin_now_declares_secret_is_never_in_the_values_of_a_response(world):
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    instance = world.store.get_instance(world.instance.id)
    instance.settings_json = json.dumps({"credit_line": "plain-credit-0123", "concurrency": 3})
    world.store.get_connection(created["id"]).settings_json = json.dumps({"region": "plain-region-0123"})
    world.store._session.commit()
    # An update of the plugin turns two text fields, populated in the clear, into secrets.
    manifest = world.plugins[PLUGIN].manifest["settings"]
    for field in manifest["fields"]:
        if field["key"] == "credit_line":
            field["type"] = "secret"
    manifest["backend_types"]["openrouter"]["fields"].append({"key": "region", "type": "secret"})
    state = world.call("get", "state")
    assert state.status_code == 200
    engine = state.json["instances"][0]
    assert "plain-credit-0123" not in world.last_text and "plain-region-0123" not in world.last_text
    assert "credit_line" not in engine["values"] and engine["values"]["concurrency"] == 3
    assert "region" not in engine["connections"][0]["values"]
    # Saving drops what is no longer a setting: the value has to be typed again, as a secret.
    saved = world.call("patch", f"connections/{created['id']}", {"values": {"region": "typed-region-0123"}})
    assert saved.status_code == 200 and "typed-region-0123" not in world.last_text
    assert "region" not in json.loads(world.store.get_connection(created["id"]).settings_json)
    assert world.store.secrets_for_request(("connection", created["id"]), frozenset())["region"] == "typed-region-0123"


def test_a_stored_address_with_a_password_is_never_shown_on_the_page(world):
    created = _new_connection(world, backend_type="openai_compatible",
                              values={"base_url": "http://a.example/v1"}, accept_destinations=["http://a.example/v1"])
    row = world.store.get_connection(created.json["connection"]["id"])
    row.settings_json = json.dumps({"base_url": "https://user:hunter2@a.example/v1"})  # stored some other way
    world.store._session.commit()
    state = world.call("get", "state")
    assert state.status_code == 200 and "hunter2" not in world.last_text
    shown = state.json["instances"][0]["connections"][0]["values"]
    assert "base_url" not in shown


def test_clearing_a_required_connection_secret_alone_is_refused_like_clearing_it_with_values(world):
    created = _new_connection(world, values={"api_key": SECRET}).json["connection"]
    path = f"connections/{created['id']}"
    for body in ({"clear_secrets": ["api_key"]}, {"values": {}, "clear_secrets": ["api_key"]}):
        refused = world.call("patch", path, body)
        assert refused.status_code == 400 and refused.json["field_errors"] == {"api_key": "is required"}
    stored = world.store.get_connection(created["id"])
    assert stored.revision == created["revision"]
    assert "api_key" in world.store.secret_summary(("connection", created["id"]), frozenset())
    # Typing a new key in the same request keeps it valid.
    ok = world.call("patch", path, {"values": {"api_key": "new-key"}, "clear_secrets": ["api_key"]})
    assert ok.status_code == 200 and ok.json["connection"]["secrets"]["api_key"]["configured"] is True


def test_clearing_a_required_engine_secret_alone_is_refused(world):
    path = f"engines/{world.instance.id}"
    assert world.call("patch", path, {"values": {"service_token": SECRET}}).status_code == 200
    for field in world.plugins[PLUGIN].manifest["settings"]["fields"]:
        if field["key"] == "service_token":
            field["required"] = True
    refused = world.call("patch", path, {"clear_secrets": ["service_token"]})
    assert refused.status_code == 400 and refused.json["field_errors"] == {"service_token": "is required"}
    assert world.store.get_instance(world.instance.id).revision == 2
    assert world.store.secrets_for_request(("instance", world.instance.id), frozenset()) == {"service_token": SECRET}


def test_turning_something_on_checks_what_it_holds_while_turning_it_off_needs_nothing(world):
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    path = f"connections/{created['id']}"
    assert world.call("patch", path, {"enabled": False}).status_code == 200
    # The plugin now asks for something this connection never had.
    world.plugins[PLUGIN].manifest["settings"]["backend_types"]["openrouter"]["fields"].append(
        {"key": "region", "type": "text", "required": True})
    refused = world.call("patch", path, {"enabled": True})
    assert refused.status_code == 400 and refused.json["field_errors"] == {"region": "is required"}
    assert world.store.get_connection(created["id"]).enabled == 0
    assert world.call("patch", path, {"values": {"region": "eu"}, "enabled": True}).status_code == 200
    assert world.call("patch", path, {"enabled": False}).status_code == 200  # off is always allowed
    # The same for the engine.
    engine = f"engines/{world.instance.id}"
    assert world.call("patch", engine, {"enabled": False}).status_code == 200
    world.plugins[PLUGIN].manifest["settings"]["fields"].append({"key": "site", "type": "text", "required": True})
    refused = world.call("patch", engine, {"enabled": True})
    assert refused.status_code == 400 and refused.json["field_errors"] == {"site": "is required"}
    assert world.store.get_instance(world.instance.id).enabled == 0
    assert world.call("patch", engine, {"values": {"site": "x"}, "enabled": True}).status_code == 200


def test_turning_an_engine_on_or_off_needs_the_revision_and_a_stale_one_is_a_conflict(world):
    path = f"engines/{world.instance.id}"
    assert world.call("patch", path, {"enabled": False}, current=False).json["error"] == "expected_revision_required"
    assert world.store.get_instance(world.instance.id).enabled == 1
    off = world.call("patch", path, {"enabled": False, "expected_revision": 1})
    assert off.status_code == 200 and off.json["engine"]["enabled"] is False and off.json["engine"]["revision"] == 2
    # A delayed "turn on" from a view that still showed revision 1 must not win.
    late = world.call("patch", path, {"enabled": True, "expected_revision": 1})
    assert late.status_code == 409 and late.json["error"] == "stale_revision" and late.json["revision"] == 2
    assert world.store.get_instance(world.instance.id).enabled == 0
    on = world.call("patch", path, {"enabled": True, "expected_revision": 2})
    assert on.status_code == 200 and on.json["engine"]["revision"] == 3
    # Settings and the switch in one request move the revision once for each.
    both = world.call("patch", path, {"enabled": False, "expected_revision": 3, "values": {"credit_line": "x"}})
    assert both.status_code == 200 and both.json["engine"]["revision"] == 5 and both.json["engine"]["enabled"] is False


def test_the_store_swaps_the_revision_when_it_turns_an_engine_on_or_off(world):
    from sqlalchemy import update

    from app.database import TableTranslatorEngineInstance
    from subtitles.tools.translate.runtime.store import StaleRevisionError

    instance = world.store.get_instance(world.instance.id)
    # Another writer got in after this one read the row: the in-memory revision still says 1.
    world.store._session.execute(update(TableTranslatorEngineInstance).where(
        TableTranslatorEngineInstance.id == instance.id).values(revision=5).execution_options(
        synchronize_session=False))
    with pytest.raises(StaleRevisionError):
        world.store.set_instance_enabled(instance.id, False)
    assert instance.enabled == 1


def test_removing_a_connection_needs_the_revision_and_a_stale_one_is_a_conflict(world):
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    path = f"connections/{created['id']}"
    assert world.call("delete", path, current=False).json["error"] == "expected_revision_required"
    assert world.call("delete", path + "?expected_revision=abc", current=False).status_code == 400
    # Someone else saved it after this view loaded it.
    assert world.call("patch", path, {"enabled": False, "expected_revision": created["revision"]}).status_code == 200
    stale = world.call("delete", path + f"?expected_revision={created['revision']}", current=False)
    assert stale.status_code == 409 and stale.json["error"] == "stale_revision"
    assert world.store.get_connection(created["id"]) is not None
    assert world.call("delete", path).status_code == 204


def test_settings_and_the_switch_in_one_request_are_kept_or_dropped_together(world, monkeypatch):
    from subtitles.tools.translate.runtime.store import StaleRevisionError as StoreStale

    def loses(*args, **kwargs):
        raise StoreStale("the engine settings changed since they were loaded", 9)

    monkeypatch.setattr(world.store, "set_instance_enabled", loses)
    path = f"engines/{world.instance.id}"
    refused = world.call("patch", path, {"enabled": False, "values": {"credit_line": "kept?"}, "expected_revision": 1})
    assert refused.status_code == 409 and refused.json["error"] == "stale_revision"
    world.store._session.expire_all()
    stored = world.store.get_instance(world.instance.id)
    assert stored.revision == 1 and stored.settings_json in ("{}", "") and stored.enabled == 1


def test_a_connection_created_with_an_empty_controlling_value_is_checked_as_it_will_be_stored(world):
    fields = world.plugins[PLUGIN].manifest["settings"]["backend_types"]["openrouter"]["fields"]
    fields.append({"key": "mode", "type": "text", "default": "custom"})
    for field in fields:
        if field["key"] == "api_key":
            field["show_if"] = {"key": "mode", "equals": "custom"}
    refused = _new_connection(world, values={"mode": ""})
    assert refused.status_code == 400 and refused.json["field_errors"] == {"api_key": "is required"}
    assert world.store.list_connections(world.instance.id) == []
    assert _new_connection(world, values={"mode": "other"}).status_code == 201


def test_a_condition_on_the_model_sees_the_model_that_will_be_stored(world):
    fields = world.plugins[PLUGIN].manifest["settings"]["backend_types"]["openrouter"]["fields"]
    fields.append({"key": "extra", "type": "text", "required": True, "show_if": {"key": "model", "equals": "special"}})
    refused = _new_connection(world, values={"api_key": "k", "model": "special"})
    assert refused.status_code == 400 and refused.json["field_errors"] == {"extra": "is required"}
    created = _new_connection(world, values={"api_key": "k", "model": "plain"})
    assert created.status_code == 201
    switched = world.call("patch", f"connections/{created.json['connection']['id']}", {"values": {"model": "special"}})
    assert switched.status_code == 400 and switched.json["field_errors"] == {"extra": "is required"}


def test_a_manifest_that_declares_a_price_or_the_model_has_its_limits_and_defaults_applied(world):
    fields = world.plugins[PLUGIN].manifest["settings"]["backend_types"]["openrouter"]["fields"]
    fields.append({"key": "price_input_per_million", "type": "number", "min": 0.1, "max": 2, "default": 0.5})
    fields.append({"key": "model", "type": "options", "max_length": 10, "default": "vendor/df"})
    # Create: left out takes the default; over the limit, under the limit and too long are refused.
    created = _new_connection(world, values={"api_key": "k"})
    assert created.status_code == 201, created.json
    values = created.json["connection"]["values"]
    assert values["price_input_per_million"] == 0.5 and values["model"] == "vendor/df"
    assert values["price_output_per_million"] == 0  # not declared: the host's own default
    for bad, field in (({"price_input_per_million": 3}, "price_input_per_million"),
                       ({"price_input_per_million": 0.01}, "price_input_per_million"),
                       ({"model": "x" * 11}, "model"), ({"price_output_per_million": -1}, "price_output_per_million")):
        refused = _new_connection(world, values={"api_key": "k", **bad})
        assert refused.status_code == 400 and list(refused.json["field_errors"]) == [field]
    assert len(world.store.list_connections(world.instance.id)) == 1
    # Edit: a limit applies to a change, a value inside the limits is kept.
    path = f"connections/{created.json['connection']['id']}"
    assert world.call("patch", path, {"values": {"price_input_per_million": 3}}).status_code == 400
    edited = world.call("patch", path, {"values": {"price_input_per_million": 1.5, "model": "mine"}})
    assert edited.status_code == 200 and edited.json["connection"]["values"]["price_input_per_million"] == 1.5
    # Reset: an emptied price or model goes back to the declared default, not to zero or blank.
    reset = world.call("patch", path, {"values": {"price_input_per_million": None, "model": ""}})
    values = reset.json["connection"]["values"]
    assert values["price_input_per_million"] == 0.5 and values["model"] == "vendor/df"
    # Turning it on checks what it holds against the same rules.
    assert world.call("patch", path, {"enabled": False}).status_code == 200
    row = world.store.get_connection(created.json["connection"]["id"])
    row.price_input_per_million = 9
    world.store._session.commit()
    refused = world.call("patch", path, {"enabled": True})
    assert refused.status_code == 400 and list(refused.json["field_errors"]) == ["price_input_per_million"]


def test_a_stale_toggle_loses_to_a_save_that_lands_after_the_request_began(world, monkeypatch):
    from sqlalchemy import update

    from app.database import TableTranslatorEngineInstance

    path = f"engines/{world.instance.id}"
    real = world.store.transaction

    def another_writer_gets_in_first():
        world.store._session.execute(update(TableTranslatorEngineInstance).where(
            TableTranslatorEngineInstance.id == world.instance.id).values(revision=2)
            .execution_options(synchronize_session=False))
        return real()

    monkeypatch.setattr(world.store, "transaction", another_writer_gets_in_first)
    stale = world.call("patch", path, {"enabled": False, "expected_revision": 1})
    assert stale.status_code == 409 and stale.json["error"] == "stale_revision"
    monkeypatch.undo()
    world.store._session.expire_all()
    assert world.store.get_instance(world.instance.id).enabled == 1


def test_a_flag_that_is_not_a_boolean_is_refused(world):
    assert _new_connection(world, enabled="false", values={"api_key": "k"}).status_code == 400
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    assert world.call("patch", f"connections/{created['id']}", {"enabled": "no"}).status_code == 400
    assert world.call("patch", f"engines/{world.instance.id}", {"enabled": 0}).status_code == 400

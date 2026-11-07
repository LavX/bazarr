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
            if kind == "connections" or body.get("values") is not None or body.get("clear_secrets"):
                body = {**body, "expected_revision": row.revision}
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


def test_a_flag_that_is_not_a_boolean_is_refused(world):
    assert _new_connection(world, enabled="false", values={"api_key": "k"}).status_code == 400
    created = _new_connection(world, values={"api_key": "k"}).json["connection"]
    assert world.call("patch", f"connections/{created['id']}", {"enabled": "no"}).status_code == 400
    assert world.call("patch", f"engines/{world.instance.id}", {"enabled": 0}).status_code == 400

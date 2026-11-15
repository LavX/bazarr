"""Lingarr as a legacy engine: listed, chosen and configured through the typed API, in the settings it always used.

It has no plugin and no worker. Its address and token stay in the two Lingarr settings, so nothing migrates,
and the token is write-only like every other secret of the runtime.
"""
import json
import logging

import pytest
from flask import Blueprint, Flask
from flask_restx import Api

from subtitles.tools.translate.runtime import legacy_engines
from subtitles.tools.translate.runtime.settings_service import TranslatorSettingsService
from subtitles.tools.translate.runtime.store import TranslatorStore

API_KEY = "translation-lingarr-test-key"  # pragma: allowlist secret
TOKEN = "lingarr-token-0123456789abcdef"  # pragma: allowlist secret
OTHER_TOKEN = "lingarr-token-fedcba9876543210"  # pragma: allowlist secret
URL = "http://192.168.1.30:9876"


class FakeLingarr:
    """The Lingarr settings held in memory, shaped like the config-backed object."""

    def __init__(self):
        self.address = legacy_engines.DEFAULT_URL
        self.token = ""
        self.fail = False

    def lock(self):
        import threading

        return threading.RLock()

    def url(self):
        return self.address

    def token_set(self):
        return bool(self.token)

    def set(self, url, token):
        if self.fail:
            return False
        self.address = url
        if token is not None:
            self.token = token
        return True


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
    from subtitles.tools.translate import runtime

    import api.translation.translation as module
    from api import api_bp
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)
    fake = FakeLingarr()
    monkeypatch.setattr(legacy_engines, "ConfigLingarr", lambda: fake)
    store = TranslatorStore(schema_session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: False)

    class World:
        pass

    w = World()
    w.session, w.store, w.fake, w.runtime, w.legacy = schema_session, store, fake, runtime, True
    monkeypatch.setattr(module, "get_service", lambda: TranslatorSettingsService(
        store, plugin_lookup=lambda plugin_id: None, catalog_engines=lambda: [], update_lookup=lambda plugin_id: None,
        legacy=w.legacy))
    blueprint = Blueprint("translation_lingarr_test", __name__, url_prefix="/api")
    Api(blueprint, authorizations={"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}},
        security="apikey").add_namespace(module.api_ns_translation, "/")
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.register_blueprint(api_bp)
    client = app.test_client()

    def call(method, path, body=None, key=True):
        response = getattr(client, method)("/api/translation/" + path, headers={"X-API-KEY": API_KEY} if key else {},
                                           data=None if body is None else json.dumps(body),
                                           content_type="application/json")
        w.last_text = response.get_data(as_text=True)
        return response

    def lingarr():
        return next(item for item in call("get", "state").json["instances"] if item["plugin_id"] == "lingarr")

    def save(values=None, **extra):
        body = {"expected_revision": lingarr()["revision"], **extra}
        if values is not None:
            body["values"] = values
        return call("patch", f"engines/{lingarr()['id']}", body)

    w.call, w.lingarr, w.save = call, lingarr, save
    return w


def _nothing_leaked(world, caplog):
    for secret in (TOKEN, OTHER_TOKEN):
        assert secret not in world.last_text
        assert secret not in caplog.text
        assert secret not in world.call("get", "state").get_data(as_text=True)


# ------------------------------------------------------------------- listing

def test_lingarr_is_listed_as_an_engine_with_its_small_schema(world):
    state = world.call("get", "state").json
    (engine,) = state["instances"]
    assert (engine["plugin_id"], engine["name"], engine["legacy"], engine["official"]) == (
        "lingarr", "Lingarr", True, False)
    assert engine["enabled"] is True and engine["configured"] is True and engine["revision"] == 1
    assert engine["connections"] == [] and engine["awaiting_secrets"] is False
    assert engine["plugin"] == {"name": "Lingarr", "version": "built in", "active": True, "update_version": None}
    fields = {item["key"]: item for item in engine["schema"]["fields"]}
    assert set(fields) == {"lingarr_url", "lingarr_token"}
    assert fields["lingarr_url"]["destination"] is True and fields["lingarr_token"]["type"] == "secret"
    assert engine["schema"]["backend_types"] == {}
    assert engine["values"] == {"lingarr_url": legacy_engines.DEFAULT_URL} and engine["secrets"] == {}


def test_listing_it_twice_keeps_one_engine(world):
    world.call("get", "state")
    world.call("get", "state")
    assert len(world.store.list_instances("lingarr")) == 1


def test_a_service_that_does_not_ask_for_legacy_engines_lists_none(world):
    world.legacy = False
    assert world.call("get", "state").json["instances"] == []


def test_it_is_configured_once_its_address_is_set_and_not_before(world):
    world.fake.address = ""
    engine = world.lingarr()
    assert engine["configured"] is False and engine["values"] == {"lingarr_url": ""}
    world.fake.address = URL
    assert world.lingarr()["configured"] is True
    # The token is optional, as it always was.
    assert world.fake.token == ""


def test_a_turned_off_engine_is_not_configured(world):
    assert world.save(enabled=False).status_code == 200
    assert world.lingarr()["configured"] is False


def test_the_seam_for_dispatch_names_the_in_core_service():
    engine = legacy_engines.legacy_engine_for("lingarr")
    assert (engine.plugin_id, engine.translator_type) == ("lingarr", "lingarr")
    assert legacy_engines.legacy_engine_for("ai_translator") is None
    assert legacy_engines.legacy_engine_for(None) is None
    # The type it names is one the old path's factory builds a service for.
    from subtitles.tools.translate.services import translator_factory

    assert f"'{engine.translator_type}'" in open(translator_factory.__file__).read()


def test_the_availability_gate_and_the_engine_agree_on_when_lingarr_is_ready(monkeypatch):
    from app.config import settings
    from subtitles.tools.translate.availability import translation_available

    monkeypatch.setattr(settings.translator, "lingarr_url", "")
    assert legacy_engines.configured() is False and translation_available("lingarr").available is False
    monkeypatch.setattr(settings.translator, "lingarr_url", URL)
    assert legacy_engines.configured() is True and translation_available("lingarr").available is True


# ----------------------------------------------------------------- selection

def test_it_can_be_chosen_as_the_engine_and_as_the_fallback(world):
    engine_id = world.lingarr()["id"]
    assert world.call("put", "pointers", {"engine": engine_id}).json["engine"] == engine_id
    assert world.call("get", "state").json["engine"] == engine_id
    assert world.call("put", "pointers", {"engine": None, "fallback_engine": engine_id}).json == {
        "engine": None, "fallback_engine": engine_id, "notices": []}


def test_it_cannot_be_both_the_engine_and_the_fallback(world):
    engine_id = world.lingarr()["id"]
    world.call("put", "pointers", {"engine": engine_id})
    assert world.call("put", "pointers", {"fallback_engine": engine_id}).status_code == 400


def test_turning_it_off_lets_the_pointer_go_with_a_notice(world):
    engine_id = world.lingarr()["id"]
    world.call("put", "pointers", {"engine": engine_id})
    response = world.save(enabled=False)
    assert response.status_code == 200
    assert [item["code"] for item in response.json["notices"]] == ["engine_reset"]
    assert world.call("get", "state").json["engine"] is None


# ------------------------------------------------------------ settings and secrets

def test_the_token_goes_to_the_existing_setting_and_never_comes_back(world, caplog):
    caplog.set_level(logging.DEBUG)
    response = world.save({"lingarr_token": TOKEN})
    assert response.status_code == 200, response.json
    assert world.fake.token == TOKEN
    assert response.json["engine"]["secrets"] == {"lingarr_token": {"configured": True, "stale": False}}
    assert "lingarr_token" not in response.json["engine"]["values"]
    _nothing_leaked(world, caplog)
    # Nothing of it is in the runtime's own tables.
    from app.database import TableTranslatorSecret

    assert world.session.query(TableTranslatorSecret).count() == 0
    assert world.store.get_instance(world.lingarr()["id"]).settings_json == "{}"


def test_an_empty_token_keeps_the_stored_one_and_a_new_one_replaces_it(world, caplog):
    world.save({"lingarr_token": TOKEN})
    world.save({"lingarr_token": "", "lingarr_url": legacy_engines.DEFAULT_URL})
    assert world.fake.token == TOKEN
    world.save({"lingarr_token": OTHER_TOKEN})
    assert world.fake.token == OTHER_TOKEN
    _nothing_leaked(world, caplog)


def test_clearing_the_token_removes_it(world):
    world.save({"lingarr_token": TOKEN})
    response = world.save(clear_secrets=["lingarr_token"])
    assert response.status_code == 200 and response.json["engine"]["secrets"] == {}
    assert world.fake.token == ""
    assert world.save(clear_secrets=["lingarr_url"]).status_code == 400


def test_a_new_address_needs_consent_in_the_same_request(world):
    response = world.save({"lingarr_url": URL})
    assert response.status_code == 409 and response.json["error"] == "consent_required"
    assert response.json["destinations"] == [URL] and world.fake.address == legacy_engines.DEFAULT_URL
    assert world.lingarr()["revision"] == 1
    saved = world.save({"lingarr_url": URL}, accept_destinations=[URL])
    assert saved.status_code == 200 and world.fake.address == URL
    assert saved.json["engine"]["values"] == {"lingarr_url": URL} and saved.json["engine"]["revision"] == 2


def test_the_default_address_needs_no_consent(world):
    world.save({"lingarr_url": URL}, accept_destinations=[URL])
    assert world.save({"lingarr_url": ""}).status_code == 200
    assert world.fake.address == legacy_engines.DEFAULT_URL


def test_a_changed_address_never_keeps_the_stored_token(world, caplog):
    world.save({"lingarr_token": TOKEN})
    world.save({"lingarr_url": URL}, accept_destinations=[URL])
    assert world.fake.token == ""
    assert world.lingarr()["secrets"] == {}
    _nothing_leaked(world, caplog)


def test_a_changed_address_with_a_new_token_keeps_the_new_one(world):
    world.save({"lingarr_url": URL, "lingarr_token": TOKEN}, accept_destinations=[URL])
    assert (world.fake.address, world.fake.token) == (URL, TOKEN)


def test_the_same_address_keeps_the_token(world):
    world.save({"lingarr_url": URL, "lingarr_token": TOKEN}, accept_destinations=[URL])
    world.save({"lingarr_url": URL.upper().replace("HTTP", "http")})
    assert world.fake.token == TOKEN


@pytest.mark.parametrize("values,field", [
    ({"lingarr_url": "ftp://host"}, "lingarr_url"),
    ({"lingarr_url": "http://user:pw@host:9876"}, "lingarr_url"),
    ({"lingarr_url": "not a url"}, "lingarr_url"),
    ({"lingarr_url": 5}, "lingarr_url"),
    ({"lingarr_url": "http://h/" + "a" * 3000}, "lingarr_url"),
    ({"lingarr_token": 5}, "lingarr_token"),
    ({"lingarr_token": ["a"]}, "lingarr_token"),
    ({"unknown": 1}, "unknown"),
])
def test_an_invalid_value_is_refused_by_field_and_nothing_is_saved(world, values, field, caplog):
    response = world.save(values, accept_destinations=["http://h:1"])
    assert response.status_code == 400 and response.json["error"] == "invalid"
    assert field in response.json["field_errors"]
    assert "pw" not in json.dumps(response.json["field_errors"]).replace("password", "")
    assert world.lingarr()["revision"] == 1 and world.fake.address == legacy_engines.DEFAULT_URL


def test_a_save_without_the_loaded_revision_is_refused_and_a_stale_one_is_409(world):
    engine_id = world.lingarr()["id"]
    missing = world.call("patch", f"engines/{engine_id}", {"values": {"lingarr_token": TOKEN}})
    assert missing.status_code == 400 and missing.json["error"] == "expected_revision_required"
    assert world.save({"lingarr_token": TOKEN}).status_code == 200
    stale = world.call("patch", f"engines/{engine_id}", {"expected_revision": 1, "values": {"lingarr_token": OTHER_TOKEN}})
    assert stale.status_code == 409 and stale.json["error"] == "stale_revision" and stale.json["revision"] == 2
    assert world.fake.token == TOKEN


def test_settings_that_cannot_be_written_leave_everything_as_it_was(world, caplog):
    world.fake.fail = True
    response = world.save({"lingarr_token": TOKEN})
    assert response.status_code == 400
    assert world.lingarr()["revision"] == 1 and world.fake.token == ""
    _nothing_leaked(world, caplog)


def test_lingarr_has_no_back_ends_to_add(world):
    response = world.call("post", f"engines/{world.lingarr()['id']}/connections", {"backend_type": "x"})
    assert response.status_code == 400


# --------------------------------------------------------------- the switch

@pytest.mark.parametrize("method,path,body", [
    ("get", "state", None), ("put", "pointers", {"engine": 1}), ("patch", "engines/1", {"values": {}})])
def test_the_routes_that_serve_it_are_404_while_the_switch_is_off(world, monkeypatch, method, path, body):
    monkeypatch.setattr(world.runtime, "runtime_v2_enabled", lambda: False)
    assert world.call(method, path, body).status_code == 404
    assert world.call(method, path, body, key=False).status_code == 404
    # Nothing was created behind the refusal.
    assert world.store.list_instances("lingarr") == []
    assert world.fake.token == "" and world.fake.address == legacy_engines.DEFAULT_URL


def test_the_config_backed_settings_are_the_existing_lingarr_keys(monkeypatch):
    import app.config as config
    from app.config import settings

    store = legacy_engines.ConfigLingarr()
    before = (settings.translator.lingarr_url, settings.translator.lingarr_token)
    monkeypatch.setattr(config, "write_config", lambda *args, **kwargs: False)
    assert store.set(URL, TOKEN) is False
    assert (settings.translator.lingarr_url, settings.translator.lingarr_token) == before
    monkeypatch.setattr(config, "write_config", lambda *args, **kwargs: True)
    assert store.set(URL, TOKEN) is True
    assert (settings.translator.lingarr_url, settings.translator.lingarr_token) == (URL, TOKEN)
    assert store.url() == URL and store.token_set() is True
    assert store.set(URL, None) is True and settings.translator.lingarr_token == TOKEN
    settings.translator.lingarr_url, settings.translator.lingarr_token = before

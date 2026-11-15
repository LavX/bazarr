"""The state snapshot's catalog fields: engines that could be installed, and a newer release of an installed one.

The hub's own rules decide both. An engine is offered only when its source may install it and this host can run
it, and an update is shown only when the Updates tab would offer it, so the page never names a release the hub
would not stage.
"""
import json

import pytest
from flask import Blueprint, Flask
from flask_restx import Api

from plugin_platform_helpers import service_manifest
from subtitles.tools.translate.runtime import catalog_view
from subtitles.tools.translate.runtime.settings_service import InstalledPlugin, TranslatorSettingsService
from subtitles.tools.translate.runtime.store import OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore
from test_provider_hub import _manifest as provider_manifest

API_KEY = "translation-state-test-key"  # pragma: allowlist secret
PLUGIN = OFFICIAL_ENGINE_PLUGIN_ID
GOOGLE = "google_translate"


@pytest.fixture(autouse=True)
def translator_kind():
    from provider_hub import kinds
    from provider_hub.kinds.translator import register_translator_kind

    before = kinds._KINDS.pop("translator", None)
    register_translator_kind()
    try:
        yield
    finally:
        kinds._KINDS.pop("translator", None)
        if before is not None:
            kinds._KINDS["translator"] = before


@pytest.fixture
def stageable(monkeypatch):
    """The installer accepts translator manifests, which the real one does not yet, so the tests that
    list what the catalog offers can see it. The tests that need the real refusal do not use this."""
    import provider_hub.service as hub

    monkeypatch.setattr(hub, "real_validate_manifest", hub.validate_manifest, raising=False)
    monkeypatch.setattr(hub, "validate_manifest", lambda manifest, **kwargs: None)


def _ai_manifest(version="2.2.0", **extra):
    return service_manifest(
        PLUGIN, version, kind="translator", kind_api="bazarr.translator.v1", name="AI Subtitle Translator",
        settings_version=1,
        settings={"fields": [], "backend_types": {
            "openrouter": {"label": "OpenRouter", "endpoints": ["https://openrouter.ai:443"],
                           "fields": [{"key": "api_key", "type": "secret"}]},
            "lmstudio": {"label": "LM Studio", "fields": [
                {"key": "base_url", "type": "text", "destination": True, "default": "http://localhost:1234/v1"}]}}},
        **extra)


def _google_manifest(version="1.0.0", **extra):
    fields = {"description": "Free, with no key. Plainer translations than the AI engine.", **extra}
    return service_manifest(
        GOOGLE, version, kind="translator", kind_api="bazarr.translator.v1", name="Google Translate",
        settings_version=1, endpoints=["https://translate.googleapis.com:443"], settings={"fields": []}, **fields)


def _entry(manifest, source="official", trusted=True):
    return {"source": source, "source_name": source.title(), "provider_id": manifest["id"], "name": manifest["name"],
            "version": manifest["version"], "trusted": trusted, "manifest": manifest, "catalog_list": "plugins"}


def _state(tmp_path, monkeypatch, entries=(), installations=None):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "catalog_entries": {f"{e['source']}:{e['provider_id']}:{e['version']}": e for e in entries},
        "installations": installations or {}, "jobs": []}), encoding="utf-8")
    monkeypatch.setenv("BAZARR_PROVIDER_HUB_STATE", str(state_file))


# ----------------------------------------------------------------- catalog view

def test_an_engine_is_described_for_the_page_with_its_destinations_and_no_key_needed():
    item = catalog_view.describe_entry(_entry(_google_manifest()))
    assert item == {
        "plugin_id": GOOGLE, "name": "Google Translate", "version": "1.0.0", "source": "official",
        "official": False, "free": True, "destinations": ["https://translate.googleapis.com"],
        "description": "Free, with no key. Plainer translations than the AI engine."}


def test_the_official_engine_needs_a_key_and_lists_every_address_it_may_send_to():
    item = catalog_view.describe_entry(_entry(_ai_manifest()))
    assert item["official"] is True and item["free"] is False
    assert item["destinations"] == ["http://localhost:1234", "https://openrouter.ai"]
    assert "description" not in item


def test_a_description_is_trimmed_and_a_manifest_that_does_not_parse_is_skipped():
    long_text = "word " * 200
    assert len(catalog_view.describe_entry(_entry(_google_manifest(description=long_text)))["description"]) <= 300
    broken = _google_manifest()
    broken["kind_api"] = "not-a-kind-api"
    assert catalog_view.describe_entry(_entry(broken)) is None


# ---------------------------------------------------------------- the hub side

def test_an_uninstalled_engine_is_installable_and_an_installed_one_is_not(tmp_path, monkeypatch, stageable):
    from provider_hub.service import installable_catalog_plugins

    _state(tmp_path, monkeypatch, [_entry(_ai_manifest()), _entry(_google_manifest())],
           installations={GOOGLE: {"provider_id": GOOGLE, "name": "Google Translate", "active_version": "1.0.0",
                                   "state": "active", "kind": "translator"}})
    assert [entry["provider_id"] for entry in installable_catalog_plugins("translator")] == [PLUGIN]


def test_the_newest_version_of_each_engine_is_the_one_offered(tmp_path, monkeypatch, stageable):
    from provider_hub.service import installable_catalog_plugins

    _state(tmp_path, monkeypatch, [_entry(_google_manifest("1.0.0")), _entry(_google_manifest("1.2.0")),
                                   _entry(_google_manifest("1.1.0"))])
    (entry,) = installable_catalog_plugins("translator")
    assert entry["version"] == "1.2.0"


def test_a_subtitle_provider_is_not_an_engine_and_a_trusted_source_wins_a_tie(tmp_path, monkeypatch, stageable):
    from provider_hub.service import installable_catalog_plugins

    provider = provider_manifest(provider_id="examplehub", version="1.0.0")
    entry = {"source": "official", "source_name": "Official", "provider_id": "examplehub", "name": "Example",
             "version": "1.0.0", "trusted": True, "manifest": provider}
    _state(tmp_path, monkeypatch, [entry, _entry(_google_manifest(), source="community", trusted=False),
                                   _entry(_google_manifest(), source="official", trusted=True)])
    (found,) = installable_catalog_plugins("translator")
    assert (found["provider_id"], found["source"]) == (GOOGLE, "official")


def test_an_engine_the_install_path_would_refuse_is_not_offered(tmp_path, monkeypatch):
    from provider_hub.service import installable_catalog_plugins, stage_install

    manifest = _google_manifest()
    _state(tmp_path, monkeypatch, [_entry(manifest)])
    # The install path refuses a translator manifest, so the page must not start that install.
    with pytest.raises(ValueError):
        stage_install(manifest, source_id="official")
    assert installable_catalog_plugins("translator") == []
    assert catalog_view.available_engines() == []


def test_an_entry_whose_manifest_is_not_a_valid_translator_is_not_offered(tmp_path, monkeypatch, stageable):
    from provider_hub.service import installable_catalog_plugins

    wrong_api = _google_manifest(version="1.0.1")
    wrong_api["kind_api"] = "bazarr.subtitle_provider.v1"
    _state(tmp_path, monkeypatch, [_entry(wrong_api), _entry(_ai_manifest())])
    assert [entry["provider_id"] for entry in installable_catalog_plugins("translator")] == [PLUGIN]


def test_the_update_a_page_shows_is_the_one_the_updates_tab_offers(tmp_path, monkeypatch):
    from provider_hub.service import check_updates, offered_update_version

    installed = provider_manifest(provider_id="examplehub", version="1.0.0")
    newer = provider_manifest(provider_id="examplehub", version="1.1.0")
    row = {"provider_id": "examplehub", "name": "Example", "active_version": "1.0.0", "active_path": "/old",
           "python_path": "/old/python", "state": "active", "pending_restart": False, "source_bound": True,
           "source_id": "official", "manifest": installed}
    entry = {"source": "official", "source_name": "Official", "provider_id": "examplehub", "name": "Example",
             "version": "1.1.0", "trusted": True, "manifest": newer}
    _state(tmp_path, monkeypatch, [entry], installations={"examplehub": row})
    assert offered_update_version("examplehub") == "1.1.0"
    assert check_updates()["details"]["updates_available"] == 1
    assert offered_update_version("missing") is None
    row["state"] = "removed"
    _state(tmp_path, monkeypatch, [entry], installations={"examplehub": row})
    assert offered_update_version("examplehub") is None


def test_an_update_the_hub_does_not_stage_for_a_translator_is_not_announced(tmp_path, monkeypatch):
    from provider_hub.service import offered_update_version

    row = {"provider_id": PLUGIN, "name": "AI", "active_version": "2.2.0", "active_path": "/old", "state": "active",
           "pending_restart": False, "source_bound": True, "source_id": "official", "kind": "translator",
           "manifest": _ai_manifest("2.2.0")}
    _state(tmp_path, monkeypatch, [_entry(_ai_manifest("2.3.0"))], installations={PLUGIN: row})
    assert offered_update_version(PLUGIN) is None


def test_a_catalog_that_cannot_be_read_leaves_the_list_empty_and_the_chip_off(monkeypatch):
    import provider_hub.service as hub

    def broken(*args, **kwargs):
        raise OSError("no state")

    monkeypatch.setattr(hub, "installable_catalog_plugins", broken)
    monkeypatch.setattr(hub, "offered_update_version", broken)
    assert catalog_view.available_engines() == []
    assert catalog_view.update_version(PLUGIN) is None


def test_an_engine_bazarr_runs_itself_is_never_offered_from_the_catalog(monkeypatch):
    import provider_hub.service as hub

    lingarr = service_manifest("lingarr", kind="translator", kind_api="bazarr.translator.v1", name="Lingarr",
                               settings_version=1, settings={"fields": []})
    monkeypatch.setattr(hub, "installable_catalog_plugins",
                        lambda kind: [_entry(lingarr), _entry(_google_manifest())])
    assert [item["plugin_id"] for item in catalog_view.available_engines()] == [GOOGLE]


def test_the_official_engine_is_listed_first(monkeypatch):
    import provider_hub.service as hub

    monkeypatch.setattr(hub, "installable_catalog_plugins",
                        lambda kind: [_entry(_google_manifest()), _entry(_ai_manifest())])
    assert [item["plugin_id"] for item in catalog_view.available_engines()] == [PLUGIN, GOOGLE]


# -------------------------------------------------------------------- the route

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
    from app.config import settings

    monkeypatch.setattr(settings.auth, "apikey", API_KEY)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)
    plugins = {PLUGIN: InstalledPlugin(PLUGIN, "AI Subtitle Translator", "2.2.0", True, _ai_manifest())}
    store = TranslatorStore(schema_session, pointers=MemoryPointers(), plugin_active=lambda pid: pid in plugins)

    class World:
        pass

    w = World()
    w.available, w.updates, w.hooks = [], {}, {}
    monkeypatch.setattr(module, "get_service", lambda: TranslatorSettingsService(
        store, plugin_lookup=plugins.get, catalog_engines=lambda: w.available,
        update_lookup=lambda plugin_id: w.updates.get(plugin_id), **w.hooks))
    blueprint = Blueprint("translation_state_test", __name__, url_prefix="/api")
    Api(blueprint, authorizations={"apikey": {"type": "apiKey", "in": "header", "name": "X-API-KEY"}},
        security="apikey").add_namespace(module.api_ns_translation, "/")
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    client = app.test_client()
    store.create_instance(PLUGIN, name="AI", settings_version=1, plugin_version="2.2.0")
    w.state = lambda: client.get("/api/translation/state", headers={"X-API-KEY": API_KEY}).json
    return w


def test_the_state_lists_the_routes_that_work_and_leaves_test_out_until_something_can_answer_one(world):
    state = world.state()
    assert state["capabilities"] == ["summary", "limits", "queue"]
    assert state["available_engines"] == []
    world.hooks["connection_probe"] = lambda request: {"ok": True}
    assert world.state()["capabilities"] == ["summary", "limits", "test", "queue"]


def test_the_state_carries_the_engines_the_catalog_offers_as_the_page_reads_them(world):
    world.available = [catalog_view.describe_entry(_entry(_google_manifest()))]
    (engine,) = world.state()["available_engines"]
    assert (engine["plugin_id"], engine["source"], engine["version"]) == (GOOGLE, "official", "1.0.0")
    assert engine["free"] is True and engine["destinations"] == ["https://translate.googleapis.com"]


def test_an_instance_shows_the_release_the_hub_offers_and_null_when_it_offers_none(world):
    assert world.state()["instances"][0]["plugin"]["update_version"] is None
    world.updates[PLUGIN] = "2.3.0"
    plugin = world.state()["instances"][0]["plugin"]
    assert plugin == {"name": "AI Subtitle Translator", "version": "2.2.0", "active": True, "update_version": "2.3.0"}

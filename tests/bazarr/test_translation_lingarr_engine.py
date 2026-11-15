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
        self.token_value = ""
        self.fail = False
        self.was_selected = False
        self.depth = 0
        self.calls = []

    def lock(self):
        import contextlib

        @contextlib.contextmanager
        def held():
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

        return held()

    def url(self):
        self.calls.append(("url", self.depth))
        return self.address

    def token(self):
        return self.token_value

    def selected(self):
        return self.was_selected

    def set(self, url, token):
        self.calls.append(("set", self.depth))
        if self.fail:
            return False
        self.address = url
        if token is not None:
            self.token_value = token
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

def test_lingarr_is_listed_in_exactly_the_shape_the_page_reads(world):
    state = world.call("get", "state").json
    (engine,) = state["instances"]
    assert engine == {
        "id": engine["id"], "plugin_id": "lingarr", "name": "Lingarr", "official": False, "legacy": True,
        "description": "Sends each job to your own Lingarr service.", "enabled": True, "revision": 1,
        "awaiting_secrets": False, "configured": False, "settings_version": 1, "plugin_version": None,
        "plugin": None,
        "schema": {
            "settings_version": 1, "endpoints": [], "backend_types": {},
            "fields": [
                {"key": "url", "type": "text", "label": "Lingarr URL", "required": True, "destination": True,
                 "placeholder": "http://lingarr:9876", "help": "The address Bazarr+ reaches your Lingarr at."},
                {"key": "token", "type": "secret", "label": "API token", "help": "Only when your Lingarr asks for one."},
            ]},
        "values": {"url": "http://lingarr:9876"}, "secrets": {}, "connections": []}
    assert isinstance(engine["id"], int)


def test_a_stored_token_is_reported_as_configured_with_its_last_four_and_nothing_more(world, caplog):
    world.fake.token_value = TOKEN
    assert world.lingarr()["secrets"] == {"token": {"configured": True, "last4": TOKEN[-4:]}}
    world.fake.token_value = "short"
    assert world.lingarr()["secrets"] == {"token": {"configured": True}}
    world.fake.token_value = TOKEN
    _nothing_leaked(world, caplog)


def test_listing_it_twice_keeps_one_engine(world):
    world.call("get", "state")
    world.call("get", "state")
    assert len(world.store.list_instances("lingarr")) == 1


def test_a_service_that_does_not_ask_for_legacy_engines_lists_none(world):
    world.legacy = False
    assert world.call("get", "state").json["instances"] == []


def test_the_shipped_default_address_alone_is_not_set_up(world):
    assert world.fake.address == legacy_engines.DEFAULT_URL
    assert world.lingarr()["configured"] is False


def test_it_is_configured_once_the_person_has_set_an_address(world):
    world.fake.address = ""
    assert world.lingarr()["configured"] is False
    world.fake.address = URL
    assert world.lingarr()["configured"] is True
    # The token is optional, as it always was.
    assert world.fake.token_value == ""


def test_saving_the_default_address_from_the_page_sets_it_up_and_needs_no_consent(world):
    saved = world.save({"url": legacy_engines.DEFAULT_URL})
    assert saved.status_code == 200 and saved.json["engine"]["configured"] is True
    assert world.store.get_instance(saved.json["engine"]["id"]).settings_json == '{"address_saved": true}'
    assert world.lingarr()["configured"] is True


def test_an_install_that_already_ran_lingarr_is_set_up_without_a_save(world):
    world.fake.was_selected = True
    assert world.lingarr()["configured"] is True


def test_a_turned_off_engine_is_not_configured(world):
    world.fake.address = URL
    assert world.lingarr()["configured"] is True
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


def test_the_availability_gate_and_the_engine_agree_on_an_address_the_person_set(monkeypatch):
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
    response = world.save({"token": TOKEN})
    assert response.status_code == 200, response.json
    assert world.fake.token_value == TOKEN
    assert response.json["engine"]["secrets"] == {"token": {"configured": True, "last4": TOKEN[-4:]}}
    assert "token" not in response.json["engine"]["values"]
    _nothing_leaked(world, caplog)
    # Nothing of it is in the runtime's own tables.
    from app.database import TableTranslatorSecret

    assert world.session.query(TableTranslatorSecret).count() == 0
    assert world.store.get_instance(world.lingarr()["id"]).settings_json == "{}"


def test_an_empty_token_keeps_the_stored_one_and_a_new_one_replaces_it(world, caplog):
    world.save({"token": TOKEN})
    world.save({"token": ""})
    assert world.fake.token_value == TOKEN
    world.save({"token": OTHER_TOKEN})
    assert world.fake.token_value == OTHER_TOKEN
    _nothing_leaked(world, caplog)


def test_clearing_the_token_removes_it(world):
    world.save({"token": TOKEN})
    response = world.save(clear_secrets=["token"])
    assert response.status_code == 200 and response.json["engine"]["secrets"] == {}
    assert world.fake.token_value == ""
    assert world.save(clear_secrets=["url"]).status_code == 400


def test_a_new_address_needs_consent_in_the_same_request(world):
    response = world.save({"url": URL})
    assert response.status_code == 409 and response.json["error"] == "consent_required"
    assert response.json["destinations"] == [URL] and world.fake.address == legacy_engines.DEFAULT_URL
    assert world.lingarr()["revision"] == 1
    saved = world.save({"url": URL}, accept_destinations=[URL])
    assert saved.status_code == 200 and world.fake.address == URL
    assert saved.json["engine"]["values"] == {"url": URL} and saved.json["engine"]["revision"] == 2


def test_an_emptied_address_is_refused_as_required_and_nothing_changes(world):
    response = world.save({"url": ""})
    assert response.status_code == 400 and response.json["field_errors"] == {"url": "is required"}
    assert world.fake.address == legacy_engines.DEFAULT_URL and world.lingarr()["revision"] == 1


def test_going_back_to_the_default_address_follows_the_same_consent_rule(world):
    world.save({"url": URL}, accept_destinations=[URL])
    refused = world.save({"url": legacy_engines.DEFAULT_URL})
    assert refused.status_code == 409 and refused.json["destinations"] == [legacy_engines.DEFAULT_URL]
    assert world.save({"url": legacy_engines.DEFAULT_URL},
                      accept_destinations=[legacy_engines.DEFAULT_URL]).status_code == 200


def test_a_changed_address_never_keeps_the_stored_token_and_the_engine_does_not_wait_for_its_key(world, caplog):
    world.save({"token": TOKEN})
    saved = world.save({"url": URL}, accept_destinations=[URL])
    assert world.fake.token_value == ""
    assert saved.json["engine"]["secrets"] == {} and saved.json["engine"]["awaiting_secrets"] is False
    assert saved.json["engine"]["configured"] is True
    _nothing_leaked(world, caplog)


def test_a_changed_address_with_a_new_token_keeps_the_new_one(world):
    world.save({"url": URL, "token": TOKEN}, accept_destinations=[URL])
    assert (world.fake.address, world.fake.token_value) == (URL, TOKEN)


def test_the_same_address_keeps_the_token(world):
    world.save({"url": URL, "token": TOKEN}, accept_destinations=[URL])
    world.save({"url": URL.upper().replace("HTTP", "http")})
    assert world.fake.token_value == TOKEN


@pytest.mark.parametrize("values,field", [
    ({"url": "ftp://host"}, "url"),
    ({"url": "http://user:pw@host:9876"}, "url"),
    ({"url": "not a url"}, "url"),
    ({"url": 5}, "url"),
    ({"url": "http://h/" + "a" * 3000}, "url"),
    ({"token": 5}, "token"),
    ({"token": ["a"]}, "token"),
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
    missing = world.call("patch", f"engines/{engine_id}", {"values": {"token": TOKEN}})
    assert missing.status_code == 400 and missing.json["error"] == "expected_revision_required"
    assert world.save({"token": TOKEN}).status_code == 200
    stale = world.call("patch", f"engines/{engine_id}", {"expected_revision": 1, "values": {"token": OTHER_TOKEN}})
    assert stale.status_code == 409 and stale.json["error"] == "stale_revision" and stale.json["revision"] == 2
    assert world.fake.token_value == TOKEN


def test_settings_that_cannot_be_written_leave_everything_as_it_was(world, caplog):
    world.fake.fail = True
    response = world.save({"token": TOKEN})
    assert response.status_code == 400
    assert world.lingarr()["revision"] == 1 and world.fake.token_value == ""
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
    assert world.fake.token_value == "" and world.fake.address == legacy_engines.DEFAULT_URL


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
    assert store.url() == URL and store.token() == TOKEN
    assert store.set(URL, None) is True and settings.translator.lingarr_token == TOKEN
    settings.translator.lingarr_url, settings.translator.lingarr_token = before


# ----------------------------------------------------- locking, undo, ownership

def test_the_config_lock_is_held_from_the_read_of_the_old_address_to_the_write(world):
    engine_id = world.lingarr()["id"]
    world.fake.calls.clear()
    response = world.call("patch", f"engines/{engine_id}", {
        "expected_revision": 1, "values": {"url": URL}, "accept_destinations": [URL]})
    assert response.status_code == 200
    # The first read (the old address) and the write both happened with the lock held.
    # Held by the whole save (and again by the settings write, which re-enters it).
    assert world.fake.calls[0][0] == "url" and world.fake.calls[0][1] >= 1
    assert any(name == "set" for name, _ in world.fake.calls)
    assert all(depth >= 1 for name, depth in world.fake.calls if name == "set")
    assert world.fake.depth == 0


def test_a_failure_after_the_config_write_puts_the_old_settings_back(world, monkeypatch, caplog):
    from subtitles.tools.translate.runtime.store import StaleRevisionError as StoreStale

    world.save({"token": TOKEN})
    before = (world.fake.address, world.fake.token_value)

    def lose_the_race(*args, **kwargs):
        raise StoreStale("changed", 9)

    monkeypatch.setattr(world.store, "set_instance_enabled", lose_the_race)
    response = world.save({"url": URL, "token": OTHER_TOKEN}, accept_destinations=[URL], enabled=False)
    assert response.status_code == 409 and response.json["error"] == "stale_revision"
    assert (world.fake.address, world.fake.token_value) == before
    assert world.fake.depth == 0
    _nothing_leaked(world, caplog)


def test_an_undo_never_overwrites_values_a_newer_save_wrote(world, monkeypatch):
    from subtitles.tools.translate.runtime.store import StaleRevisionError as StoreStale

    def lose_the_race(*args, **kwargs):
        # While this save fails, another writer changes the address.
        world.fake.address = "http://10.9.9.9:9876"
        raise StoreStale("changed", 9)

    monkeypatch.setattr(world.store, "set_instance_enabled", lose_the_race)
    response = world.save({"url": URL}, accept_destinations=[URL], enabled=False)
    assert response.status_code == 409
    assert world.fake.address == "http://10.9.9.9:9876"


def test_the_address_is_server_owned_for_the_old_settings_route_only_while_the_runtime_is_on(monkeypatch):
    from subtitles.tools.translate import runtime

    from app.config import is_server_owned_setting, settings

    monkeypatch.setattr(settings.translator, "runtime_v2", False)
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)
    assert is_server_owned_setting("translator.lingarr_url") is False
    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    assert is_server_owned_setting("translator.lingarr_url") is True
    # The switch is set but the tables are not there yet: the same answer the translation routes give.
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: False)
    assert is_server_owned_setting("translator.lingarr_url") is False
    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: True)
    assert is_server_owned_setting("translator.lingarr_token") is False
    assert is_server_owned_setting("translator.engine") is True


# ------------------------------------------------------------ one instance only

@pytest.fixture(params=["sqlite", "postgres"])
def database(request, tmp_path):
    """A database with the runtime tables built from the models, on SQLite or a PostgreSQL schema of its own."""
    import os
    from uuid import uuid4

    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    if request.param == "sqlite":
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'engines.db'}", connect_args={"timeout": 30})
        Base.metadata.create_all(engine)
        yield engine, sessionmaker(bind=engine)
        engine.dispose()
        return
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    schema = "lingarr_" + uuid4().hex
    admin = sa.create_engine(url)
    with admin.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    # The application runs PostgreSQL in AUTOCOMMIT, where rollback() does nothing.
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")

    @sa.event.listens_for(engine, "connect")
    def _path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {schema}")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine, sessionmaker(bind=engine)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def test_many_first_listings_at_once_create_one_lingarr_engine(database):
    import threading

    import sqlalchemy as sa

    engine, make_session = database
    barrier = threading.Barrier(8)
    ids, errors = [], []

    def first_listing():
        session = make_session()
        try:
            store = TranslatorStore(session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: False)
            barrier.wait()
            ids.append(store.ensure_instance("lingarr", name="Lingarr", settings_version=1).id)
        except Exception as error:  # reported below
            errors.append(error)
        finally:
            session.close()

    threads = [threading.Thread(target=first_listing) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    assert len(ids) == 8 and len(set(ids)) == 1
    session = make_session()
    assert session.execute(sa.text(
        "SELECT count(*) FROM translator_engine_instances WHERE plugin_id = 'lingarr'")).scalar() == 1
    session.close()


def test_the_database_refuses_a_second_lingarr_engine_and_the_loser_reads_the_winner(database):
    import sqlalchemy as sa

    engine, make_session = database
    session = make_session()
    store = TranslatorStore(session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: False)
    first_id = store.create_instance("lingarr", name="Lingarr").id
    with pytest.raises(sa.exc.IntegrityError):
        store.create_instance("lingarr", name="Lingarr again")
    session.rollback()
    # Another plugin id is not limited to one instance.
    store.create_instance("example_engine")
    store.create_instance("example_engine")
    assert store.ensure_instance("lingarr").id == first_id

    # A caller that lost the race (its check saw none, its insert hit the index) returns the winner's row.
    real_list = store.list_instances
    seen = []

    def blind_first(plugin_id=None):
        seen.append(plugin_id)
        return [] if len(seen) <= 2 and plugin_id == "lingarr" else real_list(plugin_id)

    store.list_instances = blind_first
    assert store.ensure_instance("lingarr").id == first_id
    session.close()


# ----------------------------------------------------------------- lock order

def test_a_config_writer_that_then_writes_a_row_never_deadlocks_with_an_engine_save(database):
    """A writer holds the config lock and then writes a row, while an engine save runs. With the config lock
    taken before the transaction the save waits its turn; with the transaction first, the two wait for each
    other for ever. The interleaving is forced: the writer takes the lock first, the save signals that it has
    reached for the lock, and only then does the writer write."""
    import threading

    import sqlalchemy as sa

    from app.database import TableTranslatorEngineInstance

    engine, make_session = database
    config_lock = threading.RLock()
    save_reached_the_lock = threading.Event()
    writer_holds_the_lock = threading.Event()

    class OneLockLingarr(FakeLingarr):
        def lock(self):
            save_reached_the_lock.set()
            return config_lock

    fake = OneLockLingarr()

    def build(session):
        store = TranslatorStore(session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: True)
        return store, TranslatorSettingsService(
            store, plugin_lookup=lambda plugin_id: None, catalog_engines=lambda: [],
            update_lookup=lambda plugin_id: None, legacy=True, lingarr=fake)

    setup = make_session()
    store, service = build(setup)
    service.snapshot()
    lingarr_id = store.list_instances("lingarr")[0].id
    revision = setup.get(TableTranslatorEngineInstance, lingarr_id).revision
    failures = []

    def writer():
        session = make_session()
        try:
            own, _ = build(session)
            with config_lock:
                writer_holds_the_lock.set()
                if not save_reached_the_lock.wait(10):
                    failures.append(("writer", "the save never reached for the lock"))
                own.create_instance("example_engine")
        except Exception as error:  # reported below
            failures.append(("writer", error))
        finally:
            session.close()

    def save():
        session = make_session()
        try:
            _, own = build(session)
            writer_holds_the_lock.wait(10)
            own.save_engine(lingarr_id, {"expected_revision": revision, "values": {"url": URL},
                                         "accept_destinations": [URL]})
        except Exception as error:
            failures.append(("save", error))
        finally:
            session.close()

    threads = [threading.Thread(target=target, daemon=True) for target in (writer, save)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads), "deadlock between the config lock and the transaction"
    assert failures == []
    assert fake.address == URL
    count = setup.execute(sa.text("SELECT count(*) FROM translator_engine_instances WHERE plugin_id = "
                                  "'example_engine'")).scalar()
    assert count == 1
    setup.close()


def test_a_turn_off_that_releases_a_pointer_takes_the_config_lock_before_its_transaction(database):
    import threading

    engine, make_session = database
    config_lock = threading.RLock()

    class LockedPointers(MemoryPointers):
        def lock(self):
            return config_lock

    pointers = LockedPointers()
    session = make_session()
    store = TranslatorStore(session, pointers=pointers, plugin_active=lambda plugin_id: True)
    other = store.create_instance("example_engine")
    pointers.engine = other.id
    holder_has_lock, release = threading.Event(), threading.Event()
    done = []

    def holder():
        with config_lock:
            holder_has_lock.set()
            release.wait(10)

    def turn_off():
        own = make_session()
        try:
            TranslatorStore(own, pointers=pointers, plugin_active=lambda plugin_id: True).set_instance_enabled(
                other.id, False)
            done.append(True)
        finally:
            own.close()

    first = threading.Thread(target=holder, daemon=True)
    first.start()
    holder_has_lock.wait(10)
    second = threading.Thread(target=turn_off, daemon=True)
    second.start()
    second.join(0.5)
    # While another writer holds the config lock, the turn-off waits for it without holding a write
    # transaction, so a writer that is inside one is not blocked by it.
    other_session = make_session()
    other_store = TranslatorStore(other_session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: True)
    other_store.create_instance("example_engine")
    other_session.close()
    assert done == []
    release.set()
    second.join(20)
    first.join(20)
    assert done == [True] and pointers.engine is None
    session.close()

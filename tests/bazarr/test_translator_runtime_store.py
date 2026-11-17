"""Engine instances, connections, secrets and the engine pointers, on SQLite and PostgreSQL."""
import json
import os
from uuid import uuid4

import pytest

from provider_hub.manifest import ManifestValidationError  # noqa: F401  (import order guard)
from subtitles.tools.translate.runtime.destinations import (
    DestinationError, DestinationSchema, added_destinations, covers, effective_destinations, normalize_destination,
    schema_for_backend, schema_for_engine)
from subtitles.tools.translate.runtime.store import (
    StaleRevisionError, CONNECTION, INSTANCE, OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore, TranslatorStoreError)

OPENAI = DestinationSchema(fields=({"key": "base_url", "type": "text", "destination": True,
                                    "default": "https://api.openai.com/v1"},
                                   {"key": "api_key", "type": "secret"}),
                           endpoints=())
KEY = "sk-test-0123456789abcdef-ABCD"  # pragma: allowlist secret


class MemoryPointers:
    def __init__(self, engine=None, fallback=None, writable=True):
        self.engine, self.fallback, self.writable = engine, fallback, writable

    def get(self):
        return self.engine, self.fallback

    def set(self, engine, fallback):
        if not self.writable:
            return False
        self.engine, self.fallback = engine, fallback
        return True


def _make_session(kind, tmp_path):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import (Base, TableTranslatorConnection, TableTranslatorEngineInstance,
                              TableTranslatorSecret)
    tables = [TableTranslatorEngineInstance.__table__, TableTranslatorConnection.__table__,
              TableTranslatorSecret.__table__]
    if kind == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'store.db'}")

        @event.listens_for(engine, "connect")
        def _fk(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine, tables=tables)
        return engine, scoped_session(sessionmaker(bind=engine)), None
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL lane")
    import sqlalchemy as sa
    schema = "translator_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    # The application runs PostgreSQL in AUTOCOMMIT, where rollback() does nothing.
    engine = create_engine(url, isolation_level="AUTOCOMMIT")

    @event.listens_for(engine, "connect")
    def _path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {schema}")
        cursor.close()

    Base.metadata.create_all(engine, tables=tables)
    return engine, scoped_session(sessionmaker(bind=engine)), (admin, schema)


@pytest.fixture(params=["sqlite", "postgres"])
def session(request, tmp_path):
    import sqlalchemy as sa
    engine, scoped, pg = _make_session(request.param, tmp_path)
    try:
        yield scoped
    finally:
        scoped.remove()
        engine.dispose()
        if pg:
            admin, schema = pg
            with admin.begin() as connection:
                connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
            admin.dispose()


@pytest.fixture
def active_plugins():
    return {OFFICIAL_ENGINE_PLUGIN_ID, "google_translate", "extension_backend"}


@pytest.fixture
def pointers():
    return MemoryPointers()


@pytest.fixture
def store(session, pointers, active_plugins):
    return TranslatorStore(session, pointers=pointers, plugin_active=lambda plugin_id: plugin_id in active_plugins)


def _ai_engine(store, *, with_connection=True):
    instance = store.create_instance(OFFICIAL_ENGINE_PLUGIN_ID, name="AI", settings_version=1, plugin_version="2.2.0")
    if with_connection:
        store.add_connection(instance.id, "openrouter", model="m", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    return instance


# ---------------------------------------------------------------- instances and revision

def test_an_instance_starts_at_revision_one_with_its_settings(store):
    instance = store.create_instance("google_translate", settings={"a": 1}, settings_version=3, plugin_version="1.0.0")
    assert (instance.revision, instance.enabled, instance.settings_version) == (1, 1, 3)
    assert store.settings_of(instance) == {"a": 1}


def test_every_settings_save_bumps_the_revision(store):
    instance = store.create_instance("google_translate")
    for expected in (2, 3, 4):
        result = store.save_instance_settings(instance.id, {"prompt": f"v{expected}"}, schema=DestinationSchema())
        assert result.revision == expected
    # An identical save still counts: any saved change starts pending jobs afresh, so does a re-save.
    assert store.save_instance_settings(instance.id, {"prompt": "v4"}, schema=DestinationSchema()).revision == 5


def test_a_stale_revision_is_refused(store):
    instance = store.create_instance("google_translate")
    store.save_instance_settings(instance.id, {"a": 1}, schema=DestinationSchema())
    with pytest.raises(TranslatorStoreError):
        store.save_instance_settings(instance.id, {"a": 2}, schema=DestinationSchema(), expected_revision=1)
    assert store.settings_of(store.get_instance(instance.id)) == {"a": 1}


def test_connection_changes_do_not_bump_the_instance_revision(store):
    instance = _ai_engine(store)
    connection = store.list_connections(instance.id)[0]
    store.update_connection(connection.id, schema=DestinationSchema(), model="other", enabled=False)
    second = store.add_connection(instance.id, "gemini", model="g")
    store.reorder_connections(instance.id, [second.id, connection.id])
    assert store.get_instance(instance.id).revision == 1


def test_a_plugin_has_no_limit_of_one_instance(store):
    first = store.create_instance("google_translate")
    second = store.create_instance("google_translate")
    assert first.id != second.id and len(store.list_instances("google_translate")) == 2
    assert store.ensure_instance("google_translate").id == first.id


# ---------------------------------------------------------------- connections

def test_connections_keep_their_order_and_reorder_without_tripping_the_unique_index(store):
    instance = _ai_engine(store, with_connection=False)
    ids = [store.add_connection(instance.id, "openrouter", model=str(i)).id for i in range(3)]
    assert [c.position for c in store.list_connections(instance.id)] == [0, 1, 2]
    store.reorder_connections(instance.id, [ids[2], ids[0], ids[1]])
    assert [c.id for c in store.list_connections(instance.id)] == [ids[2], ids[0], ids[1]]
    with pytest.raises(TranslatorStoreError):
        store.reorder_connections(instance.id, [ids[0], ids[1]])


def test_deleting_a_connection_closes_the_gap_and_drops_its_secrets(store):
    instance = _ai_engine(store, with_connection=False)
    a, b, c = (store.add_connection(instance.id, "openrouter", model=m) for m in "abc")
    store.set_secret((CONNECTION, b.id), "api_key", KEY, destinations=frozenset({"x"}))
    store.delete_connection(b.id)
    assert [(row.id, row.position) for row in store.list_connections(instance.id)] == [(a.id, 0), (c.id, 1)]
    assert store.secret_summary((CONNECTION, b.id), frozenset({"x"})) == {}


def test_several_connections_of_one_type_are_allowed(store):
    instance = _ai_engine(store, with_connection=False)
    store.add_connection(instance.id, "gemini", model="free")
    store.add_connection(instance.id, "gemini", model="paid")
    assert [c.backend_type for c in store.list_connections(instance.id)] == ["gemini", "gemini"]


def test_prices_default_to_free_and_reject_nonsense(store):
    instance = _ai_engine(store, with_connection=False)
    free = store.add_connection(instance.id, "openai_compatible")
    assert (free.price_input_per_million, free.price_output_per_million) == (0, 0)
    paid = store.add_connection(instance.id, "openrouter", price_input_per_million=0.15,
                                price_output_per_million=0.6)
    assert (paid.price_input_per_million, paid.price_output_per_million) == (0.15, 0.6)
    for bad in (-1, float("nan"), True, "1"):
        with pytest.raises(TranslatorStoreError):
            store.add_connection(instance.id, "openrouter", price_input_per_million=bad)


def test_deleting_an_instance_removes_connections_and_all_secrets(store):
    instance = _ai_engine(store)
    connection = store.list_connections(instance.id)[0]
    store.set_secret((INSTANCE, instance.id), "token", KEY, destinations=frozenset())
    store.set_secret((CONNECTION, connection.id), "api_key", KEY, destinations=frozenset())
    store.delete_instance(instance.id)
    assert store.get_instance(instance.id) is None
    assert store.list_connections(instance.id) == []
    assert store.secret_summary((INSTANCE, instance.id), frozenset()) == {}
    assert store.secret_summary((CONNECTION, connection.id), frozenset()) == {}


# ---------------------------------------------------------------- usable and configured

def test_a_connection_is_usable_only_when_on_served_and_not_awaiting_secrets(store, active_plugins):
    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openrouter", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    assert store.connection_usable(connection)
    store.update_connection(connection.id, schema=DestinationSchema(), enabled=False)
    assert not store.connection_usable(connection)
    store.update_connection(connection.id, schema=DestinationSchema(), enabled=True)
    active_plugins.discard(OFFICIAL_ENGINE_PLUGIN_ID)
    assert not store.connection_usable(connection)
    active_plugins.add(OFFICIAL_ENGINE_PLUGIN_ID)
    connection.awaiting_secrets = 1
    assert not store.connection_usable(connection)


def test_a_connection_of_an_extension_follows_the_extension_not_the_engine(store, active_plugins):
    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "extension_backend", plugin_id="extension_backend")
    assert store.connection_usable(connection)
    active_plugins.discard("extension_backend")
    assert not store.connection_usable(connection)


def test_the_ai_engine_is_configured_only_with_a_usable_connection(store, active_plugins):
    empty = _ai_engine(store, with_connection=False)
    assert not store.instance_configured(empty)
    connection = store.add_connection(empty.id, "openrouter", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    assert store.instance_configured(empty)
    store.update_connection(connection.id, schema=DestinationSchema(), enabled=False)
    assert not store.instance_configured(empty)


def test_a_plain_engine_is_configured_when_installed_and_on(store, active_plugins):
    google = store.create_instance("google_translate")
    assert store.instance_configured(google)
    store.set_instance_enabled(google.id, False)
    assert not store.instance_configured(google)
    store.set_instance_enabled(google.id, True)
    active_plugins.discard("google_translate")
    assert not store.instance_configured(google)


# ---------------------------------------------------------------- pointers

def test_the_engine_and_fallback_never_name_the_same_instance(store, pointers):
    ai = _ai_engine(store)
    google = store.create_instance("google_translate")
    store.set_engine(ai.id)
    store.set_fallback(google.id)
    assert pointers.get() == (ai.id, google.id)
    with pytest.raises(TranslatorStoreError):
        store.set_fallback(ai.id)
    assert pointers.get() == (ai.id, google.id)


def test_selecting_the_fallback_as_engine_resets_the_fallback_to_its_default(store, pointers):
    ai = _ai_engine(store)
    google = store.create_instance("google_translate")
    store.set_fallback(google.id)
    change = store.set_engine(google.id)
    # The official engine is configured and is not the selected engine, so it is the default.
    assert pointers.get() == (google.id, ai.id)
    assert [n.code for n in change.notices] == ["fallback_reset"]


def test_the_default_fallback_is_none_when_the_ai_engine_is_the_engine_or_not_configured(store, pointers):
    ai = _ai_engine(store)
    google = store.create_instance("google_translate")
    store.set_fallback(google.id)
    store.set_engine(ai.id)  # the fallback was google, unaffected
    assert pointers.get() == (ai.id, google.id)
    assert store.default_fallback(ai.id) is None
    empty = TranslatorStore(store._session, pointers=MemoryPointers(), plugin_active=lambda _p: True)
    for connection in empty.list_connections(ai.id):
        empty.update_connection(connection.id, schema=DestinationSchema(), enabled=False)
    assert empty.default_fallback(google.id) is None


def test_turning_off_the_selected_engine_unselects_it_with_a_notice(store, pointers):
    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    change = store.set_instance_enabled(google.id, False)
    assert pointers.get() == (None, None)
    assert [n.code for n in change.notices] == ["engine_reset"]
    assert store.set_instance_enabled(google.id, True).notices == []
    assert pointers.get() == (None, None)  # switching it back on does not select it again


def test_turning_off_the_fallback_resets_it_to_the_default(store, pointers):
    ai = _ai_engine(store)
    google = store.create_instance("google_translate")
    other = store.create_instance("google_translate")
    store.set_engine(google.id)
    store.set_fallback(other.id)
    change = store.set_instance_enabled(other.id, False)
    assert pointers.get() == (google.id, ai.id)
    assert [n.code for n in change.notices] == ["fallback_reset"]


def test_a_disabled_official_fallback_does_not_come_back_as_its_own_default(store, pointers):
    ai = _ai_engine(store)
    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    store.set_fallback(ai.id)
    store.set_instance_enabled(ai.id, False)
    assert pointers.get() == (google.id, None)


def test_uninstalling_a_plugin_removes_its_instances_and_resets_pointers(store, pointers):
    ai = _ai_engine(store)
    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    store.set_fallback(ai.id)
    change = store.remove_plugin("google_translate")
    assert store.list_instances("google_translate") == []
    assert pointers.get() == (None, ai.id)  # the fallback did not name the removed plugin
    assert {n.code for n in change.notices} == {"engine_reset"}
    store.remove_plugin(OFFICIAL_ENGINE_PLUGIN_ID)
    assert pointers.get() == (None, None)


@pytest.mark.parametrize("remove", ["instance", "plugin"])
def test_a_removal_whose_pointer_write_fails_deletes_nothing(store, pointers, remove):
    google = store.create_instance("google_translate")
    store.add_connection(google.id, "openrouter", plugin_id="google_translate")
    store.set_secret((INSTANCE, google.id), "token", KEY, destinations=frozenset())
    store.set_engine(google.id)
    pointers.writable = False  # config.yaml cannot be written
    with pytest.raises(TranslatorStoreError):
        if remove == "instance":
            store.delete_instance(google.id)
        else:
            store.remove_plugin("google_translate")
    pointers.writable = True
    assert [row.id for row in store.list_instances("google_translate")] == [google.id]
    assert len(store.list_connections(google.id)) == 1
    assert "token" in store.secret_summary((INSTANCE, google.id), frozenset())
    assert pointers.get() == (google.id, None)


def test_turning_an_engine_off_when_the_pointer_write_fails_leaves_it_on(store, pointers):
    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    pointers.writable = False
    with pytest.raises(TranslatorStoreError):
        store.set_instance_enabled(google.id, False)
    assert store.get_instance(google.id).enabled == 1 and pointers.get() == (google.id, None)
    pointers.writable = True
    store.set_instance_enabled(google.id, False)
    assert store.get_instance(google.id).enabled == 0 and pointers.get() == (None, None)


def _move_revision_elsewhere(store, model, row_id, revision):
    """Another writer got in after this one read the row: the in-memory revision keeps saying what it said."""
    from sqlalchemy import update

    store._session.execute(update(model).where(model.id == row_id).values(revision=revision)
                           .execution_options(synchronize_session=False))


def test_a_removal_that_loses_the_revision_race_deletes_neither_the_connection_nor_its_secrets(store):
    from app.database import TableTranslatorConnection

    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openrouter", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    store.set_secret((CONNECTION, connection.id), "api_key", KEY, destinations=frozenset())
    _move_revision_elsewhere(store, TableTranslatorConnection, connection.id, 9)
    with pytest.raises(StaleRevisionError):
        store.delete_connection(connection.id, expected_revision=connection.revision)
    store._session.expire_all()
    assert store.get_connection(connection.id) is not None
    assert "api_key" in store.secret_summary((CONNECTION, connection.id), frozenset())
    store.delete_connection(connection.id, expected_revision=9)
    assert store.get_connection(connection.id) is None
    assert store.secret_summary((CONNECTION, connection.id), frozenset()) == {}


def test_turning_an_engine_off_that_loses_the_revision_race_leaves_the_pointers_alone(store, pointers):
    from app.database import TableTranslatorEngineInstance

    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    _move_revision_elsewhere(store, TableTranslatorEngineInstance, google.id, 9)
    with pytest.raises(StaleRevisionError):
        store.set_instance_enabled(google.id, False, expected_revision=google.revision)
    assert pointers.get() == (google.id, None)
    store._session.expire_all()
    assert store.get_instance(google.id).enabled == 1


def test_a_plugin_update_masks_the_secret_it_moved_in_the_very_next_log_line(store, session, monkeypatch):
    from secret_store import scrub

    from subtitles.tools.translate.runtime.store import collect_secret_values

    monkeypatch.setattr("app.database.database", session)
    seen = []
    monkeypatch.setattr(scrub, "refresh_secret_values", lambda *args, **kwargs: seen.append(collect_secret_values()))
    after = DestinationSchema(fields=({"key": "token", "type": "secret"},))
    store.create_instance("example_engine", settings={"token": KEY})
    store.apply_plugin_update("example_engine", _resolver({INSTANCE: after, CONNECTION: after}))
    assert seen and KEY in seen[-1]


def test_every_plugin_update_that_changes_what_an_owner_stores_moves_its_revision(store):
    after = DestinationSchema(fields=({"key": "token", "type": "secret"}, {"key": "label", "type": "text"}))
    instance = store.create_instance("example_engine", settings={"token": KEY, "label": "x"})
    moved_only = store.create_instance("example_engine", settings={"token": "", "label": "y"})
    untouched = store.create_instance("example_engine", settings={"label": "z"})
    connection = store.add_connection(instance.id, "t", plugin_id="example_engine", settings={"label": "c"})
    store.apply_plugin_update("example_engine", _resolver({INSTANCE: after, CONNECTION: after}),
                              new_settings=lambda kind, row: {"label": "migrated"} if kind == CONNECTION else None)
    store._session.expire_all()
    assert store.get_instance(instance.id).revision == 2  # the token left its settings
    assert store.get_instance(moved_only.id).revision == 2  # an empty plaintext value left too
    assert store.get_instance(untouched.id).revision == 1  # nothing it stores changed
    assert store.get_connection(connection.id).revision == 2  # migrated settings


class SlowLockedPointers(MemoryPointers):
    """Pointers held like config.yaml is: one lock for a whole cycle, and a read that takes its time."""

    def __init__(self, engine=None, fallback=None):
        super().__init__(engine, fallback)
        import threading

        self._lock = threading.RLock()

    def lock(self):
        return self._lock

    def get(self):
        import time

        found = (self.engine, self.fallback)
        time.sleep(0.2)  # long enough for another request to read the same pair
        return found


def _two_requests_at_once(session, active_plugins, pointers, first, second):
    import threading

    failures = []

    def run(action):
        try:
            action(TranslatorStore(session, pointers=pointers, plugin_active=lambda plugin_id: plugin_id in active_plugins))
        except BaseException as error:  # the test reports whatever went wrong
            failures.append(repr(error))
        finally:
            session.remove()

    threads = [threading.Thread(target=run, args=(action,)) for action in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads) and failures == []


def test_two_pointer_changes_at_once_both_land(session, active_plugins):
    setup = TranslatorStore(session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: plugin_id in active_plugins)
    a, b, c, d = (setup.create_instance("google_translate", name=name) for name in "abcd")
    pointers = SlowLockedPointers(a.id, b.id)
    _two_requests_at_once(session, active_plugins, pointers,
                          lambda store: store.set_engine(c.id), lambda store: store.set_fallback(d.id))
    assert (pointers.engine, pointers.fallback) == (c.id, d.id)


def test_a_release_and_a_pointer_change_at_once_both_land(session, active_plugins):
    setup = TranslatorStore(session, pointers=MemoryPointers(), plugin_active=lambda plugin_id: plugin_id in active_plugins)
    a, b, c = (setup.create_instance("google_translate", name=name) for name in "abc")
    pointers = SlowLockedPointers(a.id, b.id)
    _two_requests_at_once(session, active_plugins, pointers,
                          lambda store: store.release_pointers({a.id}, why="removed"),
                          lambda store: store.set_fallback(c.id))
    assert (pointers.engine, pointers.fallback) == (None, c.id)


def test_a_session_reads_what_another_session_changed_in_engines_connections_and_secrets(session, store):
    from sqlalchemy.orm import sessionmaker

    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openrouter", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    store.set_secret((CONNECTION, connection.id), "api_key", KEY, destinations=frozenset({"https://a.test"}))
    # What this session has loaded and keeps hold of.
    held = (store.get_instance(instance.id), store.list_connections(instance.id)[0],
            store._secret_rows((CONNECTION, connection.id))[0])
    other = sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)()
    try:
        elsewhere = TranslatorStore(other, pointers=MemoryPointers(), plugin_active=lambda plugin_id: True)
        elsewhere.set_instance_enabled(instance.id, False)
        elsewhere.update_connection(connection.id, schema=DestinationSchema(), model="changed")
        elsewhere.set_secret((CONNECTION, connection.id), "api_key", KEY, destinations=frozenset({"https://b.test"}))
    finally:
        other.close()
    assert store.get_instance(instance.id, fresh=True).enabled == 0
    assert store.list_connections(instance.id)[0].model == "changed"
    assert store._secret_rows((CONNECTION, connection.id))[0].destinations_json == '["https://b.test"]'
    assert held[0] is not None and held[1] is not None and held[2] is not None


def test_releasing_a_plugin_resets_pointers_and_deletes_nothing_until_asked(store, pointers):
    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    change = store.release_plugin("google_translate")
    assert {n.code for n in change.notices} == {"engine_reset"} and pointers.get() == (None, None)
    assert [row.id for row in store.list_instances("google_translate")] == [google.id]
    store.delete_plugin_data("google_translate")
    assert store.list_instances("google_translate") == []


@pytest.mark.parametrize("keep", [True, False])
def test_removing_a_translator_keeps_or_deletes_what_it_stored_as_asked(store, pointers, monkeypatch, keep):
    """The kind hook that runs once a removal is saved: the answer decides whether the engine instance, its
    connections and its secrets survive, and a reinstall finds them when they did."""
    from provider_hub.kinds.translator import TranslatorKind
    from subtitles.tools.translate.runtime import worker_binding

    marked, cleared = [], []
    monkeypatch.setattr("provider_hub.kinds.translator._store", lambda: store)
    monkeypatch.setattr(worker_binding, "plugin_kept", marked.append)
    monkeypatch.setattr(worker_binding, "plugin_removed", cleared.append)
    instance = _ai_engine(store)
    store.set_secret((CONNECTION, store.list_connections(instance.id)[0].id), "api_key", KEY,
                     destinations=frozenset({"https://openrouter.ai"}))
    kind = TranslatorKind()
    assert kind.on_removed(OFFICIAL_ENGINE_PLUGIN_ID) is True

    kind.after_removed(OFFICIAL_ENGINE_PLUGIN_ID, keep_data=keep)

    if keep:
        assert (marked, cleared) == ([OFFICIAL_ENGINE_PLUGIN_ID], [])
        assert [row.id for row in store.list_instances(OFFICIAL_ENGINE_PLUGIN_ID)] == [instance.id]
        assert len(store.list_connections(instance.id)) == 1
        # Installing the plugin again picks the same instance up instead of making a blank one.
        assert kind.on_installed(OFFICIAL_ENGINE_PLUGIN_ID) is True
        assert [row.id for row in store.list_instances(OFFICIAL_ENGINE_PLUGIN_ID)] == [instance.id]
    else:
        assert (marked, cleared) == ([], [OFFICIAL_ENGINE_PLUGIN_ID])
        assert store.list_instances(OFFICIAL_ENGINE_PLUGIN_ID) == []
        assert store.list_connections(instance.id) == []


def test_disabling_a_plugin_releases_the_pointers_but_keeps_its_instances(store, pointers):
    google = store.create_instance("google_translate")
    store.set_engine(google.id)
    store.plugin_disabled("google_translate")
    assert pointers.get() == (None, None)
    assert store.get_instance(google.id) is not None and store.get_instance(google.id).enabled == 1


def test_a_pointer_to_a_missing_instance_is_refused(store):
    with pytest.raises(TranslatorStoreError):
        store.set_engine(999)
    with pytest.raises(TranslatorStoreError):
        store.set_fallback(999)


def test_a_pointer_write_that_fails_is_reported(session, active_plugins):
    store = TranslatorStore(session, pointers=MemoryPointers(writable=False),
                            plugin_active=lambda plugin_id: plugin_id in active_plugins)
    google = store.create_instance("google_translate")
    with pytest.raises(TranslatorStoreError):
        store.set_engine(google.id)


def test_the_config_pointers_round_trip_and_roll_back_on_a_failed_write(monkeypatch):
    from app import config
    from subtitles.tools.translate.runtime.store import ConfigPointers

    before = (config.settings.translator.engine, config.settings.translator.fallback_engine)
    monkeypatch.setattr(config, "write_config", lambda *a, **k: True)
    try:
        pointers = ConfigPointers()
        assert pointers.set(4, 7) is True
        assert pointers.get() == (4, 7)
        assert (config.settings.translator.engine, config.settings.translator.fallback_engine) == ("4", "7")
        monkeypatch.setattr(config, "write_config", lambda *a, **k: False)
        assert pointers.set(None, None) is False
        assert pointers.get() == (4, 7)
        monkeypatch.setattr(config, "write_config", lambda *a, **k: True)
        assert pointers.set(None, None) is True
        assert pointers.get() == (None, None)
    finally:
        config.settings.translator.engine, config.settings.translator.fallback_engine = before


# ---------------------------------------------------------------- secrets

def test_a_secret_is_encrypted_at_rest(store, session):
    from app.database import TableTranslatorSecret
    from secret_store import is_encrypted

    instance = _ai_engine(store, with_connection=False)
    owner = (INSTANCE, instance.id)
    store.set_secret(owner, "token", KEY, destinations=frozenset({"d"}))
    stored = session.query(TableTranslatorSecret).one().encrypted_value
    assert KEY not in stored and is_encrypted(stored)
    assert store.secrets_for_request(owner, frozenset({"d"})) == {"token": KEY}


@pytest.fixture
def scrub_with_the_runtime_on(monkeypatch, session):
    """The log scrubber seeing a settings credential, a Hub credential and this test's database."""
    from app import database
    from app.config import settings
    from subtitles.tools.translate import runtime
    from secret_store import scrub

    settings_value, hub_value = "settings-credential-0123456789", "hub-credential-0123456789"
    monkeypatch.setattr(settings.translator, "runtime_v2", True, raising=False)
    monkeypatch.setattr(database, "database", session)
    monkeypatch.setattr(runtime, "_tables_present", True)
    monkeypatch.setattr(scrub, "collect_settings_secret_values", lambda settings_obj: {settings_value})
    monkeypatch.setattr(scrub, "collect_hub_secret_values", lambda state=None: {hub_value})
    scrub.clear_secret_values()
    scrub.refresh_secret_values()
    try:
        yield scrub, {settings_value, hub_value}
    finally:
        scrub.clear_secret_values()


def test_a_translator_secret_is_scrubbed_without_unmasking_the_other_credentials(store, scrub_with_the_runtime_on):
    scrub, others = scrub_with_the_runtime_on
    assert scrub.registered_secret_values() == others
    owner = (INSTANCE, _ai_engine(store, with_connection=False).id)

    store.set_secret(owner, "token", KEY, destinations=frozenset())
    assert scrub.registered_secret_values() == others | {KEY}
    assert scrub.scrub_text(f"failed with {KEY}") == f"failed with {scrub.MASK}"

    # What the next boot, settings save or Hub write does: rebuild the set from its sources.
    scrub.clear_secret_values()
    scrub.refresh_secret_values()
    assert scrub.registered_secret_values() == others | {KEY}

    store.clear_secret(owner, "token", destinations=frozenset())
    assert scrub.registered_secret_values() == others


def test_with_the_switch_off_the_translator_secrets_are_not_collected(store, scrub_with_the_runtime_on, monkeypatch):
    from app.config import settings

    scrub, others = scrub_with_the_runtime_on
    owner = (INSTANCE, _ai_engine(store, with_connection=False).id)
    store.set_secret(owner, "token", KEY, destinations=frozenset())
    monkeypatch.setattr(settings.translator, "runtime_v2", False, raising=False)
    scrub.refresh_secret_values()
    assert scrub.registered_secret_values() == others


def test_a_short_engine_key_and_key_list_entries_are_masked_as_whole_tokens(store, scrub_with_the_runtime_on):
    scrub, others = scrub_with_the_runtime_on
    owner = (INSTANCE, _ai_engine(store, with_connection=False).id)
    store.set_secret(owner, "token", "k3y", destinations=frozenset())
    store.set_secret(owner, "keys", json.dumps(["ab1", "cd2"]), destinations=frozenset())

    # In context a short key is masked, inside a longer word it is left alone.
    assert scrub.scrub_text("token=k3y and password: ab1") == f"token={scrub.MASK} and password: {scrub.MASK}"
    assert scrub.scrub_text("first=ab1&second=cd2") == f"first={scrub.MASK}&second={scrub.MASK}"
    assert scrub.scrub_text("the k3yboard and cd2x") == "the k3yboard and cd2x"
    # Standing alone as a value, it is masked whole.
    assert scrub.scrub_leaf("k3y") == scrub.MASK
    assert scrub.scrub_leaf("cd2") == scrub.MASK
    assert scrub.scrub_leaf("k3yboard") == "k3yboard"


def test_the_mask_text_is_refused_as_a_new_secret_and_keeps_the_stored_one(store):
    owner = (INSTANCE, _ai_engine(store, with_connection=False).id)
    with pytest.raises(TranslatorStoreError, match="cannot be"):
        store.set_secret(owner, "token", "***", destinations=frozenset())
    assert store.secrets_for_request(owner, frozenset()) == {}
    store.set_secret(owner, "token", KEY, destinations=frozenset())
    with pytest.raises(TranslatorStoreError, match="translator.token cannot be"):
        store.set_secret(owner, "token", " *** ", destinations=frozenset())
    assert store.secrets_for_request(owner, frozenset()) == {"token": KEY}


def test_an_empty_value_keeps_the_stored_secret_and_clear_removes_it(store):
    instance = _ai_engine(store, with_connection=False)
    owner = (INSTANCE, instance.id)
    store.set_secret(owner, "token", KEY, destinations=frozenset({"d"}))
    store.set_secret(owner, "token", "", destinations=frozenset({"d"}))
    assert store.secrets_for_request(owner, frozenset({"d"})) == {"token": KEY}
    store.clear_secret(owner, "token", destinations=frozenset({"d"}))
    assert store.secrets_for_request(owner, frozenset({"d"})) == {}


def test_the_summary_shows_last_four_only_for_a_long_secret(store):
    instance = _ai_engine(store, with_connection=False)
    owner = (INSTANCE, instance.id)
    store.set_secret(owner, "long", KEY, destinations=frozenset())
    store.set_secret(owner, "short", "short-secret-123", destinations=frozenset())  # 16 characters
    store.set_secret(owner, "tiny", "tiny", destinations=frozenset())
    summary = store.secret_summary(owner, frozenset())
    assert summary["long"] == {"configured": True, "stale": False, "last4": KEY[-4:]}
    assert summary["short"]["last4"] == "t-123"[-4:]
    assert summary["tiny"] == {"configured": True, "stale": False}
    assert KEY not in repr(summary)


def test_a_changed_base_url_never_reuses_the_stored_key(store):
    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openai_compatible", settings={})
    owner = (CONNECTION, connection.id)
    first = effective_destinations(OPENAI, {})
    store.set_secret(owner, "api_key", KEY, destinations=first)
    assert store.secrets_for_request(owner, first) == {"api_key": KEY}

    result = store.update_connection(connection.id, schema=OPENAI, settings={"base_url": "http://translator.example:1234/v1"})
    assert result.new_destinations == {"http://translator.example:1234/v1"}
    assert result.awaiting_secrets is True
    assert store.get_connection(connection.id).awaiting_secrets == 1
    assert not store.connection_usable(store.get_connection(connection.id))
    changed = effective_destinations(OPENAI, {"base_url": "http://translator.example:1234/v1"})
    assert store.secrets_for_request(owner, changed) == {}
    assert store.secret_summary(owner, changed)["api_key"]["stale"] is True

    # Entering the key again binds it to the new address and lets the connection serve.
    store.set_secret(owner, "api_key", KEY, destinations=changed)
    assert store.get_connection(connection.id).awaiting_secrets == 0
    assert store.secrets_for_request(owner, changed) == {"api_key": KEY}
    # Going back to the first address finds the key bound to the second, not the first.
    assert store.secrets_for_request(owner, first) == {}


def test_a_changed_destination_without_a_stored_secret_needs_nothing_entered(store):
    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openai_compatible")
    result = store.update_connection(connection.id, schema=OPENAI, settings={"base_url": "http://lmstudio.lan:1234/v1"})
    assert result.new_destinations and result.awaiting_secrets is False


def test_an_instance_save_with_a_new_destination_marks_it_awaiting(store):
    schema = DestinationSchema(fields=({"key": "server", "type": "text", "destination": True},
                                       {"key": "token", "type": "secret"}))
    instance = store.create_instance("example_engine", settings={"server": "http://a:9876"})
    owner = (INSTANCE, instance.id)
    store.set_secret(owner, "token", KEY, destinations=effective_destinations(schema, {"server": "http://a:9876"}))
    result = store.save_instance_settings(instance.id, {"server": "http://b:9876"}, schema=schema)
    assert result.new_destinations == {"http://b:9876"} and result.awaiting_secrets
    assert store.get_instance(instance.id).awaiting_secrets == 1


# ---------------------------------------------------------------- plugin update

def _resolver(schema_by_kind):
    return lambda kind, _row: schema_by_kind[kind]


def test_a_key_list_entry_equal_to_the_mask_text_is_refused_unless_it_is_already_stored(store):
    owner = (INSTANCE, _ai_engine(store, with_connection=False).id)
    with pytest.raises(TranslatorStoreError, match="translator.keys cannot be"):
        store.set_secret(owner, "keys", json.dumps(["real-key-0123456789", "***"]), destinations=frozenset())
    assert store.secrets_for_request(owner, frozenset()) == {}
    store.set_secret(owner, "keys", json.dumps(["real-key-0123456789"]), destinations=frozenset())
    with pytest.raises(TranslatorStoreError, match="cannot be"):
        store.set_secret(owner, "keys", json.dumps(["real-key-0123456789", " *** "]), destinations=frozenset())
    assert json.loads(store.secrets_for_request(owner, frozenset())["keys"]) == ["real-key-0123456789"]


def test_a_plugin_update_drops_a_mask_entry_from_a_stored_key_list(store):
    after = DestinationSchema(fields=({"key": "tokens", "type": "key_list", "secret": True},
                                      {"key": "only", "type": "key_list", "secret": True}))
    instance = store.create_instance("lingarr", settings={"tokens": ["k1-0123456789", "***"], "only": ["***"]})
    store.apply_plugin_update("lingarr", _resolver({INSTANCE: after, CONNECTION: after}))
    held = store.secrets_for_request((INSTANCE, instance.id), frozenset())
    assert json.loads(held["tokens"]) == ["k1-0123456789"] and "only" not in held


def test_a_plugin_update_drops_a_stored_mask_text_instead_of_failing(store, caplog):
    after = DestinationSchema(fields=({"key": "token", "type": "secret"}, {"key": "other", "type": "secret"},
                                      {"key": "label", "type": "text"}))
    instance = store.create_instance("lingarr", settings={"token": "***", "other": KEY, "label": "x"})
    with caplog.at_level("WARNING"):
        store.apply_plugin_update("lingarr", _resolver({INSTANCE: after, CONNECTION: after}))
    # The mask text left the settings and was not stored as a secret; the real secret moved as usual.
    assert store.settings_of(store.get_instance(instance.id)) == {"label": "x"}
    assert store.secrets_for_request((INSTANCE, instance.id), frozenset()) == {"other": KEY}
    assert list(store.secret_summary((INSTANCE, instance.id), frozenset())) == ["other"]
    assert "token" in caplog.text and "***" not in caplog.text.replace("the mask text", "")


def test_a_renamed_destination_field_with_a_new_value_is_caught(store):
    old = DestinationSchema(fields=({"key": "base_url", "type": "text", "destination": True},))
    new = DestinationSchema(fields=({"key": "server_url", "type": "text", "destination": True},))
    instance = store.create_instance("example_engine", settings={"base_url": "http://a:1", "server_url": "http://evil:1"})
    store.set_secret((INSTANCE, instance.id), "token", KEY, destinations=effective_destinations(
        old, {"base_url": "http://a:1"}))
    changes = store.destination_changes_for_update(
        "example_engine", _resolver({INSTANCE: old, CONNECTION: old}), _resolver({INSTANCE: new, CONNECTION: new}))
    assert [(c.owner, c.added, c.has_secrets) for c in changes] == [((INSTANCE, instance.id), {"http://evil:1"}, True)]
    # Nothing is written until the update is activated.
    assert store.get_instance(instance.id).awaiting_secrets == 0
    waiting = store.apply_plugin_update("example_engine", _resolver({INSTANCE: new, CONNECTION: new}))
    assert waiting == [(INSTANCE, instance.id)]
    assert store.secrets_for_request((INSTANCE, instance.id), effective_destinations(
        new, {"base_url": "http://a:1", "server_url": "http://evil:1"})) == {}


def test_a_field_that_becomes_a_secret_moves_its_stored_plaintext_into_the_secret_store(store):
    after = DestinationSchema(fields=({"key": "base_url", "type": "text", "destination": True},
                                      {"key": "token", "type": "secret"},
                                      {"key": "tokens", "type": "key_list", "secret": True},
                                      {"key": "label", "type": "text"}))
    instance = store.create_instance("example_engine", settings={"base_url": "http://a:1", "token": KEY, "label": "x",
                                                          "tokens": ["k1-0123456789", "k2-0123456789"]})
    connection = store.add_connection(instance.id, "t", plugin_id="example_engine", settings={"token": "", "label": "y"})
    waiting = store.apply_plugin_update("example_engine", _resolver({INSTANCE: after, CONNECTION: after}))
    assert waiting == []
    assert store.settings_of(store.get_instance(instance.id)) == {"base_url": "http://a:1", "label": "x"}
    held = store.secrets_for_request((INSTANCE, instance.id), effective_destinations(after, {"base_url": "http://a:1"}))
    assert held["token"] == KEY and json.loads(held["tokens"]) == ["k1-0123456789", "k2-0123456789"]
    # An empty plaintext value just leaves the settings; it does not become an empty secret.
    assert store.settings_of(store.get_connection(connection.id)) == {"label": "y"}
    assert store.secret_summary((CONNECTION, connection.id), frozenset()) == {}


def test_a_changed_default_is_a_new_destination(store):
    old = DestinationSchema(fields=({"key": "base_url", "type": "text", "destination": True,
                                     "default": "https://api.example.com"},))
    new = DestinationSchema(fields=({"key": "base_url", "type": "text", "destination": True,
                                     "default": "https://api.other.example"},))
    instance = store.create_instance("example_engine", settings={})
    changes = store.destination_changes_for_update(
        "example_engine", _resolver({INSTANCE: old, CONNECTION: old}), _resolver({INSTANCE: new, CONNECTION: new}))
    assert changes[0].added == {"https://api.other.example"} and changes[0].has_secrets is False
    # A value the user typed wins over the default, so the changed default is not a new destination.
    store.save_instance_settings(instance.id, {"base_url": "https://api.example.com"}, schema=old)
    assert store.destination_changes_for_update(
        "example_engine", _resolver({INSTANCE: old, CONNECTION: old}), _resolver({INSTANCE: new, CONNECTION: new})) == []


def test_a_changed_endpoint_origin_is_a_new_destination_for_its_connections(store):
    old = DestinationSchema(endpoints=("https://openrouter.ai:443",))
    new = DestinationSchema(endpoints=("https://openrouter.example:443",))
    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openrouter", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    store.set_secret((CONNECTION, connection.id), "api_key", KEY, destinations=effective_destinations(old, {}))
    changes = store.destination_changes_for_update(
        OFFICIAL_ENGINE_PLUGIN_ID, _resolver({INSTANCE: DestinationSchema(), CONNECTION: old}),
        _resolver({INSTANCE: DestinationSchema(), CONNECTION: new}))
    assert [c.owner for c in changes] == [(CONNECTION, connection.id)]
    waiting = store.apply_plugin_update(OFFICIAL_ENGINE_PLUGIN_ID, _resolver({INSTANCE: DestinationSchema(),
                                                                              CONNECTION: new}))
    assert waiting == [(CONNECTION, connection.id)]
    assert not store.connection_usable(store.get_connection(connection.id))
    # The old key is still stored, bound to the old origin, so a rollback finds it.
    assert store.secrets_for_request((CONNECTION, connection.id), effective_destinations(old, {})) == {"api_key": KEY}


def test_an_update_that_adds_no_destination_changes_nothing(store):
    schema = DestinationSchema(endpoints=("https://openrouter.ai:443",))
    instance = _ai_engine(store, with_connection=False)
    connection = store.add_connection(instance.id, "openrouter", plugin_id=OFFICIAL_ENGINE_PLUGIN_ID)
    store.set_secret((CONNECTION, connection.id), "api_key", KEY, destinations=effective_destinations(schema, {}))
    resolver = _resolver({INSTANCE: DestinationSchema(), CONNECTION: schema})
    assert store.destination_changes_for_update(OFFICIAL_ENGINE_PLUGIN_ID, resolver, resolver) == []
    assert store.apply_plugin_update(OFFICIAL_ENGINE_PLUGIN_ID, resolver, plugin_version="2.3.0") == []
    assert store.get_connection(connection.id).plugin_version == "2.3.0"


def test_a_migration_that_changes_a_destination_is_caught_through_its_settings(store):
    schema = DestinationSchema(fields=({"key": "base_url", "type": "text", "destination": True},))
    instance = store.create_instance("example_engine", settings={"base_url": "http://a:1"})
    resolver = _resolver({INSTANCE: schema, CONNECTION: schema})
    changes = store.destination_changes_for_update(
        "example_engine", resolver, resolver, new_settings=lambda kind, row: {"base_url": "http://migrated:1"})
    assert changes[0].added == {"http://migrated:1"}
    store.apply_plugin_update("example_engine", resolver, new_settings=lambda kind, row: {"base_url": "http://migrated:1"})
    assert store.settings_of(store.get_instance(instance.id)) == {"base_url": "http://migrated:1"}
    assert store.get_instance(instance.id).revision == 2


# ---------------------------------------------------------------- destinations, pure

def test_destination_values_are_normalised_and_defaults_apply():
    assert normalize_destination("  HTTP://LM.Lan:1234/v1 ") == "http://lm.lan:1234/v1"
    assert normalize_destination("   ") == ""
    for bad in ("not a url", "ftp://x.test", "x.test:1234", "http://", "https://[bad/v1", "http://x.test:port/v1"):
        with pytest.raises(DestinationError):
            normalize_destination(bad)
    assert effective_destinations(OPENAI, None) == {"https://api.openai.com/v1"}
    assert effective_destinations(OPENAI, {"base_url": ""}) == {"https://api.openai.com/v1"}
    assert effective_destinations(OPENAI, {"base_url": "http://x:1"}) == {"http://x:1"}
    assert effective_destinations(DestinationSchema(fields=({"key": "a", "type": "text"},)), {"a": "http://x"}) == set()
    listed = DestinationSchema(fields=({"key": "urls", "type": "list", "destination": True},))
    assert effective_destinations(listed, {"urls": ["http://a", "http://b", ""]}) == {"http://a", "http://b"}
    assert added_destinations({"a"}, {"a", "b"}) == {"b"} and covers({"a", "b"}, {"a"}) and not covers({"a"}, {"b"})


def test_a_stored_or_default_value_that_is_not_a_clean_address_never_matches_a_secret_binding():
    secret_url = "https://user:password@example.test/v1"
    found = effective_destinations(OPENAI, {"base_url": secret_url})
    assert len(found) == 1 and not any("password" in item or "example" in item for item in found)
    assert not covers({"https://api.openai.com/v1", secret_url}, found)  # nothing a secret was bound to equals it
    assert effective_destinations(OPENAI, {"base_url": secret_url}) == found  # stable, so it still compares


def test_schemas_come_from_the_manifest_envelope():
    manifest = {
        "schema_version": 2, "id": "ai_translator", "kind": "translator", "kind_api": "bazarr.translator.v1",
        "name": "AI", "version": "2.2.0", "settings_version": 1,
        "endpoints": ["https://plugin.example:443"],
        "settings": {
            "fields": [{"key": "server", "type": "text", "destination": True, "default": "http://engine:1"}],
            "backend_types": {
                "openrouter": {"endpoints": ["https://openrouter.ai:443"], "fields": []},
                "openai_compatible": {"endpoints": [], "fields": [
                    {"key": "base_url", "type": "text", "destination": True, "default": "https://api.openai.com/v1"}]},
            },
        },
    }
    assert effective_destinations(schema_for_engine(manifest), {}) == {"http://engine:1", "https://plugin.example:443"}
    assert effective_destinations(schema_for_backend(manifest, "openrouter"), {}) == {"https://openrouter.ai:443"}
    assert effective_destinations(schema_for_backend(manifest, "openai_compatible"), {}) == {
        "https://api.openai.com/v1"}
    assert effective_destinations(schema_for_backend(manifest, "unknown"), {}) == set()

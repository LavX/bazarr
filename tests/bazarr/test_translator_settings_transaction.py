"""A settings save is one transaction: the revision claim, the settings and every secret write or clear.

Runs on SQLite, and on PostgreSQL when BAZARR_PG_TEST_URL is set. The engines are built like the
application's: AUTOCOMMIT, with no pool on SQLite.
"""
import os
import threading
from uuid import uuid4

import pytest

from plugin_platform_helpers import service_manifest
from subtitles.tools.translate.runtime.settings_service import (
    InstalledPlugin, StaleRevisionError, TranslatorSettingsService)
from subtitles.tools.translate.runtime.store import INSTANCE, OFFICIAL_ENGINE_PLUGIN_ID, TranslatorStore

PLUGIN = OFFICIAL_ENGINE_PLUGIN_ID
FIRST = "first-writer-token-0123456789"  # pragma: allowlist secret
SECOND = "second-writer-token-0123456789"  # pragma: allowlist secret


class MemoryPointers:
    def __init__(self):
        self.engine = self.fallback = None

    def get(self):
        return self.engine, self.fallback

    def set(self, engine, fallback):
        self.engine, self.fallback = engine, fallback
        return True


def _manifest():
    return service_manifest(
        PLUGIN, settings_version=2, kind="translator", kind_api="bazarr.translator.v1",
        endpoints=["https://updates.example.test:443"],
        settings={"fields": [{"key": "credit_line", "type": "text", "max_length": 80},
                             {"key": "service_token", "type": "secret"}],
                  "backend_types": {"openrouter": {"label": "OpenRouter", "endpoints": ["https://openrouter.ai:443"],
                                                   "fields": [{"key": "api_key", "type": "secret"}]}}})


def _tables():
    from app.database import TableTranslatorConnection, TableTranslatorEngineInstance, TableTranslatorSecret

    return [TableTranslatorEngineInstance.__table__, TableTranslatorConnection.__table__,
            TableTranslatorSecret.__table__]


def _make_engine(kind, tmp_path):
    from sqlalchemy import create_engine, event

    from app.database import Base

    if kind == "sqlite":
        from sqlalchemy.pool import NullPool

        from app.database import configure_sqlite_connection

        engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}", poolclass=NullPool,
                               isolation_level="AUTOCOMMIT")
        event.listen(engine, "connect", configure_sqlite_connection)
        Base.metadata.create_all(engine, tables=_tables())
        return engine, None
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL tests")
    import sqlalchemy as sa

    schema = "translator_settings_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(url, isolation_level="AUTOCOMMIT")

    @event.listens_for(engine, "connect")
    def _path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {schema}")
        cursor.close()

    Base.metadata.create_all(engine, tables=_tables())
    return engine, (admin, schema)


@pytest.fixture(params=["sqlite", "postgres"])
def world(request, tmp_path):
    import sqlalchemy as sa
    from sqlalchemy.orm import scoped_session, sessionmaker

    engine, pg = _make_engine(request.param, tmp_path)
    sessions = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    plugins = {PLUGIN: InstalledPlugin(PLUGIN, "AI Subtitle Translator", "2.2.0", True, _manifest())}

    def service():
        """A service on this thread's own session, as each request has."""
        store = TranslatorStore(sessions, pointers=MemoryPointers(), plugin_active=lambda pid: pid in plugins)
        return TranslatorSettingsService(store, plugin_lookup=plugins.get)

    first = service()
    instance_id = first._store.create_instance(PLUGIN, name="AI", settings_version=2, plugin_version="2.2.0").id

    class World:
        pass

    w = World()
    w.service, w.instance_id, w.sessions = service, instance_id, sessions

    def state():
        """What is stored now, read on a fresh session so no cached row hides a difference."""
        sessions.remove()
        store = service()._store
        row = store.get_instance(instance_id)
        held = store.secrets_for_request((INSTANCE, instance_id), frozenset())
        return row.revision, store.settings_of(row), held

    w.state = state
    try:
        yield w
    finally:
        sessions.remove()
        engine.dispose()
        if pg:
            admin, schema = pg
            with admin.begin() as connection:
                connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
            admin.dispose()


def _save(service, instance_id, revision, **payload):
    return service.save_engine(instance_id, {"expected_revision": revision, **payload})


def test_a_save_that_fails_while_writing_its_secret_leaves_no_new_revision(world, monkeypatch):
    service = world.service()
    monkeypatch.setattr(service._store, "set_secret", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("the disk is full")))
    with pytest.raises(RuntimeError):
        _save(service, world.instance_id, 1, values={"credit_line": "new", "service_token": FIRST})
    revision, settings, held = world.state()
    assert (revision, settings, held) == (1, {}, {})


def test_a_clear_only_save_claims_a_revision(world):
    service = world.service()
    _save(service, world.instance_id, 1, values={"service_token": FIRST})
    assert world.state()[0] == 2
    _save(service, world.instance_id, 2, clear_secrets=["service_token"])
    revision, _settings, held = world.state()
    assert revision == 3 and held == {}
    # A tab that loaded the engine before the clear cannot save over it.
    with pytest.raises(StaleRevisionError):
        _save(service, world.instance_id, 2, values={"credit_line": "from an old tab"})


def test_a_clear_only_save_with_an_old_revision_is_refused_and_clears_nothing(world):
    service = world.service()
    _save(service, world.instance_id, 1, values={"service_token": FIRST})
    _save(service, world.instance_id, 2, values={"credit_line": "someone else"})
    with pytest.raises(StaleRevisionError):
        _save(service, world.instance_id, 2, clear_secrets=["service_token"])
    revision, settings, held = world.state()
    assert revision == 3 and settings == {"credit_line": "someone else"} and held == {"service_token": FIRST}


def test_a_save_made_after_the_first_saves_revision_cannot_be_overwritten_by_its_secrets(world, monkeypatch):
    """The first save is still writing its secret when a second one, built on the new revision, starts.
    The second must not run between the first's revision and the first's secret."""
    first = world.service()
    real_set = first._store.set_secret
    outcome = {}

    def second_writer():
        try:
            outcome["second"] = _save(world.service(), world.instance_id, 2, values={"service_token": SECOND})
        except StaleRevisionError as error:
            outcome["second"] = error
        finally:
            world.sessions.remove()

    def set_secret_while_a_second_save_tries(*args, **kwargs):
        worker = threading.Thread(target=second_writer)
        worker.start()
        worker.join(timeout=1.5)
        outcome["worker"] = worker
        return real_set(*args, **kwargs)

    monkeypatch.setattr(first._store, "set_secret", set_secret_while_a_second_save_tries)
    _save(first, world.instance_id, 1, values={"service_token": FIRST})
    outcome["worker"].join(timeout=30)
    assert not outcome["worker"].is_alive()
    revision, _settings, held = world.state()
    # Whatever order the two saves ended up in, the secret on file is the one the stored revision belongs to.
    assert held == {"service_token": SECOND if revision == 3 else FIRST}


def test_two_saves_of_one_revision_have_exactly_one_winner_and_the_winners_secret_stays(world):
    results = []
    barrier = threading.Barrier(2)

    def writer(secret):
        try:
            service = world.service()
            service._instance(world.instance_id)  # both load revision 1 before either saves
            barrier.wait(timeout=30)
            _save(service, world.instance_id, 1, values={"service_token": secret})
            results.append(("saved", secret))
        except StaleRevisionError:
            results.append(("stale", secret))
        finally:
            world.sessions.remove()

    threads = [threading.Thread(target=writer, args=(secret,)) for secret in (FIRST, SECOND)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(kind for kind, _secret in results) == ["saved", "stale"]
    winner = next(secret for kind, secret in results if kind == "saved")
    revision, _settings, held = world.state()
    assert revision == 2 and held == {"service_token": winner}

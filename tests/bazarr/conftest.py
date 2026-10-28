import os
import logging
from pathlib import Path

import pytest

os.environ["NO_CLI"] = "true"
os.environ["SZ_USER_AGENT"] = "test"
os.environ["BAZARR_VERSION"] = "test" # fixme

# App imports may run startup before a test fixture can intercept HTTP.
# Seed the two cached documents so an isolated run never fetches them.
for _document in ("releases.txt", "announcements.txt"):
    _cached_document = Path(os.environ["BAZARR_CONFIG_DIR"]) / "config" / _document
    if not _cached_document.exists():
        _cached_document.write_text("[]", encoding="utf-8")

logging.getLogger("rebulk").setLevel(logging.WARNING)


@pytest.fixture
def schema_session():
    """Fresh in-memory SQLite database built from the live ORM metadata, with
    SQLite foreign-key enforcement ON.

    ``create_engine`` does not run the app's ``configure_sqlite_connection``
    listener (which sets ``PRAGMA foreign_keys=ON``), so ON DELETE / FK
    behaviour is silently off in plain test engines. Schema and constraint
    tests need it on to exercise real enforcement, so we register our own
    connect hook here.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import Base

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = scoped_session(sessionmaker(bind=engine))
    try:
        yield session
    finally:
        session.remove()
        engine.dispose()


@pytest.fixture
def languages_loaded(monkeypatch):
    """Seed the module-level language table the naming lookups read.

    create_languages_dict() normally runs at app startup off the database. The
    auto-translate paths resolve a translation's destination when they queue,
    so a test that reaches those paths needs the table the way boot leaves it.
    """
    from languages import get_languages

    monkeypatch.setattr(get_languages, 'languages_dict', [
        {'code2': 'de', 'code3': 'deu', 'name': 'German', 'code3b': 'deu'},
        {'code2': 'en', 'code3': 'eng', 'name': 'English', 'code3b': 'eng'},
        {'code2': 'fr', 'code3': 'fra', 'name': 'French', 'code3b': 'fra'},
        {'code2': 'hu', 'code3': 'hun', 'name': 'Hungarian', 'code3b': 'hun'},
        {'code2': 'nl', 'code3': 'nld', 'name': 'Dutch', 'code3b': 'nld'},
    ], raising=False)


@pytest.fixture
def scheduler_runtime(schema_session, monkeypatch):
    """Give scheduler imports a real isolated repository without starting jobs.

    Scheduler construction reads enabled sports owners even when both legacy
    integrations are disabled. Unit tests importing it must supply that database
    dependency instead of relying on an initialized application data directory.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from arr_instances.repository import ArrInstanceRepository
    from sportarr import scheduler as sports_scheduler
    from sportarr import sse_client
    from types import SimpleNamespace

    def client_factory(owner, identity, client):
        return SimpleNamespace(identity=identity, start=lambda: None,
                               stop=lambda timeout=35: True, is_alive=lambda: True)
    monkeypatch.setattr(sse_client, 'database', schema_session)
    monkeypatch.setattr(sse_client, '_manager', sse_client.SportarrClientManager(client_factory))

    monkeypatch.setattr(sports_scheduler, 'ArrInstanceRepository',
                        lambda _session: ArrInstanceRepository(schema_session))
    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self: None)


@pytest.fixture(autouse=True)
def _keep_the_session_generation():
    """Undo what a test does to the process-wide session generation.

    A save that changes the password, the username or the auth type advances
    the generation, and the settings object outlives the test. Left alone, a
    later test that hands a plain session cookie to the form-login gate would
    meet a generation it never set up. The auth section is looked up again at
    the end, because a settings save replaces it with a new object. Only
    touched when the settings were already loaded, so a test that never needed
    them does not pay for them.
    """
    import sys

    def live_auth():
        config = sys.modules.get("app.config")
        return getattr(getattr(config, "settings", None), "auth", None)

    def forget_issued_numbers():
        # The mark of numbers already handed out belongs to the process, so it
        # is reset together with the number it follows.
        generation = sys.modules.get("app.auth.generation")
        if generation is not None:
            generation._issued_high_water = 0

    auth = live_auth()
    before = auth.get("session_generation") if auth is not None else None
    forget_issued_numbers()
    yield
    forget_issued_numbers()
    auth = live_auth()
    if auth is not None and before is not None:
        auth["session_generation"] = before

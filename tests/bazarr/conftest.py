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


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """A release that is detached while a test runs is finished on threads of its own (its row deleted once its
    calls are done). They end before the test's fixtures are torn down, so a thread that ends late finishes against
    the state the test set up, and none of them writes into another test's state.

    This is a hook and not an autouse fixture on purpose: a fixture that asks for ``monkeypatch`` is created before
    the test's own fixtures and torn down after them, which changes the teardown order of every unrelated test. The wait
    runs first, before any fixture of the item is finalised, and touches nothing when no release was detached."""
    import sys

    detach = sys.modules.get("provider_hub.detach")
    finished = True
    if detach is not None:
        finished = detach.wait_idle(30)
        if not finished:
            # The list is cleared so the next test does not wait on a thread this one leaked.
            with detach._PENDING_LOCK:
                detach._PENDING.clear()
    yield
    # A release whose detach never ends is a test that leaked a thread: it fails here, after the fixtures are undone.
    assert finished, "a release detached by this test was still being detached 30 seconds after it ended"


@pytest.fixture
def service_kind():
    """A second plugin kind, registered for one test, that records every hook call."""
    from plugin_platform_helpers import SERVICE_KIND, SERVICE_KIND_API
    from provider_hub.kinds import ATTACH_LIVE, Kind, register_kind, unregister_kind

    class RecordingKind(Kind):
        name = SERVICE_KIND
        kind_apis = frozenset({SERVICE_KIND_API})
        attach_policy = ATTACH_LIVE
        noun = "service"

        def __init__(self):
            self.calls = []

        def on_installed(self, plugin_id, missing_required=()):
            self.calls.append(("installed", plugin_id))
            return True

        def on_removed(self, plugin_id):
            self.calls.append(("removed", plugin_id))
            return True

        def after_removed(self, plugin_id, keep_data=False):
            self.calls.append(("after_removed", plugin_id))
            self.kept = keep_data

        def set_enabled(self, installation, enabled):
            super().set_enabled(installation, enabled)
            self.calls.append(("enabled", installation["provider_id"], enabled))
            return True

        def on_config_saved(self, plugin_id):
            self.calls.append(("configured", plugin_id))

        def validate_manifest(self, manifest, trusted):
            # The shared bundle checks every kind has, so a test plugin can be staged.
            from provider_hub.manifest import validate_plugin_bundle
            return validate_plugin_bundle(manifest)

    kind = register_kind(RecordingKind())
    try:
        yield kind
    finally:
        unregister_kind(SERVICE_KIND)


@pytest.fixture
def own_process_candidates(monkeypatch):
    """Limit the Provider Hub's process scan to this test process and its descendants.

    The scan counts any unreadable process of ours that is newer than the test
    run as possibly using an environment, which is right in production and
    wrong on a busy machine: another session's sandboxed process, running
    beside the tests, would then block every cleanup. A fake ``/proc`` handed
    in by a test is listed as it is.
    """
    import provider_hub.venv as venvmod

    real = venvmod._candidate_pids

    def own(proc):
        names = real(proc)
        if os.path.realpath(proc) != "/proc":
            return names
        parent_of = {}
        for name in names:
            try:
                with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as handle:
                    parent_of[name] = handle.read().rsplit(")", 1)[1].split()[1]
            except (OSError, IndexError):
                continue
        mine = {str(os.getpid())}
        grew = True
        while grew:
            grew = False
            for name, parent in parent_of.items():
                if parent in mine and name not in mine:
                    mine.add(name)
                    grew = True
        return [name for name in names if name in mine]

    monkeypatch.setattr(venvmod, "_candidate_pids", own)


@pytest.fixture(autouse=True)
def _keep_the_scrub_registry():
    """Undo what a test does to the process-wide credential registry.

    The scrubber's registered values, and the three sets they are the union of
    (what the settings and Provider Hub collection found, what the database
    collection found, and the keys writers added), live in module globals. A test
    that registers a short value or adds an instance key would otherwise leave it
    masked in every later test. When the scrubber was already imported, its state
    is put back as it was; when the test was the first to import it, the state is
    cleared; when the module has gone, nothing is touched.
    """
    import sys

    scrub = sys.modules.get("secret_store.scrub")
    names = ("_VALUE_STATE", "_COLLECTED", "_DATABASE_VALUES", "_ADDED_VALUES", "_ADDED_SEQUENCE",
             "_DECLARED_VALUES")
    before = {name: getattr(scrub, name) for name in names} if scrub is not None else None
    yield
    after = sys.modules.get("secret_store.scrub")
    if after is None:
        return
    if before is None:
        # The scrubber was first imported by this test: whatever it registered
        # belongs to the test.
        after.clear_secret_values()
        return
    for name, value in before.items():
        setattr(after, name, value)

"""Shared fixtures for the dispatch tests: a database on SQLite or PostgreSQL, a real worker pool running the
fake translator, and a dispatcher wired to both."""
import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from plugin_platform_helpers import service_manifest
from provider_hub.runtime.pool import PoolLimits, WorkerDefinition, WorkerPool
from provider_hub.runtime.session import SessionLimits
from provider_hub.runtime.testing.fake_translator import fake_translator_command, fake_translator_env
from subtitles.tools.translate.runtime.dispatch import Dispatcher, JobSpec, PluginInfo, RetryWindows
from subtitles.tools.translate.runtime.gate import GateHolds
from subtitles.tools.translate.runtime.history import HistoryStore
from subtitles.tools.translate.runtime.host_calls import host_calls
from subtitles.tools.translate.runtime.jobs import TranslationJobs, target_key
from subtitles.tools.translate.runtime.store import TranslatorStore
from subtitles.tools.translate.runtime.usage import CostLimits
from subtitles.tools.translate.runtime.workers import PoolWorkers

PLUGIN = "ai_translator"
SECOND_PLUGIN = "second_engine"
SOURCE = "/media/film/film.en.srt"
LINES = ["One", "Two", "Three", "Four", "Five"]
KEY = "sk-dispatch-secret-0123456789"  # pragma: allowlist secret

TABLE_NAMES = (
    "TableTranslatorEngineInstance", "TableTranslatorConnection", "TableTranslatorSecret", "TableTranslatorJob",
    "TableTranslatorCheckpoint", "TableTranslationHistory", "TableTranslationHistoryConnection",
    "TableTranslationUsage", "TableTranslationBudget")


def tables():
    from app import database

    return [getattr(database, name).__table__ for name in TABLE_NAMES]


def make_engine(kind, tmp_path):
    from sqlalchemy import create_engine, event

    from app.database import Base

    if kind == "sqlite":
        from sqlalchemy.pool import NullPool

        from app.database import configure_sqlite_connection

        engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}", poolclass=NullPool,
                               isolation_level="AUTOCOMMIT")
        event.listen(engine, "connect", configure_sqlite_connection)
        Base.metadata.create_all(engine, tables=tables())
        return engine, None
    url = os.environ.get("BAZARR_PG_TEST_URL")
    if not url:
        pytest.skip("BAZARR_PG_TEST_URL is required for the PostgreSQL tests")
    import sqlalchemy as sa

    schema = "dispatch_" + uuid4().hex
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

    Base.metadata.create_all(engine, tables=tables())
    return engine, (admin, schema)


def manifest(plugin_id=PLUGIN):
    return service_manifest(
        plugin_id, settings_version=2, kind="translator", kind_api="bazarr.translator.v1",
        endpoints=["https://updates.example.test:443"],
        settings={"fields": [{"key": "credit_line", "type": "text", "max_length": 80}],
                  "backend_types": {
                      "openrouter": {"label": "OpenRouter", "endpoints": ["https://openrouter.ai:443"],
                                     "fields": [{"key": "api_key", "type": "secret"}]},
                      "openai_compatible": {"label": "OpenAI compatible", "endpoints": [],
                                            "fields": [{"key": "base_url", "type": "text", "destination": True,
                                                        "default": "http://lan.example.test:1234/v1"},
                                                       {"key": "api_key", "type": "secret"}]}}})


class MemoryPointers:
    def __init__(self, engine=None, fallback=None):
        self.engine, self.fallback = engine, fallback

    def get(self):
        return self.engine, self.fallback

    def set(self, engine, fallback):
        self.engine, self.fallback = engine, fallback
        return True


class FakeClock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 11, 15, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class FakeMonotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def usd(dollars):
    from subtitles.tools.translate.runtime.usage import to_micros

    return to_micros(dollars)


TARGET = "fr"


def _fast_session(**overrides):
    values = dict(handshake_timeout=10.0, heartbeat_interval=0.1, heartbeat_timeout=3.0, cancel_grace=0.5,
                  kill_grace=3.0, tick=0.02)
    values.update(overrides)
    return SessionLimits(**values)


class Env:
    """A database, a store, a pool of fake translator workers and a dispatcher, wired as production wires them."""

    def __init__(self, session, tmp_path):
        self.session = session
        self.tmp_path = tmp_path
        from subtitles.tools.translate.runtime import dispatch

        dispatch._maintained_at = None  # the upkeep is due again in every test
        self.pointers = MemoryPointers()
        self.active = {PLUGIN, SECOND_PLUGIN}
        self.store = TranslatorStore(session, pointers=self.pointers, plugin_active=lambda pid: pid in self.active)
        self.clock = FakeClock()
        self.monotonic = FakeMonotonic()
        self.scripts = {}
        self.limits = CostLimits()
        self.report = tmp_path / "report.jsonl"
        self.session_overrides = {}
        self.pool = WorkerPool(PoolLimits(tick=0.05, heartbeat_interval=0.1, acquire_timeout=2.0, close_grace=1.0,
                                          budget_interval=0.0, backoff_base=0.05, backoff_cap=0.2),
                               autostart=True)
        self.workers = PoolWorkers(self.pool, self._definition)
        self.abandoned = []
        self.holds = GateHolds()
        self.windows = RetryWindows(self.monotonic)
        self.installed = {PLUGIN: PluginInfo(PLUGIN, "AI Subtitle Translator", "2.2.0", "digest-a",
                                             manifest(PLUGIN)),
                          SECOND_PLUGIN: PluginInfo(SECOND_PLUGIN, "Second Engine", "1.0.0", "digest-b",
                                                    manifest(SECOND_PLUGIN))}

    def _definition(self, plugin_id):
        script = self.scripts.get(plugin_id, {})
        return WorkerDefinition(plugin_id, kind="translator", command=fake_translator_command(),
                                env=fake_translator_env(script, self.report, self.tmp_path / "attempts.json"),
                                host_calls=host_calls(), session_limits=_fast_session(**self.session_overrides))

    def dispatcher(self, **overrides):
        values = dict(store=self.store, workers=self.workers, plugins=lambda pid: self.installed.get(pid),
                      limits=lambda: self.limits, clock=self.clock, monotonic=self.monotonic, holds=self.holds,
                      windows=self.windows, poll=0.05, sleep=lambda _s: None)
        values.update(overrides)
        return Dispatcher(self.session, **values)

    # --- setup helpers
    def engine(self, plugin_id=PLUGIN, select=True, **kwargs):
        instance = self.store.create_instance(plugin_id, **kwargs)
        if select:
            self.pointers.engine = instance.id
        return instance

    def ai_engine(self, select=True, **kwargs):
        """The official engine with one free connection, which is what makes it configured."""
        instance = self.engine(PLUGIN, select=select, **kwargs)
        self.connection(instance)
        return instance

    def connection(self, instance, backend="openrouter", model="model-a", **kwargs):
        return self.store.add_connection(instance.id, backend, model=model, **kwargs)

    def requests(self):
        if not self.report.exists():
            return []
        return [json.loads(line) for line in self.report.read_text().splitlines()]

    def served(self):
        """The connection ids the worker was asked for, in order."""
        return [item["connection"] for item in self.requests()]

    # --- reads
    def job(self, job_id=None):
        jobs = TranslationJobs(self.session, clock=self.clock)
        return jobs.get(job_id) if job_id else jobs.active_for(SOURCE, TARGET)

    def last_job(self):
        from sqlalchemy import select

        from app.database import TableTranslatorJob

        return self.session.execute(select(TableTranslatorJob).order_by(TableTranslatorJob.id.desc())
                                    .execution_options(populate_existing=True)).scalars().first()

    def history(self, job):
        return HistoryStore(self.session, clock=self.clock).get_by_job(str(job.id))

    def checkpoints(self):
        from sqlalchemy import select

        from app.database import TableTranslatorCheckpoint

        return self.session.execute(select(TableTranslatorCheckpoint).execution_options(
            populate_existing=True)).scalars().all()

    def close(self):
        self.pool.close()


def make_spec(lines=LINES, target=TARGET, **overrides):
    values = dict(source_key=SOURCE, target_key=target_key(target), lines=list(lines), target_language=target,
                  source_language="en", history_media={"media_type": "movie", "media_id": 7, "media_title": "Film"},
                  media={"type": "movie", "title": "Film", "year": 2020})
    values.update(overrides)
    return JobSpec(**values)


def build_env(kind, tmp_path):
    """Yield an ``Env`` on the given database kind and clean everything up afterwards."""
    import sqlalchemy as sa
    from sqlalchemy.orm import scoped_session, sessionmaker

    engine, pg = make_engine(kind, tmp_path)
    scoped = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    holder = Env(scoped, tmp_path)
    try:
        yield holder
    finally:
        holder.close()
        scoped.remove()
        engine.dispose()
        if pg:
            admin, schema = pg
            with admin.begin() as connection:
                connection.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
            admin.dispose()

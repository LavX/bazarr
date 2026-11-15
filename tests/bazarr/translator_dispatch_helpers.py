"""Shared fixtures for the dispatch tests: a database on SQLite or PostgreSQL, a real worker pool running the
fake translator, and a dispatcher wired to both."""
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from plugin_platform_helpers import service_manifest

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

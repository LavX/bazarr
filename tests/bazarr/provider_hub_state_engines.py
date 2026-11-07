"""An AUTOCOMMIT engine built the way production builds it, on either backend.

Shared by the Plugin Hub state store tests. PostgreSQL runs in a disposable
schema of the server named by BAZARR_PG_TEST_URL and is skipped without one.
"""
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool


@pytest.fixture(params=["sqlite", "postgresql"])
def hub_engine(request, tmp_path):
    schema = None
    admin = None
    if request.param == "postgresql":
        url = os.environ.get("BAZARR_PG_TEST_URL")
        if not url:
            pytest.skip("BAZARR_PG_TEST_URL is required for real PostgreSQL coverage")
        schema = "hub_state_" + uuid.uuid4().hex
        admin = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = sa.create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True,
                                  pool_size=5, max_overflow=10, pool_pre_ping=True,
                                  connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = sa.create_engine(f"sqlite:///{tmp_path / 'bazarr.db'}", poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")

        @sa.event.listens_for(engine, "connect")
        def _configure(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.close()
    try:
        yield engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()


@pytest.fixture
def hub_store(hub_engine, tmp_path, monkeypatch):
    """The store pointed at the engine, with state.json under tmp_path."""
    from provider_hub import state_store
    monkeypatch.delenv("BAZARR_PROVIDER_HUB_STATE", raising=False)
    legacy = tmp_path / "provider_hub" / "state.json"
    monkeypatch.setattr(state_store, "legacy_state_path", lambda: legacy)
    state_store.use_engine(hub_engine)
    try:
        yield state_store
    finally:
        state_store.reset()


def raw_row(engine):
    """The stored row as a second connection sees it."""
    with engine.connect() as connection:
        row = connection.execute(sa.text(
            "SELECT revision, document, schema_version FROM provider_hub_state WHERE id = 1")).first()
    return None if row is None else tuple(row)

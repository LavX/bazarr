"""Safety properties of the Plugin Hub state on the database.

Credentials must stay out of database error text, the state.json copy must end
as the last committed state whichever writer publishes last, and a write that
loses a race must not repeat anything outside the document.
"""
import json
import logging
import traceback

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import provider_hub_state_engines as engines

hub_engine = engines.hub_engine
hub_store = engines.hub_store

SECRET = "plaintext-credential-9f3a"  # pragma: allowlist secret


# Database error text -------------------------------------------------------


def _refusing_engine(tmp_path):
    """A plain engine, as a caller outside the app could hand in, on a table that refuses the document."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'refusing.db'}", poolclass=NullPool,
                              isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE provider_hub_state (id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, "
            "revision INTEGER NOT NULL, document TEXT NOT NULL CHECK (length(document) < 5), "
            "updated_at DATETIME NOT NULL)")
    return engine


def test_a_failed_import_prints_no_credential(tmp_path, monkeypatch, caplog):
    from provider_hub import state_store
    legacy = tmp_path / "provider_hub" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"installations": {"examplehub": {"config": {"api_key": SECRET}}}}),
                      encoding="utf-8")
    monkeypatch.setattr(state_store, "legacy_state_path", lambda: legacy)
    monkeypatch.delenv("BAZARR_PROVIDER_HUB_STATE", raising=False)
    engine = _refusing_engine(tmp_path)
    state_store.use_engine(engine)
    try:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(sa.exc.DBAPIError) as failure:
                state_store.ensure_ready()
            logging.getLogger("provider_hub.test").error("import failed", exc_info=failure.value)
    finally:
        state_store.reset()
        engine.dispose()
    rendered = "".join(traceback.format_exception(failure.value))
    assert SECRET not in str(failure.value)
    assert SECRET not in rendered
    assert SECRET not in caplog.text
    assert "SQL parameters hidden" in str(failure.value)


def test_a_failed_write_prints_no_credential(hub_store, hub_engine, caplog):
    from provider_hub.state import mutate_state
    mutate_state(lambda state: state["jobs"].append({"id": "seed"}))
    with hub_engine.connect() as connection:
        # The row stops accepting a document longer than the one it holds now.
        length = connection.execute(sa.text("SELECT length(document) FROM provider_hub_state")).scalar()
        if hub_engine.dialect.name == "sqlite":
            connection.exec_driver_sql(
                "CREATE TRIGGER refuse BEFORE UPDATE ON provider_hub_state BEGIN "
                f"SELECT RAISE(ABORT, 'refused') WHERE length(NEW.document) > {length}; END")
        else:
            connection.exec_driver_sql(
                "ALTER TABLE provider_hub_state ADD CONSTRAINT refuse "
                f"CHECK (length(document) <= {length})")

    def add_credential(state):
        state["jobs"].append({"id": "later", "note": SECRET})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(sa.exc.DBAPIError) as failure:
            mutate_state(add_credential)
        logging.getLogger("provider_hub.test").error("write failed", exc_info=failure.value)
    assert SECRET not in "".join(traceback.format_exception(failure.value))
    assert SECRET not in caplog.text


def test_every_engine_the_store_uses_hides_its_parameters(tmp_path):
    from provider_hub import state_store
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'plain.db'}", poolclass=NullPool)
    assert not engine.hide_parameters
    state_store.use_engine(engine)
    try:
        assert state_store.engine() is engine
        assert engine.hide_parameters is True
    finally:
        state_store.reset()
        engine.dispose()

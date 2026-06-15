import os
import logging

import pytest

os.environ["NO_CLI"] = "true"
os.environ["SZ_USER_AGENT"] = "test"
os.environ["BAZARR_VERSION"] = "test" # fixme

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

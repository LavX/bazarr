import sqlite3


class _Cursor:
    def __init__(self):
        self.statements = []
        self.closed = False

    def execute(self, statement):
        self.statements.append(str(statement))

    def close(self):
        self.closed = True


class _DbapiConnection:
    def __init__(self):
        self.cursor_obj = _Cursor()

    def cursor(self):
        return self.cursor_obj


class _Dialect:
    def __init__(self, name):
        self.name = name


class _SqlConnection:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement):
        self.calls.append(str(statement))


class _Engine:
    def __init__(self, name="sqlite"):
        self.dialect = _Dialect(name)
        self.calls = []
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return _SqlConnection(self.calls)


def test_log_sqlite_runtime_version_logs_for_sqlite(monkeypatch, caplog):
    from app import database as db_module

    monkeypatch.setattr(db_module.sqlite3, "sqlite_version", "3.46.1")

    with caplog.at_level("INFO"):
        assert db_module.log_sqlite_runtime_version(_Engine()) is True

    assert "SQLite runtime version: 3.46.1" in caplog.text


def test_log_sqlite_runtime_version_skips_non_sqlite(caplog):
    from app import database as db_module

    with caplog.at_level("INFO"):
        assert db_module.log_sqlite_runtime_version(_Engine(name="postgresql")) is False

    assert "SQLite runtime version" not in caplog.text


def test_configure_sqlite_connection_applies_the_maintenance_pragmas():
    """Assert the resulting connection state, not the statements issued.

    This used to pass a stand-in object and compare the recorded SQL. The
    function now guards on the connection really being a sqlite3 one, because it
    is registered globally and must not fire PRAGMA at a PostgreSQL connection
    living in the same process. The stand-in fails that guard, so the function
    returned immediately and the test asserted against an empty list without
    anyone noticing: the file was never collected by CI.

    Checking the pragmas actually took effect is also the stronger assertion. It
    survives a rewrite of how the statements are issued, and it would catch a
    pragma that is sent but rejected.
    """
    from app import database as db_module

    connection = sqlite3.connect(":memory:")
    try:
        # sqlite already defaults synchronous to FULL, so asserting it after the
        # fact proves nothing: the assertion passes even with that pragma
        # deleted. Move it off the default first so the check is real.
        connection.execute("PRAGMA synchronous=OFF")

        db_module.configure_sqlite_connection(connection, None)

        cursor = connection.cursor()
        try:
            def value(pragma):
                cursor.execute(f"PRAGMA {pragma}")
                return cursor.fetchone()[0]

            # An in-memory database cannot use WAL, so journal_mode is asserted
            # against a file-backed database below instead.
            assert value("synchronous") == 2, "expected FULL"
            assert value("foreign_keys") == 1
            assert value("busy_timeout") == 60000
        finally:
            cursor.close()
    finally:
        connection.close()


def test_configure_sqlite_connection_enables_wal_on_a_file_database(tmp_path):
    """WAL is the one pragma an in-memory database cannot take."""
    from app import database as db_module

    connection = sqlite3.connect(str(tmp_path / "bazarr.db"))
    try:
        db_module.configure_sqlite_connection(connection, None)
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode")
            assert cursor.fetchone()[0].lower() == "wal"
        finally:
            cursor.close()
    finally:
        connection.close()


def test_configure_sqlite_connection_leaves_a_non_sqlite_connection_alone():
    """The guard exists because this listener is registered globally: firing
    PRAGMA at a PostgreSQL connection in the same process is invalid SQL."""
    from app import database as db_module

    dbapi_connection = _DbapiConnection()

    db_module.configure_sqlite_connection(dbapi_connection, None)

    assert dbapi_connection.cursor_obj.statements == []


def test_optimize_sqlite_database_is_skipped_for_non_sqlite(monkeypatch):
    from app import database as db_module

    monkeypatch.setattr(db_module.sqlite3, "sqlite_version_info", (3, 46, 0))
    engine = _Engine(name="postgresql")

    assert db_module.optimize_sqlite_database(engine) is False
    assert engine.connect_count == 0
    assert engine.calls == []


def test_optimize_sqlite_database_is_skipped_before_sqlite_346(monkeypatch):
    from app import database as db_module

    monkeypatch.setattr(db_module.sqlite3, "sqlite_version_info", (3, 45, 3))
    engine = _Engine()

    assert db_module.optimize_sqlite_database(engine) is False
    assert engine.connect_count == 0
    assert engine.calls == []


def test_optimize_sqlite_database_runs_on_sqlite_346_or_newer(monkeypatch):
    from app import database as db_module

    monkeypatch.setattr(db_module.sqlite3, "sqlite_version_info", (3, 46, 0))
    engine = _Engine()

    assert db_module.optimize_sqlite_database(engine) is True
    assert engine.calls == ["PRAGMA optimize"]

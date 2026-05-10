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


def test_configure_sqlite_connection_sets_wal_once():
    from app import database as db_module

    dbapi_connection = _DbapiConnection()

    db_module.configure_sqlite_connection(dbapi_connection, None)

    assert dbapi_connection.cursor_obj.statements == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=FULL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=60000",
    ]
    assert dbapi_connection.cursor_obj.closed is True


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

import logging

from sqlalchemy import create_engine, text

from utilities.sql_profiler import install_slow_query_log


def test_slow_query_logged_when_enabled(monkeypatch, caplog):
    monkeypatch.setenv("BAZARR_SQL_PROFILE", "1")
    engine = create_engine("sqlite:///:memory:")

    installed = install_slow_query_log(engine, threshold_ms=0)
    assert installed is True

    with caplog.at_level(logging.WARNING, logger="bazarr.sql_profiler"):
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    matching = [r for r in caplog.records if "SLOW_SQL" in r.getMessage()]
    assert matching, "expected at least one SLOW_SQL warning"
    assert matching[0].levelno == logging.WARNING


def test_noop_when_env_unset(monkeypatch, caplog):
    monkeypatch.delenv("BAZARR_SQL_PROFILE", raising=False)
    engine = create_engine("sqlite:///:memory:")

    installed = install_slow_query_log(engine, threshold_ms=0)
    assert installed is False
    assert not getattr(engine, "_bazarr_sql_profile_installed", False)

    with caplog.at_level(logging.WARNING, logger="bazarr.sql_profiler"):
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    assert not [r for r in caplog.records if "SLOW_SQL" in r.getMessage()]

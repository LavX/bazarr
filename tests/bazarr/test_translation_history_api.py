"""The translation history API: dark while the runtime is off, job and per-connection rows when it is on."""
from datetime import datetime

import pytest
from flask import Flask
from flask_restx import Api


@pytest.fixture
def client(monkeypatch, tmp_path):
    import sqlalchemy as sa
    from sqlalchemy.orm import scoped_session, sessionmaker

    from api.translation import history as history_api
    from app.config import settings
    from app import database as history_tables
    from app.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'history.db'}")
    Base.metadata.create_all(engine, tables=[
        getattr(history_tables, name).__table__ for name in (
            "TableTranslationHistory", "TableTranslationHistoryConnection", "TableTranslationUsage",
            "TableTranslationBudget")])
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(history_api, "database", session)

    app = Flask(__name__)
    api = Api(app)
    api.add_namespace(history_api.api_ns_translation_history, "/")
    test_client = app.test_client()
    test_client.session = session
    test_client.headers = {"X-API-KEY": settings.auth.apikey}
    try:
        yield test_client
    finally:
        session.remove()
        engine.dispose()


def _runtime(monkeypatch, enabled):
    from subtitles.tools.translate import runtime

    monkeypatch.setattr(runtime, "runtime_v2_enabled", lambda: enabled)


def _seed(session):
    from subtitles.tools.translate.runtime.history import HistoryStore
    from subtitles.tools.translate.runtime.usage import CostLimits, UsageLedger

    moment = datetime(2026, 11, 15, 12, 0, 0)
    store = HistoryStore(session, clock=lambda: moment)
    ledger = UsageLedger(session, clock=lambda: moment)
    limits = CostLimits.from_dollars(month_cap=50, job_cap=5)
    store.open_job("job-a", media_type="movie", media_id=1, arr_instance_id=2, target_language="hu",
                   plugin_id="ai_translator", plugin_version="2.2.0")
    ledger.reserve_job_budget("job-a", limits)
    store.note_attempt("job-a", connection_id=1, backend_type="openai_compatible", outcome="failed_over",
                       error_class="unreachable")
    store.note_attempt("job-a", connection_id=2, backend_type="openrouter", model="m")
    ledger.reserve_call("job-a", "c1", worst_case_micros=2_000_000, limits=limits, connection_id=2,
                        backend_type="openrouter", model="m")
    ledger.settle_call("job-a", "c1", input_tokens=10, output_tokens=5, actual_micros=1_500_000)
    store.finish_job("job-a", "completed")
    store.open_job("job-b", arr_instance_id=9)
    store.finish_job("job-b", "failed", error_class="validation")


def test_with_the_runtime_off_every_route_answers_404_without_reading_the_database(client, monkeypatch):
    _runtime(monkeypatch, False)
    monkeypatch.setattr("api.translation.history.HistoryStore",
                        lambda *a, **k: pytest.fail("the history was read with the runtime off"))
    assert client.get("/translation/history", headers=client.headers).status_code == 404
    assert client.get("/translation/history/1", headers=client.headers).status_code == 404
    # Without the key too: the guard sits outside authentication, so a missing route looks the same.
    assert client.get("/translation/history").status_code == 404


def test_the_routes_need_the_api_key(client, monkeypatch):
    _runtime(monkeypatch, True)
    assert client.get("/translation/history").status_code == 401


def test_the_list_returns_job_rows_with_their_connection_rows(client, monkeypatch):
    _runtime(monkeypatch, True)
    _seed(client.session)
    body = client.get("/translation/history", headers=client.headers).get_json()
    assert body["total"] == 2
    newest, oldest = body["data"]
    assert (newest["job_id"], newest["status"], newest["error_class"]) == ("job-b", "failed", "validation")
    assert oldest["job_id"] == "job-a" and oldest["failovers"] == 1
    assert (oldest["cost_micros"], oldest["cost"], oldest["input_tokens"]) == (1_500_000, 1.5, 10)
    first, second = oldest["connections"]
    assert (first["backend_type"], first["failed_over"], first["batches"]) == ("openai_compatible", True, 0)
    assert (second["backend_type"], second["models"], second["cost_micros"]) == ("openrouter", ["m"], 1_500_000)


def test_the_list_filters_and_pages(client, monkeypatch):
    _runtime(monkeypatch, True)
    _seed(client.session)
    only_failed = client.get("/translation/history?status=failed", headers=client.headers).get_json()
    assert [row["job_id"] for row in only_failed["data"]] == ["job-b"] and only_failed["total"] == 1
    one_instance = client.get("/translation/history?arr_instance_id=2", headers=client.headers).get_json()
    assert [row["job_id"] for row in one_instance["data"]] == ["job-a"]
    page = client.get("/translation/history?start=1&length=1", headers=client.headers).get_json()
    assert [row["job_id"] for row in page["data"]] == ["job-a"] and page["total"] == 2


def test_one_job_by_id_and_a_missing_one(client, monkeypatch):
    _runtime(monkeypatch, True)
    _seed(client.session)
    listed = client.get("/translation/history", headers=client.headers).get_json()["data"]
    row = client.get(f"/translation/history/{listed[1]['id']}", headers=client.headers).get_json()
    assert row["job_id"] == "job-a" and len(row["connections"]) == 2
    assert client.get("/translation/history/99999", headers=client.headers).status_code == 404


def test_the_namespace_is_registered_only_with_the_switch_on():
    import api as api_package
    from api.translation import api_ns_list_translation

    # The test configuration leaves the switch off, so the routes are not in the live list.
    assert api_ns_list_translation not in api_package.api_ns_list


def test_the_boot_time_create_all_does_not_build_the_ledger_tables():
    from app.database import MIGRATION_ONLY_TABLES

    assert {"translation_history", "translation_history_connections", "translation_usage",
            "translation_budgets"} <= MIGRATION_ONLY_TABLES


def test_a_switched_off_import_of_the_api_leaves_the_runtime_package_unloaded():
    import os
    import subprocess
    import sys
    from pathlib import Path

    bazarr = str(Path(__file__).parents[2] / "bazarr")
    code = ("import api, sys; "
            "print('runtime' if 'subtitles.tools.translate.runtime' in sys.modules else 'clean', "
            "'api.translation' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                            cwd=bazarr, env={**os.environ, "NO_CLI": "true",
                                             "PYTHONPATH": os.pathsep.join([bazarr, *sys.path])})
    assert result.stdout.strip().splitlines()[-1] == "clean False", result.stderr[-2000:]

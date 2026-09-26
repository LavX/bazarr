# coding=utf-8
"""Metrics endpoint against real PostgreSQL.

The aggregates lean on three things that differ between backends, so SQLite
passing proves little on its own:

* ``func.date`` returns TEXT on SQLite but a ``date`` object on PostgreSQL.
* ``int / int`` truncates on both, so the percentage needs a float multiplier.
* ``CAST(x AS INTEGER)`` rounds on PostgreSQL but truncates on SQLite, which
  would shift every histogram bucket by one.

Skips when no Postgres is reachable (set BAZARR_PG_TEST_URL, default the dev
container on 55432). CI provides a postgres service so this does NOT skip there.
"""
import os
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from flask import Flask

_PG_URL = os.environ.get(
    "BAZARR_PG_TEST_URL",
    "postgresql+psycopg://postgres:test@127.0.0.1:55432/bazarr")


def _pg_session():
    from sqlalchemy.orm import scoped_session, sessionmaker

    from app.database import Base

    try:
        engine = sa.create_engine(_PG_URL)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable at {_PG_URL}: {exc}")

    # Fresh schema per run so repeated runs do not accumulate rows.
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    return scoped_session(sessionmaker(bind=engine))


@pytest.fixture
def pg_session():
    session = _pg_session()
    yield session
    session.remove()


def _call(metrics, query=""):
    app = Flask(__name__)
    with app.test_request_context(f"/api/history/metrics{query}"):
        return metrics.HistoryMetrics.get.__wrapped__(metrics.HistoryMetrics())


def _episode(session, **over):
    from app.database import TableHistory

    base = dict(
        action=1,
        description="downloaded",
        language="en",
        provider="provider-a",
        score=360,
        score_out_of=360,
        timestamp=datetime.now() - timedelta(days=1),
        video_path="/tv/a.mkv",
    )
    base.update(over)
    session.add(TableHistory(**base))


def test_peak_day_is_a_string_on_postgres(pg_session, monkeypatch):
    """func.date yields a date object here, not TEXT. Without normalising it,
    the JSON shape differs per backend and the chart's date key stops matching.
    """
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", pg_session)
    busy = datetime.now() - timedelta(days=2)
    _episode(pg_session, timestamp=busy)
    _episode(pg_session, timestamp=busy)
    _episode(pg_session, timestamp=datetime.now() - timedelta(days=1))
    pg_session.flush()

    body = _call(metrics, "?timeFrame=week")

    assert isinstance(body["totals"]["peakDate"], str)
    assert body["totals"]["peakDate"] == busy.strftime("%Y-%m-%d")
    assert body["totals"]["peakCount"] == 2


def test_score_percentage_is_not_integer_divided_on_postgres(pg_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", pg_session)
    # 90 of 360 is 25%. Integer division would report 0.
    _episode(pg_session, provider="alpha", score=90, score_out_of=360)
    pg_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["avgScorePct"] == 25.0


def test_histogram_buckets_match_sqlite_on_postgres(pg_session, monkeypatch):
    """CAST rounds on PostgreSQL and truncates on SQLite, so 29% must not land
    in bucket 3 here while landing in bucket 2 there."""
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", pg_session)
    _episode(pg_session, score=36, score_out_of=360)    # 10% -> bucket 1
    _episode(pg_session, score=104, score_out_of=360)   # 28.9% -> bucket 2
    _episode(pg_session, score=360, score_out_of=360)   # 100% -> overflow
    pg_session.flush()

    body = _call(metrics)

    counts = {b["bucket"]: b["count"] for b in body["scoreHistogram"]}
    assert counts[1] == 1
    assert counts[2] == 1
    assert counts[10] == 1


def test_zero_score_denominator_coalesces_on_postgres(pg_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", pg_session)
    _episode(pg_session, provider="alpha", score=0, score_out_of=None)
    _episode(pg_session, provider="alpha", score=360, score_out_of=360)
    pg_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["count"] == 2
    assert alpha["avgScorePct"] == 50.0


def test_unscored_rows_are_neither_averaged_nor_bucketed_on_postgres(pg_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", pg_session)
    _episode(pg_session, provider="alpha", score=360, score_out_of=360)
    _episode(pg_session, provider="alpha", score=None, score_out_of=None)
    pg_session.flush()

    body = _call(metrics)

    alpha = next(p for p in body["byProvider"] if p["provider"] == "alpha")
    assert alpha["count"] == 2
    assert alpha["avgScorePct"] == 100.0
    counts = {b["bucket"]: b["count"] for b in body["scoreHistogram"]}
    assert counts[10] == 1
    assert sum(counts.values()) == 1


def test_language_variants_match_on_postgres(pg_session, monkeypatch):
    from api.history import metrics

    monkeypatch.setattr(metrics, "database", pg_session)
    _episode(pg_session, language="en")
    _episode(pg_session, language="en:hi")
    _episode(pg_session, language="hu")
    pg_session.flush()

    body = _call(metrics, "?language=en")

    assert body["totals"]["downloads"] == 2

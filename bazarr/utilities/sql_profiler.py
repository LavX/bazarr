# coding=utf-8
"""Dev-only SQLAlchemy slow-query log. Enable by exporting

    BAZARR_SQL_PROFILE=1
    BAZARR_SQL_PROFILE_THRESHOLD_MS=100   # optional, default 100

before starting Bazarr. The two listeners below are no-ops at module
import time; they are only registered when install_slow_query_log()
is called explicitly from app/database.py (which itself only fires
under the env-var gate). When the env var is unset, the listeners
are never attached and SQLAlchemy goes through its normal hot path
with zero overhead.
"""

import logging
import os
import time
import traceback
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger("bazarr.sql_profiler")


def _is_enabled() -> bool:
    return os.getenv("BAZARR_SQL_PROFILE", "").lower() in ("1", "true", "yes", "on")


def _threshold_ms() -> int:
    raw = os.getenv("BAZARR_SQL_PROFILE_THRESHOLD_MS", "100")
    try:
        return max(0, int(raw))
    except ValueError:
        return 100


def _truncate(value, limit: int = 200) -> str:
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _short_callsite() -> str:
    """Last bazarr/ frame in the stack, so we can attribute slow queries
    to a specific module:line without printing the entire trace."""
    for frame in reversed(traceback.extract_stack()[:-3]):
        if "/bazarr/" in frame.filename and "/utilities/sql_profiler.py" not in frame.filename:
            return f"{frame.filename}:{frame.lineno} in {frame.name}"
    return "<unknown>"


def install_slow_query_log(engine: Engine, threshold_ms: Optional[int] = None) -> bool:
    """Attach before/after_cursor_execute listeners. Returns True iff
    listeners were actually installed (i.e. env-var gate passed). Safe
    to call multiple times; second call is a no-op."""
    if not _is_enabled():
        return False
    if getattr(engine, "_bazarr_sql_profile_installed", False):
        return False

    threshold = threshold_ms if threshold_ms is not None else _threshold_ms()

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._bazarr_query_start = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        start = getattr(context, "_bazarr_query_start", None)
        if start is None:
            return
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if elapsed_ms < threshold:
            return
        single_line = " ".join(statement.split())
        logger.warning(
            "SLOW_SQL %.1fms callsite=%s sql=%s params=%s",
            elapsed_ms,
            _short_callsite(),
            single_line,
            _truncate(parameters),
        )

    engine._bazarr_sql_profile_installed = True
    logger.info("Slow-query log enabled (threshold=%dms)", threshold)
    return True

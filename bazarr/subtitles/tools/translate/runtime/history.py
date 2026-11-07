# coding=utf-8
"""Translation history: one row per job and one per connection that contributed to it.

The rows are written from Bazarr+'s own records of what happened, never from what a
plugin says about itself. A job that failed over part way is therefore attributed
to every connection and model it used, including a connection that failed over
before it produced a batch and what an attempt spent after its last checkpoint.
Cost and tokens come from the usage ledger (``usage.py``); this module keeps the
rest: who asked, which engine, the status, the failovers and the batches.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import func, select

from app.database import (TableTranslationHistory, TableTranslationHistoryConnection,
                          TableTranslationUsage)

from .transaction import ledger_transaction

MICROS_PER_DOLLAR = 1_000_000

RUNNING = "running"
FINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled", "superseded", "declined"})
# A failed job that will be tried again later is not finished.
STATUSES = FINAL_STATUSES | {RUNNING, "awaiting_retry"}
ATTEMPT_OUTCOMES = frozenset({"ok", "failed_over", "failed"})
ERROR_DETAIL_LIMIT = 500
ERROR_CLASS_LIMIT = 100


class HistoryError(ValueError):
    """A request the history refuses, with a reason a person can read."""


def _counted_cost():
    """What a usage row counts for: its actual cost, or its worst case while unsettled."""
    return func.coalesce(TableTranslationUsage.actual_micros, TableTranslationUsage.worst_case_micros)


def _scrubbed(value: str | None, limit: int = ERROR_DETAIL_LIMIT) -> str | None:
    if not value:
        return None
    from secret_store.scrub import scrub_text

    return str(scrub_text(str(value)))[:limit]


def _loads_list(text: str | None) -> list[str]:
    try:
        value = json.loads(text) if text else []
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class HistoryStore:
    def __init__(self, session, *, clock: Callable[[], datetime] = datetime.now):
        self._session = session
        self._clock = clock

    # ------------------------------------------------------------------ writes
    def open_job(self, job_id: str, *, media_type: str | None = None, media_id: int | None = None,
                 media_title: str | None = None, arr_instance_id: int | None = None,
                 source_language: str | None = None, target_language: str | None = None,
                 plugin_id: str | None = None, plugin_version: str | None = None,
                 engine_instance_id: int | None = None) -> int:
        """Create the job's row, or reopen it for another attempt. Returns the row id."""
        if not job_id:
            raise HistoryError("A history row needs a job id")
        now = self._clock()
        with ledger_transaction(self._session):
            row = self._row(job_id)
            if row is None:
                row = TableTranslationHistory(job_id=job_id, started_at=now, created_at=now)
                self._session.add(row)
            row.status = RUNNING
            row.finished_at = None
            row.error_class = None
            row.error_detail = None
            for name, value in (("media_type", media_type), ("media_id", media_id),
                                ("media_title", media_title), ("arr_instance_id", arr_instance_id),
                                ("source_language", source_language), ("target_language", target_language),
                                ("plugin_id", plugin_id), ("plugin_version", plugin_version),
                                ("engine_instance_id", engine_instance_id)):
                if value is not None:
                    setattr(row, name, value)
            row.updated_at = now
            self._session.flush()
            return row.id

    def note_attempt(self, job_id: str, *, connection_id: int, backend_type: str = "", model: str = "",
                     outcome: str = "ok", error_class: str | None = None) -> None:
        """Record that an attempt ran on a connection, whether or not it produced a batch."""
        if outcome not in ATTEMPT_OUTCOMES:
            raise HistoryError(f"Unknown attempt outcome: {outcome}")
        now = self._clock()
        with ledger_transaction(self._session):
            history = self._require(job_id)
            row = self._connection_row(history, connection_id, backend_type)
            row.attempts += 1
            if model:
                self._add_model(row, model)
            if outcome != "ok":
                row.last_error_class = _scrubbed(error_class, ERROR_CLASS_LIMIT)
            if outcome == "failed_over":
                row.failed_over = 1
                history.failovers += 1
            row.updated_at = history.updated_at = now
            self._session.flush()

    def record_batches(self, job_id: str, *, connection_id: int, model: str = "", backend_type: str = "",
                       count: int = 1) -> None:
        """Count batches a connection translated, and the model that served them."""
        if count < 1:
            raise HistoryError("A batch count is at least 1")
        now = self._clock()
        with ledger_transaction(self._session):
            history = self._require(job_id)
            row = self._connection_row(history, connection_id, backend_type)
            row.batches += count
            if model:
                self._add_model(row, model)
            row.updated_at = history.updated_at = now
            self._session.flush()

    def note_declined(self, job_id: str, *, instance_id: int | None, reason: str | None = None,
                      taken_over_by: int | None = None) -> None:
        """Record the engine that declined the job and, when there was one, the engine that took it over."""
        now = self._clock()
        with ledger_transaction(self._session):
            history = self._require(job_id)
            history.declined_by_instance_id = instance_id
            history.declined_reason = _scrubbed(reason)
            history.taken_over_by_instance_id = taken_over_by
            history.updated_at = now
            self._session.flush()

    def finish_job(self, job_id: str, status: str, *, error_class: str | None = None,
                   error_detail: str | None = None) -> None:
        """Set the job's status, and its end time for a status that ends it. Totals are brought up to date."""
        if status not in STATUSES or status == RUNNING:
            raise HistoryError(f"Not a status a job can end with: {status}")
        now = self._clock()
        with ledger_transaction(self._session):
            history = self._require(job_id)
            history.status = status
            history.error_class = _scrubbed(error_class, ERROR_CLASS_LIMIT)
            history.error_detail = _scrubbed(error_detail)
            history.finished_at = now if status in FINAL_STATUSES else None
            history.updated_at = now
            self.refresh_in_transaction(job_id)
            self._session.flush()

    def refresh_totals(self, job_id: str) -> None:
        """Rebuild the job's tokens and cost, and each connection's, from the usage ledger."""
        with ledger_transaction(self._session):
            self.refresh_in_transaction(job_id)

    def refresh_in_transaction(self, job_id: str) -> None:
        """``refresh_totals`` for a caller that already holds the ledger transaction.

        A job with no history row has nothing to refresh: usage can be recorded before
        a row is opened, and the row picks it up the first time it is refreshed.
        """
        self._session.flush()  # the reads below replace what a loaded row holds, so write pending changes first
        history = self._row(job_id)
        if history is None:
            return
        usage = self._session.execute(
            select(TableTranslationUsage.connection_id, TableTranslationUsage.backend_type,
                   TableTranslationUsage.model, TableTranslationUsage.input_tokens,
                   TableTranslationUsage.output_tokens, _counted_cost().label("micros"))
            .where(TableTranslationUsage.job_id == job_id)).all()
        history.input_tokens = sum(item.input_tokens for item in usage)
        history.output_tokens = sum(item.output_tokens for item in usage)
        history.cost_micros = int(sum(item.micros for item in usage))
        by_connection: dict[int, list] = {}
        for item in usage:
            if item.connection_id is not None:
                by_connection.setdefault(item.connection_id, []).append(item)
        for connection_id, items in by_connection.items():
            row = self._connection_row(history, connection_id, next((i.backend_type for i in items if i.backend_type), ""))
            row.input_tokens = sum(i.input_tokens for i in items)
            row.output_tokens = sum(i.output_tokens for i in items)
            row.cost_micros = int(sum(i.micros for i in items))
            for model in sorted({i.model for i in items if i.model}):
                self._add_model(row, model)
        history.updated_at = self._clock()
        self._session.flush()

    # ------------------------------------------------------------------- reads
    def get(self, history_id: int) -> dict[str, Any] | None:
        row = self._session.get(TableTranslationHistory, history_id)
        if row is None:
            return None
        return self._serialize([row])[0]

    def get_by_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._row(job_id)
        return None if row is None else self._serialize([row])[0]

    def list_jobs(self, *, start: int = 0, length: int = 50, status: str | None = None,
             arr_instance_id: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Newest first. Returns the page and the number of rows that match."""
        query = select(TableTranslationHistory)
        count = select(func.count()).select_from(TableTranslationHistory)
        if status:
            query = query.where(TableTranslationHistory.status == status)
            count = count.where(TableTranslationHistory.status == status)
        if arr_instance_id is not None:
            query = query.where(TableTranslationHistory.arr_instance_id == arr_instance_id)
            count = count.where(TableTranslationHistory.arr_instance_id == arr_instance_id)
        total = int(self._session.execute(count).scalar() or 0)
        query = query.order_by(TableTranslationHistory.id.desc()).offset(max(0, start))
        if length is not None and length >= 0:
            query = query.limit(length)
        rows = list(self._session.execute(query).scalars().all())
        return self._serialize(rows), total

    # ----------------------------------------------------------------- helpers
    def _row(self, job_id: str):
        return self._session.execute(
            select(TableTranslationHistory).where(TableTranslationHistory.job_id == job_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()

    def _require(self, job_id: str):
        row = self._row(job_id)
        if row is None:
            raise HistoryError(f"No history row for job {job_id}")
        return row

    def _connection_row(self, history, connection_id: int, backend_type: str):
        row = self._session.execute(
            select(TableTranslationHistoryConnection)
            .where(TableTranslationHistoryConnection.history_id == history.id,
                   TableTranslationHistoryConnection.connection_id == connection_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if row is None:
            now = self._clock()
            row = TableTranslationHistoryConnection(
                history_id=history.id, connection_id=connection_id, backend_type=backend_type or "",
                models_json="[]", attempts=0, batches=0, input_tokens=0, output_tokens=0, cost_micros=0,
                failed_over=0, created_at=now, updated_at=now)
            self._session.add(row)
            self._session.flush()
        elif backend_type and not row.backend_type:
            row.backend_type = backend_type
        return row

    @staticmethod
    def _add_model(row, model: str) -> None:
        models = _loads_list(row.models_json)
        if model not in models:
            models.append(model)
            row.models_json = json.dumps(models)

    def _serialize(self, rows) -> list[dict[str, Any]]:
        ids = [row.id for row in rows]
        by_history: dict[int, list] = {}
        if ids:
            connections = self._session.execute(
                select(TableTranslationHistoryConnection)
                .where(TableTranslationHistoryConnection.history_id.in_(ids))
                .order_by(TableTranslationHistoryConnection.id)).scalars().all()
            for connection in connections:
                by_history.setdefault(connection.history_id, []).append(connection)
        return [{
            "id": row.id,
            "job_id": row.job_id,
            "media_type": row.media_type,
            "media_id": row.media_id,
            "media_title": row.media_title,
            "arr_instance_id": row.arr_instance_id,
            "source_language": row.source_language,
            "target_language": row.target_language,
            "plugin_id": row.plugin_id,
            "plugin_version": row.plugin_version,
            "engine_instance_id": row.engine_instance_id,
            "status": row.status,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cost_micros": row.cost_micros,
            "cost": row.cost_micros / MICROS_PER_DOLLAR,
            "error_class": row.error_class,
            "error_detail": row.error_detail,
            "failovers": row.failovers,
            "declined_by_instance_id": row.declined_by_instance_id,
            "declined_reason": row.declined_reason,
            "taken_over_by_instance_id": row.taken_over_by_instance_id,
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "connections": [{
                "connection_id": item.connection_id,
                "backend_type": item.backend_type,
                "models": _loads_list(item.models_json),
                "attempts": item.attempts,
                "batches": item.batches,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "cost_micros": item.cost_micros,
                "cost": item.cost_micros / MICROS_PER_DOLLAR,
                "failed_over": bool(item.failed_over),
                "last_error_class": item.last_error_class,
            } for item in by_history.get(row.id, [])],
        } for row in rows]

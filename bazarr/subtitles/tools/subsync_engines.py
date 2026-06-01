# coding=utf-8

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


SYNC_ENGINES = ("ffsubsync", "autosubsync", "alass")
SYNC_ENGINE_OUTPUT_MARKERS = tuple(f".{engine}." for engine in SYNC_ENGINES)
SYNC_ENGINE_LANGUAGE_MODIFIERS = tuple(f"sync-{engine}" for engine in SYNC_ENGINES)
DEFAULT_ENABLED_ENGINES = list(SYNC_ENGINES)
OUTPUT_MODE_OVERWRITE = "overwrite"
OUTPUT_MODE_KEEP_ALL = "keep_all"
SUPPORTED_OUTPUT_MODES = (OUTPUT_MODE_OVERWRITE, OUTPUT_MODE_KEEP_ALL)
FAILURE_THRESHOLD = 3

RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"


class MissingSyncEngineError(RuntimeError):
    def __init__(self, engine, message):
        super().__init__(message)
        self.engine = engine


@dataclass
class SyncEngineResult:
    engine: str
    status: str
    output_path: str = None
    generated_path: str = None
    reason: str = None
    message: str = None
    raw_result: object = None

    @property
    def success(self):
        return self.status == RESULT_SUCCESS

    @property
    def failed(self):
        return self.status == RESULT_FAILED

    @property
    def skipped(self):
        return self.status == RESULT_SKIPPED

    def as_dict(self):
        return {
            "engine": self.engine,
            "status": self.status,
            "output_path": self.output_path,
            "generated_path": self.generated_path,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass
class SyncRunResult:
    source_path: str
    output_mode: str
    results: list = field(default_factory=list)

    @property
    def success(self):
        return bool(self.successful_results)

    @property
    def successful_results(self):
        return [item for item in self.results if item.success]

    @property
    def failed_results(self):
        return [item for item in self.results if item.failed]

    @property
    def skipped_results(self):
        return [item for item in self.results if item.skipped]

    @property
    def first_success(self):
        successes = self.successful_results
        return successes[0] if successes else None

    def as_dict(self):
        return {
            "source_path": self.source_path,
            "output_mode": self.output_mode,
            "success": self.success,
            "results": [item.as_dict() for item in self.results],
        }


class InMemorySubsyncFailureStore:
    def __init__(self, failure_threshold=FAILURE_THRESHOLD):
        self.failure_threshold = failure_threshold
        self._failures = {}

    @staticmethod
    def _key(subtitle_path, engine):
        return os.path.normcase(os.path.abspath(subtitle_path)), engine

    def failure_count(self, subtitle_path, engine):
        return self._failures.get(self._key(subtitle_path, engine), 0)

    def should_skip(self, subtitle_path, engine):
        return self.failure_count(subtitle_path, engine) >= self.failure_threshold

    def record_success(self, subtitle_path, engine):
        self._failures.pop(self._key(subtitle_path, engine), None)

    def reset(self, subtitle_path=None, engine=None):
        if subtitle_path is None and engine is None:
            self._failures.clear()
            return
        for key in list(self._failures):
            subtitle_matches = (
                subtitle_path is None or key[0] == self._key(subtitle_path, key[1])[0]
            )
            engine_matches = engine is None or key[1] == engine
            if subtitle_matches and engine_matches:
                self._failures.pop(key, None)

    def record_failure(self, subtitle_path, engine, message=None):
        key = self._key(subtitle_path, engine)
        self._failures[key] = self._failures.get(key, 0) + 1
        return self._failures[key]


class DatabaseSubsyncFailureStore:
    def __init__(self, failure_threshold=FAILURE_THRESHOLD):
        self.failure_threshold = failure_threshold

    @staticmethod
    def _subtitle_key(subtitle_path):
        return os.path.normcase(os.path.abspath(subtitle_path))

    def _row(self, subtitle_path, engine):
        from app.database import TableSubsyncEngineFailure, database, select

        return database.execute(
            select(TableSubsyncEngineFailure)
            .where(
                TableSubsyncEngineFailure.subtitle_path
                == self._subtitle_key(subtitle_path)
            )
            .where(TableSubsyncEngineFailure.engine == engine)
        ).first()

    def failure_count(self, subtitle_path, engine):
        row = self._row(subtitle_path, engine)
        return row[0].consecutive_failures if row else 0

    def should_skip(self, subtitle_path, engine):
        row = self._row(subtitle_path, engine)
        if not row:
            return False
        failure = row[0]
        return (
            bool(failure.is_skipped)
            or failure.consecutive_failures >= self.failure_threshold
        )

    def record_success(self, subtitle_path, engine):
        from app.database import TableSubsyncEngineFailure, database, delete

        database.execute(
            delete(TableSubsyncEngineFailure)
            .where(
                TableSubsyncEngineFailure.subtitle_path
                == self._subtitle_key(subtitle_path)
            )
            .where(TableSubsyncEngineFailure.engine == engine)
        )

    def reset(self, subtitle_path=None, engine=None):
        from app.database import TableSubsyncEngineFailure, database, delete

        stmt = delete(TableSubsyncEngineFailure)
        if subtitle_path is not None:
            stmt = stmt.where(
                TableSubsyncEngineFailure.subtitle_path
                == self._subtitle_key(subtitle_path)
            )
        if engine is not None:
            stmt = stmt.where(TableSubsyncEngineFailure.engine == engine)
        database.execute(stmt)

    def record_failure(self, subtitle_path, engine, message=None):
        from datetime import datetime

        from app.database import TableSubsyncEngineFailure, database, insert, update

        subtitle_key = self._subtitle_key(subtitle_path)
        row = self._row(subtitle_path, engine)
        now = datetime.now()
        if row:
            failure = row[0]
            count = (failure.consecutive_failures or 0) + 1
            database.execute(
                update(TableSubsyncEngineFailure)
                .where(TableSubsyncEngineFailure.id == failure.id)
                .values(
                    consecutive_failures=count,
                    is_skipped=count >= self.failure_threshold,
                    last_error=message,
                    updated_at=now,
                )
            )
            return count

        database.execute(
            insert(TableSubsyncEngineFailure).values(
                subtitle_path=subtitle_key,
                engine=engine,
                consecutive_failures=1,
                is_skipped=False,
                last_error=message,
                created_at=now,
                updated_at=now,
            )
        )
        return 1


def normalize_output_mode(output_mode):
    if output_mode in SUPPORTED_OUTPUT_MODES:
        return output_mode
    return OUTPUT_MODE_OVERWRITE


def normalize_enabled_engines(enabled_engines):
    if enabled_engines is None:
        return list(DEFAULT_ENABLED_ENGINES)
    if isinstance(enabled_engines, str):
        enabled_engines = [
            item.strip() for item in enabled_engines.split(",") if item.strip()
        ]
    enabled = set(enabled_engines or [])
    return [engine for engine in SYNC_ENGINES if engine in enabled]


def engine_output_path(srt_path, engine):
    path = Path(srt_path)
    if engine not in SYNC_ENGINES:
        raise ValueError(f"Unsupported sync engine: {engine}")
    suffix = path.suffix or ".srt"
    return path.with_name(f"{path.stem}.{engine}{suffix}")


def temporary_engine_output_path(srt_path, engine):
    path = Path(srt_path)
    suffix = path.suffix or ".srt"
    fd, temp_path = tempfile.mkstemp(
        prefix=f".bazarr-sync-{engine}-", suffix=suffix, dir=str(path.parent)
    )
    os.close(fd)
    os.unlink(temp_path)
    return Path(temp_path)


def sync_engine_from_output_path(path):
    filename = os.path.basename(str(path)).lower()
    stem, extension = os.path.splitext(filename)
    if not extension:
        return None

    parts = stem.split(".")
    if len(parts) < 2:
        return None

    engine = parts[-1]
    return engine if engine in SYNC_ENGINES else None


def is_sync_engine_output(path):
    return sync_engine_from_output_path(path) is not None


def is_sync_engine_language_key(language):
    if not isinstance(language, str):
        return False

    modifiers = language.split(":")[1:]
    return any(
        modifier.lower() in SYNC_ENGINE_LANGUAGE_MODIFIERS for modifier in modifiers
    )


class SubsyncEngineRunner:
    def __init__(self, failure_store=None, failure_threshold=FAILURE_THRESHOLD):
        self.failure_store = failure_store or DatabaseSubsyncFailureStore(
            failure_threshold=failure_threshold
        )
        self.failure_threshold = failure_threshold

    def _existing_keep_all_output_is_current(self, srt_path, output_path, engine):
        if self.failure_store.failure_count(srt_path, engine) > 0:
            return False

        try:
            source_stat = Path(srt_path).stat()
            output_stat = output_path.stat()
        except OSError:
            return False

        return (
            output_stat.st_size > 0
            and output_stat.st_mtime_ns >= source_stat.st_mtime_ns
        )

    def run(
        self, srt_path, output_mode, enabled_engines, execute_engine, force_sync=False
    ):
        output_mode = normalize_output_mode(output_mode)
        result = SyncRunResult(source_path=srt_path, output_mode=output_mode)

        if is_sync_engine_output(srt_path):
            result.results.append(
                SyncEngineResult(
                    engine="all",
                    status=RESULT_SKIPPED,
                    reason="generated_source",
                    message="Generated sync output is not used as a source subtitle.",
                )
            )
            return result

        for engine in normalize_enabled_engines(enabled_engines):
            final_engine_output_path = engine_output_path(srt_path, engine)
            output_path = (
                final_engine_output_path
                if output_mode == OUTPUT_MODE_KEEP_ALL
                else temporary_engine_output_path(srt_path, engine)
            )

            if self.failure_store.should_skip(srt_path, engine) and not force_sync:
                result.results.append(
                    SyncEngineResult(
                        engine=engine,
                        status=RESULT_SKIPPED,
                        output_path=str(final_engine_output_path),
                        reason="failure_threshold",
                        message=f"{engine} skipped after {self.failure_threshold} consecutive failures.",
                    )
                )
                continue

            if (
                output_mode == OUTPUT_MODE_KEEP_ALL
                and final_engine_output_path.is_file()
                and not force_sync
            ):
                if self._existing_keep_all_output_is_current(
                    srt_path, final_engine_output_path, engine
                ):
                    result.results.append(
                        SyncEngineResult(
                            engine=engine,
                            status=RESULT_SKIPPED,
                            output_path=str(final_engine_output_path),
                            reason="output_exists",
                            message="Generated sync output already exists.",
                        )
                    )
                    continue

            try:
                if output_path.is_file():
                    output_path.unlink()

                raw_result = execute_engine(engine, output_path)
                if not output_path.is_file():
                    raise RuntimeError(
                        f"{engine} did not create a synced subtitle file."
                    )
                if output_path.stat().st_size == 0:
                    raise RuntimeError(
                        f"{engine} created an empty synced subtitle file."
                    )

                generated_path = str(output_path)
                final_output_path = output_path
                if output_mode == OUTPUT_MODE_OVERWRITE:
                    os.replace(str(output_path), srt_path)
                    final_output_path = Path(srt_path)
                    generated_path = None

                self.failure_store.record_success(srt_path, engine)
                result.results.append(
                    SyncEngineResult(
                        engine=engine,
                        status=RESULT_SUCCESS,
                        output_path=str(final_output_path),
                        generated_path=generated_path,
                        raw_result=raw_result,
                    )
                )

                if output_mode == OUTPUT_MODE_OVERWRITE:
                    break

            except MissingSyncEngineError as exc:
                logging.warning("BAZARR %s sync engine skipped: %s", engine, exc)
                result.results.append(
                    SyncEngineResult(
                        engine=engine,
                        status=RESULT_SKIPPED,
                        output_path=str(final_engine_output_path),
                        reason="missing_engine",
                        message=str(exc),
                    )
                )
            except Exception as exc:
                logging.exception(
                    "BAZARR %s sync engine failed for %s", engine, srt_path
                )
                if output_path.is_file():
                    output_path.unlink()
                self.failure_store.record_failure(srt_path, engine, str(exc)[:500])
                result.results.append(
                    SyncEngineResult(
                        engine=engine,
                        status=RESULT_FAILED,
                        output_path=str(final_engine_output_path),
                        reason="engine_failed",
                        message=str(exc),
                    )
                )

        return result

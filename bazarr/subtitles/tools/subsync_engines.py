# coding=utf-8

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


SYNC_ENGINES = ('ffsubsync', 'autosubsync', 'alass')
SYNC_ENGINE_OUTPUT_MARKERS = tuple(f'.{engine}.' for engine in SYNC_ENGINES)
SYNC_ENGINE_LANGUAGE_MODIFIERS = tuple(f'sync-{engine}' for engine in SYNC_ENGINES)
DEFAULT_ENABLED_ENGINES = list(SYNC_ENGINES)
OUTPUT_MODE_OVERWRITE = 'overwrite'
OUTPUT_MODE_KEEP_ALL = 'keep_all'
SUPPORTED_OUTPUT_MODES = (OUTPUT_MODE_OVERWRITE, OUTPUT_MODE_KEEP_ALL)
FAILURE_THRESHOLD = 3

# Handed to ffsubsync in place of the user's maximum offset so it searches without a
# window. ffsubsync masks out-of-window candidates in MaxScoreAligner.transform, i.e.
# after the full correlation is computed, so a window never saves work: it only hides
# the alignment the engine would otherwise have chosen, and makes it return the best
# in-window candidate instead. A day is beyond any real audio/subtitle offset, so the
# mask is a no-op while still exercising ffsubsync's normal (non-None) code path.
UNCONSTRAINED_MAX_OFFSET_SECONDS = 86400.0

RESULT_SUCCESS = 'success'
RESULT_FAILED = 'failed'
RESULT_SKIPPED = 'skipped'

# Why an engine produced no output. This is reporting vocabulary: every code here is
# rendered into a sentence for the user in subtitles/sync.py, so the four ways a run
# can come back empty stay distinguishable without opening System > Logs.
#
#   generated_source  the source subtitle is itself a generated sync output
#   output_exists     a current output for this engine was already on disk
#   missing_engine    the executable or Python package is absent
#   failure_threshold the engine was skipped after repeated failures on this file
#   engine_declined   the engine ran to completion and rejected its own result
#   result_rejected   the engine returned a result Bazarr refused to accept
#   engine_failed     the engine errored out
REASON_GENERATED_SOURCE = 'generated_source'
REASON_OUTPUT_EXISTS = 'output_exists'
REASON_MISSING_ENGINE = 'missing_engine'
REASON_FAILURE_THRESHOLD = 'failure_threshold'
REASON_ENGINE_DECLINED = 'engine_declined'
REASON_RESULT_REJECTED = 'result_rejected'
REASON_ENGINE_FAILED = 'engine_failed'

ENGINE_LABELS = {
    'ffsubsync': 'FFsubsync',
    'autosubsync': 'Autosubsync',
    'alass': 'ALASS',
}


class MissingSyncEngineError(RuntimeError):
    def __init__(self, engine, message):
        super().__init__(message)
        self.engine = engine


class SyncResultRejectedError(RuntimeError):
    """An engine produced an output the host refuses to accept."""

    def __init__(self, engine, message):
        super().__init__(message)
        self.engine = engine


class SyncEngineDeclinedError(RuntimeError):
    """An engine ran to completion and rejected its own result.

    Distinct from a crash: nothing went wrong, the engine simply could not align this
    file and said so. autosubsync does this routinely, its ``synchronize`` returns
    False when its own quality check fails. Reporting that as a failure with a
    traceback tells the user to go fix an installation that is not broken.
    """

    def __init__(self, engine, message):
        super().__init__(message)
        self.engine = engine


def acceptance_limit_seconds(max_offset_seconds):
    """Normalize the configured maximum offset into a positive float, or None.

    None means "no limit": an unset, unparsable or non-positive setting must not
    silently reject every alignment.
    """
    if max_offset_seconds is None:
        return None
    try:
        limit = abs(float(max_offset_seconds))
    except (TypeError, ValueError):
        return None
    return limit or None


def validate_engine_result(engine, raw_result, max_offset_seconds):
    """Accept or reject what an engine returned. Raises SyncResultRejectedError.

    The engines search without an offset window, so the configured maximum is applied
    here, on the host, as an acceptance threshold. Two things get rejected:

    1. A run the engine itself reports as unsuccessful. ffsubsync writes an output file
       even then (an anti-correlated alignment, or the untouched subtitles when it
       declines to shift them), so "a non-empty file exists" is not evidence of a sync.
    2. An alignment whose absolute offset is larger than the configured maximum.

    Only ffsubsync reports an offset, so only ffsubsync is held to the maximum. That is
    a knowing inconsistency: autosubsync and alass report success or failure and nothing
    measurable, and there is no way to bound their shift short of re-deriving it from
    their output. They are still held to condition 1.
    """
    if not isinstance(raw_result, dict):
        return

    for key in ('sync_was_successful', 'success'):
        if key in raw_result and not raw_result[key]:
            # The engine's own verdict on its own output, which is a decline
            # rather than a rejection: ffsubsync saying no here is the same act
            # as autosubsync's quality check saying no. Only the offset limit
            # below is Bazarr overruling an engine that claimed success.
            raise SyncEngineDeclinedError(
                engine, f'{engine} reported the synchronization as unsuccessful.')

    limit = acceptance_limit_seconds(max_offset_seconds)
    offset_seconds = raw_result.get('offset_seconds')
    if limit is None or offset_seconds is None:
        return
    try:
        offset_seconds = float(offset_seconds)
    except (TypeError, ValueError):
        return
    if abs(offset_seconds) > limit:
        raise SyncResultRejectedError(
            engine,
            f'{engine} aligned the subtitles by {abs(offset_seconds):.3f} seconds, '
            f'more than the {limit:g} second maximum offset.')


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
            'engine': self.engine,
            'status': self.status,
            'output_path': self.output_path,
            'generated_path': self.generated_path,
            'reason': self.reason,
            'message': self.message,
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
            'source_path': self.source_path,
            'output_mode': self.output_mode,
            'success': self.success,
            'results': [item.as_dict() for item in self.results],
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
            subtitle_matches = subtitle_path is None or key[0] == self._key(subtitle_path, key[1])[0]
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
            .where(TableSubsyncEngineFailure.subtitle_path == self._subtitle_key(subtitle_path))
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
        return bool(failure.is_skipped) or failure.consecutive_failures >= self.failure_threshold

    def record_success(self, subtitle_path, engine):
        from app.database import TableSubsyncEngineFailure, database, delete

        database.execute(
            delete(TableSubsyncEngineFailure)
            .where(TableSubsyncEngineFailure.subtitle_path == self._subtitle_key(subtitle_path))
            .where(TableSubsyncEngineFailure.engine == engine)
        )

    def reset(self, subtitle_path=None, engine=None):
        from app.database import TableSubsyncEngineFailure, database, delete

        stmt = delete(TableSubsyncEngineFailure)
        if subtitle_path is not None:
            stmt = stmt.where(TableSubsyncEngineFailure.subtitle_path == self._subtitle_key(subtitle_path))
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
            insert(TableSubsyncEngineFailure)
            .values(
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
        enabled_engines = [item.strip() for item in enabled_engines.split(',') if item.strip()]
    enabled = set(enabled_engines or [])
    return [engine for engine in SYNC_ENGINES if engine in enabled]


def engine_output_path(srt_path, engine):
    path = Path(srt_path)
    if engine not in SYNC_ENGINES:
        raise ValueError(f'Unsupported sync engine: {engine}')
    suffix = path.suffix or '.srt'
    return path.with_name(f'{path.stem}.{engine}{suffix}')


def temporary_engine_output_path(srt_path, engine):
    path = Path(srt_path)
    suffix = path.suffix or '.srt'
    fd, temp_path = tempfile.mkstemp(prefix=f'.bazarr-sync-{engine}-', suffix=suffix, dir=str(path.parent))
    os.close(fd)
    os.unlink(temp_path)
    return Path(temp_path)


def sync_engine_from_output_path(path):
    filename = os.path.basename(str(path)).lower()
    stem, extension = os.path.splitext(filename)
    if not extension:
        return None

    parts = stem.split('.')
    if len(parts) < 2:
        return None

    engine = parts[-1]
    return engine if engine in SYNC_ENGINES else None


def is_sync_engine_output(path):
    return sync_engine_from_output_path(path) is not None


def is_sync_engine_language_key(language):
    if not isinstance(language, str):
        return False

    modifiers = language.split(':')[1:]
    return any(modifier.lower() in SYNC_ENGINE_LANGUAGE_MODIFIERS for modifier in modifiers)


class SubsyncEngineRunner:
    def __init__(self, failure_store=None, failure_threshold=FAILURE_THRESHOLD):
        self.failure_store = failure_store or DatabaseSubsyncFailureStore(failure_threshold=failure_threshold)
        self.failure_threshold = failure_threshold

    def _existing_keep_all_output_is_current(self, srt_path, output_path, engine):
        if self.failure_store.failure_count(srt_path, engine) > 0:
            return False

        try:
            source_stat = Path(srt_path).stat()
            output_stat = output_path.stat()
        except OSError:
            return False

        return output_stat.st_size > 0 and output_stat.st_mtime_ns >= source_stat.st_mtime_ns

    def run(self, srt_path, output_mode, enabled_engines, execute_engine, force_sync=False):
        output_mode = normalize_output_mode(output_mode)
        result = SyncRunResult(source_path=srt_path, output_mode=output_mode)

        if is_sync_engine_output(srt_path):
            result.results.append(SyncEngineResult(
                engine='all',
                status=RESULT_SKIPPED,
                reason=REASON_GENERATED_SOURCE,
                message='Generated sync output is not used as a source subtitle.',
            ))
            return result

        for engine in normalize_enabled_engines(enabled_engines):
            final_engine_output_path = engine_output_path(srt_path, engine)
            output_path = (
                final_engine_output_path if output_mode == OUTPUT_MODE_KEEP_ALL
                else temporary_engine_output_path(srt_path, engine)
            )

            if self.failure_store.should_skip(srt_path, engine) and not force_sync:
                result.results.append(SyncEngineResult(
                    engine=engine,
                    status=RESULT_SKIPPED,
                    output_path=str(final_engine_output_path),
                    reason=REASON_FAILURE_THRESHOLD,
                    message=f'{engine} skipped after {self.failure_threshold} consecutive failures.',
                ))
                continue

            if output_mode == OUTPUT_MODE_KEEP_ALL and final_engine_output_path.is_file() and not force_sync:
                if self._existing_keep_all_output_is_current(srt_path, final_engine_output_path, engine):
                    result.results.append(SyncEngineResult(
                        engine=engine,
                        status=RESULT_SKIPPED,
                        output_path=str(final_engine_output_path),
                        reason=REASON_OUTPUT_EXISTS,
                        message='Generated sync output already exists.',
                    ))
                    continue

            try:
                if output_path.is_file():
                    output_path.unlink()

                raw_result = execute_engine(engine, output_path)
                if not output_path.is_file():
                    raise RuntimeError(f'{engine} did not create a synced subtitle file.')
                if output_path.stat().st_size == 0:
                    raise RuntimeError(f'{engine} created an empty synced subtitle file.')

                generated_path = str(output_path)
                final_output_path = output_path
                if output_mode == OUTPUT_MODE_OVERWRITE:
                    os.replace(str(output_path), srt_path)
                    final_output_path = Path(srt_path)
                    generated_path = None

                self.failure_store.record_success(srt_path, engine)
                result.results.append(SyncEngineResult(
                    engine=engine,
                    status=RESULT_SUCCESS,
                    output_path=str(final_output_path),
                    generated_path=generated_path,
                    raw_result=raw_result,
                ))

                if output_mode == OUTPUT_MODE_OVERWRITE:
                    break

            except MissingSyncEngineError as exc:
                logging.warning('BAZARR %s sync engine skipped: %s', engine, exc)
                result.results.append(SyncEngineResult(
                    engine=engine,
                    status=RESULT_SKIPPED,
                    output_path=str(final_engine_output_path),
                    reason=REASON_MISSING_ENGINE,
                    message=str(exc),
                ))
            except SyncEngineDeclinedError as exc:
                # The engine ran and rejected its own result. Nothing is broken, so
                # this is logged as a plain warning: a traceback here reads like a
                # crash and sends users hunting for a missing dependency.
                logging.warning('BAZARR %s declined its own sync result for %s: %s', engine, srt_path, exc)
                self._discard_engine_output(srt_path, engine, output_path, exc)
                result.results.append(SyncEngineResult(
                    engine=engine,
                    status=RESULT_FAILED,
                    output_path=str(final_engine_output_path),
                    reason=REASON_ENGINE_DECLINED,
                    message=str(exc),
                ))

            except SyncResultRejectedError as exc:
                # The engine reported success but Bazarr refused the result, so the
                # engine is not at fault either. Same reasoning as above.
                logging.warning('BAZARR rejected the %s sync result for %s: %s', engine, srt_path, exc)
                self._discard_engine_output(srt_path, engine, output_path, exc)
                result.results.append(SyncEngineResult(
                    engine=engine,
                    status=RESULT_FAILED,
                    output_path=str(final_engine_output_path),
                    reason=REASON_RESULT_REJECTED,
                    message=str(exc),
                ))

            except Exception as exc:
                logging.exception('BAZARR %s sync engine failed for %s', engine, srt_path)
                self._discard_engine_output(srt_path, engine, output_path, exc)
                result.results.append(SyncEngineResult(
                    engine=engine,
                    status=RESULT_FAILED,
                    output_path=str(final_engine_output_path),
                    reason=REASON_ENGINE_FAILED,
                    message=str(exc),
                ))

        return result

    def _discard_engine_output(self, srt_path, engine, output_path, exc):
        """Drop whatever the engine left behind and count the attempt against it.

        Shared by every outcome that produced no usable output. A decline and a
        rejection differ from a crash only in how they are reported: they still cost
        the engine a strike towards the failure threshold, and they still must not
        leave a half-written file next to the subtitle.
        """
        if output_path.is_file():
            output_path.unlink()
        self.failure_store.record_failure(srt_path, engine, str(exc)[:500])

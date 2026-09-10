# coding=utf-8

import logging
import os
import stat
import shutil
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass, field
from contextlib import contextmanager, ExitStack, nullcontext
from pathlib import Path
from threading import Lock, RLock
from weakref import WeakValueDictionary


_subtitle_write_locks = WeakValueDictionary()
_subtitle_write_locks_guard = Lock()


class _SubtitleWriteState:
    def __init__(self):
        self.lock = RLock()
        self.revisions = {}

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()

    def changed(self, path):
        key = os.path.normcase(os.path.realpath(path))
        self.revisions[key] = self.revisions.get(key, 0) + 1

    def revision(self, path):
        return self.revisions.setdefault(os.path.normcase(os.path.realpath(path)), 0)


def subtitle_write_lock(video_path, subtitle_directory):
    """Coordinate one media destination, including its subtitle language variants.

    The saver can change HI tags and format extensions. The target directory and
    video stem identify their shared destination without predicting those names.
    Weak references release idle locks once all participating operations finish.
    """
    key = (os.path.normcase(os.path.realpath(subtitle_directory)),
           os.path.normcase(os.path.splitext(os.path.basename(video_path))[0]))
    with _subtitle_write_locks_guard:
        lock = _subtitle_write_locks.get(key)
        if lock is None:
            lock = _SubtitleWriteState()
            _subtitle_write_locks[key] = lock
        return lock


def subtitle_source_version(path):
    """Identify the saved file a queued upload sync is allowed to replace."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


class SubtitlePublication:
    """Hold a media coordinator while a queued sync watches exact destinations."""

    def __init__(self, video_path, source_path, source_version):
        self.state = subtitle_write_lock(video_path, os.path.dirname(source_path))
        self.source_path = source_path
        self.source_version = source_version
        with self.state:
            self.source_revision = self.state.revision(source_path)
            self.owns_destinations = sync_output_owner_is_unique(video_path, source_path)
            self.destinations = {
                str(path): (self.state.revision(path), subtitle_source_version(path))
                for path in (engine_output_path(source_path, engine) for engine in SYNC_ENGINES)
            }

    def source_unchanged(self):
        return (self.state is not None and self.source_version is not None
                and self.state.revision(self.source_path) == self.source_revision
                and subtitle_source_version(self.source_path) == self.source_version)

    def destination_owned(self, path):
        return self.owns_destinations and not os.path.islink(path)

    def destination_unchanged(self, path):
        expected = self.destinations.get(str(path))
        return (self.state is not None and self.destination_owned(path) and expected is not None
                and expected == (self.state.revision(path), subtitle_source_version(path)))

    def release(self):
        self.state = None


def source_is_unchanged(path, version):
    if isinstance(version, SubtitlePublication):
        return version.source_unchanged()
    return subtitle_source_version(path) == version


def release_subtitle_publication(version):
    if isinstance(version, SubtitlePublication):
        version.release()


def release_unqueued_subtitle_publication(version, queue):
    if not isinstance(version, SubtitlePublication):
        return
    # Duplicate submission can pass the very same snapshot as the queued job.
    # Its coordinator belongs to that job until the running call finishes.
    with queue._queue_lock:
        if any(job.kwargs.get('source_version') is version
               for job in (*queue.jobs_pending_queue, *queue.jobs_running_queue)):
            return
    release_subtitle_publication(version)


@contextmanager
def subtitle_write_locks(video_path, *paths):
    """Acquire participating subtitle directories in stable order."""
    directories = sorted({os.path.normcase(os.path.realpath(os.path.dirname(path))) for path in (video_path, *paths)})
    with ExitStack() as stack:
        states = {directory: stack.enter_context(subtitle_write_lock(video_path, directory))
                  for directory in directories}
        yield states


@contextmanager
def subtitle_mutation(video_path, *paths, invalidate_outputs=True):
    """Coordinate exact local writes and invalidate pending publications.

    Network and engine work stays outside. Indexers share this coordinator for
    their external-file scan and database update.
    """
    with subtitle_write_locks(video_path, *paths) as states:
        versions = {path: subtitle_source_version(path) for path in paths}
        try:
            yield
        finally:
            for path, version in versions.items():
                if subtitle_source_version(path) != version:
                    directory = os.path.normcase(os.path.realpath(os.path.dirname(path)))
                    states[directory].changed(path)
                    if invalidate_outputs:
                        quarantine_sync_outputs_after_mutation(video_path, path)


@contextmanager
def staged_subtitle_write(video_path, destination, before_publish=None, allow_empty=False,
                          source_paths=(), after_publish=None, on_publish=None):
    """Compute privately, then publish only while source and destination are current."""
    with subtitle_write_locks(video_path, destination, *source_paths) as states:
        def version(path):
            directory = os.path.normcase(os.path.realpath(os.path.dirname(path)))
            return (states[directory].revision(path), subtitle_source_version(path))

        destination_version = version(destination)
        source_versions = {path: version(path) for path in source_paths}
    temporary = os.path.join(os.path.dirname(destination), f'.bazarr-write-{uuid.uuid4().hex}{Path(destination).suffix}')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    os.close(fd)
    try:
        yield temporary
        if allow_empty and os.path.isfile(temporary) and os.path.getsize(temporary) == 0:
            return
        if not os.path.isfile(temporary) or os.path.getsize(temporary) == 0:
            raise OSError('Subtitle writer did not produce a nonempty file')
        with subtitle_write_locks(video_path, destination, *source_paths):
            with subtitle_mutation(video_path, destination):
                if before_publish:
                    before_publish()
                if destination_version != version(destination):
                    raise SubtitleDestinationChanged('Subtitle changed during processing')
                if any(expected[1] is None or expected != version(path) for path, expected in source_versions.items()):
                    raise SubtitleSourceChanged('Source subtitle changed during processing')
                if os.path.isfile(destination):
                    shutil.copymode(destination, temporary)
                os.replace(temporary, destination)
                _report_subtitle_publication(on_publish, destination)
                if after_publish:
                    after_publish()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _report_subtitle_publication(callback, path):
    if callback:
        try:
            callback(str(path))
        except Exception:
            logging.warning('BAZARR subtitle publication notification failed')


def write_subtitle_file(video_path, destination, content, written_paths=None, on_publish=None):
    """Write one saver output atomically and record that exact successful path."""
    with staged_subtitle_write(video_path, destination,
                              on_publish=on_publish,
                              after_publish=(lambda: written_paths.append(destination))
                              if written_paths is not None else None) as temporary:
        with open(temporary, 'wb') as handle:
            handle.write(content)


def _normalized_media_stem(path):
    return unicodedata.normalize('NFC', os.path.splitext(os.path.basename(path))[0].lower())


class SyncOutputOwnerIndex:
    """Lazily map library owners once for one sequential subtitle scan.

    Only indexers share this snapshot. File mutations and queued publications
    use fresh ownership checks so a completed scan cannot authorize a later write.
    """

    def __init__(self):
        self._loaded = False
        self._owners = {}

    @staticmethod
    def _load_owners():
        from app.database import database, select, TableEpisodes, TableMovies
        from app.config import settings
        from utilities.path_mappings import path_mappings

        subfolder = settings.general.subfolder
        custom_folder = settings.general.subfolder_custom
        absolute_folder = (os.path.normcase(os.path.realpath(custom_folder))
                           if subfolder == 'absolute' else None)
        owners = {}
        for table, media_type in ((TableEpisodes, 'episode'), (TableMovies, 'movie')):
            for row in database.execute(select(table.path, table.arr_instance_id)).all():
                if not row.path:
                    continue
                mapped = os.path.normcase(os.path.realpath(path_mappings.path_replace_instance(
                    row.path, row.arr_instance_id, media_type)))
                folders = {os.path.dirname(mapped)}
                if subfolder == 'absolute':
                    folders.add(absolute_folder)
                elif subfolder == 'relative':
                    folders.add(os.path.normcase(os.path.realpath(os.path.join(
                        os.path.dirname(mapped), custom_folder))))
                stem = _normalized_media_stem(mapped)
                for folder in folders:
                    owners.setdefault((folder, stem), set()).add(mapped)
        return owners

    def is_unique(self, video, directory, conflicting_stems):
        if not self._loaded:
            # A failed build leaves an empty index, never a partial ownership
            # claim or repeated full-library retries during the same scan.
            self._loaded = True
            self._owners = self._load_owners()
        owned = False
        expected_owner = {video}
        for stem in conflicting_stems:
            owners = self._owners.get((directory, stem))
            if owners:
                if owners != expected_owner:
                    return False
                owned = True
        return owned


def sync_output_owner_is_unique(video_path, source_path, ownership_index=None):
    """Prove ownership before moving or replacing an exact generated variant."""
    from subliminal_patch.core import VIDEO_EXTENSIONS

    video = os.path.normcase(os.path.realpath(video_path))
    directory = os.path.normcase(os.path.realpath(os.path.dirname(source_path)))
    media_directory = os.path.dirname(video)

    stem = _normalized_media_stem
    if stem(source_path) != stem(video) and not stem(source_path).startswith(stem(video) + '.'):
        return False
    conflicting_stems = {stem(video), stem(source_path)}
    try:
        with os.scandir(media_directory) as entries:
            local_videos = {os.path.normcase(os.path.realpath(entry.path)) for entry in entries
                            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTENSIONS)}
        if any(path != video and stem(path) in conflicting_stems for path in local_videos):
            return False
        if directory == media_directory:
            return video in local_videos

        # Separate media directories can share a custom subtitle folder. Keep
        # all mapped owners, amortizing that lookup only within an explicit scan.
        if ownership_index is None:
            ownership_index = SyncOutputOwnerIndex()
        return ownership_index.is_unique(video, directory, conflicting_stems)
    except (OSError, ValueError):
        return False
    except Exception:
        logging.exception('BAZARR unable to verify generated subtitle ownership')
        return False


def quarantine_sync_outputs(video_path, source_path):
    """Preserve proven generated siblings under names no subtitle scan accepts."""
    if is_sync_engine_output(source_path) or not sync_output_owner_is_unique(video_path, source_path):
        return
    from subtitles.indexer.utils import add_sync_engine_outputs

    directory = os.path.dirname(source_path)
    with subtitle_write_lock(video_path, directory) as state:
        owned = add_sync_engine_outputs(directory, {}, video_path=video_path)
        for engine in SYNC_ENGINES:
            output = engine_output_path(source_path, engine)
            if output.name not in owned:
                continue
            try:
                if not stat.S_ISREG(output.lstat().st_mode):
                    continue
            except FileNotFoundError:
                continue
            fd, quarantine = tempfile.mkstemp(prefix=f'.bazarr-sync-obsolete-{output.name[:40]}-', suffix='.bak', dir=directory)
            os.close(fd)
            try:
                os.replace(output, quarantine)
            except BaseException:
                os.unlink(quarantine)
                raise
            state.changed(output)


def quarantine_sync_outputs_after_mutation(video_path, source_path):
    """Report cleanup failure separately from an already completed file change."""
    try:
        quarantine_sync_outputs(video_path, source_path)
    except OSError as exc:
        logging.error(
            'BAZARR subtitle change completed for %s, but obsolete generated subtitle cleanup failed (%s). '
            'Previous generated files may remain available.', source_path, type(exc).__name__)


class SubtitleSourceChanged(Exception):
    """The upload was replaced or deleted while its sync was queued or running."""


class SubtitleDestinationChanged(Exception):
    """A user changed or deleted this engine's destination after enqueueing."""


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
REASON_SOURCE_CHANGED = 'source_changed'
REASON_DESTINATION_CHANGED = 'destination_changed'
REASON_DESTINATION_AMBIGUOUS = 'destination_ambiguous'

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
        limit = float(max_offset_seconds)
    except (TypeError, ValueError):
        return None
    # Not abs(): a negative value is one of the non-positive ones this contract
    # calls "no limit", and the sync API takes an arbitrary integer, not just
    # the settings page's choices.
    return limit if limit > 0 else None


def validate_engine_result(engine, raw_result, max_offset_seconds):
    """Accept or reject what an engine returned.

    Raises SyncEngineDeclinedError when the engine rejected its own result, and
    SyncResultRejectedError when Bazarr overrules an engine that claimed success.

    The engines search without an offset window, so the configured maximum is applied
    here, on the host, as an acceptance threshold. Two things get rejected:

    1. A run the engine itself reports as unsuccessful. ffsubsync writes an output file
       even then (an anti-correlated alignment, or the untouched subtitles when it
       declines to shift them), so "a non-empty file exists" is not evidence of a sync.
    2. An alignment whose absolute offset is larger than the configured maximum.

    ffsubsync and autosubsync report an offset (autosubsync returns its measured
    shift under the same key), so both are held to the maximum. alass is the
    knowing inconsistency: it reports success or failure and nothing measurable,
    and there is no way to bound its shift short of re-deriving it from its
    output. It is still held to condition 1.
    """
    if not isinstance(raw_result, dict):
        return

    for key in ('sync_was_successful', 'success'):
        if key in raw_result and not raw_result[key]:
            # The engine's verdict on its own output, which is a decline rather
            # than a rejection: routine, and it must not count towards the
            # consecutive-failure quarantine that would skip the engine next time.
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

    def run(self, srt_path, output_mode, enabled_engines, execute_engine, force_sync=False,
            source_version=None, before_publish=None, publication_lock=None, after_publish=None, on_publish=None):
        output_mode = normalize_output_mode(output_mode)
        result = self.result = SyncRunResult(source_path=srt_path, output_mode=output_mode)

        if is_sync_engine_output(srt_path):
            result.results.append(SyncEngineResult(
                engine='all',
                status=RESULT_SKIPPED,
                reason=REASON_GENERATED_SOURCE,
                message='Generated sync output is not used as a source subtitle.',
            ))
            return result

        for engine in normalize_enabled_engines(enabled_engines):
            if source_version is not None and not source_is_unchanged(srt_path, source_version):
                result.results.append(SyncEngineResult(
                    engine=engine, status=RESULT_SKIPPED, reason=REASON_SOURCE_CHANGED,
                    message='The uploaded subtitle was replaced or deleted.',
                ))
                break
            final_engine_output_path = engine_output_path(srt_path, engine)
            output_path = temporary_engine_output_path(srt_path, engine)

            if (output_mode == OUTPUT_MODE_KEEP_ALL and isinstance(source_version, SubtitlePublication)
                    and not source_version.destination_unchanged(final_engine_output_path)):
                result.results.append(SyncEngineResult(
                    engine=engine, status=RESULT_SKIPPED,
                    reason=(REASON_DESTINATION_CHANGED if source_version.destination_owned(final_engine_output_path)
                            else REASON_DESTINATION_AMBIGUOUS),
                    message='The synchronized subtitle destination changed or its owner is ambiguous.',
                ))
                continue

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
                with publication_lock or nullcontext() as state:
                    if before_publish:
                        before_publish()
                    if source_version is not None and not source_is_unchanged(srt_path, source_version):
                        raise SubtitleSourceChanged()
                    if (output_mode == OUTPUT_MODE_KEEP_ALL and isinstance(source_version, SubtitlePublication)
                            and not source_version.destination_unchanged(final_engine_output_path)):
                        raise SubtitleDestinationChanged()
                    if output_mode == OUTPUT_MODE_OVERWRITE:
                        os.replace(str(output_path), srt_path)
                        final_output_path = Path(srt_path)
                        generated_path = None
                    else:
                        os.replace(str(output_path), str(final_engine_output_path))
                        final_output_path = final_engine_output_path
                        generated_path = str(final_engine_output_path)
                    _report_subtitle_publication(on_publish, final_output_path)
                    if hasattr(state, 'changed'):
                        state.changed(final_output_path)
                    if after_publish:
                        after_publish()

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

            except SubtitleSourceChanged:
                if output_path.is_file():
                    output_path.unlink()
                result.results.append(SyncEngineResult(
                    engine=engine, status=RESULT_SKIPPED, reason=REASON_SOURCE_CHANGED,
                    message='The uploaded subtitle was replaced or deleted.',
                ))
                break
            except SubtitleDestinationChanged:
                if output_path.is_file():
                    output_path.unlink()
                result.results.append(SyncEngineResult(
                    engine=engine, status=RESULT_SKIPPED,
                    reason=(REASON_DESTINATION_CHANGED if source_version.destination_owned(final_engine_output_path)
                            else REASON_DESTINATION_AMBIGUOUS),
                    message='The synchronized subtitle destination changed or its owner is ambiguous.',
                ))
                continue
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
                # No strike: the engine works, this file is simply one it could
                # not align, and three of those would otherwise quarantine it for
                # this subtitle even after the reference or the settings change.
                self._discard_engine_output(srt_path, engine, output_path, exc, record=False)
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
                if before_publish:
                    from app.jobs_queue import JobCancelled
                    if isinstance(exc, JobCancelled):
                        self._discard_engine_output(srt_path, engine, output_path, exc, record=False)
                        raise
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

    def _discard_engine_output(self, srt_path, engine, output_path, exc, record=True):
        """Drop whatever the engine left behind, and usually count it against it.

        Shared by every outcome that produced no usable output: none of them may
        leave a half-written file next to the subtitle. A crash and a rejection
        cost the engine a strike towards the failure threshold. A decline does
        not: the engine ran correctly and said this file is one it cannot align,
        which is routine, and quarantining it for that would skip it later even
        after the reference or the sync settings change.
        """
        if output_path.is_file():
            output_path.unlink()
        if record:
            self.failure_store.record_failure(srt_path, engine, str(exc)[:500])

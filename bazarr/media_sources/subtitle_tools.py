# coding=utf-8
"""Resolve indexed source sidecars and hold their media identity through a tool write."""
import ast
import os
from contextlib import contextmanager
from dataclasses import dataclass

from app.config import empty_values
from subtitles.tools.subsync_engines import (SubtitleSourceChanged, read_regular_subtitle, regular_subtitle_version,
                                             is_sync_engine_output, is_sync_stream_reference, sync_output_owner_is_unique)
from utilities.security_guards import SUBTITLE_FILE_EXTENSIONS

from . import repository
from .analysis import _content_stamp, local_stat
from .context import SourceItemContext, SourceItemGone, resolve_item
from .indexer import SourcePublicationOwnerIndex, source_subtitle_directories
from .translation import live_source

SOURCE_SUBTITLE_EDITED_ACTION = 10


class SourceToolError(ValueError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def source_video_stamp(path):
    stat = local_stat(path)
    return _content_stamp(stat) if stat is not None else None


def _video_in_roots(source, target=None):
    from compat.local_subs import _path_under_any_root
    from .kinds.folder import is_video_file

    try:
        path = os.path.realpath(source.path, strict=True)
        roots = [os.path.realpath(root.local_path) for root in repository.get_roots(source.library_id, target)]
    except OSError:
        return False
    return (is_video_file(path) and not path.lower().endswith('.strm')
            and _path_under_any_root(path, roots))


def tool_item(item_id, arr_instance_id=None):
    if arr_instance_id is not None:
        raise SourceToolError('Source items do not have an Arr instance', 400)
    try:
        source = resolve_item(item_id)
    except SourceItemGone as error:
        raise SourceToolError('Source item not found', 404) from error
    if live_source(source) is None or not _video_in_roots(source):
        raise SourceToolError('The source item is disabled or its video is unavailable', 404)
    item = repository.get_item(item_id)
    if item is None or not source.same_item(item) or item.path != source.path:
        raise SourceToolError('Source item not found', 404)
    return source, item


def indexed_subtitles(item):
    try:
        entries = ast.literal_eval(item.subtitles or '[]')
    except (ValueError, SyntaxError, TypeError):
        return []
    if not isinstance(entries, list):
        return []
    return [(entry[0], entry[1]) for entry in entries if isinstance(entry, (tuple, list)) and len(entry) >= 2
            and isinstance(entry[0], str) and isinstance(entry[1], str) and entry[1]]


def _in_area(source, path):
    if not os.path.isabs(path) or os.path.splitext(path)[1].lower() not in SUBTITLE_FILE_EXTENSIONS:
        return False
    directories = {os.path.normcase(os.path.realpath(directory))
                   for directory in source_subtitle_directories(source.path)}
    return os.path.normcase(os.path.realpath(os.path.dirname(path))) in directories


def _path_key(path):
    return os.path.normcase(os.path.normpath(path))


def _owned(source, path, owners):
    return _in_area(source, path) and sync_output_owner_is_unique(source.path, path, ownership_index=owners)


@dataclass(frozen=True)
class IndexedSidecar:
    language: str
    path: str
    version: tuple

    def read(self, max_bytes=None):
        return read_regular_subtitle(self.path, self.version, max_bytes=max_bytes)

    def now_listed(self, source, item):
        return ((self.language, self.path) in indexed_subtitles(item) and _in_area(source, self.path)
                and regular_subtitle_version(self.path) == self.version)


def indexed_sidecar(source, item, *, path=None, language=None):
    found = [(code, listed) for code, listed in indexed_subtitles(item)
             if ((path is None or _path_key(listed) == _path_key(path)) and (language is None or code == language))]
    if not found:
        raise SourceToolError('Subtitle is not listed for this source item', 404 if path is None else 403)
    owners = SourcePublicationOwnerIndex()
    for code, listed in found:
        if not _owned(source, listed, owners):
            continue
        version = regular_subtitle_version(listed)
        if version is not None:
            return IndexedSidecar(code, listed, version)
    raise SourceToolError('The listed subtitle is unavailable or has ambiguous ownership', 409)


@dataclass(frozen=True)
class SourceSubtitleTool:
    source: SourceItemContext
    sidecar: IndexedSidecar
    video_stamp: tuple
    reference: IndexedSidecar | None = None

    @classmethod
    def capture(cls, source, sidecar, reference=None):
        stamp = source_video_stamp(source.path)
        if stamp is None:
            raise SourceToolError('The source video is unavailable', 404)
        return cls(source, sidecar, stamp, reference)

    @classmethod
    def capture_sync(cls, source, item, sidecar, reference):
        """Resolve the reference and capture the identities used by either sync entry point."""
        if is_sync_engine_output(sidecar.path):
            raise SourceToolError('Generated sync output files cannot be synchronized again.', 400)
        external = None
        if reference in empty_values or reference == source.path:
            reference = source.path
        elif not is_sync_stream_reference(reference):
            if not isinstance(reference, str):
                raise SourceToolError('Invalid sync reference', 400)
            external = indexed_sidecar(source, item, path=reference)
            reference = external.path
        return cls.capture(source, sidecar, external), reference

    def check_current(self, current, target=None):
        if (current is None or source_video_stamp(self.source.path) != self.video_stamp
                or not _video_in_roots(current, target)):
            raise SubtitleSourceChanged('The source video changed, moved, or was removed')
        item = repository.get_item(current.item_id, target)
        if (item is None or not self.sidecar.now_listed(current, item)
                or (self.reference is not None and not self.reference.now_listed(current, item))):
            raise SubtitleSourceChanged('The source subtitle or reference changed')

    def validate(self):
        self.check_current(live_source(self.source))
        owners = SourcePublicationOwnerIndex()
        paths = [self.sidecar.path]
        if self.reference is not None:
            paths.append(self.reference.path)
        if not all(_owned(self.source, path, owners) for path in paths):
            raise SubtitleSourceChanged('The source subtitle or reference has ambiguous ownership')

    @contextmanager
    def publication(self, output_path, *, cleanup_outputs=False):
        owners = SourcePublicationOwnerIndex()

        def prepare():
            paths = [self.sidecar.path, output_path]
            if self.reference is not None:
                paths.append(self.reference.path)
            if not all(_owned(self.source, path, owners) for path in paths):
                raise SubtitleSourceChanged('The source subtitle or output has ambiguous ownership')

        with owners.publication(self.source, prepare, destination=output_path,
                                cleanup_outputs=cleanup_outputs) as (current, connection, _):
            self.check_current(current, connection)
            if os.path.lexists(output_path) and regular_subtitle_version(output_path) is None:
                raise SubtitleSourceChanged('The output is no longer a regular subtitle file')
            yield


    def write(self, data, operation):
        """Replace the captured sidecar and return its new file version."""
        from media_servers.events import publication_callback
        from subtitles.new_subtitle import apply_subtitle_chmod, write_bytes_atomically
        from subtitles.tools.subsync_engines import (_report_subtitle_publication, subtitle_mutation,
                                                     subtitle_write_locks)

        video_path, path = self.source.path, self.sidecar.path
        locked_paths = (path,) if self.reference is None else (path, self.reference.path)
        with subtitle_write_locks(video_path, *locked_paths):
            with subtitle_mutation(video_path, path, ownership_index=SourcePublicationOwnerIndex()), self.publication(path):
                write_bytes_atomically(path, data)
                apply_subtitle_chmod(path)
                publish = publication_callback(self.source.publication_type, video_path, operation, source=self.source)
                _report_subtitle_publication(publish, path)
                return regular_subtitle_version(path)


def log_source_edit(source, subtitle_path, language, description, action=SOURCE_SUBTITLE_EDITED_ACTION):
    from subtitles.processing import ProcessSubtitlesResult

    from .db import source_transaction
    from .history import history_log_source

    result = ProcessSubtitlesResult(message=description, reversed_path=source.path,
                                    downloaded_language_code2=language, downloaded_provider=None, score=None,
                                    forced=False, subtitle_id=None, reversed_subtitles_path=subtitle_path,
                                    hearing_impaired=False)
    with source_transaction() as connection:
        history_log_source(action, source, result, target=connection)

# coding=utf-8
"""Manual subtitle actions for an item the user selected."""

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass

from app.database import get_audio_profile_languages
from app.jobs_queue import JobFailed, jobs_queue
from subtitles.tools.subsync_engines import (SubtitleDestinationChanged, regular_subtitle_version,
                                            sync_output_owner_is_unique, write_subtitle_file)
from utilities.helper import get_target_folder

from . import repository
from .analysis import local_content_stamp
from .context import SourceItemContext, resolve_same_item, source_item_title
from .db import source_transaction
from .history import get_blacklist_source, history_log_source
from .indexer import SourcePublicationOwnerIndex


@dataclass(frozen=True)
class SourceCandidate:
    source: SourceItemContext
    subtitle: object
    video_stamp: tuple


def validate_source_candidate(candidate, source, provider, *, target=None):
    if (not isinstance(candidate, SourceCandidate) or not candidate.source.same_item(source)
            or candidate.source.path != source.path or candidate.source.source_id != source.source_id
            or candidate.source.library_id != source.library_id or candidate.source.kind != source.kind
            or candidate.subtitle.provider_name != provider):
        raise JobFailed('This search result does not match the selected item or provider. Search again.')
    if candidate.video_stamp is None or candidate.video_stamp != local_content_stamp(source.path):
        raise JobFailed('The source video changed since the search. Search again.')
    if (provider, str(candidate.subtitle.id)) in get_blacklist_source(source.kind, target):
        raise JobFailed('This release has been excluded. Search again.')
    return candidate.subtitle


def source_search_title(source):
    if source.kind == 'episode' and source.group_title:
        return source.group_title
    return source.title


def current_manual_source(source):
    current = resolve_same_item(source)
    if current is None:
        raise JobFailed('The source item moved or was removed.', reason='item_gone')
    state = repository.item_state(current.item_id)
    if state is None or not state.source_enabled or not state.library_enabled or state.server_missing:
        raise JobFailed('The source item is unavailable.', reason='item_unavailable')
    if not os.path.isfile(current.path):
        raise JobFailed('The source video file is unavailable.', reason='video_unavailable')
    return current


def queue_source_download(source, provider, subtitle, hi, forced, original_format):
    return jobs_queue.feed_jobs_pending_queue(
        job_name=f'Downloading subtitles for {source_item_title(source)}',
        module=__name__, func='download_source_subtitle', is_progress=False,
        kwargs=dict(source=source, provider=provider, subtitle=subtitle, hi=hi, forced=forced,
                    original_format=original_format))


def download_source_subtitle(source, provider, subtitle, hi, forced, original_format, job_id=None):
    from app.config import settings
    from app.notifier import send_notifications_source
    from subtitles.manual import manual_download_subtitle

    source = current_manual_source(source)
    audio = get_audio_profile_languages(source.audio_language)
    result = manual_download_subtitle(
        source.path, audio[0]['name'] if audio else 'None', hi, forced, subtitle, provider, str(source.scene_name),
        source_search_title(source), source.media_type, original_format, source.profile_id, job_id=job_id, source=source)
    if result:
        with source_transaction() as connection:
            history_log_source(2, source, result, target=connection)
        current = resolve_same_item(source)
        if current is not None and not settings.general.dont_notify_manual_actions:
            try:
                send_notifications_source(current, result.message)
            except Exception:
                logging.exception('BAZARR could not send a notification for source item %s', current.item_id)


@contextmanager
def manual_source_publication(source, destination, ownership_index, *, release=None, cleanup_outputs=False):
    def prepare():
        video_directory = os.path.dirname(source.path)
        custom_directory = get_target_folder(source.path, create=False)
        allowed_directories = {os.path.normcase(os.path.realpath(directory))
                               for directory in (video_directory, custom_directory) if directory}
        if os.path.normcase(os.path.realpath(os.path.dirname(destination))) not in allowed_directories:
            raise SubtitleDestinationChanged('The subtitle is outside the media and configured subtitle folders')
        if not sync_output_owner_is_unique(source.path, destination, ownership_index=ownership_index):
            raise SubtitleDestinationChanged('The subtitle destination does not have one media owner')

    with ownership_index.publication(source, prepare, destination=destination, release=release,
                                     cleanup_outputs=cleanup_outputs) as (
            current, connection, _):
        state = repository.item_state(current.item_id, connection) if current is not None else None
        if state is None or state.server_missing:
            raise SubtitleDestinationChanged('The source item is no longer available')
        stamp = local_content_stamp(current.path)
        if stamp is None or any(recorded is not None and recorded != actual
                                for recorded, actual in zip((state.file_dev, state.file_ino), stamp[:2])):
            raise SubtitleDestinationChanged('The scanned video no longer matches the file at this path')
        if os.path.lexists(destination) and regular_subtitle_version(destination) is None:
            raise SubtitleDestinationChanged('The subtitle is no longer a regular file')
        yield current, connection


def write_source_subtitle(source, destination, content, *, written_paths, on_publish, candidate=None, mode=None):
    ownership_index = SourcePublicationOwnerIndex()
    release = None if candidate is None else (source.kind, candidate.subtitle.provider_name, candidate.subtitle.id)

    @contextmanager
    def publication():
        with manual_source_publication(source, destination, ownership_index, release=release,
                                       cleanup_outputs=True) as (current, connection):
            if candidate is not None:
                # Read after any exclusion of this release, including one from a different item.
                validate_source_candidate(candidate, current, candidate.subtitle.provider_name, target=connection)
            yield

    return write_subtitle_file(source.path, destination, content, written_paths=written_paths, on_publish=on_publish,
                               publication_guard=publication, invalidate_outputs=False,
                               mode=mode, capture_version=candidate is not None)

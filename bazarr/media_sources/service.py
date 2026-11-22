# coding=utf-8
"""What the API calls: create a source with a library, queue a scan, and search one item.

A search runs through the shared pipeline with the item's ``SourceItemContext``. Its history is written here,
by the caller that did the work, never inside ``process_subtitle``.
"""
import ast
import logging
import os
import threading

from sqlalchemy.exc import IntegrityError

from app.database import get_audio_profile_languages, get_profiles_list
from app.get_providers import get_providers
from app.jobs_queue import JobFailed, jobs_queue

from . import repository
from .context import SourceItemGone, resolve_item
from .db import source_transaction
from .history import history_log_source
from .indexer import index_item
from .kinds import UnknownSourceKind, get_kind


_creation_lock = threading.Lock()


class SourceError(ValueError):
    """A refused request. ``status`` is the HTTP code it maps to."""

    def __init__(self, message, status=400, code='invalid'):
        super().__init__(message)
        self.status = status
        self.code = code


class _LibraryDraft:
    """What a kind validates before the library exists: the same two fields the stored row has."""

    def __init__(self, locator, content_kind):
        self.locator = locator
        self.content_kind = content_kind


def create_source(payload, session=None):
    """Create a source and its first library in one transaction. Returns the new source id."""
    name = payload.get('name')
    kind_name = payload.get('kind') or 'folder'
    library = payload.get('library')
    if not isinstance(name, str) or not name.strip():
        raise SourceError('A name is required')
    name = name.strip()
    if not isinstance(library, dict):
        raise SourceError('A library is required')
    try:
        kind = get_kind(kind_name)
    except UnknownSourceKind as error:
        raise SourceError(str(error)) from error
    content_kind = library.get('content_kind')
    if content_kind not in repository.CONTENT_KINDS:
        raise SourceError(f"content_kind must be one of {', '.join(repository.CONTENT_KINDS)}")
    path = library.get('path')
    if isinstance(path, str) and path.strip() and '\x00' not in path:
        # Stored the way it will be walked: a trailing slash or a ".." must not make one folder two locators.
        path = repository.normalize_path(path)
    draft = _LibraryDraft(path, content_kind)
    problems = kind.validate_library(None, draft)
    if problems:
        raise SourceError(' '.join(problem.message for problem in problems))
    profile_id = library.get('profile_id')
    if profile_id is not None and (type(profile_id) is not int or profile_id < 1
                                   or get_profiles_list(profile_id=profile_id) is None):
        raise SourceError('That language profile does not exist')
    enabled = payload.get('enabled', True)
    if type(enabled) is not bool:
        raise SourceError('enabled must be true or false')
    frequency = payload.get('scan_frequency_hours', 12)
    if type(frequency) is not int or frequency < 0:
        raise SourceError('scan_frequency_hours must be a whole number, 0 or more')
    # The overlap has no constraint behind it, so the check and the insert are one step: two requests for
    # overlapping folders cannot both pass.
    with _creation_lock:
        clash = repository.overlapping_root(path, session)
        if clash is not None:
            raise SourceError(f"This folder overlaps {clash[1]}, a library of the media source {clash[0]}. A "
                              f"folder can belong to one library only: use a folder that neither contains nor "
                              f"sits inside another library's.", 409, 'overlap')
        if repository.name_taken(name, session):
            raise SourceError('A media source with that name already exists', 409, 'name_taken')
        try:
            with source_transaction(session) as connection:
                source_id = repository.create_source(
                    kind=kind_name, name=name, enabled=enabled, scan_frequency_hours=frequency,
                    target=connection)
                repository.create_library(source_id=source_id, content_kind=content_kind, locator=path,
                                          profile_id=profile_id, target=connection)
        except IntegrityError as error:
            # Another process took the name between the check above and the insert.
            if repository.name_taken(name, session):
                raise SourceError('A media source with that name already exists', 409, 'name_taken') from error
            raise
    return source_id


def describe_source(source_id, session=None):
    source = repository.get_source(source_id, session)
    if source is None:
        return None
    body = {key: (value.isoformat() if hasattr(value, 'isoformat') else value)
            for key, value in source._mapping.items()}
    body['enabled'] = bool(body['enabled'])
    body['libraries'] = [
        {**{key: (value.isoformat() if hasattr(value, 'isoformat') else value)
            for key, value in library._mapping.items()},
         'enabled': bool(library.enabled),
         'roots': [{'id': root.id, 'local_path': root.local_path, 'server_path': root.server_path}
                   for root in repository.get_roots(library.id, session)]}
        for library in repository.list_libraries(source_id, session)]
    return body


def queue_scan(source_id, library_id=None):
    from .scanner import disabled_reason, scan_media_source
    reason = disabled_reason(source_id, library_id)
    if reason:
        raise SourceError(reason, 409, 'disabled')
    return scan_media_source(source_id, library_id)


def item_disabled_reason(item_id, session=None):
    """Why an item must not be searched: its library or its source is disabled. None when it may be."""
    state = repository.item_state(item_id, session)
    if state is None:
        return None
    if not state.source_enabled:
        return f'The media source {state.source_name} is disabled'
    if not state.library_enabled:
        return 'The library of this item is disabled'
    return None


def queue_search(item_id):
    reason = item_disabled_reason(item_id)
    if reason:
        raise SourceError(reason, 409, 'disabled')
    return search_source_item(item_id)


def _missing_languages(item):
    languages = []
    for language in ast.literal_eval(item.missing_subtitles or '[]'):
        if language is not None:
            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ = "True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))
    return languages


def _label(item_id):
    item = repository.get_item(item_id)
    return item.title if item is not None else str(item_id)


def search_source_item(item_id, job_id=None):
    """Search the missing subtitles of one source item. Called without a job id it queues itself."""
    if not job_id:
        return jobs_queue.add_job_from_function(f"Searching subtitles for {_label(item_id)}", is_progress=True)

    try:
        context = resolve_item(item_id)
    except SourceItemGone as error:
        raise JobFailed('The item no longer exists', reason='not_found') from error
    reason = item_disabled_reason(item_id)
    if reason:
        # Disabled after the search was queued: nothing is searched, and nothing is an error.
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message=reason)
        return None
    if not context.profile_id:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max',
                                       progress_message='No language profile: nothing to search for')
        return None
    if not os.path.exists(context.path):
        raise JobFailed(f"The file is missing: {context.path}", reason='missing_file')

    item = repository.get_item(item_id)
    if item is not None and item.missing_subtitles is None:
        # Indexing for this item is incomplete, so it is done again.
        index_item(item_id)
        item = repository.get_item(item_id)
    if item is None:
        # Deleted between resolving it and reading it.
        raise JobFailed('The item no longer exists', reason='not_found')
    languages = _missing_languages(item)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(languages), progress_message=context.title)
    if not languages:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='Nothing is missing')
        return None
    if not get_providers():
        logging.info("BAZARR All providers are throttled")
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='All providers throttled')
        return None

    audio_languages = get_audio_profile_languages(context.audio_language)
    audio_language = audio_languages[0]['name'] if audio_languages else 'None'
    # For an episode the title the providers get is the show's.
    title = context.group_title if context.kind == 'episode' and context.group_title else context.title

    from subtitles.download import generate_subtitles
    downloaded = 0
    try:
        for result in generate_subtitles(context.path, languages, audio_language, str(context.scene_name), title,
                                         context.media_type, context.profile_id, check_if_still_required=True,
                                         job_id=job_id, source=context):
            if not result:
                continue
            index_item(item_id)
            history_log_source(1, context, result)
            downloaded += 1
    except OSError as error:
        raise JobFailed(f'Unable to save the subtitles file: {error}', reason='save_failed') from error
    jobs_queue.update_job_progress(
        job_id=job_id, progress_value='max',
        progress_message=f"{downloaded} subtitle(s) downloaded" if downloaded else 'No subtitles found')
    return None

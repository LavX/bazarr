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
from subtitles.indexer.missing_refresh import queue_missing_subtitles_recalculation

from . import identity, paths, repository
from .analysis import index_matches_file, local_stat, source_search_probes
from .context import SourceItemGone, resolve_item
from .db import source_transaction
from .history import history_log_source
from .indexer import index_item
from .kinds import UnknownSourceKind, get_kind
from .schedule import refresh_tasks


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


def _profile_id(library):
    profile_id = library.get('profile_id')
    if profile_id is not None and (type(profile_id) is not int or profile_id < 1
                                   or get_profiles_list(profile_id=profile_id) is None):
        raise SourceError('That language profile does not exist')
    return profile_id


MAX_FREQUENCY_HOURS = 24 * 365


def _frequency(value):
    if type(value) is not int or value < 0 or value > MAX_FREQUENCY_HOURS:
        raise SourceError(f'scan_frequency_hours must be a whole number from 0 to {MAX_FREQUENCY_HOURS} (a year)')
    return value


def _only(payload, allowed, what):
    """Refuse the keys a request carries that nothing would read: a client that sends one is not told it worked."""
    unknown = set(payload) - set(allowed)
    if unknown:
        raise SourceError(f"Cannot set {', '.join(sorted(unknown))} when {what}")


def _flag(value, name='enabled'):
    if type(value) is not bool:
        raise SourceError(f'{name} must be true or false')
    return value


def _checked_folder(library, kind_name):
    """Validate a library's folder for a new library: the content kind, the path, the overlap. Returns
    ``(path, content_kind)``; the overlap itself is checked again under the lock, with the insert."""
    _only(library, ('path', 'content_kind', 'profile_id'), 'adding a library')
    content_kind = library.get('content_kind')
    if content_kind not in repository.CONTENT_KINDS:
        raise SourceError(f"content_kind must be one of {', '.join(repository.CONTENT_KINDS)}")
    # Stored the way it will be walked: a trailing slash or a ".." must not make one folder two locators.
    path = paths.normalize(library.get('path'))
    draft = _LibraryDraft(path, content_kind)
    problems = get_kind(kind_name).validate_library(None, draft)
    if problems:
        raise SourceError(' '.join(problem.message for problem in problems))
    return path, content_kind


def _overlap_error(path, session):
    clash = repository.overlapping_root(path, session)
    if clash is not None:
        raise SourceError(f"This folder overlaps {clash[1]}, a library of the media source {clash[0]}. A "
                          f"folder can belong to one library only: use a folder that neither contains nor "
                          f"sits inside another library's.", 409, 'overlap')


def create_source(payload, session=None):
    """Create a source and its first library in one transaction. Returns the new source id."""
    _only(payload, ('name', 'kind', 'library', 'enabled', 'scan_frequency_hours'), 'creating a media source')
    name = payload.get('name')
    kind_name = payload.get('kind') or 'folder'
    library = payload.get('library')
    if not isinstance(name, str) or not name.strip():
        raise SourceError('A name is required')
    name = name.strip()
    if not isinstance(library, dict):
        raise SourceError('A library is required')
    try:
        get_kind(kind_name)
    except UnknownSourceKind as error:
        raise SourceError(str(error)) from error
    path, content_kind = _checked_folder(library, kind_name)
    profile_id = _profile_id(library)
    enabled = _flag(payload.get('enabled', True))
    frequency = _frequency(payload.get('scan_frequency_hours', 12))
    # The overlap has no constraint behind it, so the check and the insert are one step: two requests for
    # overlapping folders cannot both pass.
    with _creation_lock:
        _overlap_error(path, session)
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
    refresh_tasks()
    return source_id


def list_sources(session=None):
    return [describe_source(source.id, session) for source in repository.list_sources(session)]


def validate_path(payload, session=None):
    """What the Connections page shows next to a folder before it is added."""
    content_kind = payload.get('content_kind') or 'movie'
    if content_kind not in repository.CONTENT_KINDS:
        raise SourceError(f"content_kind must be one of {', '.join(repository.CONTENT_KINDS)}")
    return paths.validate_path(payload.get('path'), content_kind, target=session).as_dict()


def update_source(source_id, payload, session=None):
    """Edit a source: its name, whether it is enabled and how often it is scanned."""
    source = repository.get_source(source_id, session)
    if source is None:
        raise SourceError('No such media source', 404, 'not_found')
    values = {}
    if 'name' in payload:
        name = payload['name']
        if not isinstance(name, str) or not name.strip():
            raise SourceError('A name is required')
        name = name.strip()
        if name != source.name and repository.name_taken(name, session):
            raise SourceError('A media source with that name already exists', 409, 'name_taken')
        values['name'] = name
    if 'enabled' in payload:
        values['enabled'] = 1 if _flag(payload['enabled']) else 0
    if 'scan_frequency_hours' in payload:
        values['scan_frequency_hours'] = _frequency(payload['scan_frequency_hours'])
    unknown = set(payload) - {'name', 'enabled', 'scan_frequency_hours'}
    if unknown:
        raise SourceError(f"Cannot edit {', '.join(sorted(unknown))}")
    if values:
        try:
            repository.update_source(source_id, session, **values)
        except IntegrityError as error:
            raise SourceError('A media source with that name already exists', 409, 'name_taken') from error
        refresh_tasks()
    return describe_source(source_id, session)


def delete_source(source_id, session=None):
    """Remove a source. Its libraries, roots, groups and items go; its history and blacklist rows stay, with the
    snapshots they were written with, still listed and filterable."""
    if repository.get_source(source_id, session) is None:
        raise SourceError('No such media source', 404, 'not_found')
    repository.delete_source(source_id, session)
    refresh_tasks()


def add_library(source_id, payload, session=None):
    """Add a folder library to a folder source."""
    source = repository.get_source(source_id, session)
    if source is None:
        raise SourceError('No such media source', 404, 'not_found')
    if source.kind != 'folder':
        raise SourceError('Libraries of a media server source come from the server', 400, 'not_folder')
    path, content_kind = _checked_folder(payload, source.kind)
    profile_id = _profile_id(payload)
    with _creation_lock:
        _overlap_error(path, session)
        try:
            # The library and its root are one step: no library is left without its root.
            with source_transaction(session) as connection:
                library_id = repository.create_library(source_id=source_id, content_kind=content_kind,
                                                       locator=path, profile_id=profile_id, target=connection)
        except IntegrityError as error:
            raise SourceError('That folder is already a library of this source', 409, 'overlap') from error
    return describe_library(library_id, session)


def update_library(source_id, library_id, payload, session=None):
    """Edit a library: whether it is enabled, its default language profile and its name. A folder library's path
    is immutable: moving a folder library is removing it and adding a new one."""
    library = repository.get_library(library_id, session)
    if library is None or library.source_id != source_id:
        raise SourceError('No such library', 404, 'not_found')
    if 'path' in payload or 'locator' in payload:
        raise SourceError("A folder library's path cannot be edited: remove the library and add a new one", 400,
                          'immutable')
    unknown = set(payload) - {'enabled', 'profile_id', 'name'}
    if unknown:
        raise SourceError(f"Cannot edit {', '.join(sorted(unknown))}")
    values = {}
    if 'enabled' in payload:
        values['enabled'] = 1 if _flag(payload['enabled']) else 0
    if 'profile_id' in payload:
        values['profile_id'] = _profile_id(payload)
    if 'name' in payload:
        if payload['name'] is not None and not isinstance(payload['name'], str):
            raise SourceError('name must be text')
        values['name'] = payload['name']
    if values:
        if not repository.update_library(library_id, session, **values):
            raise SourceError('No such library', 404, 'not_found')
        if 'profile_id' in values:
            queue_missing_subtitles_recalculation(source_scope={'library_id': library_id})
        if 'enabled' in values:
            refresh_tasks()
    return describe_library(library_id, session)


def update_group_profile(group_id, payload, session=None):
    """Set or clear a source group's profile and refresh its items after the write."""
    if not isinstance(payload, dict) or payload.keys() != {'profile_id'}:
        raise SourceError('A group profile edit requires profile_id')
    group = repository.get_group(group_id, session)
    if group is None:
        raise SourceError('No such group', 404, 'not_found')
    profile_id = _profile_id(payload)
    if profile_id != group.profile_id:
        if not repository.update_group(group_id, session, profile_id=profile_id):
            raise SourceError('No such group', 404, 'not_found')
        queue_missing_subtitles_recalculation(source_scope={'group_id': group_id})
        group = repository.get_group(group_id, session)
    if group is None:
        raise SourceError('No such group', 404, 'not_found')
    return group


def delete_library(source_id, library_id, session=None):
    """Remove a library. Its items and groups go; their history and blacklist rows stay."""
    library = repository.get_library(library_id, session)
    if library is None or library.source_id != source_id:
        raise SourceError('No such library', 404, 'not_found')
    repository.delete_library(library_id, session)
    refresh_tasks()


def preview_root_edit(library_id, local_paths, session=None):
    """The rows a root edit would delete: the ones whose path lies under none of ``local_paths`` and whose file is
    still on disk (the files are there but no longer in the library). Rows whose path is gone are left alone: if
    a mount moved and the user changed ``local_path`` to follow it, the next scan finds the files under the new
    root and re-paths those rows with their overrides."""
    roots = [repository.normalize_path(path) for path in local_paths]
    out = []
    for row in repository.library_items(library_id, session):
        if any(row.path == root or row.path.startswith(root.rstrip(os.sep) + os.sep) for root in roots):
            continue
        if not identity.path_is_gone(row.path):
            out.append(row.id)
    return out


def change_library_roots(source_id, library_id, roots, session=None):
    """Replace the roots of a media server library. ``roots`` is ``[{server_path, local_path}]``.

    The test runs against the ``local_path`` values after the edit, so an edit that changes only ``server_path``
    deletes nothing. The out-of-scope rows whose files still exist are deleted in the same transaction as the
    edit, and a scan of the library is queued at once. Returns how many rows were deleted, for the dialog, which
    says that history and blacklist rows are kept."""
    library = repository.get_library(library_id, session)
    source = repository.get_source(source_id, session)
    if library is None or source is None or library.source_id != source_id:
        raise SourceError('No such library', 404, 'not_found')
    if source.kind == 'folder':
        raise SourceError("A folder library's path cannot be edited: remove the library and add a new one", 400,
                          'immutable')
    cleaned = []
    for root in roots:
        if not isinstance(root, dict) or not isinstance(root.get('local_path'), str) \
                or not os.path.isabs(root['local_path']):
            raise SourceError('Each root needs an absolute local_path')
        local = repository.normalize_path(root['local_path'])
        if not os.path.isdir(local) or not os.access(local, os.R_OK | os.X_OK):
            raise SourceError(f'{local} is not a readable folder')
        clash = repository.overlapping_root(local, session, exclude_library_id=library_id)
        if clash is not None:
            raise SourceError(f"{local} overlaps {clash[1]}, a library of the media source {clash[0]}", 409,
                              'overlap')
        cleaned.append((root.get('server_path'), local))
    if not cleaned or len({local for _, local in cleaned}) != len(cleaned):
        raise SourceError('A library needs at least one root, and no root twice')
    doomed = preview_root_edit(library_id, [local for _, local in cleaned], session)
    with source_transaction(session) as connection:
        repository.replace_roots(library_id, cleaned, connection)
        repository.delete_items(doomed, connection)
    try:
        queue_scan(source_id, library_id)
    except SourceError as error:
        # The edit is committed. A library that is disabled is scanned when it is enabled again.
        logging.info('BAZARR no scan was queued after the roots of library %s changed: %s', library_id, error)
    return len(doomed)


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


def describe_library(library_id, session=None):
    library = repository.get_library(library_id, session)
    if library is None:
        return None
    body = {key: (value.isoformat() if hasattr(value, 'isoformat') else value)
            for key, value in library._mapping.items()}
    body['enabled'] = bool(body['enabled'])
    body['roots'] = [{'id': root.id, 'local_path': root.local_path, 'server_path': root.server_path}
                     for root in repository.get_roots(library_id, session)]
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
    with source_search_probes(item_id):
        return _search_source_item(item_id, job_id)


def _search_source_item(item_id, job_id):
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
    item = repository.get_item(item_id)
    if item is None:
        raise JobFailed('The item no longer exists', reason='not_found')
    stat = local_stat(item.path)
    if stat is None:
        raise JobFailed(f"The file is missing: {item.path}", reason='missing_file')
    if item.path != context.path:
        raise JobFailed('The source file moved while the search was starting. Try again.', reason='conflict')
    if item.missing_subtitles is None or not index_matches_file(item, stat):
        # The scanner still owns admission and re-identification. Refresh the tracks
        # and wanted languages of the file at this path for the immediate search.
        if index_item(item_id, keep_index_on_probe_failure=True) is None:
            raise JobFailed('The source subtitle index could not be refreshed. Try again.', reason='conflict')
        item = repository.get_item(item_id)
        if item is None:
            raise JobFailed('The item no longer exists', reason='not_found')
        try:
            refreshed = resolve_item(item_id)
        except SourceItemGone as error:
            raise JobFailed('The item no longer exists', reason='not_found') from error
        if refreshed.path != context.path or item.path != context.path:
            raise JobFailed('The source file moved while the search was starting. Try again.', reason='conflict')
        context = refreshed
    languages = _missing_languages(item)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(languages), progress_message=context.title)
    if not languages:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='Nothing is missing')
        return None
    if not get_providers():
        logging.info("BAZARR All providers are throttled")
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='All providers throttled')
        return None

    audio_languages = get_audio_profile_languages(item.audio_language)
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
            # The shared download pipeline indexes the saved subtitle during processing.
            history_log_source(1, context, result)
            downloaded += 1
    except OSError as error:
        raise JobFailed(f'Unable to save the subtitles file: {error}', reason='save_failed') from error
    jobs_queue.update_job_progress(
        job_id=job_id, progress_value='max',
        progress_message=f"{downloaded} subtitle(s) downloaded" if downloaded else 'No subtitles found')
    return None

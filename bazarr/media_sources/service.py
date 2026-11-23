# coding=utf-8
"""What the API calls: create a source with a library, queue a scan, and search one item.

A search runs through the shared pipeline with the item's ``SourceItemContext``. Its history is written here,
by the caller that did the work, never inside ``process_subtitle``.
"""
import logging
import os
import threading
from dataclasses import replace

from sqlalchemy.exc import IntegrityError

from app.database import get_audio_profile_languages, get_profiles_list
from app.get_providers import get_providers
from app.jobs_queue import JobFailed, jobs_queue
from subtitles.indexer.missing_refresh import queue_missing_subtitles_recalculation

from . import paths, repository
from .analysis import index_matches_file, local_content_stamp, local_stat, source_search_probes
from .context import SourceItemGone, resolve_item, resolve_same_item
from .db import source_transaction
from .history import history_log_source
from .indexer import index_and_notify_item, index_item
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


def _server_connection(instance_id, kind=None, session=None):
    from media_servers.http import MediaServerError

    from .kinds.media_server import server_row

    if not isinstance(instance_id, str) or not instance_id:
        raise SourceError('A saved media server connection is required')
    try:
        return server_row(instance_id, kind, session)
    except MediaServerError as error:
        raise SourceError('No matching saved Jellyfin or Emby connection', 404, 'not_found') from error


def server_libraries(instance_id, session=None):
    return _read_server_libraries(_server_connection(instance_id, session=session), session)


def _read_server_libraries(saved, session, *, for_write=False):
    from media_servers.http import MediaServerError

    from .kinds.media_server import library_choices

    try:
        return library_choices(saved, target=session)
    except MediaServerError as error:
        if for_write and saved.enabled and error.code == 'configuration_changed':
            raise SourceError('The saved connection changed. Reload it and try again.', 409, 'connection_changed') from error
        raise SourceError('Could not list libraries from the saved media server connection', 502,
                          'server_unavailable') from error


def _checked_server_roots(roots, content_kind, session, library_id=None):
    from media_servers.paths import is_media_path, media_paths_equal

    from .kinds.folder import is_storable

    if not isinstance(roots, list) or not roots:
        raise SourceError('Map at least one server root to a local folder')
    result = []
    for root in roots:
        if (not isinstance(root, dict) or not is_media_path(root.get('server_path'))
                or not is_storable(root['server_path'])):
            raise SourceError('Each root needs an absolute server_path')
        _only(root, ('local_path', 'server_path'), 'adding a server root')
        remote = root['server_path']
        check = paths.validate_path(root.get('local_path'), content_kind, target=session, exclude_library_id=library_id,
                                    include_warnings=False)
        if check.errors:
            problem = check.errors[0]
            raise SourceError(problem.message, 409 if problem.code == 'overlap' else 400, problem.code)
        if not is_storable(check.path):
            raise SourceError('The local path cannot be stored')
        if any(media_paths_equal(previous, remote) or os.path.normcase(local) == os.path.normcase(check.path)
               for previous, local in result):
            raise SourceError('Server and local roots must each be unique')
        result.append((remote, check.path))
    return result


def validate_server_roots(payload, session=None):
    """Check root mappings for a new server library without changing it."""
    _only(payload, ('roots', 'content_kind'), 'validating server roots')
    content_kind = payload.get('content_kind')
    if content_kind not in repository.CONTENT_KINDS:
        raise SourceError(f"content_kind must be one of {', '.join(repository.CONTENT_KINDS)}")
    _checked_server_roots(payload.get('roots'), content_kind, session)


def _server_library_fields(saved, library, session):
    _only(library, ('server_library_id', 'content_kind', 'profile_id', 'roots'), 'adding a server library')
    locator = library.get('server_library_id')
    if not isinstance(locator, str) or not locator:
        raise SourceError('server_library_id is required')
    chosen = next((item for item in _read_server_libraries(saved, session, for_write=True) if item['id'] == locator), None)
    if chosen is None:
        raise SourceError('Library not found on the saved media server', 404, 'not_found')
    content_kind = library.get('content_kind') or chosen['content_kind']
    if content_kind not in repository.CONTENT_KINDS:
        raise SourceError(f"content_kind must be one of {', '.join(repository.CONTENT_KINDS)}")
    return dict(locator=locator, name=chosen['name'], content_kind=content_kind,
                profile_id=_profile_id(library), roots=library.get('roots', chosen['roots']))


def _insert_server_library(source_id, fields, connection):
    roots = _checked_server_roots(fields['roots'], fields['content_kind'], connection)
    return repository.create_library(source_id=source_id, locator=fields['locator'], name=fields['name'],
                                     content_kind=fields['content_kind'], profile_id=fields['profile_id'],
                                     server_roots=roots, target=connection)


def _lock_saved_connection(saved, connection, *, require_enabled=True):
    from media_servers.http import MediaServerError

    from .kinds.media_server import check_server_revision

    try:
        check_server_revision(saved.id, saved.kind, saved.revision, connection, lock=True,
                              require_enabled=require_enabled)
    except MediaServerError as error:
        raise SourceError('The saved connection changed. Reload it and try again.', 409, 'connection_changed') from error


def _create_server_source(payload, kind, session):
    from .kinds.folder import is_storable

    saved = _server_connection(payload.get('media_server_instance_id'), kind, session)
    instance_id = saved.id
    name = payload.get('name', saved.name)
    if not isinstance(name, str) or not name.strip() or '\x00' in name or not is_storable(name):
        raise SourceError('A name is required')
    name = name.strip()
    enabled = _flag(payload.get('enabled', True))
    frequency = _frequency(payload.get('scan_frequency_hours', 12))
    library = payload.get('library')
    if library is not None and not isinstance(library, dict):
        raise SourceError('library must be an object')
    fields = _server_library_fields(saved, library, session) if library is not None else None
    try:
        with _creation_lock, source_transaction(session) as connection:
            _lock_saved_connection(saved, connection, require_enabled=fields is not None)
            if repository.source_for_server(instance_id, connection):
                raise SourceError('This connection already has a media source', 409, 'source_exists')
            if repository.name_taken(name, connection):
                raise SourceError('A media source with that name already exists', 409, 'name_taken')
            source_id = repository.create_source(kind=kind, name=name, enabled=enabled,
                                                 scan_frequency_hours=frequency,
                                                 media_server_instance_id=instance_id, target=connection)
            if fields is not None:
                _insert_server_library(source_id, fields, connection)
    except IntegrityError as error:
        raise SourceError('The media source could not be created because its configuration changed',
                          409, 'source_conflict') from error
    refresh_tasks()
    return source_id


def create_source(payload, session=None):
    """Create a source and its first library in one transaction. Returns the new source id."""
    kind_name = payload.get('kind') or 'folder'
    fields = ('name', 'kind', 'library', 'enabled', 'scan_frequency_hours')
    if kind_name in ('jellyfin', 'emby'):
        _only(payload, (*fields, 'media_server_instance_id'), 'creating a media server source')
        return _create_server_source(payload, kind_name, session)
    _only(payload, fields, 'creating a media source')
    name = payload.get('name')
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
    return [description for source in repository.list_sources(session)
            if (description := describe_source(source.id, session)) is not None]


def source_availability(sources, session=None):
    retained = repository.retained_activity_by_kind(session)
    by_kind = {kind: {'browsable': False, 'searchable': False, 'retained': retained[kind]}
               for kind in repository.CONTENT_KINDS}
    if retained['sports']:
        owners = repository.retained_sports_owners_without_libraries(session)
        if None in owners:
            by_kind['sports']['removed'] = True
        retained_ids = sorted(owners - {None})
        if retained_ids:
            by_kind['sports']['retained_source_ids'] = retained_ids
    for source in sources:
        for library in source['libraries']:
            kind = by_kind[library['content_kind']]
            kind['browsable'] = True
            if source['enabled'] and library['enabled']:
                kind['searchable'] = True
    return by_kind


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
    """Add one folder or server library to its source."""
    source = repository.get_source(source_id, session)
    if source is None:
        raise SourceError('No such media source', 404, 'not_found')
    if source.kind != 'folder':
        saved = _server_connection(source.media_server_instance_id, source.kind, session)
        fields = _server_library_fields(saved, payload, session)
        try:
            with _creation_lock, source_transaction(session) as connection:
                _lock_saved_connection(saved, connection)
                current = repository.get_source(source.id, connection, lock_config=True)
                if (current is None or current.created_at != source.created_at or current.kind != saved.kind
                        or current.media_server_instance_id != saved.id):
                    raise SourceError('The media source changed. Reload it and try again.', 409, 'source_changed')
                library_id = _insert_server_library(source_id, fields, connection)
        except IntegrityError as error:
            raise SourceError('That server library is already part of this source', 409, 'library_exists') from error
        return describe_library(library_id, session)
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
    if 'roots' in payload:
        _only(payload, ('roots', 'confirmed_removed'), 'editing server roots')
        deleted = change_library_roots(source_id, library_id, payload['roots'], session,
                                       confirmed_removed=payload.get('confirmed_removed'))
        return {**describe_library(library_id, session), 'removed_items': deleted}
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


def update_group_profile(group_id, payload, session=None, *, expected_kind=None):
    """Set or clear a source group's profile and refresh its items after the write."""
    if not isinstance(payload, dict) or payload.keys() != {'profile_id'}:
        raise SourceError('A group profile edit requires profile_id')
    with source_transaction(session) as connection:
        group = repository.get_group(group_id, connection, for_update=True)
        if group is None or (expected_kind is not None and group.kind != expected_kind):
            raise SourceError('No such group', 404, 'not_found')
        profile_id = _profile_id(payload)
        changed = profile_id != group.profile_id
        if changed:
            repository.update_group(group_id, connection, profile_id=profile_id)
            group = repository.get_group(group_id, connection)
    if changed:
        queue_missing_subtitles_recalculation(source_scope={'group_id': group_id})
    return group


def update_item_profile(item_id, payload, session=None, *, expected_kind=None):
    """Set or clear a source item's profile and refresh its stored missing subtitles."""
    if not isinstance(payload, dict) or payload.keys() != {'profile_id'}:
        raise SourceError('An item profile edit requires profile_id')
    profile_id = _profile_id(payload)
    with source_transaction(session) as connection:
        item = repository.get_item(item_id, connection, for_update=True)
        if item is None or (expected_kind is not None and item.kind != expected_kind):
            raise SourceError('No such item', 404, 'not_found')
        changed = profile_id != item.profile_id
        if changed:
            repository.update_item(item_id, connection, profile_id=profile_id)
            item = repository.get_item(item_id, connection)
    if changed:
        queue_missing_subtitles_recalculation(source_scope={'item_id': item_id})
    return item


def delete_library(source_id, library_id, session=None):
    """Remove a library. Its items and groups go; their history and blacklist rows stay."""
    library = repository.get_library(library_id, session)
    if library is None or library.source_id != source_id:
        raise SourceError('No such library', 404, 'not_found')
    repository.delete_library(library_id, session)
    refresh_tasks()


def preview_root_edit(library_id, local_paths, session=None):
    """Only existing files outside every retained local root are removed by a root edit."""
    roots = [os.path.normcase(repository.normalize_path(path)) for path in local_paths]
    return [row.id for row in repository.library_items(library_id, session)
            if os.path.isabs(row.path)
            and not any(repository._inside(os.path.normcase(row.path), root) for root in roots)
            and os.path.isfile(row.path)]


def _server_library_for_edit(source_id, library_id, session, *, for_update=False):
    library = repository.get_library(library_id, session, for_update=for_update)
    source = repository.get_source(source_id, session)
    if library is None or source is None or library.source_id != source_id:
        raise SourceError('No such library', 404, 'not_found')
    if source.kind == 'folder':
        raise SourceError("A folder library's path cannot be edited: remove the library and add a new one", 400,
                          'immutable')
    return library


def preview_library_roots(source_id, library_id, roots, session=None):
    library = _server_library_for_edit(source_id, library_id, session)
    checked = _checked_server_roots(roots, library.content_kind, session, library_id)
    return len(preview_root_edit(library_id, [local for _, local in checked], session))


def change_library_roots(source_id, library_id, roots, session=None, *, confirmed_removed=None):
    """Replace the mappings and remove only the existing files confirmed in the preview."""
    from .scanner import library_lock

    lock = library_lock(library_id)
    if not lock.acquire(blocking=False):
        raise SourceError('This library is scanning. Try again when its scan finishes.', 409, 'library_busy')
    try:
        with _creation_lock, source_transaction(session) as connection:
            library = _server_library_for_edit(source_id, library_id, connection, for_update=True)
            cleaned = _checked_server_roots(roots, library.content_kind, connection, library_id)
            doomed = preview_root_edit(library_id, [local for _, local in cleaned], connection)
            if (confirmed_removed is not None or doomed) and (
                    type(confirmed_removed) is not int or confirmed_removed != len(doomed)):
                raise SourceError(f'This change removes {len(doomed)} items. Preview the change and confirm that count.',
                                  409, 'root_change_confirmation')
            repository.replace_roots(library_id, cleaned, connection)
            deleted = repository.delete_items(doomed, connection, owner_library_id=library_id)
    finally:
        lock.release()
    try:
        queue_scan(source_id, library_id)
    except SourceError as error:
        logging.info('BAZARR no scan was queued after the roots of library %s changed: %s', library_id, error)
    return deleted


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
    try:
        return search_source_item(item_id)
    except JobFailed as error:
        if error.reason == 'not_found':
            raise SourceError(str(error), 404, 'not_found') from error
        if error.reason == 'incomplete_identity':
            raise SourceError(str(error), 409, error.reason) from error
        raise


def queue_group_search(group_id):
    from .scanner import disabled_reason

    group = repository.get_group(group_id)
    if group is None or group.kind != 'show':
        raise SourceError('No such show', 404, 'not_found')
    reason = disabled_reason(group.source_id, group.library_id)
    if reason:
        raise SourceError(reason, 409, 'disabled')
    return search_source_group(group_id, (group.source_id, group.library_id, group.created_at.isoformat()))


def search_source_group(group_id, expected_group, job_id=None):
    """Search eligible episodes of this show using the normal source Wanted search."""
    if not job_id:
        return jobs_queue.add_job_from_function('Search for Missing Show Subtitles', is_progress=True,
                                                return_existing=True)
    from .db import handle
    from .wanted import wanted_items

    group = repository.get_group(group_id)
    if (group is None or group.kind != 'show'
            or (group.source_id, group.library_id, group.created_at.isoformat()) != tuple(expected_group)):
        raise JobFailed('The show has changed or no longer exists', reason='not_found')
    items = repository.Items.c
    eligible = wanted_items(items.id, items.created_at, items.title, kind='episode').where(
        items.group_id == group_id, items.source_id == group.source_id, items.library_id == group.library_id,
        repository.Groups.c.created_at == group.created_at)
    session = handle()
    last_id = 0
    while page := session.execute(eligible.where(items.id > last_id).order_by(items.id).limit(500)).all():
        last_id = page[-1].id
        for item_id, created_at, title in page:
            jobs_queue.raise_if_cancelled(job_id)
            jobs_queue.update_job_progress(job_id=job_id, progress_message=title)
            try:
                search_wanted_item(item_id, created_at, job_id, group_id=group_id)
            except JobFailed as error:
                logging.info('BAZARR source episode %s could not be searched: %s', item_id, error)
    jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='Search completed')


def _language_code(language, hi, forced):
    return language + (':hi' if hi == 'True' else ':forced' if forced == 'True' else '')


def _require_sports_identity(item):
    if item.kind == 'sports_event' and item.identity_state not in ('parsed', 'identified'):
        raise JobFailed('Complete the sports event identity before automatic search, or use manual search.',
                        reason='incomplete_identity')


def _search_label(item_id):
    item = repository.get_item(item_id)
    if item is None:
        raise JobFailed('The item no longer exists', reason='not_found')
    _require_sports_identity(item)
    return item.title


def search_source_item(item_id, job_id=None, *, translation_gate=True, expected_source=None):
    """Search the missing subtitles of one source item. Called without a job id it queues itself."""
    if not job_id:
        if expected_source is None:
            try:
                expected_source = resolve_item(item_id)
            except SourceItemGone as error:
                raise JobFailed('The item no longer exists', reason='not_found') from error
        return jobs_queue.add_job_from_function(f"Searching subtitles for {_search_label(item_id)}", is_progress=True)
    with source_search_probes(item_id):
        return _search_source_item(item_id, job_id, translation_gate=translation_gate, expected_source=expected_source)


def _search_source_item(item_id, job_id, *, translation_gate, expected_source):
    if expected_source is not None:
        context = resolve_same_item(expected_source)
        if context is None or context.item_id != item_id:
            return None
    else:
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
    if item is None or not context.same_item(item):
        raise JobFailed('The item no longer exists', reason='not_found')
    if item.server_missing:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max',
                                       progress_message='The item is missing from its media server library')
        return None
    _require_sports_identity(item)
    stat = local_stat(item.path)
    if stat is None:
        raise JobFailed(f"The file is missing: {item.path}", reason='missing_file')
    if item.path != context.path:
        raise JobFailed('The source file moved while the search was starting. Try again.', reason='conflict')
    if item.missing_subtitles is None or not index_matches_file(item, stat):
        # The scanner still owns admission and re-identification. Refresh the tracks
        # and wanted languages of the file at this path for the immediate search.
        if index_item(item_id, keep_index_on_probe_failure=True, expected_path=context.path,
                      expected_source=context) is None:
            raise JobFailed('The source subtitle index could not be refreshed. Try again.', reason='conflict')
        item = repository.get_item(item_id)
        if item is None or not context.same_item(item):
            raise JobFailed('The item no longer exists', reason='not_found')
        refreshed = resolve_same_item(context)
        if refreshed is None or item.path != context.path:
            raise JobFailed('The source file moved while the search was starting. Try again.', reason='conflict')
        context = refreshed
        _require_sports_identity(item)
    from .translation import missing_language_candidates

    candidates = missing_language_candidates(context)
    languages = _search_languages(context, candidates, translation_gate=translation_gate)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(languages), progress_message=context.title)
    if not languages:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max',
                                       progress_message='Subtitles found or queued' if candidates else 'Nothing is missing')
        return None
    if not get_providers():
        logging.info("BAZARR All providers are throttled")
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max', progress_message='All providers throttled')
        return None

    downloaded = _download_source_subtitles(context, item, languages, job_id, translation_gate=translation_gate)
    jobs_queue.update_job_progress(
        job_id=job_id, progress_value='max',
        progress_message=f"{len(downloaded)} subtitle(s) downloaded" if downloaded else 'No subtitles found')
    return None


def _search_languages(context, languages, *, translation_gate, automatic=False):
    if translation_gate and languages:
        from .translation import queue_missing_translations

        try:
            covered = queue_missing_translations(context, **({'wanted': True} if automatic else {}))
        except Exception:
            logging.exception('BAZARR could not queue a translation for source item %s', context.item_id)
        else:
            languages = [entry for entry in languages if _language_code(*entry) not in covered]
    return languages


def search_wanted_fallback(source, target_language, job_id=None):
    """Resume one rejected translation's target under the scheduled search rules."""
    if not job_id:
        return jobs_queue.add_job_from_function(f"Searching subtitles for {source.title}", is_progress=True,
                                                return_existing=True)
    return search_wanted_item(source.item_id, source.created_at, job_id, translation_gate=False,
                              expected_source=source, target_language=target_language)


def search_wanted_item(item_id, created_at, job_id, *, translation_gate=True, expected_source=None,
                       target_language=None, group_id=None, expected_source_id=None):
    """Search only stored missing languages, after rechecking eligibility and the current Arr path."""
    from .shadows import path_stage_shadow
    from .translation import stored_missing_language_candidates
    from .wanted import arr_claim_blocks_wanted, notify_wanted_changes, wanted_items
    from .db import handle
    from subtitles.adaptive_searching import is_search_active, updateFailedAttempts

    items = repository.Items
    same_row = (items.c.id == item_id, items.c.created_at == created_at)
    if group_id is not None:
        same_row += (items.c.group_id == group_id,)
    if expected_source_id is not None:
        same_row += (items.c.source_id == expected_source_id,)
    session = handle()
    item = session.execute(wanted_items(*items.c).where(*same_row)).first()
    if item is None:
        return
    stamp = local_content_stamp(item.path)
    if stamp is None or (expected_source is not None and (
            expected_source.item_id, expected_source.path, expected_source.source_id,
            expected_source.library_id, expected_source.wanted_file_stamp
    ) != (item_id, item.path, item.source_id, item.library_id, stamp)):
        return
    if path_stage_shadow(item_id):
        notify_wanted_changes(item.kind)
        return
    try:
        context = resolve_item(item_id)
    except SourceItemGone:
        return
    if not context.same_item(item) or context.path != item.path or not context.profile_id:
        return

    candidates = stored_missing_language_candidates(item)
    if target_language is not None:
        candidates = [entry for entry in candidates if _language_code(*entry) == target_language]
    wanted_source = replace(context, wanted_file_stamp=stamp)
    languages = [
        entry for entry in _search_languages(wanted_source, candidates, translation_gate=translation_gate, automatic=True)
        if is_search_active(_language_code(*entry), item.failed_attempts or '[]')
    ]
    if not languages or not get_providers():
        return

    # Settings, indexing and scanning can change eligibility while the translation gate runs.
    current = session.execute(wanted_items(*items.c, profile_id=context.profile_id).where(*same_row)).first()
    if current is None or current.path != context.path or current.library_id != context.library_id:
        return
    languages = [entry for entry in languages if entry in stored_missing_language_candidates(current)]
    if not languages:
        return
    with source_search_probes(item_id):
        downloaded = _download_source_subtitles(wanted_source, current, languages, job_id,
                                                translation_gate=translation_gate, wanted_stamp=stamp)
    failed = {_language_code(*entry) for entry in languages} - set(downloaded)
    if failed and local_content_stamp(context.path) == stamp and not arr_claim_blocks_wanted(wanted_source):
        with source_transaction() as connection:
            current = repository.get_item(item_id, connection, for_update=True)
            if (current is None or not context.same_item(current) or current.path != context.path
                    or current.arr_shadow or current.server_missing or not current.monitored):
                return
            failed &= {_language_code(*entry) for entry in stored_missing_language_candidates(current)}
            if failed:
                attempts = current.failed_attempts or '[]'
                for language in sorted(failed):
                    attempts = updateFailedAttempts(language, attempts)
                repository.set_failed_attempts(item_id, attempts, connection)


def _download_source_subtitles(context, item, languages, job_id, *, translation_gate=True, wanted_stamp=None):
    audio_languages = get_audio_profile_languages(item.audio_language)
    audio_language = audio_languages[0]['name'] if audio_languages else 'None'
    # For an episode the title the providers get is the show's.
    title = context.group_title if context.kind == 'episode' and context.group_title else context.title

    from subtitles.download import generate_subtitles
    from app.notifier import send_notifications_source

    downloaded = []
    try:
        for result in generate_subtitles(context.path, languages, audio_language, str(context.scene_name), title,
                                         context.media_type, context.profile_id, check_if_still_required=True,
                                         job_id=job_id, source=context,
                                         **({'source_wanted_stamp': wanted_stamp} if wanted_stamp is not None else {}),
                                         **({} if translation_gate else {'auto_translate': False})):
            if not result:
                continue
            with source_transaction() as connection:
                history_log_source(1, context, result, target=connection)
            downloaded.append(result.language_code)
            current = resolve_same_item(context)
            if current is None:
                continue
            if getattr(result, 'source_indexed', False) is not True:
                # Retry an incomplete listing, including results from outside shared processing.
                index_and_notify_item(current)
            try:
                send_notifications_source(current, result.message)
            except Exception:
                logging.exception('BAZARR could not send a notification for source item %s', context.item_id)
    except OSError as error:
        raise JobFailed(f'Unable to save the subtitles file: {error}', reason='save_failed') from error
    return downloaded

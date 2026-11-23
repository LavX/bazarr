# coding=utf-8
"""Shared Jellyfin and Emby discovery over bounded, authenticated collection requests."""
import os
import re
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import requests
from sqlalchemy import select

from media_servers.http import MediaServerError, validate_server_url
from media_servers.item_pages import item_id
from media_servers.paths import is_media_path, map_media_path, media_paths_equal
from media_servers.repository import MediaServerInstanceRepository

from .. import identity, paths, repository
from ..db import handle
from . import DiscoveredFile, IdentityHints, Listing, Problem, SourceHealth
from .folder import FolderKind, is_storable, is_video_file

_COLLECTION_KINDS = {'movies': 'movie', 'tvshows': 'series'}
_ITEM_TYPES = {'movie': ('Movie',), 'series': ('Episode',), 'sports': ('Movie', 'Episode', 'Video')}


def server_row(instance_id, kind=None, target=None):
    from app.database import TableMediaServerInstances

    table = TableMediaServerInstances.__table__
    row = handle(target).execute(select(table).where(table.c.id == instance_id)).first()
    if row is None or row.kind not in ('jellyfin', 'emby') or (kind is not None and row.kind != kind):
        raise MediaServerError('source_connection_missing')
    return row


def check_server_revision(instance_id, kind, revision, target, *, lock=False, require_enabled=True):
    """Fence a request or, inside a write transaction, the corresponding database changes."""
    from app.database import TableMediaServerInstances

    table = TableMediaServerInstances.__table__
    query = select(table.c.revision, table.c.enabled).where(table.c.id == instance_id, table.c.kind == kind)
    if lock:
        query = query.with_for_update(key_share=True)
    current = handle(target).execute(query).first()
    if current is None or current.revision != revision or (require_enabled and not current.enabled):
        raise MediaServerError('configuration_changed')


@contextmanager
def server_client(instance_id, kind, target=None, *, expected_revision=None):
    """Read the saved address and its credential together. Recheck the revision before every request."""
    repo = MediaServerInstanceRepository(handle(target))
    try:
        row = server_row(instance_id, kind, target)
    except MediaServerError:
        if expected_revision is not None:
            raise MediaServerError('configuration_changed') from None
        raise
    if expected_revision is not None and (row.revision != expected_revision or not row.enabled):
        raise MediaServerError('configuration_changed')
    revision, url, verify_ssl = row.revision, validate_server_url(row.url), bool(row.verify_ssl)
    key = repo.snapshot_api_key(row)
    if not row.enabled or not key:
        raise MediaServerError('source_connection_unavailable')

    def guard():
        check_server_revision(instance_id, kind, revision, target)

    if kind == 'jellyfin':
        from jellyfin.client import JellyfinClient

        client = JellyfinClient(url, key, verify_ssl, guard=guard)
        client.session.trust_env = False
        try:
            yield client, guard
            guard()
        finally:
            client.session.close()
    else:
        from emby.client import EmbyClient

        with EmbyClient(url, key, verify_ssl, guard=guard) as client:
            yield client, guard
            guard()


def _suggest_local_path(connection, server_path, library_id):
    import json

    if connection.kind != 'emby':
        return None
    try:
        mappings = [{'local_path': row['remote_path'], 'remote_path': row['local_path']}
                    for row in json.loads(connection.path_mappings)
                    if not row.get('library_id') or row['library_id'] == library_id]
        path = map_media_path(server_path, mappings)['path']
    except (MediaServerError, ValueError, TypeError, KeyError):
        return None
    return path if os.path.isabs(path) else None


def library_choices(connection, *, target=None):
    """Safe server library names and root suggestions from an existing connection."""
    from media_servers.instances import parse_options

    try:
        with server_client(connection.id, connection.kind, target,
                           expected_revision=connection.revision) as (client, _):
            libraries = client.get_libraries()
    except (requests.RequestException, ValueError, RecursionError):
        raise MediaServerError('collection_request_failed') from None
    if not isinstance(libraries, list):
        raise MediaServerError('invalid_response')
    options = parse_options(connection.options) if connection.kind == 'jellyfin' else {}
    selected = {value for key in ('movie_library_ids', 'series_library_ids', 'sports_library_ids')
                if isinstance(options.get(key), list) for value in options[key] if isinstance(value, str)}
    result = {}
    for row in libraries:
        if not isinstance(row, dict):
            continue
        try:
            library_id = item_id(row.get('ItemId') or row.get('Id'))
        except MediaServerError:
            continue
        name = _text(row.get('Name')) or library_id
        collection = (_text(row.get('CollectionType')) or '').lower()
        locations = row.get('Locations')
        root_paths = []
        for location in locations if isinstance(locations, list) else []:
            if is_media_path(location) and is_storable(location) and not any(media_paths_equal(location, known) for known in root_paths):
                root_paths.append(location)
        result[library_id] = {
            'id': library_id, 'name': name, 'type': collection, 'content_kind': _COLLECTION_KINDS.get(collection),
            'selected': library_id in selected,
            'roots': [{'server_path': path, 'local_path': _suggest_local_path(connection, path, library_id)}
                      for path in root_paths],
        }
    return list(result.values())


def _text(value):
    if isinstance(value, str) and '\x00' not in value and is_storable(value):
        return value.strip() or None
    return None


def _integer(value, maximum=2_147_483_647):
    if type(value) is str and value.isascii() and value.isdecimal() and len(value) < 12:
        value = int(value)
    return value if type(value) is int and 0 <= value <= maximum else None


def _date(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    return None


def _hints(row):
    providers = row.get('ProviderIds')
    if not isinstance(providers, dict):
        providers = {}
    providers = {name.lower(): value for name, value in providers.items() if isinstance(name, str)}
    imdb = providers.get('imdb')
    imdb = imdb if isinstance(imdb, str) and re.fullmatch(r'tt[0-9]{1,20}', imdb) else None
    tmdb, tvdb = _integer(providers.get('tmdb')), _integer(providers.get('tvdb'))
    return IdentityHints(title=_text(row.get('Name')), year=_integer(row.get('ProductionYear'), 9999),
                         season=_integer(row.get('ParentIndexNumber')), episode=_integer(row.get('IndexNumber')),
                         imdb_id=imdb, tmdb_id=str(tmdb) if tmdb else None, tvdb_id=tvdb or None,
                         external_id=row['Id'], premiere_date=_date(row.get('PremiereDate')))


def _group_directory(path, root):
    relative = os.path.relpath(path, root).split(os.sep)
    return os.path.join(root, relative[0]) if len(relative) > 1 else None


class MediaServerKind:
    groups_are_directories = False

    def __init__(self, target=None):
        self.target = target
        self.series = {}

    def validate_library(self, source, library, *, roots=None):
        if source is None or source.media_server_instance_id is None:
            return [Problem('source_connection_missing', 'A saved media server connection is required.')]
        try:
            item_id(library.locator)
        except MediaServerError:
            return [Problem('invalid_library', 'A server library identifier is required.')]
        if roots is None:
            roots = repository.get_roots(library.id, self.target)
        if not roots:
            return [Problem('invalid_roots', 'Map at least one server root to a local folder.')]
        problems = []
        for root in roots:
            if not is_media_path(root.server_path):
                problems.append(Problem('invalid_roots', 'Server roots must be absolute paths.'))
            problems.extend(FolderKind().validate_library(None, SimpleNamespace(
                locator=root.local_path, content_kind=library.content_kind)))
        return problems

    def health(self, source):
        try:
            with server_client(source.media_server_instance_id, self.kind, self.target) as (client, _):
                client.get_libraries()
        except (MediaServerError, requests.RequestException, ValueError):
            return SourceHealth(False, 'Could not read libraries from the saved media server connection.')
        return SourceHealth(True)

    def enumerate(self, source, library, cancel=None, *, roots=None, revision=None):
        listing = Listing(files=iter(()))
        listing.files = self._scan(source, library, listing, cancel, roots, revision)
        return listing

    def _scan(self, source, library, result, cancel, roots, revision):
        roots = paths.ServerRoots(repository.get_roots(library.id, self.target) if roots is None else roots)
        self.series.clear()
        try:
            with server_client(source.media_server_instance_id, self.kind, self.target,
                               expected_revision=revision) as (client, _):
                series = client.get_item_pages(library.locator, ('Series',), cancel=cancel) if (
                    library.content_kind == 'series') else None
                media = client.get_item_pages(library.locator, _ITEM_TYPES[library.content_kind], cancel=cancel)
        except MediaServerError as error:
            if revision is not None and error.code == 'configuration_changed':
                raise
            result.problems.append('The media server connection became unavailable.')
            return
        pages = (media,) if series is None else (series, media)
        result.complete = all(page.complete for page in pages)
        result.drifted = any(page.drifted for page in pages)
        if any(page.error is not None for page in pages):
            result.problems.append('The media server library listing stopped before every page was received.')
        if result.drifted:
            result.problems.append('The media server library count changed or included repeated items.')
        if series is not None:
            self.series.update({f'server:{item}': _hints(row) for item, row in series.items.items()
                                if row.get('Type', 'Series') == 'Series'})
        result.seen_external_ids.update(media.items)
        seen_paths = set()
        seen_files = set()
        for row in media.items.values():
            if cancel is not None:
                cancel()
            try:
                path, root = roots.resolve(row.get('Path'))
            except MediaServerError:
                result.unmapped += 1
                continue
            if path in seen_paths:
                result.drifted = True
                continue
            if not is_storable(path) or not is_video_file(path) or path.lower().endswith('.strm'):
                result.unavailable += 1
                continue
            stat = identity.stat_file(path) if os.path.isfile(path) else None
            if stat is None:
                result.unavailable += 1
                continue
            if (stat.dev, stat.ino) in seen_files:
                continue
            group_key = None
            if library.content_kind != 'movie':
                if library.content_kind == 'series':
                    try:
                        group_key = f"server:{item_id(row.get('SeriesId'))}"
                    except MediaServerError:
                        pass
                group_key = group_key or _group_directory(path, root)
                if group_key is None:
                    result.ungrouped += 1
                    continue
            seen_paths.add(path)
            seen_files.add((stat.dev, stat.ino))
            yield DiscoveredFile(path, stat.size, stat.mtime_ns, stat.dev, stat.ino, group_key, _hints(row))

    def lookup_item_ids(self, source, groups, cancel=None, *, revision=None):
        with server_client(source.media_server_instance_id, self.kind, self.target,
                           expected_revision=revision) as (client, guard):
            for ids in groups:
                if cancel is not None:
                    cancel()
                found = client.lookup_item_ids(ids)
                guard()
                yield ids, found

    def series_identity(self, source, group_key):
        return self.series.get(group_key)

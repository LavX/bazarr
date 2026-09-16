# coding=utf-8
"""Rescan every library one destination is scoped to, on request.

The refresh worker only climbs the library rung for a publication it is holding.
A user who wants their server re-read now, after a restore or after fixing a
connection, has nothing to press: retry only drains targets that are already
queued, and an empty queue drains nothing.

This is that button. It walks the same ``refresh_library`` rung the ladder uses,
over the scoping each kind actually has: the libraries chosen on the instance for
Jellyfin and Plex, the library ids in the path mappings for Silo, and the mapped
roots for Emby, which resolves a library from a path rather than from an id.
"""

import logging

from . import resolution
from .http import MediaServerError
from .instances import KIND_STEPS, LIBRARY_KEYS
from .paths import _media_path

# The types a library scan can be asked for. Sports lives inside a movie or
# series library everywhere, and is scoped separately on the kinds that have it.
_MEDIA_TYPES = ('movie', 'episode', 'sports')


def supports_library_refresh(kind):
    return resolution.LIBRARY in KIND_STEPS.get(kind, ())


def _requests(snapshot):
    """Every (call description) this destination can be asked to rescan.

    Yields callables taking the client, so the walk below stays the same shape
    for a kind that addresses libraries by handle and one that addresses them by
    a path it maps into.
    """
    kind = snapshot.kind
    if kind in LIBRARY_KEYS:
        for media_type in _MEDIA_TYPES:
            if snapshot.libraries(media_type):
                yield media_type, (lambda client, media_type=media_type:
                                   client.refresh_library(media_type))
        return
    if kind == 'silo':
        seen = set()
        for row in snapshot.mappings():
            library_id = (row.get('library_id') or '').strip()
            if not library_id or library_id in seen:
                continue
            seen.add(library_id)
            yield library_id, (lambda client, library_id=library_id:
                               client.refresh_library(library_id))
        return
    # Emby resolves the library from a path, so its scoping is the remote roots
    # its mappings point at, asked once per media type the root can hold.
    for row in snapshot.mappings():
        remote = row.get('remote_path')
        try:
            _media_path(remote)
        except MediaServerError:
            continue
        for media_type in _MEDIA_TYPES:
            yield f'{media_type}:{remote}', (lambda client, media_type=media_type, remote=remote:
                                             client.refresh_library(media_type, remote))


def refresh_libraries(instance_id):
    """Ask one saved destination to rescan everything it is scoped to.

    Returns how many library scans the server accepted. A rung that answers
    nothing for one scope is that scope holding no such library, not a failure,
    so it is skipped and the rest still run.
    """
    from .dispatcher import _client, get_native_configuration
    configuration = get_native_configuration()
    _revision, snapshot, _changing = configuration.read(instance_id)
    if not supports_library_refresh(snapshot.kind):
        raise MediaServerError('invalid_kind')
    if not snapshot.enabled:
        raise MediaServerError('connection_disabled')
    if snapshot.configuration_error:
        raise MediaServerError(snapshot.configuration_error)
    scopes = list(_requests(snapshot))
    if not scopes:
        raise MediaServerError('library_missing')
    requested = 0
    with _client(snapshot.kind, snapshot) as client:
        for scope, call in scopes:
            try:
                if call(client) is not None:
                    requested += 1
            except MediaServerError:
                raise
            except Exception:
                logging.debug('BAZARR could not rescan %s on a media server destination',
                              scope, exc_info=True)
    if not requested:
        # Every scope answered nothing, so the server holds no library the
        # instance is pointed at. Saying so beats reporting a scan that ran.
        raise MediaServerError('library_missing')
    return requested

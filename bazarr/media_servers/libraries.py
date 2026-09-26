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

# Refusals that are about the server rather than one scope. Every other scope
# would fail the same way, each after waiting out its own timeout, so the run
# stops at the first of these.
_SERVER_WIDE = frozenset({'connection_error', 'timeout', 'tls_error', 'unauthorized',
                          'forbidden', 'missing_credentials'})


def supports_library_refresh(kind):
    return resolution.LIBRARY in KIND_STEPS.get(kind, ())


def _requests(snapshot):
    """Every (call description) this destination can be asked to rescan.

    Yields callables taking the client and the rung's ``ensure_current`` and
    ``coalesce`` keywords, so the walk below stays the same shape for a kind that
    addresses libraries by handle and one that addresses them by a path it maps
    into.
    """
    kind = snapshot.kind
    if kind in LIBRARY_KEYS:
        for media_type in _MEDIA_TYPES:
            if snapshot.libraries(media_type):
                yield media_type, (lambda client, media_type=media_type, **guards:
                                   client.refresh_library(media_type, **guards))
        return
    if kind == 'silo':
        seen = set()
        for row in snapshot.mappings():
            library_id = (row.get('library_id') or '').strip()
            if not library_id or library_id in seen:
                continue
            seen.add(library_id)
            yield library_id, (lambda client, library_id=library_id, **guards:
                               client.refresh_library(library_id, **guards))
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
            yield f'{media_type}:{remote}', (lambda client, media_type=media_type, remote=remote, **guards:
                                             client.refresh_library(media_type, remote, **guards))


def refresh_libraries(instance_id):
    """Ask one saved destination to rescan everything it is scoped to.

    Returns how many library scans the server accepted and how many it refused.
    A rung that answers nothing for one scope is that scope holding no such
    library, not a failure, so it is skipped and the rest still run. A scope the
    server refuses does not stop the rest either: it is counted, and the run
    fails only when nothing was accepted. A server that cannot be reached, or
    will not accept the credentials, stops the run at once.
    """
    from .dispatcher import _client, _coalescer, get_native_configuration, running_dispatcher
    configuration = get_native_configuration()
    revision, snapshot, _changing = configuration.read(instance_id)
    if not supports_library_refresh(snapshot.kind):
        raise MediaServerError('invalid_kind')
    if not snapshot.enabled:
        raise MediaServerError('connection_disabled')
    if snapshot.configuration_error:
        raise MediaServerError(snapshot.configuration_error)
    scopes = list(_requests(snapshot))
    if not scopes:
        raise MediaServerError('library_missing')

    def guard():
        # An edit, a switch-off or a delete while this walks its scopes stops it
        # before the next request, as it stops the refresh worker.
        configuration.ensure_current(instance_id, revision)

    # One scan per library for the whole run. Emby's sports request matches any
    # library holding the root, so the movie or TV library its typed request had
    # just scanned was scanned a second time.
    coalesce = _coalescer({}, 0)
    workers = running_dispatcher()
    dropped = workers.dropped(instance_id) if workers else 0
    requested = 0
    refused = []
    complete = True
    with _client(snapshot.kind, snapshot) as client:
        for scope, call in scopes:
            try:
                if call(client, ensure_current=guard, coalesce=coalesce) is not None:
                    requested += 1
            except MediaServerError as error:
                # A destination edited, switched off or deleted under the run is
                # not one scope's refusal either, so nothing more is asked of it.
                if error.code in _SERVER_WIDE or error.code == 'configuration_changed':
                    raise
                logging.warning('BAZARR could not rescan %s on a media server destination: %s',
                                scope, error.code)
                refused.append(error)
            except Exception:
                complete = False
                logging.debug('BAZARR could not rescan %s on a media server destination',
                              scope, exc_info=True)
    if not requested:
        if refused:
            raise refused[0]
        # Every scope answered nothing, so the server holds no library the
        # instance is pointed at. Saying so beats reporting a scan that ran.
        raise MediaServerError('library_missing')
    if complete and not refused and workers:
        # Every library the destination is scoped to was just asked to rescan,
        # which covers the mutations the full queue dropped before this began.
        workers.covered(instance_id, dropped)
    return {'requested': requested, 'failed': len(refused)}

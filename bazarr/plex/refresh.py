# coding=utf-8
"""Plex on the shared resolution ladder, asked for exactly what it was before.

`plex_refresh_item` resolves an item by its IMDB guid across the configured
sections and otherwise updates the whole section. Those are the two rungs, and
no title rung is invented here: Plex never had one. Plex resolves the item
itself, so it declares no path rung and needs no path mappings.
"""

import logging

from media_servers import resolution
from media_servers.http import MediaServerError

logger = logging.getLogger(__name__)


def _sections(server, names):
    """Each configured section that exists, in the order the user listed them."""
    for name in names:
        try:
            yield name, server.library.section(name)
        except Exception:
            # A renamed or removed section is not this publication's failure;
            # the remaining sections and the rungs below still apply.
            logger.debug('Plex section %r is not available for a refresh', name)


class PlexRefreshClient:
    REFRESH_STEPS = (resolution.PROVIDER_ID, resolution.LIBRARY)

    def __init__(self, snapshot):
        from media_servers.http import validate_server_url
        from .operations import plex_server_for
        if not snapshot.apikey or not snapshot.apikey.strip():
            raise MediaServerError('missing_credentials')
        self.snapshot = snapshot
        self.url = validate_server_url(snapshot.url)
        self.token = snapshot.apikey
        self.verify_ssl = snapshot.verify_ssl
        self._server = None
        self._connect = plex_server_for

    def close(self):
        # The pooled PlexServer is cached by endpoint and outlives one refresh.
        self._server = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @property
    def server(self):
        if self._server is None:
            try:
                self._server = self._connect(self.url, self.token, self.verify_ssl)
            except Exception:
                raise MediaServerError('connection_error') from None
        return self._server

    def refresh_by_provider_id(self, media_type, metadata, *, ensure_current=None):
        """The IMDB guid, across the configured sections of this type."""
        if media_type not in ('movie', 'episode'):
            raise MediaServerError('internal_error')
        if not metadata.imdb_id or not metadata.locatable(media_type):
            return None
        for name, section in _sections(self.server, self.snapshot.libraries(media_type)):
            if ensure_current:
                ensure_current()
            try:
                item = section.getGuid(f'imdb://{metadata.imdb_id}')
                if media_type != 'movie':
                    item = item.episode(season=metadata.season, episode=metadata.episode)
                item.refresh()
            except Exception:
                logger.debug('Plex section %r holds no item for this publication', name)
                continue
            if ensure_current:
                ensure_current()
            return {'status': 'requested'}
        return None

    def refresh_library(self, media_type, *, ensure_current=None, coalesce=None):
        """Update each configured section of this type, as the singleton did."""
        names = self.snapshot.libraries(media_type)
        if not names:
            return None
        updated = False
        for name, section in _sections(self.server, names):
            if coalesce is not None and coalesce(name):
                updated = True
                continue
            if ensure_current:
                ensure_current()
            try:
                section.update()
            except Exception:
                logger.debug('Plex section %r refused a library update', name)
                continue
            updated = True
        if ensure_current:
            ensure_current()
        # Nothing was asked of Plex at all, so this rung did not answer and the
        # walk reports the publication as unresolved rather than as requested.
        return {'status': 'requested'} if updated else None

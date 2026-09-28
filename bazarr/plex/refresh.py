# coding=utf-8
"""Plex on the shared resolution ladder, asked for exactly what it was before.

`plex_refresh_item` resolves an item by its IMDB guid across the configured
sections and otherwise updates the whole section. Those are the two rungs, and
no title rung is invented here: Plex never had one. Plex resolves the item
itself, so it declares no path rung and needs no path mappings.
"""

import logging

import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from media_servers import resolution
from media_servers.http import MediaServerError
from media_servers.paths import file_name

logger = logging.getLogger(__name__)


def _refusal(error):
    """A Plex failure in the shared vocabulary, so the status names the real cause.

    Only a lookup miss means "not in this section". Anything else, an expired
    token or a server that stopped answering, has to reach the dispatcher as
    itself rather than as item_missing, which points the user at the library.
    """
    if isinstance(error, MediaServerError):
        return error
    if isinstance(error, Unauthorized):
        return MediaServerError('unauthorized')
    if isinstance(error, requests.exceptions.SSLError):
        return MediaServerError('tls_error')
    if isinstance(error, requests.exceptions.Timeout):
        return MediaServerError('timeout')
    if isinstance(error, (BadRequest, NotFound)):
        return MediaServerError('request_rejected')
    return MediaServerError('connection_error')


def _sections(server, names):
    """Each configured section that exists, in the order the user listed them."""
    for name in names:
        try:
            section = server.library.section(name)
        except NotFound:
            # A renamed or removed section is not this publication's failure;
            # the remaining sections and the rungs below still apply.
            logger.debug('Plex section %r is not available for a refresh', name)
            continue
        except Exception as error:
            # Looking the name up can reach the server, and a server that has
            # stopped answering is not a section that went away.
            raise _refusal(error) from None
        yield name, section


def _by_guid(section, guid, matched):
    """What ``section.getGuid(guid)`` returns, with the agent asked once per lookup.

    ``getGuid`` has the section's agent match the external id to a plex://
    guid on every call, a match that can leave the LAN, and then searches the
    section for that guid. The match depends on the agent and its language,
    not on the section, so ``matched`` keeps it for the rest of one lookup and
    each further section is only searched. A miss raises NotFound, as there.
    """
    key = (section.agent, getattr(section, 'language', None), guid)
    if key not in matched:
        sample = section.search(maxresults=1)
        if not sample:
            # An empty section holds nothing and has no item to match with;
            # the next section of this agent asks instead.
            raise NotFound(guid)
        found = sample[0].matches(agent=section.agent, title=guid.replace('://', '-'))
        matched[key] = found[0].guid if found else None
    items = section.search(guid=matched[key]) if matched[key] else None
    if not items:
        raise NotFound(guid)
    return items[0]


def _item_for(section, guid, media_type, metadata, matched=None):
    """The item one guid names in one section, down to the episode, or nothing.

    The refresh and the link lookup both come through here, so a guid finds
    the same item for either. ``getGuid`` has the Plex agent match an
    external id, which can leave the LAN; a lookup across several sections
    passes ``matched`` so each agent is asked once. Only a lookup miss is
    nothing; anything else is the server refusing, in the shared vocabulary.
    """
    try:
        item = section.getGuid(guid) if matched is None else _by_guid(section, guid, matched)
        if media_type == 'episode':
            item = item.episode(season=metadata.season, episode=metadata.episode)
    except NotFound:
        return None
    except Exception as error:
        raise _refusal(error) from None
    return item


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
        libraries = self.snapshot.libraries(media_type)
        if ensure_current:
            # The connection and the section listing happen before any rung
            # callback runs, so the guard runs first: a configuration change
            # must stop the refresh before the credential is sent anywhere.
            ensure_current()
        for name, section in _sections(self.server, libraries):
            if ensure_current:
                ensure_current()
            item = _item_for(section, f'imdb://{metadata.imdb_id}', media_type, metadata)
            if item is None:
                logger.debug('Plex section %r holds no item for this publication', name)
                continue
            try:
                item.refresh()
            except Exception as error:
                # The item exists, so a refused refresh is a failure, not a miss
                # that should fall through to rescanning the whole section.
                raise _refusal(error) from None
            if ensure_current:
                ensure_current()
            return {'status': 'requested'}
        return None

    def refresh_library(self, media_type, *, ensure_current=None, coalesce=None):
        """Update each configured section of this type, as the singleton did."""
        names = self.snapshot.libraries(media_type)
        if not names:
            return None
        if ensure_current:
            # The connection and the section listing happen before any section
            # update is asked for, so the guard runs first: a configuration
            # change must stop the refresh before the credential is sent.
            ensure_current()
        updated = False
        failure = None
        for name, section in _sections(self.server, names):
            if coalesce is not None and coalesce(name):
                updated = True
                continue
            if ensure_current:
                ensure_current()
            try:
                section.update()
            except Exception as error:
                logger.debug('Plex section %r refused a library update', name)
                # The rest are still asked, but the refresh is not reported as
                # requested while one section never was.
                failure = failure or _refusal(error)
                continue
            updated = True
        if ensure_current:
            ensure_current()
        if failure is not None:
            raise failure
        # Nothing was asked of Plex at all, so this rung did not answer and the
        # walk reports the publication as unresolved rather than as requested.
        return {'status': 'requested'} if updated else None

    def locate(self, media_type, metadata, video_path=None):
        """The Plex item a library row names, with nothing refreshed.

        Through the refresh's own lookup, and in its order: the IMDB guid in
        every configured section first, so an item the refresh finds is the
        item found here. Only then the TMDB or TVDB guid, which the refresh
        never asks and a link still can. A miss is never turned into a scan.
        """
        if media_type == 'sports':
            return self._locate_recording(metadata, video_path)
        if media_type not in ('movie', 'episode', 'series'):
            raise MediaServerError('internal_error')
        if not metadata.locatable(media_type):
            return None
        guids = [f'{provider}://{value}' for provider, value in metadata.provider_ids(media_type)]
        if not guids:
            return None
        sections = list(_sections(self.server, self.snapshot.libraries(media_type)))
        matched = {}
        for guid in guids:
            for _name, section in sections:
                item = _item_for(section, guid, media_type, metadata, matched)
                if item is not None:
                    return item
        return None

    def _locate_recording(self, metadata, video_path):
        """The one item titled like the event whose file has the recording's name.

        A recording has no identifiers, so the event's title narrows the
        sports sections and the file name decides, because Plex's path and
        Bazarr's can differ in everything but that. A tie is nothing.
        """
        name = file_name(video_path)
        if name is None or not metadata.title:
            return None
        matches = []
        for _name, section in _sections(self.server, self.snapshot.libraries('sports')):
            libtype = 'episode' if getattr(section, 'type', None) == 'show' else None
            try:
                found = section.search(title=metadata.title, libtype=libtype)
                matches += [item for item in found
                            if any(file_name(getattr(part, 'file', None)) == name for part in item.iterParts())]
            except NotFound:
                continue
            except Exception as error:
                raise _refusal(error) from None
        return matches[0] if len(matches) == 1 else None

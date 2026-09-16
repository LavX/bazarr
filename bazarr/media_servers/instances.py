# coding=utf-8
"""Immutable destination snapshots and strict request validation."""

import json
import re
from dataclasses import dataclass

from . import resolution
from .http import MediaServerError, validate_server_url
from .paths import validate_path_mappings

VALID_KINDS = ('emby', 'jellyfin', 'plex', 'silo')
FIELDS = {'name', 'enabled', 'url', 'verify_ssl', 'api_key', 'clear_api_key', 'path_mappings',
          'refresh_movies', 'refresh_episodes', 'options'}
PROBE_FIELDS = {'url', 'verify_ssl', 'api_key', 'clear_api_key'}
TOGGLES = ('refresh_movies', 'refresh_episodes')

# Every rung a kind's client can climb, declared here as well as on the client
# itself. Whether a destination needs path mappings is decided before any
# client exists - by request validation, and by the dispatcher when it decides
# whether a publication is even addressed to that destination - so the ladder
# cannot live only behind a network connection.
KIND_STEPS = {
    'emby': resolution.CHAIN,
    # Silo 10.12.0 returns items with no ProviderIds field at all, so neither
    # identity rung has anything to match on. Measured, not assumed.
    'silo': (resolution.PATH, resolution.LIBRARY),
    # Jellyfin resolves the item itself, so it never needed path mappings and
    # must not start needing them here.
    'jellyfin': (resolution.PROVIDER_ID, resolution.TITLE_YEAR, resolution.LIBRARY),
    # Plex matches on an IMDB guid and otherwise updates the whole section. It
    # has no title fallback today and is not being given one here.
    'plex': (resolution.PROVIDER_ID, resolution.LIBRARY),
}

# What is genuinely kind-specific, and the only keys each kind accepts. Lists
# of library handles: Jellyfin addresses libraries by item id, Plex by section
# name. Emby and Silo scope their libraries through path_mappings.
LIBRARY_KEYS = {
    'jellyfin': ('movie_library_ids', 'series_library_ids', 'sports_library_ids'),
    'plex': ('movie_libraries', 'series_libraries', 'sports_libraries'),
}
OPTION_KEYS = {
    'emby': (),
    'silo': (),
    'jellyfin': LIBRARY_KEYS['jellyfin'] + ('refresh_method',),
    'plex': LIBRARY_KEYS['plex'],
}
REFRESH_METHODS = ('immediate', 'async')


def needs_path_mappings(kind):
    return resolution.PATH in KIND_STEPS.get(kind, ())


def validate_options(kind, options):
    """The per-kind blob, accepted only as the closed set that kind declares."""
    if not isinstance(options, dict) or set(options) - set(OPTION_KEYS.get(kind, ())):
        raise MediaServerError('invalid_settings')
    for key in LIBRARY_KEYS.get(kind, ()):
        value = options.get(key, [])
        if (not isinstance(value, list)
                or any(not isinstance(item, str) or not item.strip()
                       or any(ord(char) < 32 for char in item) for item in value)):
            raise MediaServerError('library_invalid')
    if 'refresh_method' in options and options['refresh_method'] not in REFRESH_METHODS:
        raise MediaServerError('invalid_settings')
    return options


def parse_options(value):
    """Stored options, or nothing usable. Never a reason to lose a row."""
    try:
        options = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return options if isinstance(options, dict) else {}


@dataclass(frozen=True, repr=False)
class ConnectionSnapshot:
    id: str
    kind: str
    name: str
    instance_enabled: bool
    master_enabled: bool
    url: str
    apikey: str
    verify_ssl: bool
    path_mappings: tuple
    refresh_movies: bool = True
    refresh_episodes: bool = True
    options_json: str = '{}'
    revision: int = 1
    configuration_error: str | None = None

    @property
    def enabled(self):
        return self.instance_enabled and self.master_enabled

    def mappings(self):
        return [dict(row) for row in self.path_mappings]

    def options(self):
        return parse_options(self.options_json)

    def libraries(self, media_type):
        """The library handles this destination was told to scan, by type."""
        keys = LIBRARY_KEYS.get(self.kind)
        if keys is None:
            return []
        key = keys[0] if media_type == 'movie' else keys[2] if media_type == 'sports' else keys[1]
        values = self.options().get(key)
        # A stored blob that validation never saw is a destination with nothing
        # configured, not a crash on the refresh worker.
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str) and item.strip()]

    def refreshes(self, media_type):
        """Whether this destination was asked to refresh this kind of media.

        A sports publication has its own library configuration and no
        equivalent opt-in, so the movie and episode toggles do not speak for it.
        """
        if media_type == 'movie':
            return self.refresh_movies
        if media_type == 'episode':
            return self.refresh_episodes
        return True


def validate_fields(body, *, create=False, probe=False):
    allowed = PROBE_FIELDS if probe else FIELDS | ({'kind'} if create else set())
    if not isinstance(body, dict) or set(body) - allowed:
        raise MediaServerError('invalid_settings')
    if create and body.get('kind') not in VALID_KINDS:
        raise MediaServerError('invalid_kind')
    for name in ('name', 'url', 'api_key'):
        if name in body and not isinstance(body[name], str):
            raise MediaServerError('invalid_settings')
    for name in ('enabled', 'verify_ssl', 'clear_api_key', *TOGGLES):
        if name in body and type(body[name]) is not bool:
            raise MediaServerError('invalid_settings')
    if body.get('api_key') and body.get('clear_api_key'):
        raise MediaServerError('invalid_settings')
    if 'name' in body and (not body['name'].strip() or any(ord(c) < 32 for c in body['name'])):
        raise MediaServerError('invalid_name')
    if 'url' in body:
        validate_server_url(body['url'])
    if 'path_mappings' in body:
        validate_path_mappings(body['path_mappings'])
    if 'options' in body:
        if not create and 'kind' not in body:
            # The kind decides which keys are legal, and an update never carries
            # one. The repository re-validates against the stored kind.
            if not isinstance(body['options'], dict):
                raise MediaServerError('invalid_settings')
        else:
            validate_options(body.get('kind'), body['options'])
    return body


def validate_connection(values, *, probe=False):
    validate_server_url(values['url'])
    if not values['name'].strip():
        raise MediaServerError('invalid_name')
    mappings = validate_path_mappings(values['path_mappings'])
    validate_options(values['kind'], values.get('options') or {})
    if values['kind'] == 'silo':
        for row in mappings:
            value = row.get('library_id')
            if not isinstance(value, str) or re.fullmatch(r'[0-9]+', value) is None or not value.strip('0'):
                raise MediaServerError('library_invalid')
    if values['enabled'] or probe:
        if not values['api_key'].strip():
            raise MediaServerError('missing_credentials')
        if not mappings and not probe and needs_path_mappings(values['kind']):
            raise MediaServerError('mapping_missing')
    return values

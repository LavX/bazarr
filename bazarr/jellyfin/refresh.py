# coding=utf-8
"""Jellyfin on the shared resolution ladder, with the singleton's own matching.

The rungs here are the ones `jellyfin_refresh_item` already climbed: the
provider ids in Jellyfin's precedence, then an exact title and year, then the
configured libraries. Jellyfin resolves the item itself, so it declares no path
rung and needs no path mappings, which is what it has always done and the one
thing this must not change.
"""

import requests
from urllib3.exceptions import ReadTimeoutError

from media_servers import resolution
from media_servers.http import MediaServerError
from media_servers.libraries import _SERVER_WIDE

# Jellyfin refreshes a movie item, a series item or an episode item the same
# way. Which one an identity rung resolves is all that differs.
_PARENT_TYPES = {'movie': 'Movie', 'episode': 'Series'}
_PROVIDER_KEYS = {'imdb': 'Imdb', 'tmdb': 'Tmdb', 'tvdb': 'Tvdb'}


def translated(call, *args, **kwargs):
    """The singleton client's transport failures in the shared vocabulary.

    JellyfinClient predates the shared layer and raises requests exceptions, so
    without this every unreachable server and every rejected credential would
    reach the dispatcher as internal_error and the status card would say
    nothing useful about which of the two it was.
    """
    try:
        return call(*args, **kwargs)
    except MediaServerError:
        raise
    except requests.exceptions.SSLError:
        raise MediaServerError('tls_error') from None
    except requests.exceptions.Timeout:
        raise MediaServerError('timeout') from None
    except requests.exceptions.HTTPError as error:
        status = getattr(error.response, 'status_code', None)
        raise MediaServerError({401: 'unauthorized', 403: 'forbidden', 404: 'not_found'}.get(
            status, 'request_rejected')) from None
    except requests.exceptions.ConnectionError as error:
        code = 'timeout' if error.args and isinstance(error.args[0], ReadTimeoutError) else 'connection_error'
        raise MediaServerError(code) from None
    except requests.exceptions.RequestException:
        raise MediaServerError('connection_error') from None
    except ValueError:
        # The client raises this for an oversized body and an unsafe item id.
        raise MediaServerError('invalid_response') from None


class JellyfinRefreshClient:
    REFRESH_STEPS = (resolution.PROVIDER_ID, resolution.TITLE_YEAR, resolution.LIBRARY)

    def __init__(self, snapshot):
        from media_servers.http import validate_server_url
        from .client import JellyfinClient
        if not snapshot.apikey or not snapshot.apikey.strip():
            raise MediaServerError('missing_credentials')
        self.snapshot = snapshot
        self.client = JellyfinClient(validate_server_url(snapshot.url), snapshot.apikey,
                                     verify_ssl=snapshot.verify_ssl)
        self.immediate = snapshot.options().get('refresh_method', 'immediate') != 'async'

    def close(self):
        self.client.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _libraries(self, media_type):
        return self.snapshot.libraries(media_type)

    def _items(self, media_type, library_id, params):
        result = translated(self.client.get_items, {'parentId': library_id, 'recursive': 'true',
                                                    'includeItemTypes': _PARENT_TYPES[media_type],
                                                    'fields': 'Path,ProviderIds', **params})
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise MediaServerError('invalid_response')
        return result

    def _refresh_item(self, item, ensure_current):
        item_id, path = item.get('Id'), item.get('Path')
        if not isinstance(item_id, str) or not item_id:
            raise MediaServerError('invalid_response')
        if ensure_current:
            ensure_current()
        # The saved refresh_method, unchanged: an immediate item refresh, or a
        # fire-and-forget filesystem notification that Jellyfin batches.
        if self.immediate or not isinstance(path, str) or not path:
            translated(self.client.refresh_item, item_id)
        else:
            translated(self.client.report_media_updated, path)
        if ensure_current:
            ensure_current()
        return {'status': 'requested'}

    def _resolve(self, media_type, metadata, ensure_current, match):
        """The first library holding exactly one matching parent, or nothing."""
        for library_id in self._libraries(media_type):
            if ensure_current:
                ensure_current()
            params = {'years': str(metadata.year)} if metadata.year else {}
            matches = [item for item in self._items(media_type, library_id, params) if match(item)]
            if not matches:
                continue
            if len(matches) != 1:
                # Two items claiming the same identity is not a resolution, and
                # guessing one is how the wrong film gets refreshed.
                raise MediaServerError('item_ambiguous')
            parent = matches[0]
            if media_type == 'movie':
                return self._refresh_item(parent, ensure_current)
            return self._refresh_episode(parent, metadata, ensure_current)
        return None

    def _refresh_episode(self, series, metadata, ensure_current):
        if not self.immediate:
            # The singleton reports the series path for an async refresh rather
            # than looking the episode up, and Jellyfin rescans it from there.
            return self._refresh_item(series, ensure_current)
        series_id = series.get('Id')
        if not isinstance(series_id, str) or not series_id:
            raise MediaServerError('invalid_response')
        episodes = translated(self.client.get_episodes, series_id, metadata.season)
        if not isinstance(episodes, list):
            raise MediaServerError('invalid_response')
        matches = [item for item in episodes if isinstance(item, dict)
                   and item.get('IndexNumber') == metadata.episode]
        if not matches:
            return None
        if len(matches) != 1:
            raise MediaServerError('item_ambiguous')
        return self._refresh_item(matches[0], ensure_current)

    def refresh_by_provider_id(self, media_type, metadata, *, ensure_current=None):
        if media_type not in _PARENT_TYPES:
            raise MediaServerError('internal_error')
        if not metadata.locatable(media_type):
            return None
        for provider, value in metadata.provider_ids(media_type):
            key = _PROVIDER_KEYS[provider]
            result = self._resolve(media_type, metadata, ensure_current,
                                   lambda item, key=key, value=value:
                                   (item.get('ProviderIds') or {}).get(key) == value)
            if result is not None:
                return result
        return None

    def refresh_by_title_year(self, media_type, metadata, *, ensure_current=None):
        """An exact title and an exact year, the way the singleton matched.

        A title without a year is not asked at all. Titles repeat across
        remakes, and accepting whichever single film came back is how a 1922
        film gets refreshed for a 2024 one while the library rung below is
        never tried.
        """
        if media_type not in _PARENT_TYPES:
            raise MediaServerError('internal_error')
        if not metadata.title or not metadata.year or not metadata.locatable(media_type):
            return None
        title = metadata.title.lower()
        return self._resolve(media_type, metadata, ensure_current,
                             lambda item: isinstance(item.get('Name'), str)
                             and item['Name'].lower() == title)

    def refresh_library(self, media_type, *, ensure_current=None, coalesce=None):
        """Refresh each configured library of this type, as the singleton did.

        A Jellyfin library is an item, so refreshing it is the same call. The
        coalescer keeps one drain from asking the same library once per file.

        A library the server refuses, most often a saved id it no longer has,
        does not stop the ones after it, but the refresh is not reported as
        requested while one of them was not. A refusal about the server rather
        than the library would meet every library the same way, so it stops.
        """
        libraries = self._libraries(media_type)
        if not libraries:
            return None
        failure = None
        for library_id in libraries:
            if coalesce is not None and coalesce(library_id):
                continue
            if ensure_current:
                ensure_current()
            try:
                translated(self.client.refresh_item, library_id)
            except MediaServerError as error:
                if error.code in _SERVER_WIDE:
                    raise
                failure = failure or error
        if ensure_current:
            ensure_current()
        if failure is not None:
            raise failure
        return {'status': 'requested'}

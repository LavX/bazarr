# coding=utf-8

import logging

from app.config import settings
from .client import JellyfinClient, _redact_secret

logger = logging.getLogger(__name__)


def _redact(text) -> str:
    """Pull api_key from settings and scrub it (plus any Token="..." form)
    from text destined for logs or HTTP responses."""
    secret = ''
    try:
        candidate = settings.jellyfin.apikey
        if isinstance(candidate, str):
            secret = candidate
    except Exception:
        pass
    return _redact_secret(str(text), secret)


def get_jellyfin_client(url: str = None, apikey: str = None) -> JellyfinClient:
    """Create a JellyfinClient from settings or explicit parameters."""
    url = url or settings.jellyfin.url
    apikey = apikey or settings.jellyfin.apikey

    if not url:
        raise ValueError("Jellyfin URL not configured.")
    if not apikey:
        raise ValueError("Jellyfin API key not configured.")

    return JellyfinClient(url, apikey)


def jellyfin_test_connection(url: str = None, apikey: str = None) -> dict:
    """Test connectivity to a Jellyfin server. Returns server info or error.

    Error responses do NOT echo exception text back to the caller: a misbehaving
    Jellyfin can put server banners, URLs, or even pieces of the Authorization
    header into HTTP error messages, and we already strip those server-side via
    _redact() before logging. Surface a coarse `error_code` instead so the UI
    can render a friendly message without leaking diagnostics to the network."""
    try:
        client = get_jellyfin_client(url, apikey)
        info = client.get_system_info()
        return {
            'success': True,
            'server_name': info.get('ServerName', ''),
            'version': info.get('Version', ''),
        }
    except ValueError as e:
        # Configuration errors (missing url / apikey) are safe to surface —
        # they originate from our own code, not the server.
        logger.error(f"Failed to connect to Jellyfin server: {_redact(e)}")
        return {
            'success': False,
            'error_code': 'configuration',
        }
    except Exception as e:
        logger.error(f"Failed to connect to Jellyfin server: {_redact(e)}")
        return {
            'success': False,
            'error_code': 'connection_failed',
        }


def jellyfin_refresh_all_libraries() -> dict:
    """Trigger a refresh on every configured Jellyfin library, both movie
    and series. Used by the Maintenance "Refresh now" button so the user
    can verify their setup or recover from a Jellyfin restart without
    waiting for the next subtitle download.

    Returns a structured summary so the UI can render a toast like
    "Refreshed 4 of 5 libraries" without leaking exception text.
    """
    result = {
        'success': False,
        'movies_total': 0,
        'movies_refreshed': 0,
        'series_total': 0,
        'series_refreshed': 0,
    }
    try:
        client = get_jellyfin_client()
    except ValueError as e:
        logger.error(f"Jellyfin refresh-all aborted: {_redact(e)}")
        return {**result, 'error_code': 'configuration'}
    except Exception as e:
        logger.error(f"Jellyfin refresh-all aborted: {_redact(e)}")
        return {**result, 'error_code': 'connection_failed'}

    for is_movie in (True, False):
        ids = (settings.jellyfin.movie_library_ids if is_movie
               else settings.jellyfin.series_library_ids)
        if not isinstance(ids, list):
            ids = [ids] if ids else []
        ids = [i for i in ids if i]
        key_total = 'movies_total' if is_movie else 'series_total'
        key_done = 'movies_refreshed' if is_movie else 'series_refreshed'
        result[key_total] = len(ids)
        for library_id in ids:
            try:
                client.refresh_item(library_id)
                result[key_done] += 1
            except Exception as e:
                logger.error(
                    f"Failed to refresh Jellyfin library {library_id!r}: "
                    f"{_redact(e)}"
                )

    refreshed = result['movies_refreshed'] + result['series_refreshed']
    total = result['movies_total'] + result['series_total']
    result['success'] = total > 0 and refreshed == total
    if total == 0:
        result['error_code'] = 'no_libraries_configured'
    return result


def jellyfin_get_libraries(url: str = None, apikey: str = None) -> list:
    """Get movie and series libraries from the configured Jellyfin server."""
    try:
        client = get_jellyfin_client(url, apikey)
        libraries = client.get_libraries()

        logger.debug(f"Jellyfin returned {len(libraries)} library folders: "
                     f"{[(lib.get('Name'), lib.get('CollectionType')) for lib in libraries]}")

        return [
            {
                'id': lib.get('ItemId', ''),
                'name': lib['Name'],
                'type': (lib.get('CollectionType') or '').lower(),
            }
            for lib in libraries
            if (lib.get('CollectionType') or '').lower() in ('movies', 'tvshows')
        ]
    except Exception as e:
        logger.error(f"Failed to get Jellyfin libraries: {_redact(e)}")
        return []


def _find_item(client: JellyfinClient, is_movie: bool, library_ids: list,
               imdb_id: str = None, tmdb_id: str = None, tvdb_id: int = None,
               title: str = None, year: int = None) -> dict | None:
    """Find a Jellyfin item by provider IDs, with title fallback.

    Searches by year to narrow results, matches by provider IDs first,
    falls back to case-insensitive title match.
    Returns the full item dict (with Path, Id, ProviderIds) or None.
    """
    item_type = 'Movie' if is_movie else 'Series'

    for library_id in library_ids:
        if not library_id:
            continue

        try:
            params = {
                'parentId': library_id,
                'recursive': 'true',
                'includeItemTypes': item_type,
                'fields': 'Path,ProviderIds',
            }
            if year:
                params['years'] = str(year)

            items = client.get_items(params)

            # Single pass: check IDs first, remember title match as fallback
            title_match = None
            for item in items:
                provider_ids = item.get('ProviderIds', {})
                if imdb_id and provider_ids.get('Imdb') == imdb_id:
                    return item
                if tmdb_id and provider_ids.get('Tmdb') == str(tmdb_id):
                    return item
                if tvdb_id and provider_ids.get('Tvdb') == str(tvdb_id):
                    return item
                if title and not title_match and item.get('Name', '').lower() == title.lower():
                    title_match = item

            if title_match:
                return title_match
        except Exception as e:
            logger.debug(f"Error searching library {library_id}: {_redact(e)}")
            continue

    return None


def _find_episode(client: JellyfinClient, series_id: str, season: int,
                  episode: int) -> str | None:
    """Find a specific episode within a series by season and episode number."""
    try:
        episodes = client.get_episodes(series_id, season)
        for ep in episodes:
            if ep.get('IndexNumber') == episode:
                return ep['Id']
    except Exception as e:
        logger.debug(f"Error finding episode S{season:02d}E{episode:02d} in series {series_id}: {_redact(e)}")

    return None


def jellyfin_refresh_item(imdb_id: str = None, is_movie: bool = True, season: int = None,
                          episode: int = None, tmdb_id: str = None, tvdb_id: int = None,
                          title: str = None, year: int = None) -> None:
    """
    Refresh a specific item in Jellyfin after subtitle changes.

    1. Find item by provider IDs (IMDB, TMDB, TVDB) with title fallback
    2. Immediate: POST /Items/{id}/Refresh (direct, no external API calls)
       Async: POST /Library/Media/Updated with file path (batched ~30-60s)
    3. Fall back to full library update if item not found
    """
    try:
        client = get_jellyfin_client()
        library_ids = (settings.jellyfin.movie_library_ids if is_movie
                       else settings.jellyfin.series_library_ids)

        if not isinstance(library_ids, list):
            library_ids = [library_ids] if library_ids else []

        if not library_ids:
            library_type = "movie" if is_movie else "series"
            logger.debug(f"No {library_type} libraries configured in Jellyfin settings")
            return

        if not (imdb_id or tmdb_id or tvdb_id or title):
            logger.warning("No IDs or title provided for Jellyfin refresh")
            jellyfin_update_library(client, is_movie, library_ids)
            return

        immediate = settings.jellyfin.get('refresh_method', 'immediate') == 'immediate'

        if is_movie:
            item = _find_item(client, is_movie=True, library_ids=library_ids,
                              imdb_id=imdb_id, tmdb_id=tmdb_id, title=title, year=year)
            if item:
                _refresh_or_report(client, immediate, item_id=item['Id'], path=item.get('Path'))
                logger.info(f"Refreshed movie in Jellyfin: {item.get('Path', item['Id'])}")
                return
        else:
            series = _find_item(client, is_movie=False, library_ids=library_ids,
                                imdb_id=imdb_id, tvdb_id=tvdb_id, title=title, year=year)
            if series and season is not None and episode is not None:
                if immediate:
                    episode_id = _find_episode(client, series['Id'], season, episode)
                    if episode_id:
                        client.refresh_item(episode_id)
                        logger.info(f"Refreshed episode in Jellyfin (immediate): "
                                    f"S{season:02d}E{episode:02d}")
                        return
                else:
                    _refresh_or_report(client, immediate, path=series.get('Path'))
                    logger.info(f"Reported series update to Jellyfin (async): "
                                f"{series.get('Path')} S{season:02d}E{episode:02d}")
                    return

        # Fallback: full library update
        logger.warning(f"Item not found in Jellyfin (IMDB: {imdb_id}, TMDB: {tmdb_id}, TVDB: {tvdb_id}), "
                       f"falling back to library update")
        jellyfin_update_library(client, is_movie, library_ids)

    except Exception as e:
        logger.warning(f"Failed to refresh Jellyfin item, falling back to library update: {_redact(e)}")
        try:
            jellyfin_update_library(get_jellyfin_client(), is_movie)
        except Exception:
            logger.error("Failed to refresh Jellyfin library as fallback")


def _refresh_or_report(client: JellyfinClient, immediate: bool,
                       item_id: str = None, path: str = None) -> None:
    """Execute the appropriate refresh method based on user setting."""
    if immediate and item_id:
        client.refresh_item(item_id)
    elif path:
        client.report_media_updated(path)
    elif item_id:
        client.refresh_item(item_id)


def jellyfin_update_library(client: JellyfinClient = None, is_movie_library: bool = True,
                            library_ids: list = None) -> None:
    """
    Trigger a library refresh for configured libraries of the given type.
    Uses POST /Library/Media/Updated with library paths for a directory rescan.
    """
    try:
        if client is None:
            client = get_jellyfin_client()

        if library_ids is None:
            library_ids = (settings.jellyfin.movie_library_ids if is_movie_library
                           else settings.jellyfin.series_library_ids)
            if not isinstance(library_ids, list):
                library_ids = [library_ids] if library_ids else []

        if not library_ids:
            library_type = "movie" if is_movie_library else "series"
            logger.debug(f"No {library_type} libraries configured in Jellyfin settings")
            return

        updated_count = 0
        for library_id in library_ids:
            if not library_id:
                continue

            try:
                client.refresh_item(library_id)
                logger.info(f"Triggered refresh for Jellyfin library: {library_id}")
                updated_count += 1
            except Exception as e:
                logger.error(f"Failed to refresh Jellyfin library '{library_id}': {_redact(e)}")
                continue

        if updated_count > 0:
            logger.debug(f"Successfully triggered refresh for {updated_count} Jellyfin libraries")
        else:
            logger.warning("Failed to refresh any Jellyfin libraries")

    except Exception as e:
        logger.error(f"Error in jellyfin_update_library: {_redact(e)}")

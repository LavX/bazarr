# coding=utf-8
import logging
import threading
from collections import OrderedDict
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import settings, write_config  # noqa: F401
from plexapi.server import PlexServer

logger = logging.getLogger(__name__)
DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'

# Cache PlexServer instances (and their underlying pooled requests.Session)
# keyed by (baseurl, token, verify). PlexServer is expensive to construct:
# its initialiser performs a /system/status round trip and warms an internal
# metadata cache. plex_set_movie_added_date_now / plex_refresh_item / etc.
# are called once per item processed, so without this cache we were paying
# a fresh TCP+TLS handshake AND a fresh PlexServer init for every item.
#
# The cache is bounded (FIFO eviction) so a credential rotation in the
# settings UI cannot leak stale PlexServer objects forever.
_PLEX_CACHE_MAX_ENTRIES = 4
_plex_cache: "OrderedDict[tuple, PlexServer]" = OrderedDict()
_plex_cache_lock = threading.Lock()


def _build_pooled_session(verify: bool) -> requests.Session:
    """Build a requests.Session backed by an HTTPAdapter that actually pools
    connections, so subsequent calls to the same Plex server reuse the
    existing TCP+TLS connection.

    Retries are limited to transient gateway statuses (502/503/504) so 4xx
    responses still surface immediately to the caller, matching the
    pre-existing behaviour of the rest of the code base."""
    s = requests.Session()
    s.verify = verify
    adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        max_retries=Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=(502, 503, 504),
        ),
    )
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s


def get_plex_server() -> PlexServer:
    """Connect to the Plex server and return the server instance.

    Credentials live encrypted at rest via secret_store and are decrypted
    into the live settings object at boot, so this function reads
    plain-text apikey / token directly from settings - no per-call
    decryption ceremony, no auto-encrypt branch, no encryption_key /
    apikey_encrypted bookkeeping.

    The constructed PlexServer (and its pooled Session) is cached by
    (baseurl, token, verify) so a sync that touches many items does not
    rebuild the server connection for each one. The cache is FIFO-bounded
    so a settings rotation evicts old entries.
    """
    try:
        auth_method = settings.plex.get('auth_method', 'apikey')

        if auth_method == 'oauth':
            token = settings.plex.get('token')
            if not token:
                raise ValueError("OAuth token not found. Please re-authenticate with Plex.")

            baseurl = settings.plex.get('server_url')
            if not baseurl:
                raise ValueError("Server URL not configured. Please select a Plex server.")
        else:
            protocol = "https://" if settings.plex.ssl else "http://"
            baseurl = f"{protocol}{settings.plex.ip}:{settings.plex.port}"

            token = settings.plex.get('apikey')
            if not token:
                raise ValueError("API key not configured. Please configure Plex authentication.")

        # Verify is False here for compatibility with the prior behaviour:
        # the original code unconditionally set ``session.verify = False``.
        # If TLS verification ever becomes user-configurable for Plex, plumb
        # that flag through to the cache key as well.
        verify = False
        cache_key = (baseurl, token, verify)

        with _plex_cache_lock:
            cached = _plex_cache.get(cache_key)
            if cached is not None:
                # Move-to-end to keep the most-recently-used at the tail
                # so eviction continues to drop the oldest entry.
                _plex_cache.move_to_end(cache_key)
                return cached

            session = _build_pooled_session(verify=verify)
            plex_server = PlexServer(baseurl, token, session=session)

            _plex_cache[cache_key] = plex_server
            while len(_plex_cache) > _PLEX_CACHE_MAX_ENTRIES:
                _plex_cache.popitem(last=False)

            return plex_server

    except Exception as e:
        logger.error(f"Failed to connect to Plex server: {e}")  # noqa: G004
        raise


def update_added_date(video, added_date: str) -> None:
    """Update the added date of a video in Plex."""
    try:
        updates = {"addedAt.value": added_date}
        video.edit(**updates)
        logger.info(f"Updated added date for {video.title} to {added_date}")  # noqa: G004
    except Exception as e:
        logger.error(f"Failed to update added date for {video.title}: {e}")  # noqa: G004
        raise


def plex_set_movie_added_date_now(movie_metadata) -> None:
    """
    Update the added date of a movie in Plex to the current datetime.
    Searches across all configured movie libraries.

    :param movie_metadata: Metadata object containing the movie's IMDb ID.
    """
    try:
        plex = get_plex_server()
        movie_libraries = settings.plex.movie_library
        
        # Ensure we have a list
        if not isinstance(movie_libraries, list):
            movie_libraries = [movie_libraries] if movie_libraries else []
        
        if not movie_libraries:
            logger.debug("No movie libraries configured in Plex settings")
            return
        
        # Search through all configured movie libraries
        for library_name in movie_libraries:
            if not library_name:  # Skip empty strings
                continue
                
            try:
                library = plex.library.section(library_name)
                video = library.getGuid(guid=movie_metadata.imdbId)
                update_added_date(video, datetime.now().strftime(DATETIME_FORMAT))
                logger.debug(f"Updated added date for movie in library '{library_name}'")  # noqa: G004
                return  # Success - no need to check other libraries
            except Exception as lib_error:
                # Movie not found in this library, try next one
                logger.debug(f"Movie not found in library '{library_name}': {lib_error}")  # noqa: G004
                continue
        
        # If we get here, movie wasn't found in any library
        logger.warning(f"Movie with IMDB ID {movie_metadata.imdbId} not found in any configured Plex movie library")  # noqa: G004
        
    except Exception as e:
        logger.error(f"Error in plex_set_movie_added_date_now: {e}")  # noqa: G004


def plex_set_episode_added_date_now(episode_metadata) -> None:
    """
    Update the added date of a TV episode in Plex to the current datetime.
    Searches across all configured series libraries.

    :param episode_metadata: Metadata object containing the episode's IMDb ID, season, and episode number.
    """
    try:
        plex = get_plex_server()
        series_libraries = settings.plex.series_library
        
        # Ensure we have a list
        if not isinstance(series_libraries, list):
            series_libraries = [series_libraries] if series_libraries else []
        
        if not series_libraries:
            logger.debug("No series libraries configured in Plex settings")
            return
        
        # Search through all configured series libraries
        for library_name in series_libraries:
            if not library_name:  # Skip empty strings
                continue
                
            try:
                library = plex.library.section(library_name)
                show = library.getGuid(episode_metadata.imdbId)
                episode = show.episode(season=episode_metadata.season, episode=episode_metadata.episode)
                update_added_date(episode, datetime.now().strftime(DATETIME_FORMAT))
                logger.debug(f"Updated added date for episode in library '{library_name}'")  # noqa: G004
                return  # Success - no need to check other libraries
            except Exception as lib_error:
                # Show not found in this library, try next one
                logger.debug(f"Show not found in library '{library_name}': {lib_error}")  # noqa: G004
                continue
        
        # If we get here, show wasn't found in any library
        logger.warning(f"Show with IMDB ID {episode_metadata.imdbId} not found in any configured Plex series library")  # noqa: G004
        
    except Exception as e:
        logger.error(f"Error in plex_set_episode_added_date_now: {e}")  # noqa: G004


def plex_update_library(is_movie_library: bool) -> None:
    """
    Trigger a library update for the specified library type.
    Updates all configured libraries of the given type.

    :param is_movie_library: True for movie library, False for series library.
    """
    try:
        plex = get_plex_server()
        library_names = settings.plex.movie_library if is_movie_library else settings.plex.series_library
        
        # Ensure we have a list
        if not isinstance(library_names, list):
            library_names = [library_names] if library_names else []
        
        if not library_names:
            library_type = "movie" if is_movie_library else "series"
            logger.debug(f"No {library_type} libraries configured in Plex settings")  # noqa: G004
            return
        
        # Update all configured libraries
        updated_count = 0
        for library_name in library_names:
            if not library_name:  # Skip empty strings
                continue
                
            try:
                library = plex.library.section(library_name)
                library.update()
                logger.info(f"Triggered update for library: {library_name}")  # noqa: G004
                updated_count += 1
            except Exception as lib_error:
                logger.error(f"Failed to update library '{library_name}': {lib_error}")  # noqa: G004
                continue
        
        if updated_count > 0:
            logger.debug(f"Successfully triggered update for {updated_count} libraries")  # noqa: G004
        else:
            logger.warning("Failed to update any Plex libraries")
            
    except Exception as e:
        logger.error(f"Error in plex_update_library: {e}")  # noqa: G004


def plex_refresh_item(imdb_id: str, is_movie: bool, season: int = None, episode: int = None) -> None:
    """
    Refresh a specific item in Plex instead of scanning the entire library.
    This is much more efficient than a full library scan when subtitles are added.
    Searches across all configured libraries of the appropriate type.

    :param imdb_id: IMDB ID of the content
    :param is_movie: True for movie, False for TV episode
    :param season: Season number for TV episodes
    :param episode: Episode number for TV episodes
    """
    try:
        plex = get_plex_server()
        library_names = settings.plex.movie_library if is_movie else settings.plex.series_library
        
        # Ensure we have a list
        if not isinstance(library_names, list):
            library_names = [library_names] if library_names else []
        
        if not library_names:
            library_type = "movie" if is_movie else "series"
            logger.debug(f"No {library_type} libraries configured in Plex settings")  # noqa: G004
            return
        
        # Search through all configured libraries
        for library_name in library_names:
            if not library_name:  # Skip empty strings
                continue
                
            try:
                library = plex.library.section(library_name)
                
                if is_movie:
                    # Refresh specific movie
                    item = library.getGuid(f"imdb://{imdb_id}")
                    item.refresh()
                    logger.info(f"Refreshed movie in '{library_name}': {item.title} (IMDB: {imdb_id})")  # noqa: G004
                    return  # Success - no need to check other libraries
                else:
                    # Refresh specific episode
                    show = library.getGuid(f"imdb://{imdb_id}")
                    episode_item = show.episode(season=season, episode=episode)
                    episode_item.refresh()
                    logger.info(f"Refreshed episode in '{library_name}': {show.title} S{season:02d}E{episode:02d} (IMDB: {imdb_id})")  # noqa: G004
                    return  # Success - no need to check other libraries
                    
            except Exception as lib_error:
                # Item not found in this library, try next one
                logger.debug(f"Item not found in library '{library_name}': {lib_error}")  # noqa: G004
                continue
        
        # If we get here, item wasn't found in any library - fall back to full update
        logger.warning(f"Item (IMDB: {imdb_id}) not found in any configured library, falling back to library update")  # noqa: G004
        plex_update_library(is_movie)
            
    except Exception as e:
        logger.warning(f"Failed to refresh specific item (IMDB: {imdb_id}), falling back to library update: {e}")  # noqa: G004
        # Fallback to full library update if specific refresh fails
        plex_update_library(is_movie)

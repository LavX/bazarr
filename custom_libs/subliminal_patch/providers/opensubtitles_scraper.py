# coding=utf-8
"""
OpenSubtitles Web Scraper Implementation

This module provides HTTP-based communication with OpenSubtitles scraper services,
bypassing the need for traditional API authentication.
"""

import base64
import logging
import requests
import json
from subzero.language import Language
from subliminal.exceptions import ServiceUnavailable, ConfigurationError
from subliminal_patch.exceptions import APIThrottled

logger = logging.getLogger(__name__)


class OpenSubtitlesScraperMixin:
    """
    Mixin class that provides web scraper functionality for OpenSubtitles provider.
    This allows communication with scraper services that provide OpenSubtitles-compatible APIs.
    """
    
    def _query_scraper(self, video, languages, hash=None, size=None, imdb_id=None, query=None, season=None, 
                      episode=None, tag=None, use_tag_search=False, only_foreign=False, also_foreign=False):
        """
        Query the scraper service for subtitles.
        
        This method converts the traditional OpenSubtitles XML-RPC format to HTTP requests
        that are compatible with scraper services.
        """
        logger.info('Querying scraper service at %s', self.scraper_service_url)
        
        # Build search criteria similar to the API version
        criteria = []
        if hash and size:
            criteria.append({'moviehash': hash, 'moviebytesize': str(size)})
        if use_tag_search and tag:
            criteria.append({'tag': tag})
        if imdb_id:
            if season and episode:
                criteria.append({'imdbid': imdb_id[2:], 'season': season, 'episode': episode})
            else:
                criteria.append({'imdbid': imdb_id[2:]})
                
        if not criteria:
            raise ValueError('Not enough information')

        # Add language information
        for criterion in criteria:
            criterion['sublanguageid'] = ','.join(sorted(l.opensubtitles for l in languages))

        try:
            # Make HTTP request to scraper service
            response = self._make_scraper_request('/search', {
                'criteria': criteria,
                'only_foreign': only_foreign,
                'also_foreign': also_foreign
            })
            
            if not response or not response.get('data'):
                logger.info('No subtitles found from scraper service')
                return []
                
            return self._parse_scraper_response(response['data'], languages, only_foreign, also_foreign, video)
            
        except APIThrottled:
            raise
        except requests.RequestException as e:
            logger.error('Scraper service request failed: %s', e)
            raise ServiceUnavailable(f'Scraper service unavailable: {e}')
        except Exception as e:
            logger.error('Unexpected error querying scraper service: %s', e)
            raise ServiceUnavailable(f'Scraper service error: {e}')

    def _make_scraper_request(self, endpoint, data):
        """
        Make an HTTP request to the scraper service.
        
        Args:
            endpoint: API endpoint (e.g., '/search', '/download')
            data: Request payload
            
        Returns:
            dict: JSON response from scraper service
        """
        # Ensure URL has proper protocol scheme
        base_url = self.scraper_service_url.rstrip('/')
        if not base_url.startswith(('http://', 'https://')):
            base_url = f'http://{base_url}'
            
        url = f"{base_url}{endpoint}"
        
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Bazarr-OpenSubtitles-Scraper/1.0'
        }
        
        logger.debug('Making scraper request to %s with data: %s', url, data)
        
        response = requests.post(
            url,
            json=data,
            headers=headers,
            timeout=120  # Increased timeout for scraper service (IMDB lookups + page navigation)
        )

        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After")
            response.close()
            message = "Scraper service busy"
            if retry_after:
                message = f"{message}, retry after {retry_after}s"
            raise APIThrottled(message)

        response.raise_for_status()
        return response.json()

    def _parse_scraper_response(self, data, languages, only_foreign, also_foreign, video):
        """
        Parse the scraper service response and create subtitle objects.
        
        Args:
            data: Response data from scraper service
            languages: Requested languages
            only_foreign: Only foreign/forced subtitles
            also_foreign: Include foreign/forced subtitles
            video: Video object for matching
            
        Returns:
            list: List of OpenSubtitlesSubtitle objects
        """
        subtitles = []
        
        for subtitle_item in data:
            try:
                # Parse subtitle item (similar to API version)
                language = Language.fromopensubtitles(subtitle_item['SubLanguageID'])
                hearing_impaired = bool(int(subtitle_item.get('SubHearingImpaired', 0)))
                page_link = subtitle_item.get('SubtitlesLink', '')
                subtitle_id = int(subtitle_item['IDSubtitleFile'])
                matched_by = subtitle_item.get('MatchedBy', 'hash')
                movie_kind = subtitle_item.get('MovieKind', 'movie')
                hash = subtitle_item.get('MovieHash', '')
                movie_name = subtitle_item.get('MovieName', '')
                movie_release_name = subtitle_item.get('MovieReleaseName', '')
                movie_year = int(subtitle_item['MovieYear']) if subtitle_item.get('MovieYear') else None
                
                # Handle IMDB ID
                if subtitle_item.get('SeriesIMDBParent'):
                    movie_imdb_id = 'tt' + subtitle_item['SeriesIMDBParent']
                elif subtitle_item.get('IDMovieImdb'):
                    movie_imdb_id = 'tt' + subtitle_item['IDMovieImdb']
                else:
                    movie_imdb_id = None
                    
                movie_fps = subtitle_item.get('MovieFPS')
                series_season = int(subtitle_item['SeriesSeason']) if subtitle_item.get('SeriesSeason') else None
                series_episode = int(subtitle_item['SeriesEpisode']) if subtitle_item.get('SeriesEpisode') else None
                filename = subtitle_item.get('SubFileName', '')
                encoding = subtitle_item.get('SubEncoding')
                foreign_parts_only = bool(int(subtitle_item.get('SubForeignPartsOnly', 0)))

                # Apply foreign/forced filtering
                if only_foreign and not foreign_parts_only:
                    continue
                elif not only_foreign and not also_foreign and foreign_parts_only:
                    continue
                elif (also_foreign or only_foreign) and foreign_parts_only:
                    language = Language.rebuild(language, forced=True)

                # Set hearing impaired language
                if hearing_impaired:
                    language = Language.rebuild(language, hi=True)

                if language not in languages:
                    continue

                # IMDB ID matching
                if video.imdb_id and movie_imdb_id and (movie_imdb_id != video.imdb_id):
                    continue

                query_parameters = subtitle_item.get("QueryParameters", {})

                # Create subtitle object
                subtitle = self.subtitle_class(
                    language, hearing_impaired, page_link, subtitle_id, matched_by,
                    movie_kind, hash, movie_name, movie_release_name, movie_year, movie_imdb_id,
                    series_season, series_episode, query_parameters, filename, encoding,
                    movie_fps, skip_wrong_fps=self.skip_wrong_fps
                )
                
                # Set additional attributes needed for matching
                subtitle.uploader = subtitle_item.get('UserNickName', 'anonymous')
                
                # For TV series, set movie_name which is used as series_name in matching
                # The series_name property uses movie_name internally
                if movie_kind == 'episode':
                    if not movie_name and movie_release_name:
                        # Extract series name from release name if movie_name is empty
                        # e.g., "The Exchange" Bank of Tomorrow -> "The Exchange"
                        import re
                        series_match = re.match(r'^"([^"]+)"', movie_release_name)
                        if series_match:
                            subtitle.movie_name = series_match.group(1)
                        else:
                            # Fallback: use first part of release name
                            subtitle.movie_name = movie_release_name.split()[0] if movie_release_name else 'Unknown'
                    # movie_name is already set correctly from the API response
                
                logger.debug('Found subtitle %r by %s via scraper', subtitle, matched_by)
                subtitles.append(subtitle)
                
            except (KeyError, ValueError, TypeError) as e:
                logger.warning('Failed to parse subtitle item from scraper: %s', e)
                continue

        return subtitles

    def _download_subtitle_scraper(self, subtitle):
        """
        Download subtitle content from scraper service.
        
        Args:
            subtitle: OpenSubtitlesSubtitle object
        """
        logger.info('Downloading subtitle %r via scraper', subtitle)
        
        try:
            response = self._make_scraper_request('/download', {
                'subtitle_id': str(subtitle.subtitle_id)
            })
            
            if response and response.get('data'):
                # The scraper returns base64-encoded subtitle content
                subtitle.content = base64.b64decode(response['data'])
            else:
                raise ServiceUnavailable('No subtitle content received from scraper')
                
        except APIThrottled:
            raise
        except requests.RequestException as e:
            logger.error('Failed to download subtitle from scraper: %s', e)
            raise ServiceUnavailable(f'Scraper download failed: {e}')

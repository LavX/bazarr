# coding=utf-8
"""
OpenSubtitles Web Scraper Integration (v1 API)

Uses the scraper service's REST API directly instead of the legacy
compatibility endpoint, passing all available metadata for best results.
"""

import re
import base64
import logging
import requests
from subliminal.video import Episode
from subzero.language import Language
from subliminal.exceptions import ServiceUnavailable
from subliminal_patch.exceptions import APIThrottled

logger = logging.getLogger(__name__)

# Map opensubtitles 3-letter codes to 2-letter codes for the scraper
_LANG_3_TO_2 = {
    'eng': 'en', 'hun': 'hu', 'spa': 'es', 'fre': 'fr', 'ger': 'de',
    'ita': 'it', 'por': 'pt', 'rus': 'ru', 'chi': 'zh', 'jpn': 'ja',
    'kor': 'ko', 'ara': 'ar', 'dut': 'nl', 'pol': 'pl', 'tur': 'tr',
    'swe': 'sv', 'nor': 'no', 'dan': 'da', 'fin': 'fi', 'cze': 'cs',
    'rum': 'ro', 'hrv': 'hr', 'srp': 'sr', 'bul': 'bg', 'gre': 'el',
    'heb': 'he', 'tha': 'th', 'vie': 'vi', 'ind': 'id', 'may': 'ms',
    'per': 'fa', 'ukr': 'uk', 'est': 'et', 'lav': 'lv', 'lit': 'lt',
    'slv': 'sl', 'slo': 'sk', 'ice': 'is', 'cat': 'ca', 'bos': 'bs',
    'glg': 'gl', 'baq': 'eu', 'geo': 'ka', 'mac': 'mk', 'alb': 'sq',
}


class OpenSubtitlesScraperMixin:
    """
    Mixin class providing web scraper functionality via the v1 REST API.
    """

    def _get_scraper_base_url(self):
        base_url = self.scraper_service_url.rstrip('/')
        if not base_url.startswith(('http://', 'https://')):
            base_url = f'http://{base_url}'
        return base_url

    def _scraper_request(self, endpoint, data):
        """Make a POST request to the scraper service."""
        url = f"{self._get_scraper_base_url()}{endpoint}"
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Bazarr-OpenSubtitles-Scraper/2.0'
        }

        logger.debug('Scraper request: %s %s', url, data)

        response = requests.post(url, json=data, headers=headers, timeout=120)

        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After")
            response.close()
            msg = "Scraper service busy"
            if retry_after:
                msg = f"{msg}, retry after {retry_after}s"
            raise APIThrottled(msg)

        response.raise_for_status()
        result = response.json()
        response.close()
        return result

    def _query_scraper(self, video, languages, hash=None, size=None, imdb_id=None, query=None,
                       season=None, episode=None, tag=None, use_tag_search=False,
                       only_foreign=False, also_foreign=False):
        """Query the scraper v1 API with full metadata."""
        logger.info('Querying scraper v1 API at %s', self.scraper_service_url)

        is_episode = isinstance(video, Episode)

        # Build the best possible query string from available info
        search_query = ''
        if query and isinstance(query, list) and query[0]:
            search_query = query[0]
        elif query and isinstance(query, str):
            search_query = query
        elif hasattr(video, 'series') and video.series:
            search_query = video.series
        elif hasattr(video, 'title') and video.title:
            search_query = video.title

        # Get year from video
        year = getattr(video, 'year', None)

        try:
            # Step 1: Search for the movie/show
            search_endpoint = '/api/v1/search/tv' if is_episode else '/api/v1/search/movies'
            search_data = {
                'query': search_query,
                'imdb_id': imdb_id if imdb_id else None,
                'year': year,
                'kind': 'episode' if is_episode else 'movie',
            }

            logger.info('Scraper search: %s query=%r imdb=%s year=%s',
                         search_endpoint, search_query, imdb_id, year)

            search_response = self._scraper_request(search_endpoint, search_data)

            results = search_response.get('results', [])
            if not results:
                # Fallback: try generic search endpoint
                search_response = self._scraper_request('/api/v1/search', search_data)
                results = search_response.get('results', [])

            if not results:
                logger.info('Scraper: no search results for %r', search_query)
                return []

            # Step 2: Select best matching result
            best_result = self._select_best_result(results, imdb_id, search_query, year)
            if not best_result:
                logger.info('Scraper: no matching result for imdb=%s query=%r', imdb_id, search_query)
                return []

            movie_url = best_result.get('url')
            if not movie_url:
                logger.warning('Scraper: search result has no URL')
                return []

            logger.info('Scraper: selected result: %s (%s) - %s',
                         best_result.get('title'), best_result.get('year'), movie_url)

            # Step 3: Get subtitle listings
            lang_codes = []
            for lang in languages:
                code_3 = lang.opensubtitles
                code_2 = _LANG_3_TO_2.get(code_3, code_3)
                lang_codes.append(code_2)

            subs_response = self._scraper_request('/api/v1/subtitles', {
                'movie_url': movie_url,
                'languages': lang_codes,
            })

            subtitle_list = subs_response.get('subtitles', [])
            if not subtitle_list:
                logger.info('Scraper: no subtitles found at %s', movie_url)
                return []

            logger.info('Scraper: found %d subtitles, parsing...', len(subtitle_list))

            # Step 4: Convert to bazarr subtitle objects
            return self._parse_v1_subtitles(
                subtitle_list, best_result, languages, season, episode,
                only_foreign, also_foreign, video, imdb_id
            )

        except APIThrottled:
            raise
        except requests.RequestException as e:
            logger.error('Scraper service request failed: %s', e)
            raise ServiceUnavailable(f'Scraper service unavailable: {e}')
        except Exception as e:
            logger.error('Unexpected error querying scraper: %s', e)
            raise ServiceUnavailable(f'Scraper service error: {e}')

    def _select_best_result(self, results, imdb_id, query, year):
        """Select the best search result by matching IMDB ID, title, and year."""
        if not results:
            return None

        # Exact IMDB match is best
        if imdb_id:
            for r in results:
                if r.get('imdb_id') == imdb_id:
                    return r

        # Score remaining results
        scored = []
        query_lower = (query or '').lower().strip()
        for r in results:
            score = 0
            title_lower = (r.get('title') or '').lower().strip()

            # Title matching
            if query_lower and title_lower:
                if title_lower == query_lower:
                    score += 10
                elif query_lower in title_lower or title_lower in query_lower:
                    score += 5

            # Year matching
            if year and r.get('year') == year:
                score += 3

            # Prefer results with more subtitles
            score += min(r.get('subtitle_count', 0) / 100, 2)

            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else results[0]

    def _parse_v1_subtitles(self, subtitle_list, search_result, languages, season, episode,
                            only_foreign, also_foreign, video, imdb_id):
        """Convert v1 API subtitle objects to bazarr OpenSubtitlesSubtitle objects."""
        subtitles = []
        series_title = search_result.get('title', '')
        result_year = search_result.get('year')
        result_imdb = search_result.get('imdb_id', '')

        for sub in subtitle_list:
            try:
                # Map 2-letter language back to opensubtitles format
                lang_2 = sub.get('language', '')
                try:
                    language = Language.fromietf(lang_2)
                except Exception:
                    try:
                        language = Language(lang_2)
                    except Exception:
                        logger.debug('Skipping subtitle with unknown language: %s', lang_2)
                        continue

                hearing_impaired = sub.get('hearing_impaired', False)
                foreign_parts_only = sub.get('forced', False)

                # Apply foreign/forced filtering
                if only_foreign and not foreign_parts_only:
                    continue
                elif not only_foreign and not also_foreign and foreign_parts_only:
                    continue
                elif (also_foreign or only_foreign) and foreign_parts_only:
                    language = Language.rebuild(language, forced=True)

                if hearing_impaired:
                    language = Language.rebuild(language, hi=True)

                if language not in languages:
                    continue

                subtitle_id = int(sub['subtitle_id'])
                filename = sub.get('filename', '')
                release_name = sub.get('release_name', '')
                fps = sub.get('fps')
                download_url = sub.get('download_url', '')

                # Build movie_name in the format bazarr expects
                is_episode = season is not None and episode is not None
                if is_episode and series_title:
                    movie_name = f'"{series_title}" {release_name}'
                    movie_kind = 'episode'
                else:
                    movie_name = series_title or release_name
                    movie_kind = 'movie'

                movie_imdb_id = result_imdb if result_imdb else (imdb_id or None)
                if movie_imdb_id and not movie_imdb_id.startswith('tt'):
                    movie_imdb_id = f'tt{movie_imdb_id}'

                # IMDB ID matching
                if video.imdb_id and movie_imdb_id and movie_imdb_id != video.imdb_id:
                    continue

                query_parameters = {}

                subtitle = self.subtitle_class(
                    language, hearing_impaired, download_url, subtitle_id, 'imdbid',
                    movie_kind, '', movie_name, release_name, result_year, movie_imdb_id,
                    season if is_episode else None,
                    episode if is_episode else None,
                    query_parameters, filename, None,
                    fps, skip_wrong_fps=self.skip_wrong_fps
                )

                subtitle.uploader = sub.get('uploader', 'anonymous')
                # Store download_url for the download step
                subtitle.scraper_download_url = download_url

                logger.debug('Found scraper subtitle: %s [%s] by %s (%d downloads)',
                             filename, lang_2, subtitle.uploader, sub.get('download_count', 0))
                subtitles.append(subtitle)

            except (KeyError, ValueError, TypeError) as e:
                logger.warning('Failed to parse scraper subtitle: %s', e)
                continue

        logger.info('Scraper: returning %d subtitles after filtering', len(subtitles))
        return subtitles

    def _download_subtitle_scraper(self, subtitle):
        """Download subtitle content via the v1 API."""
        logger.info('Downloading subtitle %d via scraper', subtitle.subtitle_id)

        download_url = getattr(subtitle, 'scraper_download_url', None)
        if not download_url:
            download_url = f'https://www.opensubtitles.org/en/subtitles/{subtitle.subtitle_id}'

        try:
            response = self._scraper_request('/api/v1/download/subtitle', {
                'subtitle_id': str(subtitle.subtitle_id),
                'download_url': download_url,
            })

            content = response.get('content')
            if content:
                subtitle.content = base64.b64decode(content)
            else:
                raise ServiceUnavailable('No subtitle content received from scraper')

        except APIThrottled:
            raise
        except requests.RequestException as e:
            logger.error('Failed to download subtitle from scraper: %s', e)
            raise ServiceUnavailable(f'Scraper download failed: {e}')

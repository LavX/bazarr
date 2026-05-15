# coding=utf-8

from __future__ import absolute_import

import logging
import re

from subzero.language import Language
from subliminal.cache import SHOW_EXPIRATION_TIME
from subliminal.cache import region
from subliminal.exceptions import NotInitializedProviderError
from subliminal.providers import ParserBeautifulSoup
from subliminal.providers.tvsubtitles import link_re
from subliminal.providers.tvsubtitles import TVsubtitlesProvider as _TVsubtitlesProvider
from subliminal.providers.tvsubtitles import TVsubtitlesSubtitle as _TVsubtitlesSubtitle
from subliminal.utils import sanitize
from subliminal.video import Episode

logger = logging.getLogger(__name__)


class TVsubtitlesSubtitle(_TVsubtitlesSubtitle):
    def __init__(
        self,
        language,
        subtitle_id,
        *,
        page_link=None,
        series=None,
        season=None,
        episode=None,
        year=None,
        rip=None,
        release=None,
    ):
        super(TVsubtitlesSubtitle, self).__init__(
            language,
            subtitle_id,
            page_link=page_link,
            series=series,
            season=season,
            episode=episode,
            year=year,
            rip=rip,
            release=release,
        )
        self.release_info = u"%s, %s" % (rip, release)
        self.matches = set()

    def get_matches(self, video):
        self.matches = super(TVsubtitlesSubtitle, self).get_matches(video)
        return self.matches


class TVsubtitlesProvider(_TVsubtitlesProvider):
    languages = {Language('por', 'BR')} | {Language(code) for code in [
        'ara', 'bul', 'ces', 'dan', 'deu', 'ell', 'eng', 'fin', 'fra', 'hun', 'ita', 'jpn', 'kor', 'nld', 'pol', 'por',
        'ron', 'rus', 'spa', 'swe', 'tur', 'ukr', 'zho'
    ]}
    video_types = (Episode,)
    subtitle_class = TVsubtitlesSubtitle

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def search_show_id(self, series, year=None):
        if not self.session:
            raise NotInitializedProviderError

        logger.info('Searching show id for %r', series)
        r = self.session.post(self.server_url + '/search1.php', data={'qs': series}, timeout=10)
        r.raise_for_status()

        soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])
        sanitized = sanitize(series)
        show_id = None
        for suggestion in soup.select('div.left li div a', href=re.compile(r'\/tvshow-')):
            match = link_re.match(suggestion.text)
            if not match:
                logger.error('Failed to match %s', suggestion.text)
                continue

            found_series = sanitize(match.group('series'))
            if found_series == sanitized:
                if year is not None and int(match.group('first_year')) != year:
                    logger.debug('Year does not match')
                    continue
                show_id = int(suggestion['href'][8:-5])
                logger.debug('Found show id %d', show_id)
                break

        return show_id

    def query(self, show_id, series, season, episode, year=None):
        episode = min(episode) if episode and isinstance(episode, list) else episode
        subtitles = super(TVsubtitlesProvider, self).query(show_id, series, season, episode, year)
        for subtitle in subtitles:
            subtitle.language = Language.rebuild(subtitle.language)
        return subtitles

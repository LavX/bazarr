# coding=utf-8

from __future__ import absolute_import

import bisect
from collections import defaultdict
import io
import json
import logging
import zipfile

from guessit import guessit
from requests import Session
import subliminal
from subliminal.cache import SHOW_EXPIRATION_TIME
from subliminal.cache import region
from subliminal.exceptions import AuthenticationError, ConfigurationError, ProviderError
from subliminal.providers import ParserBeautifulSoup
from subliminal.subtitle import fix_line_ending
from subliminal.utils import sanitize
from subliminal.video import Episode, Movie
from subzero.language import Language

from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches

logger = logging.getLogger(__name__)


class SubsCenterSubtitle(Subtitle):
    provider_name = 'subscenter'
    hearing_impaired_verifiable = True

    def __init__(self, language, hearing_impaired, page_link, series, season, episode, title, subtitle_id, subtitle_key,
                 subtitle_version, downloaded, releases):
        super(SubsCenterSubtitle, self).__init__(language, hearing_impaired, page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.subtitle_id = subtitle_id
        self.subtitle_key = subtitle_key
        self.subtitle_version = subtitle_version
        self.downloaded = downloaded
        self.releases = releases
        self.release_info = u", ".join(releases)
        self.page_link = page_link
        self.matches = set()

    @property
    def id(self):
        return str(self.subtitle_id)

    def get_matches(self, video):
        self.matches = set()

        if isinstance(video, Episode):
            if video.series and sanitize(self.series) == sanitize(video.series):
                self.matches.add('series')
            if video.season and self.season == video.season:
                self.matches.add('season')
            if video.episode and self.episode == video.episode:
                self.matches.add('episode')

        if video.title and sanitize(self.title) == sanitize(video.title):
            self.matches.add('title')

        type_ = "episode" if isinstance(video, Episode) else "movie"
        for release in self.releases:
            self.matches |= guess_matches(video, guessit(release, {'type': type_}))

        return self.matches

    def __repr__(self):
        return '<%s %r %s [%s]>' % (self.__class__.__name__, self.page_link, self.id, self.language)


class SubsCenterProvider(Provider):
    languages = {Language.fromalpha2(code) for code in ['he']}
    video_types = (Episode, Movie)
    server_url = 'http://www.subscenter.info/he/'
    subtitle_class = SubsCenterSubtitle
    hearing_impaired_verifiable = True

    def __init__(self, username=None, password=None):
        if username is not None and password is None or username is None and password is not None:
            raise ConfigurationError('Username and password must be specified')

        self.session = None
        self.username = username
        self.password = password
        self.logged_in = False

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = 'Subliminal/{}'.format(subliminal.__short_version__)

        if self.username is not None and self.password is not None:
            logger.debug('Logging in')
            url = self.server_url + 'subscenter/accounts/login/'

            self.session.get(url, timeout=10)
            csrf_token = self.session.cookies['csrftoken']

            data = {'username': self.username, 'password': self.password, 'csrfmiddlewaretoken': csrf_token}
            r = self.session.post(url, data, allow_redirects=False, timeout=10)

            if r.status_code != 302:
                raise AuthenticationError(self.username)

            logger.info('Logged in')
            self.logged_in = True

    def terminate(self):
        if self.logged_in:
            logger.info('Logging out')
            r = self.session.get(self.server_url + 'subscenter/accounts/logout/', timeout=10)
            r.raise_for_status()
            logger.info('Logged out')
            self.logged_in = False

        self.session.close()

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME, should_cache_fn=lambda value: value)
    def _search_url_titles(self, title):
        logger.info('Searching title name for %r', title)
        r = self.session.get(self.server_url + 'subtitle/search/', params={'q': title}, timeout=10)
        r.raise_for_status()

        if r.history and all([h.status_code == 302 for h in r.history]):
            logger.debug('Redirected to the subtitles page')
            links = [r.url]
        else:
            soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])
            links = [link.attrs['href'] for link in soup.select('#processes div.generalWindowTop a')]
            logger.debug('Found %d suggestions', len(links))

        url_titles = defaultdict(list)
        for link in links:
            parts = link.split('/')
            url_titles[parts[-3]].append(parts[-2])

        return url_titles

    def query(self, title, season=None, episode=None):
        url_titles = self._search_url_titles(title)

        if season and episode:
            if 'series' not in url_titles:
                logger.error('No URL title found for series %r', title)
                return []
            url_title = url_titles['series'][0]
            logger.debug('Using series title %r', url_title)
            url = self.server_url + 'cst/data/series/sb/{}/{}/{}/'.format(url_title, season, episode)
            page_link = self.server_url + 'subtitle/series/{}/{}/{}/'.format(url_title, season, episode)
        else:
            if 'movie' not in url_titles:
                logger.error('No URL title found for movie %r', title)
                return []
            url_title = url_titles['movie'][0]
            logger.debug('Using movie title %r', url_title)
            url = self.server_url + 'cst/data/movie/sb/{}/'.format(url_title)
            page_link = self.server_url + 'subtitle/movie/{}/'.format(url_title)

        logger.debug('Getting the list of subtitles')
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        results = json.loads(r.text)

        subtitles = {}
        for language_code, language_data in results.items():
            for quality_data in language_data.values():
                for subtitles_data in quality_data.values():
                    for subtitle_item in subtitles_data.values():
                        language = Language.fromalpha2(language_code)
                        hearing_impaired = bool(subtitle_item['hearing_impaired'])
                        subtitle_id = subtitle_item['id']
                        subtitle_key = subtitle_item['key']
                        subtitle_version = subtitle_item['h_version']
                        downloaded = subtitle_item['downloaded']
                        release = subtitle_item['subtitle_version']

                        if subtitle_id in subtitles:
                            logger.debug('Found additional release %r for subtitle %d', release, subtitle_id)
                            bisect.insort_left(subtitles[subtitle_id].releases, release)
                            subtitles[subtitle_id].downloaded += downloaded
                            continue

                        subtitle = self.subtitle_class(language, hearing_impaired, page_link, title, season, episode,
                                                       title, subtitle_id, subtitle_key, subtitle_version, downloaded,
                                                       [release])
                        logger.debug('Found subtitle %r', subtitle)
                        subtitles[subtitle_id] = subtitle

        return list(subtitles.values())

    def list_subtitles(self, video, languages):
        season = episode = None
        title = video.title

        if isinstance(video, Episode):
            title = video.series
            season = video.season
            episode = video.episode

        return [subtitle for subtitle in self.query(title, season, episode) if subtitle.language in languages]

    def download_subtitle(self, subtitle):
        url = self.server_url + 'subtitle/download/{}/{}/'.format(subtitle.language.alpha2, subtitle.subtitle_id)
        params = {'v': subtitle.subtitle_version, 'key': subtitle.subtitle_key}
        r = self.session.get(url, params=params, headers={'Referer': subtitle.page_link}, timeout=10)
        r.raise_for_status()

        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                namelist = [name for name in zf.namelist() if not name.endswith('.txt')]
                if len(namelist) > 1:
                    raise ProviderError('More than one file to unzip')

                subtitle.content = fix_line_ending(zf.read(namelist[0]))
        except zipfile.BadZipfile:
            raise ProviderError('Daily limit exceeded')

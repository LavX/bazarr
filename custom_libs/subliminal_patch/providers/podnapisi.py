# coding=utf-8

from __future__ import absolute_import

import io
import json
import logging
import ssl
from zipfile import ZipFile

from babelfish import language_converters
from guessit import guessit
from requests.adapters import HTTPAdapter
from urllib3 import poolmanager

from subliminal.providers.podnapisi import PodnapisiProvider as _PodnapisiProvider
from subliminal.providers.podnapisi import PodnapisiSubtitle as _PodnapisiSubtitle
from subliminal.video import Episode, Movie
from subliminal_patch.providers.mixins import ProviderSubtitleArchiveMixin
from subliminal_patch.subtitle import guess_matches
from subliminal_patch.utils import fix_inconsistent_naming as _fix_inconsistent_naming
from subliminal_patch.utils import sanitize
from subzero.language import Language

logger = logging.getLogger(__name__)


def fix_inconsistent_naming(title):
    """Fix titles with inconsistent naming using dictionary and sanitize them."""
    replacements = {}
    normalized_title = title.replace("Marvels", "").replace("Marvel's", "")
    if normalized_title != title:
        replacements[title] = normalized_title

    return _fix_inconsistent_naming(title, replacements)


def _to_int(value):
    if value in (None, ""):
        return None
    return int(value)


def _flags(data):
    flags = data or []
    if isinstance(flags, str):
        flags = [flags]
    return set(flags)


def _is_foreign_only(flags):
    return bool(flags & {"f", "foreign", "foreign_only", "foreign_part", "foreign_parts", "forced"})


class PodnapisiSubtitle(_PodnapisiSubtitle):
    provider_name = 'podnapisi'
    hearing_impaired_verifiable = True

    def __init__(
        self,
        language,
        subtitle_id,
        *,
        hearing_impaired=False,
        page_link=None,
        releases=None,
        title=None,
        season=None,
        episode=None,
        year=None,
        asked_for_release_group=None,
        asked_for_episode=None,
    ):
        super(PodnapisiSubtitle, self).__init__(
            language,
            subtitle_id,
            hearing_impaired=hearing_impaired,
            page_link=page_link,
            releases=releases,
            title=title,
            season=season,
            episode=episode,
            year=year,
        )
        self.pid = subtitle_id
        self.release_info = u", ".join(self.releases)
        self.asked_for_release_group = asked_for_release_group
        self.asked_for_episode = asked_for_episode
        self.matches = set()

    def get_matches(self, video):
        matches = set()

        if isinstance(video, Episode):
            if video.series and (fix_inconsistent_naming(self.title) in (
                    fix_inconsistent_naming(name) for name in [video.series] + video.alternative_series)):
                matches.add('series')
            if video.original_series and self.year is None or video.year and video.year == self.year:
                matches.add('year')
            if video.season and self.season == video.season:
                matches.add('season')
            if video.episode and self.episode == video.episode:
                matches.add('episode')
            for release in self.releases:
                matches |= guess_matches(video, guessit(release, {'type': 'episode'}))
        elif isinstance(video, Movie):
            if video.title and (sanitize(self.title) in (
                    sanitize(name) for name in [video.title] + video.alternative_titles)):
                matches.add('title')
            if video.year and self.year == video.year:
                matches.add('year')
            for release in self.releases:
                matches |= guess_matches(video, guessit(release, {'type': 'movie'}))

        self.matches = matches
        return matches


class PodnapisiAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        ctx = ssl.create_default_context()
        ctx.set_ciphers('DEFAULT@SECLEVEL=0')
        ctx.check_hostname = False
        self.poolmanager = poolmanager.PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_version=ssl.PROTOCOL_TLS,
            ssl_context=ctx
        )


class PodnapisiProvider(_PodnapisiProvider, ProviderSubtitleArchiveMixin):
    languages = ({Language('por', 'BR'), Language('srp', script='Latn'), Language('srp', script='Cyrl')} |
                 {Language.fromalpha2(code) for code in language_converters['alpha2'].codes})
    languages.update(set(Language.rebuild(language, forced=True) for language in languages))
    languages.update(set(Language.rebuild(language, hi=True) for language in languages))

    video_types = (Episode, Movie)
    server_url = 'https://www.podnapisi.net/subtitles'
    only_foreign = False
    also_foreign = False
    verify_ssl = True
    subtitle_class = PodnapisiSubtitle
    hearing_impaired_verifiable = True

    def __init__(self, only_foreign=False, also_foreign=False, verify_ssl=True, timeout=10):
        self.only_foreign = only_foreign
        self.also_foreign = also_foreign
        self.verify_ssl = verify_ssl

        if only_foreign:
            logger.info("Only searching for foreign/forced subtitles")

        super(PodnapisiProvider, self).__init__(timeout=timeout)

    def initialize(self):
        super(PodnapisiProvider, self).initialize()
        self.session.mount('https://', PodnapisiAdapter())
        self.session.verify = self.verify_ssl

    def list_subtitles(self, video, languages):
        if getattr(video, "is_special", False):
            logger.info("%s can't search for specials right now, skipping", self)
            return []

        season = episode = None
        if isinstance(video, Episode):
            titles = [fix_inconsistent_naming(title) for title in [video.series] + video.alternative_series]
            season = video.season
            episode = video.episode
        elif isinstance(video, Movie):
            titles = [video.title] + video.alternative_titles
        else:
            return []

        for title in titles:
            subtitles = [
                subtitle
                for language in languages
                for subtitle in self.query(language, title, video=video, season=season, episode=episode,
                                           year=video.year)
            ]
            if subtitles:
                return subtitles

        return []

    def query(self, language, keyword, video=None, season=None, episode=None, year=None,
              only_foreign=None, also_foreign=None):
        if self.session is None:
            return []

        only_foreign = self.only_foreign if only_foreign is None else only_foreign
        also_foreign = self.also_foreign if also_foreign is None else also_foreign
        api_language = Language.rebuild(language, hi=False, forced=False)
        search_language = str(api_language).lower()
        if search_language == "sr-cyrl":
            search_language = "sr"

        params = {'keywords': keyword, 'language': search_language}
        is_episode = False
        if season is not None and episode is not None:
            is_episode = True
            params['seasons'] = season
            params['episodes'] = min(episode) if isinstance(episode, list) else episode
            params['movie_type'] = ['tv-series', 'mini-series']
        else:
            params['movie_type'] = 'movie'
        if year:
            params['year'] = year

        logger.info('Searching subtitles %r', params)
        subtitles = []
        pids = set()
        while True:
            response = self.session.get(self.server_url + '/search/advanced', params=params, timeout=self.timeout)
            response.raise_for_status()
            result = json.loads(response.text)

            for data in result['data']:
                pid = str(data['id'])
                if pid in pids:
                    logger.debug('Ignoring duplicate %r', pid)
                    continue

                movie = data.get('movie') or {}
                if is_episode and movie.get('type') == 'movie':
                    logger.error('Wrong type detected: movie for episode')
                    continue

                flags = _flags(data.get('flags'))
                hearing_impaired = 'n' in flags or 'hearing_impaired' in flags
                foreign = _is_foreign_only(flags)
                if only_foreign and not foreign:
                    continue
                if not only_foreign and not also_foreign and foreign:
                    continue

                subtitle_language = Language.fromietf(data['language'])
                if foreign and also_foreign:
                    subtitle_language = Language.rebuild(subtitle_language, forced=True)
                if hearing_impaired:
                    subtitle_language = Language.rebuild(subtitle_language, hi=True)
                if language != subtitle_language:
                    continue

                episode_info = movie.get('episode_info') or {}
                subtitle = self.subtitle_class(
                    language=subtitle_language,
                    subtitle_id=pid,
                    hearing_impaired=hearing_impaired,
                    page_link=data.get('url'),
                    releases=(data.get('releases') or []) + (data.get('custom_releases') or []),
                    title=movie.get('title'),
                    season=_to_int(episode_info.get('season')) if is_episode else None,
                    episode=_to_int(episode_info.get('episode')) if is_episode else None,
                    year=_to_int(movie.get('year')),
                    asked_for_release_group=getattr(video, "release_group", None),
                    asked_for_episode=episode,
                )

                logger.debug('Found subtitle %r', subtitle)
                subtitles.append(subtitle)
                pids.add(pid)

            if int(result['page']) >= int(result['all_pages']):
                break

            params['page'] = int(result['page']) + 1
            logger.debug('Getting page %d', params['page'])

        return subtitles

    def download_subtitle(self, subtitle):
        logger.info('Downloading subtitle %r', subtitle)
        response = self.session.get(
            self.server_url + f'/{subtitle.subtitle_id}/download',
            params={'container': 'zip'},
            timeout=self.timeout,
        )
        response.raise_for_status()

        with ZipFile(io.BytesIO(response.content)) as zf:
            subtitle.content = self.get_subtitle_from_archive(subtitle, zf)

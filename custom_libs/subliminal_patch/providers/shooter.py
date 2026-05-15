# coding=utf-8

from __future__ import absolute_import

import json
import logging
import os

from babelfish import language_converters
from requests import Session
import subliminal
from subliminal.subtitle import fix_line_ending
from subliminal.video import Episode, Movie
from subzero.language import Language

from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle

logger = logging.getLogger(__name__)

try:
    language_converters.register('shooter = subliminal_patch.converters.shooter:ShooterConverter')
except ValueError:
    pass


class ShooterSubtitle(Subtitle):
    provider_name = 'shooter'

    def __init__(self, language, hash, download_link):
        super(ShooterSubtitle, self).__init__(language)
        self.hash = hash
        self.download_link = download_link
        self.release_info = hash
        self.page_link = download_link
        self.matches = set()

    @property
    def id(self):
        return self.download_link

    def get_matches(self, video):
        self.matches = set()

        if 'shooter' in video.hashes and video.hashes['shooter'] == self.hash:
            self.matches.add('hash')

        return self.matches


class ShooterProvider(Provider):
    languages = {Language(code) for code in ['eng', 'zho']}
    server_url = 'https://www.shooter.cn/api/subapi.php'
    subtitle_class = ShooterSubtitle
    video_types = (Episode, Movie)

    def __init__(self):
        self.session = None

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = 'Subliminal/%s' % subliminal.__short_version__

    def terminate(self):
        self.session.close()

    def query(self, language, filename, hash=None):
        params = {'filehash': hash, 'pathinfo': os.path.realpath(filename), 'format': 'json', 'lang': language.shooter}
        logger.debug('Searching subtitles %r', params)
        r = self.session.post(self.server_url, params=params, timeout=10)
        r.raise_for_status()

        if r.content == b'\xff':
            logger.debug('No subtitles found')
            return []

        results = json.loads(r.text)
        return [self.subtitle_class(language, hash, item['Link']) for result in results for item in result['Files']]

    def list_subtitles(self, video, languages):
        return [subtitle for language in languages for subtitle in self.query(language, video.name,
                video.hashes.get('shooter'))]

    def download_subtitle(self, subtitle):
        logger.info('Downloading subtitle %r', subtitle)
        r = self.session.get(subtitle.download_link, timeout=10)
        r.raise_for_status()

        subtitle.content = fix_line_ending(r.content)

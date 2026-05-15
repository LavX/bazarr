# -*- coding: utf-8 -*-
from __future__ import absolute_import

import re

from subliminal.providers.subtitulamos import (
    SubtitulamosProvider as _UpstreamSubtitulamosProvider,
    SubtitulamosSubtitle as _UpstreamSubtitulamosSubtitle,
)
from subzero.language import Language


def _to_bazarr_language(language):
    return Language.fromietf(str(language))


class SubtitulamosTVSubtitle(_UpstreamSubtitulamosSubtitle):
    provider_name = 'subtitulamostv'
    hash_verifiable = False

    @classmethod
    def from_upstream(cls, subtitle):
        return cls(
            _to_bazarr_language(subtitle.language),
            subtitle.subtitle_id,
            hearing_impaired=subtitle.hearing_impaired,
            page_link=subtitle.page_link,
            series=subtitle.series,
            season=subtitle.season,
            episode=subtitle.episode,
            title=subtitle.title,
            year=subtitle.year,
            release_group=subtitle.release_group,
            download_link=subtitle.download_link,
        )

    def __init__(
        self,
        language,
        subtitle_id='',
        hearing_impaired=None,
        page_link=None,
        series=None,
        season=None,
        episode=None,
        title=None,
        year=None,
        release_group=None,
        download_link=None,
    ):
        super(SubtitulamosTVSubtitle, self).__init__(
            language,
            subtitle_id,
            hearing_impaired=hearing_impaired,
            page_link=page_link,
            series=series,
            season=season,
            episode=episode,
            title=title,
            year=year,
            release_group=release_group,
            download_link=download_link,
        )
        self.release_info = release_group
        self.matches = set()

    @property
    def id(self):
        return self.download_link or super(SubtitulamosTVSubtitle, self).id

    @property
    def info(self):
        return self.release_info or super(SubtitulamosTVSubtitle, self).info

    def get_matches(self, video):
        self.matches = super(SubtitulamosTVSubtitle, self).get_matches(video)
        return self.matches


class SubtitulamosTVProvider(_UpstreamSubtitulamosProvider):
    """Subtitulamos.tv provider exposed under Bazarr's existing provider id."""

    languages = {_to_bazarr_language(language) for language in _UpstreamSubtitulamosProvider.languages}
    video_types = _UpstreamSubtitulamosProvider.video_types
    subtitle_class = SubtitulamosTVSubtitle

    @staticmethod
    def _normalize_search_title(title):
        return re.sub(r'\s+\(\d{4}\)$', '', title or '').strip().lower()

    def _query_search(self, search_param):
        title = self._normalize_search_title(search_param)
        results = super(SubtitulamosTVProvider, self)._query_search(search_param)
        return [
            result for result in results
            if self._normalize_search_title(result.get('show_name')) == title
        ]

    def query(self, series=None, season=None, episode=None, year=None, languages=None):
        subtitles = super(SubtitulamosTVProvider, self).query(series, season, episode, year, languages)
        return [self.subtitle_class.from_upstream(subtitle) for subtitle in subtitles]

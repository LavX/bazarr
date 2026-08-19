# -*- coding: utf-8 -*-

import logging
import time

from requests import HTTPError
from requests import Session
from subliminal.score import get_equivalent_release_groups
from subliminal.utils import sanitize_release_group
from subliminal_patch.core import Episode
from subliminal_patch.language import PatchedAddic7edConverter
from subliminal_patch.providers import Provider
from subliminal_patch.providers.utils import update_matches
from subliminal_patch.subtitle import Subtitle
from subzero.language import Language

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.gestdown.info"


class GestdownSubtitle(Subtitle):
    provider_name = "gestdown"
    hash_verifiable = False
    hearing_impaired_verifiable = True

    def __init__(self, language, data: dict, series=None, season=None, episode=None):
        super().__init__(language, hearing_impaired=data["hearingImpaired"])
        self.page_link = _BASE_URL + data["downloadUri"]
        self._id = data["subtitleId"]
        raw_releases = [v.strip() for v in data["version"].split(",") if v.strip()]
        self.releases = []
        for v in raw_releases:
            if season is not None and episode is not None and not (
                f"s{season:02d}" in v.lower()
                or f"{season}x" in v.lower()
                or (series and series.lower() in v.lower())
            ):
                clean_ver = v.replace(" ", ".")
                clean_series = series.strip().replace(" ", ".") if series else ""
                formatted = (
                    f"{clean_series}.S{season:02d}E{episode:02d}.{clean_ver}"
                    if clean_series
                    else f"S{season:02d}E{episode:02d}.{clean_ver}"
                )
                self.releases.append(formatted)
            else:
                self.releases.append(v)
        self.qualities = data.get("qualities") or []
        self.release_info = "\n".join(self.releases) if self.releases else data.get("version", "")
        self.matches = set()
    def get_matches(self, video):
        self.matches = {"title", "series", "season", "episode", "tvdb_id"}

        update_matches(self.matches, video, self.release_info)

        # release_group
        if (
            "release_group" not in self.matches
            and video.release_group
            and self.releases
        ):
            video_release_groups = get_equivalent_release_groups(
                sanitize_release_group(video.release_group)
            )
            for release in self.releases:
                if any(
                    r in sanitize_release_group(release) for r in video_release_groups
                ):
                    self.matches.add("release_group")
                    break

        # resolution
        if video.resolution and self.qualities and video.resolution in self.qualities:
            self.matches.add("resolution")

        return self.matches

    @property
    def id(self):
        return self._id


def _retry_on_423(method):
    def retry(self, *args, **kwargs):
        retries = 0
        while 3 > retries:
            try:
                return method(self, *args, **kwargs)
            except HTTPError as error:
                if error.response.status_code != 423:
                    raise

                retries += 1

                logger.debug("423 returned. Retrying in 30 seconds")
                time.sleep(30)

        logger.debug("Retries limit exceeded. Ignoring query")
        return []

    return retry


class GestdownProvider(Provider):
    provider_name = "gestdown"

    video_types = (Episode,)

    # fmt: off
    languages = {Language('por', 'BR')} | {Language(lang) for lang in [
        'ara', 'aze', 'ben', 'bos', 'bul', 'cat', 'ces', 'dan', 'deu', 'ell', 'eng', 'eus', 'fas', 'fin', 'fra', 'glg',
        'heb', 'hrv', 'hun', 'hye', 'ind', 'ita', 'jpn', 'kor', 'mkd', 'msa', 'nld', 'nor', 'pol', 'por', 'ron', 'rus',
        'slk', 'slv', 'spa', 'sqi', 'srp', 'swe', 'tha', 'tur', 'ukr', 'vie', 'zho'
    ]} | {Language.fromietf(lang) for lang in ["sr-Latn", "sr-Cyrl"]}
    languages.update(set(Language.rebuild(lang, hi=True) for lang in languages))
    # fmt: on

    _converter = PatchedAddic7edConverter()

    def initialize(self):
        self._session = Session()
        self._session.headers.update({"User-Agent": "Bazarr"})

    def terminate(self):
        self._session.close()

    def _subtitles_search(self, video, language: Language, show_id):
        lang = self._converter.convert(language.alpha3)
        response = self._session.get(
            f"{_BASE_URL}/subtitles/get/{show_id}/{video.season}/{video.episode}/{lang}"
        )

        # TODO: implement rate limiting
        response.raise_for_status()
        resp_json = response.json()
        matching_subtitles = resp_json.get("matchingSubtitles")

        if not matching_subtitles:
            logger.debug("No episodes found for '%s' language", language)
            return None

        episode_info = resp_json.get("episode") or {}
        series_name = episode_info.get("show") or getattr(video, "series", None)
        season_num = episode_info.get("season") or getattr(video, "season", None)
        episode_num = episode_info.get("number") or getattr(video, "episode", None)

        for subtitle_dict in matching_subtitles:
            if not subtitle_dict["completed"]:
                continue

            sub = GestdownSubtitle(
                language,
                subtitle_dict,
                series=series_name,
                season=season_num,
                episode=episode_num,
            )
            logger.debug("Found subtitle: %s", sub)
            yield sub
    def _search_show(self, video):
        try:
            response = self._session.get(
                f"{_BASE_URL}/shows/external/tvdb/{video.series_tvdb_id}"
            )
            response.raise_for_status()
            return response.json()["shows"]
        except HTTPError as error:
            if error.response.status_code == 404:
                return None
            raise

    @_retry_on_423
    def list_subtitles(self, video, languages):
        subtitles = []
        shows = self._search_show(video)
        if shows is None:
            logger.debug("Couldn't find the show")
            return subtitles

        for language in languages:
            try:
                for show in shows:
                    subs = list(self._subtitles_search(video, language, show["id"]))
                    if len(subs) > 0:
                        subtitles += subs
                        continue
            except HTTPError as error:
                if error.response.status_code == 404:
                    logger.debug("Couldn't find the show or its season/episode")
                    return []
                raise

        return subtitles

    def download_subtitle(self, subtitle: GestdownSubtitle):
        response = self._session.get(subtitle.page_link, allow_redirects=True)
        response.raise_for_status()
        subtitle.content = response.content

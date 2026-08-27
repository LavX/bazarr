# -*- coding: utf-8 -*-
# LEGACY PROVIDER. Do not fix subtitle providers here.
#
# Built-in providers are deprecated and will be removed after Bazarr+ v3.0.0.
# Providers now ship as plugins in the Bazarr+ provider catalog and are
# installed at runtime through the Provider Hub, so a fix reaches users as a
# plugin release instead of waiting for a Bazarr+ release, and a broken
# provider can no longer take the application down with it.
#
# Send provider fixes here instead:
#   https://github.com/LavX/bazarr-provider-catalog
#   docs/writing-a-scraper-provider.md in that repo explains how to port one.
#
# A pull request against this file will most likely be asked to move.

import logging
import re
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


def _token_boundaries(token):
    """``token`` surrounded by release-name separators, or by nothing.

    Not ``\\b``: the underscore is a regex word character but a release-name
    separator, so ``Show_S01E02_1080p`` would read as naming no episode at all
    and get a second copy of the name prefixed onto it.
    """
    return rf"(?<![a-z0-9]){token}(?![a-z0-9])"


# A season tag and everything hanging off it, so a multi-episode release such
# as S01E01-E02, S01E01E02 or S01E01-10 is recognised as naming all of them.
# The tail is one flat character class rather than a repeated group of
# quantifiers: a nested one backtracks exponentially on a malformed token like
# S01E0000000000000000x, and one such Gestdown result would stall the listing.
# A trailing vN is a re-release marker rather than part of the episode number,
# and the numbers are length-bounded because int() refuses a string of more
# than 4300 digits and the ValueError would take the whole listing with it.
_SEASON_EPISODES = re.compile(
    r"(?<![a-z0-9])s(\d{1,4})((?:[-_. ]|e|\d)*)(?:v\d{1,3})?(?![a-z0-9])"
)
# The same in the NxMM spelling.
_SEASON_X_EPISODES = re.compile(
    r"(?<![a-z0-9])(\d{1,4})x((?:[-_. ]|\d)*)(?:v\d{1,3})?(?![a-z0-9])"
)

# Longer than any real episode number, and short enough for int() to accept.
_MAX_NUMBER_DIGITS = 6

# A season named without an episode, in either spelling, plus a word saying the
# release is the whole of it.
_SEASON_ONLY = re.compile(r"(?<![a-z0-9])s(\d{1,4})(?![a-z0-9])")
_SEASON_WORD = re.compile(r"(?<![a-z0-9])season[-_. ]?(\d{1,4})(?![0-9])")
# What turns a season into a whole season. "complete" and "pack" say it on their
# own; "full" does not, because "Full HD" is a resolution, so it counts only in
# the phrase "full season". "season" is deliberately absent: it is the token that
# names the season in the first place, so accepting it here would classify an
# ordinary "Season 1 WEB-DL" as a pack and strip the episode marker from the most
# common tag shape there is.
_PACK_WORDS = re.compile(
    r"(?<![a-z0-9])(?:complete|pack)(?![a-z0-9])"
    r"|(?<![a-z0-9])full[^a-z0-9]+season(?![a-z0-9])"
)


def _tail_covers(tail, episode):
    """True when ``tail`` names ``episode``, expanding ranges as it goes."""
    parts = [part for part in re.findall(r"\d+|-", tail)
             if part == "-" or len(part) <= _MAX_NUMBER_DIGITS]
    for index, part in enumerate(parts):
        if part != "-":
            if int(part) == episode:
                return True
            continue

        if 0 < index < len(parts) - 1 and parts[index - 1] != "-" and parts[index + 1] != "-":
            first, last = int(parts[index - 1]), int(parts[index + 1])
            if first <= episode <= last:
                return True

    return False


def _already_names_the_episode(lowered, season, episode):
    """True when the release name already says which episode this is."""
    for pattern in (_SEASON_EPISODES, _SEASON_X_EPISODES):
        for match in pattern.finditer(lowered):
            if int(match.group(1)) == season and _tail_covers(match.group(2), episode):
                return True

    return False


def _covers_the_whole_season(lowered, season):
    """True when the release is a pack of the season being searched.

    A pack covers the requested episode without naming it, so the formatter
    would treat it as a bare tag and prefix Show.SxxEyy. onto a name that
    already says S01: two conflicting season markers, and less for guessit to
    work with than the proper pack name it arrived with.

    Only the requested season counts. S03.COMPLETE says nothing about a season
    1 episode and still deserves the marker.
    """
    named = any(int(match.group(1)) == season
                for pattern in (_SEASON_ONLY, _SEASON_WORD)
                for match in pattern.finditer(lowered)
                if len(match.group(1)) <= _MAX_NUMBER_DIGITS)
    return named and _PACK_WORDS.search(lowered) is not None


def _format_release(version_item, series, season, episode):
    """Display-only scene-style name: ``Series.SxxEyy.version``.

    Left alone when the version already names the episode, so a proper release
    name is never mangled. The tests are anchored rather than bare substrings:
    an unanchored series test matches far too eagerly on short titles.
    """
    if season is None or episode is None:
        return version_item

    lowered = version_item.lower()
    if _already_names_the_episode(lowered, season, episode):
        return version_item
    if _covers_the_whole_season(lowered, season):
        return version_item

    clean_version = version_item.replace(" ", ".")
    # Trailing separator punctuation goes: a title like "S.W.A.T." ends in the
    # dot that also separates release tokens, and the two share it, so keeping
    # it would make the boundary test look past it at the W of WEB-DL and
    # prefix a second copy of the title.
    clean_series = series.strip().replace(" ", ".").strip("._-") if series else ""
    if clean_series and re.search(
        _token_boundaries(re.escape(clean_series.lower())), clean_version.lower()
    ):
        return clean_version
    if clean_series:
        return f"{clean_series}.S{season:02d}E{episode:02d}.{clean_version}"
    return f"S{season:02d}E{episode:02d}.{clean_version}"


class GestdownSubtitle(Subtitle):
    provider_name = "gestdown"
    hash_verifiable = False
    hearing_impaired_verifiable = True

    def __init__(self, language, data: dict, series=None, season=None, episode=None):
        super().__init__(language, hearing_impaired=data["hearingImpaired"])
        self.page_link = _BASE_URL + data["downloadUri"]
        self._id = data["subtitleId"]
        # `releases` stays RAW. get_matches below searches it for the video's
        # release group, so injecting the series title here would let a group
        # name that occurs inside the show's own title score a match it did not
        # earn. Only release_info, which is display text, gets the scene-style
        # name.
        self.releases = [v.strip() for v in data["version"].split(",") if v.strip()]
        self.qualities = data.get("qualities") or []
        formatted = [
            _format_release(release, series, season, episode)
            for release in self.releases
        ]
        self.release_info = "\n".join(formatted) if formatted else data.get("version", "")
        self.matches = set()

    def get_matches(self, video):
        self.matches = {"title", "series", "season", "episode", "tvdb_id"}

        # Raw versions, never release_info. update_matches searches whatever it
        # is handed for the video's release group, so display text prefixed with
        # the series title would score the same unearned match the loop below is
        # careful to avoid.
        update_matches(self.matches, video, self.releases or [self.release_info])

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

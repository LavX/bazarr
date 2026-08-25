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
import io
import os
import re
import zipfile
import tempfile
import subprocess
import shutil
import time
from time import sleep
from urllib.parse import quote
from urllib.parse import parse_qs
from requests.exceptions import HTTPError
import rarfile
from bs4 import FeatureNotFound

from guessit import guessit
from requests.exceptions import RequestException
from subliminal.cache import region
from subliminal.exceptions import ConfigurationError, AuthenticationError, ServiceUnavailable, DownloadLimitExceeded
from subliminal.providers import ParserBeautifulSoup
from subliminal.subtitle import SUBTITLE_EXTENSIONS, fix_line_ending
from subliminal.utils import sanitize, sanitize_release_group
from subliminal.video import Episode, Movie
from subliminal_patch.exceptions import TooManyRequests, IPAddressBlocked, SearchLimitReached
from subliminal_patch.http import RetryingCFSession
from subliminal_patch.providers import Provider, reinitialize_on_error
from subliminal_patch.score import get_scores, framerate_equal
from subliminal_patch.subtitle import Subtitle, guess_matches
from subzero.language import Language
from dogpile.cache.api import NO_VALUE

logger = logging.getLogger(__name__)

# Budget for a single CLI extractor invocation. These three limits are one
# policy: the archives come from a third-party subtitle site, so their contents
# are untrusted, and _extract_via_cli runs inside the download worker.
#
# Time bounds a hang. A malformed or password-protected archive can make
# unar/7z/unrar sit waiting forever. Mirrors FILEBOT_XATTR_TIMEOUT in
# subliminal_patch.refiners.filebot.
#
# Size and member count bound a fill, which the timeout does not: extraction
# writes to disk, and a small response can expand enormously. A 76KB 7z reaching
# 500MB, roughly 6800:1, is trivial to build, so a 1MB provider response can fill
# several GB of the user's disk in seconds. A huge member count is the same
# attack aimed at inodes instead of bytes.
#
# Headroom is deliberate, since a cap that trips on a genuine season pack would
# be worse than no cap. The largest real archive in tests/subliminal_patch/data
# is titlovi_some_subtitle_pack.zip: 588639 bytes across 24 members. 128MB is
# roughly 230x that size and 2000 members roughly 80x that count, which leaves
# room for packs far larger than anything this provider ships, VobSub .sub
# members included.
CLI_EXTRACT_TIMEOUT = 60
CLI_EXTRACT_MAX_BYTES = 128 * 1024 * 1024
CLI_EXTRACT_MAX_MEMBERS = 2000

# How often the budget is checked while an extractor runs. Peak disk use is the
# cap plus at most one interval of writing, so this trades a little polling
# against the overshoot. Measured on a 1.2GB zip bomb (1028:1): the extractor is
# killed at about 156MB against the 128MB cap, in well under a second.
CLI_EXTRACT_POLL_SECONDS = 0.1


def guess_matches_wanted_episode(video, guess):
    """False when a guessed name contradicts the episode we are downloading for.

    A missing season or episode in the guess is not a contradiction, only a
    present-and-different one is. Every path that picks a member out of an
    archive has to apply this, otherwise the caller silently gets a subtitle
    for the wrong episode and the episode is marked done.
    """
    if not isinstance(video, Episode):
        return True

    episode = guess.get('episode')
    if episode is not None:
        if isinstance(episode, list):
            if video.episode not in episode:
                return False
        elif episode != video.episode:
            return False

    season = guess.get('season')
    if season is not None:
        if isinstance(season, list):
            if video.season not in season:
                return False
        elif season != video.season:
            return False

    return True


def name_matches_wanted_episode(video, name):
    """guess_matches_wanted_episode() for a member name that is not guessed yet."""
    if not isinstance(video, Episode):
        return True

    return guess_matches_wanted_episode(video, guessit(name))


def clean_release_line(text):
    # Separate glued keywords like versãoThe or releaseThe
    text = re.sub(r"(vers[aã]o|release|filme)([A-Z0-9])", r"\1 \2", text, flags=re.I)
    # Strip common Portuguese subtitle upload prefixes
    prefix_pattern = (
        r"^(legendas?\s*(anteriormente\s*)?(enviadas?\s*(por|pelo|do)?\s*[\w\d_]+\s*)?"
        r"|sincronizadas?|ressincronizadas?|sinc|sync|traduzidas?|ripadas?\s*(por\s*mim)?|ajustad[ao]s?|ajustei\s*(a\s*)?sincronia)?"
        r"\s*(do\s*dvd\.?|de\s*raiz\s*)?(para\s*(a|o|as|os)?\s*)?(vers[aã]o|release[s]?|filme|nomes?)?\s*[:\-–]?\s*"
    )
    return re.sub(prefix_pattern, "", text, flags=re.I).strip().strip("*").strip("`").strip()


def extract_release_info(title, year, desc):
    default_name = f"{title} ({year})" if year and title else (title or "")
    if not desc or desc.strip().lower() in (
        "não há descrição disponível",
        "nao ha descricao disponivel",
        "n/a",
        "none",
        "",
    ):
        return default_name

    lines = [line.strip().strip("*").strip("`") for line in desc.splitlines() if line.strip()]
    candidates = []
    release_re = re.compile(
        r"(2160p|1080p|720p|480p|4k|bluray|blu-ray|bdrip|brrip|web-dl|webdl|web-rip|webrip|web|dvdrip|dvd|hdtv|x264|x265|hevc|h\.264|h\.265|xvid|divx|remastered|proper|internal|repack)",
        re.I,
    )
    conversational_re = re.compile(
        r"^(legenda[s]?|ripada[s]?|enviada[s]?|postada[s]?|corrigido[s]?|feita[s]?|fiz\s|peguei\s|são\s|sao\s|não\s|nao\s|avisem|cumps|enjoy|obrigado|duração|duracao)",
        re.I,
    )

    for line in lines:
        cleaned = clean_release_line(line)
        if not cleaned:
            continue
        if release_re.search(cleaned):
            match = re.search(
                r"([\w\.\-_]+(?:2160p|1080p|720p|480p|4k|bluray|blu-ray|bdrip|brrip|web-dl|webdl|dvdrip|dvd|x264|x265|hevc|xvid|divx)[\w\.\-_]*)",
                cleaned,
                re.I,
            )
            if match and len(match.group(1)) > 10:
                candidates.append(match.group(1).strip("."))
            elif not conversational_re.search(cleaned) and len(cleaned) > 5:
                candidates.append(cleaned)
        elif title and title.lower() in cleaned.lower() and len(cleaned) > len(title) and not conversational_re.search(cleaned):
            candidates.append(cleaned)

    if candidates:
        def candidate_quality(cand):
            score = len(cand)
            if title and title.lower() in cand.lower():
                score += 100
            if "." in cand or "-" in cand:
                score += 50
            return score

        best = max(candidates, key=candidate_quality)
        return best

    return default_name


class LegendasdivxSubtitle(Subtitle):
    """Legendasdivx Subtitle."""
    provider_name = 'legendasdivx'

    def __init__(self, language, video, data, skip_wrong_fps=True):
        super(LegendasdivxSubtitle, self).__init__(language)
        self.page_link = data['link']
        self.hits = data['hits']
        self.exact_match = data['exact_match']
        self.title = data.get('title', '')
        self.year = data.get('year')
        self.description = data['description']
        self.video = video
        self.sub_frame_rate = data['frame_rate']
        self.uploader = data['uploader']
        self.wrong_fps = False
        self.skip_wrong_fps = skip_wrong_fps
        self.release_info = data.get('release_info') or self.description
        self.matches = set()
    @property
    def id(self):
        try:
            return parse_qs(self.page_link)["lid"][0]
        except (KeyError, IndexError):
            return f"legendasdivx_{self.video.imdb_id}_{self.release_info}_{self.uploader}"

    def get_matches(self, video):
        # if skip_wrong_fps = True no point to continue if they don't match
        subtitle_fps = None
        try:
            subtitle_fps = float(self.sub_frame_rate)
        except ValueError:
            pass

        # check fps match and skip based on configuration
        if video.fps and subtitle_fps and not framerate_equal(video.fps, subtitle_fps):
            self.wrong_fps = True

            if self.skip_wrong_fps:
                logger.debug("Legendasdivx :: Skipping subtitle due to FPS mismatch (expected: %s, got: %s)", video.fps,
                             self.sub_frame_rate)
                # not a single match :)
                return set()
            logger.debug("Legendasdivx :: Frame rate mismatch (expected: %s, got: %s, but continuing...)", video.fps,
                         self.sub_frame_rate)

        description = sanitize(self.description)

        # Match title
        if video.title:
            for movie_name in [video.title] + getattr(video, 'alternative_titles', []):
                if sanitize(movie_name) == sanitize(self.title) or sanitize(movie_name) in description:
                    self.matches.update(['title'])

        # Match year
        if video.year:
            if self.year and video.year == self.year:
                self.matches.update(['year'])
            elif '{:04d}'.format(video.year) in description:
                self.matches.update(['year'])

        type_ = "movie" if isinstance(video, Movie) else "episode"

        if isinstance(video, Movie):
            # score.py expands a movie imdb_id match into title plus year, which
            # is 100 of the 180 available points against a default
            # minimum_score_movie of 126. The episode branch below can take that
            # shortcut because query() passes series_imdb_id to the site's imdb=
            # filter, so the backend guarantees the match. For movies query()
            # sends imdbid='' and only puts the id in the free-text query, so
            # there is no such guarantee: claim it only when this result
            # actually carries the id.
            if video.imdb_id and video.imdb_id.lower() in self.description.lower():
                self.matches.update(['imdb_id'])
        else:
            if video.series:
                for series_name in [video.series] + getattr(video, 'alternative_series', []):
                    if sanitize(series_name) == sanitize(self.title) or sanitize(series_name) in description:
                        self.matches.update(['series'])
            if video.series_imdb_id:
                self.matches.update(['series', 'series_imdb_id', 'season', 'episode'])
            if video.season and 's{:02d}'.format(video.season) in description:
                self.matches.update(['season'])
            if video.episode and 'e{:02d}'.format(video.episode) in description:
                self.matches.update(['episode'])

        # release_group matching
        if video.release_group and sanitize_release_group(video.release_group) in sanitize_release_group(self.description):
            self.matches.update(['release_group'])

        # Guess matches from release_info
        if self.release_info:
            self.matches |= guess_matches(video, guessit(self.release_info, {"type": type_}))

        # Also guess matches across description candidate lines
        for line in self.description.splitlines():
            cleaned_line = clean_release_line(line)
            if len(cleaned_line) > 5 and cleaned_line != self.release_info:
                self.matches |= guess_matches(video, guessit(cleaned_line, {"type": type_}))

        return self.matches


class LegendasdivxProvider(Provider):
    """Legendasdivx Provider."""
    languages = {Language('por', 'BR')} | {Language('por')}
    video_types = (Episode, Movie)
    SEARCH_THROTTLE = 8
    SAFE_SEARCH_LIMIT = 145  # real limit is 150, but we use 145 to keep a buffer and prevent IPAddressBlocked exception to be raised
    site = 'https://www.legendasdivx.pt'
    headers = {
        'User-Agent': os.environ.get("SZ_USER_AGENT", "Sub-Zero/2"),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Origin': 'https://www.legendasdivx.pt',
        'Referer': 'https://www.legendasdivx.pt'
    }
    loginpage = site + '/forum/ucp.php?mode=login'
    searchurl = site + '/modules.php?name=Downloads&file=jz&d_op={d_op}&op={op}&query={query}&temporada={season}&episodio={episode}&imdb={imdbid}'
    download_link = site + '/modules.php{link}'

    def __init__(self, username, password, skip_wrong_fps=True):
        # make sure login credentials are configured.
        if any((username, password)) and not all((username, password)):
            raise ConfigurationError('Legendasdivx.pt :: Username and password must be specified')
        self.username = username
        self.password = password
        self.skip_wrong_fps = skip_wrong_fps

    def initialize(self):
        logger.debug("Legendasdivx.pt :: Creating session for requests")
        self.session = RetryingCFSession()
        # re-use PHP Session if present
        prev_cookies = region.get("legendasdivx_cookies2")
        if prev_cookies != NO_VALUE:
            logger.debug("Legendasdivx.pt :: Re-using previous legendasdivx cookies: %s", prev_cookies)
            self.session.cookies.update(prev_cookies)
        # login if session has expired
        else:
            logger.debug("Legendasdivx.pt :: Session cookies not found!")
            self.session.headers.update(self.headers)
            self.login()

    def terminate(self):
        # session close
        self.session.close()

    def login(self):
        logger.debug('Legendasdivx.pt :: Logging in')
        try:
            # sleep for a 1 second before another request
            sleep(1)
            res = self.session.get(self.loginpage)
            res.raise_for_status()
            bsoup = ParserBeautifulSoup(res.content, ['lxml'])

            _allinputs = bsoup.find_all('input')
            data = {}
            # necessary to set 'sid' for POST request
            for field in _allinputs:
                data[field.get('name')] = field.get('value')

            # sleep for a 1 second before another request
            sleep(1)
            data['username'] = self.username
            data['password'] = self.password
            res = self.session.post(self.loginpage, data)
            res.raise_for_status()
            # make sure we're logged in
            uid = self.session.cookies.get('phpbb3_2z8zs_u')
            session_id = self.session.cookies.get('PHPSESSID') or self.session.cookies.get('phpbb3_2z8zs_sid')
            if uid == '1' or not session_id:
                logger.error("Legendasdivx.pt :: Couldn't get session ID, check your credentials")
                raise AuthenticationError("Legendasdivx.pt :: Couldn't get session ID, check your credentials")

            logger.debug('Legendasdivx.pt :: Logged in successfully: session: %s', session_id)
            cj = self.session.cookies.copy()
            store_cks = ("PHPSESSID", "phpbb3_2z8zs_sid", "phpbb3_2z8zs_k", "phpbb3_2z8zs_u", "lang")
            for cn in list(self.session.cookies.keys()):
                if cn not in store_cks:
                    del cj[cn]
            # store session cookies on cache
            logger.debug("Legendasdivx.pt :: Storing legendasdivx session cookies: %r", cj)
            region.set("legendasdivx_cookies2", cj)

        except (AuthenticationError, ConfigurationError, IPAddressBlocked, TooManyRequests):
            raise
        except KeyError:
            logger.error("Legendasdivx.pt :: Couldn't get session ID, check your credentials")
            raise AuthenticationError("Legendasdivx.pt :: Couldn't get session ID, check your credentials")
        except HTTPError as e:
            if "bloqueado" in res.text.lower():
                logger.error("LegendasDivx.pt :: Your IP is blocked on this server.")
                raise IPAddressBlocked("LegendasDivx.pt :: Your IP is blocked on this server.")
            logger.error("Legendasdivx.pt :: HTTP Error %s", e)
            raise TooManyRequests("Legendasdivx.pt :: HTTP Error %s", e)
        except FeatureNotFound:
            logger.error("LegendasDivx.pt :: lxml Python module isn't installed. Make sure to install requirements.")
            raise ConfigurationError("LegendasDivx.pt :: lxml Python module isn't installed. Make sure to install "
                                     "requirements.")
        except Exception as e:
            logger.error("LegendasDivx.pt :: Uncaught error: %r", e)
            raise ServiceUnavailable("LegendasDivx.pt :: Uncaught error: %r", e)
    def _process_page(self, video, bsoup):

        subtitles = []

        _allsubs = bsoup.find_all("div", {"class": "sub_box"})

        for _subbox in _allsubs:

            hits = 0
            for th in _subbox.find_all("th"):
                if th.text == 'Hits:':
                    hits = int(th.find_next("td").text)
                if th.text == 'Idioma:':
                    lang = th.find_next("td").find("img").get('src')
                    if 'brazil' in lang.lower():
                        lang = Language.fromopensubtitles('pob')
                    elif 'portugal' in lang.lower():
                        lang = Language.fromopensubtitles('por')
                    else:
                        continue
                if th.text == "Frame Rate:":
                    frame_rate = th.find_next("td").text.strip()

            # get description for matches
            description = _subbox.find("td", {"class": "td_desc brd_up"}).get_text()

            # get subtitle link from footer
            sub_footer = _subbox.find("div", {"class": "sub_footer"})
            download = sub_footer.find("a", {"class": "sub_download"}) if sub_footer else None

            # sometimes 'a' tag is not found and returns None. Most likely HTML format error!
            try:
                download_link = self.download_link.format(link=download.get('href'))
                logger.debug("Legendasdivx.pt :: Found subtitle link on: %s ", download_link)
            except:
                logger.debug("Legendasdivx.pt :: Couldn't find download link. Trying next...")
                continue

            # get title and year from sub_header
            title = ''
            year = None
            sub_header = _subbox.find("div", {"class": "sub_header"})
            if sub_header:
                title_elem = sub_header.find("b")
                if title_elem:
                    title = title_elem.get_text().strip()
                header_text = sub_header.get_text()
                year_match = re.search(r'\((\d{4})\)', header_text)
                if year_match:
                    try:
                        year = int(year_match.group(1))
                    except ValueError:
                        pass

            uploader = sub_header.find("a").text if sub_header and sub_header.find("a") else 'anonymous'
            release_info = extract_release_info(title, year, description)

            exact_match = False
            if video.name.lower() in description.lower():
                exact_match = True

            data = {'link': download_link,
                    'exact_match': exact_match,
                    'hits': hits,
                    'uploader': uploader,
                    'frame_rate': frame_rate,
                    'title': title,
                    'year': year,
                    'release_info': release_info,
                    'description': description
                    }
            subtitles.append(
                LegendasdivxSubtitle(lang, video, data, skip_wrong_fps=self.skip_wrong_fps)
            )
        return subtitles

    @reinitialize_on_error((RequestException,), attempts=1)
    def query(self, video, languages):

        _searchurl = self.searchurl

        subtitles = []

        # Set the default search criteria
        d_op = 'search'
        op = '_jz00'

        lang_filter_key = 'form_cat'

        if isinstance(video, Movie):
            querytext = video.imdb_id if video.imdb_id else video.title
        if isinstance(video, Episode):
            # Overwrite the parameters to refine via imdb_id
            if video.series_imdb_id:
                querytext = '&faz=pesquisa_episodio'
                lang_filter_key = 'idioma'
                d_op = 'jz_00'
                op = ''
            else:
                querytext = '%22{}%22%20S{:02d}E{:02d}'.format(video.series, video.season, video.episode)
                querytext = quote(querytext.lower())

        # language query filter
        if not isinstance(languages, (tuple, list, set)):
            languages = [languages]

        for language in languages:
            logger.debug("Legendasdivx.pt :: searching for %s subtitles.", language)
            language_id = language.opensubtitles
            if 'por' in language_id:
                lang_filter = '&{}=28'.format(lang_filter_key)
            elif 'pob' in language_id:
                lang_filter = '&{}=29'.format(lang_filter_key)
            else:
                lang_filter = ''

            querytext = querytext + lang_filter if lang_filter else querytext

            search_url = _searchurl.format(
                    query=querytext,
                    season='' if isinstance(video, Movie) else video.season,
                    episode='' if isinstance(video, Movie) else video.episode,
                    imdbid='' if isinstance(video, Movie) else video.series_imdb_id.replace('tt', '') if video.series_imdb_id else None,
                    op=op,
                    d_op=d_op,
            )

            try:
                # sleep for a 1 second before another request
                sleep(1)
                searchLimitReached = False
                self.headers['Referer'] = self.site + '/index.php'
                self.session.headers.update(self.headers)
                res = self.session.get(search_url, allow_redirects=False)
                res.raise_for_status()
                if res.status_code == 200 and "<!--pesquisas:" in res.text:
                    searches_count_groups = re.search(r'<!--pesquisas: (\d*)-->', res.text)
                    if searches_count_groups:
                        try:
                            searches_count = int(searches_count_groups.group(1))
                        except TypeError:
                            pass
                        else:
                            if searches_count >= self.SAFE_SEARCH_LIMIT:
                                searchLimitReached = True
                if (res.status_code == 200 and "A legenda não foi encontrada" in res.text):
                    logger.warning('Legendasdivx.pt :: query %s return no results!', querytext)
                    # for series, if no results found, try again just with series and season (subtitle packs)
                    if isinstance(video, Episode):
                        logger.debug("Legendasdivx.pt :: trying again with just series and season on query.")
                        querytext = re.sub(r"(e|E)(\d{2})", "", querytext)
                        # sleep for a 1 second before another request
                        sleep(1)
                        res = self.session.get(search_url, allow_redirects=False)
                        res.raise_for_status()
                        if res.status_code == 200 and "<!--pesquisas:" in res.text:
                            searches_count_groups = re.search(r'<!--pesquisas: (\d*)-->', res.text)
                            if searches_count_groups:
                                try:
                                    searches_count = int(searches_count_groups.group(1))
                                except TypeError:
                                    pass
                                else:
                                    if searches_count >= self.SAFE_SEARCH_LIMIT:
                                        searchLimitReached = True
                        if (res.status_code == 200 and "A legenda não foi encontrada" in res.text):
                            logger.warning(
                                'Legendasdivx.pt :: query {0} return no results for language {1}(for series and season only).'.format(
                                    querytext, language_id))
                            continue
                if res.status_code == 302:  # got redirected to login page.
                    # seems that our session cookies are no longer valid... clean them from cache
                    region.delete("legendasdivx_cookies2")
                    logger.debug("Legendasdivx.pt :: Logging in again. Cookies have expired!")
                    # login and try again
                    self.login()
                    # sleep for a 1 second before another request
                    sleep(1)
                    res = self.session.get(search_url, allow_redirects=False)
                    res.raise_for_status()
                    if res.status_code == 200 and "<!--pesquisas:" in res.text:
                        searches_count_groups = re.search(r'<!--pesquisas: (\d*)-->', res.text)
                        if searches_count_groups:
                            try:
                                searches_count = int(searches_count_groups.group(1))
                            except TypeError:
                                pass
                            else:
                                if searches_count >= self.SAFE_SEARCH_LIMIT:
                                    searchLimitReached = True
            except (AuthenticationError, ConfigurationError, IPAddressBlocked, TooManyRequests, SearchLimitReached):
                raise
            except HTTPError as e:
                if "bloqueado" in res.text.lower():
                    logger.error("LegendasDivx.pt :: Your IP is blocked on this server.")
                    raise IPAddressBlocked("LegendasDivx.pt :: Your IP is blocked on this server.")
                logger.error("Legendasdivx.pt :: HTTP Error %s", e)
                raise TooManyRequests("Legendasdivx.pt :: HTTP Error %s", e)
            except Exception as e:
                logger.error("LegendasDivx.pt :: Uncaught error: %r", e)
                raise ServiceUnavailable("LegendasDivx.pt :: Uncaught error: %r", e)

            if searchLimitReached:
                raise SearchLimitReached(
                    "LegendasDivx.pt :: You've reached maximum number of search for the day.")

            bsoup = ParserBeautifulSoup(res.content, ['html.parser'])

            # search for more than 10 results (legendasdivx uses pagination)
            # don't throttle - maximum results = 6 * 10
            MAX_PAGES = 6

            # get number of pages bases on results found
            page_header = bsoup.find("div", {"class": "pager_bar"})
            results_found = re.search(r'\((.*?) encontradas\)', page_header.text).group(1) if page_header else 0
            logger.debug("Legendasdivx.pt :: Found %s subtitles", str(results_found))
            num_pages = (int(results_found) // 10) + 1
            num_pages = min(MAX_PAGES, num_pages)

            # process first page
            subtitles += self._process_page(video, bsoup)

            # more pages?
            if num_pages > 1:
                for num_page in range(2, num_pages + 1):
                    sleep(1) # another 1 sec before requesting...
                    _search_next = search_url + "&page={0}".format(str(num_page))
                    logger.debug("Legendasdivx.pt :: Moving on to next page: %s", _search_next)
                    # sleep for a 1 second before another request
                    sleep(1)
                    res = self.session.get(_search_next)
                    next_page = ParserBeautifulSoup(res.content, ['html.parser'])
                    subs = self._process_page(video, next_page)
                    subtitles.extend(subs)

        return subtitles

    def list_subtitles(self, video, languages):
        return self.query(video, languages)

    @reinitialize_on_error((RequestException,), attempts=1)
    def download_subtitle(self, subtitle):

        try:
            # sleep for a 1 second before another request
            sleep(1)
            res = self.session.get(subtitle.page_link)
            res.raise_for_status()
        except (AuthenticationError, ConfigurationError, IPAddressBlocked, TooManyRequests, DownloadLimitExceeded):
            raise
        except HTTPError as e:
            if "bloqueado" in res.text.lower():
                logger.error("LegendasDivx.pt :: Your IP is blocked on this server.")
                raise IPAddressBlocked("LegendasDivx.pt :: Your IP is blocked on this server.")
            logger.error("Legendasdivx.pt :: HTTP Error %s", e)
            raise TooManyRequests("Legendasdivx.pt :: HTTP Error %s", e)
        except Exception as e:
            logger.error("LegendasDivx.pt :: Uncaught error: %r", e)
            raise ServiceUnavailable("LegendasDivx.pt :: Uncaught error: %r", e)

        # make sure we haven't maxed out our daily limit
        if (res.status_code == 200 and 'limite de downloads diário atingido' in res.text.lower()):
            logger.error("LegendasDivx.pt :: Daily download limit reached!")
            raise DownloadLimitExceeded("Legendasdivx.pt :: Daily download limit reached!")

        archive = self._get_archive(res.content)
        if archive:
            subtitle_content = self._get_subtitle_from_archive(archive, res.content, subtitle)
        else:
            subtitle_content = self._extract_via_cli(res.content, video=subtitle.video)

        if subtitle_content:
            subtitle.content = fix_line_ending(subtitle_content)
            subtitle.normalize()
            return subtitle
        return

    def _get_archive(self, content):
        # open the archive
        archive_stream = io.BytesIO(content)
        if rarfile.is_rarfile(archive_stream):
            logger.debug('Legendasdivx.pt :: Identified rar archive')
            archive = rarfile.RarFile(archive_stream)
        elif zipfile.is_zipfile(archive_stream):
            logger.debug('Legendasdivx.pt :: Identified zip archive')
            archive = zipfile.ZipFile(archive_stream)
        else:
            logger.error('Legendasdivx.pt :: Unsupported compressed format')
            return None
        return archive

    def _read_from_archive(self, archive, content, target_name, video=None):
        try:
            return archive.read(target_name)
        except Exception as e:
            logger.warning("Legendasdivx.pt :: Direct archive.read failed (%s), attempting CLI extraction fallback", e)
            return self._extract_via_cli(content, target_name, video=video)

    @staticmethod
    def _extracted_usage(outdir):
        """(bytes, member count) written below outdir so far.

        lstat, not stat: a symlink member must not be charged the size of
        whatever it points at, and _is_safe_extracted_file drops those anyway.
        """
        total_bytes = 0
        members = 0
        for root, _, files in os.walk(outdir):
            for f in files:
                members += 1
                try:
                    total_bytes += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    continue

        return total_bytes, members

    def _over_extraction_budget(self, outdir, tool):
        used_bytes, members = self._extracted_usage(outdir)
        if used_bytes > CLI_EXTRACT_MAX_BYTES or members > CLI_EXTRACT_MAX_MEMBERS:
            logger.warning("Legendasdivx.pt :: %s blew the extraction budget "
                           "(%s bytes against a %s limit, %s members against %s), abandoning it",
                           tool, used_bytes, CLI_EXTRACT_MAX_BYTES, members, CLI_EXTRACT_MAX_MEMBERS)
            return True

        return False

    def _run_extractor(self, tool, cmd, outdir):
        """Run one extractor under the time, size and member budget.

        True only when it exited successfully inside the budget. Never raises: a
        hang, a blown budget or a failure to start is just this extractor not
        working out, and the caller moves on to the next one.
        """
        try:
            # stdin is closed so an extractor that prompts, which is what an
            # encrypted archive triggers, cannot block on an inherited terminal.
            # Output is discarded rather than piped: nothing reads it, and an
            # unread pipe would deadlock a long extraction once its buffer fills.
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as err:
            logger.warning("Legendasdivx.pt :: could not run %s: %s", tool, err)
            return False

        deadline = time.monotonic() + CLI_EXTRACT_TIMEOUT
        returncode = None
        try:
            while True:
                try:
                    returncode = proc.wait(timeout=CLI_EXTRACT_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    break

                if time.monotonic() >= deadline:
                    logger.warning("Legendasdivx.pt :: %s timed out after %ss extracting archive, "
                                   "trying next extractor", tool, CLI_EXTRACT_TIMEOUT)
                    return False

                if self._over_extraction_budget(outdir, tool):
                    return False
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        if returncode != 0:
            return False

        # a fast extractor can blow the budget between two polls
        return not self._over_extraction_budget(outdir, tool)

    @staticmethod
    def _is_safe_extracted_file(full_path, safe_root):
        """Reject anything that is not a regular file inside the temp directory.

        unar restores symlink members verbatim, absolute targets included, so an
        archive can ship a link named like a subtitle and have the extractor
        point it at any file the process can read. os.walk() does not descend
        into symlinked directories, but a symlinked file still shows up in
        files, and opening it reads the link target.
        """
        if os.path.islink(full_path):
            logger.warning("Legendasdivx.pt :: Skipping symlink member in archive: %s", full_path)
            return False

        real_path = os.path.realpath(full_path)
        if real_path != safe_root and not real_path.startswith(safe_root + os.sep):
            logger.warning("Legendasdivx.pt :: Skipping archive member escaping the temp directory: %s", full_path)
            return False

        if not os.path.isfile(real_path):
            logger.warning("Legendasdivx.pt :: Skipping non-regular archive member: %s", full_path)
            return False

        return True

    def _extract_via_cli(self, content, target_name=None, video=None):
        tmpdir = tempfile.mkdtemp()
        try:
            tmparc = os.path.join(tmpdir, "archive.rar")
            with open(tmparc, "wb") as f:
                f.write(content)

            # Extract into a directory of its own, beside the archive rather than
            # around it. Each extractor then gets a clean tree and its own budget,
            # instead of inheriting whatever a previous attempt left behind when
            # it timed out or overran.
            outdir = os.path.join(tmpdir, "extracted")

            extracted = False
            for tool, cmd in [
                ("unar", ["unar", "-o", outdir, "-f", tmparc]),
                ("7z", ["7z", "x", "-y", f"-o{outdir}", tmparc]),
                ("unrar", ["unrar", "x", "-y", tmparc, outdir]),
            ]:
                if not shutil.which(tool):
                    continue

                os.makedirs(outdir, exist_ok=True)
                if self._run_extractor(tool, cmd, outdir):
                    extracted = True
                    break

                shutil.rmtree(outdir, ignore_errors=True)

            if extracted:
                _tmp = list(SUBTITLE_EXTENSIONS)
                if ".txt" in _tmp:
                    _tmp.remove(".txt")
                _subtitle_extensions = tuple(_tmp)

                target_base = os.path.split(target_name)[-1].lower() if target_name else None
                safe_root = os.path.realpath(outdir)
                found_files = []
                for root, _, files in os.walk(outdir):
                    for f in files:
                        if not f.lower().endswith(_subtitle_extensions):
                            continue

                        full_path = os.path.join(root, f)
                        if not self._is_safe_extracted_file(full_path, safe_root):
                            continue

                        if target_base and f.lower() == target_base:
                            with open(full_path, "rb") as sf:
                                return sf.read()
                        found_files.append(full_path)

                if target_base:
                    # The caller named the member the scoring loop chose. Handing
                    # back a different one is how a subtitle for the wrong episode
                    # ends up in the library, marked done, with no error anywhere.
                    logger.error("Legendasdivx.pt :: %s not found in the extracted archive", target_name)
                    return None

                # No specific member was asked for, so drop anything the wanted
                # episode contradicts before falling back to the first one.
                found_files = [f for f in found_files
                               if name_matches_wanted_episode(video, os.path.split(f)[-1])]

                if found_files:
                    with open(found_files[0], "rb") as sf:
                        return sf.read()

                logger.error("Legendasdivx.pt :: No usable subtitle in the extracted archive")
        except Exception as err:
            logger.error("Legendasdivx.pt :: CLI extraction fallback failed: %s", err)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        return None

    def _get_subtitle_from_archive(self, archive, content, subtitle):
        # some files have a non subtitle with .txt extension
        _tmp = list(SUBTITLE_EXTENSIONS)
        if '.txt' in _tmp:
            _tmp.remove('.txt')
        _subtitle_extensions = tuple(_tmp)
        _max_score = -1
        _max_name = None
        _scores = get_scores(subtitle.video)

        candidate_files = []
        for name in archive.namelist():
            # discard hidden files and directories
            if os.path.split(name)[-1].startswith('.') or name.endswith('/'):
                continue

            # discard non-subtitle files
            if not name.lower().endswith(_subtitle_extensions):
                continue

            candidate_files.append(name)

        if not candidate_files:
            logger.error("Legendasdivx.pt :: No subtitle file found in archive")
            return self._extract_via_cli(content, video=subtitle.video)

        # If archive contains only 1 subtitle file, return it directly, but only
        # once it has passed the same episode check the scoring loop applies.
        # This is the common shape for this provider, one subtitle per upload,
        # so skipping the check here misfiles far more often than the pack path.
        if len(candidate_files) == 1:
            if not name_matches_wanted_episode(subtitle.video, candidate_files[0]):
                logger.error("Legendasdivx.pt :: Only subtitle in archive (%s) is not the wanted episode",
                             candidate_files[0])
                return None
            logger.debug("Legendasdivx.pt :: Only 1 subtitle in archive, returning: %s", candidate_files[0])
            return self._read_from_archive(archive, content, candidate_files[0], video=subtitle.video)

        for name in candidate_files:
            _guess = guessit(name)
            if not guess_matches_wanted_episode(subtitle.video, _guess):
                continue

            matches = set()
            matches |= guess_matches(subtitle.video, _guess)
            _score = sum((_scores.get(match, 0) for match in matches))
            if isinstance(subtitle.video, Episode) and _guess.get('episode') == subtitle.video.episode:
                _score += 10

            if _score > _max_score:
                _max_name = name
                _max_score = _score
                logger.debug("Legendasdivx.pt :: candidate %s scored %s", name, _score)

        if _max_name:
            logger.debug("Legendasdivx.pt :: returning from archive: %s (score %s)", _max_name, _max_score)
            return self._read_from_archive(archive, content, _max_name, video=subtitle.video)

        # Every candidate was rejected by the episode check. Returning
        # candidate_files[0] here hands back a subtitle for a different episode,
        # which is written to the library and marks the episode done with no
        # error anywhere. Nothing usable means nothing, same as the base did.
        logger.error("Legendasdivx.pt :: No subtitle in archive matches the wanted episode")
        return None

from __future__ import annotations
import logging
import os
from threading import Lock
from typing import Iterable
from urllib.parse import quote
from babelfish import Language
from subliminal.video import Episode, Movie, Video
from subliminal_patch.core_persistent import list_all_subtitles_parallel

from app.config import settings
from app.get_providers import get_provider_language_hook, get_providers_sorted, get_providers_auth
from . import auth, cache as C, response_mapper as M
from .local_subs import search_local
from utilities.url_guard import assert_safe_outbound, resolve_safe_url, UnsafeURLError  # noqa: F401

logger = logging.getLogger("bazarr.compat.service")

_pool_lock = Lock()
_compat_pool = None  # lazy singleton, dedicated (B2)
# Providers that consume the client-supplied OpenSubtitles-style moviehash.
# NapiProjekt is intentionally excluded: it keys on a different hash (md5 of the
# first 10 MB, a 32-char digest), and feeding it the 16-char OS hash makes its
# get_subhash() raise instead of matching.
_CLIENT_MOVIEHASH_PROVIDERS = (
    "bsplayer",
    "opensubtitles",
    "opensubtitlescom",
)


def _get_compat_pool():
    """Dedicated SZAsyncProviderPool instance. MUST NOT share app.get_providers._pools."""
    global _compat_pool
    with _pool_lock:
        if _compat_pool is None:
            from subliminal_patch.core import SZAsyncProviderPool
            _compat_pool = SZAsyncProviderPool(
                providers=get_providers_sorted(),
                provider_configs=get_providers_auth(),
                blacklist=None,
                ban_list=None,
                language_hook=get_provider_language_hook(),
                language_equals=[],
            )
        return _compat_pool


def reset_compat_pool() -> None:
    """Called on settings-change or provider toggle so stale creds don't persist."""
    global _compat_pool
    with _pool_lock:
        _compat_pool = None


def _tt(imdb_id) -> str:
    """Normalize an IMDb id to the 'tt' + digits form that external
    services (OMDB, TVDB v1, TVDB v4) uniformly require. Clients like the
    Jellyfin plugin strip 'tt' before sending it to us as an int-string;
    every outbound metadata call has to put it back.
    Returns '' for None / empty / unparseable."""
    if imdb_id is None:
        return ""
    s = str(imdb_id).strip().lower()
    if not s:
        return ""
    if s.startswith("tt"):
        return s
    return f"tt{s}" if s.isdigit() or s.lstrip("0").isdigit() else ""


def _lookup_library_metadata(imdb_id: str, media_type: str,
                             season: int | None = None,
                             episode: int | None = None) -> dict:
    """Best-effort title/year/tvdb_id/path resolution from the local Bazarr DB.

    Providers like supersubtitles and yifysubtitles score heavily on title
    match, so searching with a bare `imdb_id` and empty title returns few or
    irrelevant results. When the title is already in TableMovies/TableShows
    (almost always, since this is a library-backed deployment) we can hand
    the real title/year to the Video constructor and dramatically improve
    hit rate.

    When season+episode are supplied for episode searches, also resolves the
    per-episode file path and sceneName so _build_video can delegate to
    Bazarr's real parse_video pipeline (same scoring intelligence as the
    native manual search). Returns {} if the imdb_id is not in the library.
    """
    try:
        from app.database import (database, select, TableMovies,
                                  TableShows, TableEpisodes)
    except Exception:
        return {}
    imdb = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
    try:
        if media_type == "episode":
            show = database.execute(
                select(TableShows.sonarrSeriesId, TableShows.title,
                       TableShows.year, TableShows.tvdbId)
                .where(TableShows.imdbId == imdb)
            ).first()
            if not show:
                return {}
            out = {"title": show[1] or "", "year": show[2], "tvdb_id": show[3]}
            if season is not None and episode is not None:
                ep = database.execute(
                    select(TableEpisodes.path, TableEpisodes.sceneName,
                           TableEpisodes.title)
                    .where(TableEpisodes.sonarrSeriesId == show[0])
                    .where(TableEpisodes.season == int(season))
                    .where(TableEpisodes.episode == int(episode))
                ).first()
                if ep:
                    out["path"] = ep[0] or ""
                    out["sceneName"] = ep[1] or ""
                    out["episode_title"] = ep[2] or ""
            return out
        else:
            row = database.execute(
                select(TableMovies.title, TableMovies.year,
                       TableMovies.path, TableMovies.sceneName)
                .where(TableMovies.imdbId == imdb)
            ).first()
            if row:
                return {"title": row[0] or "", "year": row[1],
                        "path": row[2] or "", "sceneName": row[3] or ""}
    except Exception as e:
        logger.debug("compat library metadata lookup failed for %s: %s", imdb, e)
    return {}


def _guessit_filename(filename: str) -> dict:
    """Run guessit on a filename and return a plain dict of the interesting
    fields. Isolated from subliminal's Video.fromname so it doesn't raise
    when the type disagrees with what the client told us."""
    if not filename:
        return {}
    try:
        from subliminal_patch.core import guessit as _guessit
        g = _guessit(filename)
        return dict(g) if g else {}
    except Exception as e:
        logger.debug("compat guessit failed on %r: %s", filename, e)
        return {}


def _parse_video_from_library(path: str, meta: dict, media_type: str,
                              imdb_id: str, season: int | None,
                              episode: int | None,
                              moviehash: str | None,
                              moviebytesize: int | None = None) -> Video | None:
    """Build a Video via Bazarr's parse_video (same as native manual search).

    Returns None when the path is missing on disk or parse_video raises;
    the caller falls back to the virtual Video build in that case. On
    success the returned Video carries the rich metadata (release_group,
    source, resolution, codecs, and opensubtitles hashes when hashing is
    enabled) that ComputeScore needs to differentiate subtitle matches.
    """
    import os
    if not os.path.exists(path):
        return None
    try:
        from subtitles.utils import get_video
    except Exception as e:
        logger.debug("compat: get_video import failed: %s", e)
        return None
    title = meta.get("title") or ""
    scene_name = meta.get("sceneName") or "None"
    native_media_type = "series" if media_type == "episode" else "movie"
    try:
        v = get_video(path, title, scene_name,
                      providers=None, media_type=native_media_type)
    except Exception as e:
        logger.debug("compat: get_video failed for %r: %s", path, e)
        return None
    if v is None:
        return None

    # get_video populates from path analysis and refiners, but doesn't know
    # the caller-supplied identifiers. Attach them so providers that look
    # up by imdb/tvdb hit on a single match.
    if media_type == "episode":
        if not getattr(v, "series_imdb_id", None):
            v.series_imdb_id = imdb_id
        if not getattr(v, "season", None) and season is not None:
            v.season = int(season)
        if not getattr(v, "episode", None) and episode is not None:
            v.episode = int(episode)
        tvdb_id = meta.get("tvdb_id")
        if tvdb_id and not getattr(v, "series_tvdb_id", None):
            try:
                v.series_tvdb_id = int(tvdb_id)
            except (TypeError, ValueError):
                pass
    else:
        if not getattr(v, "imdb_id", None):
            v.imdb_id = imdb_id

    # Client-supplied hash wins over whatever parse_video computed; the
    # client already has the file open and its computation is canonical.
    _apply_client_moviehash(v, moviehash)
    if moviebytesize:
        v.size = int(moviebytesize)
    return v


def _apply_client_moviehash(video, moviehash: str | None = None):
    if not moviehash:
        return video
    hashes = dict(getattr(video, "hashes", {}) or {})
    for provider_id in _CLIENT_MOVIEHASH_PROVIDERS:
        hashes[provider_id] = str(moviehash)
    video.hashes = hashes
    return video


def _apply_anidb_ids(video, series_anidb_id: int | None = None,
                     series_anidb_episode_id: int | None = None):
    if series_anidb_id is not None:
        video.series_anidb_id = int(series_anidb_id)
        video.series_anidb_series_id = int(series_anidb_id)
    if series_anidb_episode_id is not None:
        video.series_anidb_episode_id = int(series_anidb_episode_id)
    return video


def _build_video(imdb_id: str, season: int | None, episode: int | None,
                 media_type: str, query: str | None = None,
                 moviehash: str | None = None,
                 moviebytesize: int | None = None,
                 series_anidb_id: int | None = None,
                 series_anidb_episode_id: int | None = None) -> Video:
    """Construct a Video for compat fanout.

    Preferred path: when the imdb_id resolves to a library entry with a
    real file on disk, delegate to Bazarr's parse_video pipeline so
    providers and ComputeScore get the same release_group / source /
    resolution / codecs / hash intelligence as Bazarr's native manual
    search UI. Attach imdb / tvdb identifiers that the library path
    doesn't populate so providers can still look up the title.

    Fallback (library miss OR library hit but file missing): build a
    virtual Video from whatever we can scrape, library metadata + guessit
    on the client's filename + OMDB/TVDB refiner lookups. Lower scoring
    signal but still better than nothing for query-only searches.
    """
    # Normalize up front: clients (Jellyfin plugin) strip 'tt' before
    # sending. OMDB / TVDB v1 / v4 all reject the bare numeric form, so
    # carrying the normalized value through the Video avoids having to
    # re-prepend in every downstream caller.
    imdb_id = _tt(imdb_id) or imdb_id
    meta = _lookup_library_metadata(imdb_id, media_type, season, episode)

    path = meta.get("path") or ""
    if path:
        real = _parse_video_from_library(path, meta, media_type,
                                          imdb_id, season, episode, moviehash,
                                          moviebytesize)
        if real is not None:
            return _apply_anidb_ids(real, series_anidb_id, series_anidb_episode_id)
        logger.debug("compat: library has path %r but parse_video failed; "
                     "falling back to virtual Video build", path)
    title = meta.get("title") or ""
    year_raw = meta.get("year")
    try:
        year = int(str(year_raw)[:4]) if year_raw else None
    except (TypeError, ValueError):
        year = None

    g = _guessit_filename(query) if query else {}
    # guessit may expose 'title' in a different shape than our library; prefer
    # the library title when both exist because it's been curated.
    g_title = g.get("title") or ""
    g_year = g.get("year")
    g_source = g.get("source")
    g_release_group = g.get("release_group")
    g_resolution = g.get("screen_size") or g.get("resolution")
    g_video_codec = g.get("video_codec")
    g_audio_codec = g.get("audio_codec")

    # Prefer the client's filename (it carries release info) as the Video.name,
    # since many providers match subtitle names against it fuzzily.
    name_fallback = query or None

    # Title precedence: library (curated) > refiner (OMDB/TVDB) > guessit.
    # guessit titles are unreliable noise when the filename is bad, so we
    # only fall back to them as a last resort.
    if media_type == "episode":
        name = name_fallback or (
            f"{title or g_title or imdb_id}.S{int(season or 0):02d}E{int(episode or 0):02d}.mkv"
        )
        v = Episode(
            name=name,
            series=title,  # will be filled by refiner then guessit if empty
            season=int(season or 0),
            episode=int(episode or 0),
            series_imdb_id=imdb_id,
            year=year,
            source=g_source,
            release_group=g_release_group,
            resolution=g_resolution,
            video_codec=g_video_codec,
            audio_codec=g_audio_codec,
        )
        tvdb_id = meta.get("tvdb_id")
        if tvdb_id:
            try:
                v.series_tvdb_id = int(tvdb_id)
            except (TypeError, ValueError):
                pass
    else:
        name = name_fallback or (
            f"{title or imdb_id}.{year}.mkv" if year
            else f"{title or imdb_id}.mkv"
        )
        v = Movie(
            name=name,
            title=title,  # will be filled by refiner then guessit if empty
            year=year,
            imdb_id=imdb_id,
            source=g_source,
            release_group=g_release_group,
            resolution=g_resolution,
            video_codec=g_video_codec,
            audio_codec=g_audio_codec,
        )
    v.size = int(moviebytesize) if moviebytesize else None
    # Client-supplied hashes give hash-based providers an exact-match path
    # when the client knows the provider-specific value.
    v.hashes = {}
    _apply_client_moviehash(v, moviehash)

    _apply_anidb_ids(v, series_anidb_id, series_anidb_episode_id)

    # Library miss -> try OMDB/TVDB refiners (network, best-effort).
    if not getattr(v, "title", None) and not getattr(v, "series", None):
        _refine_from_imdb(v, media_type)

    # Refiner miss -> last-resort guessit title. Do NOT clobber a real value.
    if media_type == "episode":
        if not getattr(v, "series", None) and g_title:
            v.series = g_title
    else:
        if not getattr(v, "title", None) and g_title:
            v.title = g_title
    if not getattr(v, "year", None) and g_year:
        v.year = g_year
    return v


def _refine_from_imdb(video, media_type: str) -> None:
    """Best-effort network lookup to populate title/year/tvdb_id when the
    local library doesn't know the imdb_id. Swallows all exceptions.

    Episode resolution path (fastest first):
      1. TVDB v4 by imdb_id - native episode support via /search/remoteid.
         Works whether the client sent the series' imdb or the episode's.
      2. OMDB bridge - translates episode-imdb to series-imdb via OMDB's
         `seriesID` field, then hits subliminal's TVDB v1 by series imdb.
      3. Subliminal's TVDB v1 series-imdb search - final fallback.

    For movies we just hit OMDB directly (TVDB v4 also supports movies
    but OMDB has better coverage for movie-imdb lookups in practice).
    """
    if media_type == "episode":
        if _tvdb_v4_episode_lookup(video):
            return
        series_imdb = _omdb_episode_to_series_imdb(video)
        if series_imdb:
            video.series_imdb_id = series_imdb
        _tvdb_lookup_by_imdb(video)
    else:
        _omdb_lookup_by_imdb(video)


def _tvdb_v4_episode_lookup(video) -> bool:
    """Resolve an episode via TVDB v4's /search/remoteid endpoint.

    Returns True if we populated at least series_tvdb_id; False on any
    failure or no-match. Hydrates series name/year from subliminal's v1
    get_series() once we have the numeric series_tvdb_id, since v4's
    search result omits series fields when the imdb maps to an episode.
    """
    try:
        from subliminal_patch.refiners import tvdb_v4
        imdb = getattr(video, "series_imdb_id", None) or getattr(video, "imdb_id", None)
        if not imdb:
            return False
        match = tvdb_v4.get_client().search_by_imdb_id(imdb)
        if not match:
            return False

        series_id = None
        if "episode" in match and isinstance(match["episode"], dict):
            ep = match["episode"]
            series_id = ep.get("seriesId")
            episode_id = ep.get("id")
            if not getattr(video, "tvdb_id", None) and episode_id:
                try:
                    video.tvdb_id = int(episode_id)
                except (TypeError, ValueError):
                    pass
            if not getattr(video, "title", None) and ep.get("name"):
                video.title = ep["name"]
            aired = ep.get("aired") or ""
            if aired and not getattr(video, "year", None):
                try:
                    video.year = int(aired[:4])
                except (TypeError, ValueError):
                    pass
            # /search/remoteid returns episode stubs with seasonNumber /
            # number set to null. Fetch the full record so season/episode
            # numbers land on the video, which the response mapper reads
            # for feature_details - otherwise clients that filter results
            # by (season, episode) see all zeros and drop every hit.
            if episode_id and (not getattr(video, "season", None)
                               or not getattr(video, "episode", None)):
                try:
                    full_ep = tvdb_v4.get_client().get_episode(int(episode_id))
                except (TypeError, ValueError):
                    full_ep = None
                if full_ep:
                    if not getattr(video, "season", None):
                        sn = full_ep.get("seasonNumber")
                        if sn is not None:
                            try:
                                video.season = int(sn)
                            except (TypeError, ValueError):
                                pass
                    if not getattr(video, "episode", None):
                        en = full_ep.get("number")
                        if en is not None:
                            try:
                                video.episode = int(en)
                            except (TypeError, ValueError):
                                pass
                    if not getattr(video, "title", None) and full_ep.get("name"):
                        video.title = full_ep["name"]
        elif "series" in match and isinstance(match["series"], dict):
            s = match["series"]
            series_id = s.get("id")
            if not getattr(video, "series", None) and s.get("name"):
                video.series = s["name"]
            fa = s.get("firstAired") or ""
            if fa and not getattr(video, "year", None):
                try:
                    video.year = int(fa[:4])
                except (TypeError, ValueError):
                    pass
        else:
            return False

        if not series_id:
            return False
        try:
            video.series_tvdb_id = int(series_id)
        except (TypeError, ValueError):
            return False

        # Hydrate series name/year via subliminal's v1 get_series if we
        # don't have them yet (v4's episode match omits series fields).
        if not getattr(video, "series", None) or not getattr(video, "year", None):
            try:
                from subliminal.refiners.tvdb import tvdb_client
                s_data = tvdb_client.get_series(int(series_id))
                if s_data:
                    if not getattr(video, "series", None):
                        video.series = s_data.get("seriesName") or ""
                    if not getattr(video, "year", None):
                        fa = s_data.get("firstAired") or ""
                        try:
                            video.year = int(fa[:4])
                        except (TypeError, ValueError):
                            pass
            except Exception as e:
                logger.debug("v1 get_series hydrate failed: %s", e)
        return True
    except Exception as e:
        logger.debug("TVDB v4 episode lookup failed: %s", e)
        return False


def _omdb_episode_to_series_imdb(video) -> str | None:
    """When the client sends the episode's imdb_id, OMDB tells us the
    series' imdb_id via the `seriesID` field. Also populates year /
    episode title as a side-effect. Returns the series imdb ('tt...') or
    None on any failure / no-key. Safe to call without OMDB configured."""
    try:
        from subliminal_patch.refiners.omdb import _resolve_omdb_apikey
        apikey = _resolve_omdb_apikey()
        if not apikey:
            return None
        imdb = _tt(getattr(video, "series_imdb_id", None)
                   or getattr(video, "imdb_id", None))
        if not imdb:
            return None
        import requests
        r = requests.get("https://www.omdbapi.com/",
                         params={"i": imdb, "apikey": apikey},
                         timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("Response") != "True":
            return None
        # Year on the episode record is the air year; good enough as a
        # fallback when TVDB doesn't know the show.
        if not getattr(video, "year", None):
            try:
                video.year = int(str(data.get("Year", ""))[:4])
            except (TypeError, ValueError):
                pass
        # Episode record carries the episode title; the series title we'll
        # get from TVDB if we can.
        if not getattr(video, "title", None):
            video.title = data.get("Title") or None
        # OMDB episode records carry Season / Episode numbers as strings;
        # populate them when the client didn't send season_number /
        # episode_number (which is exactly when we took this path).
        if not getattr(video, "season", None):
            try:
                season = int(data.get("Season", "") or 0)
                if season:
                    video.season = season
            except (TypeError, ValueError):
                pass
        if not getattr(video, "episode", None):
            try:
                episode = int(data.get("Episode", "") or 0)
                if episode:
                    video.episode = episode
            except (TypeError, ValueError):
                pass
        series_id = data.get("seriesID")
        if series_id and str(series_id).startswith("tt"):
            return str(series_id)
        # If OMDB says this is actually a series root (not an episode),
        # the caller's series_imdb_id is already correct.
        return None
    except Exception as e:
        logger.debug("compat OMDB episode->series lookup failed: %s", e)
        return None


def _omdb_lookup_by_imdb(video) -> None:
    """Query OMDB by imdb_id directly (`?i=tt...`) and populate title/year.
    Requires settings.omdb.apikey or OMDB_API_KEY env. No-op without a key."""
    try:
        from subliminal_patch.refiners.omdb import _resolve_omdb_apikey
        apikey = _resolve_omdb_apikey()
        if not apikey:
            return
        imdb = _tt(getattr(video, "imdb_id", None))
        if not imdb:
            return
        import requests
        r = requests.get("https://www.omdbapi.com/",
                         params={"i": imdb, "apikey": apikey},
                         timeout=5)
        if r.status_code != 200:
            return
        data = r.json()
        if data.get("Response") != "True":
            return
        if not getattr(video, "title", None):
            video.title = data.get("Title") or ""
        if not getattr(video, "year", None):
            try:
                video.year = int(str(data.get("Year", ""))[:4])
            except (TypeError, ValueError):
                pass
    except Exception as e:
        logger.debug("compat OMDB-by-imdb lookup failed: %s", e)


def _tvdb_lookup_by_imdb(video) -> None:
    """Query TVDB with series_imdb_id (not series name). Populates
    video.series_tvdb_id, video.series, video.year and the episode's tvdb_id
    when available. Subliminal's stock TVDB refiner requires a series name,
    which we don't have on library miss."""
    try:
        from subliminal.refiners.tvdb import tvdb_client
        imdb = _tt(getattr(video, "series_imdb_id", None)
                   or getattr(video, "imdb_id", None))
        if not imdb:
            return
        # TVDB v1 search rejects bare numeric ids; always send tt-prefixed.
        results = tvdb_client.search_series(imdb_id=imdb)
        if not results:
            return
        series = results[0]
        tvdb_id = series.get("id")
        if not tvdb_id:
            return
        video.series_tvdb_id = int(tvdb_id)
        if not getattr(video, "series", None):
            video.series = series.get("seriesName") or ""
        first_aired = series.get("firstAired") or ""
        if first_aired and not getattr(video, "year", None):
            try:
                video.year = int(first_aired[:4])
            except (TypeError, ValueError):
                pass
        if getattr(video, "season", None) and getattr(video, "episode", None):
            ep = tvdb_client.get_series_episode(tvdb_id, int(video.season), int(video.episode))
            if ep:
                if not getattr(video, "tvdb_id", None):
                    video.tvdb_id = ep.get("id")
                if not getattr(video, "title", None):
                    video.title = ep.get("episodeName") or None
    except Exception as e:
        logger.debug("compat TVDB-by-imdb lookup failed: %s", e)


# Providers whose check() passes for a virtual video but that physically
# cannot produce results without the file on disk. Skipping them at fanout
# time frees a thread-pool slot and cuts wall time.
_SKIP_FOR_VIRTUAL_VIDEO = frozenset({"embeddedsubtitles"})


def _do_fanout(imdb_id, season, episode, languages, media_type,
               query=None, moviehash=None, moviebytesize=None,
               series_anidb_id=None, series_anidb_episode_id=None,
               moviehash_match=None,
               requested_languages=None):
    from subliminal_patch.provider_health import get_tracker as _get_health_tracker
    from subliminal_patch.score import ComputeScore, MAX_SCORES
    health = _get_health_tracker()
    pool = _get_compat_pool()
    video = _build_video(imdb_id, season, episode, media_type,
                         query=query, moviehash=moviehash,
                         moviebytesize=moviebytesize,
                         series_anidb_id=series_anidb_id,
                         series_anidb_episode_id=series_anidb_episode_id)
    health_discarded = health.currently_discarded()
    video_has_file = bool(getattr(video, "name", None)
                          and os.path.exists(getattr(video, "name", "")))
    exclude = health_discarded | (set() if video_has_file else set(_SKIP_FOR_VIRTUAL_VIDEO))
    logger.info("compat fanout: video=%r lang=%s providers=%d health_skipped=%s",
                video, [str(l) for l in languages], len(pool.providers),  # noqa: E741
                sorted(health_discarded) or "[]")

    stats: dict[str, tuple[str, int]] = {}

    def _on_result(name, outcome, latency_ms):
        stats[name] = (outcome, latency_ms)
        health.record(name, outcome, latency_ms)

    wall = int(settings.compat_endpoint.search_timeout_seconds)
    per_provider = max(3, int(wall * 0.6))
    results = list_all_subtitles_parallel(
        [video], set(languages), pool,
        per_provider_timeout=per_provider,
        wall_timeout=wall,
        exclude_providers=exclude,
        on_result=_on_result,
    )

    if stats:
        compact = ", ".join(f"{n}={o}:{l}ms"
                            for n, (o, l) in sorted(stats.items()))  # noqa: E741
        logger.info("compat fanout complete: %s", compact)

    subs = []
    for v, sub_list in results.items():  # noqa: PERF102
        subs.extend(sub_list)

    # moviehash_match filtering: "only" drops every non-hash row. This
    # is what makes Jellyfin's "perfect match" toggle work: without the
    # filter, plugin sees all results and filters client-side on the
    # all-False moviehash_match field, which historically eliminated
    # everything.
    def _has_hash(s):
        try:
            return "hash" in (getattr(s, "matches", None) or set())
        except TypeError:
            return False

    if moviehash_match == "only":
        subs = [s for s in subs if _has_hash(s)]

    # Project Bazarr's real score into attributes.ratings so the plugin's
    # sort (moviehash_match -> download_count -> ratings) has something
    # meaningful beyond the first two keys.
    compute = ComputeScore()
    max_score = MAX_SCORES["episode" if media_type == "episode" else "movie"]

    # Pick a single requested_language for the mapper. The plugin sends
    # one language per search anyway (BCP-47), so when there's only one
    # entry we preserve its region subtag.
    req_lang_map = _build_requested_language_map(requested_languages or [])

    entries = []
    for sub in subs:
        file_id = auth.mint_file_id(
            provider=getattr(sub, "provider_name", "unknown"),
            native_id=getattr(sub, "id", ""),
            language=str(getattr(sub, "language", "")),
            release_info=getattr(sub, "release_info", "") or "",
            subtitle=sub,
        )
        try:
            matches = sub.get_matches(video) if hasattr(sub, "get_matches") \
                      else (getattr(sub, "matches", None) or set())
        except Exception:
            matches = getattr(sub, "matches", None) or set()
        try:
            score, _sc_no_hash = compute(matches, sub, video)
        except Exception:
            score = 0
        # Popularity augment: when the virtual video has no source file,
        # ComputeScore collapses every sub with the same matches to the
        # same number, so the plugin's tertiary sort (ratings) has nothing
        # to rank on. A log-scaled download_count boost differentiates
        # community-validated subs without ever exceeding a real
        # release_group match (20 pts). Subs with no count stay at raw
        # score.
        try:
            dc = int(getattr(sub, "download_count", 0) or 0)
            if dc > 0:
                import math
                score = int(score) + min(20, int(math.log10(dc + 1) * 4))
        except (TypeError, ValueError):
            pass
        sub_lang = getattr(sub, "language", None)
        sub_alpha2 = getattr(sub_lang, "alpha2", None) or ""
        req_lang = req_lang_map.get(sub_alpha2)
        if req_lang and "-" in req_lang:
            sub_country = getattr(getattr(sub_lang, "country", None), "alpha2", None) or ""
            req_region = req_lang.split("-", 1)[1].upper()
            if sub_country and sub_country.upper() != req_region:
                req_lang = None
        entries.append(M.subtitle_to_os_entry(
            sub, file_id, media_type, imdb_id, season, episode,
            video=video,
            hash_matched=_has_hash(sub),
            score=(int(score), int(max_score)),
            requested_language=req_lang,
        ))
    # Surface locally-stored subtitles alongside provider results when the
    # operator hasn't disabled the feature. Locals carry a synthetic high
    # download_count, so the existing single-key sort below pins them to
    # the top of the list naturally.
    if bool(settings.compat_endpoint.serve_local_subs):
        try:
            local_entries = search_local(
                imdb_id=imdb_id, season=season, episode=episode,
                media_type=media_type,
                languages=requested_languages or [],
                query=query, moviehash=moviehash,
                moviehash_match=moviehash_match,
            )
        except Exception as e:
            logger.warning("compat: search_local failed (continuing without locals): %s", e)
            local_entries = []
        if local_entries:
            entries = local_entries + entries
    entries.sort(key=lambda e: -int(e["attributes"].get("download_count", 0)))
    return M.search_envelope(entries, per_page=50, page=1)


def _build_requested_language_map(requested_languages: list[str]) -> dict:
    """Map alpha2 -> original BCP-47 code so mapper can preserve region
    subtags like zh-CN.

    When the caller sends multiple variants of the same base (e.g.
    'zh-CN,zh-TW'), we can't safely tag every returned Chinese sub with
    one specific region, so we drop the override for that base and let
    the mapper emit the bare alpha2. The plugin's match is
    case-insensitive on alpha2, so this degrades correctly.
    """
    collected: dict[str, list[str]] = {}
    for code in requested_languages:
        if not code:
            continue
        base = code.split("-", 1)[0].lower()
        collected.setdefault(base, []).append(code)
    out: dict[str, str] = {}
    for base, codes in collected.items():
        if len(codes) == 1:
            out[base] = codes[0]
    return out


def search(imdb_id: str, season, episode, languages: Iterable[Language],
           media_type: str, query: str | None = None,
           moviehash: str | None = None,
           moviebytesize: int | None = None,
           series_anidb_id: int | None = None,
           series_anidb_episode_id: int | None = None,
           moviehash_match: str | None = None,
           requested_languages: list[str] | None = None) -> dict:
    enabled = get_providers_sorted()
    key = C.build_key(media_type, imdb_id, season, episode, languages, enabled,
                      query=query, moviehash=moviehash,
                      moviebytesize=moviebytesize,
                      series_anidb_id=series_anidb_id,
                      series_anidb_episode_id=series_anidb_episode_id,
                      moviehash_match=moviehash_match,
                      requested_languages=requested_languages)
    cache_ttl = int(settings.compat_endpoint.cache_ttl_seconds)
    fid_ttl = int(settings.compat_endpoint.file_id_ttl_seconds)
    ttl = min(cache_ttl, fid_ttl)
    return C.compat_region.get_or_create(
        key,
        creator=lambda: _do_fanout(imdb_id, season, episode, languages,
                                    media_type, query=query, moviehash=moviehash,
                                    moviebytesize=moviebytesize,
                                    series_anidb_id=series_anidb_id,
                                    series_anidb_episode_id=series_anidb_episode_id,
                                    moviehash_match=moviehash_match,
                                    requested_languages=requested_languages),
        expiration_time=ttl,
    )


def download(file_id, base_host: str = "",
             remaining: int = 0, reset_iso: str = "") -> dict:
    """Resolve the int file_id, mint a short-lived stream token, return a
    download link. No provider fetch happens here - the subtitle is fetched
    only when the client follows the link.

    The link is relative by default (starts with `/api/v1/...`) because
    Bazarr+ can't reliably determine its own public URL behind its supervisor
    proxy. Clients already know the host they connected to. Pass base_host
    to override for callers that want an absolute URL.
    """
    ok, _payload = auth.parse_file_id(file_id)
    if not ok:
        raise FileNotFoundError("file_id invalid or expired")
    stream_tok = auth.mint_file_stream_token(int(file_id))
    base_url = (settings.general.base_url or "").rstrip("/")
    path = f"{base_url}/api/v1/download/stream/{quote(stream_tok, safe='')}"
    link = f"{base_host.rstrip('/')}{path}" if base_host else path
    return M.download_response(link, remaining=remaining, reset_iso=reset_iso)


def _fetch_subtitle_bytes(sub) -> bytes:
    """SSRF-guard the provider URL and invoke pool.download_subtitle(sub).

    `sub` is the original Subtitle instance retained from search (stashed in
    the file_id store). Downloading via the pool is the only portable path:
    each provider's `download_subtitle(sub)` knows exactly how to resolve its
    own subtitle's URL, auth, and content encoding - most providers do not
    expose a generic `get_subtitle_by_id` method.
    """
    if sub is None:
        raise FileNotFoundError("subtitle payload missing")

    # SSRF guard: only apply when download_link/url is a full http(s) URL.
    # Many providers store an internal reference (native id, zip name, etc.)
    # in these fields and construct the actual fetch URL inside their own
    # download_subtitle(). Running the guard on a non-URL string would reject
    # perfectly legitimate providers for the wrong reason.
    #
    # Scheme detection is case-insensitive: HTTP clients treat schemes as
    # case-insensitive per RFC 3986, so a hostile provider returning
    # `HTTP://169.254.169.254/...` (uppercase) must NOT slip past the
    # guard just because `startswith` is byte-exact. The request would
    # still hit the cloud-metadata endpoint via pool.download_subtitle()
    # with no SSRF check applied.
    #
    # Also walk the redirect chain via HEAD before invoking the pool. The
    # provider HTTP client follows redirects automatically, so a public
    # URL that 30x-redirects to 127.0.0.1 or 169.254.169.254 would
    # otherwise fetch the private target with no per-hop SSRF check.
    # Pre-resolve every Location and refuse any unsafe hop.
    url = getattr(sub, "download_link", None) or getattr(sub, "url", None)
    if isinstance(url, str) and url[:8].lower().startswith(("http://", "https://")):
        resolve_safe_url(url)  # raises UnsafeURLError on any unsafe hop

    provider_name = getattr(sub, "provider_name", None)
    if not provider_name:
        raise FileNotFoundError("subtitle has no provider_name")

    pool = _get_compat_pool()
    try:
        pool.download_subtitle(sub)
    except Exception as e:
        logger.exception("compat: download_subtitle failed for %s: %s",
                         provider_name, e)
        raise

    # Re-validate after download: some providers follow redirects that
    # could land on a private/loopback address, bypassing the pre-download
    # SSRF guard. Check the post-download URL if the provider updated it.
    post_url = getattr(sub, "download_link", None) or getattr(sub, "url", None)
    if (isinstance(post_url, str)
            and post_url[:8].lower().startswith(("http://", "https://"))
            and post_url != url):
        assert_safe_outbound(post_url)
    content = getattr(sub, "content", None)
    if not content:
        # Plugin contract: 200 + empty body = "broken subtitle, blocklist
        # this file_id and don't retry". Returning FileNotFoundError ->
        # 404 breaks that signal and the plugin retries on every scan.
        return b""
    return content


def serve_subtitle_content(stream_token: str) -> tuple[bytes, str]:
    """Validate the stream token and return the subtitle bytes + content-type.

    Raises:
        ValueError: stream token invalid or expired
        FileNotFoundError: subtitle not found
        UnsafeURLError: provider URL failed SSRF guard
    """
    ok, payload = auth.parse_file_stream_token(stream_token)
    if not ok:
        raise ValueError("stream token invalid or expired")
    fid = payload.get("fid")
    if fid is None:
        raise ValueError("stream token missing file_id")
    ok, fpayload = auth.parse_file_id(fid)
    if not ok:
        raise FileNotFoundError("file_id expired or not found")

    # Local-library payloads carry an explicit kind discriminator; serve
    # them from disk (with on-the-fly format conversion) instead of the
    # provider fanout pool.
    if fpayload.get("kind") == "local":
        from .local_subs import serve_local
        return serve_local(fpayload)

    sub = fpayload.get("sub")
    # Surface the guard symbol so tests can patch it.
    _ = assert_safe_outbound
    blob = _fetch_subtitle_bytes(sub)
    return blob, "application/x-subrip"


def guessit_filename(filename: str) -> dict:
    """Thin wrapper over guessit. Returns a JSON-safe dict.

    Rejects null bytes and filenames >512 chars (defense against path abuse).
    """
    if not filename or "\x00" in filename:
        raise ValueError("filename contains null byte or is empty")
    if len(filename) > 512:
        raise ValueError("filename too long")
    from subliminal_patch.core import guessit as _guessit
    result = _guessit(filename)
    # guessit returns a MatchesDict; coerce to plain dict with JSON-friendly values.
    import json
    from guessit.jsonutils import GuessitEncoder
    return json.loads(json.dumps(dict(result), cls=GuessitEncoder))

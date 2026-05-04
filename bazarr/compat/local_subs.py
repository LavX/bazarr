"""Library-side subtitle resolution and serving for the compat endpoint.

DO NOT import from bazarr.subtitles.manual, bazarr.subtitles.indexer, or
bazarr/api/subtitles/*. The compat surface is isolated by design (see
bazarr/compat/__init__.py); this module re-implements the small slice of
DB lookup and path-safety logic it needs inline.
"""
from __future__ import annotations

import os
import struct
import threading
from collections import OrderedDict


_CHUNK_SIZE = 64 * 1024  # 64 KB - OpenSubtitles algorithm constant


def _opensubtitles_hash(path: str) -> str:
    """Compute the OpenSubtitles file hash.

    Algorithm: read first 64KB and last 64KB, sum as little-endian uint64
    chunks plus the file size, mod 2^64. Returns 16-char lowercase hex.
    """
    size = os.path.getsize(path)
    h = size & 0xFFFFFFFFFFFFFFFF

    with open(path, "rb") as f:
        head = f.read(min(_CHUNK_SIZE, size))
        for i in range(0, len(head) - 7, 8):
            h = (h + struct.unpack_from("<Q", head, i)[0]) & 0xFFFFFFFFFFFFFFFF
        if size > _CHUNK_SIZE:
            f.seek(max(0, size - _CHUNK_SIZE))
            tail = f.read(_CHUNK_SIZE)
            for i in range(0, len(tail) - 7, 8):
                h = (h + struct.unpack_from("<Q", tail, i)[0]) & 0xFFFFFFFFFFFFFFFF

    return f"{h:016x}"


class _HashCache:
    """In-memory LRU: (realpath, mtime_ns, size) -> oshash hex string.

    Stat-on-every-get auto-invalidates when (mtime_ns, size) drift. Bounded
    LRU; lifetime = process; restart flushes (acceptable, first cold lookup
    recomputes).
    """

    def __init__(self, max_entries: int = 5000):
        self._lock = threading.Lock()
        self._max = max_entries
        self._store: "OrderedDict[tuple, str]" = OrderedDict()

    def get(self, path: str) -> str | None:
        try:
            real = os.path.realpath(path)
            st = os.stat(real)
        except (OSError, ValueError):
            return None
        key = (real, st.st_mtime_ns, st.st_size)
        with self._lock:
            cached = self._store.get(key)
            if cached is not None:
                self._store.move_to_end(key)
                return cached
        try:
            h = _opensubtitles_hash(real)
        except OSError:
            return None
        with self._lock:
            self._store[key] = h
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)
        return h

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


_hash_cache = _HashCache()


import logging

logger = logging.getLogger("bazarr.compat.local_subs")


def _tt(imdb_id: str | None) -> str:
    if not imdb_id:
        return ""
    s = str(imdb_id).strip().lower()
    if not s:
        return ""
    if s.startswith("tt"):
        return s
    return f"tt{s}" if s.lstrip("0").isdigit() or s.isdigit() else ""


def _resolve_by_imdb(imdb_id: str, season: int | None, episode: int | None,
                    media_type: str) -> tuple[str, int] | None:
    from app.database import select, TableMovies, TableShows, TableEpisodes
    imdb = _tt(imdb_id)
    if not imdb:
        return None
    try:
        if media_type == "episode":
            show = database.execute(
                select(TableShows.sonarrSeriesId)
                .where(TableShows.imdbId == imdb)
            ).first()
            if not show:
                return None
            ep = database.execute(
                select(TableEpisodes.sonarrEpisodeId)
                .where(TableEpisodes.sonarrSeriesId == show.sonarrSeriesId)
                .where(TableEpisodes.season == int(season or 0))
                .where(TableEpisodes.episode == int(episode or 0))
            ).first()
            return ("episode", int(ep.sonarrEpisodeId)) if ep else None
        else:
            row = database.execute(
                select(TableMovies.radarrId)
                .where(TableMovies.imdbId == imdb)
            ).first()
            return ("movie", int(row.radarrId)) if row else None
    except Exception as e:
        logger.debug("compat local: imdb resolve failed: %s", e)
        return None


def _guessit_filename(filename: str) -> dict:
    """Run guessit; return plain dict (never raises)."""
    if not filename:
        return {}
    try:
        from subliminal_patch.core import guessit as _g
        return dict(_g(filename) or {})
    except Exception as e:
        logger.debug("compat local: guessit failed on %r: %s", filename, e)
        return {}


def _resolve_by_query(query: str, media_type: str) -> tuple[str, int] | None:
    from app.database import select, TableMovies, TableShows, TableEpisodes
    g = _guessit_filename(query)
    title = (g.get("title") or "").strip()
    if not title:
        return None
    try:
        if media_type == "episode":
            season = g.get("season")
            episode = g.get("episode")
            if season is None or episode is None:
                return None
            show = database.execute(
                select(TableShows.sonarrSeriesId)
                .where(TableShows.title.ilike(title))
            ).first()
            if not show:
                show = database.execute(
                    select(TableShows.sonarrSeriesId)
                    .where(TableShows.alternativeTitles.ilike(f"%{title}%"))
                ).first()
            if not show:
                return None
            ep = database.execute(
                select(TableEpisodes.sonarrEpisodeId)
                .where(TableEpisodes.sonarrSeriesId == show.sonarrSeriesId)
                .where(TableEpisodes.season == int(season))
                .where(TableEpisodes.episode == int(episode))
            ).first()
            return ("episode", int(ep.sonarrEpisodeId)) if ep else None
        else:
            year = g.get("year")
            rows = database.execute(
                select(TableMovies.radarrId, TableMovies.year)
                .where(TableMovies.title.ilike(title))
            ).all()
            if not rows:
                return None
            if year is not None:
                year_str = str(year)
                for r in rows:
                    if str(r.year) == year_str:
                        return ("movie", int(r.radarrId))
            return ("movie", int(rows[0].radarrId))
    except Exception as e:
        logger.debug("compat local: query resolve failed: %s", e)
        return None


try:
    from utilities.path_mappings import path_mappings
except Exception:
    path_mappings = None


def _resolve_by_moviehash(moviehash: str, media_type: str) -> tuple[str, int] | None:
    if not moviehash:
        return None
    target = str(moviehash).strip().lower()
    if not target or len(target) != 16:
        return None
    from app.database import select, TableMovies, TableEpisodes
    try:
        if media_type == "episode":
            rows = database.execute(
                select(TableEpisodes.sonarrEpisodeId, TableEpisodes.path)
            ).all()
            for r in rows:
                local = path_mappings.path_replace(r.path) if path_mappings else r.path
                h = _hash_cache.get(local)
                if h and h.lower() == target:
                    return ("episode", int(r.sonarrEpisodeId))
            return None
        else:
            rows = database.execute(
                select(TableMovies.radarrId, TableMovies.path)
            ).all()
            for r in rows:
                local = path_mappings.path_replace_movie(r.path) if path_mappings else r.path
                h = _hash_cache.get(local)
                if h and h.lower() == target:
                    return ("movie", int(r.radarrId))
            return None
    except Exception as e:
        logger.debug("compat local: moviehash resolve failed: %s", e)
        return None


def _resolve_media(imdb_id: str | None, season: int | None,
                   episode: int | None, media_type: str,
                   query: str | None, moviehash: str | None) -> tuple[str, int] | None:
    if imdb_id:
        hit = _resolve_by_imdb(imdb_id, season, episode, media_type)
        if hit:
            return hit
    if query:
        hit = _resolve_by_query(query, media_type)
        if hit:
            return hit
    if moviehash:
        hit = _resolve_by_moviehash(moviehash, media_type)
        if hit:
            return hit
    return None


import ast as _ast

# Bound through a local name so the call site is `_parse_literal(raw)`,
# matching the same safe-parse contract Bazarr uses elsewhere
# (see bazarr/api/utils.py for the same pattern on `subtitles` / `tags`).
_parse_literal = _ast.literal_eval


_CONVERTIBLE_FORMATS = frozenset({"srt", "ass", "ssa", "vtt", "sub", "smi", "ttml", "dfxp"})


def _parse_subtitles_blob(raw) -> list:
    """Parse Bazarr's repr-encoded `subtitles` column. Returns [] on any
    failure. Wrapper exists so tests can mock the parser at one place."""
    if not raw:
        return []
    try:
        items = _parse_literal(raw)
    except (ValueError, SyntaxError):
        return []
    return items if isinstance(items, list) else []


def _parse_lang_code(code: str) -> tuple[str, str | None]:
    """Parse a Bazarr lang code into (base, modifier).

    "en"           -> ("en", None)
    "en:hi"        -> ("en", "hi")
    "pt-BR:forced" -> ("pt-BR", "forced")
    """
    if ":" in code:
        base, mod = code.split(":", 1)
    else:
        base, mod = code, None
    if mod and mod not in ("hi", "forced"):
        mod = None
    return base, mod


def _parse_request_bcp47(code: str) -> tuple[str, str | None]:
    """Split a BCP-47 request code into (base_alpha2, region)."""
    if "-" in code:
        base, region = code.split("-", 1)
        return base.lower(), region.upper()
    return code.lower(), None


def _lang_matches(entry_base: str, request_base: str,
                  request_region: str | None) -> bool:
    e_parts = entry_base.split("-", 1)
    e_base = e_parts[0].lower()
    e_region = e_parts[1].upper() if len(e_parts) > 1 else None
    if e_base != request_base.lower():
        return False
    if request_region is None:
        return True
    return e_region == request_region.upper()


def _resolve_format(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext if ext in _CONVERTIBLE_FORMATS else None


# Module-level `database` symbol so tests can patch via
# `compat.local_subs.database`. Real reference imported lazily.
try:
    from app.database import database
except Exception:
    database = None

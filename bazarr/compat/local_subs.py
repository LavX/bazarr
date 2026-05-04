"""Library-side subtitle resolution and serving for the compat endpoint.

DO NOT import from bazarr.subtitles.manual, bazarr.subtitles.indexer, or
bazarr/api/subtitles/*. The compat surface is isolated by design (see
bazarr/compat/__init__.py); this module re-implements the small slice of
DB lookup and path-safety logic it needs inline.
"""
from __future__ import annotations

import ast as _ast
import logging
import os
import struct
import threading
from collections import OrderedDict


# Bound through a local name so the call site reads `_parse_literal(raw)`,
# matching the same safe-parse contract Bazarr uses elsewhere
# (see bazarr/api/utils.py for the same pattern on `subtitles` / `tags`).
_parse_literal = _ast.literal_eval

logger = logging.getLogger("bazarr.compat.local_subs")

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


def _decode_subtitle_bytes(raw: bytes) -> str:
    """BOM check -> UTF-8 -> charset_normalizer -> cp1252 fallback.

    Mirrors bazarr/api/subtitles/content.py:read_subtitle_file.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import charset_normalizer
        result = charset_normalizer.from_bytes(raw).best()
        if result is not None:
            return str(result)
    except Exception:
        pass
    return raw.decode("cp1252", errors="replace")


def _normalize_srt(raw: bytes) -> bytes:
    """Decode any incoming SRT bytes, re-emit UTF-8 without BOM."""
    return _decode_subtitle_bytes(raw).encode("utf-8")


def _convert_to_srt(raw: bytes, fmt: str) -> bytes:
    """Convert ass/ssa/vtt/sub/smi/ttml/dfxp source to SRT bytes via pysubs2.

    Returns b"" on any conversion failure (preserves the plugin's
    "broken sub, blocklist this file_id" contract -- 200 + empty body).
    """
    try:
        import pysubs2
        text = _decode_subtitle_bytes(raw)
        sub = pysubs2.SSAFile.from_string(text, format_=fmt)
        return sub.to_string("srt").encode("utf-8")
    except Exception as e:
        logger.debug("compat local: convert %s -> srt failed: %s", fmt, e)
        return b""


_MAX_SUB_BYTES = 5 * 1024 * 1024


def serve_local(payload: dict) -> tuple[bytes, str]:
    """Validate, read, and (if needed) convert a local subtitle to SRT.

    Returns (bytes, "application/x-subrip"). Empty bytes is a valid
    response: it's the plugin signal for "broken sub, blocklist".
    """
    path = payload.get("path") or ""
    media_dir = payload.get("media_dir") or ""
    fmt = payload.get("fmt") or "srt"

    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        raise FileNotFoundError("invalid path")
    try:
        media_dir_real = os.path.realpath(media_dir)
        if os.path.commonpath([real, media_dir_real]) != media_dir_real:
            raise FileNotFoundError("subtitle moved outside media dir")
    except ValueError:
        raise FileNotFoundError("invalid media dir")

    if not os.path.isfile(real):
        raise FileNotFoundError("subtitle missing on disk")

    try:
        size = os.path.getsize(real)
    except OSError:
        raise FileNotFoundError("subtitle stat failed")
    if size > _MAX_SUB_BYTES:
        raise FileNotFoundError(f"subtitle file too large ({size} bytes)")

    with open(real, "rb") as f:
        raw = f.read()

    if fmt == "srt":
        return _normalize_srt(raw), "application/x-subrip"
    return _convert_to_srt(raw, fmt), "application/x-subrip"


def _fetch_media_row(media_type: str, media_id: int):
    """Fetch the row needed to enumerate subtitles + media path."""
    from app.database import select, TableMovies, TableEpisodes
    try:
        if media_type == "episode":
            return database.execute(
                select(TableEpisodes.subtitles, TableEpisodes.path)
                .where(TableEpisodes.sonarrEpisodeId == int(media_id))
            ).first()
        else:
            return database.execute(
                select(TableMovies.subtitles, TableMovies.path)
                .where(TableMovies.radarrId == int(media_id))
            ).first()
    except Exception as e:
        logger.debug("compat local: media row fetch failed: %s", e)
        return None


def _build_request_to_lang_map(requested: list[str]) -> dict[str, str]:
    """Map "en" / "pt" / "zh" -> the original BCP-47 string when there's
    exactly one variant per base, so the mapper can preserve the region
    subtag (zh-CN / pt-BR)."""
    by_base: dict[str, list[str]] = {}
    for code in requested or []:
        base = code.split("-", 1)[0].lower()
        by_base.setdefault(base, []).append(code)
    return {b: codes[0] for b, codes in by_base.items() if len(codes) == 1}


def _path_replace_for(media_type: str):
    if path_mappings is None:
        return lambda p: p
    return (path_mappings.path_replace
            if media_type == "episode"
            else path_mappings.path_replace_movie)


def _select_local_subs(raw_subtitles, media_dir: str,
                      requested_languages: list[str]) -> list[dict]:
    """Filter Bazarr's `subtitles` column entries by requested languages
    and surviving on-disk files.

    Returns a list of intermediate dicts (NOT yet OS.com-shaped). Each:
      {"lang": str, "modifier": "hi"|"forced"|None, "fmt": str,
       "path": str (realpath inside media_dir), "filename": str,
       "size": int, "mtime": float}

    Path safety: each entry is realpath'd and required to live inside
    realpath(media_dir). The caller is responsible for path-mapping the
    raw subtitle paths *before* passing them in (the raw `subtitles`
    column stores Sonarr/Radarr-side paths).
    """
    items = _parse_subtitles_blob(raw_subtitles)
    if not items:
        return []
    requests = [_parse_request_bcp47(c) for c in requested_languages if c]
    if not requests:
        return []

    media_dir_real = os.path.realpath(media_dir)
    out: list[dict] = []
    for item in items:
        if not (isinstance(item, list) and len(item) >= 2):
            continue
        lang_code, raw_path = item[0], item[1]
        if not (isinstance(lang_code, str) and isinstance(raw_path, str)
                and raw_path):
            continue

        entry_base, modifier = _parse_lang_code(lang_code)

        if not any(_lang_matches(entry_base, rb, rr) for rb, rr in requests):
            continue

        try:
            real = os.path.realpath(raw_path)
        except (OSError, ValueError):
            continue
        try:
            common = os.path.commonpath([real, media_dir_real])
        except ValueError:
            continue
        if common != media_dir_real:
            continue
        if not os.path.isfile(real):
            continue

        fmt = _resolve_format(real)
        if fmt is None:
            continue

        try:
            st = os.stat(real)
        except OSError:
            continue

        out.append({
            "lang": entry_base,
            "modifier": modifier,
            "fmt": fmt,
            "path": real,
            "filename": os.path.basename(real),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return out


# Module-level `database` symbol so tests can patch via
# `compat.local_subs.database`. Real reference imported lazily.
try:
    from app.database import database
except Exception:
    database = None


def search_local(
    imdb_id: str | None,
    season: int | None,
    episode: int | None,
    media_type: str,
    languages: list[str],
    query: str | None = None,
    moviehash: str | None = None,
) -> list[dict]:
    """OS.com-shaped entries for locally-available subtitles.

    Empty list on resolve miss or no matches. Never raises.
    """
    if not languages:
        return []
    try:
        from .response_mapper import local_to_os_entry
        from . import auth as _auth

        resolved = _resolve_media(imdb_id, season, episode, media_type,
                                   query, moviehash)
        if resolved is None:
            return []
        media_type_resolved, media_id = resolved

        row = _fetch_media_row(media_type_resolved, media_id)
        if row is None:
            return []

        path_replace = _path_replace_for(media_type_resolved)
        media_local = path_replace(row.path)

        try:
            media_dir_real = os.path.realpath(os.path.dirname(media_local))
        except (OSError, ValueError):
            return []

        # Path-replace each subtitle path before selection so the realpath
        # barrier in _select_local_subs sees local-filesystem paths.
        items = _parse_subtitles_blob(row.subtitles)
        mapped = [
            [it[0], path_replace(it[1])]
            for it in items
            if isinstance(it, list) and len(it) >= 2
        ]
        raw_remapped = repr(mapped)

        candidates = _select_local_subs(
            raw_subtitles=raw_remapped,
            media_dir=media_dir_real,
            requested_languages=languages,
        )
        if not candidates:
            return []

        req_lang_map = _build_request_to_lang_map(languages)
        out: list[dict] = []
        for c in candidates:
            file_id = _auth.mint_local_file_id(
                path=c["path"],
                lang=c["lang"],
                modifier=c["modifier"],
                fmt=c["fmt"],
                media_type=media_type_resolved,
                media_id=media_id,
                media_dir=media_dir_real,
            )
            base_alpha2 = c["lang"].split("-", 1)[0].lower()
            requested_language = req_lang_map.get(base_alpha2)
            out.append(local_to_os_entry(
                file_id=file_id,
                lang=c["lang"],
                modifier=c["modifier"],
                filename=c["filename"],
                upload_mtime=c["mtime"],
                media_type=media_type_resolved,
                media_id=media_id,
                requested_language=requested_language,
            ))
        return out
    except Exception as e:
        logger.warning("compat local: search_local crashed (returning []): %s", e)
        return []

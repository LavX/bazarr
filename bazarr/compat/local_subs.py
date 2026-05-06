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


def _imdb_candidates(imdb_id: str | None) -> list[str]:
    """Build the set of plausible IMDb id strings the DB might store.

    Jellyfin/OS-compat clients commonly strip leading zeros (sending
    `481369` for `tt0481369`); Sonarr/Radarr keep them. So we generate
    candidates: the as-supplied form, plus zero-padded widths 7, 8, 9
    that cover historical and current IMDb id lengths. Empty list when
    the input has no digits.
    """
    norm = _tt(imdb_id)
    if not norm:
        return []
    digits = norm[2:].lstrip("0") or "0"
    candidates = {norm, f"tt{digits}"}
    for width in (7, 8, 9):
        candidates.add(f"tt{digits.zfill(width)}")
    return sorted(candidates)


def _resolve_by_imdb(imdb_id: str, season: int | None, episode: int | None,
                    media_type: str) -> tuple[str, int] | None:
    from app.database import select, TableMovies, TableShows, TableEpisodes
    candidates = _imdb_candidates(imdb_id)
    if not candidates:
        return None
    try:
        if media_type == "episode":
            show = database.execute(
                select(TableShows.sonarrSeriesId)
                .where(TableShows.imdbId.in_(candidates))
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
                .where(TableMovies.imdbId.in_(candidates))
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
                   query: str | None, moviehash: str | None
                   ) -> tuple[str, int, str] | None:
    """Return (media_type, media_id, source) on hit, None on miss.

    `source` records which resolver path produced the hit:
      - "imdb"      : imdb_id + season/episode lookup
      - "query"     : guessit-on-filename lookup
      - "moviehash" : library scan + OS hash match
    Callers use `source == "moviehash"` to set attributes.moviehash_match
    on the response entry: hash-resolved rows are hash-validated by
    construction, regardless of the request's moviehash_match mode.
    """
    if imdb_id:
        hit = _resolve_by_imdb(imdb_id, season, episode, media_type)
        if hit:
            return (*hit, "imdb")
    if query:
        hit = _resolve_by_query(query, media_type)
        if hit:
            return (*hit, "query")
    if moviehash:
        hit = _resolve_by_moviehash(moviehash, media_type)
        if hit:
            return (*hit, "moviehash")
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

    Path-safety: the file must still live inside one of the
    `allowed_roots` recorded at mint time. Older payloads (pre-migration)
    only have `media_dir` — fall back to that single root.
    """
    path = payload.get("path") or ""
    media_dir = payload.get("media_dir") or ""
    fmt = payload.get("fmt") or "srt"
    raw_roots = payload.get("allowed_roots") or [media_dir]

    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        raise FileNotFoundError("invalid path")

    real_roots: list[str] = []
    for r in raw_roots:
        try:
            real_roots.append(os.path.realpath(r))
        except (OSError, ValueError):
            continue
    if not real_roots:
        raise FileNotFoundError("invalid media dir")
    if not _path_under_any_root(real, real_roots):
        raise FileNotFoundError("subtitle moved outside allowed roots")

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
    """Fetch the row needed to enumerate subtitles, media path, and the
    metadata that goes into `feature_details` (title/year/imdb/season/
    episode). Episodes need a join to TableShows for series-level fields.
    """
    from app.database import select, TableMovies, TableEpisodes, TableShows
    try:
        if media_type == "episode":
            return database.execute(
                select(
                    TableEpisodes.subtitles, TableEpisodes.path,
                    TableEpisodes.season, TableEpisodes.episode,
                    TableEpisodes.title.label("episode_title"),
                    TableShows.title.label("series_title"),
                    TableShows.year, TableShows.imdbId,
                )
                .join(TableShows,
                      TableEpisodes.sonarrSeriesId == TableShows.sonarrSeriesId)
                .where(TableEpisodes.sonarrEpisodeId == int(media_id))
            ).first()
        else:
            return database.execute(
                select(
                    TableMovies.subtitles, TableMovies.path,
                    TableMovies.title, TableMovies.year, TableMovies.imdbId,
                )
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


def _allowed_subtitle_roots(media_dir_real: str, media_path_real: str) -> list[str]:
    """Compose the allowed subtitle roots: the media file's directory
    plus any configured target folder (relative or absolute).

    `general.subfolder == "absolute"` users keep subs in a dedicated
    library that has no path overlap with the video file, so a strict
    media-dir-only check would silently filter every local entry out of
    the compat response. Mirrors the pattern in
    `bazarr/api/subtitles/content.py:resolve_subtitle_path`.
    """
    roots = [media_dir_real]
    try:
        from utilities.helper import get_target_folder
        target = get_target_folder(media_path_real)
        if target:
            target_real = os.path.realpath(target)
            if target_real and target_real not in roots:
                roots.append(target_real)
    except Exception as e:
        logger.debug("compat local: get_target_folder lookup failed: %s", e)
    return roots


def _path_under_any_root(real_path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath([real_path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _select_local_subs(raw_subtitles, media_dir: str,
                      requested_languages: list[str],
                      media_path: str | None = None) -> list[dict]:
    """Filter Bazarr's `subtitles` column entries by requested languages
    and surviving on-disk files.

    Returns a list of intermediate dicts (NOT yet OS.com-shaped). Each:
      {"lang": str, "modifier": "hi"|"forced"|None, "fmt": str,
       "path": str (realpath inside media_dir), "filename": str,
       "size": int, "mtime": float}

    Path safety: each entry is realpath'd and required to live inside
    `media_dir` OR the configured `general.subfolder` target. The caller
    is responsible for path-mapping the raw subtitle paths *before*
    passing them in (the raw `subtitles` column stores Sonarr/Radarr-
    side paths). Files larger than `_MAX_SUB_BYTES` are dropped at
    selection time so they never appear as broken download links.
    """
    items = _parse_subtitles_blob(raw_subtitles)
    if not items:
        return []
    requests = [_parse_request_bcp47(c) for c in requested_languages if c]
    if not requests:
        return []

    media_dir_real = os.path.realpath(media_dir)
    media_path_real = os.path.realpath(media_path) if media_path else media_dir_real
    allowed_roots = _allowed_subtitle_roots(media_dir_real, media_path_real)

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
        if not _path_under_any_root(real, allowed_roots):
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
        # Drop oversized files at selection time so they never appear as
        # broken download links: serve_local would 404 them anyway, but
        # surfacing them in the picker just produces a guaranteed-fail
        # download. Keep the picker honest.
        if st.st_size > _MAX_SUB_BYTES:
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
    moviehash_match: str | None = None,
) -> list[dict]:
    """OS.com-shaped entries for locally-available subtitles.

    Empty list on resolve miss or no matches. Never raises.

    `moviehash_match="only"` honors the OS.com perfect-match contract:
    only return locals when the media row was resolved via the moviehash
    path (which proves the on-disk file matches the client's hash).
    Locals resolved via imdb/query are NOT hash-validated and would
    silently break the strict-hash workflow if surfaced under "only".
    """
    if not languages:
        return []
    try:
        from .response_mapper import local_to_os_entry
        from . import auth as _auth

        if moviehash_match == "only":
            # Strict hash mode: only the moviehash resolution path can
            # certify that the local file actually matches the client's
            # hash. Skip imdb/query — they'd produce false positives.
            if not moviehash:
                return []
            hit = _resolve_by_moviehash(moviehash, media_type)
            resolved = (*hit, "moviehash") if hit else None
        else:
            resolved = _resolve_media(imdb_id, season, episode, media_type,
                                       query, moviehash)
        if resolved is None:
            return []
        media_type_resolved, media_id, resolve_source = resolved

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
            media_path=media_local,
        )
        if not candidates:
            return []

        # The same roots list the selector used to validate paths; carry
        # it into each minted file_id so serve_local can re-validate at
        # stream time, including subs in the absolute target folder.
        allowed_roots = _allowed_subtitle_roots(media_dir_real,
                                                 os.path.realpath(media_local))

        # Pull metadata for feature_details (Jellyfin's plugin filters
        # entries lacking it). Episodes use series_title; movies use
        # title.
        if media_type_resolved == "episode":
            md_title = getattr(row, "series_title", "") or ""
            md_episode_title = getattr(row, "episode_title", "") or ""
            md_year_raw = getattr(row, "year", None)
            md_imdb = getattr(row, "imdbId", "") or ""
            md_season = getattr(row, "season", None)
            md_episode = getattr(row, "episode", None)
        else:
            md_title = getattr(row, "title", "") or ""
            md_episode_title = ""
            md_year_raw = getattr(row, "year", None)
            md_imdb = getattr(row, "imdbId", "") or ""
            md_season = None
            md_episode = None
        try:
            md_year = int(str(md_year_raw)[:4]) if md_year_raw else 0
        except (TypeError, ValueError):
            md_year = 0

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
                allowed_roots=allowed_roots,
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
                imdb_id=md_imdb,
                title=md_title,
                year=md_year,
                season=md_season,
                episode=md_episode,
                episode_title=md_episode_title,
                # Hash-match flag reflects how the row was resolved, not
                # the request mode: a moviehash-resolved row is
                # hash-validated regardless of moviehash_match=include vs
                # only. Codex P2.
                hash_matched=resolve_source == "moviehash",
            ))
        return out
    except Exception as e:
        logger.warning("compat local: search_local crashed (returning []): %s", e)
        return []

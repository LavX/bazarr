from __future__ import annotations
import datetime as dt
import re

# Plugin contract: upload_date is STRICT and must be a valid ISO 8601
# datetime. Empty string crashes the Jellyfin plugin with
# System.Text.Json.JsonException. When a provider doesn't expose an upload
# timestamp we emit the Unix epoch as a safe "unknown" sentinel that still
# deserializes cleanly into System.DateTime.
_EPOCH_ISO = "1970-01-01T00:00:00Z"


# Release-quality markers used to score release-string parts when providers
# pack multiple releases into one field (gestdown, SUBDL Anonymus, etc.).
# Parts containing any of these are "specific" and preferred over bare tokens.
_RELEASE_QUALITY_RE = re.compile(
    r"\b("
    r"2160p|1080p|720p|480p|"
    r"WEB-?DL|WEBRip|WEB|BluRay|BDRip|BRRip|HDRip|HDTV|DVDRip|"
    r"x265|x264|H\.?265|H\.?264|HEVC|AVC|"
    r"DDP?5\.1|DDP?7\.1|DTS|AAC|AC3|FLAC"
    r")\b",
    re.IGNORECASE,
)

# Providers whose subtitles are considered trusted when the provider
# object doesn't set sub.from_trusted itself. Curated: these providers
# are the historical OS.com-tier uploaders.
_TRUSTED_PROVIDERS = frozenset({
    "opensubtitlescom", "opensubtitles", "addic7ed",
})

# Release-string markers that mean "HD". Narrower than _RELEASE_QUALITY_RE:
# only resolution and source markers count, not codec or audio.
_HD_RE = re.compile(
    r"\b(2160p|1080p|720p|WEB-?DL|BluRay|BDRip|BRRip)\b",
    re.IGNORECASE,
)


def _normalize_release(raw) -> str:
    """Pick a single clean release string from whatever the provider handed us.

    Providers like gestdown and SUBDL frequently pack multiple matching
    release names into one `release` field, separated by newlines, slashes,
    or pipes. Surfacing that raw string in the Jellyfin picker looks like
    a disaster: a 22-line blob or a 'WEB-DL\\nx264' with an embedded newline.

    Strategy: split on common separators, keep the part with the most
    release-quality markers (resolution, source, codec, audio), tiebreak
    on length. No info loss in the download flow because we key on
    subtitle_id, not release.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # Split on newline, CR, slash-with-optional-spaces, pipe-with-optional-spaces.
    # Commas are NOT separators (a release like "x264, AAC" is one release).
    parts = re.split(r"[\n\r]+|\s*/\s*|\s*\|\s*", s)
    parts = [p.strip(" \t.,-") for p in parts]
    parts = [p for p in parts if p]
    if not parts:
        return s  # whatever it was, return the stripped original
    if len(parts) == 1:
        return parts[0]
    # Score: (count of quality markers, length). Prefer specific over long.
    def _score(p: str):
        return (len(_RELEASE_QUALITY_RE.findall(p)), len(p))
    parts.sort(key=_score, reverse=True)
    return parts[0]


def _imdb_to_int(imdb_id) -> int:
    """OS.com returns feature_details.imdb_id as int (not string with 'tt').
    Accept 'tt0111161', '0111161', 111161, etc. Return 0 on parse failure."""
    if imdb_id is None:
        return 0
    s = str(imdb_id).strip().lower()
    if s.startswith("tt"):
        s = s[2:]
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _format_upload_date(value) -> str:
    """Emit valid ISO-8601 in UTC with trailing Z, never `+00:00Z`.

    Accepts naive or tz-aware datetime; returns the epoch fallback for
    None or anything that doesn't look like a datetime. Strict strftime
    so no fractional seconds either - Jellyfin plugin's parser is picky.
    """
    if not value:
        return _EPOCH_ISO
    try:
        if getattr(value, "tzinfo", None) is not None:
            value = value.astimezone(dt.timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (AttributeError, TypeError, ValueError):
        return _EPOCH_ISO


def _emit_language(lang, requested_language: str | None) -> str:
    """Prefer the BCP-47 code the caller requested (preserves region
    subtags like zh-CN, pt-BR). Fallback: reconstruct from language
    object's alpha2 + country, then bare alpha2."""
    if requested_language:
        return str(requested_language)
    alpha2 = getattr(lang, "alpha2", None) or ""
    country = getattr(lang, "country", None)
    country_code = getattr(country, "alpha2", None) if country else None
    if alpha2 and country_code:
        return f"{alpha2.lower()}-{country_code.upper()}"
    return str(alpha2).lower()


def _emit_file_name(sub, file_id: int, lang_code: str) -> str:
    """Prefer the provider's actual filename; fall back to an
    unambiguous synthetic that never starts with '.'.

    Old synthetic was f'{imdb_id}.{lang}.srt', which became '.en.srt'
    whenever imdb was empty (query-only search) and was also inconsistent
    across callers that passed tt-prefixed vs bare imdb ids.
    """
    provider = getattr(sub, "filename", None)
    if provider:
        return str(provider)
    code = lang_code or "und"
    return f"subtitle-{int(file_id)}.{code}.srt"


def _derive_from_trusted(sub) -> bool:
    """Provider sets its own from_trusted wins; otherwise the curated
    list decides."""
    explicit = getattr(sub, "from_trusted", None)
    if explicit is not None:
        return bool(explicit)
    provider = getattr(sub, "provider_name", "") or ""
    return provider in _TRUSTED_PROVIDERS


def _derive_hd(release_info) -> bool:
    if not release_info:
        return False
    return bool(_HD_RE.search(str(release_info)))


def _derive_ratings(score_tuple) -> float:
    """score/max_score -> 0.0..10.0 with 2 decimals. Clamped defensively."""
    if not score_tuple:
        return 0.0
    try:
        score, max_score = score_tuple
        if not max_score:
            return 0.0
        v = (float(score) / float(max_score)) * 10.0
        return round(max(0.0, min(10.0, v)), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def subtitle_to_os_entry(sub, file_id: int, media_type: str, imdb_id: str,
                         season=None, episode=None, video=None,
                         hash_matched: bool | None = None,
                         score: tuple[int, int] | None = None,
                         requested_language: str | None = None) -> dict:
    """Map a Subtitle to an OS.com `data[].attributes` shape.

    Optional kwargs:
        hash_matched: set by the caller when it filtered on moviehash
            (authoritative). If None, derive from 'hash' in sub.matches.
        score: (score, max_score) from subliminal_patch.score.ComputeScore,
            projected to attributes.ratings as 0.0-10.0.
        requested_language: BCP-47 code the client asked for; preserves
            region subtags that lang.alpha2 drops.
    """
    lang = getattr(sub, "language", None)
    lang_alpha2 = getattr(lang, "alpha2", None) or ""
    language_out = _emit_language(lang, requested_language)
    release = _normalize_release(getattr(sub, "release_info", ""))
    raw_release = getattr(sub, "release_info", "") or ""
    provider_name = getattr(sub, "provider_name", "") or ""
    uploader_name = getattr(sub, "uploader", None) or provider_name
    feat_type = "Episode" if media_type == "episode" else "Movie"

    v_title = ""
    v_year = 0
    ep_title = ""
    if video is not None:
        if media_type == "episode":
            v_title = getattr(video, "series", "") or ""
            ep_title = getattr(video, "title", "") or ""
        else:
            v_title = getattr(video, "title", "") or ""
        year_raw = getattr(video, "year", None)
        try:
            v_year = int(year_raw) if year_raw else 0
        except (TypeError, ValueError):
            v_year = 0
        if media_type == "episode":
            if not season:
                vs = getattr(video, "season", None)
                if vs:
                    season = vs
            if not episode:
                ve = getattr(video, "episode", None)
                if ve:
                    episode = ve
    if media_type == "episode":
        movie_name = ep_title or v_title
    elif v_year and v_title:
        movie_name = f"{v_year} - {v_title}"
    else:
        movie_name = v_title

    imdb_id_int = _imdb_to_int(imdb_id)

    # moviehash_match: explicit override wins, else derive from matches.
    if hash_matched is None:
        matches_set = getattr(sub, "matches", None) or set()
        try:
            hash_matched_final = "hash" in matches_set
        except TypeError:
            hash_matched_final = False
    else:
        hash_matched_final = bool(hash_matched)

    attributes = {
        "language": language_out,
        "subtitle_id": str(getattr(sub, "id", "")),
        "release": release,
        "comments": str(raw_release),
        "download_count": int(getattr(sub, "download_count", 0) or 0),
        # Ratings precedence: provider's own ratings (OSCom, YIFY) if > 0,
        # else project ComputeScore into 0-10 so the field is meaningful
        # for providers that don't expose a community rating.
        "ratings": (float(getattr(sub, "ratings", 0) or 0)
                    if float(getattr(sub, "ratings", 0) or 0) > 0
                    else (_derive_ratings(score) if score is not None else 0.0)),
        "votes": 0,
        "from_trusted": _derive_from_trusted(sub),
        "hd": _derive_hd(raw_release),
        "hearing_impaired": bool(getattr(sub, "hearing_impaired", False)),
        "moviehash_match": hash_matched_final,
        "ai_translated": bool(getattr(sub, "ai_translated", False)),
        "machine_translated": bool(getattr(sub, "machine_translated", False)),
        "foreign_parts_only": bool(getattr(sub, "foreign_parts_only", False)),
        "fps": float(getattr(sub, "fps", 0.0) or getattr(sub, "frame_rate", 0.0) or 0.0),
        "upload_date": _format_upload_date(getattr(sub, "upload_date", None)),
        "uploader": {"name": f"{provider_name}:{uploader_name}" if provider_name else str(uploader_name)},
        "feature_details": {
            "feature_type": feat_type,
            "imdb_id": imdb_id_int,
            "season_number": int(season) if season is not None else 0,
            "episode_number": int(episode) if episode is not None else 0,
            "title": v_title,
            "movie_name": movie_name,
            "year": v_year,
        },
        "url": getattr(sub, "page_link", None) or "",
        "files": [{
            "file_id": int(file_id),
            "file_name": _emit_file_name(sub, file_id, lang_alpha2 or "und"),
        }],
    }
    return {"id": str(file_id), "type": "subtitle", "attributes": attributes}


def search_envelope(entries: list, per_page: int = 50, page: int = 1) -> dict:
    """OS.com search envelope: top-level total_pages/total_count/per_page/page.

    VLSub and other OS-compat clients read total_count for their result-count
    display; Jellyfin reads per_page for pagination sanity. Bazarr+ fans out
    all providers in one shot so all results fit on page 1.
    """
    total = len(entries)
    total_pages = max(1, (total + per_page - 1) // per_page) if per_page > 0 else 1
    return {
        "total_pages": total_pages,
        "total_count": total,
        "per_page": per_page,
        "page": page,
        "data": entries,
    }


def download_response(link: str, remaining: int, reset_iso: str) -> dict:
    """OS.com-shape download response. No duplicate fields; VLSub and
    Jellyfin both read `remaining_downloads`, `remaining` is kept for
    VLSub compat."""
    return {
        "link": link,
        "remaining": int(remaining),
        "remaining_downloads": int(remaining),
        "reset_time_utc": reset_iso,
    }


def user_info_response(remaining: int, allowed: int, reset_iso: str) -> dict:
    return {
        "data": {
            "allowed_downloads": int(allowed),
            "remaining_downloads": int(remaining),
            "reset_time_utc": reset_iso,
            "level": "User",
            "user_id": 0,
            "ext_installed": False,
            "vip": False,
        }
    }


def languages_response() -> dict:
    """BCP-47 language codes for all audited clients.

    OS.com wire contract: each entry has 'language_code' and
    'language_name' (not 'code' / 'name'). The Jellyfin plugin's
    LanguageInfo model deserializes from 'language_code'.

    Region subtags are emitted in the canonical mixed case that the
    plugin normalizes toward: 'zh-CN' (for 'zh'), 'zh-TW', 'pt-BR',
    'pt-PT'. The plugin's match is case-insensitive, but emitting
    canonical case guards against stricter clients and keeps the
    response readable.
    """
    codes = [
        "en", "es", "fr", "de", "it", "pt-BR", "pt-PT", "nl", "pl",
        "ru", "zh-CN", "zh-TW", "ja", "ko", "ar", "hu", "tr", "cs",
        "da", "no", "sv", "fi", "el", "he", "th", "vi", "ro", "sk",
        "bg", "uk", "hr", "sr", "id",
    ]
    return {"data": [{"language_code": c, "language_name": c.upper()}
                     for c in codes]}


def local_to_os_entry(*, file_id: int, lang: str, modifier: str | None,
                      filename: str, upload_mtime: float,
                      media_type: str, media_id: int,
                      requested_language: str | None,
                      imdb_id: str = "", title: str = "",
                      year: int = 0,
                      season: int | None = None,
                      episode: int | None = None,
                      episode_title: str = "",
                      hash_matched: bool = False) -> dict:
    """OS.com-shaped entry for a locally-stored subtitle.

    Schema parity with `subtitle_to_os_entry` is required: the Jellyfin
    plugin (and other OS-compat clients) silently drop entries missing
    expected fields like `feature_details`, `uploader`, `fps`, etc.
    """
    upload_iso = (
        dt.datetime.fromtimestamp(int(upload_mtime), dt.timezone.utc)
          .strftime("%Y-%m-%dT%H:%M:%SZ")
        if upload_mtime else _EPOCH_ISO
    )
    language_out = requested_language or lang
    subtitle_id = f"local-{media_type}-{int(media_id)}-{lang}"
    if modifier:
        subtitle_id = f"{subtitle_id}:{modifier}"

    feat_type = "Episode" if media_type == "episode" else "Movie"
    if media_type == "episode":
        movie_name = episode_title or title
    elif year and title:
        movie_name = f"{year} - {title}"
    else:
        movie_name = title

    # Append the per-file id so two locals with the same lang+modifier
    # (e.g. two distinct on-disk `.en.srt` files) don't collide on
    # subtitle_id and get de-duplicated by the client. Codex P2.
    subtitle_id = f"{subtitle_id}-{int(file_id)}"

    return {
        "id": f"subtitle-{int(file_id)}",
        "type": "subtitle",
        "attributes": {
            "subtitle_id": subtitle_id,
            "language": language_out,
            "release": filename,
            "comments": filename,
            "download_count": 999_999,
            "ratings": 10.0,
            "votes": 0,
            "from_trusted": True,
            "hd": False,
            "hearing_impaired": modifier == "hi",
            "moviehash_match": bool(hash_matched),
            "ai_translated": False,
            "machine_translated": False,
            "foreign_parts_only": modifier == "forced",
            "fps": 0.0,
            "upload_date": upload_iso,
            "uploader": {"name": "bazarr:local"},
            "feature_details": {
                "feature_type": feat_type,
                "imdb_id": _imdb_to_int(imdb_id),
                "season_number": int(season) if season is not None else 0,
                "episode_number": int(episode) if episode is not None else 0,
                "title": title,
                "movie_name": movie_name,
                "year": int(year) if year else 0,
            },
            "url": "",
            "files": [{"file_id": int(file_id), "file_name": filename}],
        },
    }

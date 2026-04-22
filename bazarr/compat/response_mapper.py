from __future__ import annotations
import datetime as dt
import re

_STUB_REMAINING = 1000

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


def _tomorrow_utc_iso() -> str:
    t = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def subtitle_to_os_entry(sub, file_id: int, media_type: str, imdb_id: str,
                         season=None, episode=None, video=None) -> dict:
    """Map a Subtitle instance to an OS.com `data[].attributes` shape.

    file_id is a server-mapped int (OS.com contract); entry-level `id` is the
    same value as a numeric string (OS.com returns numeric strings like
    "10832492"). `video` is the enriched virtual Video built in
    compat.service._build_video - used to populate feature_details.title /
    movie_name / year from library lookup + refiner output.
    """
    lang = getattr(sub, "language", None)
    lang_alpha2 = getattr(lang, "alpha2", None) or ""
    release = _normalize_release(getattr(sub, "release_info", ""))
    uploader_name = getattr(sub, "uploader", None) or getattr(sub, "provider_name", "")
    feat_type = "Episode" if media_type == "episode" else "Movie"  # B12

    # Pull metadata off the virtual video (library + refiner populated).
    # Falls back to empty/0 when the video wasn't threaded through.
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
        # Prefer video's resolved season/episode when the route didn't
        # receive them. OS-compat clients (Jellyfin plugin) filter results
        # by these fields, so 0/0 means every hit gets dropped.
        if media_type == "episode":
            if not season:
                vs = getattr(video, "season", None)
                if vs:
                    season = vs
            if not episode:
                ve = getattr(video, "episode", None)
                if ve:
                    episode = ve
    # OS.com's movie_name is "YYYY - Title" for movies; for episodes it's
    # usually the episode title. Best-effort replication:
    if media_type == "episode":
        movie_name = ep_title or v_title
    elif v_year and v_title:
        movie_name = f"{v_year} - {v_title}"
    else:
        movie_name = v_title

    imdb_id_int = _imdb_to_int(imdb_id)
    attributes = {
        "language": str(lang_alpha2).lower(),
        "subtitle_id": str(getattr(sub, "id", "")),
        "release": release,
        "download_count": int(getattr(sub, "download_count", 0) or 0),
        "ratings": float(getattr(sub, "ratings", 0) or 0),
        "votes": 0,
        "from_trusted": bool(getattr(sub, "from_trusted", False)),
        "hd": bool(getattr(sub, "hd", False)),
        "hearing_impaired": bool(getattr(sub, "hearing_impaired", False)),
        "moviehash_match": False,
        "ai_translated": False,
        "machine_translated": False,
        "foreign_parts_only": False,
        "fps": 0.0,
        "upload_date": getattr(sub, "upload_date", None).isoformat() + "Z"
                       if getattr(sub, "upload_date", None) else _EPOCH_ISO,
        "uploader": {"name": str(uploader_name)},
        "feature_details": {
            "feature_type": feat_type,
            "imdb_id": imdb_id_int,
            "season_number": int(season) if season is not None else 0,
            "episode_number": int(episode) if episode is not None else 0,
            "title": v_title,
            "movie_name": movie_name,
            "year": v_year,
        },
        "files": [{
            "file_id": int(file_id),
            "file_name": f"{imdb_id}.{lang_alpha2}.srt",
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


def download_response(link: str, reset_iso: str | None = None) -> dict:
    return {
        "link": link,
        "remaining": _STUB_REMAINING,
        "remaining_downloads": _STUB_REMAINING,  # B11 — Jellyfin
        "requests": 0,
        "reset_time": reset_iso or _tomorrow_utc_iso(),
        "reset_time_utc": reset_iso or _tomorrow_utc_iso(),
    }


def user_info_response() -> dict:
    return {
        "data": {
            "allowed_downloads": _STUB_REMAINING,
            "remaining_downloads": _STUB_REMAINING,
            "remaining": _STUB_REMAINING,
            "reset_time_utc": _tomorrow_utc_iso(),
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

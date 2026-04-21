from __future__ import annotations
import logging
from threading import Lock
from typing import Iterable
from urllib.parse import quote
from babelfish import Language
from subliminal.video import Episode, Movie, Video
from subliminal_patch.core_persistent import list_all_subtitles_parallel

from bazarr.app.config import settings
from bazarr.app.get_providers import get_providers_sorted, get_providers_auth
from bazarr.compat import auth, cache as C, response_mapper as M
from bazarr.utilities.url_guard import assert_safe_outbound, UnsafeURLError

logger = logging.getLogger("bazarr.compat.service")

_pool_lock = Lock()
_compat_pool = None  # lazy singleton, dedicated (B2)


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
                language_hook=None,
                language_equals=[],
            )
        return _compat_pool


def reset_compat_pool() -> None:
    """Called on settings-change or provider toggle so stale creds don't persist."""
    global _compat_pool
    with _pool_lock:
        _compat_pool = None


def _build_video(imdb_id: str, season: int | None, episode: int | None,
                 media_type: str) -> Video:
    """No filesystem, no parse_video, no scan_video."""
    if media_type == "episode":
        v = Episode(
            name=f"virtual_{imdb_id}_s{season}e{episode}.mkv",
            series="",
            season=int(season or 0),
            episode=int(episode or 0),
            series_imdb_id=imdb_id,
        )
    else:
        v = Movie(
            name=f"virtual_{imdb_id}.mkv",
            title="",
            year=None,
            imdb_id=imdb_id,
        )
    v.size = None
    v.hashes = {}
    return v


def _do_fanout(imdb_id, season, episode, languages, media_type):
    pool = _get_compat_pool()
    video = _build_video(imdb_id, season, episode, media_type)
    results = list_all_subtitles_parallel(
        [video], set(languages), pool,
        per_provider_timeout=int(settings.compat_endpoint.per_provider_timeout_seconds),
        wall_timeout=int(settings.compat_endpoint.search_timeout_seconds),
    )
    subs = []
    for v, sub_list in results.items():
        subs.extend(sub_list)
    entries = []
    for sub in subs:
        file_id = auth.mint_file_id(
            provider=getattr(sub, "provider_name", "unknown"),
            native_id=getattr(sub, "id", ""),
            language=str(getattr(sub, "language", "")),
            release_info=getattr(sub, "release_info", "") or "",
        )
        entries.append(M.subtitle_to_os_entry(sub, file_id, media_type, imdb_id,
                                              season, episode))
    entries.sort(key=lambda e: -int(e["attributes"].get("download_count", 0)))
    return {"data": entries, "total_pages": 1, "page": 1}


def search(imdb_id: str, season, episode, languages: Iterable[Language],
           media_type: str) -> dict:
    enabled = get_providers_sorted()
    key = C.build_key(media_type, imdb_id, season, episode, languages, enabled)
    ttl = int(settings.compat_endpoint.cache_ttl_seconds)
    return C.compat_region.get_or_create(
        key,
        creator=lambda: _do_fanout(imdb_id, season, episode, languages, media_type),
        expiration_time=ttl,
    )


def download(file_id: str, base_host: str) -> dict:
    """Parse the file_id, mint a short-lived stream token, return a Bazarr+-hosted link.

    No provider fetch happens here — the subtitle is fetched only when the client
    follows the link (see serve_subtitle_content)."""
    ok, payload = auth.parse_file_id(file_id)
    if not ok:
        raise FileNotFoundError("file_id invalid or expired")
    stream_tok = auth.mint_stream_token(payload["p"], payload["i"])
    link = f"{base_host.rstrip('/')}/api/v1/download/stream/{quote(stream_tok, safe='')}"
    return M.download_response(link)


def _fetch_subtitle_bytes(provider_name: str, native_id: str) -> bytes:
    """Reconstruct a provider subtitle proxy and download.

    Real implementation in Task 15 — this stub raises NotImplementedError.
    Callers must invoke assert_safe_outbound on the resolved provider URL
    BEFORE requests.get (and re-verify after any redirect).
    """
    raise NotImplementedError("per-provider reconstruction — see Task 15")


def serve_subtitle_content(stream_token: str) -> tuple[bytes, str]:
    """Validate the stream token and return the subtitle bytes + content-type.

    Raises:
        ValueError: stream token invalid or expired
        FileNotFoundError: subtitle not found
        UnsafeURLError: provider URL failed SSRF guard
    """
    ok, payload = auth.parse_stream_token(stream_token)
    if not ok:
        raise ValueError("stream token invalid or expired")
    # Surface the guard symbol so tests can patch it; Task 15 uses it inside _fetch.
    _ = assert_safe_outbound
    blob = _fetch_subtitle_bytes(payload["p"], payload["i"])
    return blob, "application/x-subrip"

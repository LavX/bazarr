from __future__ import annotations
import hashlib
import json
from dogpile.cache import make_region

from utilities.locked_lru import LockedLRU

# Bound the in-memory region with a thread-safe LRU and a sane region default
# TTL. Each cached envelope holds provider matches + scores and can be tens of
# KB; without a bound, a 5000-episode library could hold 5000 envelopes for up
# to 24h. maxsize=2048 caps worst-case footprint regardless of library size,
# evicting the least-recently-used envelope on overflow. expiration_time=1800
# is the region default; callers in service.py override it per-call via
# `expiration_time=...`, so this only matters for callers that forget to pass
# one. LockedLRU wraps cachetools.LRUCache with a threading.Lock because
# Waitress runs threads=100 request workers and dogpile's set()/delete()
# bypass the per-key mutex, leaving the LRU's OrderedDict linked list open
# to concurrent corruption otherwise.
compat_region = make_region(key_mangler=lambda k: k).configure(
    "dogpile.cache.memory",
    arguments={"cache_dict": LockedLRU(maxsize=2048)},
    expiration_time=1800,
)


def build_key(media_type: str, imdb_id: str, season: int | None,
              episode: int | None, languages, enabled_providers,
              query: str | None = None, moviehash: str | None = None,
              moviebytesize: int | None = None,
              series_anidb_id: int | None = None,
              series_anidb_episode_id: int | None = None,
              moviehash_match: str | None = None,
              requested_languages: list[str] | None = None,
              exclude_providers: list[str] | None = None,
              timeout_seconds: int | None = None,
              only_providers: list[str] | None = None) -> str:
    """Deterministic across restarts. Language variants preserved.

    query/moviehash/moviebytesize/AniDB ids/moviehash_match are part of the key
    because they change the virtual Video construction AND post-fanout filtering,
    so different values produce different result shapes and must not
    cross-contaminate via cache hits.

    exclude_providers/only_providers/timeout_seconds are per-request
    (Distribution Hub) knobs. exclude_providers drops providers; only_providers
    is the inverse allow-list (restrict to exactly these). Both change which
    results appear, so they MUST influence the key - otherwise a narrowed
    search would hit the full-provider cached envelope. They are folded in
    independently (not collapsed to one set) so the key stays stable regardless
    of how many providers the install happens to have enabled. timeout_seconds
    is folded in too so a short-timeout (partial) search can't poison a later
    full search's cache.
    """
    lang_tuples = sorted(
        (str(l.alpha3), str(l.country) if l.country else "",
         bool(getattr(l, "forced", False)), bool(getattr(l, "hi", False)))
        for l in languages  # noqa: E741
    )
    provider_hash = hashlib.sha256(
        ",".join(sorted(enabled_providers or [])).encode()
    ).hexdigest()[:16]
    req_langs = ",".join(sorted(requested_languages or []))
    # Local-sub merging changes envelope shape (locals are pinned to the
    # top), so it must influence the cache key. Without this, toggling the
    # serve_local_subs flag would keep returning the previous cached
    # envelope until natural TTL expiry.
    from app.config import settings as _cfg
    local_flag = int(bool(_cfg.compat_endpoint.serve_local_subs))
    excl = ",".join(sorted(exclude_providers or []))
    # only_providers is tri-state and its three cases MUST stay distinct in the
    # key: None = no allow-list (every provider in play), [] = allow-list active
    # but matches nothing (data == []), [names] = scoped. Collapsing None and []
    # to "" would let an active-empty search hit the full-provider envelope.
    incl = "*" if only_providers is None else ",".join(sorted(only_providers))
    to = int(timeout_seconds) if timeout_seconds else 0
    extras = hashlib.sha256(
        f"{query or ''}|{moviehash or ''}|{moviebytesize or ''}|{moviehash_match or ''}"
        f"|anidb={series_anidb_id or ''}|anidb_ep={series_anidb_episode_id or ''}"
        f"|{req_langs}|local={local_flag}|excl={excl}|incl={incl}|to={to}".encode()
    ).hexdigest()[:16]
    return (
        f"compat:v2:{media_type}:{imdb_id}:{season or 0}:{episode or 0}"
        f":{provider_hash}:{extras}"
        f":{json.dumps(lang_tuples, sort_keys=True, separators=(',', ':'))}"
    )


def invalidate_all() -> None:
    """Hard invalidation of the entire compat region. Called post secret rotation."""
    compat_region.invalidate(hard=True)

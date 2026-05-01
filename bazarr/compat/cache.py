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
              moviehash_match: str | None = None,
              requested_languages: list[str] | None = None) -> str:
    """Deterministic across restarts. Language variants preserved.

    query/moviehash/moviehash_match are part of the key because they
    change the virtual Video construction AND post-fanout filtering, so
    different values produce different result shapes and must not
    cross-contaminate via cache hits.
    """
    lang_tuples = sorted(
        (str(l.alpha3), str(l.country) if l.country else "",
         bool(getattr(l, "forced", False)), bool(getattr(l, "hi", False)))
        for l in languages
    )
    provider_hash = hashlib.sha256(
        ",".join(sorted(enabled_providers or [])).encode()
    ).hexdigest()[:16]
    req_langs = ",".join(sorted(requested_languages or []))
    extras = hashlib.sha256(
        f"{query or ''}|{moviehash or ''}|{moviehash_match or ''}|{req_langs}".encode()
    ).hexdigest()[:16]
    return (
        f"compat:v2:{media_type}:{imdb_id}:{season or 0}:{episode or 0}"
        f":{provider_hash}:{extras}"
        f":{json.dumps(lang_tuples, sort_keys=True, separators=(',', ':'))}"
    )


def invalidate_all() -> None:
    """Hard invalidation of the entire compat region. Called post secret rotation."""
    compat_region.invalidate(hard=True)

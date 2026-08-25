# coding=utf-8
from __future__ import annotations

# Built-in provider ids that a TRUSTED Provider Hub catalog entry is allowed to
# replace ("shadow"). Shadowing a built-in is otherwise rejected; only entries from
# a trusted/official catalog source whose id is on this list may take over a built-in
# provider. Untrusted catalogs can never replace a built-in regardless of id.
MIGRATED_BUILT_IN_PROVIDER_IDS = frozenset({
    "addic7ed",
    "animekalesi",
    "animesubinfo",
    "animetosho",
    "assrt",
    "avistaz",
    "betaseries",
    "bsplayer",
    "cinemaz",
    "embeddedsubtitles",
    "gestdown",
    "greeksubs",
    "greeksubtitles",
    "hdbits",
    "jimaku",
    "karagarga",
    "ktuvit",
    "legendasdivx",
    "legendasnet",
    "napiprojekt",
    "napisy24",
    "nekur",
    "opensubtitles",
    "opensubtitlescom",
    "pipocas",
    "prijevodionline",
    "regielive",
    "shooter",
    "soustitreseu",
    "subclub",
    "subdl",
    "subf2m",
    "subs4free",
    "subs4series",
    "subsarr",
    "subsource",
    "subsro",
    "subssabbz",
    "subsunacs",
    "subsynchro",
    "subtis",
    "subtitrarinoi",
    "subtitriid",
    "subtitulamostv",
    "subx",
    "supersubtitles",
    "titlovi",
    "titrari",
    "titulky",
    "turkcealtyaziorg",
    "tvsubtitles",
    "whisperai",
    "wizdom",
    "yavkanet",
    "yifysubtitles",
    "zimuku",
})


# Built-in provider ids whose module has been deleted. Deleting it drops the id
# from provider_registry.names(), and that is the dynamic half of the shadow gate,
# so the id would otherwise fall out of the built-in set entirely and the next
# plugin to ask for it would get it. These ids stay claimed instead.
#
# Ids here are registry keys, which is the provider module name, not the class's
# provider_name attribute (argenteamdump registered as "argenteamdump" while its
# provider_name read "argenteam_dump").
#
# This is a separate set from the migration allowlist above and stays separate:
# migration carries its own auto-install and rename semantics for a built-in that
# a catalog entry takes over, while this one only keeps a dead id spoken for.
RETIRED_BUILT_IN_PROVIDER_IDS = frozenset({
    "argenteamdump",
    "hosszupuska",
    "subdivx",
    "subscene",
    "subscene_cloudscraper",
    "tusubtitulo",
})


# Both sets are shadowable, by a trusted source only. Catalog plugins are the
# provider mechanism going forward, so a trusted catalog entry adopting a migrated
# or a retired built-in id is the intended path, not an attack: "trusted" means
# the entry came from a catalog source marked trusted AND its manifest hash
# matched. What the gate exists to stop is an UNTRUSTED plugin impersonating a
# well-known provider id, and that stays blocked for every id in either set.
_TRUSTED_SHADOWABLE_PROVIDER_IDS = (
    MIGRATED_BUILT_IN_PROVIDER_IDS | RETIRED_BUILT_IN_PROVIDER_IDS
)


def can_shadow_built_in_provider(provider_id: str, trusted: bool) -> bool:
    return bool(trusted) and str(provider_id or "") in _TRUSTED_SHADOWABLE_PROVIDER_IDS


def validation_built_in_provider_ids(
    provider_id: str,
    built_in_provider_ids: set[str] | None,
    trusted: bool,
) -> set[str]:
    provider_ids = set(built_in_provider_ids or set())
    if can_shadow_built_in_provider(provider_id, trusted):
        provider_ids.discard(str(provider_id or ""))
    return provider_ids

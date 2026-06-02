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


def can_shadow_built_in_provider(provider_id: str, trusted: bool) -> bool:
    return bool(trusted) and str(provider_id or "") in MIGRATED_BUILT_IN_PROVIDER_IDS


def validation_built_in_provider_ids(
    provider_id: str,
    built_in_provider_ids: set[str] | None,
    trusted: bool,
) -> set[str]:
    provider_ids = set(built_in_provider_ids or set())
    if can_shadow_built_in_provider(provider_id, trusted):
        provider_ids.discard(str(provider_id or ""))
    return provider_ids

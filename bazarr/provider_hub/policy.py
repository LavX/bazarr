# coding=utf-8
from __future__ import annotations


MIGRATED_BUILT_IN_PROVIDER_IDS = frozenset(
    {
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
    }
)


def replacement_provider_ids_for_source(trusted: bool) -> frozenset[str]:
    if trusted:
        return MIGRATED_BUILT_IN_PROVIDER_IDS
    return frozenset()

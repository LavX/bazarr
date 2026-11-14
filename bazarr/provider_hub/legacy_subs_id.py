# coding=utf-8
"""How a built-in provider's subtitle id maps onto the id its catalog plugin gives.

Blacklist rows and history rows name a subtitle by ``(provider, subs_id)``. A
built-in provider wrote whatever its own subtitle class called its id: a native
id, a URL, a path, a composite. A plugin subtitle is a ``HubWorkerSubtitle``
whose id is ``<provider>:<worker id>``, with the worker id chosen by the plugin.
So not one old id equals a new one, even when the raw id is unchanged: an old
blacklist entry stops suppressing its release after the switch.

This module is the parity table for the migrated built-ins and the one-time
rewrite rules for the rows that can follow. It reads nothing and writes
nothing: a caller owns the rows and decides when to rewrite them.

Classes (``LEGACY_ID_CLASS``):

``prefix``
    The plugin reuses the built-in's raw id, so only the host prefix is new.
``derived``
    The plugin id is a function of the old id and the row's language. A rule is
    written, in ``rewrite_legacy_subs_id``.
``derivable``
    The plugin id is a function of the old id plus context a row may hold (the
    language, the season and episode, a value parsed from a URL). Read from the
    plugin code, no rule written yet.
``lost``
    The plugin id embeds data an old row never recorded (a file hash, an entry
    id, a digest). The row cannot be rewritten: its entry stops suppressing and
    that is documented rather than fixed.

Plugin ids carry no stability promise beyond what each plugin does today, so a
catalog update can change a format. The tests pin each written rule to the
shape the host gives a worker id.
"""
from __future__ import annotations

import re

from babelfish import Language
from babelfish.exceptions import Error as BabelfishError

PREFIX = "prefix"
DERIVED = "derived"
DERIVABLE = "derivable"
LOST = "lost"

LEGACY_ID_CLASS: dict[str, str] = {
    "addic7ed": DERIVABLE,  # needs the season and episode
    "animekalesi": DERIVABLE,  # sha1 of the old id, a colon and the alpha3
    "animesubinfo": DERIVABLE,
    "animetosho": LOST,  # embeds the entry id
    "assrt": DERIVABLE,  # needs the site's own language code
    "avistaz": DERIVABLE,
    "betaseries": DERIVED,
    "bsplayer": DERIVED,
    "cinemaz": DERIVABLE,
    "embeddedsubtitles": DERIVABLE,  # ephemeral, the id holds the video path
    "gestdown": DERIVABLE,
    "greeksubs": DERIVED,
    "greeksubtitles": DERIVABLE,  # the number is parsed out of the old URL
    "hdbits": DERIVED,
    "jimaku": LOST,  # embeds the entry id
    "karagarga": PREFIX,
    "ktuvit": PREFIX,
    "legendasdivx": DERIVABLE,
    "legendasnet": DERIVABLE,
    "napiprojekt": LOST,  # digest over a source value the old row lacks
    "napisy24": LOST,  # old id is the video hash, new id the site's own id
    "nekur": DERIVABLE,
    "opensubtitles": DERIVABLE,
    "opensubtitlescom": DERIVED,
    "pipocas": DERIVABLE,
    "prijevodionline": DERIVABLE,
    "regielive": DERIVABLE,  # the id is parsed out of the old URL
    "shooter": LOST,  # digest over the file hash
    "soustitreseu": LOST,  # digest over a URL the old row lacks
    "subclub": DERIVABLE,
    "subdl": PREFIX,  # single files only; pack members and AI rows are lost
    "subf2m": DERIVABLE,
    "subs4free": DERIVABLE,
    "subs4series": LOST,  # digest over the detail URL
    "subsarr": DERIVABLE,
    "subsource": PREFIX,
    "subsro": DERIVED,
    "subssabbz": LOST,  # digest over the download URL and the file name
    "subsunacs": LOST,  # digest over the download URL and an identity
    "subsynchro": LOST,  # the file id comes from the download page
    "subtis": LOST,  # digest over a method the old row lacks
    "subtitrarinoi": DERIVABLE,
    "subtitriid": DERIVABLE,
    "subtitulamostv": PREFIX,
    "subx": DERIVABLE,
    "supersubtitles": DERIVED,
    "titlovi": DERIVED,
    "titrari": DERIVED,
    "titulky": DERIVED,
    "turkcealtyaziorg": DERIVABLE,  # case and dash change; movies keep the raw id
    "tvsubtitles": DERIVED,
    "whisperai": LOST,  # digest, and the old id held the video's original name
    "wizdom": DERIVED,
    "yavkanet": LOST,  # digest over the page URL
    "yifysubtitles": DERIVABLE,
    "zimuku": LOST,  # digest over the language label of the detail page
}

# The prefix rows whose plugin side was read but not run on a recorded
# response: the plugin takes its id from a normalised URL or an id the old
# class built from a differently named field.
PREFIX_UNCONFIRMED = frozenset({"karagarga", "ktuvit", "subtitulamostv"})

# Derived rules whose plugin id ends in the row's language as an ISO 639-3
# code, in the form ``<provider>-<old id>-<alpha3>``.
_WITH_ALPHA3 = frozenset({
    "betaseries", "opensubtitlescom", "titlovi", "titrari", "titulky", "tvsubtitles",
})

_DIGITS = re.compile(r"\d+")


def host_subtitle_id(provider_id: str, worker_id: str) -> str:
    """The id the host gives a plugin subtitle (see ``HubWorkerSubtitle.id``)."""
    return f"{provider_id}:{worker_id}"


def _row_language(language: str | None) -> tuple[str, bool] | None:
    """``(alpha3, forced)`` for a blacklist row's language, or None.

    A row stores ``Language.basename`` with ``:forced`` or ``:hi`` appended for
    those variants, for example ``en``, ``pt-BR``, ``sr-Cyrl`` or ``en:forced``.
    """
    if not language or not isinstance(language, str):
        return None
    base, _, variant = language.partition(":")
    try:
        alpha3 = Language.fromietf(base).alpha3
    except (BabelfishError, ValueError, KeyError):
        return None
    return alpha3, variant == "forced"


def rewrite_legacy_subs_id(provider_id: str, subs_id: str, language: str | None = None) -> str | None:
    """The id the catalog plugin of ``provider_id`` gives the subtitle that the
    built-in stored as ``subs_id``, or None when the row cannot be followed.

    ``language`` is the row's language column. None is returned, never a
    guess, for a provider whose class is not ``prefix`` or ``derived``, for an
    id that does not have the built-in's shape, and for a derived rule whose
    language is missing or unreadable.
    """
    kind = LEGACY_ID_CLASS.get(provider_id)
    if kind not in (PREFIX, DERIVED) or not isinstance(subs_id, str) or not subs_id:
        return None
    if kind == PREFIX:
        return host_subtitle_id(provider_id, subs_id)

    if provider_id == "hdbits":
        # The built-in wrote ``hdbits_<n>``; the plugin writes ``hdbits-<n>``.
        match = re.fullmatch(r"hdbits_(\d+)", subs_id)
        return host_subtitle_id(provider_id, f"hdbits-{match.group(1)}") if match else None
    if provider_id in ("wizdom", "bsplayer", "greeksubs"):
        return host_subtitle_id(provider_id, f"{provider_id}-{subs_id}")
    if provider_id == "subsro" and not _DIGITS.fullmatch(subs_id):
        # A built-in row can hold the page link in place of the site id.
        return None
    if provider_id not in _WITH_ALPHA3 and provider_id not in ("subsro", "supersubtitles"):
        return None

    parsed = _row_language(language)
    if parsed is None:
        return None
    alpha3, forced = parsed
    suffix = "-forced" if provider_id == "supersubtitles" and forced else ""
    return host_subtitle_id(provider_id, f"{provider_id}-{subs_id}-{alpha3}{suffix}")

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
    The plugin id is a function of the old id plus context a row, or the media
    file it belongs to, can supply (the language, the season and episode, a
    value parsed from a URL, the video's hash). Read from the plugin code, no
    rule written yet.
``lost``
    The plugin id embeds data an old row never recorded (a file hash, an entry
    id, a hash of content the row never held). The row cannot be rewritten: its
    entry stops suppressing and that is documented rather than fixed.

Languages. A blacklist row stores the language the search used, with
``:forced`` or ``:hi`` appended for those variants, and the History page stores
Bazarr's own two-letter code the same way. That includes Bazarr's custom codes
``pb``, ``zt`` and ``ea``, which a plugin reports as ``por``, ``zho`` and
``spa``; they are mapped before the code is parsed.

One limit: the supersubtitles plugin appends ``-forced`` when the subtitle
itself is a forced one, which it reads from the listing. A row records only the
variant that was requested, so a ``:forced`` row maps to the ``-forced`` id and
any other row to the plain one. A subtitle the site marks as forced but that
was blacklisted under a plain request keeps an old id that no rule reaches.

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
    # sha1 over the torrent entry id, the download URL, the alpha3 and the
    # country. The old row holds the URL and the language but never the entry
    # id, which only the site's feed can give back.
    "animetosho": LOST,
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
    # Scraper-mode ids only (``opensubtitles-native-<id>``). A row written by
    # the XML-RPC built-in holds an IDSubtitleFile, which the plugin never
    # reports: those rows are lost.
    "opensubtitles": DERIVABLE,
    "opensubtitlescom": DERIVED,
    "pipocas": DERIVABLE,
    "prijevodionline": DERIVABLE,
    "regielive": DERIVABLE,  # the id is parsed out of the old URL
    # sha1 over the media file's content hash, the site's language code and
    # the download URL. The row holds the URL and the language; the hash comes
    # from the media file the row belongs to.
    "shooter": DERIVABLE,
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
    # sha1 over the lookup method and the lookup URL. The old id is the stored
    # lookup URL, and the method is one of four (hash, bytes, name,
    # alternative) that a rule can try against it.
    "subtis": DERIVABLE,
    "subtitrarinoi": DERIVABLE,
    "subtitriid": DERIVABLE,
    "subtitulamostv": PREFIX,
    "subx": DERIVABLE,
    "supersubtitles": DERIVED,
    "titlovi": DERIVED,
    # The built-in's id is the position of the result in the search page, not
    # the site's id the plugin uses, so no old row names the new subtitle.
    "titrari": LOST,
    "titulky": DERIVED,
    "turkcealtyaziorg": DERIVABLE,  # case and dash change; movies keep the raw id
    "tvsubtitles": DERIVED,
    "whisperai": LOST,  # digest, and the old id held the video's original name
    "wizdom": DERIVED,
    "yavkanet": LOST,  # digest over the page URL
    "yifysubtitles": DERIVABLE,
    "zimuku": LOST,  # digest over the language label of the detail page
}

# The prefix rows read but not run, where the two sides may still differ in
# form: the plugin takes its id from a normalised URL, or the old class built
# it from a differently named field. A rewrite that is wrong here yields an id
# no subtitle has, so the default applies the rule; a caller that wants only
# the rest passes ``confirmed_only=True``.
PREFIX_UNCONFIRMED = frozenset({"karagarga", "ktuvit", "subtitulamostv"})

# Bazarr's own codes for the languages babelfish has no plain code for, and the
# ISO 639-3 code a plugin reports for each.
_BAZARR_CODES = {"pb": "por", "zt": "zho", "ea": "spa"}

# Derived rules whose plugin id ends in the row's language as an ISO 639-3
# code, in the form ``<provider>-<old id>-<alpha3>``.
_WITH_ALPHA3 = frozenset({
    "betaseries", "opensubtitlescom", "titlovi", "titulky", "tvsubtitles",
})

_DIGITS = re.compile(r"\d+")


def host_subtitle_id(provider_id: str, worker_id: str) -> str:
    """The id the host gives a plugin subtitle (see ``HubWorkerSubtitle.id``)."""
    return f"{provider_id}:{worker_id}"


def _row_language(language: str | None) -> tuple[str, bool] | None:
    """``(alpha3, forced)`` for a row's language, or None.

    Stored forms: ``Language.basename`` (``en``, ``pt-BR``, ``sr-Cyrl``) or
    Bazarr's code2 (``en``, ``pb``, ``zt``, ``ea``), each with ``:forced`` or
    ``:hi`` (any case) appended for those variants.
    """
    if not language or not isinstance(language, str):
        return None
    base, _, variant = language.strip().partition(":")
    base = base.strip()
    alpha3 = _BAZARR_CODES.get(base.lower())
    if alpha3 is None:
        try:
            alpha3 = Language.fromietf(base).alpha3
        except (BabelfishError, ValueError, KeyError):
            return None
    return alpha3, variant.strip().lower() == "forced"


def rewrite_legacy_subs_id(
    provider_id: str, subs_id: str, language: str | None = None, *, confirmed_only: bool = False,
) -> str | None:
    """The id the catalog plugin of ``provider_id`` gives the subtitle that the
    built-in stored as ``subs_id``, or None when the row cannot be followed.

    ``language`` is the row's language column. None is returned, never a
    guess, for a provider whose class is not ``prefix`` or ``derived``, for an
    id that does not have the built-in's shape, and for a derived rule whose
    language is missing or unreadable. ``confirmed_only`` also withholds the
    rules in ``PREFIX_UNCONFIRMED``.
    """
    kind = LEGACY_ID_CLASS.get(provider_id)
    if kind not in (PREFIX, DERIVED) or not isinstance(subs_id, str) or not subs_id:
        return None
    if confirmed_only and provider_id in PREFIX_UNCONFIRMED:
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

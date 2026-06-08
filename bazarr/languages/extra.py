# coding=utf-8
"""Register ISO 639-3 languages missing from babelfish's bundled data snapshot.

babelfish ships a pre-2017 iso-639-3 table that lacks some codes - notably Montenegrin
("cnr"), added to ISO 639-3 in 2017 - so Language("cnr") raises. We register them at
startup so they resolve like any other language across subzero, subliminal, the Provider
Hub registry, and the UI. Idempotent and safe to call repeatedly.

Registration touches several structures that are built once at import:
  * LANGUAGE_MATRIX / LANGUAGES        - so Language("cnr") constructs;
  * NameConverter.SYMBOLS              - so .name / fromname work (it's a class-level dict
                                         built from LANGUAGE_MATRIX at module import);
  * language_converters cache          - cleared so already-loaded converters rebuild;
  * subzero FULL_LANGUAGE_LIST/ALPHA3b - so external-subtitle suffix stripping recognises
                                         ".cnr" (subzero.language builds these from the
                                         pre-patch matrix when imported before this runs).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# (alpha3, babelfish alpha2, settings code2, name)
#
# babelfish alpha2 is the real ISO 639-1 code (empty when none exists).
# settings code2 is the 2-letter key Bazarr's settings/profile UI uses to list and enable
# a language; since cnr has no ISO 639-1 code we assign a synthetic one ("me", matching
# Montenegro) so Montenegrin is selectable in profiles - otherwise the languages table
# (which only ingests ISO 639-1 languages) would never get a row for it.
_EXTRA_LANGUAGES = [
    ("cnr", "", "me", "Montenegrin"),
]


def extra_settings_language_rows() -> list[dict]:
    """Rows to add to TableSettingsLanguages so these languages are selectable in profiles."""
    return [
        {"code3": alpha3, "code2": settings_code2, "name": name}
        for alpha3, _alpha2, settings_code2, name in _EXTRA_LANGUAGES
    ]


def register_extra_languages() -> None:
    try:
        from babelfish import language as babelfish_language
        from babelfish import language_converters
        from babelfish.converters.name import NameConverter
    except Exception:  # pragma: no cover - babelfish is always available
        return

    matrix = getattr(babelfish_language, "LANGUAGE_MATRIX", None)
    known = getattr(babelfish_language, "LANGUAGES", None)
    if not matrix or known is None:
        return

    iso_language = getattr(babelfish_language, "IsoLanguage", None) or type(matrix[0])

    added = False
    for alpha3, alpha2, _settings_code2, name in _EXTRA_LANGUAGES:
        if alpha3 in known:
            continue
        try:
            # alpha3b/alpha3t (ISO 639-2 bibliographic/terminologic) are intentionally empty:
            # cnr is a 639-3-only code with no 639-2 assignment, exactly like the ~7455 other
            # 639-3-only languages babelfish ships with empty 3b/3t. Reverse lookups for cnr go
            # through Language()/fromalpha3/fromietf/fromname (all work post-registration);
            # fromalpha3b is not the lookup path for 639-3 codes and raises for every such
            # language. Faking 3b/3t='cnr' would be the only inconsistency in the matrix and is
            # covered against by test_montenegrin_alpha3b_is_unset_matching_babelfish_convention.
            matrix.append(
                iso_language(
                    alpha3=alpha3,
                    alpha3b="",
                    alpha3t="",
                    alpha2=alpha2,
                    scope="I",
                    type="L",
                    name=name,
                    comment="",
                )
            )
            known.add(alpha3)
            if name:
                NameConverter.SYMBOLS[alpha3] = name
            added = True
            logger.debug("Registered extra language %s (%s)", alpha3, name)
        except Exception:
            logger.exception("Failed to register extra language %s", alpha3)

    if added:
        # Drop ONLY the cached name converter so it rebuilds from the updated SYMBOLS.
        # Clearing the whole cache would also evict converters guessit relies on, which
        # disrupts release parsing elsewhere in the process.
        try:
            language_converters.converters.pop("name", None)
        except Exception:
            logger.exception("Failed to refresh babelfish name converter")

    # Always refresh subzero's cached language lists (idempotent). subzero.language builds
    # them from LANGUAGE_MATRIX at import, which - via subliminal_patch.extensions - happens
    # before this runs at startup, so the lists would otherwise miss our codes regardless of
    # whether this particular call added anything to babelfish.
    _refresh_subzero_language_lists()


def _refresh_subzero_language_lists() -> None:
    # _search_external_subtitles strips a trailing ".<lang>" from a filename only when the
    # token is in subzero.language.FULL_LANGUAGE_LIST, so an external file like "Movie.cnr.srt"
    # is skipped under strict matching unless "cnr" is in that list. The list is a plain
    # module-level list built once at import; append our alpha3 codes in place. Only patch if
    # subzero.language is already imported - if it is imported later it builds from the
    # now-patched matrix and includes the codes anyway.
    import sys

    sl = sys.modules.get("subzero.language")
    if sl is None:
        return
    try:
        for alpha3, _alpha2, _settings_code2, _name in _EXTRA_LANGUAGES:
            for list_name in ("ALPHA3b_LIST", "FULL_LANGUAGE_LIST"):
                lst = getattr(sl, list_name, None)
                if isinstance(lst, list) and alpha3 not in lst:
                    lst.append(alpha3)
    except Exception:
        logger.exception("Failed to refresh subzero language lists")

# coding=utf-8
"""Language normalization for a translation request.

An engine gets each language as a BCP-47 tag plus its English name, its script and
whether it is written right to left. Bazarr+ builds all four, so they are the same
for every engine and every engine is spared the quirks of Bazarr's own codes:
``zt`` (Traditional Chinese) is sent as ``zh-TW``, ``pb`` as ``pt-BR``, ``ea``
(Spanish, Latin America) as its regional tag ``es-419``, and Hebrew is ``he``, never
the retired ``iw``.

Pure functions. Nothing here reads settings, the database or any translation
service, so both the old and the new translation paths can use it.
"""
from __future__ import annotations

import re

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

AUTO_DETECT = "auto"


class UnsupportedLanguage(ValueError):
    """The code is not a language tag this module can send to an engine."""


@dataclass(frozen=True)
class LanguageSpec:
    """One language as an engine receives it."""

    code: str
    name: str
    script: str | None
    rtl: bool
    source_code: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "script": self.script, "rtl": self.rtl}


# Bazarr's own codes and the ones other places write for the same languages. The value is
# (tag, name, script). Keys are lower case.
_BAZARR_CODES: dict[str, tuple[str, str, str | None]] = {
    "zt": ("zh-TW", "Chinese (Traditional)", "Hant"),
    "zht": ("zh-TW", "Chinese (Traditional)", "Hant"),
    "zh-tw": ("zh-TW", "Chinese (Traditional)", "Hant"),
    "zh": ("zh-CN", "Chinese (Simplified)", "Hans"),
    "zho": ("zh-CN", "Chinese (Simplified)", "Hans"),
    "chi": ("zh-CN", "Chinese (Simplified)", "Hans"),
    "zh-cn": ("zh-CN", "Chinese (Simplified)", "Hans"),
    "pb": ("pt-BR", "Portuguese (Brazil)", None),
    "pob": ("pt-BR", "Portuguese (Brazil)", None),
    "pt": ("pt-PT", "Portuguese (Portugal)", None),
    "por": ("pt-PT", "Portuguese (Portugal)", None),
    "ea": ("es-419", "Spanish (Latin America)", None),
    "spl": ("es-419", "Spanish (Latin America)", None),
    "es-mx": ("es-419", "Spanish (Latin America)", None),
    "es-la": ("es-419", "Spanish (Latin America)", None),
    # Montenegrin has no ISO 639-1 code; Bazarr lists it under the settings code "me".
    "me": ("cnr", "Montenegrin", None),
    "cnr": ("cnr", "Montenegrin", None),
}

# Two-letter codes ISO 639 retired. Engines and models follow the current ones.
_RETIRED_CODES = {"iw": "he", "in": "id", "ji": "yi", "jw": "jv", "mo": "ro"}

_NAME_OVERRIDES = {
    "el": "Greek",
    "he": "Hebrew",
    "fa": "Persian",
    "ms": "Malay",
    "no": "Norwegian",
    "nb": "Norwegian (Bokmal)",
    "nn": "Norwegian (Nynorsk)",
}

_RTL_SCRIPTS = frozenset({"Arab", "Hebr", "Thaa", "Syrc", "Nkoo", "Adlm", "Rohg", "Mand", "Samr"})
_RTL_LANGUAGES = frozenset({"ar", "arc", "ckb", "dv", "fa", "he", "ks", "ps", "sd", "syr", "ug", "ur", "yi"})

# The script a language is written in when the tag does not say. Only languages that are not
# written in Latin script, so a language missing here has no stated script, not a wrong one.
_DEFAULT_SCRIPT = {
    **dict.fromkeys(("ar", "fa", "ur", "ps", "sd", "ug", "ckb", "ks"), "Arab"),
    **dict.fromkeys(("he", "yi"), "Hebr"),
    "dv": "Thaa", "syr": "Syrc",
    **dict.fromkeys(("ru", "uk", "bg", "be", "sr", "mk", "kk", "ky", "tg", "mn", "tt", "ba", "ce", "cv", "os"),
                    "Cyrl"),
    "el": "Grek", "hy": "Armn", "ka": "Geor",
    **dict.fromkeys(("hi", "mr", "ne", "sa"), "Deva"),
    "bn": "Beng", "as": "Beng", "pa": "Guru", "gu": "Gujr", "or": "Orya", "ta": "Taml", "te": "Telu",
    "kn": "Knda", "ml": "Mlym", "si": "Sinh", "th": "Thai", "lo": "Laoo", "km": "Khmr", "my": "Mymr",
    "bo": "Tibt", "dz": "Tibt", "am": "Ethi", "ti": "Ethi", "ko": "Kore", "ja": "Jpan", "iu": "Cans",
    "chr": "Cher", "zh": "Hans",
}

_TAG = re.compile(r"^(?P<lang>[A-Za-z]{2,3})(?:-(?P<script>[A-Za-z]{4}))?(?:-(?P<region>[A-Za-z]{2}|[0-9]{3}))?$")


@lru_cache(maxsize=None)
def _pycountry_language(code: str):
    try:
        import pycountry
    except ImportError:  # pragma: no cover - pycountry ships with Bazarr+
        return None
    attribute = "alpha_2" if len(code) == 2 else "alpha_3"
    try:
        return pycountry.languages.get(**{attribute: code})
    except (KeyError, LookupError):
        return None


@lru_cache(maxsize=None)
def _alpha2_of(language: str) -> str:
    """The two-letter code of a three-letter one, or the three-letter code when it has none."""
    if len(language) == 2:
        return language
    found = _pycountry_language(language)
    if found is None:
        # A bibliographic code (heb is terminologic, ger is bibliographic).
        try:
            import pycountry
            found = next((item for item in pycountry.languages if getattr(item, "bibliographic", None) == language),
                         None)
        except ImportError:  # pragma: no cover
            found = None
    alpha2 = getattr(found, "alpha_2", None)
    return alpha2 if alpha2 else language


@lru_cache(maxsize=None)
def _english_name(language: str) -> str:
    if language in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[language]
    found = _pycountry_language(language)
    return str(found.name) if found is not None else language


def _is_rtl(language: str, script: str | None) -> bool:
    if script is not None:
        return script in _RTL_SCRIPTS
    return language in _RTL_LANGUAGES


def normalize_language(code: str, name: str | None = None) -> LanguageSpec:
    """The language an engine receives for a Bazarr language code or a BCP-47 tag.

    ``name`` is used for the display name when the caller has one (the name in the profile); it
    never changes the tag. Raises UnsupportedLanguage for anything that is not a language tag.
    """
    if not isinstance(code, str) or not code.strip():
        raise UnsupportedLanguage("a language code is required")
    original = code.strip()
    text = original.replace("_", "-")
    known = _BAZARR_CODES.get(text.lower())
    if known is not None:
        tag, default_name, script = known
        language = tag.split("-")[0]
        script = script or _DEFAULT_SCRIPT.get(language)
        return LanguageSpec(tag, name or default_name, script, _is_rtl(language, script), original)

    match = _TAG.match(text)
    if match is None:
        raise UnsupportedLanguage(f"{original!r} is not a language code")
    language = match["lang"].lower()
    language = _RETIRED_CODES.get(language, language)
    language = _alpha2_of(language)
    if _pycountry_language(language) is None:
        raise UnsupportedLanguage(f"{original!r} is not a known language code")
    script = match["script"].title() if match["script"] else None
    region = match["region"].upper() if match["region"] else None
    tag = "-".join(part for part in (language, script, region) if part)
    effective_script = script or _DEFAULT_SCRIPT.get(language)
    return LanguageSpec(tag, name or _english_name(language), effective_script,
                        _is_rtl(language, effective_script), original)


def normalize_source_language(code: str | None, name: str | None = None) -> LanguageSpec:
    """The source language, or auto-detect when none is given."""
    if code is None or not str(code).strip() or str(code).strip().lower() in (AUTO_DETECT, "und"):
        return LanguageSpec(AUTO_DETECT, "Auto-detect", None, False, str(code or ""))
    return normalize_language(str(code), name)


def normalize_request_languages(source: str | None, target: str, *, source_name: str | None = None,
                                target_name: str | None = None) -> tuple[LanguageSpec, LanguageSpec]:
    return normalize_source_language(source, source_name), normalize_language(target, target_name)

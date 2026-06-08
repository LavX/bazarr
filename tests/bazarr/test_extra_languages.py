"""Montenegrin ("cnr") was added to ISO 639-3 in 2017 but is missing from babelfish's
bundled data snapshot, so Language("cnr") raises. register_extra_languages() registers it
at startup so it resolves like any other language.
"""
import pytest
from babelfish.exceptions import LanguageReverseError

from languages.extra import extra_settings_language_rows, register_extra_languages
from subzero.language import Language, language_from_stream


def test_register_extra_languages_adds_montenegrin():
    register_extra_languages()  # idempotent

    lang = Language("cnr")
    assert lang.alpha3 == "cnr"
    assert lang.name == "Montenegrin"
    # resolvable via IETF parsing too
    assert Language.fromietf("cnr").alpha3 == "cnr"


def test_register_extra_languages_is_idempotent():
    register_extra_languages()
    register_extra_languages()
    assert Language("cnr").alpha3 == "cnr"


def test_montenegrin_resolves_through_real_lookup_paths():
    # cnr is an ISO 639-3-only code with no ISO 639-2 (bibliographic/terminologic) code. Like
    # the ~7455 other 639-3-only languages in babelfish's matrix, its alpha3b/alpha3t stay
    # empty, so Language.fromalpha3b('cnr') raises *by design*. The reverse-lookup paths Bazarr
    # actually uses all resolve cnr after registration.
    register_extra_languages()

    assert Language("cnr").name == "Montenegrin"
    assert Language.fromietf("cnr").alpha3 == "cnr"
    assert Language.fromname("Montenegrin").alpha3 == "cnr"
    # subzero's stream-language reverse path (fromietf -> fromalpha3t -> fromalpha3b) resolves
    # cnr on the first hop, so the empty alpha3b/alpha3t never matter here.
    assert language_from_stream("cnr").alpha3 == "cnr"


def test_montenegrin_alpha3b_is_unset_matching_babelfish_convention():
    # Guard against a well-meaning "fix" that populates alpha3b/alpha3t with 'cnr': that would
    # make cnr the only 639-3-only language in the matrix carrying a bogus ISO 639-2 code, and
    # fromalpha3b is not the lookup path for 639-3 codes anyway (it fails for every 639-3-only
    # language, not just cnr). Real cnr lookups go through Language()/fromietf/fromname.
    from babelfish import language as babelfish_language

    register_extra_languages()

    entry = next((e for e in babelfish_language.LANGUAGE_MATRIX if e.alpha3 == "cnr"), None)
    assert entry is not None
    assert entry.alpha3b == ""
    assert entry.alpha3t == ""
    with pytest.raises(LanguageReverseError):
        Language.fromalpha3b("cnr")


def test_extra_settings_language_rows_makes_montenegrin_selectable():
    # cnr has no ISO 639-1 code, so it needs an explicit settings-languages row with a
    # synthetic code2 to be selectable in profiles (load_language_in_db only ingests
    # ISO 639-1 languages otherwise).
    rows = extra_settings_language_rows()
    cnr = next((r for r in rows if r["code3"] == "cnr"), None)
    assert cnr is not None
    assert cnr["code2"] == "me"
    assert cnr["name"] == "Montenegrin"

"""Montenegrin ("cnr") was added to ISO 639-3 in 2017 but is missing from babelfish's
bundled data snapshot, so Language("cnr") raises. register_extra_languages() registers it
at startup so it resolves like any other language.
"""
from languages.extra import register_extra_languages
from subzero.language import Language


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

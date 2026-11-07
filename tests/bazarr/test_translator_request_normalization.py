"""Language normalization for translation requests: every Bazarr language, custom code and retired code."""
import pytest

from subtitles.tools.translate.runtime.request import (
    AUTO_DETECT, LanguageSpec, UnsupportedLanguage, normalize_language, normalize_request_languages,
    normalize_source_language)

# Bazarr's codes and their neighbours, as an engine must receive them.
# (input, tag, name, script, rtl)
MAPPING = [
    ("zt", "zh-TW", "Chinese (Traditional)", "Hant", False),
    ("zht", "zh-TW", "Chinese (Traditional)", "Hant", False),
    ("zh-TW", "zh-TW", "Chinese (Traditional)", "Hant", False),
    ("zh", "zh-CN", "Chinese (Simplified)", "Hans", False),
    ("zho", "zh-CN", "Chinese (Simplified)", "Hans", False),
    ("zh-CN", "zh-CN", "Chinese (Simplified)", "Hans", False),
    ("zh-Hant", "zh-Hant", "Chinese", "Hant", False),
    ("pb", "pt-BR", "Portuguese (Brazil)", None, False),
    ("pob", "pt-BR", "Portuguese (Brazil)", None, False),
    ("pt", "pt-PT", "Portuguese (Portugal)", None, False),
    ("pt_br", "pt-BR", "Portuguese", None, False),
    ("ea", "es-419", "Spanish (Latin America)", None, False),
    ("spl", "es-419", "Spanish (Latin America)", None, False),
    ("es-419", "es-419", "Spanish", None, False),
    ("he", "he", "Hebrew", "Hebr", True),
    ("iw", "he", "Hebrew", "Hebr", True),
    ("heb", "he", "Hebrew", "Hebr", True),
    ("ar", "ar", "Arabic", "Arab", True),
    ("fa", "fa", "Persian", "Arab", True),
    ("ur", "ur", "Urdu", "Arab", True),
    ("yi", "yi", "Yiddish", "Hebr", True),
    ("ji", "yi", "Yiddish", "Hebr", True),
    ("dv", "dv", "Divehi", "Thaa", True),
    ("en", "en", "English", None, False),
    ("eng", "en", "English", None, False),
    ("ja", "ja", "Japanese", "Jpan", False),
    ("ko", "ko", "Korean", "Kore", False),
    ("el", "el", "Greek", "Grek", False),
    ("ru", "ru", "Russian", "Cyrl", False),
    ("hi", "hi", "Hindi", "Deva", False),
    ("th", "th", "Thai", "Thai", False),
    ("id", "id", "Indonesian", None, False),
    ("in", "id", "Indonesian", None, False),
    ("jw", "jv", "Javanese", None, False),
    ("sr", "sr", "Serbian", "Cyrl", False),
    ("sr-latn", "sr-Latn", "Serbian", "Latn", False),
    ("me", "cnr", "Montenegrin", None, False),
    ("cnr", "cnr", "Montenegrin", None, False),
    ("az-arab", "az-Arab", "Azerbaijani", "Arab", True),
    ("ku-latn", "ku-Latn", "Kurdish", "Latn", False),
    ("ar-latn", "ar-Latn", "Arabic", "Latn", False),
    ("ZH-hant-tw", "zh-Hant-TW", "Chinese", "Hant", False),
    ("  fr  ", "fr", "French", None, False),
    ("ger", "de", "German", None, False),
    ("fre", "fr", "French", None, False),
]


@pytest.mark.parametrize("code, tag, name, script, rtl", MAPPING)
def test_the_mapping_table(code, tag, name, script, rtl):
    spec = normalize_language(code)
    assert (spec.code, spec.name, spec.script, spec.rtl) == (tag, name, script, rtl)
    assert spec.source_code == code.strip()


def test_hebrew_is_never_sent_as_the_retired_iw():
    for code in ("he", "iw", "heb", "HE", "he-IL"):
        assert normalize_language(code).code.split("-")[0] == "he"


def test_a_profile_name_replaces_only_the_display_name():
    spec = normalize_language("zt", "Traditional Chinese")
    assert (spec.code, spec.name, spec.script) == ("zh-TW", "Traditional Chinese", "Hant")


def test_the_wire_form_carries_the_four_facts():
    assert normalize_language("he").to_wire() == {"code": "he", "name": "Hebrew", "script": "Hebr", "rtl": True}


@pytest.mark.parametrize("bad", ["", "   ", None, 5, "x", "e n", "english", "123", "zz", "zzz", "en-", "en-x-y",
                                 "en-US-POSIX", "e1", "../x"])
def test_something_that_is_not_a_language_is_refused(bad):
    with pytest.raises(UnsupportedLanguage):
        normalize_language(bad)


def test_the_source_can_be_auto_detected():
    for empty in (None, "", "auto", "AUTO", "und"):
        spec = normalize_source_language(empty)
        assert (spec.code, spec.script, spec.rtl) == (AUTO_DETECT, None, False)
    assert normalize_source_language("de").code == "de"
    source, target = normalize_request_languages(None, "zt")
    assert (source.code, target.code) == (AUTO_DETECT, "zh-TW")
    source, target = normalize_request_languages("he", "ea", source_name="Hebrew", target_name="Spanish (Latino)")
    assert (source.code, source.rtl, target.code, target.name) == ("he", True, "es-419", "Spanish (Latino)")


def test_every_bazarr_standard_language_reaches_the_engine_as_a_valid_tag():
    """Every language Bazarr lists from ISO 639-1 resolves, to its own code unless Bazarr maps it."""
    import pycountry

    special = {"zh": "zh-CN", "pt": "pt-PT"}
    seen = 0
    for language in pycountry.languages:
        alpha2 = getattr(language, "alpha_2", None)
        if not alpha2:
            continue
        seen += 1
        spec = normalize_language(alpha2)
        assert spec.code == special.get(alpha2, alpha2), alpha2
        assert isinstance(spec, LanguageSpec) and spec.name and isinstance(spec.rtl, bool)
        assert spec.code != "iw"
        assert normalize_language(language.alpha_3).code == spec.code, language.alpha_3
        bibliographic = getattr(language, "bibliographic", None)
        if bibliographic:
            assert normalize_language(bibliographic).code == spec.code, bibliographic
    assert seen > 150


def test_every_right_to_left_language_is_flagged_and_no_other_is():
    import pycountry

    rtl = {"ar", "ckb", "dv", "fa", "he", "ks", "ps", "sd", "ug", "ur", "yi"}
    flagged = {language.alpha_2 for language in pycountry.languages
               if getattr(language, "alpha_2", None) and normalize_language(language.alpha_2).rtl}
    assert flagged == rtl & {language.alpha_2 for language in pycountry.languages
                             if getattr(language, "alpha_2", None)}
    assert {"ar", "he", "fa", "ur", "yi", "dv", "ps", "sd", "ug"} <= flagged


def test_every_custom_language_bazarr_defines_has_a_mapping():
    """A custom language added without a mapping here would reach an engine as a code nobody knows."""
    from app import database  # noqa: F401  (imported first: custom_lang needs the database module loaded)
    from languages.custom_lang import CustomLanguage

    customs = CustomLanguage.__subclasses__()
    assert {c.alpha2 for c in customs} >= {"pb", "pt", "zt", "ea"}
    for custom in customs:
        expected = custom.regional_tag or custom.language
        assert normalize_language(custom.alpha2).code == expected, custom.alpha2
        assert normalize_language(custom.alpha3).code == expected, custom.alpha3
        assert normalize_language(custom.language).code == expected, custom.language


def test_the_extra_iso_639_3_languages_bazarr_adds_have_a_mapping():
    from languages.extra import _EXTRA_LANGUAGES

    for alpha3, _alpha2, settings_code2, _name in _EXTRA_LANGUAGES:
        assert normalize_language(settings_code2).code == alpha3
        assert normalize_language(alpha3).code == alpha3


def test_the_regional_tag_a_translated_file_is_named_with_is_the_tag_an_engine_receives():
    from app import database  # noqa: F401
    from languages.custom_lang import LatinAmericanSpanish

    assert normalize_language("ea").code == LatinAmericanSpanish.regional_tag == "es-419"


def test_the_module_does_not_reach_for_the_old_services_or_the_database():
    import ast
    from pathlib import Path

    runtime = Path(__file__).parents[2] / "bazarr/subtitles/tools/translate/runtime"
    forbidden = ("subtitles.tools.translate.services", "app.database", "app.config", "languages", "app.jobs_queue")
    for name in ("request.py", "output_checks.py"):
        tree = ast.parse((runtime / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [("." * node.level) + (node.module or "")]
            for module in modules:
                assert not module.startswith(forbidden), (name, module)
                assert not module.startswith(".."), (name, module)

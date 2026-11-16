# coding=utf-8
"""Parity between a built-in provider's subtitle id and its catalog plugin's.

A blacklist row names a subtitle by ``(provider, subs_id)``. The pool drops a
candidate when that pair is in the blacklist, so a switch from the built-in to
the plugin only keeps a row working if the plugin's host id equals the stored
one, or the row is rewritten to it. The fixtures pair what each built-in
stores with the id its plugin builds. The built-in side comes from this
repository's provider classes; the plugin side is transcribed from the id
formulas of the catalog at ``beta`` commit f51d041, read but not run, so a
catalog change to an id format is not caught here.
"""
import pytest
from babelfish import Language

from provider_hub import legacy_subs_id as legacy
from provider_hub.legacy_subs_id import (
    DERIVABLE, DERIVED, LEGACY_ID_CLASS, LOST, PREFIX, PREFIX_UNCONFIRMED, host_subtitle_id,
    rewrite_legacy_subs_id,
)
from provider_hub.migration import MIGRATED_BUILT_IN_PROVIDER_IDS
from provider_hub.protocol import HubWorkerSubtitle
from subliminal_patch.core import _Blacklist


# (provider, id the built-in stored, row language, id the plugin's subtitle has in the host)
REWRITES = [
    # Same raw id, new prefix.
    ("subsource", "555123", None, "subsource:555123"),
    ("subdl", "sd1234567", None, "subdl:sd1234567"),
    ("ktuvit", "91827", None, "ktuvit:91827"),
    ("karagarga", "https://forum.karagarga.in/viewtopic.php?t=1", None,
     "karagarga:https://forum.karagarga.in/viewtopic.php?t=1"),
    ("subtitulamostv", "https://www.subtitulamos.tv/subtitles/31/download", None,
     "subtitulamostv:https://www.subtitulamos.tv/subtitles/31/download"),
    # The plugin id repeats the provider and ends in the language.
    ("betaseries", "12345", "en", "betaseries:betaseries-12345-eng"),
    ("titulky", "98001", "cs", "titulky:titulky-98001-ces"),
    ("titlovi", "444", "pt-BR", "titlovi:titlovi-444-por"),
    ("titlovi", "445", "sr-Cyrl", "titlovi:titlovi-445-srp"),
    ("tvsubtitles", "7788", "fr", "tvsubtitles:tvsubtitles-7788-fra"),
    ("opensubtitlescom", "3344", "de", "opensubtitlescom:opensubtitlescom-3344-deu"),
    ("opensubtitlescom", "3345", "de:hi", "opensubtitlescom:opensubtitlescom-3345-deu"),
    ("subsro", "6001", "ro", "subsro:subsro-6001-ron"),
    ("supersubtitles", "77", "hu", "supersubtitles:supersubtitles-77-hun"),
    ("supersubtitles", "77", "hu:forced", "supersubtitles:supersubtitles-77-hun-forced"),
    # No language in the plugin id.
    ("wizdom", "321", None, "wizdom:wizdom-321"),
    ("bsplayer", "5566", "en", "bsplayer:bsplayer-5566"),
    ("greeksubs", "tok3n", None, "greeksubs:greeksubs-tok3n"),
    # The built-in joined the provider name with an underscore.
    ("hdbits", "hdbits_5519", "en", "hdbits:hdbits-5519"),
]


@pytest.mark.parametrize("provider, old, language, expected", REWRITES)
def test_a_rewritten_row_equals_the_id_the_plugin_subtitle_has(provider, old, language, expected):
    assert rewrite_legacy_subs_id(provider, old, language) == expected


@pytest.mark.parametrize("provider, old, language, expected", REWRITES)
def test_the_expected_id_is_what_the_host_gives_a_worker_subtitle(provider, old, language, expected):
    worker_id = expected.split(":", 1)[1]
    subtitle = HubWorkerSubtitle("providerhub", provider, worker_id, Language("eng"), {})
    assert subtitle.id == expected == host_subtitle_id(provider, worker_id)


@pytest.mark.parametrize("provider, old, language, expected", REWRITES)
def test_the_old_row_stops_suppressing_and_the_rewritten_row_suppresses(provider, old, language, expected):
    worker_id = expected.split(":", 1)[1]
    subtitle = HubWorkerSubtitle("providerhub", provider, worker_id, Language("eng"), {})

    assert _Blacklist([(provider, old)]).is_valid(provider, subtitle) is True
    assert _Blacklist([(provider, expected)]).is_valid(provider, subtitle) is False


def test_the_table_covers_exactly_the_migrated_built_ins():
    assert set(LEGACY_ID_CLASS) == set(MIGRATED_BUILT_IN_PROVIDER_IDS)
    assert len(LEGACY_ID_CLASS) == 56


def test_the_audit_tally_holds():
    classes = list(LEGACY_ID_CLASS.values())
    assert classes.count(PREFIX) == 5
    assert classes.count(DERIVED) == 11
    assert classes.count(DERIVED) + classes.count(DERIVABLE) == 41
    assert classes.count(LOST) == 10


def test_every_written_rule_is_a_derived_or_prefix_provider_and_only_those():
    written = {provider for provider, *_ in REWRITES}
    assert written == {p for p, kind in LEGACY_ID_CLASS.items() if kind in (PREFIX, DERIVED)}


def test_unconfirmed_prefix_rows_are_prefix_rows():
    assert PREFIX_UNCONFIRMED <= {p for p, kind in LEGACY_ID_CLASS.items() if kind == PREFIX}


@pytest.mark.parametrize("provider", sorted(p for p, kind in LEGACY_ID_CLASS.items() if kind in (LOST, DERIVABLE)))
def test_a_provider_without_a_written_rule_is_never_rewritten(provider):
    for language in (None, "en", "en:forced"):
        assert rewrite_legacy_subs_id(provider, "12345", language) is None


def test_an_unknown_provider_is_never_rewritten():
    assert rewrite_legacy_subs_id("not-a-provider", "1", "en") is None


@pytest.mark.parametrize("provider", ["betaseries", "titulky", "titlovi", "tvsubtitles",
                                      "opensubtitlescom", "subsro", "supersubtitles"])
@pytest.mark.parametrize("language", [None, "", "xx", "not a language"])
def test_a_rule_that_needs_the_language_returns_none_without_one(provider, language):
    assert rewrite_legacy_subs_id(provider, "1001", language) is None


@pytest.mark.parametrize("provider", ["subsource", "wizdom", "greeksubs", "bsplayer"])
def test_an_empty_or_non_text_id_is_not_rewritten(provider):
    assert rewrite_legacy_subs_id(provider, "", "en") is None
    assert rewrite_legacy_subs_id(provider, None, "en") is None
    assert rewrite_legacy_subs_id(provider, 12345, "en") is None


def test_an_hdbits_id_without_the_built_in_shape_is_not_rewritten():
    assert rewrite_legacy_subs_id("hdbits", "5519", "en") is None
    assert rewrite_legacy_subs_id("hdbits", "hdbits_abc", "en") is None


def test_a_subsro_row_holding_a_page_link_is_not_rewritten():
    assert rewrite_legacy_subs_id("subsro", "https://subs.ro/subtitrare/x-1", "ro") is None


def test_the_forced_suffix_only_exists_for_the_provider_whose_plugin_writes_it():
    assert rewrite_legacy_subs_id("titulky", "5", "cs:forced") == "titulky:titulky-5-ces"
    assert rewrite_legacy_subs_id("supersubtitles", "5", "hu:forced").endswith("-hun-forced")


def test_two_providers_never_rewrite_to_the_same_id():
    seen = {}
    for provider, old, language, _expected in REWRITES:
        rewritten = rewrite_legacy_subs_id(provider, old, language)
        assert seen.setdefault(rewritten, provider) == provider


def test_the_module_does_not_reach_the_database():
    assert not hasattr(legacy, "mutate_state")
    assert not any(name in vars(legacy) for name in ("database", "TableBlacklist", "TableHistory"))


@pytest.mark.parametrize("provider, kind", [
    ("animetosho", LOST),  # hashes a torrent entry id the row never held
    ("subtis", DERIVABLE),  # hashes the stored lookup URL and its method
    ("shooter", DERIVABLE),  # hashes the stored URL, the language and the media hash
    ("titrari", LOST),  # the built-in's id is a result position, not the site id
    ("subtitrarinoi", LOST),  # same: a result position
    ("opensubtitles", LOST),  # rows do not say which mode wrote them
    ("subsynchro", DERIVABLE),  # the stored URL, hashed on the legacy AJAX path
    ("subs4series", DERIVABLE),  # the stored detail URL and the alpha3
    ("zimuku", DERIVABLE),  # the stored page link and a tag of a closed set
    ("napiprojekt", DERIVABLE),  # the stored video hash and one of a few sources
    ("yavkanet", LOST),  # several per-file rows collapse onto one archive id
    ("subssabbz", LOST),  # the old page link and file name have no delimiter
    ("subsunacs", LOST),  # same, and an identity the row lacks
    ("whisperai", DERIVABLE),  # transcription rows only
    ("jimaku", LOST),  # unconfirmed: derivable if the file URL embeds the entry id
])
def test_the_reviewed_classes_are_pinned(provider, kind):
    assert LEGACY_ID_CLASS[provider] == kind
    assert rewrite_legacy_subs_id(provider, "https://x/y", "en") is None


# Language column forms as Bazarr persists them: code2 for the history rows and
# for the UI, basename for a pool-recorded blacklist row, each with a variant.
PERSISTED_LANGUAGES = [
    ("en", "eng", False), ("pt", "por", False), ("pt-BR", "por", False), ("sr-Cyrl", "srp", False),
    ("pb", "por", False), ("zt", "zho", False), ("ea", "spa", False),
    ("pb:forced", "por", True), ("zt:forced", "zho", True), ("ea:forced", "spa", True),
    ("pb:hi", "por", False), ("zt:HI", "zho", False), ("ea:hi", "spa", False),
    ("en:forced", "eng", True), ("en:hi", "eng", False), ("hu:Forced", "hun", True),
]


@pytest.mark.parametrize("stored, alpha3, forced", PERSISTED_LANGUAGES)
def test_every_persisted_language_form_reaches_the_plugin_alpha3(stored, alpha3, forced):
    assert rewrite_legacy_subs_id("titlovi", "77", stored) == f"titlovi:titlovi-77-{alpha3}"
    expected = f"supersubtitles:supersubtitles-77-{alpha3}{'-forced' if forced else ''}"
    assert rewrite_legacy_subs_id("supersubtitles", "77", stored) == expected


def test_the_bazarr_codes_map_to_the_alpha3_the_plugins_report():
    from languages.custom_lang import BrazilianPortuguese, ChineseTraditional, LatinAmericanSpanish

    for custom in (BrazilianPortuguese(), ChineseTraditional(), LatinAmericanSpanish()):
        assert rewrite_legacy_subs_id("titlovi", "1", custom.alpha2) == (
            f"titlovi:titlovi-1-{custom.official_alpha3}")


def test_a_plain_row_for_a_site_marked_forced_subtitle_is_the_documented_gap():
    # The plugin would have called this subtitle supersubtitles-77-hun-forced.
    assert rewrite_legacy_subs_id("supersubtitles", "77", "hu") == "supersubtitles:supersubtitles-77-hun"


def test_confirmed_only_withholds_the_prefix_rules_read_but_not_run():
    for provider in PREFIX_UNCONFIRMED:
        assert rewrite_legacy_subs_id(provider, "9", None) == f"{provider}:9"
        assert rewrite_legacy_subs_id(provider, "9", None, confirmed_only=True) is None
    assert rewrite_legacy_subs_id("subsource", "9", None, confirmed_only=True) == "subsource:9"
    assert rewrite_legacy_subs_id("betaseries", "9", "en", confirmed_only=True) == "betaseries:betaseries-9-eng"

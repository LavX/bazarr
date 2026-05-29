# coding=utf-8

from api.utils import _subtitle_language_details


def test_plain_language_has_no_modifier():
    d = _subtitle_language_details("en")
    assert d["modifier"] is None
    assert d["hi"] is False and d["forced"] is False


def test_sync_modifier_is_extracted():
    d = _subtitle_language_details("en:sync-ffsubsync")
    assert d["modifier"] == "sync-ffsubsync"


def test_combined_modifier_is_extracted():
    d = _subtitle_language_details("en:combined-hu")
    assert d["modifier"] == "combined-hu"


def test_combined_three_languages():
    d = _subtitle_language_details("de:combined-es-zh")
    assert d["modifier"] == "combined-es-zh"
    assert d["base"] == "de"


def test_hi_plus_combined():
    d = _subtitle_language_details("en:hi:combined-hu")
    assert d["modifier"] == "combined-hu"
    assert d["hi"] is True

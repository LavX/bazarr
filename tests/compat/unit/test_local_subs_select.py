def test_parse_subtitles_blob_well_formed():
    from compat.local_subs import _parse_subtitles_blob
    raw = "[['en', '/x/m.en.srt'], ['en:hi', '/x/m.en.hi.srt']]"
    parsed = _parse_subtitles_blob(raw)
    assert parsed == [["en", "/x/m.en.srt"], ["en:hi", "/x/m.en.hi.srt"]]


def test_parse_subtitles_blob_garbage_returns_empty():
    from compat.local_subs import _parse_subtitles_blob
    assert _parse_subtitles_blob("not a list") == []
    assert _parse_subtitles_blob("") == []
    assert _parse_subtitles_blob(None) == []


def test_parse_lang_code_plain():
    from compat.local_subs import _parse_lang_code
    assert _parse_lang_code("en") == ("en", None)


def test_parse_lang_code_with_modifier():
    from compat.local_subs import _parse_lang_code
    assert _parse_lang_code("en:hi") == ("en", "hi")
    assert _parse_lang_code("en:forced") == ("en", "forced")


def test_parse_lang_code_with_region():
    from compat.local_subs import _parse_lang_code
    assert _parse_lang_code("pt-BR") == ("pt-BR", None)
    assert _parse_lang_code("pt-BR:forced") == ("pt-BR", "forced")


def test_parse_request_bcp47():
    from compat.local_subs import _parse_request_bcp47
    assert _parse_request_bcp47("en") == ("en", None)
    assert _parse_request_bcp47("pt-BR") == ("pt", "BR")
    assert _parse_request_bcp47("zh-CN") == ("zh", "CN")


def test_lang_matches_request():
    from compat.local_subs import _lang_matches
    assert _lang_matches("en", "en", None)
    assert _lang_matches("pt-BR", "pt", "BR")
    assert not _lang_matches("pt", "pt", "BR")
    assert not _lang_matches("pt-PT", "pt", "BR")
    assert _lang_matches("pt", "pt", None)
    assert _lang_matches("pt-BR", "pt", None)


def test_resolve_subtitle_format_known():
    from compat.local_subs import _resolve_format
    assert _resolve_format("/x/foo.srt") == "srt"
    assert _resolve_format("/x/foo.ass") == "ass"
    assert _resolve_format("/x/foo.vtt") == "vtt"


def test_resolve_subtitle_format_skipped():
    from compat.local_subs import _resolve_format
    assert _resolve_format("/x/foo.idx") is None
    assert _resolve_format("/x/foo.sup") is None
    assert _resolve_format("/x/foo.unknown") is None

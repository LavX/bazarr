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


def test_select_returns_strict_region(tmp_path):
    from compat.local_subs import _select_local_subs
    pt_br = tmp_path / "movie.pt-BR.srt"
    pt_pt = tmp_path / "movie.pt-PT.srt"
    pt_br.write_text("hello")
    pt_pt.write_text("ola")
    raw = repr([["pt-BR", str(pt_br)], ["pt-PT", str(pt_pt)]])
    matches = _select_local_subs(raw, str(tmp_path), ["pt-BR"])
    assert len(matches) == 1
    assert matches[0]["lang"] == "pt-BR"
    assert matches[0]["modifier"] is None
    assert matches[0]["fmt"] == "srt"
    assert matches[0]["path"] == str(pt_br)


def test_select_surfaces_hi_and_forced_separately(tmp_path):
    from compat.local_subs import _select_local_subs
    plain = tmp_path / "m.en.srt"
    hi = tmp_path / "m.en.hi.srt"
    forced = tmp_path / "m.en.forced.srt"
    for f in (plain, hi, forced):
        f.write_text("x")
    raw = repr([["en", str(plain)], ["en:hi", str(hi)], ["en:forced", str(forced)]])
    matches = _select_local_subs(raw, str(tmp_path), ["en"])
    mods = sorted(m["modifier"] or "" for m in matches)
    assert mods == ["", "forced", "hi"]


def test_select_drops_missing_files(tmp_path):
    from compat.local_subs import _select_local_subs
    real = tmp_path / "real.en.srt"
    real.write_text("x")
    raw = repr([["en", str(real)], ["en", str(tmp_path / "ghost.en.srt")]])
    matches = _select_local_subs(raw, str(tmp_path), ["en"])
    assert len(matches) == 1
    assert matches[0]["path"] == str(real)


def test_select_drops_unsupported_formats(tmp_path):
    from compat.local_subs import _select_local_subs
    idx = tmp_path / "movie.en.idx"
    idx.write_text("x")
    raw = repr([["en", str(idx)]])
    matches = _select_local_subs(raw, str(tmp_path), ["en"])
    assert matches == []


def test_select_drops_paths_outside_media_dir(tmp_path):
    from compat.local_subs import _select_local_subs
    outside = tmp_path / "outside" / "evil.en.srt"
    outside.parent.mkdir()
    outside.write_text("x")
    raw = repr([["en", str(outside)]])
    inside_dir = tmp_path / "media"
    inside_dir.mkdir()
    matches = _select_local_subs(raw, str(inside_dir), ["en"])
    assert matches == []


def test_select_returns_empty_on_garbage_subtitles_blob():
    from compat.local_subs import _select_local_subs
    assert _select_local_subs("not a list", "/x", ["en"]) == []
    assert _select_local_subs("", "/x", ["en"]) == []
    assert _select_local_subs(None, "/x", ["en"]) == []

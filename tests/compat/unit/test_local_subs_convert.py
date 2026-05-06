def test_normalize_srt_strips_utf8_bom():
    from compat.local_subs import _normalize_srt
    raw = b"\xef\xbb\xbf1\r\n00:00:00,000 --> 00:00:01,000\r\nHi\r\n"
    out = _normalize_srt(raw)
    assert isinstance(out, bytes)
    assert not out.startswith(b"\xef\xbb\xbf")
    assert "Hi" in out.decode("utf-8")


def test_normalize_srt_passes_through_clean_utf8():
    from compat.local_subs import _normalize_srt
    raw = "1\n00:00:00,000 --> 00:00:01,000\nHi\n".encode("utf-8")
    assert _normalize_srt(raw) == raw


def test_normalize_srt_handles_cp1252_fallback():
    from compat.local_subs import _normalize_srt
    # Long enough that charset_normalizer can identify the encoding;
    # short cp1252 strings get misdetected as UTF-16 derivatives.
    raw = ("1\n00:00:00,000 --> 00:00:01,000\n"
           "L'h\xf4tel \xe9tait pr\xe8s du caf\xe9.\n").encode("cp1252")
    out = _normalize_srt(raw)
    decoded = out.decode("utf-8")
    assert "café" in decoded.lower()
    assert "hôtel" in decoded.lower() or "h" in decoded.lower()


def test_convert_ass_to_srt():
    from compat.local_subs import _convert_to_srt
    ass = (b"[Script Info]\n"
           b"ScriptType: v4.00+\n\n"
           b"[V4+ Styles]\n"
           b"Format: Name, Fontname, Fontsize\n"
           b"Style: Default,Arial,20\n\n"
           b"[Events]\n"
           b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           b"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello\n")
    out = _convert_to_srt(ass, "ass")
    text = out.decode("utf-8")
    assert "[Script Info]" not in text
    assert "00:00:00" in text
    assert "Hello" in text


def test_convert_vtt_to_srt():
    from compat.local_subs import _convert_to_srt
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
    out = _convert_to_srt(vtt, "vtt")
    assert b"WEBVTT" not in out
    assert b"Hello" in out


def test_convert_returns_empty_bytes_on_pysubs2_failure(monkeypatch):
    from compat import local_subs
    import pysubs2
    def _explode(*a, **k): raise RuntimeError("nope")
    monkeypatch.setattr(pysubs2.SSAFile, "from_string", staticmethod(_explode))
    out = local_subs._convert_to_srt(b"garbage", "ass")
    assert out == b""

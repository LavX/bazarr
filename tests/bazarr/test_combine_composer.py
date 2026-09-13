# coding=utf-8

import os

import pysubs2
import pytest

from subtitles.tools.combine.composer import compose

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "combine")


def fixture(name):
    return os.path.join(FIXTURES, name)


def test_srt_sibling_two_languages():
    out = compose(
        primary_path=fixture("en_hu_sibling_en.srt"),
        secondary_paths=[fixture("en_hu_sibling_hu.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    assert "Hello there." in text
    assert "Szia." in text
    assert text.count("-->") == 3
    cue1 = text.split("\n\n")[0]
    lines = [ln for ln in cue1.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert lines == ["Hello there.", "Szia."]


def test_srt_offset_alignment():
    out = compose(
        primary_path=fixture("en_hu_offset_en.srt"),
        secondary_paths=[fixture("en_hu_offset_hu.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    assert "Hello there." in text
    assert "Szia." in text
    cue1 = text.split("\n\n")[0]
    lines = [ln for ln in cue1.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert "Hello there." in lines
    assert "Szia." in lines


def test_srt_overlap_keeps_primary_only_when_no_match():
    out = compose(
        primary_path=fixture("en_hu_no_overlap_en.srt"),
        secondary_paths=[fixture("en_hu_no_overlap_hu.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    assert "Hello there." in text
    assert "How are you?" in text
    assert "Szia." not in text


def test_srt_trio_sibling():
    out = compose(
        primary_path=fixture("trio_en.srt"),
        secondary_paths=[fixture("trio_hu.srt"), fixture("trio_zh.srt")],
        format="srt",
    )
    text = out.decode("utf-8")
    cue1 = text.split("\n\n")[0]
    lines = [ln for ln in cue1.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert lines == ["Hello there.", "Szia.", "你好。"]


def write_subtitle(path, text, format, encoding="utf-8", bom=b""):
    if format == "srt":
        content = f"1\n00:00:01,000 --> 00:00:03,000\n{text}\n\n"
    else:
        content = (
            "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            f"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{text}\n"
        )
    path.write_bytes(bom + content.encode(encoding))


@pytest.mark.parametrize("input_format", ["srt", "ass"])
@pytest.mark.parametrize("output_format", ["srt", "ass"])
@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", b""),
    ("utf-8-sig", b""),
    ("utf-16-le", b"\xff\xfe"),
    ("utf-16-be", b"\xfe\xff"),
    ("utf-32-le", b"\xff\xfe\x00\x00"),
    ("utf-32-be", b"\x00\x00\xfe\xff"),
])
def test_unicode_punctuation_survives_combine(tmp_path, input_format, output_format, encoding, bom):
    english = "\u200eWait… I’ll be there."
    korean = "잠시만요… 곧 갈게요."
    primary = tmp_path / f"english.{input_format}"
    secondary = tmp_path / f"korean.{input_format}"
    write_subtitle(primary, english, input_format, encoding, bom)
    write_subtitle(secondary, korean, input_format, encoding, bom)
    originals = [path.read_bytes() for path in (primary, secondary)]

    output = compose(primary, [secondary], output_format)

    events = pysubs2.SSAFile.from_string(output.decode("utf-8")).events
    expected = [f"{english}\n{korean}"] if output_format == "srt" else [english, korean]
    assert [event.plaintext for event in events] == expected
    assert not output.startswith(b"\xef\xbb\xbf")
    assert [path.read_bytes() for path in (primary, secondary)] == originals


@pytest.mark.parametrize("output_format", ["srt", "ass"])
@pytest.mark.parametrize("encoding,text", [
    ("cp1250", "Árvíztűrő tükörfúrógép, ő és ű. A szép magyar nyelv a miénk."),
    ("cp1252", "“Wait…” she said. It’s a lovely café, costs €5, and feels très français."),
    ("cp1251", "Привет, мир! Сегодня прекрасный день для прогулки по городу."),
])
def test_legacy_windows_encoding_survives_combine(tmp_path, output_format, encoding, text):
    primary = tmp_path / "legacy.srt"
    secondary = tmp_path / "english.srt"
    write_subtitle(primary, text, "srt", encoding)
    write_subtitle(secondary, "Hello there.", "srt")
    originals = [path.read_bytes() for path in (primary, secondary)]

    output = compose(primary, [secondary], output_format)

    events = pysubs2.SSAFile.from_string(output.decode("utf-8")).events
    expected = [f"{text}\nHello there."] if output_format == "srt" else [text, "Hello there."]
    assert [event.plaintext for event in events] == expected
    assert [path.read_bytes() for path in (primary, secondary)] == originals


@pytest.mark.parametrize("source", [b"\xef\xbb\xbf\xff", b"\xff\xfe\x00", b"\xff\xfe\x00\x00\x00"])
def test_malformed_bom_encoding_is_not_reinterpreted(tmp_path, source):
    primary = tmp_path / "malformed.srt"
    primary.write_bytes(source)

    with pytest.raises(UnicodeDecodeError):
        compose(primary, [fixture("en_hu_sibling_hu.srt")], "srt")


def test_invalid_subtitle_format_propagates(tmp_path):
    primary = tmp_path / "invalid.srt"
    primary.write_text("This is ordinary text without subtitle timings.", encoding="utf-8")

    with pytest.raises(pysubs2.FormatAutodetectionError):
        compose(primary, [fixture("en_hu_sibling_hu.srt")], "srt")


def test_missing_subtitle_propagates(tmp_path):
    with pytest.raises(FileNotFoundError):
        compose(tmp_path / "missing.srt", [fixture("en_hu_sibling_hu.srt")], "srt")


def test_subtitle_filesystem_error_propagates(tmp_path):
    with pytest.raises(IsADirectoryError):
        compose(tmp_path, [fixture("en_hu_sibling_hu.srt")], "srt")


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_source_line_endings_are_normalized(tmp_path, newline):
    primary = tmp_path / "english.srt"
    primary.write_bytes(newline.join(["1", "00:00:01,000 --> 00:00:03,000", "Hello there.", "", ""]).encode())

    output = compose(primary, [fixture("en_hu_sibling_hu.srt")], "srt")

    assert b"\r" not in output
    assert output == "1\n00:00:01,000 --> 00:00:03,000\nHello there.\nSzia.\n\n".encode("utf-8")


class TestAssOutput:
    def test_ass_has_three_styles(self):
        out = compose(
            primary_path=fixture("trio_en.srt"),
            secondary_paths=[fixture("trio_hu.srt"), fixture("trio_zh.srt")],
            format="ass",
        )
        text = out.decode("utf-8")
        assert "[Script Info]" in text
        assert "Style: Bottom" in text
        assert "Style: Top" in text
        assert "Style: Middle" in text

    def test_ass_dialogue_lines_per_language(self):
        out = compose(
            primary_path=fixture("en_hu_sibling_en.srt"),
            secondary_paths=[fixture("en_hu_sibling_hu.srt")],
            format="ass",
        )
        text = out.decode("utf-8")
        # 3 primary cues, 2 languages each = 6 dialogue lines.
        assert text.count("Dialogue:") == 6
        assert "Dialogue: 0,0:00:01.00,0:00:03.00,Bottom" in text
        assert "Dialogue: 0,0:00:01.00,0:00:03.00,Top" in text

    def test_ass_alignment_codes(self):
        out = compose(
            primary_path=fixture("trio_en.srt"),
            secondary_paths=[fixture("trio_hu.srt"), fixture("trio_zh.srt")],
            format="ass",
        )
        text = out.decode("utf-8")
        # Alignment code 2 = bottom-center, 8 = top-center, 5 = middle-center.
        assert ",2," in text
        assert ",8," in text
        assert ",5," in text

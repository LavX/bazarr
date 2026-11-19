# coding=utf-8
# Remove HI mod: music cues wrapped in italic tags.
# See https://github.com/LavX/bazarr/issues/828
#
# pysubs2 turns SRT <i> tags into {\i1} / {\i0} on load, so the HI_music regex
# has to allow the tag pattern at the entry edges. These tests run the real mod
# over an SRT the way SubtitleModifications does in production.
import pytest

from subzero.language import Language
from subzero.modification.main import SubtitleModifications


def _modified(srt, mods, tmp_path):
    # Load from disk so pysubs2 parses the SRT the way it does for a subtitle
    # file: <i> becomes {\i1} / {\i0} before the mod sees it.
    path = tmp_path / "movie.en.srt"
    path.write_bytes(srt.encode("utf-8"))
    sm = SubtitleModifications()
    assert sm.load(fn=str(path), language=Language("eng"), mods=mods)
    sm.modify(*mods)
    return sm.f.to_string("srt")


def _lines(out):
    return [line.strip() for line in out.splitlines()]


# The mod only replaces the event list when at least one cue survives, so every
# removal test keeps a dialogue cue alongside the music cue, as real files do.
KEEP_CUE = "3\n00:00:05,000 --> 00:00:06,000\nHallo.\n\n"


@pytest.mark.parametrize("cue", [
    "<i>♪ MTV ♪</i>",
    "♪ bedrohliche Musik ♪",
])
def test_remove_hi_removes_music_cue(cue, tmp_path):
    srt = "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format(cue) + KEEP_CUE
    out = _modified(srt, ["remove_HI"], tmp_path)
    assert "MTV" not in out
    assert "bedrohliche Musik" not in out
    assert "Hallo." in out


def test_remove_hi_removes_two_line_tagged_music_cue(tmp_path):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n"
        "<i>♪ Und noch ein Lied</i>\n<i>das weitergeht ♪</i>\n\n"
    ) + KEEP_CUE
    out = _modified(srt, ["remove_HI"], tmp_path)
    assert "Und noch ein Lied" not in out
    assert "das weitergeht" not in out
    assert "Hallo." in out


def test_remove_hi_keeps_plain_dialogue(tmp_path):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHallo.\n\n" + KEEP_CUE
    out = _modified(srt, ["remove_HI"], tmp_path)
    assert "Hallo." in out


def test_remove_hi_keeps_mid_line_hash(tmp_path):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHe said # is a symbol\n\n" + KEEP_CUE
    out = _modified(srt, ["remove_HI"], tmp_path)
    assert "He said # is a symbol" in out


def test_remove_hi_keep_lyrics_keeps_tagged_lyric_line(tmp_path):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n"
        "<i>♪ Und noch ein Lied</i>\n<i>das weitergeht ♪</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n"
        "Hallo.\n"
    )
    out = _modified(srt, ["remove_HI(keep_lyrics=1)"], tmp_path)
    assert "Und noch ein Lied" in out
    assert "das weitergeht" in out
    assert "Hallo." in out


def test_remove_hi_keep_lyrics_still_drops_tagged_symbol_only_cue(tmp_path):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n<i>♪♪</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nHallo.\n"
    )
    out = _modified(srt, ["remove_HI(keep_lyrics=1)"], tmp_path)
    assert "♪♪" not in _lines(out)
    assert "Hallo." in out

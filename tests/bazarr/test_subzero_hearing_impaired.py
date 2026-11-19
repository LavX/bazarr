# coding=utf-8
# Remove HI mod: music cues wrapped in italic, bold or font tags.
# See https://github.com/LavX/bazarr/issues/828
#
# Two routes reach the mod, and they hand it different text:
#
# * "file": SubtitleModifications.load(fn=...) parses the SRT with pysubs2, which
#   turns <i> into {\i1} / {\i0} and drops unknown tags such as <font> (it does not
#   convert them). That route stands in for files that carry SSA override tags.
# * "content": Subtitle.get_modified_content, the path subtitles_apply_mods uses.
#   It loads the decoded text with keep_html_tags=True, so literal <i>, </i>,
#   <font ...> and </font> reach the mod unchanged.
#
# The HI_music rule has to match the music symbols on both routes, so every case
# below runs through both.
import re

import pytest

from subliminal_patch import Subtitle
from subzero.language import Language
from subzero.modification.main import SubtitleModifications


def _render_file(srt, mods, tmp_path):
    # Load from disk so pysubs2 parses the SRT the way it does for a subtitle file.
    path = tmp_path / "movie.en.srt"
    path.write_bytes(srt.encode("utf-8"))
    sm = SubtitleModifications()
    assert sm.load(fn=str(path), language=Language("eng"), mods=mods)
    sm.modify(*mods)
    return sm.f.to_string("srt")


def _render_content(srt, mods, tmp_path):
    # Mirror the production call in custom_libs/subliminal_patch/subtitle.py.
    sub = Subtitle(Language("eng"), mods=mods, original_format=True)
    sub.content = srt.encode("utf-8")
    assert sub.is_valid()
    content = sub.get_modified_content(format="srt")
    return content.decode("utf-8") if content else ""


_RENDERERS = {"file": _render_file, "content": _render_content}


@pytest.fixture(params=["file", "content"])
def render(request, tmp_path):
    renderer = _RENDERERS[request.param]
    return lambda srt, mods: renderer(srt, mods, tmp_path)


# The mod only replaces the event list when at least one cue survives, so every
# removal test keeps a dialogue cue alongside the music cue, as real files do.
KEEP_CUE = "3\n00:00:05,000 --> 00:00:06,000\nHallo.\n\n"


@pytest.mark.parametrize("cue", [
    "<i>♪ MTV ♪</i>",
    "<font color=\"#ff0000\">♪ MTV ♪</font>",
    "<b>♪ MTV ♪</b>",
    "♪ bedrohliche Musik ♪",
])
def test_remove_hi_removes_music_cue(cue, render):
    srt = "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format(cue) + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "MTV" not in out
    assert "bedrohliche Musik" not in out
    assert "Hallo." in out


def test_remove_hi_removes_two_line_tagged_music_cue(render):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n"
        "<i>♪ Und noch ein Lied</i>\n<i>das weitergeht ♪</i>\n\n"
    ) + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "Und noch ein Lied" not in out
    assert "das weitergeht" not in out
    assert "Hallo." in out


def test_remove_hi_keeps_plain_dialogue(render):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHallo.\n\n" + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "Hallo." in out


def test_remove_hi_keeps_mid_line_hash(render):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHe said # is a symbol\n\n" + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "He said # is a symbol" in out


def test_remove_hi_keep_lyrics_keeps_tagged_lyric_line(render):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n"
        "<i>♪ Und noch ein Lied</i>\n<i>das weitergeht ♪</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n"
        "Hallo.\n"
    )
    out = render(srt, ["remove_HI(keep_lyrics=1)"])
    assert "Und noch ein Lied" in out
    assert "das weitergeht" in out
    assert "Hallo." in out


def test_remove_hi_keep_lyrics_still_drops_tagged_symbol_only_cue(render):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n<i>♪♪</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nHallo.\n"
    )
    out = render(srt, ["remove_HI(keep_lyrics=1)"])
    assert "♪" not in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue", [
    "<i>[door creaks]</i>",
    "<i>(sighs)</i>",
])
def test_remove_hi_removes_tagged_bracket_cue(cue, render):
    srt = "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format(cue) + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "door creaks" not in out
    assert "sighs" not in out
    assert "<i></i>" not in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue", [
    "<i>♪♪</i>",
    "<b>♪ ♪</b>",
])
def test_remove_hi_removes_tagged_symbol_only_cue(cue, render):
    srt = "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format(cue) + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "♪" not in out
    assert "Hallo." in out


def _visible(out):
    """The text a viewer sees: markup (HTML or SSA override tags) removed."""
    return re.sub(r"<[^>]+>|\{\\[^}]*\}", "", out)


def _balanced(out, name):
    return out.count("<%s>" % name) == out.count("</%s>" % name)


def _cue(text):
    return "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format(text) + KEEP_CUE


@pytest.mark.parametrize("cue, kept, gone", [
    # a tag pair must never lose one half, and the words around it keep their spacing
    ("<i>[man on radio] All units respond.</i>", "All units respond.", "man on radio"),
    ("<i>I can't. [sighs]</i>", "I can't.", "sighs"),
    ("Well, <i>[sighs]</i> I guess so.", "Well, I guess so.", "sighs"),
    ("<i>He said [quietly] go</i>", "He said go", "quietly"),
])
def test_bracket_cue_keeps_words_and_balanced_tags(cue, kept, gone, render):
    out = render(_cue(cue), ["remove_HI"])
    assert kept in out
    assert gone not in out
    assert _balanced(out, "i")
    assert "Hallo." in out


def test_bracket_cue_spanning_lines_keeps_both_halves_balanced(render):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n"
        "<i>[radio chatter]\nAll units respond.</i>\n\n"
    ) + KEEP_CUE
    out = render(srt, ["remove_HI"])
    assert "radio chatter" not in out
    assert "All units respond." in out
    assert _balanced(out, "i")


def test_tagged_bracket_only_line_is_removed_whole(render):
    out = render(_cue("<i>[door creaks]</i>"), ["remove_HI"])
    assert "door creaks" not in out
    assert "<i" not in out
    assert "</i>" not in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue, line", [
    ("<i>He said # is a symbol</i>", "He said # is a symbol"),
    ("He said <i>#</i> is a symbol", "He said # is a symbol"),
    ("<font color=\"#ffffff\">Hello there.</font>", "Hello there."),
])
def test_remove_hi_keeps_tagged_text_with_hash(cue, line, render):
    out = render(_cue(cue), ["remove_HI"])
    assert line in _visible(out)
    assert "Hallo." in out


@pytest.mark.parametrize("cue, line", [
    ("<font color=\"#ffffff\">Hello there.</font>", "Hello there."),
    ("<i>He said # is a symbol</i>", "He said # is a symbol"),
])
def test_keep_lyrics_keeps_tagged_dialogue(cue, line, render):
    out = render(_cue(cue), ["remove_HI(keep_lyrics=1)"])
    assert line in _visible(out)
    assert "Hallo." in out


@pytest.mark.parametrize("cue", [
    "<i>♪ ~ ♪</i>",
    "<i>- ♪♪</i>",
    "<font color=\"#ffffff\">♪ ♪</font>",
])
def test_keep_lyrics_drops_tagged_decoration(cue, render):
    # Markup carries no lyric content, so tagged decoration goes like untagged decoration.
    out = render(_cue(cue), ["remove_HI(keep_lyrics=1)"])
    assert "♪" not in out
    assert "Hallo." in out


def test_keep_lyrics_keeps_tagged_lyric(render):
    out = render(_cue("<i>♪ la la ♪</i>"), ["remove_HI(keep_lyrics=1)"])
    assert "la la" in out
    assert "Hallo." in out


# --- detect_uppercase -------------------------------------------------------
# detect_uppercase strips HI bracket cues before counting case, so an all-caps
# file with lowercase bracket cues must still read as uppercase.
def _detect_uppercase(srt, tmp_path):
    path = tmp_path / "movie.en.srt"
    path.write_bytes(srt.encode("utf-8"))
    sm = SubtitleModifications()
    assert sm.load(fn=str(path), language=Language("eng"))
    return sm.detect_uppercase()


def test_detect_uppercase_counts_all_caps_file_with_lowercase_bracket_cues(tmp_path):
    entries = "".join(
        "{}\n00:00:{:02d},000 --> 00:00:{:02d},000\nWE ARE GOING NOW THE PLAN IS ON [door creaks]\n\n".format(
            i, i, i)
        for i in range(1, 11)
    )
    assert _detect_uppercase(entries, tmp_path) is True


def test_detect_uppercase_rejects_lowercase_dialogue(tmp_path):
    entries = "".join(
        "{}\n00:00:{:02d},000 --> 00:00:{:02d},000\nwe are going now the plan is on [door creaks]\n\n".format(
            i, i, i)
        for i in range(1, 11)
    )
    assert _detect_uppercase(entries, tmp_path) is False


@pytest.mark.parametrize("cue", [
    "<i><font color=\"#ffff00\">♪ la ♪</font></i>",
    "<b><i>♪ la ♪</i></b>",
    "<i><b>♪♪</b></i>",
    "- <i>♪ la ♪</i>",
    "<I>♪ MTV ♪</I>",
])
def test_tagged_music_cue_nested_or_prefixed_is_removed(cue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert "♪" not in out
    assert "Hallo." in out


def test_tagged_bracket_cue_nested_is_removed(render):
    out = render(_cue("<i><font color=\"#ffff00\">[door creaks]</font></i>"), ["remove_HI"])
    assert "door creaks" not in out
    assert "<i" not in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue, line", [
    ("Hello <i> [sighs]</i> world", "Hello world"),
    ("Hello<i> [sighs]</i> world", "Hello world"),
    ("Hello <i>[sighs]</i> world", "Hello world"),
    ("Hello<i>[sighs]</i> world", "Hello world"),
])
def test_bracket_cue_between_words_keeps_one_space(cue, line, render):
    out = render(_cue(cue), ["remove_HI"])
    assert line in _visible(out)
    assert _balanced(out, "i")


def test_opening_only_ssa_lyric_is_removed(render):
    out = render(_cue("{\\i1}♪ Some lyric{\\i0}"), ["remove_HI"])
    assert "Some lyric" not in out
    assert "Hallo." in out


def test_cross_cue_italic_bracket_keeps_tags_balanced(render):
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n<i>[door creaks]\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nHallo.</i>\n"
    )
    out = render(srt, ["remove_HI"])
    assert "door creaks" not in out
    assert "Hallo." in out
    assert out.count("<i>") == out.count("</i>")


def test_keep_lyrics_keeps_unpaired_tagged_music_line_on_content_route():
    # Content route only: on the file route the engine has already peeled the
    # opening SSA tag, so the line is plain decoration there and is dropped.
    # The guard keeps the HTML line because its closing tag sits on the next line.
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n<i>♪♪\nHello.</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nHallo.\n"
    )
    out = _render_content(srt, ["remove_HI(keep_lyrics=1)"], None)
    assert "<i>♪♪" in out
    assert "Hello.</i>" in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue, dialogue", [
    ("<i>♪ MTV ♪</i>\n<i>Hallo.</i>", "Hallo."),
    ("Hallo.\n<i>♪ MTV ♪</i>", "Hallo."),
    ("<i>♪ yeah</i>\nWhat did you say?", "What did you say?"),
])
def test_tagged_music_line_never_takes_the_dialogue_in_its_cue(cue, dialogue, render):
    # Music and dialogue share one cue: the dialogue line must survive either way round.
    out = render(_cue(cue), ["remove_HI"])
    assert dialogue in out
    assert "Hallo." in out

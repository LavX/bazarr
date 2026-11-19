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


# --- Round 3: dialogue must never be removed with a music cue ------------------
# A tagged music span may only go when everything inside its own pair is music.
@pytest.mark.parametrize("cue, dialogue", [
    ("<i>♪ MTV ♪</i> <i>Hallo.</i>", "Hallo."),
    ("<i>♪♪</i> <i>Hallo.</i>", "Hallo."),
    ("{\\i1}♪ yeah{\\i0} What did you say?", "What did you say?"),
])
def test_same_line_music_pair_never_takes_sibling_dialogue(cue, dialogue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert dialogue in _visible(out)


# A symbol only counts as music when it is in the visible text of a music line.
# A # or * in dialogue or in a tag's attributes never makes a line music.
@pytest.mark.parametrize("cue, dialogue", [
    ("<i>♪ Music ♪\n<font color=\"#ffffff\">Hello there.</font></i>", "Hello there."),
    ("<font color=\"#ffff00\">♪ la la ♪</font>\n<font color=\"#00ffff\">Hello there.</font>", "Hello there."),
    ("<i>♪ Music ♪\nWe're #1!</i>", "We're #1!"),
    ("<i>♪ la la ♪</i>\n<i>What the f**k?</i>", "What the f**k?"),
    ("♪ la la ♪\nHello there.", "Hello there."),
    ("♪ la la ♪\nWe're #1!", "We're #1!"),
])
def test_continuation_line_with_hash_or_star_is_dialogue(cue, dialogue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert dialogue in _visible(out)


# Realistic dialogue with markup and symbols in ordinary speech: none of it may go.
DIALOGUE_LINES = [
    "<i>Where is #7 now?</i>",
    "<font color=\"#ffff00\">Hello there, friend.</font>",
    "<b>That's *my* car!</b>",
    "<i>We're #1 in the league.</i>",
    "<font color=\"#00ffff\">What the f**k is that?</font>",
    "He said <b>#</b> is a hashtag.",
    "<i>Press * to continue.</i>",
    "<font color=\"#ff0000\">Stop!</font> <i>Now!</i>",
    "<b>Room #204, please.</b>",
    "<i>Ten * twenty = 200.</i>",
    "<font color=\"#ffffff\">Yes, #1 is correct.</font>",
    "<i>Get out of my *house*!</i>",
    "<b>Call me on 555 #2.</b>",
    "<i>I'm not * ready yet.</i>",
    "<i>He is <b>very</b> #1 in class.</i>",
    "<font color=\"#00ff00\">It's <i>all</i> # fine.</font>",
    "<i>Hey, *you* there!</i>",
    "<b>Sold #5!</b>",
    "<i>Two * three # four</i>",
    "<i>Hi.</i>\n<font color=\"#ff0000\">#1 fan!</font>",
    "<i>Hello #</i>\n<i>* Hello *</i>",
]


@pytest.mark.parametrize("line", DIALOGUE_LINES)
def test_realistic_tagged_dialogue_survives(line, render):
    out = render(_cue(line), ["remove_HI"])
    for text in _visible(line).split("\n"):
        assert text.strip() in _visible(out)


# --- Round 3: bracket and full-bracket rules ---------------------------------
@pytest.mark.parametrize("cue", [
    "He said [one] and [two] here.",
    "[sighs] <i>I know [laughs]</i>",
])
def test_bracket_cue_never_swallows_words_between_cues(cue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert "and" in _visible(out) or "I know" in _visible(out)
    assert "here." in _visible(out) or "I know" in _visible(out)


def test_hyphenated_bracket_cue_is_removed(render):
    out = render(_cue("[door-creaks] Hello there."), ["remove_HI"])
    assert "door-creaks" not in out
    assert "Hello there." in out


@pytest.mark.parametrize("cue", [
    "<i>[door creaks]\nI know you're in there (knocking)</i>",
    "[door creaks]\nI know you're in there (knocking)",
])
def test_full_bracket_cue_never_takes_dialogue_on_the_next_line(cue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert "I know you're in there" in _visible(out)


def test_lone_ssa_tag_next_to_bracket_is_removed_on_file_route(tmp_path):
    out = _render_file(_cue("Hello <i>[sighs] </i>world"), ["remove_HI"], tmp_path)
    assert "Hello world" in _visible(out)
    assert out.count("<i>") == out.count("</i>")


# --- Round 3: uppercase detection selects rules by name ----------------------
def test_case_detection_names_all_resolve_to_processors():
    from subzero.modification import registry
    from subzero.modification.main import CASE_DETECTION_PROCESSORS

    names = {p.name for p in registry.mods["remove_HI"].processors}
    assert set(CASE_DETECTION_PROCESSORS) <= names


def test_detect_uppercase_ignores_markup_letters(tmp_path):
    # The letters of <font color=...> are markup, not text, so an all-caps file that is
    # wrapped in font tags must still read as uppercase.
    entries = "".join(
        "{}\n00:00:{:02d},000 --> 00:00:{:02d},000\n"
        "<font color=\"#ffff00\">WE ARE GOING NOW THE PLAN IS ON</font> [door creaks]\n\n".format(i, i, i)
        for i in range(1, 11)
    )
    # Content route: the production load keeps the tags, pysubs2 would drop <font>.
    sm = SubtitleModifications()
    assert sm.load(content=entries, language=Language("eng"))
    assert sm.detect_uppercase() is True


@pytest.mark.parametrize("cue, dialogue", [
    ("<i>♪ Music ♪ Hello there</i>", "Hello there"),
    ("♪ Music ♪ Hello there", "Hello there"),
])
def test_text_after_a_mid_cue_music_mark_is_dialogue(cue, dialogue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert dialogue in _visible(out)

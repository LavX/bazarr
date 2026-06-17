# coding=utf-8
# Coverage for the "preserve song lyrics" option of the Remove HI mod.
# See https://github.com/LavX/bazarr/issues/225

from subliminal_patch import Subtitle


SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:02,000\n"
    "[door creaks]\n"
    "\n"
    "2\n"
    "00:00:03,000 --> 00:00:04,000\n"
    "♪ We are the champions my friend ♪\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:06,000\n"
    "JOHN: Hello there\n"
    "\n"
    "4\n"
    "00:00:07,000 --> 00:00:08,000\n"
    "♪♪\n"
)


def _modified(languages, mods, srt=SRT):
    sub = Subtitle(languages["en"], mods=mods, original_format=True)
    sub.content = srt.encode("utf-8")
    assert sub.is_valid()
    content = sub.get_modified_content(format="srt")
    return content.decode("utf-8") if content else ""


def test_remove_hi_strips_song_lyrics_by_default(languages):
    out = _modified(languages, ["remove_HI"])
    assert "champions" not in out      # music-note lyric line removed (legacy behaviour)
    assert "door creaks" not in out    # bracketed cue removed
    assert "JOHN:" not in out          # speaker label removed
    assert "Hello there" in out        # actual dialogue kept


def test_remove_hi_keep_lyrics_preserves_song_lyrics(languages):
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"])
    assert "We are the champions my friend" in out  # lyric line preserved
    assert "door creaks" not in out                 # non-lyric HI cue still removed
    assert "JOHN:" not in out                        # speaker label still removed
    assert "Hello there" in out                      # dialogue still kept


def test_keep_lyrics_still_drops_bare_music_symbol_lines(languages):
    """A line that is only decorative music symbols carries no lyrics, so it is
    removed even when lyrics are preserved."""
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"])
    lines = [line.strip() for line in out.splitlines()]
    assert "♪♪" not in lines


def test_keep_lyrics_drops_decorated_symbol_only_lines(languages):
    """Symbol-only lines decorated with dashes/spaces carry no words, so they are
    still dropped while a real lyric in the same file is kept. Regression for the
    Codex review on https://github.com/LavX/bazarr/pull/229
    """
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n- ♪♪\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n> ♪ ♪\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\n♪ We are the champions ♪\n"
    )
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"], srt=srt)
    lines = [line.strip() for line in out.splitlines()]
    assert "- ♪♪" not in lines              # decorated symbol-only line dropped
    assert "> ♪ ♪" not in lines             # decorated symbol-only line dropped
    assert "We are the champions" in out    # real lyric preserved


def test_keep_lyrics_preserves_all_music_note_text_lines(languages):
    """A music-note line carrying text is preserved when keeping lyrics, even if
    it looks like a description: a sung lyric and a cue cannot be told apart
    reliably, and over-removing was the bug being fixed (the heuristic dropped
    all-caps lyrics like "HAPPY BIRTHDAY" and lyrics containing "music").
    See the Codex review on https://github.com/LavX/bazarr/pull/229
    """
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n♪ HAPPY BIRTHDAY TO YOU ♪\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n♪ I can still hear the music playing ♪\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\n♪ We are the champions my friend ♪\n"
    )
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"], srt=srt)
    assert "HAPPY BIRTHDAY TO YOU" in out               # all-caps lyric kept
    assert "I can still hear the music playing" in out  # lyric mentioning music kept
    assert "We are the champions my friend" in out      # plain lyric kept


def test_keep_lyrics_leaves_non_music_letterless_lines(languages):
    """Lines without a music symbol are outside this processor's scope and must
    survive even when they have no letters (e.g. a year). Regression for the
    Codex review on https://github.com/LavX/bazarr/pull/229
    """
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n1939\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n♪♪\n"
    )
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"], srt=srt)
    assert "1939" in out                    # non-music letterless line kept
    lines = [line.strip() for line in out.splitlines()]
    assert "♪♪" not in lines                # music symbol-only line still dropped


def test_keep_lyrics_drops_symbol_line_within_multiline_event(languages):
    """A symbol-only line sharing a multi-line cue with a lyric is dropped on its
    own; the lyric line in the same cue must survive (not be dragged into the
    entry-wide removal). Regression for the Codex review on
    https://github.com/LavX/bazarr/pull/229
    """
    srt = "1\n00:00:01,000 --> 00:00:02,000\n♪ We are the champions ♪\n♪♪\n"
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"], srt=srt)
    assert "We are the champions" in out                # lyric in the multi-line cue kept
    lines = [line.strip() for line in out.splitlines()]
    assert "♪♪" not in lines                            # symbol-only line dropped

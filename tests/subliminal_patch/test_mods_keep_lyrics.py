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


def test_keep_lyrics_still_drops_music_cue_descriptions(languages):
    """Music *descriptions* (cues) are hearing-impaired content, not lyrics, so
    they are removed even when lyrics are preserved. Regression for the Codex
    review on https://github.com/LavX/bazarr/pull/229
    """
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n♪ MUSIC ♪\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n♪ ominous music ♪\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\n♪ We are the champions my friend ♪\n"
    )
    out = _modified(languages, ["remove_HI(keep_lyrics=1)"], srt=srt)
    assert "MUSIC" not in out                          # all-caps cue removed
    assert "ominous music" not in out                  # lowercase music cue removed
    assert "We are the champions my friend" in out     # genuine lyric preserved

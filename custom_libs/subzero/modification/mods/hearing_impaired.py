# coding=utf-8
from __future__ import absolute_import
from __future__ import unicode_literals
import re

from subzero.language import Language
from subzero.modification.mods import SubtitleTextModification, empty_line_post_processors, TAG
from subzero.modification.exc import EmptyEntryError
from subzero.modification.processors.re_processor import NReProcessor
from subzero.modification import registry


JAPANESE = Language("jpn")


class FullBracketEntryProcessor(NReProcessor):
    def process(self, content, debug=False, **kwargs):
        entry = kwargs.get("entry")
        if entry:
            rep_content = super(FullBracketEntryProcessor, self).process(entry, debug=debug, **kwargs)
            if not rep_content.strip():
                raise EmptyEntryError()
        return content


# Any alphanumeric content (letters incl. non-Latin/CJK, or digits) marks a line
# as real content rather than pure decoration.
_HI_CONTENT_RE = re.compile(r'[^\W_]', re.UNICODE)
_MUSIC_SYMBOL_RE = re.compile(r'[*#¶♫♪]')
# SSA/ASS override tags ({\i1}, {\an8}, {\c&H..&}, ...) carry letters/digits that
# must not be mistaken for lyric content when testing a line.
_OVERRIDE_TAG_RE = re.compile(r'\{[^}]*\}')


class MusicEntryProcessor(NReProcessor):
    """Removes music-note lines (the HI_music processor), unless the mod was asked
    to keep them via the keep_lyrics arg (remove_HI(keep_lyrics=1)).

    Song lyrics are legitimate subtitle content; the default Remove HI behaviour
    strips them along with the music notes. When keep_lyrics is set, this
    processor only touches lines that actually bear a music symbol (its normal
    scope): such a line is preserved if it carries any alphanumeric content
    (letters -- incl. non-Latin such as CJK -- or numbers, e.g. a count-in like
    "1 2 3 4") and dropped only if it is purely symbols/punctuation decoration.
    Lines without a music symbol are returned untouched, so unrelated content
    (numbers, punctuation) is never collateral. A sung lyric and a descriptive
    cue (e.g. "MUSIC", "ominous music") both have words and cannot be told apart
    reliably, so the cue text is kept too: the point of the option is to never
    drop a real lyric. Bracket-wrapped lines (e.g. "[music]") are descriptions by
    subtitle convention and are removed earlier by HI_brackets.
    See https://github.com/LavX/bazarr/issues/225
    """
    def process(self, content, debug=False, keep_lyrics=None, **kwargs):
        if keep_lyrics:
            text = content or ""
            # Only music-note lines are in this processor's scope; leave anything
            # else (it would not match the HI_music regex anyway) untouched.
            if not _MUSIC_SYMBOL_RE.search(text):
                return content
            # Decide per line (not via the entry-wide regex) so a symbol-only
            # line in a multi-line cue does not drag the lyric line with it:
            # keep music-note lines with alphanumeric content, drop pure
            # symbol/punctuation decoration. Strip override tags first so their
            # letters/digits (e.g. {\i1}) are not counted as content.
            stripped = _OVERRIDE_TAG_RE.sub('', text)
            return content if _HI_CONTENT_RE.search(stripped) else ""
        return super(MusicEntryProcessor, self).process(content, debug=debug, **kwargs)


class HearingImpaired(SubtitleTextModification):
    identifier = "remove_HI"
    description = "Remove Hearing Impaired tags"
    exclusive = True
    order = 20

    long_description = "Removes tags, text and characters from subtitles that are meant for hearing impaired people"

    processors = [
        # full bracket entry, single or multiline; starting with brackets and ending with brackets
        FullBracketEntryProcessor(re.compile(r'(?sux)^-?%(t)s[([].+(?=[^)\]]{3,}).+[)\]]%(t)s$' % {"t": TAG}),
                                  "", name="HI_brackets_full"),

        # uppercase text before colon (at least 3 uppercase chars); at start or after a sentence,
        # possibly with a dash in front; ignore anything ending with a quote
        NReProcessor(re.compile(r'(?u)(?:(?<=^)|(?<=[.\-!?\"\'])\s)([\s\->~]*(?=[A-ZÀ-Ž&+]\s*[A-ZÀ-Ž&+]\s*[A-ZÀ-Ž&+])'
                                r'[A-zÀ-ž-_0-9\s\"\'&+()\[\],:]+:(?![\"\'’ʼ❜‘‛”“‟„])(?:\s+|$))(?![0-9])'), "",
                     name="HI_before_colon_caps"),

        # any text before colon (at least 3 chars); at start or after a sentence,
        # possibly with a dash in front; try not breaking actual sentences with a colon at the end by not matching if
        # a space is inside the text; ignore anything ending with a quote
        NReProcessor(re.compile(r'(?u)(?:(?<=^)|(?<=[.\-!?\"]))([\s\->~]*((?=[A-zÀ-ž&+]\s*[A-zÀ-ž&+]\s*[A-zÀ-ž&+])'
                                r'[A-zÀ-ž-_0-9\s\"\'&+()\[\]]+:)(?![\"’ʼ❜‘‛”“‟„])\s*)(?![0-9]|//)'),
                     lambda match:
                     match.group(1) if (match.group(2).count(" ") > 0 or match.group(1).count("-") > 0)
                     else "" if not match.group(1).startswith(" ") else " ",
                     name="HI_before_colon_noncaps"),

        # brackets (only remove if at least 3 chars in brackets, allow numbers and spaces inside brackets)
        NReProcessor(re.compile(r'(?sux)-?%(t)s["\']*\[(?=[^\[\]]{3,})[A-Za-zÀ-ž0-9\s\'".:-_&+]+[)\]]["\']*[\s:]*%(t)s' %
                                {"t": TAG}), "", name="HI_brackets"),

        #NReProcessor(re.compile(r'(?sux)-?%(t)s[([]%(t)s(?=[A-zÀ-ž"\'.]{3,})[^([)\]]+%(t)s$' % {"t": TAG}),
        #             "", name="HI_bracket_open_start"),

        #NReProcessor(re.compile(r'(?sux)-?%(t)s(?=[A-zÀ-ž"\'.]{3,})[^([)\]]+[)\]][\s:]*%(t)s' % {"t": TAG}), "",
        #             name="HI_bracket_open_end"),

        # text before colon (and possible dash in front), max 11 chars after the first whitespace (if any)
        # NReProcessor(re.compile(r'(?u)(^[A-z\-\'"_]+[\w\s]{0,11}:[^0-9{2}][\s]*)'), "", name="HI_before_colon"),

        # starting text before colon (at least 3 chars)
        #NReProcessor(re.compile(r'(?u)(\b|^)([\s-]*(?=[A-zÀ-ž-_0-9"\']{3,})[A-zÀ-ž-_0-9"\']+:\s*)'), "",
        #             name="HI_before_colon"),


        # text in brackets at start, after optional dash, before colon or at end of line
        # fixme: may be too aggressive
        #NReProcessor(re.compile(r'(?um)(^-?\s?[([][A-zÀ-ž-_\s]{3,}[)\]](?:(?=$)|:\s*))'), "",
        #             name="HI_brackets_special"),

        # all caps line (at least 4 consecutive uppercase chars,only remove if line matches common HI cues, otherwise keep)
        NReProcessor(
            re.compile(r'(?u)(^(?=.*[A-ZÀ-Ž&+]{4,})[A-ZÀ-Ž-_\s&+]+$)'),
            lambda m: "" if any(
                cue in m.group(1)
                for cue in [
                    "LAUGH", "APPLAU", "CHEER", "MUSIC", "GASP", "SIGHS", "GROAN", "COUGH", "SCREAM", "SHOUT", "WHISPER",
                    "PHONE", "DOOR", "KNOCK", "FOOTSTEP", "THUNDER", "EXPLOSION", "GUNSHOT", "SIREN"
                ]
            ) else m.group(1),
            name="HI_all_caps",
            supported=lambda p: not p.mostly_uppercase
        ),

        # remove MAN:
        NReProcessor(re.compile(r'(?suxi)(\b(?:WO)MAN:\s*)'), "", name="HI_remove_man"),

        # dash in front
        # NReProcessor(re.compile(r'(?u)^\s*-\s*'), "", name="HI_starting_dash"),

        # all caps at start before new sentence
        NReProcessor(re.compile(r'(?u)^(?=[A-ZÀ-Ž]{4,})[A-ZÀ-Ž-_\s]+\s([A-ZÀ-Ž][a-zà-ž].+)'), r"\1",
                     name="HI_starting_upper_then_sentence", supported=lambda p: not p.mostly_uppercase),

        # remove normal and Japanese parentheses only for Japanese subtitles
        NReProcessor(re.compile(r'(?u)（.+）|\(.+\)'), "",
                     name="JP_parentheses",
                     # https://en.wikipedia.org/wiki/Japanese_punctuation#Parentheses
                     supported=lambda p: p.language == JAPANESE),
    ]

    post_processors = empty_line_post_processors
    last_processors = [
        # remove music symbols
        NReProcessor(re.compile(r'(?u)(^%(t)s[*#¶♫♪\s]*%(t)s[*#¶♫♪\s]+%(t)s[*#¶♫♪\s]*%(t)s$)' % {"t": TAG}),
                     "", name="HI_music_symbols_only"),

        # remove music entries (song lyrics); skipped when keep_lyrics is set
        MusicEntryProcessor(re.compile(r'(?ums)(^[-\s>~]*[*#¶♫♪]+\s*.+|.+\s*[*#¶♫♪]+\s*$|.+\s*[*#¶♫♪]+[)\]])'),
                            "", name="HI_music", entry=True),
    ]


registry.register(HearingImpaired)

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

# Simple HTML tags (<i>, <b>, <u>, <font ...> and their closing forms). The subtitle
# path that applies mods keeps them as literal text (Subtitle.get_modified_content
# loads with keep_html_tags=True), so the rules have to see them.
_SIMPLE_HTML_RE = re.compile(r'(?i)</?(?:[ibu]|font)(?:\s[^>]*)?>')
_HTML_NAMES = ("i", "b", "u", "font")

# A bracket cue body: "[...]" or with quotes, at least 3 characters inside.
HI_BRACKET = r"""["']*\[(?=[^\[\]]{3,})[A-Za-zÀ-ž0-9\s'".:_&+\-?!]+[)\]]["']*"""

# The body of a bracket cue that spans a whole entry (HI_brackets_full).
FULL_BRACKET_CORE = r"[([](?=[^)\]]{3,})[^()\[\]<>\\]*[)\]]"


# Music marks. Only these make a line music: # and * are ordinary dialogue characters.
_MUSIC_MARK = "♪♫¶"
# One character of a cue's text: not a line break (the two characters \N), not an SSA
# override tag, not the start of a simple HTML tag, and not a music mark.
_PLAIN_CHAR = r"(?:(?!\\N)(?!\{\\)(?!</?(?:[ibu]|font)[\s>])(?![♪♫¶]).)"
# The body of a music line: a music mark opens it and/or closes it, with no mark inside.
# Without a mark it is dialogue, and a body with a mark in the middle may hold dialogue
# after the closing mark, so neither is music.
_MUSIC_BODY = (r"(?:[-\s>~]*[♪♫¶]" + _PLAIN_CHAR + r"*[♪♫¶]?\s*"
               r"|" + _PLAIN_CHAR + r"*[♪♫¶]\s*)")


def _tag_pair(body, group):
    """Regex for body wrapped in one simple tag pair, or in two nested pairs.

    Each opening tag is matched with its own closing tag inside the same match, so a
    rule built on it can never remove half a pair. Group names are unique per call.
    """
    # Python rejects a group name used twice, even in different alternatives.
    a, c, d = ("t%d%s" % (group, s) for s in "acd")
    one = r"<(?P<%s>font|[ibu])(?:\s[^>]*)?>%s</(?P=%s)>" % (a, body, a)
    two = (r"<(?P<%s>font|[ibu])(?:\s[^>]*)?><(?P<%s>font|[ibu])(?:\s[^>]*)?>%s</(?P=%s)></(?P=%s)>"
           % (c, d, body, d, c))
    return r"(?i:%s|%s)" % (one, two)


def _ssa_pair(body, group):
    """Regex for body wrapped in one SSA override pair, or in two nested pairs."""
    a, c, d = "s%d" % group, "s%dc" % group, "s%dd" % group
    one = r"\{\\(?P<%s>[iusb])1\}%s\{\\(?P=%s)0\}" % (a, body, a)
    two = (r"\{\\(?P<%s>[iusb])1\}\{\\(?P<%s>[iusb])1\}%s\{\\(?P=%s)0\}\{\\(?P=%s)0\}"
           % (c, d, body, d, c))
    return r"(?:%s|%s)" % (one, two)


def _tagged_bracket_repl(match):
    """Drop a removed bracket cue together with the space after it, unless a space
    already separates it from the text before it."""
    before = match.string[match.start() - 1] if match.start() else " "
    return "" if before.isspace() else match.group("trail")


# One music line of a cue: plain, or wrapped in one simple HTML pair or one SSA pair.
def _music_line(group):
    """One music line. Each copy in a pattern needs its own group numbers."""
    return (r"[-\s>~]*(?:" + _tag_pair(_MUSIC_BODY, group) + r"|" + _ssa_pair(_MUSIC_BODY, group + 1)
            + r"|" + _MUSIC_BODY + r")")


def _has_unpaired_html(text):
    """True when a simple tag in the text has no partner in the same text."""
    lowered = text.lower()
    for name in _HTML_NAMES:
        opened = len(re.findall(r'<%s(?:\s[^>]*)?>' % name, lowered))
        if opened != lowered.count('</%s>' % name):
            return True
    return False


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
            # A line whose simple tags do not pair up within the line is left alone:
            # dropping it would take the other half of a pair that sits on a kept line.
            if _has_unpaired_html(text):
                return content
            # Strip markup first, so that the # in <font color="#fff"> is not a music
            # symbol and the letters of <i> are not counted as lyric content.
            stripped = _SIMPLE_HTML_RE.sub('', _OVERRIDE_TAG_RE.sub('', text))
            # Only music-note lines are in this processor's scope; leave anything
            # else (it would not match the HI_music regex anyway) untouched.
            if not _MUSIC_SYMBOL_RE.search(stripped):
                return content
            # Decide per line (not via the entry-wide regex) so a symbol-only
            # line in a multi-line cue does not drag the lyric line with it:
            # keep music-note lines with alphanumeric content, drop pure
            # symbol/punctuation decoration. Override tags ({\i1}) are stripped
            # above so their letters/digits are not counted as content.
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
        FullBracketEntryProcessor(re.compile((r'(?sux)^-?%(t)s(?:' + _tag_pair(FULL_BRACKET_CORE, 5)
                                              + r'|' + FULL_BRACKET_CORE + r')%(t)s$') % {"t": TAG}),
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
        # a bracket cue wrapped in one simple tag pair: the pair goes with its text
        NReProcessor(re.compile(_tag_pair(r"\s*" + HI_BRACKET + r"\s*", 6) + r"(?P<trail>[\s:]*)"),
                     _tagged_bracket_repl, name="HI_brackets_tagged"),

        # brackets (only remove if at least 3 chars in brackets, allow numbers and spaces inside brackets).
        # The SSA tags are taken without their whitespace: TAG's \s* would take the space
        # that separates the words around the tag.
        NReProcessor(re.compile(r'(?sux)-?(?:{\\[iusb][0-1]}\s*)*%(b)s(?:[\s:]*{\\[iusb][0-1]})*(?P<trail>[\s:]*)'
                                % {"b": HI_BRACKET}),
                     _tagged_bracket_repl, name="HI_brackets"),

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
        NReProcessor(re.compile((r'(?u)(^%(t)s[*#¶♫♪\s]*%(t)s[*#¶♫♪\s]+%(t)s[*#¶♫♪\s]*%(t)s$'
                                 r'|^' + _tag_pair(r"[*#¶♫♪\s]+", 1) + r'$)') % {"t": TAG}),
                     "", name="HI_music_symbols_only"),

        # remove music entries (song lyrics); skipped when keep_lyrics is set
        # Remove music lines (song lyrics). Every line of the cue must be a music line: a
        # music mark opens or closes it, and its text holds no tags, no line break and no
        # other mark (see _MUSIC_BODY). A line of dialogue in the cue keeps the whole cue,
        # so dialogue is never removed with a music line. The rule runs on the whole entry.
        MusicEntryProcessor(re.compile(r'(?us)^(?:' + _music_line(7) + r')(?:\\N(?:' + _music_line(9) + r'))*\s*$'),
                            "", name="HI_music", entry=True),
    ]


registry.register(HearingImpaired)

# coding=utf-8
"""Markup layer for the subtitle modifications.

The modification rules work on plain text. This layer sets aside known HTML or
SSA style pairs that wrap a line or its leading speaker label. It restores the
styles on surviving text and keeps leading SSA alignment on the surviving cue.
"""
import re

# Allow angle brackets inside quoted values and require complete attribute
# assignments so an apparent opener cannot consume malformed attributes.
_STYLE_OPEN = re.compile(
    r"<[ \t]*(?i:(i|b|u|s|font))"
    r"""(?:[ \t]+[a-zA-Z_:][a-zA-Z0-9_.:-]*[ \t]*=[ \t]*(?:"[^"]*"|'[^']*'|[^ \t<>"'=`]+))*"""
    r"[ \t]*>|\{\\([iusb])1\}"
)
_STYLE_CLOSE = re.compile(r"<[ \t]*/[ \t]*(?i:(i|b|u|s|font))[ \t]*>|\{\\([iusb])0\}")
_STYLE_CLOSE_AT_END = re.compile(r"(?:%s)$" % _STYLE_CLOSE.pattern)
_SSA_ALIGNMENT = re.compile(r"\{\\an[1-9]\}")
_SPEAKER_PREFIX = re.compile(r"[ \t]*(?:-[ \t]*)?")
_SPEAKER_COLON = re.compile(r"[ \t]*:")
# Any tag or override that the layer does not take apart.
_ANY_MARKUP = re.compile(r"<|\{\\")

LINE_BREAK = r"\N"


class EntryMarkup(object):
    """The text being processed and the markup set aside for each line and cue."""

    def __init__(self, plain, opens, closes, labels, alignment, entry_for_rules=None):
        self.plain = plain
        self.opens = opens
        self.closes = closes
        self.labels = labels
        self.alignment = alignment
        self.entry_for_rules = entry_for_rules

    def rewrap(self, lines, origins):
        """Wrap each output line in the tags of the input line it came from."""
        if len(lines) != len(origins):
            raise ValueError("%d lines but %d origins" % (len(lines), len(origins)))
        wrapped = []
        for line, origin in zip(lines, origins):
            label = self.labels[origin]
            if label and line.startswith(label[0]):
                plain, styled = label
                line = styled + line[len(plain):]
            wrapped.append(self.opens[origin] + line + self.closes[origin])
        if wrapped:
            wrapped[0] = self.alignment + wrapped[0]
        return wrapped


def _opening_tag(text, offset=0):
    match = _STYLE_OPEN.match(text, offset)
    if match and not match.group()[:-1].rstrip().endswith("/"):
        return match
    return None


def _style_name(match):
    html, ssa = match.groups()
    return (html.lower(), None) if html else (None, ssa)


def visible_style_text(line):
    """Text for inspection when all style tags on a line form known pairs."""
    styles, parts = [], []
    offset = 0
    while marker := _ANY_MARKUP.search(line, offset):
        start = marker.start()
        parts.append(line[offset:start])
        if closing := _STYLE_CLOSE.match(line, start):
            if not styles or styles.pop() != _style_name(closing):
                return None
            offset = closing.end()
        elif opening := _opening_tag(line, start):
            styles.append(_style_name(opening))
            offset = opening.end()
        else:
            return None
    if styles:
        return None
    return "".join(parts) + line[offset:]


def _unwrap_leading_label(text):
    """Recognize complete style pairs around a potential speaker label."""
    styles = []
    offset = _SPEAKER_PREFIX.match(text).end()
    prefix = text[:offset]
    while opening := _opening_tag(text, offset):
        styles.append(_style_name(opening))
        offset = opening.end()
    close_start = _ANY_MARKUP.search(text, offset)
    if not styles or close_start is None:
        return None
    label = text[offset:close_start.start()]
    offset = close_start.start()
    for style in reversed(styles):
        closing = _STYLE_CLOSE.match(text, offset)
        if not closing or _style_name(closing) != style:
            return None
        offset = closing.end()
    if not label.endswith(":"):
        colon = _SPEAKER_COLON.match(text, offset)
        if colon is None:
            return None
        label += colon.group()
        offset = colon.end()
    suffix = text[offset:]
    if _ANY_MARKUP.search(suffix):
        return None
    plain = prefix + label
    return plain + suffix, (plain.lstrip(), text[:offset])


def _unwrap_line(line):
    """The text, whole-line styles and potential speaker label styles for one line."""
    if "<" not in line and "{\\" not in line:
        return line, "", "", None
    stripped = line.strip()
    lead = line[:len(line) - len(line.lstrip())]
    trail = line[len(line.rstrip()):]
    core = stripped
    opens, closes = [], []
    while opening := _opening_tag(core):
        closing = _STYLE_CLOSE_AT_END.search(core, opening.end())
        if not closing or _style_name(opening) != _style_name(closing):
            break
        opens.append(opening.group())
        closes.append(closing.group())
        core = core[opening.end():closing.start()]
    label = None
    if _ANY_MARKUP.search(core):
        labeled = _unwrap_leading_label(core)
        if labeled is None:
            return None
        core, label = labeled
    # An empty or blank pair is not a wrapped line. Development keeps such cues verbatim.
    if opens and not core.strip():
        return None
    return lead + core + trail, "".join(opens), "".join(reversed(closes)), label


def unwrap_entry(text):
    """Set aside leading cue alignment and any fully understood set of style pairs."""
    if "<" not in text and "{\\" not in text:
        return None
    position = _SSA_ALIGNMENT.match(text)
    alignment = position.group() if position else ""
    body = text[len(alignment):]
    lines = body.split(LINE_BREAK)
    plain_lines, opens, closes, labels = [], [], [], []
    for line in lines:
        unwrapped = _unwrap_line(line)
        if unwrapped is None:
            if alignment:
                # Expose line text without changing what the entry-wide rules match.
                return EntryMarkup(body, [""] * len(lines), [""] * len(lines),
                                   [None] * len(lines), alignment, entry_for_rules=text)
            return None
        plain, open_tags, close_tags, label = unwrapped
        plain_lines.append(plain)
        opens.append(open_tags)
        closes.append(close_tags)
        labels.append(label)
    if not (alignment or any(opens) or any(labels)):
        return None
    return EntryMarkup(LINE_BREAK.join(plain_lines), opens, closes, labels, alignment)

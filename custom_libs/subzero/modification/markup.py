# coding=utf-8
"""Markup layer for the subtitle modifications.

The modification rules work on plain text. A cue whose markup is simple, meaning
complete pairs of simple HTML tags (<i>, <b>, <u>, <font ...>) or SSA override tags
({\\i1}...{\\i0}) that wrap whole lines, is handed to the rules with that markup
removed, and each surviving line is wrapped in its original tags again. Any other
markup is not understood here, and the cue reaches the rules exactly as it always did.
"""
import re

_HTML_NAMES = ("i", "b", "u", "font")
_HTML_OPEN = re.compile(r"<(i|b|u|font)(?:\s[^>]*)?>", re.IGNORECASE)
_SSA_OPEN = re.compile(r"\{\\([iusb])1\}")
# Any tag or override that the layer does not take apart.
_ANY_MARKUP = re.compile(r"<|\{\\")
# The closing tag that matches each opening tag name, compiled once.
_HTML_CLOSE = {name: re.compile(r"</%s>$" % name, re.IGNORECASE) for name in _HTML_NAMES}
_SSA_CLOSE = {name: re.compile(r"\{\\%s0\}$" % name) for name in "iusb"}

LINE_BREAK = r"\N"


class EntryMarkup(object):
    """The plain text of an entry, and the tags that wrapped each of its lines."""

    def __init__(self, plain, opens, closes):
        self.plain = plain
        self.opens = opens
        self.closes = closes

    def rewrap(self, lines, origins):
        """Wrap each output line in the tags of the input line it came from."""
        if len(lines) != len(origins):
            raise ValueError("%d lines but %d origins" % (len(lines), len(origins)))
        return [self.opens[origin] + line + self.closes[origin] for line, origin in zip(lines, origins)]


def _unwrap_line(line):
    """(plain, opening tags, closing tags) for one line, or None when its markup is not
    simple pairs around the whole line."""
    if "<" not in line and "{\\" not in line:
        return line, "", ""
    if not line.strip():
        return line, "", ""
    stripped = line.strip()
    lead = line[:len(line) - len(line.lstrip())]
    trail = line[len(line.rstrip()):]
    core = stripped
    opens, closes = [], []
    while True:
        html = _HTML_OPEN.match(core)
        ssa = None if html else _SSA_OPEN.match(core)
        if not html and not ssa:
            break
        open_text = (html or ssa).group(0)
        close_re = _HTML_CLOSE[html.group(1).lower()] if html else _SSA_CLOSE[ssa.group(1)]
        close_match = close_re.search(core)
        if not close_match or close_match.start() < len(open_text):
            return None
        opens.append(open_text)
        closes.append(close_match.group(0))
        core = core[len(open_text):close_match.start()]
    if _ANY_MARKUP.search(core):
        return None
    # An empty or blank pair is not a wrapped line. Development keeps such cues verbatim.
    if opens and not core.strip():
        return None
    return lead + core + trail, "".join(opens), "".join(reversed(closes))


def unwrap_entry(text):
    """EntryMarkup for an entry whose every line is plain or wrapped in simple tag pairs,
    with at least one wrapped line. None for plain entries and for any other markup."""
    plain_lines, opens, closes = [], [], []
    for line in text.split(LINE_BREAK):
        unwrapped = _unwrap_line(line)
        if unwrapped is None:
            return None
        plain, open_tags, close_tags = unwrapped
        plain_lines.append(plain)
        opens.append(open_tags)
        closes.append(close_tags)
    if not any(opens):
        return None
    return EntryMarkup(LINE_BREAK.join(plain_lines), opens, closes)

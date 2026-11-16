# coding=utf-8
"""Checks on what an engine returns, and the right-to-left marks Bazarr+ adds.

An engine answers with one output line for each input line. Before anything is
written next to a user's video, Bazarr+ checks that answer the same way for every
engine: the line count, a bounded length, only the markup subtitles carry, and no
control characters. ``add_rtl_marks`` then wraps right-to-left lines, again the same
for every engine.

Pure functions. Nothing here imports a translation service.
"""
from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass, field
from typing import Sequence

RLE = "\u202b"  # right-to-left embedding
PDF = "\u202c"  # pop directional formatting

# The tags any subtitle line may carry, with no attributes. Beyond these, an output line may carry only
# the tags its own source line carried (a "<font color=...>" that an engine keeps), never a new one.
ALLOWED_TAGS = frozenset({"i", "b", "u", "s"})
# An output line may be this many times as long as its input line, with room for short lines.
MAX_LENGTH_FACTOR = 4
MIN_LENGTH_LIMIT = 200
# No line is longer than this, whatever its input.
ABSOLUTE_LENGTH_LIMIT = 5000

# Something a renderer could take for markup: "<", an optional "/", then a letter, "!" or "?". A bare "<"
# in "a < b" or "<3" is text.
_TAG = re.compile(r"</?[A-Za-z!?][^<>]*>")
_ALLOWED_TAG = re.compile(r"</?[ibus]>", re.IGNORECASE)
# Directional overrides, embeddings and isolates, and the line and paragraph separators. A line break
# inside a cue is "\n"; these are not. Bazarr+ adds the direction marks itself, after the check.
# A line break inside a cue is "\n". "\r" (an engine that answers with CRLF) and a tab are ordinary
# whitespace in a subtitle, so they are not control characters here.
_ALLOWED_WHITESPACE = frozenset("\n\r\t")
_FORBIDDEN_FORMAT = frozenset("\u2028\u2029\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    index: int | None = None


@dataclass(frozen=True)
class CheckResult:
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> set[str]:
        return {violation.code for violation in self.violations}


def length_limit(source_line: str) -> int:
    return min(ABSOLUTE_LENGTH_LIMIT, max(MIN_LENGTH_LIMIT, MAX_LENGTH_FACTOR * len(source_line)))


def _control_characters(text: str) -> list[str]:
    found = []
    for char in text:
        if char in _ALLOWED_WHITESPACE:
            continue
        if unicodedata.category(char) == "Cc" or char in _FORBIDDEN_FORMAT:
            found.append(char)
    return found


def _tag_key(tag: str) -> str:
    """A tag compared without case or extra spaces: "<FONT  color=red>" and "<font color=red>" are one tag."""
    return " ".join(tag.lower().split())


def _tag_name(tag: str) -> str:
    return re.match(r"</?\s*([^\s/>]*)", tag).group(1).lower()


def _disallowed_tags(source: str, text: str) -> list[str]:
    """The tags in ``text`` that are neither a plain subtitle tag nor carried over from ``source``."""
    source_tags = [match.group(0) for match in _TAG.finditer(source)]
    source_keys = {_tag_key(tag) for tag in source_tags}
    source_names = {_tag_name(tag) for tag in source_tags}
    found = []
    for match in _TAG.finditer(text):
        tag = match.group(0)
        if _ALLOWED_TAG.fullmatch(tag) or _tag_key(tag) in source_keys:
            continue
        # A closing tag has no attributes to compare, so the source only has to use that tag name.
        if tag.startswith("</") and _tag_name(tag) in source_names:
            continue
        found.append(tag)
    return found


def check_line(source_line: str, output_line: str, index: int | None = None) -> list[Violation]:
    """The violations of one output line against its input line."""
    problems = []
    if not isinstance(output_line, str):
        return [Violation("not_text", "an output line is not text", index)]
    if source_line.strip() and not output_line.strip():
        problems.append(Violation("empty", "a line with text came back empty", index))
    if len(output_line) > length_limit(source_line):
        problems.append(Violation(
            "too_long", f"a line is {len(output_line)} characters, over its limit of {length_limit(source_line)}", index))
    if _control_characters(output_line):
        problems.append(Violation("control_character", "a line holds a control character", index))
    disallowed = _disallowed_tags(source_line, output_line)
    if disallowed:
        problems.append(Violation("disallowed_tag", f"a line holds a tag that is not allowed: {disallowed[0]}", index))
    return problems


def check_output(source_lines: Sequence[str], output_lines: Sequence[str]) -> CheckResult:
    """Check an engine's whole answer against the lines it was sent."""
    if len(source_lines) != len(output_lines):
        return CheckResult((Violation(
            "line_count", f"{len(output_lines)} lines came back for {len(source_lines)} sent"),))
    violations: list[Violation] = []
    for index, (source_line, output_line) in enumerate(zip(source_lines, output_lines)):
        violations.extend(check_line(source_line, output_line, index))
    return CheckResult(tuple(violations))


def add_rtl_marks(lines: Sequence[str], rtl: bool) -> list[str]:
    """Wrap each non-empty line of a right-to-left language in an embedding, once.

    A cue can hold several lines separated by "\\n"; each is wrapped on its own so a
    player that restarts direction at a line break still draws it right to left. A
    line that is already wrapped is left as it is, so running this twice changes nothing.
    A left-to-right language comes back unchanged.
    """
    if not rtl:
        return list(lines)
    return ["\n".join(_wrap(part) for part in line.split("\n")) for line in lines]


def _wrap(part: str) -> str:
    if not part.strip():
        return part
    start = part.startswith(RLE)
    end = part.endswith(PDF)
    if start and end:
        return part
    return (part if start else RLE + part) + ("" if end else PDF)

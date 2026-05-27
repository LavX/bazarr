# coding=utf-8

import os
import re
from dataclasses import dataclass

from subtitles.indexer.utils import get_external_subtitles_path

_COMBINED_PATTERN = re.compile(
    r"^(?P<base>.+)\.(?P<primary>[a-z]{2})\.combined-"
    r"(?P<sec>[a-z]{2})(?:-(?P<ter>[a-z]{2}))?"
    r"\.(?P<fmt>srt|ass)$"
)


@dataclass(frozen=True)
class CombinedFileInfo:
    primary: str
    secondaries: list
    format: str


def parse_combined_filename(path):
    """Return CombinedFileInfo if the filename matches the combined-subtitle
    convention, else None. Two-letter codes only, lowercase."""
    if not path:
        return None
    filename = os.path.basename(path)
    match = _COMBINED_PATTERN.match(filename)
    if not match:
        return None
    secondaries = [match.group("sec")]
    if match.group("ter"):
        secondaries.append(match.group("ter"))
    return CombinedFileInfo(
        primary=match.group("primary"),
        secondaries=secondaries,
        format=match.group("fmt"),
    )


def compose_combined_filename(video_path, primary, secondaries, format):
    """Build the absolute path to write the combined subtitle to. Uses
    get_external_subtitles_path so user settings about subtitle location win."""
    if not secondaries or len(secondaries) > 2:
        raise ValueError("secondaries must have 1 or 2 entries")
    if format not in ("srt", "ass"):
        raise ValueError(f"invalid format: {format!r}")
    base = os.path.splitext(os.path.basename(video_path))[0]
    suffix = "-".join(secondaries)
    subtitle_name = f"{base}.{primary}.combined-{suffix}.{format}"
    return get_external_subtitles_path(file=video_path, subtitle=subtitle_name)

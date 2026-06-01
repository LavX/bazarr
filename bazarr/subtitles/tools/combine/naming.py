# coding=utf-8

import os
import re
from dataclasses import dataclass

_COMBINED_PATTERN = re.compile(
    r"^(?P<base>.+)\.(?P<primary>[a-z]{2})\.combined-"
    r"(?P<sec>[a-z]{2})(?:-(?P<ter>[a-z]{2}))?"
    r"\.(?P<fmt>srt|ass)$"
)


def external_subtitles_dir(video_path):
    """Return the directory where Bazarr writes external subtitles for this
    video, honoring the `subfolder` setting.

    get_external_subtitles_path() is a lookup helper that only returns a path
    for files that already exist, so it cannot be used to build a first-time
    output path in absolute/relative subfolder modes. This computes the write
    target directly instead."""
    video_dir = os.path.dirname(video_path)
    try:
        from app.config import settings
        mode = settings.general.subfolder
        custom = settings.general.subfolder_custom
    except Exception:
        return video_dir
    if mode == "absolute" and custom:
        return custom
    if mode == "relative" and custom:
        return os.path.join(video_dir, custom)
    return video_dir


def external_subtitles_search_dirs(video_path):
    """Return the directories that may hold this video's external subtitles.
    Includes the video directory and the configured subfolder, so source
    resolution finds subtitles wherever the user stores them."""
    video_dir = os.path.dirname(video_path)
    dirs = [video_dir]
    target = external_subtitles_dir(video_path)
    if target and target != video_dir:
        dirs.append(target)
    return dirs


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
    """Build the absolute path to write the combined subtitle to, honoring the
    user's subfolder setting. Unlike get_external_subtitles_path this also works
    for first-time outputs in absolute/relative subfolder modes."""
    if not secondaries or len(secondaries) > 2:
        raise ValueError("secondaries must have 1 or 2 entries")
    if format not in ("srt", "ass"):
        raise ValueError(f"invalid format: {format!r}")
    base = os.path.splitext(os.path.basename(video_path))[0]
    suffix = "-".join(secondaries)
    subtitle_name = f"{base}.{primary}.combined-{suffix}.{format}"
    return os.path.join(external_subtitles_dir(video_path), subtitle_name)

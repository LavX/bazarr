# coding=utf-8

import glob
import logging
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePaths:
    primary: str
    secondaries: list


# Priority order when multiple variants of the same language are on disk.
# Lower number wins. Plain SRT is preferred over HI, HI over forced.
_PLAIN = 0
_HI = 1
_FORCED = 2

_SYNC_ENGINES = ("ffsubsync", "autosubsync", "alass")


def resolve_source_paths(video_path, languages):
    """Find single-language SRT files on disk matching the requested codes.

    Accepts plain, HI, and forced variants. When multiple variants of the same
    language are present, prefers plain > hi > forced. Sync-engine outputs
    (.ffsubsync, .autosubsync, .alass) and combined outputs are never picked.

    Returns SourcePaths if every requested language has a matching file,
    None if any is missing.

    languages: ordered list of code2 strings. First is primary.
    """
    if not languages or len(languages) < 2:
        return None

    video_dir = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    if not video_dir or not base:
        return None

    candidates = glob.glob(
        os.path.join(glob.escape(video_dir), f"{glob.escape(base)}.*.srt")
    )
    # Map of code -> (priority, path). Lower priority wins.
    by_code = {}
    for path in candidates:
        match = _extract_code_and_priority(base, path)
        if match is None:
            continue
        code, priority = match
        existing = by_code.get(code)
        if existing is None or priority < existing[0]:
            by_code[code] = (priority, path)

    paths = []
    for code in languages:
        if code not in by_code:
            logging.debug(
                "BAZARR combine: missing source %s for %s", code, video_path
            )
            return None
        paths.append(by_code[code][1])

    return SourcePaths(primary=paths[0], secondaries=paths[1:])


def _extract_code_and_priority(base, path):
    """Return (code, priority) for a usable single-language SRT.

    Recognizes <base>.<code>.srt (priority _PLAIN),
    <base>.<code>.hi.srt and <base>.<code>.sdh.srt and <base>.<code>.cc.srt
    (priority _HI), and <base>.<code>.forced.srt (priority _FORCED).

    Returns None for combined outputs, sync-engine outputs, and anything that
    does not match a recognized single-language pattern.
    """
    filename = os.path.basename(path)
    if not filename.startswith(base + "."):
        return None
    rest = filename[len(base) + 1:]
    parts = rest.rsplit(".", 1)
    if len(parts) != 2 or parts[1] != "srt":
        return None
    middle = parts[0]
    if "-" in middle:
        # combined-X[-Y] markers always contain a hyphen.
        return None

    segments = middle.split(".")
    if not segments:
        return None
    code = segments[0]
    if len(code) != 2 or not code.isalpha() or not code.islower():
        return None

    if len(segments) == 1:
        return code, _PLAIN

    if len(segments) == 2:
        modifier = segments[1].lower()
        if modifier in ("hi", "sdh", "cc"):
            return code, _HI
        if modifier == "forced":
            return code, _FORCED
        if modifier in _SYNC_ENGINES:
            # PR 158 sync engine output, never a combine source.
            return None
        return None

    # More than two segments: e.g. en.hi.ffsubsync.srt. Reject.
    return None

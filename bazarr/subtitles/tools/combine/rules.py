# coding=utf-8

import glob
import logging
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePaths:
    primary: str
    secondaries: list


def resolve_source_paths(video_path, languages):
    """Find single-language SRT files on disk matching the requested codes.

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

    candidates = glob.glob(os.path.join(glob.escape(video_dir), f"{glob.escape(base)}.*.srt"))
    by_code = {}
    for path in candidates:
        code = _extract_simple_code(base, path)
        if code is not None:
            by_code.setdefault(code, path)

    paths = []
    for code in languages:
        if code not in by_code:
            logging.debug(
                "BAZARR combine: missing source %s for %s", code, video_path
            )
            return None
        paths.append(by_code[code])

    return SourcePaths(primary=paths[0], secondaries=paths[1:])


def _extract_simple_code(base, path):
    """Return the language code if path is a plain single-language SRT
    (<base>.<code>.srt) where code is a 2-letter lowercase code, else None.

    Excludes files with extra modifiers like .hi, .forced, .combined-xx,
    and PR 158 sync-engine outputs (.ffsubsync, .autosubsync, .alass).
    """
    filename = os.path.basename(path)
    if not filename.startswith(base + "."):
        return None
    rest = filename[len(base) + 1:]
    parts = rest.rsplit(".", 1)
    if len(parts) != 2 or parts[1] != "srt":
        return None
    middle = parts[0]
    if "." in middle:
        return None
    if "-" in middle:
        return None
    if len(middle) != 2 or not middle.isalpha() or not middle.islower():
        return None
    return middle

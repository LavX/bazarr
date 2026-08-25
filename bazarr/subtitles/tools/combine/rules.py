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

    Custom-language filename tags (e.g. .zh-TW.srt → zt, .pt-BR.srt → pb) are
    resolved through CustomLanguage, matching the external-subtitle indexer.

    Returns SourcePaths if every requested language has a matching file,
    None if any is missing.

    languages: ordered list of code2 strings. First is primary.
    """
    if not languages or len(languages) < 2:
        return None

    base = os.path.splitext(os.path.basename(video_path))[0]
    if not base:
        return None

    from .naming import external_subtitles_search_dirs
    search_dirs = external_subtitles_search_dirs(video_path)

    candidates = []
    for d in search_dirs:
        if d and os.path.isdir(d):
            candidates.extend(glob.glob(
                os.path.join(glob.escape(d), f"{glob.escape(base)}.*.srt")
            ))

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

    Also recognizes CustomLanguage filename tags such as .zh-TW / .pt-BR so
    profiles that request zt / pb can combine those on-disk files.

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

    # Combined outputs use ".combined-" (e.g. en.combined-hu). Do not treat
    # every hyphen as combined — custom langs legitimately use tags like zh-TW.
    if "combined-" in middle:
        return None

    segments = middle.split(".")
    if not segments:
        return None

    # Sync-engine outputs are never combine sources, including custom-lang tags
    # such as zh-TW.ffsubsync.srt.
    if segments[-1].lower() in _SYNC_ENGINES:
        return None

    custom = _custom_language_match(filename, path)
    if custom is not None:
        return custom

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


def _custom_language_match(filename, path):
    """Map CustomLanguage filename tags to (code2, priority), or None."""
    try:
        from languages.custom_lang import CustomLanguage
    except Exception:
        return None

    custom_code = CustomLanguage.found_external(filename, path)
    if not custom_code:
        return None

    if ":" not in custom_code:
        return custom_code, _PLAIN

    code, modifier = custom_code.split(":", 1)
    modifier = modifier.lower()
    if modifier in ("hi", "sdh", "cc"):
        return code, _HI
    if modifier == "forced":
        return code, _FORCED
    return None

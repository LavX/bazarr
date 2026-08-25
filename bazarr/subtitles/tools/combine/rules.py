# coding=utf-8

import glob
import logging
import os
from dataclasses import dataclass
from functools import lru_cache


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

    Accepts plain, HI, and forced variants. When one language has several
    variants on disk, prefers plain, then HI, then forced; when it has several
    filename spellings at the same priority, the lowest path wins. Sync-engine
    outputs (.ffsubsync, .autosubsync, .alass) and combined outputs are never
    picked.

    Custom-language filename tags (e.g. .zh-TW.srt to zt, .pt-BR.srt to pb) are
    resolved through CustomLanguage as a fallback, after the plain 2-letter
    form fails, and only from the isolated tag segment.

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
            # Sorted so a plain-priority tie between two spellings of the
            # same language (.zh-TW and .zht both mean zt) resolves the same
            # way on every run, instead of following directory order.
            candidates.extend(sorted(glob.glob(
                os.path.join(glob.escape(d), f"{glob.escape(base)}.*.srt")
            )))

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

    Recognizes <base>.<tag>.srt (priority _PLAIN),
    <base>.<tag>.hi.srt and <base>.<tag>.sdh.srt and <base>.<tag>.cc.srt
    (priority _HI), and <base>.<tag>.forced.srt (priority _FORCED).

    <tag> is a plain 2-letter lowercase code, or, when that does not apply, a
    CustomLanguage filename tag such as .zh-TW or .pt-BR mapped to its code2
    (zt, pb). The custom lookup is a fallback, never a pre-empt: 2-letter codes
    are ISO 639-1 and must not be re-read as a custom tag (.sc is Sardinian,
    not Simplified Chinese). It also sees the isolated tag only, so neither the
    video's own name nor a neighbouring segment can supply a tag.

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
    # every hyphen as combined: custom langs legitimately use tags like zh-TW.
    # The modifier whitelist below rejects these names too; this keeps the
    # "never re-combine a combined file" rule explicit and independent of it.
    if "combined-" in middle.lower():
        return None

    segments = middle.split(".")

    # Sync-engine outputs are never combine sources, including custom-lang tags
    # such as zh-TW.ffsubsync.srt.
    if segments[-1].lower() in _SYNC_ENGINES:
        return None

    # Exactly <tag> or <tag>.<modifier>. Anything longer is another artifact
    # (or another edition's file, since the glob only prefix-tests the base).
    if len(segments) > 2:
        return None

    code = _language_code_from_tag(segments[0])
    if code is None:
        return None

    if len(segments) == 1:
        return code, _PLAIN

    modifier = segments[1].lower()
    if modifier in ("hi", "sdh", "cc"):
        return code, _HI
    if modifier == "forced":
        return code, _FORCED
    return None


def _language_code_from_tag(tag):
    """Map one isolated filename tag to a language code2, or None.

    A 2-letter tag that really is an ISO 639-1 code is taken as-is, so
    Movie.sc.srt stays Sardinian instead of being claimed by Simplified
    Chinese's alias list. Everything else falls back to CustomLanguage, which
    is what lets .zh-TW / .pt-BR / .es-MX resolve to the zt / pb / ea codes
    profiles request, and equally what lets the 2-letter aliases .tc and .gb
    resolve to zt and zh. Taking those at face value instead would yield the
    codes "tc" and "gb", which no profile asks for, so the file would be
    invisible here while the regular indexer files it as Chinese.

    A tag is one filename segment, so CustomLanguage's single dotted spelling,
    .es.ar, is deliberately not honoured: accepting a two-segment tag would
    also read Movie.en.pt.srt as Portuguese, since ".en.pt" ends in ".pt".
    """
    if len(tag) == 2 and tag.isalpha() and tag.islower() and _is_iso_639_1(tag):
        return tag
    return _custom_language_code(tag)


@lru_cache(maxsize=None)
def _is_iso_639_1(tag):
    """True when the 2-letter tag is a real ISO 639-1 code.

    Cached: this runs once per candidate file per directory scan, and the
    answer for a given tag never changes.
    """
    from babelfish import Language
    from babelfish.exceptions import LanguageReverseError

    try:
        Language.fromalpha2(tag)
    except (LanguageReverseError, ValueError):
        return False
    return True


_custom_language_cls = None
_custom_language_unavailable = False


def _custom_language_code(tag):
    """Map a CustomLanguage filename tag (zh-TW, pt-BR, cht, ...) to its code2.

    The tag is matched in isolation, as ".<tag>.srt", because
    CustomLanguage.get_alpha_type substring-matches the name it is given: fed a
    whole filename it reads "Elephant.hi.srt" as Traditional Chinese HI, since
    "hant.hi" is inside "elephant.hi". Modifiers are handled by the caller, so
    only the bare code is used here.
    """
    global _custom_language_cls, _custom_language_unavailable
    if _custom_language_cls is None:
        if _custom_language_unavailable:
            return None
        try:
            from languages.custom_lang import CustomLanguage
        except Exception:
            # Remember the failure: Python does not cache failed imports, and
            # this runs once per candidate file.
            _custom_language_unavailable = True
            logging.debug(
                "BAZARR combine: CustomLanguage unavailable, custom-language "
                "filename tags will not resolve", exc_info=True,
            )
            return None
        _custom_language_cls = CustomLanguage

    # subtitle_path is deliberately None: it only feeds CustomLanguage's fuzzy
    # whole-path checks, and the video path must not influence the tag.
    custom_code = _custom_language_cls.found_external(f".{tag}.srt", None)
    if not custom_code:
        return None
    code = custom_code.split(":", 1)[0]
    if len(code) != 2 or not code.isalpha() or not code.islower():
        return None
    return code

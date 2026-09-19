# coding=utf-8
# fmt: off

import ast
import os


def _find_existing_subtitle_path(subtitles_field, source_lang, path_replace_fn=None):
    """Return on-disk path of an existing external subtitle for source_lang
    (ignoring :hi / :forced variants), or None. subtitles_field is the raw
    DB value (a python-literal list of [code, path, length] tuples).

    Why: The wanted-scan auto-translate path in both series and movies needs
    to locate an existing same-language external subtitle on disk before
    deciding whether to queue a translation. Sharing this lookup avoids the
    copy-paste divergence the previous duplicate implementations risked.
    What: Parses the stored subtitles list and returns the first path whose
    language matches source_lang and exists on disk (preferring plain over
    :hi / :forced variants). When path_replace_fn is provided (e.g.
    path_mappings.path_replace / path_replace_movie), each candidate path
    is mapped through it before the existence check and the mapped path is
    returned, so Docker / remote setups with path mappings see the correct
    on-disk location.
    Test: Pass a literal list like "[['en','/tmp/x.srt',null]]" with an
    existing file and source_lang='en' — assert it returns '/tmp/x.srt';
    pass with a missing file or wrong code — assert None; pass a
    path_replace_fn that rewrites '/remote/x.srt' to a real local path and
    assert the mapped path is returned.
    """
    if not subtitles_field:
        return None
    try:
        entries = ast.literal_eval(subtitles_field)
    except (ValueError, SyntaxError):
        return None
    # First pass: prefer plain (non-HI, non-forced) source language
    for entry in entries:
        if not entry or len(entry) < 2:
            continue
        code = (entry[0] or '')
        path = entry[1]
        mapped = path_replace_fn(path) if (path_replace_fn and path) else path
        if code == source_lang and mapped and os.path.exists(mapped):
            return mapped
    # Fallback: accept any source-language variant
    for entry in entries:
        if not entry or len(entry) < 2:
            continue
        code = (entry[0] or '').split(':')[0]
        path = entry[1]
        mapped = path_replace_fn(path) if (path_replace_fn and path) else path
        if code == source_lang and mapped and os.path.exists(mapped):
            return mapped
    return None


def _provider_file_on_disk(subtitles_field, language, translated_path, path_replace_fn=None):
    """True when something other than a translation covers ``language`` on disk.

    The auto-translate guard in both wanted scans keys on the file the last
    action=6 row points at, and replacing that file is precisely what an upgrade
    does. The guard therefore fails after every replacement and the translation
    is queued again, which puts a translated row with the 50% default back on top
    of the history and lets the next upgrade fetch the same listing a second
    time. A provider sourced file for the language on disk ends the question.

    ``translated_path`` is the path that action=6 row recorded; that entry is
    skipped because it is the machine translation, not a provider file. There is
    no action column to check per file, so the comparison is by path: the scan
    already knows the translated path and everything else indexed for the
    language is something the indexer found on disk, which is what this answers.

    What counts mirrors how list_missing_subtitles decides a language is
    covered, so this cannot disagree with the list the scan is walking: the :hi
    variant does cover a plain entry, a :forced file does not, and neither does a
    combined artifact, which is a bilingual stack rather than a subtitle in that
    language.
    """
    if not subtitles_field:
        return False
    try:
        entries = ast.literal_eval(subtitles_field)
    except (ValueError, SyntaxError):
        return False
    base = (language or '').split(':')[0]
    for entry in entries:
        if not entry or len(entry) < 2:
            continue
        parts = (entry[0] or '').split(':')
        path = entry[1]
        if parts[0] != base or not path:
            continue
        variants = parts[1:]
        if 'forced' in variants or any(v.startswith('combined-') for v in variants):
            continue
        if translated_path and path == translated_path:
            continue
        mapped = path_replace_fn(path) if path_replace_fn else path
        if mapped and os.path.exists(mapped):
            return True
    return False

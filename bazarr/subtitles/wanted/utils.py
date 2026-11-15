# coding=utf-8
# fmt: off

import ast
import logging
import os
import time

from app.config import settings


def evaluate_translation_gate():
    """Whether the configured translator can translate, asked once per scan run.

    A closed gate is logged here with its reason, because a scan that queues
    nothing would otherwise say nothing at all about the languages it then sent
    to providers instead of translating.
    """
    from subtitles.tools.translate.availability import translation_available
    availability = translation_available()
    if not availability.available:
        logging.info("BAZARR is not translating missing subtitles: %s", availability.reason)
    return availability.available


# The history actions that put a different file at a subtitle path or remove it:
# delete, the downloads and upgrade, upload, translation and extraction from an
# embedded track. A sync (5) rewrites the same file, an embedded track (7) has
# no path and a manual score (9) only rescored it, so none of those says where
# the file at the path came from.
FILE_REPLACING_ACTIONS = (0, 1, 2, 3, 4, 6, 8)


def source_wait_enabled():
    """Whether translator.min_source_wait_minutes asks automatic translation to wait."""
    return bool(settings.translator.min_source_wait_minutes)


# The history rows a source's wait is read from: the actions that put a file at
# the path or removed it, and the sync (5) that rewrites it in place.
SOURCE_HISTORY_ACTIONS = FILE_REPLACING_ACTIONS + (5,)

# How far a file's modification time may run ahead of the history row that
# recorded it, or of the present, before it counts as a later write. Some file
# systems round a modification time up by a couple of seconds.
_MTIME_SLACK_SECONDS = 2


def _written_after(source_path, reference):
    """Whether the file was written after ``reference`` (a history timestamp).

    A modification time dated further ahead than now cannot be told from a clock
    disagreement, so it does not count as a later write: the row stands.
    """
    try:
        modified_at = os.path.getmtime(source_path)
    except OSError:
        return False
    return reference.timestamp() + _MTIME_SLACK_SECONDS < modified_at <= time.time() + _MTIME_SLACK_SECONDS


def source_history_for_wait(rows, source_path, local_forms):
    """What the history says about when a source arrived and where it came from.

    Returns ``(written_at, hand_upload)``. ``rows`` come newest first with the
    stored (reverse-mapped) path, which ``local_forms`` maps to the paths the
    scan may know it by: through the owning instance's mapping, which is how
    rows are stored now, and through the global one, which older rows used. Only
    rows stored for this very file count: a newer upload of another file says
    nothing about it.

    The newest row that put the file there (not a sync) gives the time and
    whether it was a hand upload. Only a sync keeps that row standing for a
    changed file: the sync rewrote the file in place and logged it, so a
    modification time up to the newest sync row's is the sync's own. Any other
    rewrite (an edit, a modification tool, a copy or an import from outside, a
    save not recorded yet) leaves a later modification time, and then the row's
    time and its upload exemption no longer apply: the file time decides and the
    wait starts over. A removal, or no row at all, leaves the file time to
    decide too, and ``written_at`` is then None.
    """
    synced_at = None
    for row in rows:
        if not row.subtitles_path or source_path not in local_forms(row.subtitles_path):
            continue
        if row.action == 5:
            if synced_at is None:
                synced_at = row.timestamp
            continue
        if row.action == 0:
            return None, False
        if _written_after(source_path, synced_at or row.timestamp):
            return None, False
        return row.timestamp, row.action == 4
    return None, False


def source_wait_elapsed(source_path, written_at=None):
    """Whether a source subtitle is old enough to translate automatically.

    translator.min_source_wait_minutes is measured from ``written_at``, the
    history timestamp of the download or upload that put the source there. Only
    a sync keeps that time: it rewrites the file in place and does not restart
    the wait. Any other change to the file (an edit, a modification tool, a copy
    or an import from outside) counts from the file's modification time, which
    is the fallback for a source with no history row too (a sidecar a disk scan
    found). 0 turns the wait off. A source whose modification time cannot be
    read is not held back.

    A source dated in the future is a clock disagreement: it counts as written
    just now, so it is held for the wait, unless it is dated further ahead than
    the wait itself. A date that far out cannot be a file written a moment ago,
    and holding it would hold it for as long as the date stays ahead.

    Two limits are accepted. A sync vouches for an unlogged rewrite before it:
    the sync row only tells that the file's time is the sync's own, so a file
    replaced from outside and synced afterwards is still measured from its
    download. And a file system whose clock runs a few seconds ahead of this one
    dates a fresh download later than its history row, so the wait counts from
    the file time and ends later, by up to that skew.
    """
    wait_minutes = settings.translator.min_source_wait_minutes
    if not wait_minutes:
        return True
    if written_at is not None:
        # History timestamps are naive local times, as datetime.now() writes them.
        written = written_at.timestamp()
    else:
        try:
            written = os.path.getmtime(source_path)
        except OSError:
            return True
    age = time.time() - written
    if age < 0:
        return -age > wait_minutes * 60
    return age >= wait_minutes * 60


def translation_capture_paths(destination, source_path):
    """The files whose versions an automatic translation captures when it queues.

    The destination is always captured: a subtitle that lands there while the
    job waits is the better one. While the source wait is on, the source is
    captured as well, so a source replaced while the job waits cancels the job
    and the next scan applies the wait to the new file. The destination stays
    first.
    """
    if settings.translator.min_source_wait_minutes:
        return (destination, source_path)
    return (destination,)


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
    existing file and source_lang='en'; assert it returns '/tmp/x.srt';
    pass with a missing file or wrong code; assert None; pass a
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

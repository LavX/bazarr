# coding=utf-8

import ast
import os
import logging
import unicodedata

from guess_language import guess_language
from sqlalchemy import delete
from subliminal_patch import core
from subzero.language import Language
from charset_normalizer import detect

from constants import MAXIMUM_SUBTITLE_SIZE
from app.config import settings
from utilities.path_mappings import path_mappings
from languages.custom_lang import CustomLanguage
from subtitles.indexer.entries import entry_codec, normalize_codec
from subtitles.tools.subsync_engines import SYNC_ENGINES, sync_engine_from_output_path, sync_output_owner_is_unique

import re as _re_combine

_COMBINED_MODIFIER_PATTERN = _re_combine.compile(r'^combined-[a-z]{2}(?:-[a-z]{2})?$')


def get_external_subtitles_path(file, subtitle):
    fld = os.path.dirname(file)

    if settings.general.subfolder == "current":
        path = os.path.join(fld, subtitle)
    elif settings.general.subfolder == "absolute":
        custom_fld = settings.general.subfolder_custom
        if os.path.exists(os.path.join(fld, subtitle)):
            path = os.path.join(fld, subtitle)
        elif os.path.exists(os.path.join(custom_fld, subtitle)):
            path = os.path.join(custom_fld, subtitle)
        else:
            path = None
    elif settings.general.subfolder == "relative":
        custom_fld = os.path.join(fld, settings.general.subfolder_custom)
        if os.path.exists(os.path.join(fld, subtitle)):
            path = os.path.join(fld, subtitle)
        elif os.path.exists(os.path.join(custom_fld, subtitle)):
            path = os.path.join(custom_fld, subtitle)
        else:
            path = None
    else:
        path = None

    return path


def get_subtitle_destination_path(file, subtitle):
    """Where a new external subtitle for ``file`` gets written.

    get_external_subtitles_path() answers only for files that already exist, which is
    right for indexing and wrong for a first-time write: in the absolute and relative
    subfolder modes it returned None and a translation crashed on save. An existing file
    in either location is still preferred so a re-run overwrites what the user already
    sees; otherwise the configured folder is used and created, the way downloads do it.
    """
    existing = get_external_subtitles_path(file, subtitle)
    if existing:
        return existing
    from utilities.helper import get_target_folder
    folder = get_target_folder(file) or os.path.dirname(file)
    return os.path.join(folder, subtitle)


def normalize_subtitle_language_variant(language, forced=False, hi=False):
    language_text = str(language)
    parts = language_text.split(':')
    base = parts[0]
    variants = {part.lower() for part in parts[1:]}
    normalized_variants = []

    # Keep the same priority as ProcessSubtitlesResult.language_code.
    if hi or "hi" in variants:
        normalized_variants.append('hi')
    if forced or "forced" in variants:
        normalized_variants.append('forced')
    sync_variants = sorted(part for part in variants if part.startswith('sync-'))
    normalized_variants.extend(sync_variants)
    combined_variant = next(
        (part for part in variants if _COMBINED_MODIFIER_PATTERN.match(part)),
        None,
    )
    if combined_variant:
        normalized_variants.append(combined_variant)
    if not normalized_variants:
        return base
    return ':'.join([base] + normalized_variants)


def embedded_history_language(language):
    """The language an Embedded Source history row records for a track.

    Forced wins over HI here, unlike the language code a download is stored
    under: a forced track covers only part of the dialogue, so a forced and HI
    track must not be recorded as plain HI, where it would stand in for the
    full-dialogue subtitles of that language.
    """
    base, *variants = normalize_subtitle_language_variant(language).split(':')
    return base + (':forced' if 'forced' in variants else ':hi' if 'hi' in variants else '')


def embedded_codecs_by_language(stored):
    """The codecs of the embedded entries of a stored listing, by history language.

    A codec is None for an entry indexed before codecs were stored, or whose
    format could not be named. An unreadable listing has no entries.
    """
    try:
        listing = ast.literal_eval(stored) if stored else []
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return {}
    found = {}
    for entry in listing if isinstance(listing, list) else []:
        if not isinstance(entry, (list, tuple)) or not entry or not isinstance(entry[0], str):
            continue
        if len(entry) > 1 and entry[1]:
            continue
        found.setdefault(embedded_history_language(entry[0]), set()).add(entry_codec(entry))
    return found


def note_uncounted_track(uncounted, key, codec):
    """Record a track of this file that the indexer does not count (hidden, or skipped by an ignore switch)."""
    uncounted.setdefault(embedded_history_language(key), set()).add(normalize_codec(codec))


def retract_embedded_history(session, table, video_path, uncounted, counted, previous, arr_instance_id=None,
                             guard=True):
    """Delete the Embedded Source rows of a video for tracks the indexer stopped counting.

    A track the user hid, or that an ignore switch now skips, stops covering its
    language, so the row that recorded it must stop ending the upgrades of that
    language. ``uncounted`` maps the history language of each such track seen on
    this pass to its codecs (see ``note_uncounted_track``), ``counted`` holds the
    track keys the pass did list, and ``previous`` the codecs the last stored
    listing counted for each language (``embedded_codecs_by_language``).

    A language is retracted only when a codec the last listing counted is the
    one now skipped, and no counted track still covers the language. A track
    that is merely gone from the container is never retracted, nor is one whose
    codec is not the skipped one: another tool may have copied it out next to the
    video and stripped it, leaving a hidden or ignored track of the same
    language behind, and the row is the only trace of the copy. The index on
    (video_path, language) serves the delete.

    With ``guard`` it never raises, so indexing goes on if the delete fails and
    the row ends upgrades a little longer. A caller inside a transaction that a
    failed statement would abort passes ``guard=False``.
    """
    try:
        covered = {embedded_history_language(key) for key in counted or ()}
        languages = sorted(language for language, codecs in (uncounted or {}).items()
                           if language not in covered and codecs & (previous or {}).get(language, set()))
        if not languages or not video_path:
            return 0
        statement = delete(table).where(table.action == 7, table.video_path == video_path,
                                        table.language.in_(languages))
        if arr_instance_id is not None:
            statement = statement.where(table.arr_instance_id == arr_instance_id)
        return session.execute(statement).rowcount or 0
    except Exception:
        if not guard:
            raise
        logging.exception("BAZARR error retracting embedded subtitle history for %s", video_path)
        return 0


def kept_embedded_entries(stored, hidden=()):
    """The embedded entries of a stored listing that a new listing should keep.

    For an indexer that could not read the file's tracks this time. A failed
    read says nothing about the tracks, and a listing stored without them makes
    every embedded language wanted again. Hidden keys and codecs the ignore
    switches now skip are dropped, as a good read would drop them.
    """
    try:
        listing = ast.literal_eval(stored) if stored else []
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return []
    kept = []
    for entry in listing if isinstance(listing, list) else []:
        if not isinstance(entry, (list, tuple)) or not entry or not isinstance(entry[0], str):
            continue
        if len(entry) > 1 and entry[1]:
            continue
        codec = entry_codec(entry)
        if entry[0] in hidden or (codec and ((settings.general.ignore_pgs_subs and codec == 'pgs') or
                                             (settings.general.ignore_vobsub_subs and codec == 'vobsub') or
                                             (settings.general.ignore_ass_subs and codec == 'ass'))):
            continue
        kept.append(list(entry))
    return kept


def sync_engine_from_subtitle_name(subtitle):
    return sync_engine_from_output_path(subtitle)


def _normalized_video_stem(stem):
    return unicodedata.normalize('NFC', stem.lower())


def _language_code_from_sync_engine_output(subtitle, video_filename=None):
    filename = os.path.basename(subtitle)
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in core.SUBTITLE_EXTENSIONS:
        return None

    parts = stem.split('.')
    if len(parts) < 3 or parts[-1].lower() not in SYNC_ENGINES:
        return None

    parts = parts[:-1]
    # A keep-all sync of a combined file produces e.g.
    # Movie.en.combined-hu.ffsubsync.srt. That is not a plain-language sync
    # output: skip it cleanly here (returning None) so the indexer does not
    # mis-parse "combined-hu" as a language and raise downstream. Such a synced
    # combined output is a known limitation: it is left on disk but not indexed
    # as a tracked variant. Overwrite-mode sync of a combined file works in
    # place (it keeps the Movie.en.combined-hu.srt name) and is unaffected.
    if parts and _COMBINED_MODIFIER_PATTERN.match(parts[-1].lower()):
        return None
    variants = []
    if parts and parts[-1].lower() in ['hi', 'sdh', 'cc']:
        variants.append('hi')
        parts = parts[:-1]
    if parts and parts[-1].lower() == 'forced':
        variants.append('forced')
        parts = parts[:-1]
    if not parts:
        return None

    if video_filename is not None:
        video_stem = os.path.splitext(video_filename)[0]
        if _normalized_video_stem('.'.join(parts[:-1])) != _normalized_video_stem(video_stem):
            return None

    language = parts[-1].lower().replace('_', '-')
    if not language:
        return None

    return ':'.join([language] + variants)


def add_sync_engine_outputs(dest_folder, subtitles, video_filename=None, video_path=None, ownership_index=None):
    """Add generated outputs belonging to the video, regardless of save preferences."""
    if not os.path.isdir(dest_folder):
        return subtitles

    video_stem = None
    media_stems = set()
    if video_path is not None:
        video_filename = os.path.basename(video_path)
    if video_filename is not None:
        video_stem = _normalized_video_stem(os.path.splitext(video_filename)[0])
        media_folder = (os.path.dirname(video_path) or '.') if video_path is not None else dest_folder
        with os.scandir(media_folder) as media_files:
            media_stems = {_normalized_video_stem(os.path.splitext(entry.name)[0])
                           for entry in media_files
                           if entry.is_file() and entry.name.lower().endswith(core.VIDEO_EXTENSIONS)}

    ownership = {}
    for subtitle in os.listdir(dest_folder):
        if not sync_engine_from_subtitle_name(subtitle):
            continue

        subtitle_path = os.path.join(dest_folder, subtitle)
        if not os.path.isfile(subtitle_path):
            continue

        if video_path is not None:
            stem, extension = os.path.splitext(subtitle)
            source_path = os.path.join(dest_folder, stem.rsplit('.', 1)[0] + extension)
            if source_path not in ownership:
                ownership[source_path] = sync_output_owner_is_unique(video_path, source_path,
                                                                    ownership_index=ownership_index)
            if not ownership[source_path]:
                subtitles.pop(subtitle, None)
                continue
        if subtitle in subtitles:
            continue

        if video_stem is not None:
            stem, extension = os.path.splitext(subtitle)
            if extension.lower() not in core.SUBTITLE_EXTENSIONS:
                continue
            owner_stem = _normalized_video_stem(stem.rsplit('.', 1)[0])
            if owner_stem == video_stem:
                # The complete basename owns an untagged output. Detect its
                # language normally, even if the save preference has changed.
                subtitles[subtitle] = None
                continue
            if owner_stem in media_stems:
                # Movie.en.mkv owns Movie.en.ffsubsync.srt when it exists.
                # Without that sibling, it can be a tagged output of Movie.mkv.
                continue

        language_code = _language_code_from_sync_engine_output(subtitle, video_filename=video_filename)
        if not language_code:
            logging.debug("BAZARR skipping unrelated or unrecognized generated sync subtitle: %s", subtitle_path)
            continue

        try:
            subtitles[subtitle] = _get_lang_from_str(language_code)
        except Exception:
            logging.debug("BAZARR skipping generated sync subtitle with unsupported language: %s", subtitle_path)

    return subtitles


def subtitle_language_with_sync_modifier(language_str, subtitle):
    engine = sync_engine_from_subtitle_name(subtitle)
    if not engine:
        return language_str
    parts = [part for part in str(language_str).split(':') if part]
    if not parts:
        return language_str
    base_language = parts[0]
    modifiers = [part.lower() for part in parts[1:] if not part.lower().startswith('sync-')]
    modifiers.append(f'sync-{engine}')
    return ':'.join([base_language] + modifiers)


def combined_modifier_from_subtitle_name(subtitle):
    """Return the 'combined-X[-Y]' modifier if the filename matches the
    combined-subtitle convention, else None."""
    from subtitles.tools.combine.naming import parse_combined_filename
    info = parse_combined_filename(subtitle)
    if info is None:
        return None
    return "combined-" + "-".join(info.secondaries)


def _language_code_from_combined_output(subtitle):
    from subtitles.tools.combine.naming import parse_combined_filename
    info = parse_combined_filename(subtitle)
    if info is None:
        return None
    return info.primary


def add_combined_outputs(dest_folder, subtitles, video_filename=None):
    """Scan dest_folder for combined-subtitle files and add them to the
    subtitles dict so the rest of the indexer treats them as known artifacts.

    video_filename: the basename of the video being indexed. When provided,
    only combined files belonging to that video are added. This matters in
    folders holding multiple episodes/movies, where an unfiltered scan would
    attach another item's combined file to the wrong video.

    Mirrors add_sync_engine_outputs from PR 158 in structure."""
    from subtitles.tools.combine.naming import parse_combined_filename
    if not os.path.isdir(dest_folder):
        return subtitles

    video_stem = os.path.splitext(video_filename)[0] if video_filename else None

    for subtitle in os.listdir(dest_folder):
        if subtitle in subtitles:
            continue

        info = parse_combined_filename(subtitle)
        if info is None:
            continue

        # Exact-base match, not a prefix test: the combined file must be exactly
        # <video_stem>.<primary>.combined-<secondaries>.<ext>. A prefix check
        # would wrongly attach a sibling's file (e.g. "Movie.Extended.en.combined
        # -es.srt") to "Movie" when their stems share a dotted prefix.
        if video_stem is not None:
            expected = (f"{video_stem}.{info.primary}.combined-"
                        f"{'-'.join(info.secondaries)}.{info.format}")
            if subtitle != expected:
                continue

        subtitle_path = os.path.join(dest_folder, subtitle)
        if not os.path.isfile(subtitle_path):
            continue

        language_code = info.primary

        try:
            subtitles[subtitle] = _get_lang_from_str(language_code)
        except Exception:
            logging.debug(
                "BAZARR skipping combined subtitle with unsupported language: %s",
                subtitle_path,
            )

    return subtitles


def subtitle_language_with_combined_modifier(language_str, subtitle):
    """Stamp the combined modifier onto the language string for this subtitle
    file, preserving any pre-existing hi/forced/sync- modifiers."""
    modifier = combined_modifier_from_subtitle_name(subtitle)
    if not modifier:
        return language_str
    parts = [part for part in str(language_str).split(':') if part]
    if not parts:
        return language_str
    base_language = parts[0]
    # Drop any pre-existing combined-* modifier; we always re-stamp from the
    # filename so it is authoritative.
    modifiers = [
        part.lower()
        for part in parts[1:]
        if not part.lower().startswith('combined-')
    ]
    modifiers.append(modifier)
    return ':'.join([base_language] + modifiers)


def guess_external_subtitles(dest_folder, subtitles, media_type, previously_indexed_subtitles_to_exclude=None,
                             path_reverse=None):
    for subtitle, language in subtitles.items():
        subtitle_path = os.path.join(dest_folder, subtitle)
        reverse = path_reverse or (path_mappings.path_replace_reverse if media_type == "series"
                                   else path_mappings.path_replace_reverse_movie)
        reversed_subtitle_path = reverse(subtitle_path)

        if previously_indexed_subtitles_to_exclude:
            x_found_lang = None
            for x_lang, x_path, x_size in previously_indexed_subtitles_to_exclude:
                if x_path == reversed_subtitle_path and x_size == os.stat(subtitle_path).st_size:
                    x_found_lang = x_lang
                    break
            if x_found_lang:
                if not language:
                    subtitles[subtitle] = _get_lang_from_str(x_found_lang)
                continue

        if not language:
            if os.path.exists(subtitle_path) and os.path.splitext(subtitle_path)[1] in core.SUBTITLE_EXTENSIONS:
                logging.debug("BAZARR falling back to file content analysis to detect language.")
                detected_language = None

                # detect forced subtitles
                forced = True if os.path.splitext(os.path.splitext(subtitle)[0])[1] == '.forced' else False

                # to improve performance, skip detection of files larger that 1M
                if os.path.getsize(subtitle_path) > MAXIMUM_SUBTITLE_SIZE:
                    logging.debug(f"BAZARR subtitles file is too large to be text based. Skipping this file: "  # noqa: G004
                                  f"{subtitle_path}")
                    continue

                with open(subtitle_path, 'rb') as f:
                    text = f.read()

                encoding = detect(text)
                if encoding and 'encoding' in encoding and encoding['encoding']:
                    encoding = detect(text)['encoding']
                else:
                    logging.debug(f"BAZARR skipping this subtitles because we can't guess the encoding. "  # noqa: G004
                                  f"It's probably a binary file: {subtitle_path}")
                    continue
                text = text.decode(encoding)

                detected_language = guess_language(text)

                # add simplified and traditional chinese detection
                if detected_language == 'zh':
                    traditional_chinese_fuzzy = [u"繁", u"雙語"]
                    traditional_chinese = [".cht", ".tc", ".zh-tw", ".zht", ".zh-hant", ".zhhant", ".zh_hant",
                                           ".hant", ".big5", ".traditional"]
                    if str(os.path.splitext(subtitle)[0]).lower().endswith(tuple(traditional_chinese)) or \
                            (str(subtitle_path).lower())[:-5] in traditional_chinese_fuzzy:
                        detected_language = 'zt'

                if detected_language:
                    logging.debug(f"BAZARR external subtitles detected and guessed this language: {detected_language}")  # noqa: G004
                    try:
                        subtitles[subtitle] = Language.rebuild(Language.fromietf(detected_language), forced=forced,
                                                               hi=False)
                    except Exception:
                        pass

        # If language is still None (undetected), skip it
        if hasattr(subtitles[subtitle], 'basename') and not subtitles[subtitle].basename:
            continue

        # Skip HI detection if forced
        if hasattr(language, 'forced') and language.forced:
            continue

        # Detect hearing-impaired external subtitles not identified in filename
        if hasattr(subtitles[subtitle], 'hi') and not subtitles[subtitle].hi:
            subtitle_path = os.path.join(dest_folder, subtitle)

            # check if file exist:
            if os.path.exists(subtitle_path) and os.path.splitext(subtitle_path)[1] in core.SUBTITLE_EXTENSIONS:
                # to improve performance, skip detection of files larger that 1M
                if os.path.getsize(subtitle_path) > MAXIMUM_SUBTITLE_SIZE:
                    logging.debug(f"BAZARR subtitles file is too large to be text based. Skipping this file: "  # noqa: G004
                                  f"{subtitle_path}")
                    continue

                with open(subtitle_path, 'rb') as f:
                    text = f.read()

                encoding = detect(text)
                if encoding and 'encoding' in encoding and encoding['encoding']:
                    encoding = detect(text)['encoding']
                else:
                    logging.debug(f"BAZARR skipping this subtitles because we can't guess the encoding. "  # noqa: G004
                                  f"It's probably a binary file: {subtitle_path}")
                    continue
                text = text.decode(encoding)

                if os.path.splitext(subtitle_path)[1] == 'srt':
                    if core.parse_for_hi_regex(subtitle_text=text,
                                               alpha3_language=language.alpha3 if hasattr(language, 'alpha3') else
                                               None):
                        subtitles[subtitle] = Language.rebuild(subtitles[subtitle], forced=False, hi=True)
    return subtitles


def _get_lang_from_str(x_found_lang):
    x_found_lang_split = x_found_lang.split(':')[0]
    x_hi = ':hi' in x_found_lang.lower()
    x_forced = ':forced' in x_found_lang.lower()

    if len(x_found_lang_split) == 2:
        x_custom_lang_attr = "alpha2"
    elif len(x_found_lang_split) == 3:
        x_custom_lang_attr = "alpha3"
    else:
        x_custom_lang_attr = "language"

    x_custom_lang = CustomLanguage.from_value(x_found_lang_split, attr=x_custom_lang_attr)

    if x_custom_lang is not None:
        return Language.rebuild(x_custom_lang.subzero_language(), hi=x_hi, forced=x_forced)
    else:
        return Language.rebuild(Language.fromietf(x_found_lang), hi=x_hi, forced=x_forced)

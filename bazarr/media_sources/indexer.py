# coding=utf-8
"""Listing the subtitles beside a source item and working out which languages it still lacks.

The listing uses the shared helpers the movie indexer uses, with paths taken as stored: a source item's path is
already the Bazarr-visible one, so nothing is mapped in either direction. The source item's own probe cache
supplies embedded tracks and audio languages for the profile rules.
"""
import ast
import logging
import os
import sqlite3

import sqlalchemy as sa
from babelfish.exceptions import LanguageError
from sqlalchemy.orm import Session

from subliminal_patch import core, search_external_subtitles

from app.config import settings
from app.database import get_audio_profile_languages, get_profile_cutoff, get_profiles_list
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha2_from_alpha3, get_language_set
from subtitles.adaptive_searching import is_search_given_up
from subtitles.indexer.entries import embedded_entry, normalize_codec
from subtitles.indexer.utils import (_get_lang_from_str, add_combined_outputs, add_sync_engine_outputs,
                                     get_external_subtitles_path, guess_external_subtitles,
                                     normalize_subtitle_language_variant,
                                     partition_embedded_entries, subtitle_language_with_combined_modifier,
                                     subtitle_language_with_sync_modifier)
from subtitles.tools.subsync_engines import (SYNC_ENGINE_LANGUAGE_MODIFIERS, SyncOutputOwnerIndex,
                                             _normalized_media_stem, subtitle_write_locks)
from utilities.helper import get_subtitle_destination_folder, get_target_folder
from utilities.video_analyzer import audio_languages_from_metadata, embedded_subtitles_from_metadata

from . import repository
from .analysis import (cached_metadata, failed_probe_values, index_matches_file, local_stat, probe_item,
                       read_metadata, streams)
from .db import handle, source_transaction


class SourceOutputOwnerIndex(SyncOutputOwnerIndex):
    """Who owns a generated subtitle in a shared subtitle folder, answered from the source tables only.

    The base index reads the arr and Sportarr tables, which a source never does. Here the owners are the source
    items, each claiming its own folder and, with a custom subtitle folder, that folder too.
    """

    def __init__(self, target=None):
        super().__init__()
        self._target = target
        # Post-save indexing needs the original failure to discard an incomplete listing.
        self.propagate_errors = target is not None

    def _load_owners(self):
        custom_folder = get_subtitle_destination_folder()
        subfolder = settings.general.subfolder
        absolute_folder = os.path.normcase(os.path.realpath(custom_folder)) if subfolder == 'absolute' else None
        owners = {}
        for row in handle(self._target).execute(sa.select(repository.Items.c.path)).all():
            mapped = os.path.normcase(os.path.realpath(row.path))
            folders = {os.path.dirname(mapped)}
            if absolute_folder:
                folders.add(absolute_folder)
            elif subfolder == 'relative' and custom_folder:
                folders.add(os.path.normcase(os.path.realpath(os.path.join(os.path.dirname(mapped),
                                                                         custom_folder))))
            stem = _normalized_media_stem(mapped)
            for folder in folders:
                owners.setdefault((folder, stem), set()).add(mapped)
        return owners


def list_external_subtitles(path, previous=None, ownership_index=None):
    """``[[language, subtitle path, size], ...]`` beside ``path``, or None when cache recovery is incomplete.

    The caller holds the subtitle write locks. A scan shares ``ownership_index`` across its items; a call
    without one builds its own source index."""
    if ownership_index is None:
        ownership_index = SourceOutputOwnerIndex()
    actual = []
    dest_folder = get_subtitle_destination_folder()
    core.CUSTOM_PATHS = [dest_folder] if dest_folder else []

    previously_indexed = []
    rejected_paths = set()
    for entry in previous or []:
        if len(entry) != 3 or not entry[1]:
            continue
        if not _can_borrow_language(entry[0]):
            rejected_paths.add(os.path.normcase(os.path.abspath(entry[1])))
        elif os.path.isfile(entry[1]) and os.stat(entry[1]).st_size == entry[2]:
            previously_indexed.append(entry)
    subtitles = search_external_subtitles(path, languages=get_language_set(),
                                          only_one=settings.general.single_language)
    full_dest_folder_path = os.path.dirname(path)
    if dest_folder:
        if settings.general.subfolder == "absolute":
            full_dest_folder_path = dest_folder
        elif settings.general.subfolder == "relative":
            full_dest_folder_path = os.path.join(os.path.dirname(path), dest_folder)
    subtitles = add_sync_engine_outputs(full_dest_folder_path, subtitles, video_path=path,
                                        ownership_index=ownership_index)
    subtitles = add_combined_outputs(full_dest_folder_path, subtitles, video_filename=os.path.basename(path))
    subtitles = guess_external_subtitles(full_dest_folder_path, subtitles, "movie", previously_indexed,
                                         path_reverse=lambda subtitle_path: subtitle_path)

    for subtitle, language in subtitles.items():
        subtitle_path = get_external_subtitles_path(path, subtitle)
        valid_language = False
        if language:
            if hasattr(language, 'alpha3'):
                valid_language = alpha2_from_alpha3(language.alpha3)
        if not valid_language:
            if subtitle_path and os.path.normcase(os.path.abspath(subtitle_path)) in rejected_paths:
                logging.warning('BAZARR unable to reclassify a cached subtitle for this file %s', path)
                return None
            logging.debug('BAZARR unable to define a supported subtitle language: %s', subtitle)
            continue

        try:
            subtitle_size = os.stat(subtitle_path).st_size
        except FileNotFoundError:
            logging.debug("BAZARR skipping missing subtitle file: %s", subtitle_path)
            continue

        custom = CustomLanguage.found_external(subtitle, subtitle_path)
        if custom is not None:
            actual.append([custom, subtitle_path, subtitle_size])
        elif str(language.basename) != 'und':
            if language.forced:
                language_str = f'{language}:forced'
            elif language.hi:
                language_str = f'{language}:hi'
            else:
                language_str = str(language)
            language_str = subtitle_language_with_sync_modifier(language_str, subtitle)
            language_str = subtitle_language_with_combined_modifier(language_str, subtitle)
            actual.append([language_str, subtitle_path, subtitle_size])
    return actual


def compute_missing_subtitles(profile, cutoff_items, subtitles, failed_attempts=None,
                              matches_audio=lambda language: False, use_embedded_subs=None):
    """The languages a profile still wants, by the rules the movie indexer applies.

    ``profile`` is the language profile as ``get_profiles_list`` returns it, ``cutoff_items`` what
    ``get_profile_cutoff`` returns, ``subtitles`` the listing (``[[language, path, size], ...]``).
    ``matches_audio`` says whether a profile item's language is spoken in the file: with the audio unknown
    it is False, which skips an "only if the audio matches" language and keeps an "exclude if the audio matches"
    one, as it does for an arr movie without audio information.
    """
    if not profile:
        return []
    if use_embedded_subs is None:
        use_embedded_subs = settings.general.use_embedded_subs

    desired = []
    for language in profile.get('items') or []:
        if language.get('audio_exclude') == "True" and matches_audio(language):
            continue
        if language.get('audio_only_include') == "True" and not matches_audio(language):
            continue
        desired.append({'language': language['language'], 'forced': language['forced'], 'hi': language['hi']})

    present = []
    for entry in (subtitles if use_embedded_subs else [x for x in subtitles if x[1]]):
        parts = str(entry[0]).split(':')
        # A combined artifact is a bilingual stacked file, not a standalone subtitle of its base language.
        if any(part.startswith('combined-') for part in parts[1:]):
            continue
        forced = hi = False
        if len(parts) > 1:
            if parts[1] == 'forced':
                forced = True
            elif parts[1] == 'hi':
                hi = True
        present.append({'language': parts[0], 'forced': str(forced), 'hi': str(hi)})

    for cutoff in cutoff_items or []:
        wanted = {'language': cutoff['language'], 'forced': cutoff['forced'], 'hi': cutoff['hi']}
        if cutoff.get('audio_only_include') == 'True' and not matches_audio(cutoff):
            continue
        if cutoff.get('audio_exclude') == 'True' and matches_audio(cutoff):
            return []
        if wanted in present:
            return []
        # HI is considered as good as normal.
        if {'language': wanted['language'], 'forced': 'False', 'hi': 'True'} in present:
            return []

    missing = [item for item in desired if item not in present]
    # A missing language that has a hearing-impaired subtitle on disk is not missing.
    for item in present:
        if item['hi'] == 'True':
            try:
                missing.remove({'language': item['language'], 'forced': 'False', 'hi': 'False'})
            except ValueError:
                pass

    result = []
    for item in missing:
        if is_search_given_up(item['language'], failed_attempts):
            continue
        code = item['language']
        if item['forced'] == 'True':
            code += ':forced'
        elif item['hi'] == 'True':
            code += ':hi'
        result.append(code)
    return result


def effective_profile_id(item, group=None, library=None):
    """An item's own profile wins, then its group's, then the library's. None means it is never searched."""
    for row in (item, group, library):
        if row is not None and row.profile_id is not None:
            return row.profile_id
    return None


def _missing(item, listing, audio_language, target):
    group = repository.get_group(item.group_id, target) if item.group_id is not None else None
    library = repository.get_library(item.library_id, target)
    profile_id = effective_profile_id(item, group, library)
    if profile_id is None:
        return []
    audio_codes = {entry['code2'] for entry in get_audio_profile_languages(audio_language)}
    return compute_missing_subtitles(
        get_profiles_list(profile_id=profile_id), get_profile_cutoff(profile_id=profile_id), listing,
        item.failed_attempts, matches_audio=lambda language: language['language'] in audio_codes)


def _embedded_listing(data):
    if not settings.general.use_embedded_subs:
        return []
    ignored = {
        'pgs': settings.general.ignore_pgs_subs,
        'vobsub': settings.general.ignore_vobsub_subs,
        'ass': settings.general.ignore_ass_subs,
    }
    listing = []
    for language, forced, hi, codec, *title in embedded_subtitles_from_metadata(data):
        code = alpha2_from_alpha3(language)
        if code and not ignored.get(normalize_codec(codec)):
            code = normalize_subtitle_language_variant(code, forced=forced, hi=hi)
            listing.append(embedded_entry(code, codec, *title))
    return listing


def _file_version(item):
    return item.path, item.file_dev, item.file_ino, item.file_size, item.file_mtime_ns


def _file_signature(stat):
    if stat is None:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _can_borrow_language(key):
    if not isinstance(key, str) or not key:
        return False
    modifiers = key.lower().split(':')[1:]
    normalized = normalize_subtitle_language_variant(key).split(':')[1:]
    if sorted(modifiers) != sorted(normalized) or any(
            part.startswith('sync-') and part not in SYNC_ENGINE_LANGUAGE_MODIFIERS for part in modifiers):
        return False
    try:
        # The shared classifier parses the full key and handles custom aliases differently from IETF codes.
        return _get_lang_from_str(key).alpha3 != 'und'
    except (LanguageError, ValueError):
        return False


def index_item(item_id, target=None, ownership_index=None, *, keep_index_on_probe_failure=False, expected_path=None):
    """Analyze a source file and return its subtitle listing, or None if the index is unavailable."""
    item = repository.get_item(item_id, target)
    if item is None:
        logging.info('BAZARR source item %s no longer exists; its subtitles were not indexed', item_id)
        return None
    if expected_path is not None and item.path != expected_path:
        logging.debug('BAZARR source item %s moved before its subtitles were indexed', item_id)
        return None
    stat = local_stat(item.path)
    if stat is None:
        logging.debug("BAZARR this file doesn't seem to exist or isn't accessible: %s", item.path)
        return None

    data, blob = probe_item(item, stat, target=target)
    destination = os.path.join(get_target_folder(item.path, create=False) or os.path.dirname(item.path), '.destination')
    with subtitle_write_locks(item.path, destination):
        if _file_signature(local_stat(item.path)) != _file_signature(stat):
            logging.warning('BAZARR source file changed during analysis: %s', item.path)
            return None
        # Probe before taking the lock. Hold the lock from reading sidecars through
        # committing their listing, so an older scan cannot erase a newly saved output.
        latest = repository.get_item(item_id, target)
        if latest is None or _file_version(latest) != _file_version(item):
            return None
        try:
            previous = ast.literal_eval(latest.subtitles) if latest.subtitles else []
        except (SyntaxError, ValueError):
            logging.exception('BAZARR invalid stored subtitle listing for this file %s', item.path)
            return None
        if not isinstance(previous, (list, tuple)) or any(
                not isinstance(entry, (list, tuple))
                or (len(entry) == 3 and (entry[0] is not None and not isinstance(entry[0], str)
                                         or entry[1] is not None and not isinstance(entry[1], str)))
                for entry in previous):
            logging.warning('BAZARR invalid stored subtitle listing for this file %s', item.path)
            return None
        if (keep_index_on_probe_failure and blob is None
                and all(len(entry) != 3 or not entry[1] or _can_borrow_language(entry[0]) for entry in previous)
                and latest.subtitles is not None and latest.missing_subtitles is not None
                and index_matches_file(latest, stat, allow_external_only=True)):
            return previous
        try:
            external = list_external_subtitles(item.path, previous, ownership_index)
        except (OSError, UnicodeDecodeError):
            logging.exception('BAZARR unable to index external subtitles for this file %s', item.path)
            return None
        if external is None:
            return None
        if _file_signature(local_stat(item.path)) != _file_signature(stat):
            logging.warning('BAZARR source file changed while reading its subtitles: %s', item.path)
            return None

        with source_transaction(target) as connection:
            current = repository.get_item(item_id, connection, for_update=True)
            if current is None or _file_version(current) != _file_version(item):
                logging.warning('BAZARR source item %s changed during indexing', item_id)
                return None
            if blob is None and current.ffprobe_cache != item.ffprobe_cache:
                data = cached_metadata(current, stat)
            listing = _embedded_listing(data) + external
            values = {}
            audio_language = current.audio_language
            if blob is not None:
                parsed = streams(data)
                video_track = (parsed.get('video') or [{}])[0]
                audio_track = (parsed.get('audio') or [{}])[0]
                audio_language = str(audio_languages_from_metadata(data, current.path))
                values = dict(ffprobe_cache=blob, audio_language=audio_language, audio_codec=audio_track.get('codec'),
                              video_codec=video_track.get('codec'), resolution=video_track.get('resolution'))
            elif not streams(data):
                values = failed_probe_values(current, stat)
                audio_language = values.get('audio_language', audio_language)
            missing = _missing(current, listing, audio_language, connection)
            if repository.write_item_index(item_id, str(listing), str(missing), connection, **values):
                return listing
    return None


def _recoverable_index_database_error(error):
    """Recognize operational failures without hiding SQL defects reported as OperationalError by SQLite."""
    if isinstance(error, (sa.exc.ProgrammingError, sa.exc.IntegrityError)):
        return False
    original = error.orig
    sqlite_code = getattr(original, 'sqlite_errorcode', None)
    if sqlite_code is not None:
        return sqlite_code & 0xff in {
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_IOERR,
            sqlite3.SQLITE_FULL, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PROTOCOL,
        }
    sqlstate = getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)
    if sqlstate is not None:
        if sqlstate == '57014':
            return False  # Query cancellation must propagate, even when its connection was invalidated.
        return sqlstate[:2] in {'08', '40', '53'} or sqlstate in {
            '25P02', '55P03', '57P01', '57P02', '57P03',
        }
    # Without a driver code, recognize only SQLite's exact lock messages.
    if isinstance(original, sqlite3.Error):
        return isinstance(original, sqlite3.OperationalError) and str(original) in {
            'database is locked', 'database table is locked', 'database schema is locked',
        }
    return error.connection_invalidated


def index_saved_subtitle(item_id, *, expected_path):
    """Index after a save without letting recoverable failures discard the saved result or its history."""
    try:
        # Keep repository and ownership reads in the same isolated session. The index write has
        # its own transaction, which rolls back before a failure reaches this boundary.
        with Session(bind=handle().get_bind()) as session:
            listing = index_item(item_id, target=session, ownership_index=SourceOutputOwnerIndex(session),
                                 expected_path=expected_path)
    except (sa.exc.DBAPIError, OSError) as error:
        if isinstance(error, sa.exc.DBAPIError):
            if not _recoverable_index_database_error(error):
                raise
            # Shared helpers may have invalidated the application's scoped session. Roll back this thread's
            # logical transaction as well, so a retry can acquire a new connection in AUTOCOMMIT mode.
            handle().rollback()
        logging.exception('BAZARR source item %s subtitle index failed after saving; the subtitle was retained',
                          item_id)
        return False
    if listing is None:
        logging.warning('BAZARR source item %s subtitle index is incomplete after saving; the subtitle was retained',
                        item_id)
    return listing is not None


def recalculate_missing_subtitles(*, library_id=None, group_id=None, cancel=None):
    """Re-evaluate profile and settings rules against stored tracks, without a folder walk or probe."""
    items = repository.Items
    scope = []
    if library_id is not None:
        scope.append(items.c.library_id == library_id)
    if group_id is not None:
        scope.append(items.c.group_id == group_id)
    item_ids = handle().execute(sa.select(items.c.id).where(*scope, items.c.subtitles.is_not(None))).scalars().all()
    for item_id in item_ids:
        if cancel is not None:
            cancel()
        with source_transaction() as connection:
            item = repository.get_item(item_id, connection, for_update=True)
            if item is None or item.subtitles is None:
                continue
            if ((library_id is not None and library_id != item.library_id)
                    or (group_id is not None and group_id != item.group_id)):
                continue
            external = [entry for entry in ast.literal_eval(item.subtitles) if entry[1]]
            data = read_metadata(item.ffprobe_cache)
            embedded = []
            if settings.general.use_embedded_subs:
                embedded = _embedded_listing(data) if data else partition_embedded_entries(item.subtitles)[0]
            listing = embedded + external
            subtitles = str(listing)
            missing = str(_missing(item, listing, item.audio_language, connection))
            if (subtitles, missing) != (item.subtitles, item.missing_subtitles):
                repository.write_item_index(item_id, subtitles, missing, connection, indexed=False)

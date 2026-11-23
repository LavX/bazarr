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
import unicodedata
from contextlib import contextmanager
from functools import partial
from time import sleep

import sqlalchemy as sa
from babelfish.exceptions import LanguageError
from sqlalchemy.orm import Session

from subliminal_patch import core

from app.config import settings
from app.database import get_audio_profile_languages, get_profile_cutoff, get_profiles_list
from constants import MAXIMUM_SUBTITLE_SIZE
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha2_from_alpha3, get_language_set
from subtitles.adaptive_searching import is_search_given_up
from subtitles.indexer.entries import embedded_entry, normalize_codec
from subtitles.indexer.utils import (_get_lang_from_str, add_combined_outputs, add_sync_engine_outputs,
                                     guess_external_subtitles,
                                     normalize_subtitle_language_variant, partition_embedded_entries,
                                     subtitle_language_with_combined_modifier,
                                     subtitle_language_with_sync_modifier)
from subtitles.tools.combine.naming import parse_combined_filename
from subtitles.tools.combine.rules import _dotted_custom_aliases
from subtitles.tools.subsync_engines import (SYNC_ENGINES, SYNC_ENGINE_LANGUAGE_MODIFIERS, SubtitleDestinationChanged,
                                             SubtitleSourceChanged, SyncOutputOwnerIndex, _normalized_media_stem, engine_output_path,
                                             read_regular_subtitle, regular_subtitle_version, subtitle_write_locks,
                                             sync_engine_from_output_path, sync_output_owner_is_unique)
from utilities.helper import get_subtitle_destination_folder, get_target_folder
from utilities.video_analyzer import audio_languages_from_metadata, embedded_subtitles_from_metadata

from . import identity, repository
from .analysis import (cached_metadata, failed_probe_values, index_matches_file, local_stat, probe_item,
                       read_metadata, streams)
from .context import resolve_same_item, source_item_publication
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

    def conflicting_stems(self, video, subtitle_path):
        """Include media whose stem consumes only part of the subtitle suffix."""
        stems = super().conflicting_stems(video, subtitle_path)
        stem = _normalized_media_stem(subtitle_path)
        if sync_engine_from_output_path(subtitle_path):
            stem = stem.rsplit('.', 1)[0]
            stems.add(stem)
            subtitle_path = stem + os.path.splitext(subtitle_path)[1]
        base, _, tail = stem.rpartition('.')
        modifier = f'.{tail}' if tail in core.EXTERNAL_SUBTITLE_MODIFIERS else ''
        if modifier or parse_combined_filename(subtitle_path):
            stems.add(base)
            stem = base
        stems.add(core.external_subtitle_base(stem))
        base, _, tag = stem.rpartition('.')
        if base and CustomLanguage.found_external(f'.{tag}{modifier}.srt', None):
            stems.add(base)
        for alias in _dotted_custom_aliases():
            if stem.endswith('.' + alias):
                stems.add(stem[:-len(alias) - 1])
        return stems

    @staticmethod
    def _source_owner_paths(target):
        return tuple(handle(target).execute(sa.select(repository.Items.c.path).order_by(repository.Items.c.path)).scalars())

    def _load_owners(self):
        return self._map_source_owners(self._source_owner_paths(self._target))

    @staticmethod
    def _map_source_owners(paths):
        custom_folder = get_subtitle_destination_folder()
        subfolder = settings.general.subfolder
        absolute_folder = os.path.normcase(os.path.realpath(custom_folder)) if subfolder == 'absolute' else None
        owners = {}
        for path in paths:
            mapped = os.path.normcase(os.path.realpath(path))
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


class SourcePublicationOwnersChanged(SubtitleDestinationChanged):
    """The prepared shared ownership could not be pinned for publication."""


def _owner_stem_filter(path, stems, connection):
    """Filter candidate mapped basenames before resolving their directories on disk."""
    if connection.dialect.name == 'sqlite':
        return sa.func.source_subtitle_stem(path).in_(stems)

    if os.name == 'nt':
        path = sa.func.replace(path, '\\', '/')
    filename = sa.func.regexp_replace(path, '^.*/', '')
    stem = sa.func.regexp_replace(filename, r'(^\.*[^.].*)\.[^.]*$', r'\1')
    non_ascii = r'[^\x01-\x7f]'
    if connection.connection.driver_connection.get_parameter_status('server_encoding') != 'UTF8':
        return sa.or_(sa.func.lower(stem.collate('C')).in_(stems), stem.regexp_match(non_ascii))

    # PostgreSQL's lowercase rules depend on its collation. Match the ASCII
    # letters of the decomposed name and leave other characters to Python.
    decomposed = sa.func.normalize(stem, sa.literal_column('NFD')).collate('C')
    shape = sa.func.regexp_replace(sa.func.lower(decomposed), non_ascii, '?', 'g')
    shapes = {''.join(char if char.isascii() else '?' for char in unicodedata.normalize('NFD', value))
              for value in stems}
    return shape.in_(shapes)


def _upgrade_arr_owner_paths(connection, stems):
    from app.database import TableArrInstances, TableEpisodes, TableMovies, TableSportsEvents
    from utilities.path_mappings import _instance_path_mapping, path_mappings, read_sports_mappings

    instances = connection.execute(sa.select(TableArrInstances.id, TableArrInstances.kind,
                                             TableArrInstances.path_mappings)).all()
    for table, media_type, fallback in ((TableMovies, 'movie', path_mappings.path_mapping_movies),
                                        (TableEpisodes, 'episode', path_mappings.path_mapping_series)):
        mappings = []
        for instance in instances:
            if not instance.path_mappings:
                continue
            rules = _instance_path_mapping(instance.id, media_type, target=connection)
            if rules is not None and rules != fallback:
                mappings.append((table.arr_instance_id == instance.id, identity._mapped_sql_path(table.path, rules)))
        default = identity._mapped_sql_path(table.path, fallback)
        mapped = sa.case(*mappings, else_=default) if mappings else default
        yield from connection.scalars(sa.select(mapped).where(table.path.is_not(None),
                                                             _owner_stem_filter(mapped, stems, connection)))

    for instance in instances:
        if instance.kind == 'sportarr':
            table = TableSportsEvents
            mapped = identity._mapped_sports_sql_path(table.path, read_sports_mappings(instance.path_mappings))
            yield from connection.scalars(sa.select(mapped).where(table.arr_instance_id == instance.id,
                                                                 table.path.is_not(None),
                                                                 _owner_stem_filter(mapped, stems, connection)))


class SourcePublicationOwnerIndex(SourceOutputOwnerIndex):
    """Source writes in shared subtitle folders must account for all media stored there."""

    def __init__(self):
        super().__init__()
        self._prepared_paths = None

    def _load_owners(self):
        self._prepared_paths = self._source_owner_paths(None)
        owners = self._map_source_owners(self._prepared_paths)
        for key, paths in SyncOutputOwnerIndex._load_owners().items():
            owners.setdefault(key, set()).update(paths)
        return owners

    def folder_is_shared(self, directory):
        # A relative custom path can also resolve into another video's directory.
        return bool(get_subtitle_destination_folder())

    @staticmethod
    def _needs_registry_guard(source, destination, custom_folder):
        # Another media directory can resolve its relative subtitle folder to
        # this directory, even before the first video there is admitted.
        if custom_folder:
            return True
        if destination is None:
            return False
        media_directory = os.path.normcase(os.path.realpath(os.path.dirname(source.path)))
        directory = os.path.normcase(os.path.realpath(os.path.dirname(destination)))
        return directory != media_directory

    def _recheck_upgrade_owners(self, current, paths, connection):
        if connection.dialect.name == 'postgresql':
            # Ordinary INSERT/UPDATE/DELETE take ROW EXCLUSIVE, including new
            # owners with no row yet to lock. SHARE still allows readers.
            connection.exec_driver_sql('LOCK TABLE arr_instances, table_episodes, table_movies, '
                                       'table_sports_events IN SHARE MODE NOWAIT')
        else:
            connection.connection.driver_connection.create_function(
                'source_subtitle_stem', 1, lambda path: _normalized_media_stem(path) if path else None,
                deterministic=True)
        paths = tuple(dict.fromkeys(paths))
        generated = (str(engine_output_path(path, engine)) for path in paths
                     if not sync_engine_from_output_path(path) for engine in SYNC_ENGINES)
        stems = set().union(*(self.conflicting_stems(current.path, path) for path in (*paths, *generated)))
        column = repository.Items.c.path
        source_paths = connection.scalars(sa.select(column).where(_owner_stem_filter(column, stems, connection)))
        candidates = (*source_paths, *_upgrade_arr_owner_paths(connection, stems))
        self._owners = self._map_source_owners(path for path in candidates
                                              if path and _normalized_media_stem(path) in stems)
        self._loaded = True
        for path in paths:
            if not sync_output_owner_is_unique(current.path, path, ownership_index=self):
                raise SourcePublicationOwnersChanged('The source upgrade subtitle does not have one media owner')

    @contextmanager
    def publication(self, source, prepare=None, *, destination=None, recheck_paths=()):
        """Prepare discovery before locking, then recheck requested upgrade paths during publication."""
        for _ in range(2):
            self._loaded = False
            self._owners = {}
            self._prepared_paths = None
            destination_setting = settings.general.subfolder, get_subtitle_destination_folder()
            prepared = prepare() if prepare is not None else None
            shared = self._needs_registry_guard(source, destination, destination_setting[1])
            if shared and self._prepared_paths is None:
                raise SourcePublicationOwnersChanged('The shared subtitle owners could not be verified')
            # A scanner with the conflicting lock finishes outside this transaction.
            # Retrying after rollback leaves it free to finish any remaining row writes.
            for delay in (0, 0.025, 0.05, 0.10, 0.20, 0.40, 0.40):
                if delay:
                    sleep(delay)
                publishing = False
                try:
                    with source_item_publication(source, shared=shared) as (current, connection):
                        if destination_setting != (settings.general.subfolder, get_subtitle_destination_folder()):
                            break
                        if shared and current is not None:
                            if recheck_paths:
                                self._recheck_upgrade_owners(current, recheck_paths, connection)
                            elif self._prepared_paths != self._source_owner_paths(connection):
                                break
                        if current is not None:
                            state = repository.item_state(current.item_id, connection)
                            if (state is None or not state.source_enabled or not state.library_enabled
                                    or not os.path.isfile(current.path)):
                                current = None
                        publishing = True
                        yield current, connection, prepared
                        return
                except sa.exc.OperationalError as error:
                    sqlstate = getattr(error.orig, 'sqlstate', None) or getattr(error.orig, 'pgcode', None)
                    if publishing or sqlstate != '55P03':
                        raise
            else:
                raise SourcePublicationOwnersChanged('The shared subtitle owners are being updated')
        raise SourcePublicationOwnersChanged('The shared subtitle owners changed while preparing publication')


def list_external_subtitles(path, previous=None, ownership_index=None, *, publication=False, written_paths=()):
    """List sidecars beside ``path``. An unrecovered cached language leaves the index incomplete.

    The caller holds the subtitle write locks. Publication checks fresh ownership before counting a sidecar.
    Previously indexed paths supply language guesses for files discovered in this scan.
    """
    if ownership_index is None:
        ownership_index = SourceOutputOwnerIndex()
    media_directory, filename = os.path.split(path)
    target = get_target_folder(path, create=False) or media_directory
    directories = dict.fromkeys((media_directory, target))
    excluded = {os.path.normcase(os.path.abspath(subtitle_path)) for subtitle_path in written_paths}
    indexed, rejected_paths = {}, set()
    for entry in previous or []:
        if len(entry) != 3 or not entry[1]:
            continue
        key = os.path.normcase(os.path.abspath(entry[1]))
        if _can_borrow_language(entry[0]):
            indexed[key] = entry
        else:
            rejected_paths.add(key)
    languages = get_language_set() if settings.general.single_language else None
    actual = {}
    for directory in directories:
        try:
            found = core.search_external_subtitles_in_directory(
                os.path.join(directory, filename), languages=languages, only_one=bool(languages))
            found = add_sync_engine_outputs(directory, found, video_path=path, ownership_index=ownership_index)
            found = add_combined_outputs(directory, found, video_filename=filename)
        except (FileNotFoundError, NotADirectoryError):
            continue
        for subtitle, language in found.items():
            subtitle_path = os.path.join(directory, subtitle)
            path_key = os.path.normcase(os.path.abspath(subtitle_path))
            if path_key in actual or path_key in excluded:
                continue
            version = regular_subtitle_version(subtitle_path)
            if version is None:
                continue
            if publication and not sync_output_owner_is_unique(path, subtitle_path, ownership_index=ownership_index):
                continue
            if version[2] <= MAXIMUM_SUBTITLE_SIZE:
                try:
                    read_sample = partial(read_regular_subtitle, expected_version=version,
                                          max_bytes=MAXIMUM_SUBTITLE_SIZE)
                    previous_entry = indexed.get(path_key)
                    language = guess_external_subtitles(
                        directory, {subtitle: language}, 'movie', (previous_entry,) if previous_entry else None,
                        path_reverse=os.fspath, read_bytes=read_sample)[subtitle]
                except SubtitleSourceChanged:
                    continue
                except (OSError, UnicodeError):
                    if not publication:
                        raise
                    continue
            entry = _external_subtitle_entry(subtitle, language, subtitle_path, version[2])
            if entry is not None:
                actual[path_key] = entry
            elif not publication and path_key in rejected_paths:
                logging.warning('BAZARR unable to reclassify a cached subtitle for this file %s', path)
                return None
    return list(actual.values())


def _external_subtitle_entry(subtitle, language, subtitle_path, size):
    if not hasattr(language, 'alpha3') or not alpha2_from_alpha3(language.alpha3):
        return None
    custom = CustomLanguage.found_external(subtitle, subtitle_path)
    if custom is not None:
        language_str = normalize_subtitle_language_variant(custom)
    elif str(language.basename) == 'und':
        return None
    else:
        language_str = normalize_subtitle_language_variant(
            language.basename, forced=language.forced, hi=language.hi and not language.forced)
    language_str = subtitle_language_with_sync_modifier(language_str, subtitle)
    language_str = subtitle_language_with_combined_modifier(language_str, subtitle)
    return [language_str, subtitle_path, size]


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
        parts = str(entry[0]).lower().split(':')
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
    return (item.created_at, item.source_id, item.library_id, item.path,
            item.file_dev, item.file_ino, item.file_size, item.file_mtime_ns)


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
        language = _get_lang_from_str(key)
        return language.alpha3 != 'und' and bool(alpha2_from_alpha3(language.alpha3))
    except (LanguageError, ValueError):
        return False


def index_item(item_id, target=None, ownership_index=None, *, keep_index_on_probe_failure=False, expected_path=None,
               expected_source=None):
    """Analyze a source file and return its subtitle listing, or None if the index is unavailable."""
    item = repository.get_item(item_id, target)
    if item is None:
        logging.info('BAZARR source item %s no longer exists; its subtitles were not indexed', item_id)
        return None
    if expected_source is not None and (not expected_source.same_item(item)
                                       or item.source_id != expected_source.source_id
                                       or item.library_id != expected_source.library_id):
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


def index_saved_subtitle(item_id, *, expected_path, expected_source=None):
    """Index after a save without letting recoverable failures discard the saved result or its history."""
    try:
        # Keep repository and ownership reads in the same isolated session. The index write has
        # its own transaction, which rolls back before a failure reaches this boundary.
        with Session(bind=handle().get_bind()) as session:
            listing = index_item(item_id, target=session, ownership_index=SourceOutputOwnerIndex(session),
                                 expected_path=expected_path, expected_source=expected_source)
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


def index_and_notify_item(source):
    """Refresh a source item's subtitles and the views that show it after a save."""
    from app.event_handler import event_stream

    indexed = index_saved_subtitle(source.item_id, expected_path=source.path, expected_source=source)
    current = resolve_same_item(source) if indexed else None
    if current is not None:
        event_stream(type='source-item', payload=current.item_id)
        if current.group_id is not None:
            event_stream(type='source-group', payload=current.group_id)
    return indexed


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

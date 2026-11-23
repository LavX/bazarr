# coding=utf-8
"""Listing the subtitles beside a source item and working out which languages it still lacks.

The listing uses the shared helpers the movie indexer uses, with paths taken as stored: a source item's path is
already the Bazarr-visible one, so nothing is mapped in either direction. The source item's own probe cache
supplies embedded tracks and audio languages for the profile rules.
"""
import ast
import logging
import os
from functools import partial

import sqlalchemy as sa

from subliminal_patch import core, search_external_subtitles

from app.config import settings
from app.database import get_audio_profile_languages, get_profile_cutoff, get_profiles_list
from constants import MAXIMUM_SUBTITLE_SIZE
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha2_from_alpha3, get_language_set
from subtitles.adaptive_searching import is_search_given_up
from subtitles.indexer.entries import embedded_entry, normalize_codec
from subtitles.indexer.utils import (add_combined_outputs, add_sync_engine_outputs, get_external_subtitles_path,
                                     guess_external_subtitles, normalize_subtitle_language_variant,
                                     partition_embedded_entries, subtitle_language_with_combined_modifier,
                                     subtitle_language_with_sync_modifier)
from subtitles.tools.combine.naming import parse_combined_filename
from subtitles.tools.combine.rules import _dotted_custom_aliases
from subtitles.tools.subsync_engines import (SubtitleSourceChanged, SyncOutputOwnerIndex, _normalized_media_stem,
                                             read_regular_subtitle, regular_subtitle_version, subtitle_write_locks,
                                             sync_engine_from_output_path, sync_output_owner_is_unique)
from utilities.helper import get_subtitle_destination_folder, get_target_folder
from utilities.video_analyzer import audio_languages_from_metadata, embedded_subtitles_from_metadata

from . import repository
from .analysis import cached_metadata, failed_probe_values, local_stat, probe_item, read_metadata, streams
from .db import handle, source_transaction


class SourceOutputOwnerIndex(SyncOutputOwnerIndex):
    """Who owns a generated subtitle in a shared subtitle folder, answered from the source tables only.

    The base index reads the arr and Sportarr tables, which a source never does. Here the owners are the source
    items, each claiming its own folder and, with a custom subtitle folder, that folder too.
    """

    def conflicting_stems(self, video, subtitle_path):
        """Include media whose stem consumes only part of the subtitle suffix."""
        stems = super().conflicting_stems(video, subtitle_path)
        stem = _normalized_media_stem(subtitle_path)
        if sync_engine_from_output_path(subtitle_path):
            stem = stem.rsplit('.', 1)[0]
            stems.add(stem)
            subtitle_path = stem + os.path.splitext(subtitle_path)[1]
        base, _, tail = stem.rpartition('.')
        if tail in core.EXTERNAL_SUBTITLE_MODIFIERS or parse_combined_filename(subtitle_path):
            stems.add(base)
            stem = base
        stems.add(core.external_subtitle_base(stem))
        for alias in _dotted_custom_aliases():
            if stem.endswith('.' + alias):
                stems.add(stem[:-len(alias) - 1])
        return stems

    @staticmethod
    def _load_owners():
        custom_folder = get_subtitle_destination_folder()
        subfolder = settings.general.subfolder
        absolute_folder = os.path.normcase(os.path.realpath(custom_folder)) if subfolder == 'absolute' else None
        owners = {}
        for row in handle().execute(sa.select(repository.Items.c.path)).all():
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


class SourcePublicationOwnerIndex(SourceOutputOwnerIndex):
    """Source writes in shared subtitle folders must account for all media stored there."""

    @staticmethod
    def _load_owners():
        owners = SourceOutputOwnerIndex._load_owners()
        for key, paths in SyncOutputOwnerIndex._load_owners().items():
            owners.setdefault(key, set()).update(paths)
        return owners

    def folder_is_shared(self, directory):
        # A relative custom path can also resolve into another video's directory.
        return bool(get_subtitle_destination_folder())


def list_external_subtitles(path, previous=None, ownership_index=None):
    """``[[language, subtitle path, size], ...]`` for the subtitle files beside ``path``.

    The caller holds the subtitle write locks. A scan shares ``ownership_index`` across its items; a call
    without one builds its own source index."""
    if ownership_index is None:
        ownership_index = SourceOutputOwnerIndex()
    actual = []
    dest_folder = get_subtitle_destination_folder()
    core.CUSTOM_PATHS = [dest_folder] if dest_folder else []

    previously_indexed = [entry for entry in (previous or [])
                          if len(entry) == 3 and entry[1] and os.path.isfile(entry[1])
                          and os.stat(entry[1]).st_size == entry[2]]
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
        if language is None:
            continue
        subtitle_path = get_external_subtitles_path(path, subtitle)
        if subtitle_path is None:
            continue
        try:
            subtitle_size = os.stat(subtitle_path).st_size
        except FileNotFoundError:
            logging.debug("BAZARR skipping missing subtitle file: %s", subtitle_path)
            continue

        entry = _external_subtitle_entry(subtitle, language, subtitle_path, subtitle_size)
        if entry is not None:
            actual.append(entry)
    return actual


def _external_subtitle_entry(subtitle, language, subtitle_path, size):
    if not hasattr(language, 'alpha3') or not alpha2_from_alpha3(language.alpha3):
        return None
    custom = CustomLanguage.found_external(subtitle, subtitle_path)
    if custom is not None:
        return [custom, subtitle_path, size]
    if str(language.basename) == 'und':
        return None
    language_str = f'{language}:forced' if language.forced else f'{language}:hi' if language.hi else str(language)
    language_str = subtitle_language_with_sync_modifier(language_str, subtitle)
    language_str = subtitle_language_with_combined_modifier(language_str, subtitle)
    return [language_str, subtitle_path, size]


def list_current_external_subtitles(video_path, indexed, ownership_index, written_paths=()):
    """Check the two subtitle folders for new sidecars during provider publication."""
    def unique(path):
        return sync_output_owner_is_unique(video_path, path, ownership_index=ownership_index)

    media_directory, filename = os.path.split(video_path)
    target = get_target_folder(video_path, create=False) or media_directory
    directories = dict.fromkeys(os.path.abspath(path) for path in (media_directory, target))
    excluded = {os.path.abspath(path) for path in written_paths}
    current = {}
    for entry in indexed:
        if entry[1]:
            path = os.path.abspath(entry[1])
            if (path not in excluded and os.path.dirname(path) in directories
                    and regular_subtitle_version(path) is not None and unique(path)):
                current[path] = entry
    languages = get_language_set() if settings.general.single_language else None
    for directory in directories:
        try:
            found = core.search_external_subtitles_in_directory(
                os.path.join(directory, filename), languages=languages, only_one=bool(languages))
            found = add_sync_engine_outputs(directory, found, video_path=video_path, ownership_index=ownership_index)
        except FileNotFoundError:
            continue
        for subtitle, language in found.items():
            path = os.path.abspath(os.path.join(directory, subtitle))
            if path in current or path in excluded:
                continue
            version = regular_subtitle_version(path)
            if version is None or not unique(path):
                continue
            if language is None and version[2] <= MAXIMUM_SUBTITLE_SIZE:
                try:
                    read_sample = partial(read_regular_subtitle, expected_version=version,
                                          max_bytes=MAXIMUM_SUBTITLE_SIZE)
                    language = guess_external_subtitles(directory, {subtitle: None}, 'movie', path_reverse=os.fspath,
                                                        read_bytes=read_sample)[subtitle]
                except (OSError, UnicodeError, SubtitleSourceChanged):
                    continue
            entry = _external_subtitle_entry(subtitle, language, path, version[2])
            if entry is not None:
                current[path] = entry
    return list(current.values())


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
    if not data or not settings.general.use_embedded_subs:
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


def index_item(item_id, target=None, ownership_index=None):
    """Analyze the source file and rebuild its subtitle listing. None means the index could not be written."""
    item = repository.get_item(item_id, target)
    if item is None:
        logging.info('BAZARR source item %s no longer exists; its subtitles were not indexed', item_id)
        return None
    stat = local_stat(item.path)
    if stat is None:
        logging.debug("BAZARR this file doesn't seem to exist or isn't accessible: %s", item.path)
        return None

    data, blob = probe_item(item, stat)
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
            external = list_external_subtitles(item.path, previous, ownership_index)
        except Exception:
            logging.exception('BAZARR unable to index external subtitles for this file %s', item.path)
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


def index_and_notify_item(source):
    """Refresh a source item's subtitles and the views that show it."""
    from app.event_handler import event_stream

    item = repository.get_item(source.item_id)
    if item is None or not source.same_item(item) or item.path != source.path or item.source_id != source.source_id:
        return
    if index_item(source.item_id) is not None:
        event_stream(type='source-item', payload=source.item_id)
        if source.group_id is not None:
            event_stream(type='source-group', payload=source.group_id)

# coding=utf-8
"""Listing the subtitles beside a source item and working out which languages it still lacks.

The listing uses the shared helpers the movie indexer uses, with paths taken as stored: a source item's path is
already the Bazarr-visible one, so nothing is mapped in either direction. It writes by item id and checks that
exactly one row changed. This covers the external subtitles and the profile rules (cutoff, forced, HI); embedded
tracks and the audio-language rules come with the media analysis.
"""
import ast
import logging
import os
from functools import partial

import sqlalchemy as sa

from subliminal_patch import core

from app.config import settings
from app.database import get_profile_cutoff, get_profiles_list
from constants import MAXIMUM_SUBTITLE_SIZE
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha2_from_alpha3, get_language_set
from subtitles.adaptive_searching import is_search_given_up
from subtitles.indexer.utils import (add_combined_outputs, add_sync_engine_outputs, guess_external_subtitles,
                                     normalize_subtitle_language_variant, subtitle_language_with_combined_modifier,
                                     subtitle_language_with_sync_modifier)
from subtitles.tools.combine.naming import parse_combined_filename
from subtitles.tools.combine.rules import _dotted_custom_aliases
from subtitles.tools.subsync_engines import (SubtitleSourceChanged, SyncOutputOwnerIndex, _normalized_media_stem,
                                             read_regular_subtitle, regular_subtitle_version, subtitle_write_locks,
                                             sync_engine_from_output_path, sync_output_owner_is_unique)
from utilities.helper import get_subtitle_destination_folder, get_target_folder

from . import repository
from .db import handle


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


def list_external_subtitles(path, previous=None, ownership_index=None, *, publication=False, written_paths=()):
    """``[[language, subtitle path, size], ...]`` for the subtitle files beside ``path``.

    Publication checks fresh ownership with its caller's index before counting a sidecar.
    Previously indexed paths supply language guesses for files discovered in this scan."""
    if ownership_index is None:
        ownership_index = SourceOutputOwnerIndex()
    media_directory, filename = os.path.split(path)
    target = get_target_folder(path, create=False) or media_directory
    directories = dict.fromkeys((media_directory, target))
    excluded = {os.path.abspath(subtitle_path) for subtitle_path in written_paths}
    indexed = {entry[1]: entry for entry in (previous or []) if len(entry) == 3 and entry[1]}
    languages = get_language_set() if settings.general.single_language else None
    actual = {}
    with subtitle_write_locks(path, os.path.join(target, '.destination')):
        for directory in directories:
            try:
                found = core.search_external_subtitles_in_directory(
                    os.path.join(directory, filename), languages=languages, only_one=bool(languages))
                found = add_sync_engine_outputs(directory, found, video_path=path, ownership_index=ownership_index)
                found = add_combined_outputs(directory, found, video_filename=filename)
            except FileNotFoundError:
                continue
            for subtitle, language in found.items():
                subtitle_path = os.path.join(directory, subtitle)
                if subtitle in actual or os.path.abspath(subtitle_path) in excluded:
                    continue
                version = regular_subtitle_version(subtitle_path)
                if version is None:
                    continue
                if publication and not sync_output_owner_is_unique(path, subtitle_path, ownership_index=ownership_index):
                    continue
                if language is None and version[2] <= MAXIMUM_SUBTITLE_SIZE:
                    try:
                        read_sample = partial(read_regular_subtitle, expected_version=version,
                                              max_bytes=MAXIMUM_SUBTITLE_SIZE)
                        previous_entry = indexed.get(subtitle_path)
                        language = guess_external_subtitles(
                            directory, {subtitle: None}, 'movie', (previous_entry,) if previous_entry else None,
                            path_reverse=os.fspath, read_bytes=read_sample)[subtitle]
                    except (OSError, UnicodeError, SubtitleSourceChanged):
                        continue
                entry = _external_subtitle_entry(subtitle, language, subtitle_path, version[2])
                if entry is not None:
                    actual[subtitle] = entry
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


def index_item(item_id, target=None, ownership_index=None):
    """Rebuild the subtitle listing and the missing languages of one item and store them by id.

    Returns the listing, or None when the item or its file is gone.
    """
    item = repository.get_item(item_id, target)
    if item is None:
        logging.info('BAZARR source item %s no longer exists; its subtitles were not indexed', item_id)
        return None
    if not os.path.exists(item.path):
        logging.debug("BAZARR this file doesn't seem to exist or isn't accessible: %s", item.path)
        return None
    try:
        previous = ast.literal_eval(item.subtitles) if item.subtitles else []
        listing = list_external_subtitles(item.path, previous, ownership_index)
    except Exception:
        logging.exception('BAZARR unable to index external subtitles for this file %s', item.path)
        return None

    group = repository.get_group(item.group_id, target) if item.group_id is not None else None
    library = repository.get_library(item.library_id, target)
    profile_id = effective_profile_id(item, group, library)
    missing_text = '[]'
    if profile_id:
        missing_text = str(compute_missing_subtitles(
            get_profiles_list(profile_id=profile_id), get_profile_cutoff(profile_id=profile_id), listing,
            item.failed_attempts))
    repository.write_item_index(item_id, str(listing), missing_text, target)
    return listing



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

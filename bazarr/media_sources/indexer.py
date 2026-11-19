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

from subliminal_patch import core, search_external_subtitles

from app.config import settings
from app.database import get_profile_cutoff, get_profiles_list
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha2_from_alpha3, get_language_set
from subtitles.adaptive_searching import is_search_given_up
from subtitles.indexer.utils import (add_combined_outputs, add_sync_engine_outputs, get_external_subtitles_path,
                                     guess_external_subtitles, subtitle_language_with_combined_modifier, subtitle_language_with_sync_modifier)
from subtitles.tools.subsync_engines import subtitle_write_locks
from utilities.helper import get_subtitle_destination_folder, get_target_folder

from . import repository


def list_external_subtitles(path, previous=None):
    """``[[language, subtitle path, size], ...]`` for the subtitle files beside ``path``."""
    actual = []
    destination = os.path.join(get_target_folder(path, create=False) or os.path.dirname(path), '.destination')
    with subtitle_write_locks(path, destination):
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
        subtitles = add_sync_engine_outputs(full_dest_folder_path, subtitles, video_path=path)
        subtitles = add_combined_outputs(full_dest_folder_path, subtitles, video_filename=os.path.basename(path))
        subtitles = guess_external_subtitles(full_dest_folder_path, subtitles, "movie", previously_indexed,
                                             path_reverse=lambda subtitle_path: subtitle_path)

    for subtitle, language in subtitles.items():
        valid_language = False
        if language:
            if hasattr(language, 'alpha3'):
                valid_language = alpha2_from_alpha3(language.alpha3)
        else:
            logging.debug("BAZARR skipping subtitles because we are unable to define language: %s", subtitle)
            continue
        if not valid_language:
            logging.debug('BAZARR %s is an unsupported language code.', language.alpha3)
            continue

        subtitle_path = get_external_subtitles_path(path, subtitle)
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


def index_item(item_id, target=None):
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
        listing = list_external_subtitles(item.path, previous)
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

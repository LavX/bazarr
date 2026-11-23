# coding=utf-8
# fmt: off

import os
import sys
import tempfile
import logging
from copy import deepcopy
from functools import partial
from media_servers.events import publication_callback
from arr_instances.resolution import scoped
import subliminal
import ast
from contextlib import ExitStack, contextmanager

from subzero.language import Language
from subliminal_patch.core import is_vobsub_pair, save_subtitles
from subtitles.tools.subsync_engines import (
    SYNC_ENGINES, SubtitleDestinationChanged, SubtitleSourceChanged, _report_subtitle_publication,
    engine_output_path, is_sync_engine_output, regular_subtitle_version, subtitle_mutation,
    subtitle_source_version, subtitle_write_locks, sync_output_owner_is_unique, write_subtitle_file,
)
from subliminal_patch.core_persistent import download_best_subtitles

from app.config import settings
from app.database import TableEpisodes, TableMovies, database, select, get_profiles_list
from app.jobs_queue import JobCancelled
from utilities.path_mappings import path_mappings
from utilities.helper import get_target_folder, force_unicode
from languages.get_languages import alpha3_from_alpha2

from .mismatch import clear_mismatch_for_video, report_release_type_mismatch
from .pool import update_pools, _get_pool
from .utils import get_video, _get_lang_obj, _get_scores
from .processing import process_subtitle


@update_pools
def generate_subtitles(path, languages, audio_language, sceneName, title, media_type, profile_id,
                       forced_minimum_score=None, is_upgrade=False, check_if_still_required=False,
                       previous_subtitles_to_delete=None, job_id=None, fallback_allowed=False,
                       arr_instance_id=None, source=None, auto_translate=True, source_wanted_stamp=None,
                       source_upgrade=None):
    if not languages:
        return None

    video_version = subtitle_source_version(path) if source is not None else None
    if source is not None and video_version is None:
        return None

    # A media source item is described by its context. Its publication type is what the media servers hear
    # (a sports event publishes as sports although it is scored as a movie), and nothing here reads or writes
    # an arr row for it.
    publication_type = media_type if source is None else source.publication_type

    logging.debug(f'BAZARR Searching subtitles for this file: {path}')  # noqa: G004

    if settings.general.utf8_encode:
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    pool = _get_pool(media_type, profile_id) if source is None else _get_pool(media_type, profile_id, source=source)
    providers = pool.providers

    language_set = _get_language_obj(languages=languages)
    profile = get_profiles_list(profile_id=profile_id)
    if source is not None and profile is None:
        return None
    original_format = profile['originalFormat']
    wanted_profile = deepcopy(profile) if source_wanted_stamp is not None else None
    hi_required = "force HI" if all([x.hi for x in language_set]) else "don't prefer"

    try:
        video_extra = {} if source is None else {'source': source}
        video = get_video(force_unicode(path), title, sceneName, providers=providers, media_type=media_type,
                          **video_extra)
    except ValueError as e:
        logging.exception(f'BAZARR Unable to get video object for {path}: {e}')  # noqa: G004
        return None

    if video:
        # The caller's value wins over the database refiner's, which reverses
        # the path through the global mapping and can miss the row of an
        # instance with a mapping of its own. A provider failure reports the
        # video's ids, and an exclusion the provider demands is recorded under
        # the instance they name.
        if arr_instance_id is not None:
            video.arr_instance_id = arr_instance_id
        minimum_score = settings.general.minimum_score
        minimum_score_movie = settings.general.minimum_score_movie
        if source is not None and source.kind == 'sports_event':
            # Scored as a movie, held to the sports minimum.
            minimum_score_movie = settings.general.minimum_score_sports
        min_score, max_score, scores = _get_scores(media_type, minimum_score_movie, minimum_score)

        from subtitles.tools.mods import get_subzero_mods
        subz_mods = get_subzero_mods(arr_instance_id)
        saved_any = False

        if providers:
            if job_id:
                from app.jobs_queue import jobs_queue as _jq
                active_providers = [p for p in providers if p not in pool.discarded_providers]
                _provider_count = len(active_providers)

                def _on_provider(provider_name):
                    try:
                        idx = active_providers.index(provider_name) + 1
                    except ValueError:
                        idx = 0
                    _jq.update_job_progress(job_id=job_id,
                                            progress_message=f"Searching {provider_name} ({idx}/{_provider_count})")

                pool.provider_progress_callback = _on_provider

            if forced_minimum_score:
                min_score = int(forced_minimum_score)

            # Languages whose subtitle was downloaded. Their recorded release-type
            # mismatch is cleared once the file is actually on disk, not before:
            # save_subtitles can still fail, and clearing early would drop the
            # badge while the language is in fact still missing.
            languages_that_landed = []
            for language in language_set:
                # The saver may adjust the provider's HI flag. Keep the originally requested variant.
                source_language = Language.rebuild(language) if source is not None else None
                # confirm if language is still missing or if cutoff has been reached
                if check_if_still_required and language not in (
                        check_missing_languages_source(source, stored=True) if source_wanted_stamp is not None
                        else check_missing_languages(path, media_type, arr_instance_id,
                                                     **({'source': source} if source is not None else {}))):
                    # cutoff has been reached
                    logging.debug(f"BAZARR this language ({parse_language_object(language)}) is ignored because cutoff "  # noqa: G004
                                  f"has been reached during this search.")
                    continue
                else:
                    # Every candidate this search scores lands in the sink,
                    # including the ones the download loop rejects. When the
                    # search comes back empty they are the only evidence of what
                    # the providers actually have, and looking at them costs no
                    # extra provider request.
                    candidate_sink = []
                    try:
                        downloaded_subtitles = download_best_subtitles(videos={video},
                                                                       languages={language},
                                                                       pool_instance=pool,
                                                                       min_score=int(min_score),
                                                                       hearing_impaired=hi_required,
                                                                       use_original_format=original_format in (1, "1", "True", True),
                                                                       use_provider_priority=settings.general.use_provider_priority,
                                                                       fallback_allowed=fallback_allowed,
                                                                       candidate_sink=candidate_sink)
                    except Exception as e:
                        if isinstance(e, JobCancelled) and e.job_id == job_id:
                            # The provider callback raised through the pool
                            # because Stop was pressed on this job. That is
                            # not a download failure, and swallowing it here
                            # would run every remaining language and episode
                            # of a stopped search. The pool is shared between
                            # the jobs of a profile, so a different job's stop
                            # arrives here as well; it keeps the handling
                            # below, one failed episode.
                            raise
                        logging.exception(f'BAZARR Error downloading Subtitles for this file {path}: {repr(e)}')  # noqa: G004
                        return None

                    # An upgrade search raises the minimum above the score of
                    # the subtitle the user already has, so every candidate is
                    # "rejected" by construction and nothing is actually
                    # missing. Reporting a mismatch there would be pure noise.
                    if not downloaded_subtitles.get(video) and not is_upgrade \
                            and not forced_minimum_score and source is None:
                        try:
                            report_release_type_mismatch(video, media_type, language,
                                                         candidate_sink, int(min_score),
                                                         arr_instance_id=arr_instance_id)
                        except Exception:
                            # A report must never cost the user a search.
                            logging.exception('BAZARR Error checking for a release type '
                                              'mismatch for this file %s', path)
                    elif downloaded_subtitles.get(video):
                        # Only remembered here. The record is cleared after the
                        # file is actually on disk: save_subtitles can fail on
                        # permissions or a full filesystem, and clearing first
                        # would drop the badge and the once-only record while
                        # the language is still missing.
                        languages_that_landed.append((video, language))

                if downloaded_subtitles:
                    for video, subtitles in downloaded_subtitles.items():
                        if not subtitles:
                            continue

                        subtitle_formats = set()
                        for s in subtitles:
                            s.mods = subz_mods
                            subtitle_formats.add(s.format)

                        try:
                            fld = get_target_folder(path)
                            chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
                                'win') and settings.general.chmod_enabled else None
                            publish = publication_callback(publication_type, path, 'download', arr_instance_id,
                                                           **({'source': source} if source is not None else {}))
                            with subtitle_write_locks(path, os.path.join(fld or os.path.dirname(path), '.destination'),
                                                      previous_subtitles_to_delete or path):
                                written_paths = []
                                writer = partial(write_subtitle_file, path, written_paths=written_paths,
                                                 on_publish=publish)
                                owner_kwargs = {}
                                if source is not None:
                                    writer = partial(_write_source_download, path, source=source,
                                                     language=source_language, recheck_missing=check_if_still_required,
                                                     is_upgrade=is_upgrade, written_paths=written_paths,
                                                     on_publish=publish, wanted_stamp=source_wanted_stamp,
                                                     wanted_profile=wanted_profile, upgrade=source_upgrade,
                                                     video_version=video_version)
                                    if is_upgrade and source_upgrade is None:
                                        from media_sources.indexer import SourcePublicationOwnerIndex

                                        owner_kwargs['ownership_index'] = SourcePublicationOwnerIndex()
                                saved_subtitles = save_subtitles(video.original_path, subtitles,
                                                                 single=settings.general.single_language,
                                                                 tags=None,  # fixme
                                                                 directory=fld,
                                                                 chmod=chmod,
                                                                 formats=subtitle_formats,
                                                                 path_decoder=force_unicode,
                                                                 write_subtitle=writer)
                                if is_upgrade and source_upgrade is None and previous_subtitles_to_delete and written_paths and (
                                        os.path.normcase(os.path.realpath(previous_subtitles_to_delete)) not in
                                        {os.path.normcase(os.path.realpath(written)) for written in written_paths}):
                                    try:
                                        with subtitle_mutation(path, previous_subtitles_to_delete, **owner_kwargs):
                                            os.remove(previous_subtitles_to_delete)
                                            publish(previous_subtitles_to_delete)
                                    except OSError:
                                        logging.exception('BAZARR unable to remove superseded subtitle: %s',
                                                          previous_subtitles_to_delete)
                                saved_subtitles = [saved for saved in saved_subtitles if saved.storage_path in written_paths]

                        except Exception as e:
                            logging.exception(
                                f'BAZARR Error saving Subtitles file to disk for this file {path}: {repr(e)}')  # noqa: G004
                            pass
                        else:
                            saved_any = True
                            # Only for a subtitle that really reached disk.
                            # save_subtitles returns normally without writing
                            # anything when get_modified_content yields nothing:
                            # it logs and still appends the subtitle to its
                            # result, so its return value is not evidence of a
                            # file. storage_path is set where the write happens.
                            # The file, not the attribute: save_subtitles sets
                            # storage_path before it writes, so a subtitle whose
                            # content came back empty still carries a path to a
                            # file that was never created.
                            written = {str(getattr(saved, 'language', None))
                                       for saved in saved_subtitles
                                       if getattr(saved, 'storage_path', None)
                                       and os.path.isfile(saved.storage_path)}
                            for landed_video, landed_language in languages_that_landed:
                                if str(landed_language) not in written or source is not None:
                                    # The release-type mismatch detector resolves an item by path in the arr
                                    # tables, so it never runs for a source item.
                                    continue
                                clear_mismatch_for_video(landed_video, media_type, landed_language,
                                                         arr_instance_id=arr_instance_id)
                            languages_that_landed = []
                            for subtitle in saved_subtitles:
                                if "hash" in subtitle.matches:
                                    # make matches set cleaner for history purpose when hash matches
                                    subtitle.matches = {match for match in subtitle.matches
                                                        if match in ("hash", "hearing_impaired")}
                                processed_subtitle = process_subtitle(subtitle=subtitle, media_type=media_type,
                                                                      audio_language=audio_language,
                                                                      is_upgrade=is_upgrade, is_manual=False,
                                                                      path=path, max_score=max_score, job_id=job_id,
                                                                      arr_instance_id=arr_instance_id,
                                                                      **({} if source is None else {'source': source}),
                                                                      **({} if auto_translate else {'auto_translate': False}))
                                if not processed_subtitle:
                                    logging.debug(f"BAZARR unable to process this subtitles: {subtitle}")  # noqa: G004
                                    continue
                                yield processed_subtitle
        else:
            logging.info("BAZARR All providers are throttled")
            return None

        if not saved_any:
            logging.debug(f'BAZARR No Subtitles were found for this file: {path}')  # noqa: G004
            return None

    subliminal.region.backend.sync()

    logging.debug(f'BAZARR Ended searching Subtitles for file: {path}')  # noqa: G004


def _get_language_obj(languages):
    language_set = set()

    if not isinstance(languages, (set, list)):
        languages = [languages]

    for language in languages:
        lang, hi_item, forced_item = language

        # Always use alpha2 in API Request
        lang = alpha3_from_alpha2(lang)

        lang_obj = _get_lang_obj(lang)

        if forced_item == "True":
            lang_obj = Language.rebuild(lang_obj, forced=True)
        if hi_item == "True":
            lang_obj = Language.rebuild(lang_obj, hi=True)

        language_set.add(lang_obj)

    return language_set


def _wanted_source_can_publish(source, language, stamp, profile, *, target=None):
    """Recheck the item and destination after the provider, under the subtitle write lock."""
    from media_sources import repository
    from media_sources.analysis import local_content_stamp
    from media_sources.db import handle
    from media_sources.translation import stored_missing_language_candidates
    from media_sources.wanted import arr_claim_blocks_wanted, wanted_items

    if local_content_stamp(source.path) != stamp or get_profiles_list(profile_id=source.profile_id) != profile:
        return False
    items = repository.Items.c
    live = handle(target).execute(wanted_items(items.missing_subtitles, kind=source.kind, profile_id=source.profile_id).where(
        items.id == source.item_id, items.created_at == source.created_at, items.path == source.path,
        items.source_id == source.source_id, items.library_id == source.library_id)).first()
    if live is None or language not in _get_language_obj(languages=stored_missing_language_candidates(live)):
        return False
    return not arr_claim_blocks_wanted(source, target=target)


def check_missing_languages_source(source, *, stored=False, external_subtitles=None, target=None):
    from media_sources import repository
    from media_sources.translation import missing_language_candidates, stored_missing_language_candidates

    if stored:
        item = repository.get_item(source.item_id, target)
        if item is None or not source.same_item(item) or item.path != source.path:
            return set()
        candidates = stored_missing_language_candidates(item)
    else:
        candidates = missing_language_candidates(source, external_subtitles=external_subtitles, target=target)
    return _get_language_obj(languages=candidates)


def _restore_upgrade_file(backup, path):
    from subtitles.new_subtitle import _restore_backup

    try:
        if not _restore_backup(backup, path):
            logging.error('BAZARR could not restore %s because the path is occupied. Kept %s', path, backup)
    except OSError:
        logging.exception('BAZARR could not restore %s from %s', path, backup)


@contextmanager
def _preserve_source_subtitle(path, expected_version, *, retire):
    """Back up the old bytes until the transaction commits."""
    from subtitles.new_subtitle import _backup_for_replacement, _discard

    try:
        backup = _backup_for_replacement(path, expected_version=expected_version)
    except (OSError, SubtitleSourceChanged) as error:
        raise SubtitleDestinationChanged(f'Unable to back up the previous source subtitle: {error}') from error
    if backup is None:
        raise SubtitleDestinationChanged('The previous subtitle disappeared during publication')

    try:
        if regular_subtitle_version(path) != expected_version:
            raise SubtitleDestinationChanged('The previous subtitle changed during publication')
        if retire:
            os.remove(path)
    except OSError as error:
        _discard(backup)
        raise SubtitleDestinationChanged(f'Unable to retire the previous source subtitle: {error}') from error
    except BaseException:
        _discard(backup)
        raise

    try:
        yield
    except BaseException:
        if regular_subtitle_version(path) == expected_version:
            _discard(backup)
        else:
            _restore_upgrade_file(backup, path)
        raise
    else:
        _discard(backup)


@contextmanager
def _quarantine_upgrade_output(path, expected_version):
    """Keep a generated sibling recoverable until its replacement commits."""
    if regular_subtitle_version(path) != expected_version:
        raise SubtitleDestinationChanged('A generated subtitle changed during publication')
    fd, backup = tempfile.mkstemp(prefix=f'.bazarr-sync-obsolete-{path.name[:40]}-',
                                 suffix='.bak', dir=path.parent)
    os.close(fd)
    try:
        os.replace(path, backup)
    except BaseException:
        os.unlink(backup)
        raise
    try:
        yield
    except BaseException:
        _restore_upgrade_file(backup, path)
        raise


def _prepare_upgrade_outputs(video_path, paths, ownership_index):
    outputs = {}
    vobsub_indexes = {}
    for path in paths:
        if is_sync_engine_output(path):
            continue
        for engine in SYNC_ENGINES:
            output = engine_output_path(path, engine)
            version = regular_subtitle_version(output)
            if version is None or output in outputs:
                continue
            if output.suffix.lower() == '.sub':
                if output.parent not in vobsub_indexes:
                    with os.scandir(output.parent) as entries:
                        vobsub_indexes[output.parent] = {entry.name.lower() for entry in entries
                                                         if entry.name.lower().endswith('.idx')
                                                         and entry.is_file(follow_symlinks=False)}
                if is_vobsub_pair(output.name, vobsub_indexes[output.parent]):
                    continue
            if sync_output_owner_is_unique(video_path, output, ownership_index=ownership_index):
                outputs[output] = version
    return outputs


def _write_source_download(video_path, destination, content, *, source, language, recheck_missing, is_upgrade,
                           written_paths, on_publish, wanted_stamp=None, wanted_profile=None, upgrade=None,
                           video_version=None):
    """Publish the actual provider-selected file only while its destination is still eligible."""
    from media_sources.indexer import SourcePublicationOwnerIndex
    from media_sources.translation import prepare_source_sidecars
    from media_sources.wanted import arr_claim_blocks_wanted

    ownership_index = SourcePublicationOwnerIndex()
    old_path = upgrade.subtitles_path if upgrade is not None else None
    same_destination = old_path is not None and os.path.normcase(os.path.realpath(old_path)) == (
        os.path.normcase(os.path.realpath(destination)))
    remove_previous = old_path is not None and not same_destination
    registry_destination = destination
    if old_path is not None and os.path.dirname(old_path) != os.path.dirname(video_path):
        registry_destination = old_path

    def prepare():
        if wanted_stamp is not None and not _wanted_source_can_publish(source, language, wanted_stamp, wanted_profile):
            raise SubtitleDestinationChanged('Wanted source eligibility changed during search')
        if old_path is not None:
            if arr_claim_blocks_wanted(source):
                raise SubtitleDestinationChanged('Arr claimed the source video during the upgrade search')
            if remove_previous and not sync_output_owner_is_unique(
                    video_path, old_path, ownership_index=ownership_index):
                raise SubtitleDestinationChanged('The previous subtitle does not have one media owner')
        if not sync_output_owner_is_unique(video_path, destination, ownership_index=ownership_index):
            raise SubtitleDestinationChanged('The source subtitle destination does not have one media owner')
        if old_path is not None:
            paths = (old_path, destination) if remove_previous else (destination,)
            return _prepare_upgrade_outputs(video_path, paths, ownership_index)
        if wanted_stamp is None and recheck_missing:
            return prepare_source_sidecars(source, ownership_index, written_paths)
        return None

    @contextmanager
    def publication(files):
        with ownership_index.publication(source, prepare, destination=registry_destination) as (
                current, connection, prepared):
            if current is None or current.path != video_path:
                raise SubtitleDestinationChanged('The source item moved or was removed')
            if video_version is not None and subtitle_source_version(video_path) != video_version:
                raise SubtitleDestinationChanged('The media file changed while searching for subtitles')
            overwrite = is_upgrade if upgrade is None else same_destination
            if os.path.lexists(destination) and (not overwrite or regular_subtitle_version(destination) is None):
                raise SubtitleDestinationChanged('The source subtitle destination is already occupied')
            if upgrade is not None:
                if current.profile_id != source.profile_id or not upgrade.can_publish(current, target=connection):
                    raise SubtitleDestinationChanged('Source upgrade eligibility changed during search')
                files.enter_context(subtitle_mutation(video_path, old_path, *prepared, invalidate_outputs=False))
                files.enter_context(_preserve_source_subtitle(old_path, upgrade.subtitle_version, retire=remove_previous))
                for output, version in prepared.items():
                    files.enter_context(_quarantine_upgrade_output(output, version))
            elif wanted_stamp is not None:
                if not _wanted_source_can_publish(current, language, wanted_stamp, wanted_profile, target=connection):
                    raise SubtitleDestinationChanged('Wanted source eligibility changed during search')
            elif recheck_missing and language not in check_missing_languages_source(
                    current, external_subtitles=prepared, target=connection):
                raise SubtitleDestinationChanged('The source language is no longer missing')
            yield

    try:
        with ExitStack() as files:
            if upgrade is not None:
                files.enter_context(subtitle_write_locks(video_path, destination, old_path))
            write_subtitle_file(video_path, destination, content,
                                written_paths=written_paths if upgrade is None else None,
                                on_publish=on_publish if upgrade is None else None,
                                publication_guard=partial(publication, files), ownership_index=ownership_index,
                                rollback_new_on_guard_error=upgrade is not None,
                                invalidate_outputs=upgrade is None)
    except SubtitleDestinationChanged as error:
        logging.info('BAZARR skipped saving a subtitle for source item %s: %s', source.item_id, error)
        return False
    if upgrade is not None:
        written_paths.append(destination)
        _report_subtitle_publication(on_publish, destination)
        if remove_previous:
            _report_subtitle_publication(on_publish, old_path)
    return True


def parse_language_object(language):
    if isinstance(language, Language):
        hi = ":hi" if language.hi else ""
        forced = ":forced" if language.forced else ""
        return language.basename + hi + forced
    else:
        return language


def check_missing_languages(path, media_type, arr_instance_id=None, source=None):
    if source is not None:
        return check_missing_languages_source(source)
    # confirm if language is still missing or if cutoff has been reached
    reversed_path = path_mappings.path_replace_reverse_instance(path, arr_instance_id, media_type)
    if media_type == 'series':
        confirmed_missing_subs = database.execute(scoped(
            select(TableEpisodes.missing_subtitles)
            .where(TableEpisodes.path == reversed_path), TableEpisodes.arr_instance_id, arr_instance_id))\
            .first()
    else:
        confirmed_missing_subs = database.execute(scoped(
            select(TableMovies.missing_subtitles)
            .where(TableMovies.path == reversed_path), TableMovies.arr_instance_id, arr_instance_id))\
            .first()

    if not confirmed_missing_subs:
        logging.debug(f"BAZARR no media with this path have been found in database: {reversed_path}")  # noqa: G004
        return []

    languages = []
    for language in ast.literal_eval(confirmed_missing_subs.missing_subtitles):
        if language is not None:
            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ = "True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))

    return _get_language_obj(languages=languages)

# coding=utf-8
# fmt: off

import os
import sys
import logging
import subliminal
import ast

from subzero.language import Language
from subliminal_patch.core import save_subtitles
from subliminal_patch.core_persistent import download_best_subtitles

from app.config import settings
from app.database import TableEpisodes, TableMovies, database, select, get_profiles_list
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
                       arr_instance_id=None):
    if not languages:
        return None

    logging.debug(f'BAZARR Searching subtitles for this file: {path}')  # noqa: G004

    if settings.general.utf8_encode:
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    pool = _get_pool(media_type, profile_id)
    providers = pool.providers

    language_set = _get_language_obj(languages=languages)
    profile = get_profiles_list(profile_id=profile_id)
    original_format = profile['originalFormat']
    hi_required = "force HI" if all([x.hi for x in language_set]) else "don't prefer"

    try:
        video = get_video(force_unicode(path), title, sceneName, providers=providers, media_type=media_type)
    except ValueError as e:
        logging.exception(f'BAZARR Unable to get video object for {path}: {e}')  # noqa: G004
        return None

    if video:
        minimum_score = settings.general.minimum_score
        minimum_score_movie = settings.general.minimum_score_movie
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
                min_score = int(forced_minimum_score) + 1

            # Languages whose subtitle was downloaded. Their recorded release-type
            # mismatch is cleared once the file is actually on disk, not before:
            # save_subtitles can still fail, and clearing early would drop the
            # badge while the language is in fact still missing.
            languages_that_landed = []
            for language in language_set:
                # confirm if language is still missing or if cutoff has been reached
                if check_if_still_required and language not in check_missing_languages(path, media_type):
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
                        logging.exception(f'BAZARR Error downloading Subtitles for this file {path}: {repr(e)}')  # noqa: G004
                        return None

                    # An upgrade search raises the minimum above the score of
                    # the subtitle the user already has, so every candidate is
                    # "rejected" by construction and nothing is actually
                    # missing. Reporting a mismatch there would be pure noise.
                    if not downloaded_subtitles.get(video) and not is_upgrade \
                            and not forced_minimum_score:
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
                            if is_upgrade and previous_subtitles_to_delete:
                                try:
                                    # delete previously downloaded subtitles in case of an upgrade to prevent edge loop
                                    # issue.
                                    os.remove(previous_subtitles_to_delete)
                                except (OSError, FileNotFoundError):
                                    pass
                            saved_subtitles = save_subtitles(video.original_path, subtitles,
                                                             single=settings.general.single_language,
                                                             tags=None,  # fixme
                                                             directory=fld,
                                                             chmod=chmod,
                                                             formats=subtitle_formats,
                                                             path_decoder=force_unicode
                                                             )
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
                                if str(landed_language) not in written:
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
                                                                      path=path, max_score=max_score, job_id=job_id)
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


def parse_language_object(language):
    if isinstance(language, Language):
        hi = ":hi" if language.hi else ""
        forced = ":forced" if language.forced else ""
        return language.basename + hi + forced
    else:
        return language


def check_missing_languages(path, media_type):
    # confirm if language is still missing or if cutoff has been reached
    if media_type == 'series':
        confirmed_missing_subs = database.execute(
            select(TableEpisodes.missing_subtitles)
            .where(TableEpisodes.path == path_mappings.path_replace_reverse(path)))\
            .first()
    else:
        confirmed_missing_subs = database.execute(
            select(TableMovies.missing_subtitles)
            .where(TableMovies.path == path_mappings.path_replace_reverse_movie(path)))\
            .first()

    if not confirmed_missing_subs:
        reversed_path = path_mappings.path_replace_reverse(path) if media_type == 'series' else \
            path_mappings.path_replace_reverse_movie(path)
        logging.debug(f"BAZARR no media with this path have been found in database: {reversed_path}")  # noqa: G004
        return []

    languages = []
    for language in ast.literal_eval(confirmed_missing_subs.missing_subtitles):
        if language is not None:
            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ = "True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))

    return _get_language_obj(languages=languages)

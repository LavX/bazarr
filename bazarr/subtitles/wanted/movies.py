# coding=utf-8
# fmt: off

import ast
import logging
import operator
import os

from functools import reduce

from utilities.path_mappings import path_mappings
from subtitles.indexer.movies import store_subtitles_movie, list_missing_subtitles_movies
from arr_instances.resolution import scoped
from radarr.history import history_log_movie
from app.notifier import send_notifications_movie
from app.get_providers import get_providers
from app.config import settings
from app.database import (get_exclusion_clause, get_audio_profile_languages, get_profiles_list, TableMovies,
                          TableHistoryMovie, database, update, select)
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from subliminal_patch.score import MAX_SCORES

from ..adaptive_searching import is_search_active, updateFailedAttempts
from ..download import generate_subtitles
from ..language_profiles import build_translate_from_map
from .utils import _find_existing_subtitle_path


def _wanted_movie(movie, providers_list, job_id=None):
    arr_instance_id = getattr(movie, 'arr_instance_id', None)
    audio_language_list = get_audio_profile_languages(movie.audio_language)
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    profile = get_profiles_list(profile_id=movie.profileId) if movie.profileId else None
    translate_from_map = build_translate_from_map(profile)

    languages = []
    languages_to_stamp = []
    video_path = path_mappings.path_replace_movie(movie.path)

    for language in ast.literal_eval(movie.missing_subtitles):
        lang_code = language.split(':')[0]

        translate_cfg = translate_from_map.get(language)
        if translate_cfg:
            source_srt = _find_existing_subtitle_path(
                movie.subtitles,
                translate_cfg['from'],
                path_replace_fn=path_mappings.path_replace_movie,
            )
            if source_srt:
                min_score = settings.translator.min_source_score
                history = database.execute(
                    scoped(
                        select(TableHistoryMovie.score)
                        .where(TableHistoryMovie.radarrId == movie.radarrId)
                        .where(TableHistoryMovie.language.like(f"{translate_cfg['from']}%"))
                        .where(TableHistoryMovie.score.is_not(None))
                        .order_by(TableHistoryMovie.timestamp.desc())
                        .limit(1),
                        TableHistoryMovie.arr_instance_id,
                        arr_instance_id,
                    )
                ).first()
                if history and history.score:
                    source_score_pct = round((history.score / MAX_SCORES['movie']) * 100, 1)
                else:
                    # No history record — subtitle may have been manually placed or
                    # predates history tracking. Treat as exactly at threshold so
                    # we proceed with translation instead of silently falling
                    # back to provider search.
                    source_score_pct = min_score
                if source_score_pct < min_score:
                    logging.debug(
                        "BAZARR auto-translate (wanted-scan) skipped for %s: "
                        "source score %s%% < threshold %s%% "
                        "(falling back to provider search)",
                        video_path, source_score_pct, min_score,
                    )
                else:
                    # Guard: skip if we already have a translate history entry
                    # for this target language AND the translated file still
                    # exists on disk. Why: the translate service writes
                    # action=6 on successful completion, which blocks
                    # re-queuing after a successful translation. But if the
                    # translated subtitle was deleted or moved after that
                    # success, the file is missing again and we must re-queue;
                    # checking only the history row would suppress the
                    # replacement forever.
                    already_translated = database.execute(
                        scoped(
                            select(TableHistoryMovie.subtitles_path)
                            .where(TableHistoryMovie.radarrId == movie.radarrId)
                            .where(TableHistoryMovie.language == language)
                            .where(TableHistoryMovie.action == 6)
                            .order_by(TableHistoryMovie.timestamp.desc())
                            .limit(1),
                            TableHistoryMovie.arr_instance_id,
                            arr_instance_id,
                        )
                    ).first()
                    if already_translated and already_translated.subtitles_path:
                        local_subs_path = path_mappings.path_replace_movie(
                            already_translated.subtitles_path
                        )
                        if local_subs_path and os.path.exists(local_subs_path):
                            continue
                    # Fetch additional columns required by postprocess_subtitles
                    # (imdbId/tmdbId for plex/jellyfin refresh). movie row
                    # only carries the subset selected for wanted scans.
                    metadata = database.execute(
                        scoped(
                            select(TableMovies.imdbId,
                                   TableMovies.tmdbId,
                                   TableMovies.arr_instance_id)
                            .where(TableMovies.radarrId == movie.radarrId),
                            TableMovies.arr_instance_id,
                            arr_instance_id,
                        )
                    ).first()
                    try:
                        from subtitles.tools.translate.main import translate_subtitles_file
                        translate_kwargs = dict(
                            video_path=video_path,
                            source_srt_file=source_srt,
                            from_lang=translate_cfg['from'],
                            to_lang=lang_code,
                            forced=translate_cfg['forced'],
                            hi=translate_cfg['hi'],
                            media_type='movies',
                            sonarr_series_id=None,
                            sonarr_episode_id=None,
                            radarr_id=movie.radarrId,
                            metadata=metadata,
                        )
                        # Guard: skip if an identical translate job is already
                        # pending or running. Why: history guard (action=6) only
                        # blocks re-queue after successful completion; without
                        # this check, every wanted-scan tick during a pending
                        # translation would enqueue a duplicate job.
                        if jobs_queue._is_an_existing_job(
                            module='subtitles.tools.translate.main',
                            func='translate_subtitles_file',
                            args=[],
                            kwargs=translate_kwargs,
                        ):
                            continue
                        logging.info(
                            "BAZARR auto-translate (wanted-scan) queuing %s -> %s for %s",
                            translate_cfg['from'], lang_code, video_path,
                        )
                        translate_subtitles_file(**translate_kwargs)
                        continue
                    except Exception:
                        logging.exception(
                            "BAZARR failed to queue auto-translate for %s",
                            video_path,
                        )
                        # Fall through to normal provider search on queuing failure

        if is_search_active(desired_language=language, attempt_string=movie.failedAttempts):
            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ = "True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))
            languages_to_stamp.append(language)

        else:
            logging.info(f"BAZARR Search is throttled by adaptive search for this movie {movie.path} and "  # noqa: G004
                         f"language: {language}")

    found_any = False
    for result in generate_subtitles(video_path,
                                     languages,
                                     audio_language,
                                     str(movie.sceneName),
                                     movie.title,
                                     'movie',
                                     movie.profileId,
                                     check_if_still_required=True,
                                     job_id=job_id,
                                     fallback_allowed=settings.general.use_whisper_fallback,
                                     arr_instance_id=arr_instance_id):

        if result:
            found_any = True
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles_movie(movie.path, path_mappings.path_replace_movie(movie.path))
            history_log_movie(1, movie.radarrId, result, arr_instance_id=arr_instance_id)
            event_stream(type='movie-wanted', action='delete', payload=movie.radarrId)
            send_notifications_movie(movie.radarrId, result.message, arr_instance_id=arr_instance_id)

    if not found_any and providers_list:
        for language in languages_to_stamp:
            updated = updateFailedAttempts(
                desired_language=language,
                attempt_string=movie.failedAttempts)
            stmt = scoped(
                update(TableMovies)
                .values(failedAttempts=updated)
                .where(TableMovies.radarrId == movie.radarrId),
                TableMovies.arr_instance_id, arr_instance_id)
            database.execute(stmt)


def wanted_download_subtitles_movie(radarr_id, job_id=None, arr_instance_id=None):
    stmt = scoped(
        select(TableMovies.path,
               TableMovies.missing_subtitles,
               TableMovies.radarrId,
               TableMovies.arr_instance_id,
               TableMovies.audio_language,
               TableMovies.sceneName,
               TableMovies.failedAttempts,
               TableMovies.title,
               TableMovies.profileId,
               TableMovies.subtitles)
        .where(TableMovies.radarrId == radarr_id),
        TableMovies.arr_instance_id, arr_instance_id)
    movie = database.execute(stmt).first()

    if not movie:
        logging.debug(f"BAZARR no movie with that radarrId can be found in database: {radarr_id}")  # noqa: G004
        return
    elif movie.subtitles is None:
        # subtitles indexing for this movie is incomplete, we'll do it again
        store_subtitles_movie(movie.path, path_mappings.path_replace_movie(movie.path))
        movie = database.execute(stmt).first()
    elif movie.missing_subtitles is None:
        # missing subtitles calculation for this movie is incomplete, we'll do it again
        list_missing_subtitles_movies(no=radarr_id, arr_instance_id=arr_instance_id)
        movie = database.execute(stmt).first()

    providers_list = get_providers()

    if providers_list:
        _wanted_movie(movie, providers_list, job_id=job_id)
    else:
        logging.info("BAZARR All providers are throttled")


def wanted_scan_subtitles_movies(job_id=None):
    if not job_id:
        jobs_queue.add_job_from_function("Scanning disk for missing movies subtitles", is_progress=True)
        return

    conditions = [(TableMovies.missing_subtitles.is_not(None)),
                  (TableMovies.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('movie')
    movies = database.execute(
        select(TableMovies.radarrId,
               TableMovies.path,
               TableMovies.title)
        .where(reduce(operator.and_, conditions))) \
        .all()

    count_movies = len(movies)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_movies)

    if count_movies == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    for i, movie in enumerate(movies, start=1):
        jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_message=movie.title)
        store_subtitles_movie(movie.path, path_mappings.path_replace_movie(movie.path), use_cache=False)

    jobs_queue.update_job_progress(job_id=job_id, progress_message="Scan completed")


def wanted_search_missing_subtitles_movies(job_id=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Searching for missing movies subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    conditions = [(TableMovies.missing_subtitles.is_not(None)),
                  (TableMovies.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('movie')
    movies = database.execute(
        select(TableMovies.radarrId,
               TableMovies.arr_instance_id,
               TableMovies.tags,
               TableMovies.monitored,
               TableMovies.title)
        .where(reduce(operator.and_, conditions))) \
        .all()

    count_movies = len(movies)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_movies)

    if count_movies == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    throttled = False
    for i, movie in enumerate(movies, start=1):
        jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_message=movie.title)

        providers = get_providers()
        if providers:
            wanted_download_subtitles_movie(movie.radarrId, job_id=job_id,
                                            arr_instance_id=movie.arr_instance_id)

            # make sure to override the progress value updated by the subtitles synchronization
            jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_max=count_movies)
        else:
            logging.info("BAZARR All providers are throttled")
            throttled = True
            break

    outcome_msg = ("All providers throttled" if throttled
                   else "Search completed")
    jobs_queue.update_job_progress(job_id=job_id, progress_message=outcome_msg)
    jobs_queue.update_job_name(job_id=job_id, new_job_name="Searched for missing movies subtitles")
    logging.info('BAZARR Finished searching for missing Movies Subtitles. Check History for more information.')

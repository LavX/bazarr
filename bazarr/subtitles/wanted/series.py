# coding=utf-8
# fmt: off

import ast
import logging
import operator
import gc

from functools import reduce

from utilities.path_mappings import path_mappings
from subtitles.indexer.series import store_subtitles, list_missing_subtitles
from sonarr.history import history_log
from app.notifier import send_notifications
from app.get_providers import get_providers
from app.database import (get_exclusion_clause, get_audio_profile_languages, get_profiles_list, TableShows,
                          TableEpisodes, TableHistory, database, update, select)
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from app.config import settings
from subliminal_patch.score import MAX_SCORES

from ..adaptive_searching import is_search_active, updateFailedAttempts
from ..download import generate_subtitles
from .utils import _find_existing_subtitle_path


def _wanted_episode(episode, providers_list, job_id=None):
    audio_language_list = get_audio_profile_languages(episode.audio_language)
    if len(audio_language_list) > 0:
        audio_language = audio_language_list[0]['name']
    else:
        audio_language = 'None'

    profile = get_profiles_list(profile_id=episode.profileId) if episode.profileId else None
    translate_from_map = {}
    if profile:
        for prof_item in profile.get('items', []):
            src = prof_item.get('translate_from')
            if src:
                translate_from_map[prof_item.get('language')] = {
                    'from': src,
                    'hi': prof_item.get('hi') == 'True',
                    'forced': prof_item.get('forced') == 'True',
                }

    languages = []
    languages_to_stamp = []
    video_path = path_mappings.path_replace(episode.path)

    for language in ast.literal_eval(episode.missing_subtitles):
        lang_code = language.split(':')[0]

        translate_cfg = translate_from_map.get(lang_code)
        if translate_cfg:
            source_srt = _find_existing_subtitle_path(episode.subtitles, translate_cfg['from'])
            if source_srt:
                min_score = settings.translator.min_source_score
                history = database.execute(
                    select(TableHistory.score)
                    .where(TableHistory.sonarrEpisodeId == episode.sonarrEpisodeId)
                    .where(TableHistory.language == translate_cfg['from'])
                    .where(TableHistory.score.is_not(None))
                    .order_by(TableHistory.timestamp.desc())
                    .limit(1)
                ).first()
                if history and history.score:
                    source_score_pct = round((history.score / MAX_SCORES['episode']) * 100, 1)
                else:
                    # No history record — subtitle may have been manually placed or
                    # predates history tracking. Treat as exactly at threshold so
                    # we proceed with translation instead of silently falling
                    # back to provider search.
                    source_score_pct = min_score
                if source_score_pct < min_score:
                    logging.debug(
                        f"BAZARR auto-translate (wanted-scan) skipped for {video_path}: "
                        f"source score {source_score_pct}% < threshold {min_score}% "
                        f"(falling back to provider search)"
                    )
                else:
                    # Guard: skip if we already have a translate history entry
                    # for this target language. Why: the previous translate
                    # history_log write is what blocks re-queuing after a
                    # restart while the queued job hasn't run yet. Without this
                    # check, the wanted-scan would re-queue every cycle.
                    already_translated = database.execute(
                        select(TableHistory.id)
                        .where(TableHistory.sonarrEpisodeId == episode.sonarrEpisodeId)
                        .where(TableHistory.language == lang_code)
                        .where(TableHistory.action == 6)
                        .limit(1)
                    ).first()
                    if already_translated:
                        continue
                    try:
                        from subtitles.tools.translate.main import translate_subtitles_file
                        from subtitles.tools.translate.core.translator_utils import create_process_result
                        logging.info(f"BAZARR auto-translate (wanted-scan) queuing "
                                     f"{translate_cfg['from']} -> {lang_code} for {video_path}")
                        # Pre-log a translate history entry before queuing the async
                        # translation job. Why: translate_subtitles_file (no job_id)
                        # defers to the jobs queue and returns immediately; without a
                        # marker the next wanted-scan cycle re-queues the same
                        # translation before the worker runs.
                        translate_msg = (f"BAZARR auto-translate (wanted-scan) queued "
                                         f"{translate_cfg['from']} -> {lang_code}")
                        pre_result = create_process_result(
                            message=translate_msg,
                            video_path=video_path,
                            orig_to_lang=lang_code,
                            forced=language.endswith(':forced') or translate_cfg['forced'],
                            hi=language.endswith(':hi') or translate_cfg['hi'],
                            dest_srt_file=source_srt,
                            media_type='episode',
                        )
                        history_log(6, episode.sonarrSeriesId, episode.sonarrEpisodeId, pre_result)
                        translate_subtitles_file(
                            video_path=video_path,
                            source_srt_file=source_srt,
                            from_lang=translate_cfg['from'],
                            to_lang=lang_code,
                            forced=language.endswith(':forced') or translate_cfg['forced'],
                            hi=language.endswith(':hi') or translate_cfg['hi'],
                            media_type='series',
                            sonarr_series_id=episode.sonarrSeriesId,
                            sonarr_episode_id=episode.sonarrEpisodeId,
                            radarr_id=None,
                            metadata=None,
                        )
                        continue
                    except Exception:
                        logging.exception(f"BAZARR failed to queue auto-translate for {video_path} "
                                          f"language {lang_code}; falling back to provider search")

        if is_search_active(desired_language=language, attempt_string=episode.failedAttempts):
            hi_ = "True" if language.endswith(':hi') else "False"
            forced_ = "True" if language.endswith(':forced') else "False"
            languages.append((language.split(":")[0], hi_, forced_))
            languages_to_stamp.append(language)

        else:
            logging.debug(
                f"BAZARR Search is throttled by adaptive search for this episode {episode.path} and "  # noqa: G004
                f"language: {language}")

    found_any = False
    for result in generate_subtitles(video_path,
                                     languages,
                                     audio_language,
                                     str(episode.sceneName),
                                     episode.title,
                                     'series',
                                     episode.profileId,
                                     check_if_still_required=True,
                                     job_id=job_id,
                                     fallback_allowed=settings.general.use_whisper_fallback):
        if result:
            found_any = True
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles(episode.path, path_mappings.path_replace(episode.path))
            history_log(1, episode.sonarrSeriesId, episode.sonarrEpisodeId, result)
            event_stream(type='series', action='update', payload=episode.sonarrSeriesId)
            event_stream(type='episode-wanted', action='delete', payload=episode.sonarrEpisodeId)
            send_notifications(episode.sonarrSeriesId, episode.sonarrEpisodeId, result.message)

    if not found_any and providers_list:
        for language in languages_to_stamp:
            updated = updateFailedAttempts(
                desired_language=language,
                attempt_string=episode.failedAttempts)
            database.execute(
                update(TableEpisodes)
                .values(failedAttempts=updated)
                .where(TableEpisodes.sonarrEpisodeId ==
                       episode.sonarrEpisodeId))


def wanted_download_subtitles(sonarr_episode_id, job_id=None):
    stmt = select(TableEpisodes.path,
                  TableEpisodes.missing_subtitles,
                  TableEpisodes.sonarrEpisodeId,
                  TableEpisodes.sonarrSeriesId,
                  TableEpisodes.audio_language,
                  TableEpisodes.sceneName,
                  TableEpisodes.failedAttempts,
                  TableShows.title,
                  TableShows.profileId,
                  TableEpisodes.subtitles) \
        .select_from(TableEpisodes) \
        .join(TableShows) \
        .where((TableEpisodes.sonarrEpisodeId == sonarr_episode_id))
    episode_details = database.execute(stmt).first()

    if not episode_details:
        logging.debug(f"BAZARR no episode with that sonarrId can be found in database: {sonarr_episode_id}")  # noqa: G004
        return
    elif episode_details.subtitles is None:
        # subtitles indexing for this episode is incomplete, we'll do it again
        store_subtitles(episode_details.path, path_mappings.path_replace(episode_details.path))
        episode_details = database.execute(stmt).first()
    elif episode_details.missing_subtitles is None:
        # missing subtitles calculation for this episode is incomplete, we'll do it again
        list_missing_subtitles(epno=sonarr_episode_id)
        episode_details = database.execute(stmt).first()

    providers_list = get_providers()

    if providers_list:
        _wanted_episode(episode_details, providers_list, job_id=job_id)
    else:
        logging.info("BAZARR All providers are throttled")


def wanted_scan_subtitles_series(job_id=None):
    if not job_id:
        jobs_queue.add_job_from_function("Scanning disk for missing series subtitles", is_progress=True)
        return

    conditions = [(TableEpisodes.missing_subtitles.is_not(None)),
                  (TableEpisodes.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('series')
    episodes = database.execute(
        select(TableEpisodes.sonarrSeriesId,
               TableEpisodes.sonarrEpisodeId,
               TableEpisodes.path,
               TableShows.title,
               TableEpisodes.season,
               TableEpisodes.episode,
               TableEpisodes.title.label('episodeTitle'))
        .select_from(TableEpisodes)
        .join(TableShows)
        .where(reduce(operator.and_, conditions))) \
        .all()

    count_episodes = len(episodes)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_episodes)

    if count_episodes == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    for i, episode in enumerate(episodes, start=1):
        jobs_queue.update_job_progress(job_id=job_id, progress_value=i,
                                       progress_message=f'{episode.title} - S{episode.season:02d}E{episode.episode:02d}'
                                                        f' - {episode.episodeTitle}')
        store_subtitles(episode.path, path_mappings.path_replace(episode.path), use_cache=False)

    jobs_queue.update_job_progress(job_id=job_id, progress_message="Scan completed")


def wanted_search_missing_subtitles_series(job_id=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Searching for missing series subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    conditions = [(TableEpisodes.missing_subtitles.is_not(None)),
                  (TableEpisodes.missing_subtitles != '[]')]
    conditions += get_exclusion_clause('series')
    episodes = database.execute(
        select(TableEpisodes.sonarrSeriesId,
               TableEpisodes.sonarrEpisodeId,
               TableShows.tags,
               TableEpisodes.monitored,
               TableShows.title,
               TableEpisodes.season,
               TableEpisodes.episode,
               TableEpisodes.title.label('episodeTitle'),
               TableShows.seriesType)
        .select_from(TableEpisodes)
        .join(TableShows)
        .where(reduce(operator.and_, conditions))) \
        .all()

    count_episodes = len(episodes)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_episodes)

    if count_episodes == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    throttled = False
    for i, episode in enumerate(episodes, start=1):
        jobs_queue.update_job_progress(job_id=job_id, progress_value=i,
                                       progress_message=f'{episode.title} - S{episode.season:02d}E{episode.episode:02d}'
                                                        f' - {episode.episodeTitle}')

        providers = get_providers()
        if providers:
            wanted_download_subtitles(episode.sonarrEpisodeId, job_id=job_id)

            # make sure to override the progress value updated by the subtitles synchronization
            jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_max=count_episodes)
        else:
            logging.info("BAZARR All providers are throttled")
            throttled = True
            break

    outcome_msg = ("All providers throttled" if throttled
                   else "Search completed")
    jobs_queue.update_job_progress(job_id=job_id, progress_message=outcome_msg)
    jobs_queue.update_job_name(job_id=job_id, new_job_name="Searched for missing series subtitles")
    logging.info('BAZARR Finished searching for missing Series Subtitles. Check History for more information.')

    gc.collect()

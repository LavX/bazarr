# coding=utf-8
# fmt: off

import logging
import operator
import ast

from datetime import datetime, timedelta
from functools import reduce
from sqlalchemy import and_, or_

from app.config import settings
from app.database import get_exclusion_clause, get_audio_profile_languages, TableShows, TableEpisodes, TableMovies, \
    TableHistory, TableHistoryMovie, database, select, func, get_profiles_list
from app.jobs_queue import jobs_queue
from app.get_providers import get_providers
from app.notifier import send_notifications, send_notifications_movie
from radarr.history import history_log_movie
from sonarr.history import history_log
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.indexer.series import store_subtitles
from utilities.path_mappings import path_mappings
from .download import generate_subtitles
from app.event_handler import event_stream


def upgrade_subtitles(wait_for_completion=False):
    use_sonarr = settings.general.use_sonarr
    use_radarr = settings.general.use_radarr

    if use_sonarr:
        upgrade_episodes_subtitles(wait_for_completion=wait_for_completion)

    if use_radarr:
        upgrade_movies_subtitles(wait_for_completion=wait_for_completion)

    logging.info('BAZARR Finished searching for Subtitles to upgrade. Check History for more information.')


def upgrade_episodes_subtitles(job_id=None, sonarr_series_ids=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Trying to upgrade episodes subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    episodes_to_upgrade = get_upgradable_episode_subtitles()

    query = select(TableHistory.id,
               TableShows.title.label('seriesTitle'),
               TableEpisodes.season,
               TableEpisodes.episode,
               TableEpisodes.title,
               TableHistory.language,
               TableEpisodes.audio_language,
               TableHistory.video_path,
               TableEpisodes.sceneName,
               TableHistory.score,
               TableHistory.sonarrEpisodeId,
               TableHistory.sonarrSeriesId,
               TableHistory.subtitles_path,
               TableEpisodes.path,
               TableShows.profileId,
               TableEpisodes.subtitles.label('external_subtitles')) \
        .select_from(TableHistory) \
        .join(TableShows, onclause=and_(TableHistory.sonarrSeriesId == TableShows.sonarrSeriesId,
                                  TableHistory.arr_instance_id == TableShows.arr_instance_id)) \
        .join(TableEpisodes, onclause=and_(TableHistory.sonarrEpisodeId == TableEpisodes.sonarrEpisodeId,
                                     TableHistory.arr_instance_id == TableEpisodes.arr_instance_id))

    if sonarr_series_ids:
        query = query.where(TableHistory.sonarrSeriesId.in_(sonarr_series_ids))

    episodes_data = [{
        'id': x.id,
        'seriesTitle': x.seriesTitle,
        'season': x.season,
        'episode': x.episode,
        'title': x.title,
        'language': x.language,
        'audio_language': x.audio_language,
        'video_path': x.video_path,
        'sceneName': x.sceneName,
        'score': x.score,
        'sonarrEpisodeId': x.sonarrEpisodeId,
        'sonarrSeriesId': x.sonarrSeriesId,
        'subtitles_path': x.subtitles_path,
        'path': x.path,
        'profileId': x.profileId,
        'external_subtitles': [y[1] for y in ast.literal_eval(x.external_subtitles) if y[1]],
    } for x in database.execute(query)
    .all() if _language_still_desired(x.language, x.profileId) and
              x.video_path == x.path
    ]

    for item in episodes_data:
        # do not consider subtitles that do not exist on disk anymore
        if item['subtitles_path'] not in item['external_subtitles']:
            current_sub = _find_current_subtitle_for_language(item['language'], item['external_subtitles'])
            if current_sub:
                logging.debug(f"Upgrade candidate {item['id']} ({item['seriesTitle']} S{item['season']:02d}E"  # noqa: G004
                              f"{item['episode']:02d}): history path no longer on disk, using current subtitle "
                              f"for same language ({current_sub})")
                item['subtitles_path'] = current_sub
            else:
                continue

        # Mark upgradable and get original_id
        item.update({'original_id': episodes_to_upgrade.get(item['id'])})
        item.update({'upgradable': item['id'] in episodes_to_upgrade.keys()})

        # cleanup the unused attributes
        del item['path']
        del item['external_subtitles']

    # Make sure to keep only upgradable episode subtitles
    episodes_data = [x for x in episodes_data if 'upgradable' in x and x['upgradable']]

    count_episode_to_upgrade = len(episodes_data)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_episode_to_upgrade)

    if count_episode_to_upgrade == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    for i, episode in enumerate(episodes_data, start=1):
        providers_list = get_providers()

        jobs_queue.update_job_progress(job_id=job_id, progress_value=i,
                                       progress_message=f'{episode["seriesTitle"]} - S{episode["season"]:02d}E'
                                                        f'{episode["episode"]:02d} - {episode["title"]}')

        if not providers_list:
            logging.info("BAZARR All providers are throttled")
            return

        language, is_forced, is_hi = parse_language_string(episode['language'])
        if is_hi and not _is_hi_required(language, episode['profileId']):
            is_hi = 'False'

        audio_language_list = get_audio_profile_languages(episode['audio_language'])
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = 'None'

        result = list(generate_subtitles(path_mappings.path_replace(episode['video_path']),
                                         [(language, is_hi, is_forced)],
                                         audio_language,
                                         str(episode['sceneName']),
                                         episode['seriesTitle'],
                                         'series',
                                         episode['profileId'],
                                         forced_minimum_score=int(episode['score'] or 0) + 1,
                                         is_upgrade=True,
                                         previous_subtitles_to_delete=path_mappings.path_replace(
                                             episode['subtitles_path']),
                                         job_id=job_id))

        if result:
            if isinstance(result, list) and len(result):
                result = result[0]
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles(episode['video_path'], path_mappings.path_replace(episode['video_path']))
            history_log(3, episode['sonarrSeriesId'], episode['sonarrEpisodeId'], result,
                        upgraded_from_id=episode['original_id'])
            send_notifications(episode['sonarrSeriesId'], episode['sonarrEpisodeId'], result.message)
            event_stream(type="episode-history")
    jobs_queue.update_job_name(job_id=job_id, new_job_name='Tried to upgrade episodes subtitles')


def upgrade_movies_subtitles(job_id=None, radarr_ids=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Trying to upgrade movies subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    movies_to_upgrade = get_upgradable_movies_subtitles()

    query = select(TableHistoryMovie.id,
               TableMovies.title,
               TableHistoryMovie.language,
               TableMovies.audio_language,
               TableHistoryMovie.video_path,
               TableMovies.sceneName,
               TableHistoryMovie.score,
               TableHistoryMovie.radarrId,
               TableHistoryMovie.subtitles_path,
               TableMovies.path,
               TableMovies.profileId,
               TableMovies.subtitles.label('external_subtitles')) \
        .select_from(TableHistoryMovie) \
        .join(TableMovies, onclause=and_(TableHistoryMovie.radarrId == TableMovies.radarrId,
                                  TableHistoryMovie.arr_instance_id == TableMovies.arr_instance_id))

    if radarr_ids:
        query = query.where(TableHistoryMovie.radarrId.in_(radarr_ids))

    all_rows = database.execute(query).all()
    movies_data = []
    for x in all_rows:
        if not _language_still_desired(x.language, x.profileId):
            if x.id in movies_to_upgrade:
                logging.debug(f"Upgrade candidate {x.id} ({x.title}) dropped: language {x.language} no longer desired "  # noqa: G004
                              f"in profile {x.profileId}")
            continue
        if x.video_path != x.path:
            if x.id in movies_to_upgrade:
                logging.debug(f"Upgrade candidate {x.id} ({x.title}) dropped: video_path mismatch "  # noqa: G004
                              f"(history={x.video_path} != current={x.path})")
            continue
        movies_data.append({
            'id': x.id,
            'title': x.title,
            'language': x.language,
            'audio_language': x.audio_language,
            'video_path': x.video_path,
            'sceneName': x.sceneName,
            'score': x.score,
            'radarrId': x.radarrId,
            'path': x.path,
            'profileId': x.profileId,
            'subtitles_path': x.subtitles_path,
            'external_subtitles': [y[1] for y in ast.literal_eval(x.external_subtitles) if y[1]],
        })

    for item in movies_data:
        # do not consider subtitles that do not exist on disk anymore
        if item['subtitles_path'] not in item['external_subtitles']:
            # try to find a current subtitle for the same language (file may have been renamed/re-downloaded)
            current_sub = _find_current_subtitle_for_language(item['language'], item['external_subtitles'])
            if current_sub:
                logging.debug(f"Upgrade candidate {item['id']} ({item['title']}): history path no longer on disk, "  # noqa: G004
                              f"using current subtitle for same language ({current_sub})")
                item['subtitles_path'] = current_sub
            else:
                if item['id'] in movies_to_upgrade:
                    logging.debug(f"Upgrade candidate {item['id']} ({item['title']}) dropped: no subtitle for language "  # noqa: G004
                                  f"{item['language']} found on disk")
                continue

        # Mark upgradable and get original_id
        item.update({'original_id': movies_to_upgrade.get(item['id'])})
        item.update({'upgradable': item['id'] in movies_to_upgrade.keys()})

        # cleanup the unused attributes
        del item['path']
        del item['external_subtitles']

    # Make sure to keep only upgradable movie subtitles
    movies_data = [x for x in movies_data if 'upgradable' in x and x['upgradable']]

    count_movie_to_upgrade = len(movies_data)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_movie_to_upgrade)

    if count_movie_to_upgrade == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    for i, movie in enumerate(movies_data, start=1):
        providers_list = get_providers()

        jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_message=movie['title'])

        if not providers_list:
            logging.info("BAZARR All providers are throttled")
            return

        language, is_forced, is_hi = parse_language_string(movie['language'])
        if is_hi and not _is_hi_required(language, movie['profileId']):
            is_hi = 'False'

        audio_language_list = get_audio_profile_languages(movie['audio_language'])
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = 'None'

        result = list(generate_subtitles(path_mappings.path_replace_movie(movie['video_path']),
                                         [(language, is_hi, is_forced)],
                                         audio_language,
                                         str(movie['sceneName']),
                                         movie['title'],
                                         'movie',
                                         movie['profileId'],
                                         forced_minimum_score=int(movie['score'] or 0) + 1,
                                         is_upgrade=True,
                                         previous_subtitles_to_delete=path_mappings.path_replace_movie(
                                             movie['subtitles_path']),
                                         job_id=job_id))
        if result:
            if isinstance(result, list) and len(result):
                result = result[0]
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles_movie(movie['video_path'],
                                  path_mappings.path_replace_movie(movie['video_path']))
            history_log_movie(3, movie['radarrId'], result, upgraded_from_id=movie['original_id'])
            send_notifications_movie(movie['radarrId'], result.message)
            event_stream(type="movie-history")
    jobs_queue.update_job_name(job_id=job_id, new_job_name='Tried to upgrade movies subtitles')


def get_queries_condition_parameters():
    days_to_upgrade_subs = settings.general.days_to_upgrade_subs
    minimum_timestamp = (datetime.now() - timedelta(days=int(days_to_upgrade_subs)))

    # action=7 (EmbeddedSource) intentionally excluded: embedded subs are source
    # quality and should not be upgraded
    if settings.general.upgrade_manual:
        query_actions = [1, 2, 3, 4, 6]
    else:
        query_actions = [1, 3]

    return [minimum_timestamp, query_actions]


def _find_current_subtitle_for_language(language_string, external_subtitles):
    """Find a current subtitle file on disk that matches the language from history.

    When a subtitle was re-downloaded or renamed (e.g. .hu.hi.srt -> .hu.srt),
    the history still references the old path. This finds the current file for
    the same language so upgrades can still proceed.
    """
    lang_code = language_string.split(':')[0]
    is_hi = language_string.endswith(':hi')
    is_forced = language_string.endswith(':forced')

    for sub_path in external_subtitles:
        sub_lower = sub_path.lower()
        # Check if language code is in the filename
        if f'.{lang_code.lower()}.' not in sub_lower and not sub_lower.endswith(f'.{lang_code.lower()}'):
            continue
        # Match HI/forced flags
        has_hi = '.hi.' in sub_lower or sub_lower.endswith('.hi.srt')
        has_forced = '.forced.' in sub_lower or sub_lower.endswith('.forced.srt')
        if is_hi == has_hi and is_forced == has_forced:
            return sub_path
        # If we wanted HI but only non-HI exists (or vice versa), still a candidate
        if not is_forced and not has_forced and lang_code.lower() in sub_lower:
            return sub_path
    return None


def parse_language_string(language_string):
    if language_string.endswith('forced'):
        language = language_string.split(':')[0]
        is_forced = "True"
        is_hi = "False"
    elif language_string.endswith('hi'):
        language = language_string.split(':')[0]
        is_forced = "False"
        is_hi = "True"
    else:
        language = language_string.split(':')[0]
        is_forced = "False"
        is_hi = "False"

    return [language, is_forced, is_hi]


def get_upgradable_episode_subtitles(history_id_list=None):
    if not settings.general.upgrade_subs:
        # return an empty set of rows
        logging.debug("Subtitles upgrade is disabled so we wont go further.")
        return {}

    logging.debug("Determining upgradable episode subtitles")
    minimum_timestamp, query_actions = get_queries_condition_parameters()
    max_id_timestamp = select(TableHistory.video_path,
                              TableHistory.language,
                              func.max(TableHistory.timestamp).label('timestamp')) \
        .where(TableHistory.action.in_(query_actions)) \
        .group_by(TableHistory.video_path, TableHistory.language) \
        .distinct() \
        .subquery()

    logging.debug(f"Minimum timestamp used for subtitles upgrade: {minimum_timestamp}")  # noqa: G004
    logging.debug(f"These actions are considered for subtitles upgrade: {query_actions}")  # noqa: G004

    upgradable_episodes_conditions = [(TableHistory.action.in_(query_actions)),
                                      (TableHistory.timestamp > minimum_timestamp),
                                      or_(and_(TableHistory.score.is_(None), TableHistory.action == 6),
                                      (TableHistory.score < TableHistory.score_out_of - 3))]
    upgradable_episodes_conditions += get_exclusion_clause('series')
    subtitles_to_upgrade = database.execute(
        select(TableHistory.id,
               TableHistory.video_path,
               TableHistory.language,
               TableHistory.upgradedFromId)
        .select_from(TableHistory)
        .join(TableShows, onclause=and_(TableHistory.sonarrSeriesId == TableShows.sonarrSeriesId,
                                  TableHistory.arr_instance_id == TableShows.arr_instance_id))
        .join(TableEpisodes, onclause=and_(TableHistory.sonarrEpisodeId == TableEpisodes.sonarrEpisodeId,
                                     TableHistory.arr_instance_id == TableEpisodes.arr_instance_id))
        .join(max_id_timestamp, onclause=and_(TableHistory.video_path == max_id_timestamp.c.video_path,
                                              TableHistory.language == max_id_timestamp.c.language,
                                              max_id_timestamp.c.timestamp == TableHistory.timestamp))
        .where(reduce(operator.and_, upgradable_episodes_conditions))
        .order_by(TableHistory.timestamp.desc())) \
        .all()
    logging.debug(f"{len(subtitles_to_upgrade)} subtitles are candidates and we've selected the latest timestamp for "  # noqa: G004
                  f"each of them.")

    upgradable_episode_subtitles = {}
    for subtitle_to_upgrade in subtitles_to_upgrade:
        # exclude subtitles that are not in history_id_list if provided
        if history_id_list and subtitle_to_upgrade.id not in history_id_list:
            continue

        # exclude subtitles with ID that as been "upgraded from" and shouldn't be considered
        if any(x.upgradedFromId == subtitle_to_upgrade.id for x in subtitles_to_upgrade):
            logging.debug(f"TableHistory ID {subtitle_to_upgrade.id} is the original subtitles event and has already "  # noqa: G004
                          f"been upgraded so we'll skip it.")
            continue

        # check if we have the original subtitles ID in database
        if subtitle_to_upgrade.upgradedFromId:
            upgradable_episode_subtitles.update({subtitle_to_upgrade.id: subtitle_to_upgrade.upgradedFromId})
            logging.debug(f"The original subtitles ID for TableHistory ID {subtitle_to_upgrade.id} stored in DB is: "  # noqa: G004
                          f"{subtitle_to_upgrade.upgradedFromId}")
            continue
        else:
            upgradable_episode_subtitles.update({subtitle_to_upgrade.id: None})
            logging.debug(f"It seems to be the first time we've seen this TableHistory ID {subtitle_to_upgrade.id}.")  # noqa: G004
            continue

    logging.debug(f"We've found {len(upgradable_episode_subtitles)} episode subtitles IDs to be upgradable")  # noqa: G004
    return upgradable_episode_subtitles


def get_upgradable_movies_subtitles(history_id_list=None):
    if not settings.general.upgrade_subs:
        # return an empty set of rows
        logging.debug("Subtitles upgrade is disabled so we won't go further.")
        return {}

    logging.debug("Determining upgradable movie subtitles")
    minimum_timestamp, query_actions = get_queries_condition_parameters()
    max_id_timestamp = select(TableHistoryMovie.video_path,
                              TableHistoryMovie.language,
                              func.max(TableHistoryMovie.timestamp).label('timestamp')) \
        .where(TableHistoryMovie.action.in_(query_actions)) \
        .group_by(TableHistoryMovie.video_path, TableHistoryMovie.language) \
        .distinct() \
        .subquery()

    logging.debug(f"Minimum timestamp used for subtitles upgrade: {minimum_timestamp}")  # noqa: G004
    logging.debug(f"These actions are considered for subtitles upgrade: {query_actions}")  # noqa: G004

    upgradable_movies_conditions = [(TableHistoryMovie.action.in_(query_actions)),
                                    (TableHistoryMovie.timestamp > minimum_timestamp),
                                    or_(and_(TableHistoryMovie.score.is_(None), TableHistoryMovie.action == 6),
                                    (TableHistoryMovie.score < TableHistoryMovie.score_out_of - 3))]
    upgradable_movies_conditions += get_exclusion_clause('movie')
    subtitles_to_upgrade = database.execute(
        select(TableHistoryMovie.id,
               TableHistoryMovie.video_path,
               TableHistoryMovie.language,
               TableHistoryMovie.upgradedFromId)
        .select_from(TableHistoryMovie)
        .join(TableMovies, onclause=and_(TableHistoryMovie.radarrId == TableMovies.radarrId,
                                  TableHistoryMovie.arr_instance_id == TableMovies.arr_instance_id))
        .join(max_id_timestamp, onclause=and_(TableHistoryMovie.video_path == max_id_timestamp.c.video_path,
                                              TableHistoryMovie.language == max_id_timestamp.c.language,
                                              max_id_timestamp.c.timestamp == TableHistoryMovie.timestamp))
        .where(reduce(operator.and_, upgradable_movies_conditions))
        .order_by(TableHistoryMovie.timestamp.desc())) \
        .all()
    logging.debug(f"{len(subtitles_to_upgrade)} subtitles are candidates and we've selected the latest timestamp for "  # noqa: G004
                  f"each of them.")

    upgradable_movie_subtitles = {}
    for subtitle_to_upgrade in subtitles_to_upgrade:
        # exclude subtitles that are not in history_id_list if provided
        if history_id_list and subtitle_to_upgrade.id not in history_id_list:
            continue

        # exclude subtitles with ID that as been "upgraded from" and shouldn't be considered
        if any(x.upgradedFromId == subtitle_to_upgrade.id for x in subtitles_to_upgrade):
            logging.debug(f"TableHistoryMovie ID {subtitle_to_upgrade.id} is the original subtitles event and has "  # noqa: G004
                          f"already been upgraded so we'll skip it.")
            continue

        # check if we have the original subtitles ID in database
        if subtitle_to_upgrade.upgradedFromId:
            upgradable_movie_subtitles.update({subtitle_to_upgrade.id: subtitle_to_upgrade.upgradedFromId})
            logging.debug(f"The original subtitles ID for TableHistoryMovie ID {subtitle_to_upgrade.id} stored in DB "  # noqa: G004
                          f"is: {subtitle_to_upgrade.upgradedFromId}")
            continue
        else:
            upgradable_movie_subtitles.update({subtitle_to_upgrade.id: None})
            logging.debug(f"It seems to be the first time we've seen this TableHistoryMovie ID "  # noqa: G004
                          f"{subtitle_to_upgrade.id}.")
            continue

    logging.debug(f"We've found {len(upgradable_movie_subtitles)} movie subtitles IDs to be upgradable")  # noqa: G004
    return upgradable_movie_subtitles


def _language_still_desired(language, profile_id):
    if not profile_id:
        return False

    profile = get_profiles_list(profile_id)
    if profile and language in _language_from_items(profile['items']):
        return True
    else:
        return False


def _language_from_items(items):
    results = []
    for item in items:
        if item['forced'] == 'True':
            results.append(f'{item["language"]}:forced')
        elif item['hi'] == 'True':
            results.append(f'{item["language"]}:hi')
        else:
            results.append(item['language'])
            results.append(f'{item["language"]}:hi')
    return results


def _is_hi_required(language, profile_id):
    profile = get_profiles_list(profile_id=profile_id)
    for item in profile['items']:
        if language.split(':')[0] == item['language'] and item['hi'] == 'True':
            return True
    return False

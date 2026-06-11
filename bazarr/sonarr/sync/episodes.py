# coding=utf-8

import os
import logging
import operator
import semver

from sqlalchemy.exc import IntegrityError
from datetime import datetime
from functools import reduce

from constants import MINIMUM_VIDEO_SIZE
from app.database import database, TableShows, TableEpisodes, delete, update, insert, select, get_exclusion_clause
from app.config import settings
from utilities.path_mappings import path_mappings
from subtitles.indexer.series import store_subtitles, series_full_scan_subtitles  # noqa: F401
from subtitles.mass_download import episode_download_subtitles  # noqa: F401
from app.event_handler import event_stream
from sonarr.info import get_sonarr_info
from app.jobs_queue import jobs_queue
from app.notifier import send_notifications
from subtitles.adaptive_searching import is_search_active
from arr_instances.resolution import sonarr_series_owner, stamp_owner

from .parser import episodeParser
from .utils import get_episodes_from_sonarr_api, get_episodesFiles_from_sonarr_api

# map between booleans and strings in DB
bool_map = {"True": True, "False": False}

FEATURE_PREFIX = "SYNC_EPISODES "

# Max rows per batched INSERT. Each episode row binds ~19 values, so
# 50 stays well under SQLite's legacy SQLITE_MAX_VARIABLE_NUMBER (999)
# while still amortizing round-trip cost for typical full-season
# inserts on modern SQLite/PostgreSQL deployments.
EPISODE_INSERT_CHUNK_SIZE = 50


def trace(message):
    if settings.general.debug:
        logging.debug(FEATURE_PREFIX + message)  # noqa: G003


def get_episodes_monitored_table(series_id):
    episodes_monitored = database.execute(
        select(TableEpisodes.episode_file_id, TableEpisodes.monitored)
        .where(TableEpisodes.sonarrSeriesId == series_id))\
        .all()
    episode_dict = dict((x, y) for x, y in episodes_monitored)
    return episode_dict


def check_actual_file_size(original_episode_path):
    try:
        bazarr_file_size = \
            os.path.getsize(path_mappings.path_replace(original_episode_path))
    except OSError:
        bazarr_file_size = 0

    return bazarr_file_size > MINIMUM_VIDEO_SIZE


def sync_episodes(series_id, defer_search=False, is_signalr=False, episodes_data=None):
    """Sync one series' episodes between Sonarr and the local DB.

    The bulk update_series() caller pre-fetches episode lists in
    parallel and passes them in via `episodes_data` so we skip the
    serial per-series HTTP call. Single-series callers (signalr,
    manual refresh, the insert/update branches in update_one_series)
    omit it and we fetch on demand as before.
    """
    logging.debug('BAZARR Starting episodes sync from Sonarr for series ID %s.', series_id)
    apikey_sonarr = settings.sonarr.apikey

    # Get current episodes for the series in ONE narrow-column SELECT.
    # Pre-refactor this issued two adjacent queries: a small one for the
    # id list, then a `select(TableEpisodes)` that pulled the FULL row
    # (including the heavy ffprobe_cache blob) into a dict only used for
    # comparing against episodeParser output. Combine into one query
    # that fetches just the parser-output columns - the only fields the
    # comparison and the per-row update path actually read.
    if not series_id:
        return

    current_episodes_in_db_row_as_dict = {
        row.sonarrEpisodeId: {
            'sonarrSeriesId': row.sonarrSeriesId,
            'sonarrEpisodeId': row.sonarrEpisodeId,
            'title': row.title,
            'path': row.path,
            'season': row.season,
            'episode': row.episode,
            'sceneName': row.sceneName,
            'monitored': row.monitored,
            'format': row.format,
            'resolution': row.resolution,
            'video_codec': row.video_codec,
            'audio_codec': row.audio_codec,
            'episode_file_id': row.episode_file_id,
            'audio_language': row.audio_language,
            'file_size': row.file_size,
            'absoluteEpisode': row.absoluteEpisode,
            'tvdbId': row.tvdbId,
        }
        for row in database.execute(
            select(TableEpisodes.sonarrSeriesId,
                   TableEpisodes.sonarrEpisodeId,
                   TableEpisodes.title,
                   TableEpisodes.path,
                   TableEpisodes.season,
                   TableEpisodes.episode,
                   TableEpisodes.sceneName,
                   TableEpisodes.monitored,
                   TableEpisodes.format,
                   TableEpisodes.resolution,
                   TableEpisodes.video_codec,
                   TableEpisodes.audio_codec,
                   TableEpisodes.episode_file_id,
                   TableEpisodes.audio_language,
                   TableEpisodes.file_size,
                   TableEpisodes.absoluteEpisode,
                   TableEpisodes.tvdbId)
            .where(TableEpisodes.sonarrSeriesId == series_id)).all()
    }
    current_episodes_id_db_list = list(current_episodes_in_db_row_as_dict.keys())

    current_episodes_sonarr = []
    episodes_to_update = []
    episodes_to_add = []

    # Get episodes data for a series from Sonarr (or use the
    # pre-fetched payload from the bulk update_series() caller).
    if episodes_data is None:
        episodes = get_episodes_from_sonarr_api(apikey_sonarr=apikey_sonarr, series_id=series_id)
    else:
        episodes = episodes_data
    if episodes:
        sonarr_version = get_sonarr_info.semver()
        if sonarr_version and sonarr_version >= semver.Version(*(4, 0, 9, 2421)):
            # We skip this if the episodes already contain an episodeFile structure (added with Sonarr v4.0.9.2421)
            pass
        else:
            # For Sonarr lower than 4.0.9.2421, we need to update episodes to integrate the
            # episodeFile API endpoint results
            episodeFiles = get_episodesFiles_from_sonarr_api(apikey_sonarr=apikey_sonarr, series_id=series_id)
            if episodeFiles:
                for episode in episodes:
                    if episodeFiles and episode['hasFile']:
                        item = [x for x in episodeFiles if x['id'] == episode['episodeFileId']]
                        if item:
                            episode['episodeFile'] = item[0]

        sync_monitored = settings.sonarr.sync_only_monitored_series and settings.sonarr.sync_only_monitored_episodes
        if sync_monitored:
            episodes_monitored = get_episodes_monitored_table(series_id)
            skipped_count = 0

        for episode in episodes:
            if 'hasFile' in episode and episode['hasFile'] and 'episodeFile' in episode:
                if sync_monitored:
                    try:
                        monitored_status_db = bool_map[episodes_monitored[episode['episodeFileId']]]
                    except KeyError:
                        monitored_status_db = None

                    if monitored_status_db is None:
                        # not in db, might need to add, if we have a file on disk
                        pass
                    elif monitored_status_db != episode['monitored']:
                        # monitored status changed and we don't know about it until now
                        trace(f"(Monitor Status Mismatch) {episode['title']}")
                        # pass
                    elif not episode['monitored']:
                        # Add unmonitored episode in sonarr to current episode list, otherwise it will be deleted from db
                        current_episodes_sonarr.append(episode['id'])
                        skipped_count += 1
                        continue

                if (episode['episodeFile']['size'] > MINIMUM_VIDEO_SIZE or
                        check_actual_file_size(episode['episodeFile']['path']) or
                        (settings.general.enable_strm_support and episode['episodeFile']['path'].lower().endswith('.strm'))):
                    # Add episodes in sonarr to current episode list
                    current_episodes_sonarr.append(episode['id'])

                    # Parse episode data
                    if episode['id'] in current_episodes_in_db_row_as_dict:
                        parsed_episode = episodeParser(episode)
                        if not set(parsed_episode.items()).issubset(set(current_episodes_in_db_row_as_dict[episode['id']].items())):
                            episodes_to_update.append(parsed_episode)
                    else:
                        episodes_to_add.append(episodeParser(episode))
    else:
        return

    if sync_monitored:
        # try to avoid unnecessary database calls
        if settings.general.debug:
            series_title = database.execute(select(TableShows.title).where(TableShows.sonarrSeriesId == series_id)).first()[0]
            trace(f"Skipped {skipped_count} unmonitored episodes out of {len(episodes)} for {series_title}")

    # Remove old episodes from DB
    episodes_to_delete = list(set(current_episodes_id_db_list) - set(current_episodes_sonarr))

    rows_changed = False

    # Episodes inherit their owning instance (and local series ref) from their
    # parent series, resolved once here. Falls back to the default instance when
    # the parent is unstamped; leaves both unset for a pre-backfill install.
    owner_instance_id, parent_local_id = sonarr_series_owner(database, series_id)

    if len(episodes_to_delete):
        try:
            database.execute(delete(TableEpisodes).where(TableEpisodes.sonarrEpisodeId.in_(episodes_to_delete)))
        except IntegrityError as e:
            logging.error(f"BAZARR cannot delete episodes because of {e}")  # noqa: G004
        else:
            # Per-row episode delete events used to fire one socketio
            # packet per row here (5000 packets for an initial sync of a
            # 5000-episode library). The frontend only needs to know that
            # episodes for this series changed; we coalesce all per-row
            # episode events into a single series.update emit at the end.
            rows_changed = True

    # Insert new episodes in DB. Batch inserts in fixed-size chunks to
    # stay under bind-parameter limits (SQLite builds with the legacy
    # 999-variable limit fail at ~52 rows since each episode row binds
    # ~19 values, and the IntegrityError catch wouldn't recover from
    # the resulting OperationalError). On IntegrityError fall back to
    # per-row inserts for that chunk only - preserving the existing
    # convert-to-update recovery for individual conflicting rows.
    if len(episodes_to_add):
        insertion_timestamp = datetime.now()
        for added_episode in episodes_to_add:
            added_episode['created_at_timestamp'] = insertion_timestamp
            stamp_owner(added_episode, owner_instance_id)
            if parent_local_id is not None:
                added_episode['series_id'] = parent_local_id

        for chunk_start in range(0, len(episodes_to_add), EPISODE_INSERT_CHUNK_SIZE):
            chunk = episodes_to_add[chunk_start:chunk_start + EPISODE_INSERT_CHUNK_SIZE]
            try:
                database.execute(insert(TableEpisodes).values(chunk))
            except IntegrityError as batch_err:
                logging.debug('BAZARR batched episode insert failed (%s); '
                              'falling back to per-row insert with update recovery', batch_err)
                for added_episode in chunk:
                    try:
                        database.execute(insert(TableEpisodes).values(added_episode))
                    except IntegrityError as e:
                        logging.error(f"BAZARR cannot insert episodes because of {e}. We'll try to update it instead.")  # noqa: G004
                        del added_episode['created_at_timestamp']
                        episodes_to_update.append(added_episode)
                    else:
                        store_subtitles(added_episode['path'], path_mappings.path_replace(added_episode['path']))
                        rows_changed = True
            else:
                for added_episode in chunk:
                    store_subtitles(added_episode['path'], path_mappings.path_replace(added_episode['path']))
                    rows_changed = True

    # Update existing episodes in DB
    if len(episodes_to_update):
        for updated_episode in episodes_to_update:
            previous_episode_id = updated_episode['sonarrEpisodeId']
            # Read previous values from the cache built up at the top of
            # this function instead of re-querying. Pre-refactor this
            # loop ran an extra SELECT per updated episode just to read
            # episode_file_id + path - both are already in the dict.
            cached_previous = current_episodes_in_db_row_as_dict.get(previous_episode_id, {})
            previous_episode_file_id = cached_previous.get('episode_file_id')
            previous_episode_path = cached_previous.get('path')

            try:
                updated_episode['updated_at_timestamp'] = datetime.now()
                # Stamp AFTER the subset-diff above (line ~188) so the added
                # key never forces a spurious update on the next sync.
                stamp_owner(updated_episode, owner_instance_id)
                if parent_local_id is not None:
                    updated_episode['series_id'] = parent_local_id
                database.execute(update(TableEpisodes)
                                 .values(updated_episode)
                                 .where(TableEpisodes.sonarrEpisodeId == updated_episode['sonarrEpisodeId']))
            except IntegrityError as e:
                logging.error(f"BAZARR cannot update episodes because of {e}")  # noqa: G004
            else:
                if (previous_episode_file_id != updated_episode['episode_file_id'] or
                        previous_episode_path != updated_episode['path']):
                    # Store subtitles for updated episode where path or episode_file_id changed
                    logging.debug('BAZARR updating subtitles for episode %s', updated_episode["path"])
                    store_subtitles(updated_episode['path'], path_mappings.path_replace(updated_episode['path']))
                else:
                    logging.debug('BAZARR skipping subtitle update for episode %s as path '
                                  'and episode_file_id unchanged', updated_episode["path"])
                rows_changed = True

    # Downloading missing subtitles
    series_data = database.execute(
        select(TableShows.title,
               TableShows.year,
               TableShows.path)
        .where(TableShows.sonarrSeriesId == series_id)
    ).first()
    if not series_data:
        pass
    else:
        if defer_search:
            logging.debug(
                'BAZARR searching for missing subtitles is deferred until scheduled task execution for this series: '
                '%s (%s)', series_data.title, series_data.year)
        else:
            for episode in episodes_to_update + episodes_to_add:
                episode_title = (f'{series_data.title} - S{episode["season"]:02d}E{episode["episode"]:02d} '
                                 f'- {episode["title"]}')
                if _is_there_missing_subtitles(episode_id=episode['sonarrEpisodeId']):
                    if os.path.exists(path_mappings.path_replace(episode['path'])):
                        logging.debug('BAZARR downloading missing subtitles for this episode: %s', episode_title)
                        jobs_queue.feed_jobs_pending_queue(job_name=f'Downloading missing subtitles for '
                                                                    f'{episode_title}',
                                                           module='subtitles.mass_download.series',
                                                           func='episode_download_subtitles',
                                                           args=[],
                                                           kwargs={'no': episode['sonarrEpisodeId']},
                                                           is_signalr=is_signalr)
                    else:
                        logging.debug('BAZARR cannot find this episode file yet (Sonarr may be slow to import episode '
                                      'between disks?). Searching for missing subtitles is deferred until scheduled '
                                      'task execution for this episode: %s', episode_title)
                else:
                    if is_signalr and settings.general.notify_if_nothing_is_missing_for_signalr_event:
                        send_notifications(series_id, episode['sonarrEpisodeId'],
                                           "There are no missing subtitles in this episode.")
                    logging.debug('BAZARR no missing subtitles for this episode: %s', episode_title)

    # One coalesced socketio packet per series replaces what used to be
    # up to thousands of per-row episode events. The frontend already
    # handles series.update for refresh purposes (used elsewhere via
    # update_series). When nothing changed, stay silent.
    if rows_changed:
        event_stream(type='series', action='update', payload=int(series_id))

    logging.debug('BAZARR All episodes from series ID %s synced from Sonarr into database.', series_id)


def sync_one_episode(episode_id, defer_search=False, is_signalr=False):
    logging.debug('BAZARR syncing this specific episode from Sonarr: %s', episode_id)
    apikey_sonarr = settings.sonarr.apikey

    # Check if there's a row in database for this episode ID
    existing_episode = database.execute(
        select(TableEpisodes.path, TableEpisodes.episode_file_id)
        .where(TableEpisodes.sonarrEpisodeId == episode_id)) \
        .first()

    try:
        # Get episode data from sonarr api
        episode = None
        episode_data = get_episodes_from_sonarr_api(apikey_sonarr=apikey_sonarr, episode_id=episode_id)
        if not episode_data:
            return

        else:
            if existing_episode and episode_data['hasFile']:
                episode_data['episodeFile'] = \
                    get_episodesFiles_from_sonarr_api(apikey_sonarr=apikey_sonarr,
                                                      episode_file_id=episode_data['episodeFileId'])
            episode = episodeParser(episode_data)
    except Exception:
        logging.exception('BAZARR cannot get episode returned by SignalR feed from Sonarr API.')
        return

    # Drop useless events
    if not episode and not existing_episode:
        return

    # Resolve the owning instance + local series ref from the parent series for
    # the write branches below (episode is None only on the delete path, which
    # needs neither). Guarded so a pre-backfill install stays NULL.
    owner_instance_id = parent_local_id = None
    if episode:
        owner_instance_id, parent_local_id = sonarr_series_owner(database, episode['sonarrSeriesId'])

    # Remove episode from DB
    if not episode and existing_episode:
        try:
            database.execute(
                delete(TableEpisodes)
                .where(TableEpisodes.sonarrEpisodeId == episode_id))
        except IntegrityError as e:
            logging.error(f"BAZARR cannot delete episode {existing_episode.path} because of {e}")  # noqa: G004
        else:
            event_stream(type='episode', action='delete', payload=int(episode_id))
            logging.debug(
                'BAZARR deleted this episode from the database:%s', path_mappings.path_replace(existing_episode.path))
        return

    # Update existing episodes in DB
    elif episode and existing_episode:
        try:
            episode['updated_at_timestamp'] = datetime.now()
            stamp_owner(episode, owner_instance_id)
            if parent_local_id is not None:
                episode['series_id'] = parent_local_id
            database.execute(
                update(TableEpisodes)
                .values(episode)
                .where(TableEpisodes.sonarrEpisodeId == episode_id))
        except IntegrityError as e:
            logging.error(f"BAZARR cannot update episode {episode['path']} because of {e}")  # noqa: G004
        else:
            store_subtitles(episode['path'], path_mappings.path_replace(episode['path']))
            event_stream(type='episode', action='update', payload=int(episode_id))
            logging.debug(
                'BAZARR updated this episode into the database:%s', path_mappings.path_replace(episode["path"]))

    # Insert new episodes in DB
    elif episode and not existing_episode:
        try:
            episode['created_at_timestamp'] = datetime.now()
            stamp_owner(episode, owner_instance_id)
            if parent_local_id is not None:
                episode['series_id'] = parent_local_id
            database.execute(
                insert(TableEpisodes)
                .values(episode))
        except IntegrityError as e:
            logging.error(f"BAZARR cannot insert episode {episode['path']} because of {e}")  # noqa: G004
        else:
            store_subtitles(episode['path'], path_mappings.path_replace(episode['path']))
            event_stream(type='episode', action='update', payload=int(episode_id))
            logging.debug(
                'BAZARR inserted this episode into the database:%s', path_mappings.path_replace(episode["path"]))

    # Downloading missing subtitles
    if defer_search:
        logging.debug(
            'BAZARR searching for missing subtitles is deferred until scheduled task execution for this episode: '
            '%s', path_mappings.path_replace(episode["path"]))
    else:
        series_title = database.execute(
            select(TableShows.title)
            .where(TableShows.sonarrSeriesId == episode["sonarrSeriesId"])
        ).first()[0]
        episode_full_title = (f'{series_title} - S{episode["season"]:02d}E{episode["episode"]:02d} - '
                              f'{episode["title"]}')

        if os.path.exists(path_mappings.path_replace(episode["path"])):
            logging.debug('BAZARR downloading missing subtitles for this episode: %s', episode_full_title)
            if _is_there_missing_subtitles(episode_id=episode_id):
                jobs_queue.feed_jobs_pending_queue(job_name=f'Downloading missing subtitles for {series_title}',
                                                   module='subtitles.mass_download.series',
                                                   func='episode_download_subtitles',
                                                   args=[],
                                                   kwargs={'no': episode_id},
                                                   is_signalr=is_signalr)
            else:
                if is_signalr and settings.general.notify_if_nothing_is_missing_for_signalr_event:
                    send_notifications(episode["sonarrSeriesId"], episode_id,
                                       "There are no missing subtitles in this episode.")
                logging.debug('BAZARR no missing subtitles for this episode: %s', episode_full_title)
        else:
            logging.debug('BAZARR cannot find this file yet (Sonarr may be slow to import episode between disks?). '
                          'Searching for missing subtitles is deferred until scheduled task execution for this episode'
                          ': %s', episode_full_title)


def _is_there_missing_subtitles(series_id: int = None, episode_id: int = None) -> bool:
    """
    Determines whether there are missing subtitles for a given series or episode.

    This function checks if there are missing subtitles based on the given
    series ID or episode ID. If both `series_id` and `episode_id` are provided,
    or if neither is provided, the function returns False. Otherwise, it evaluates
    the specified conditions to determine if subtitles are missing for the
    requested series or episode.

    :param series_id: The ID of the series to check for missing subtitles.
        Optional, defaults to None.
    :param episode_id: The ID of the episode to check for missing subtitles.
        Optional, defaults to None.
    :return: Boolean indicating whether there are missing subtitles (`True`)
        or not (`False`).
    :rtype: bool
    """
    episodes_conditions = [(TableEpisodes.missing_subtitles.is_not(None)),
                           (TableEpisodes.missing_subtitles != '[]')]
    if all([series_id, episode_id]) or not any([series_id, episode_id]):
        return False
    elif series_id:
        episodes_conditions.append(TableEpisodes.sonarrSeriesId == series_id)
    elif episode_id:
        episodes_conditions.append(TableEpisodes.sonarrEpisodeId == episode_id)
    episodes_conditions += get_exclusion_clause('series')
    missing_episodes = database.execute(
        select(TableEpisodes.missing_subtitles, TableEpisodes.failedAttempts)
        .select_from(TableEpisodes)
        .join(TableShows)
        .where(reduce(operator.and_, episodes_conditions))) \
        .all()
    for missing_episode in missing_episodes:
        for language in missing_episode.missing_subtitles:
            if is_search_active(desired_language=language, attempt_string=missing_episode.failedAttempts):
                return True
    return False

# coding=utf-8

import logging
import gc

from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.exc import IntegrityError
from datetime import datetime

from app.config import settings
from subtitles.indexer.series import list_missing_subtitles  # noqa: F401
from sonarr.rootfolder import check_sonarr_rootfolder
from app.database import TableShows, TableLanguagesProfiles, database, insert, update, delete, select
from utilities.path_mappings import path_mappings
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from arr_instances.resolution import client_for_instance, default_instance_id, scoped, stamp_owner

from .episodes import sync_episodes
from .parser import seriesParser
from .utils import get_profile_list, get_tags, get_series_from_sonarr_api, get_episodes_from_sonarr_api

# map between booleans and strings in DB
bool_map = {"True": True, "False": False}

FEATURE_PREFIX = "SYNC_SERIES "

# Concurrency for the parallel episode prefetch in the bulk
# update_series() loop. Sonarr's GET /api/v3/episode endpoint is the
# dominant wall-clock cost of a full sync (one HTTP round-trip per
# series); 8 workers compresses that without overwhelming a typical
# home Sonarr instance.
SONARR_PREFETCH_WORKERS = 8


def trace(message):
    if settings.general.debug:
        logging.debug(FEATURE_PREFIX + message)  # noqa: G003


def get_language_profiles():
    return database.execute(
        select(TableLanguagesProfiles.profileId, TableLanguagesProfiles.name, TableLanguagesProfiles.tag)).all()


def get_series_monitored_table(arr_instance_id=None):
    # Scope to the owning instance: two instances can share a sonarrSeriesId, so
    # an unscoped lookup collapses colliding ids (last writer wins) and would
    # hand sync_only_monitored_series another instance's monitored status.
    series_monitored = database.execute(
        scoped(select(TableShows.sonarrSeriesId, TableShows.monitored),
               TableShows.arr_instance_id, arr_instance_id))\
        .all()
    series_dict = dict((x, y) for x, y in series_monitored)
    return series_dict


def update_series(job_id=None, wait_for_completion=False, arr_instance_id=None, arr_client=None):
    # arr_instance_id/arr_client thread an instance identity through the sync.
    # Both None = today's exact default-instance path (byte-identical): the leaf
    # fetchers fall back to the scalar settings.sonarr path, and writes stamp the
    # resolved default. A client routes HTTP through that instance instead.
    if not job_id:
        jobs_queue.add_job_from_function("Syncing series with Sonarr", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    # Update root folders and update their health status
    check_sonarr_rootfolder(arr_instance_id=arr_instance_id, arr_client=arr_client)

    # Get shows data from Sonarr
    try:
        series = get_series_from_sonarr_api(apikey_sonarr=settings.sonarr.apikey, arr_client=arr_client)
    except Exception as e:
        logging.exception(f"BAZARR Error trying to get series from Sonarr: {e}")  # noqa: G004
        return
    else:
        # Hoist invariants out of the per-series loop. update_one_series
        # used to call get_profile_list / get_tags / get_language_profiles
        # internally - that produced 3N redundant calls (2 HTTP + 1 SQL
        # per show) for a sync that only needs them once. Fetch here and
        # pass through. Single-series callers (signalr, manual refresh
        # buttons) keep the lazy-fetch path inside update_one_series for
        # backwards compat.
        audio_profiles = get_profile_list()
        tags_dict = get_tags(arr_client=arr_client)
        language_profiles = get_language_profiles()

        # Get current shows in DB (scoped to this instance when instance-synced,
        # so removed-series computation never sees another instance's shows).
        current_shows_db = set(database.execute(
            scoped(select(TableShows.sonarrSeriesId),
                   TableShows.arr_instance_id, arr_instance_id)).scalars().all())

        current_shows_sonarr = set()

        series_count = len(series)
        skipped_count = 0

        series_monitored = None
        if settings.sonarr.sync_only_monitored_series:
            # Get current series monitored status in DB, scoped to this instance
            series_monitored = get_series_monitored_table(arr_instance_id=arr_instance_id)

        trace(f"Starting sync for {series_count} shows")

        # Pass 1: apply monitoring filters and decide which series we
        # will actually process. Doing this up-front lets the parallel
        # episode prefetch only fan out HTTP requests for the kept
        # series, and keeps the trace messages aligned with Sonarr's
        # original ordering.
        shows_to_process = []
        for i, show in enumerate(series, start=1):
            if settings.sonarr.sync_only_monitored_series:
                monitored_status_db = series_monitored.get(show['id'])
                if monitored_status_db is not None:
                    monitored_status_db = bool_map.get(monitored_status_db)
                if monitored_status_db is None:
                    # not in db, need to add
                    pass
                elif monitored_status_db != show['monitored']:
                    # monitored status changed and we don't know about it until now
                    trace(f"{i}: (Monitor Status Mismatch) {show['title']}")
                    # pass
                elif not show['monitored']:
                    # Add unmonitored series in sonarr to current series list, otherwise it will be deleted from db
                    trace(f"{i}: (Skipped Unmonitored) {show['title']}")
                    current_shows_sonarr.add(show['id'])
                    skipped_count += 1
                    continue

            current_shows_sonarr.add(show['id'])
            shows_to_process.append((i, show))

        process_count = len(shows_to_process)
        jobs_queue.update_job_progress(job_id=job_id, progress_max=process_count)

        # Pass 2: parallel-prefetch the per-series episode lists from
        # Sonarr (the dominant wall-clock cost of a full sync) while
        # walking the kept series serially. update_one_series runs in
        # the main thread so DB writes stay single-threaded; only the
        # GET /api/v3/episode HTTP calls are fanned out.
        apikey_sonarr = settings.sonarr.apikey
        with ThreadPoolExecutor(max_workers=SONARR_PREFETCH_WORKERS,
                                thread_name_prefix='bazarr-sonarr-prefetch') as executor:
            episode_futures = {
                show['id']: executor.submit(get_episodes_from_sonarr_api,
                                            apikey_sonarr=apikey_sonarr,
                                            series_id=show['id'],
                                            arr_client=arr_client)
                for _, show in shows_to_process
            }

            for processed_index, (orig_index, show) in enumerate(shows_to_process, start=1):
                jobs_queue.update_job_progress(job_id=job_id, progress_value=processed_index,
                                               progress_message=show['title'])
                trace(f"{orig_index}: (Processing) {show['title']}")

                # Update series row in DB - reuse the show payload we
                # already have from the bulk Sonarr fetch and the cached
                # existence answer from current_shows_db so the helper
                # doesn't re-issue a SELECT or per-series GET. The bulk
                # caller drives episode sync explicitly below using the
                # pre-fetched payload, so suppress the helper's internal
                # sync_episodes call.
                update_one_series(show['id'], action='updated', series_data=show,
                                  audio_profiles=audio_profiles, tags_dict=tags_dict,
                                  language_profiles=language_profiles,
                                  existing_in_db=show['id'] in current_shows_db,
                                  skip_episode_sync=True,
                                  arr_instance_id=arr_instance_id, arr_client=arr_client)

                try:
                    episodes_data = episode_futures[show['id']].result()
                except Exception:
                    logging.exception(f"BAZARR error pre-fetching episodes for series {show['id']}")  # noqa: G004
                    episodes_data = None

                sync_episodes(series_id=show['id'], episodes_data=episodes_data,
                              arr_instance_id=arr_instance_id, arr_client=arr_client)

        # Calculate series to remove from DB
        removed_series = current_shows_db - current_shows_sonarr

        for removed_series_id in removed_series:
            # Remove series from DB - we know it exists in DB (it came
            # from current_shows_db) so skip the existence SELECT.
            update_one_series(removed_series_id, action='deleted', existing_in_db=True,
                              arr_instance_id=arr_instance_id, arr_client=arr_client)

        if settings.sonarr.sync_only_monitored_series:
            trace(f"skipped {skipped_count} unmonitored series out of {series_count}")

        logging.debug('BAZARR All series synced from Sonarr into database.')

    jobs_queue.update_job_name(job_id=job_id, new_job_name="Synced series with Sonarr")

    gc.collect()


def update_series_for_instance(arr_instance_id, job_id=None, wait_for_completion=False):
    """Bulk-sync one Sonarr instance (scheduler fan-out entry).

    Mirrors update_series's enqueue-then-run so only the int arr_instance_id
    travels through the job queue (add_job_from_function captures this frame's
    locals); the per-instance ArrClient is built on the real run, never queued.
    A missing/disabled instance is skipped.
    """
    if not job_id:
        jobs_queue.add_job_from_function(f"Syncing series with Sonarr (instance {arr_instance_id})",
                                         is_progress=True, wait_for_completion=wait_for_completion)
        return
    arr_client = client_for_instance(database, arr_instance_id)
    if arr_client is None:
        logging.warning('BAZARR skipping Sonarr sync for unknown/disabled instance %s', arr_instance_id)
        return
    update_series(job_id=job_id, arr_instance_id=arr_instance_id, arr_client=arr_client)


def update_one_series_for_instance(arr_instance_id, series_id, action, **kwargs):
    """Single-series sync scoped to one instance (webhook/signalr entry)."""
    arr_client = client_for_instance(database, arr_instance_id)
    if arr_client is None:
        logging.warning('BAZARR skipping Sonarr series %s for unknown instance %s',
                        series_id, arr_instance_id)
        return
    update_one_series(series_id, action, arr_instance_id=arr_instance_id,
                      arr_client=arr_client, **kwargs)


def update_one_series(series_id, action, is_signalr=False, series_data=None,
                      audio_profiles=None, tags_dict=None, language_profiles=None,
                      existing_in_db=None, skip_episode_sync=False,
                      arr_instance_id=None, arr_client=None):
    """Update or delete one series in the DB.

    Optional injected arguments let the bulk `update_series()` caller
    skip the per-series Sonarr HTTP fetch and the three invariant
    profile / tag / language-profile lookups. `existing_in_db` lets
    the bulk caller answer the row-exists question from the cached
    `current_shows_db` set rather than re-issuing N SELECTs (and the
    fallback only fetches the indexed PK column rather than the full
    row). `skip_episode_sync` lets the bulk caller drive episode sync
    itself - typically with pre-fetched episode payloads - so the
    helper does not start its own serial HTTP fetch. Single-series
    callers (signalr feed, frontend "sync now" button, deletion path)
    call with only series_id + action and the helper falls back to
    the inline behavior.
    """
    logging.debug('BAZARR syncing this specific series from Sonarr: %s', series_id)

    # Check if there's a row in database for this series ID. The
    # bulk caller already knows the answer from `current_shows_db`;
    # otherwise fetch only the indexed PK column instead of the full
    # row (the rest of this function only uses the boolean answer).
    if existing_in_db is None:
        existing_in_db = database.execute(
            scoped(select(TableShows.sonarrSeriesId)
                   .where(TableShows.sonarrSeriesId == series_id),
                   TableShows.arr_instance_id, arr_instance_id)).scalar() is not None

    # Delete series from DB
    if action == 'deleted' and existing_in_db:
        database.execute(
            scoped(delete(TableShows)
                   .where(TableShows.sonarrSeriesId == int(series_id)),
                   TableShows.arr_instance_id, arr_instance_id))

        event_stream(type='series', action='delete', payload=int(series_id))
        return

    if settings.general.serie_default_enabled is True:
        serie_default_profile = settings.general.serie_default_profile
        if serie_default_profile == '':
            serie_default_profile = None
    else:
        serie_default_profile = None

    # Fetch invariants only when the bulk caller didn't pre-load them.
    if audio_profiles is None:
        audio_profiles = get_profile_list()
    if tags_dict is None:
        tags_dict = get_tags()
    if language_profiles is None:
        language_profiles = get_language_profiles()

    # series_data: the bulk caller passes the show dict from the
    # complete-list Sonarr API response so we don't re-issue
    # GET /api/v3/series/{id} N times. When it's missing (signalr
    # feed, manual refresh) we fetch the single record on demand.
    if series_data is None:
        try:
            series_data_list = get_series_from_sonarr_api(apikey_sonarr=settings.sonarr.apikey,
                                                          sonarr_series_id=int(series_id),
                                                          arr_client=arr_client)
        except Exception:
            logging.exception(f'BAZARR cannot get series with ID {series_id} from Sonarr API.')  # noqa: G004
            return
        if not series_data_list:
            return
        series_payload = series_data_list[0]
    else:
        series_payload = series_data

    # Resolve the owning instance once: the explicit instance when this sync is
    # instance-scoped, else the enabled default for the single-instance path.
    # stamp_owner leaves the column unset when neither exists (pre-backfill),
    # preserving legacy NULL behaviour.
    instance_id = arr_instance_id if arr_instance_id is not None \
        else default_instance_id(database, 'sonarr')

    if action == 'updated' and existing_in_db:
        # Update existing series in DB
        series = seriesParser(series_payload, action='update', tags_dict=tags_dict,
                              language_profiles=language_profiles,
                              serie_default_profile=serie_default_profile,
                              audio_profiles=audio_profiles)
        try:
            series['updated_at_timestamp'] = datetime.now()
            stamp_owner(series, instance_id)
            database.execute(
                scoped(update(TableShows)
                       .values(series)
                       .where(TableShows.sonarrSeriesId == series['sonarrSeriesId']),
                       TableShows.arr_instance_id, arr_instance_id))
        except IntegrityError as e:
            logging.error(f"BAZARR cannot update series {series['path']} because of {e}")  # noqa: G004
        else:
            if not is_signalr and not skip_episode_sync:
                # Sonarr emit two SignalR events when episodes must be refreshed.
                # The one that gets there doesn't include the episodeChanged flag.
                # The episodes are synced only when this function is called from the
                # frontend sync button in the episodes' page.
                sync_episodes(series_id=int(series_id),
                              arr_instance_id=arr_instance_id, arr_client=arr_client)
            event_stream(type='series', action='update', payload=int(series_id))
            logging.debug(
                'BAZARR updated this series into the database:%s', path_mappings.path_replace(series["path"]))
    elif action == 'updated' and not existing_in_db:
        # Insert new series in DB
        series = seriesParser(series_payload, action='insert', tags_dict=tags_dict,
                              language_profiles=language_profiles,
                              serie_default_profile=serie_default_profile,
                              audio_profiles=audio_profiles)

        try:
            series['created_at_timestamp'] = datetime.now()
            stamp_owner(series, instance_id)
            database.execute(
                insert(TableShows)
                .values(series))
        except IntegrityError as e:
            logging.error(f"BAZARR cannot insert series {series['path']} because of {e}")  # noqa: G004
        else:
            if not is_signalr and not skip_episode_sync:
                # Newly inserted series have zero episodes in the DB; the
                # bulk update_series() loop relies on this call to
                # populate them. signalr callers get their own episode
                # events from Sonarr and skip this path.
                sync_episodes(series_id=int(series_id),
                              arr_instance_id=arr_instance_id, arr_client=arr_client)
            event_stream(type='series', action='update', payload=int(series_id))
            logging.debug(
                'BAZARR inserted this series into the database:%s', path_mappings.path_replace(series["path"]))

# coding=utf-8

import logging

import json

from sqlalchemy import func

from app.config import settings
from app.database import (TableShowsRootfolder, TableMoviesRootfolder, TableLanguagesProfiles, database, select,
                          TableShows, TableMovies)
from app.event_handler import event_stream
from app.jobs_queue import jobs_queue
from utilities.path_mappings import path_mappings
from sonarr.rootfolder import check_sonarr_rootfolder
from radarr.rootfolder import check_radarr_rootfolder
from arr_instances.repository import ArrInstanceRepository
from arr_instances.resolution import client_for_instance


def check_health(job_id=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Checking Health", is_progress=False, wait_for_completion=wait_for_completion)
        return

    # Re-validate rootfolders per enabled instance. Mirrors the scheduler's
    # fan-out: a single default instance keeps the unscoped scalar-client path
    # (byte-identical); with multiple instances each is checked against its own
    # server, scoped, so one instance's health check can't fetch the wrong
    # server's rootfolders or delete another instance's rows. (#156)
    repo = ArrInstanceRepository(database)
    if settings.general.use_sonarr:
        sonarr_instances = repo.list('sonarr', enabled_only=True)
        if len(sonarr_instances) > 1:
            for inst in sonarr_instances:
                check_sonarr_rootfolder(arr_instance_id=inst.id,
                                        arr_client=client_for_instance(database, inst.id))
        else:
            check_sonarr_rootfolder()
    if settings.general.use_radarr:
        radarr_instances = repo.list('radarr', enabled_only=True)
        if len(radarr_instances) > 1:
            for inst in radarr_instances:
                check_radarr_rootfolder(arr_instance_id=inst.id,
                                        arr_client=client_for_instance(database, inst.id))
        else:
            check_radarr_rootfolder()
    event_stream(type='badges')

    from .backup import backup_rotation
    backup_rotation()

    jobs_queue.update_job_name(job_id=job_id, new_job_name="Checked Health")


def _any_instance_default_profile(kind):
    """True when some instance of ``kind`` supplies a default profile of its own.

    The global default is not the only source any more: media synced by an
    instance with an override gets that profile, so reporting "you must assign a
    profile" while one is configured sends the user to fix something that is
    already set up. Never raises: a health check that throws takes the whole
    page with it.
    """
    try:
        from arr_instances.media_defaults import instance_default_profile, read_media_defaults
        from arr_instances.repository import ArrInstanceRepository

        for row in ArrInstanceRepository(database).list(kind=kind):
            has_override, profile = instance_default_profile(read_media_defaults(row.options))
            if has_override and profile is not None:
                return True
    except Exception:
        logging.exception("BAZARR could not read the per-instance default language profiles")

    return False


def series_default_profile_is_missing():
    """The global series default is enabled but nothing supplies a profile."""
    if not settings.general.serie_default_enabled:
        return False
    if settings.general.serie_default_profile != '':
        return False
    return not _any_instance_default_profile('sonarr')


def movie_default_profile_is_missing():
    if not settings.general.movie_default_enabled:
        return False
    if settings.general.movie_default_profile != '':
        return False
    return not _any_instance_default_profile('radarr')


def get_health_issues():
    # this function must return a list of dictionaries consisting of to keys: object and issue
    health_issues = []

    # get Sonarr rootfolder issues
    if settings.general.use_sonarr:
        rootfolder = database.execute(
            select(TableShowsRootfolder.path,
                   TableShowsRootfolder.accessible,
                   TableShowsRootfolder.error)
            .where(TableShowsRootfolder.accessible == 0)) \
            .all()
        for item in rootfolder:
            health_issues.append({'object': path_mappings.path_replace(item.path),  # noqa: PERF401
                                  'issue': item.error})

    # get Radarr rootfolder issues
    if settings.general.use_radarr:
        rootfolder = database.execute(
            select(TableMoviesRootfolder.path,
                   TableMoviesRootfolder.accessible,
                   TableMoviesRootfolder.error)
            .where(TableMoviesRootfolder.accessible == 0)) \
            .all()
        for item in rootfolder:
            health_issues.append({'object': path_mappings.path_replace_movie(item.path),  # noqa: PERF401
                                  'issue': item.error})

    # get languages profiles duplicate ids issues when there's a cutoff set
    languages_profiles = database.execute(
        select(TableLanguagesProfiles.items, TableLanguagesProfiles.name, TableLanguagesProfiles.cutoff)).all()
    for languages_profile in languages_profiles:
        if not languages_profile.cutoff:
            # ignore profiles that don't have a cutoff set
            continue
        languages_profile_ids = []
        for items in json.loads(languages_profile.items):
            if items['id'] in languages_profile_ids:
                health_issues.append({'object': languages_profile.name,
                                      'issue': 'This languages profile has duplicate IDs. You need to edit this profile'
                                               ' and make sure to select the proper cutoff if required.'})
                break
            else:
                languages_profile_ids.append(items['id'])

    # check if there's at least one languages profile created
    languages_profiles_count = database.execute(select(func.count(TableLanguagesProfiles.profileId))).scalar()
    series_with_profile = database.execute(select(func.count(TableShows.sonarrSeriesId))
                                           .where(TableShows.profileId.is_not(None))).scalar()
    movies_with_profile = database.execute(select(func.count(TableMovies.radarrId))
                                           .where(TableMovies.profileId.is_not(None))).scalar()
    default_series_profile_empty = series_default_profile_is_missing()
    default_movies_profile_empty = movie_default_profile_is_missing()
    if languages_profiles_count == 0:
        health_issues.append({'object': 'Missing languages profile',
                              'issue': 'You must create at least one languages profile and assign it to your content.'})
    elif languages_profiles_count > 0 and ((settings.general.use_sonarr and series_with_profile == 0 and default_series_profile_empty) or
                                           (settings.general.use_radarr and movies_with_profile == 0 and default_movies_profile_empty)):
        health_issues.append({'object': 'No assigned languages profile',
                              'issue': 'Although you have created at least one languages profile, you must assign it '
                                       'to your content.'})

    return health_issues

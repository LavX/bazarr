# coding=utf-8
# fmt: off

import os
import sys
import logging
from functools import partial
from media_servers.events import publication_callback, observe_subtitle_change

from subzero.language import Language
from subliminal_patch.core import save_subtitles
from subliminal_patch.subtitle import Subtitle
from subliminal_patch.score import MAX_SCORES
from pysubs2.formats import get_format_identifier

from languages.get_languages import language_from_alpha3, alpha2_from_alpha3, alpha3_from_alpha2
from app.config import settings
from utilities.helper import get_target_folder, force_unicode
from utilities.post_processing import pp_replace, set_chmod
from utilities.path_mappings import path_mappings
from radarr.history import history_log_movie
from radarr.notify import notify_radarr
from sonarr.history import history_log
from arr_instances.resolution import scoped, client_for_instance
from sonarr.notify import notify_sonarr
from languages.custom_lang import CustomLanguage
from app.database import (TableEpisodes, TableMovies, TableShows, TableSportsEvents,
                         TableSportsLeagues,
                         get_profiles_list, get_audio_profile_languages,
                          database, select)
from app.jobs_queue import jobs_queue
from app.event_handler import event_stream
from app.notifier import send_notifications
from app.notifier import send_notifications_movie
from app.notifier import send_notifications_sports
from subtitles.processing import ProcessSubtitlesResult
from subtitles.tools.subsync_engines import (SubtitlePublication, write_subtitle_file,
                                            subtitle_source_version, subtitle_write_locks)

from .sync import sync_subtitles, _index_keep_all_outputs
from .post_processing import postprocessing
from plex.operations import plex_set_movie_added_date_now, plex_set_episode_added_date_now, plex_refresh_item
from jellyfin.operations import jellyfin_refresh_item


def _refresh_uploaded_subtitles(video_path, subtitle_path, sonarr_series_id=None, sonarr_episode_id=None,
                                radarr_id=None, arr_instance_id=None):
    _index_keep_all_outputs(video_path, sonarr_series_id=sonarr_series_id,
                            sonarr_episode_id=sonarr_episode_id, radarr_id=radarr_id,
                            arr_instance_id=arr_instance_id)


def _notify_upload(consumer, callback, *args, **kwargs):
    try:
        callback(*args, **kwargs)
    except Exception as exc:
        logging.warning('BAZARR upload notification failed for %s (%s)', consumer, type(exc).__name__)


def _refresh_upload_consumers(media_type, metadata, arr_instance_id):
    callbacks = []
    if media_type == 'sports':
        # Nothing to notify: Sportarr offers only an untargeted whole-library
        # scan, and the media-server refreshes key on an imdbId a sports event
        # has not got. The event re-index is what makes the upload visible.
        return
    if media_type == 'series':
        callbacks.append(('Sonarr', lambda: notify_sonarr(
            metadata.sonarrSeriesId,
            arr_client=client_for_instance(database, arr_instance_id, enabled_only=False))))
        if settings.general.use_plex and settings.plex.update_series_library:
            callbacks.append(('Plex', lambda: plex_refresh_item(
                metadata.imdbId, is_movie=False, season=metadata.season, episode=metadata.episode)))
        if settings.general.use_jellyfin and settings.jellyfin.update_series_library:
            callbacks.append(('Jellyfin', lambda: jellyfin_refresh_item(
                metadata.imdbId, is_movie=False, season=metadata.season, episode=metadata.episode,
                tvdb_id=metadata.tvdbId)))
    else:
        callbacks.append(('Radarr', lambda: notify_radarr(
            metadata.radarrId,
            arr_client=client_for_instance(database, arr_instance_id, enabled_only=False))))
        if settings.general.use_plex and settings.plex.update_movie_library:
            callbacks.append(('Plex', lambda: plex_refresh_item(metadata.imdbId, is_movie=True)))
        if settings.general.use_jellyfin and settings.jellyfin.update_movie_library:
            callbacks.append(('Jellyfin', lambda: jellyfin_refresh_item(
                metadata.imdbId, is_movie=True, tmdb_id=metadata.tmdbId)))
    for consumer, callback in callbacks:
        try:
            callback()
        except Exception as exc:
            logging.warning('BAZARR upload refresh failed for %s (%s)', consumer, type(exc).__name__)


def _profile_original_format(profile_id):
    """Whether this profile keeps the uploaded subtitle in its original format.

    get_profiles_list only returns a profile when the id resolves to one. A NULL
    profileId (the column is nullable, and the media editor sets it to nothing)
    makes it return the whole profile LIST, and an id that no longer resolves
    makes it return None, so subscripting the answer raised TypeError and lost
    the upload behind the endpoint's 204.

    No usable profile means no preference to honour, so the answer is False: the
    subtitle is converted to srt, which is what every profile does until someone
    turns originalFormat on. Uploading is a deliberate act on a file the user
    chose, and refusing it because the media carries no profile would be a worse
    answer than saving it in the default format.
    """
    profile = get_profiles_list(profile_id)
    if not isinstance(profile, dict):
        return False
    return bool(profile["originalFormat"])


def manual_upload_subtitle(path, language, forced, hi, media_type, subtitle, filename, audio_language, job_id=None,
                           sonarrSeriesId=None, sonarrEpisodeId=None, radarrId=None, arr_instance_id=None,
                           sportsEventId=None):
    if not job_id:
        return jobs_queue.add_job_from_function(f"Uploading {filename}", is_progress=False)

    logging.debug(f'BAZARR Manually uploading subtitles: {filename}')  # noqa: G004

    single = settings.general.single_language

    # Resolve post-processing against the owning instance (#227) so a per-instance
    # override applies to manual uploads too, matching the download path. Manual
    # uploads are not score-gated, so the threshold values are ignored here.
    from subtitles.processing import _postprocessing_config
    use_postprocessing, postprocessing_cmd, _, _ = _postprocessing_config(
        media_type, arr_instance_id)

    chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
        'win') and settings.general.chmod_enabled else None

    language = alpha3_from_alpha2(language)

    custom = CustomLanguage.from_value(language, "alpha3")
    if custom is None:
        lang_obj = Language(language)
    else:
        lang_obj = custom.subzero_language()

    if hi:
        lang_obj = Language.rebuild(lang_obj, hi=True)

    if forced:
        lang_obj = Language.rebuild(lang_obj, forced=True)

    sports_metadata = None
    if media_type == 'sports':
        # The language profile lives on the LEAGUE, not the event, so it has
        # to be joined; an event carries no profileId column of its own.
        sports_metadata = database.execute(scoped(
            select(TableSportsEvents.id, TableSportsLeagues.profileId)
            .select_from(TableSportsEvents)
            .join(TableSportsLeagues,
                  TableSportsLeagues.id == TableSportsEvents.league_id)
            .where(TableSportsEvents.id == sportsEventId),
            TableSportsEvents.arr_instance_id, arr_instance_id)) \
            .first()

        if sports_metadata:
            sportsEventId = sports_metadata.id
            use_original_format = bool(
                get_profiles_list(sports_metadata.profileId)["originalFormat"]
            ) if sports_metadata.profileId else False
        else:
            return
    elif media_type == 'series':
        episode_metadata = database.execute(scoped(
            select(TableEpisodes.sonarrSeriesId,
                   TableEpisodes.sonarrEpisodeId,
                   TableEpisodes.season,
                   TableEpisodes.episode,
                   TableShows.profileId,
                   TableShows.imdbId,
                   TableShows.tvdbId)
            .select_from(TableEpisodes)
            .join(TableShows)
            .where(TableEpisodes.sonarrEpisodeId == sonarrEpisodeId),
            TableEpisodes.arr_instance_id, arr_instance_id)) \
            .first()

        if episode_metadata:
            sonarrSeriesId = episode_metadata.sonarrSeriesId
            sonarrEpisodeId = episode_metadata.sonarrEpisodeId
            use_original_format = _profile_original_format(episode_metadata.profileId)
        else:
            return
    else:
        movie_metadata = database.execute(scoped(
            select(TableMovies.radarrId, TableMovies.profileId,
                   TableMovies.imdbId, TableMovies.tmdbId)
            .where(TableMovies.radarrId == radarrId),
            TableMovies.arr_instance_id, arr_instance_id)) \
            .first()

        if movie_metadata:
            radarrId = movie_metadata.radarrId
            use_original_format = _profile_original_format(movie_metadata.profileId)
        else:
            return

    audio_language = get_audio_profile_languages(audio_language)
    if len(audio_language) and isinstance(audio_language[0], dict):
        audio_language = audio_language[0]
    else:
        audio_language = {'name': '', 'code2': '', 'code3': ''}

    from subtitles.tools.mods import get_subzero_mods
    sub = Subtitle(
        lang_obj,
        mods=get_subzero_mods(arr_instance_id),
        original_format=use_original_format
    )

    sub.content = subtitle.getvalue()
    if not sub.is_valid():
        logging.exception(f'BAZARR Invalid subtitle file: {filename}')  # noqa: G004
        sub.mods = None

    if settings.general.utf8_encode:
        sub.set_encoding("utf-8")

    try:
        sub.format = (get_format_identifier(os.path.splitext(filename)[1]),)
    except Exception:
        pass

    saved_subtitles = []
    try:
        # ensure that formats must be a tuple of strings
        sub_format = (sub.format,) if isinstance(sub.format, str) else sub.format
        subtitle_directory = get_target_folder(path)
        with subtitle_write_locks(path, os.path.join(subtitle_directory or os.path.dirname(path), '.destination')):
            written_paths = []
            saved_subtitles = save_subtitles(path,
                                            [sub],
                                            single=single,
                                            tags=None,  # fixme
                                            directory=subtitle_directory,
                                            chmod=chmod,
                                            formats=sub_format if use_original_format else ("srt",),
                                            path_decoder=force_unicode,
                                            write_subtitle=partial(
                                                write_subtitle_file, path, written_paths=written_paths,
                                                on_publish=publication_callback(media_type, path, 'upload', arr_instance_id)))
            saved_subtitles = [saved for saved in saved_subtitles if saved.storage_path in written_paths]
            source_version = subtitle_source_version(saved_subtitles[0].storage_path) if saved_subtitles else None
            source_publication = (SubtitlePublication(path, saved_subtitles[0].storage_path, source_version)
                                  if source_version is not None else None)
    except Exception as e:
        logging.exception(f'BAZARR Error saving Subtitles file to disk for this file {path}: {repr(e)}')  # noqa: G004
        return

    if len(saved_subtitles) < 1:
        logging.exception(f'BAZARR Error saving Subtitles file to disk for this file: {path}')  # noqa: G004
        return

    # An uploaded subtitle satisfies the language as surely as a downloaded one,
    # so whatever mismatch was recorded for it no longer describes anything. This
    # path has no Video object, only the ids, which is all the resolver reads.
    from types import SimpleNamespace

    from .manual import clear_mismatch_after_manual_save

    clear_mismatch_after_manual_save(
        SimpleNamespace(sonarrEpisodeId=sonarrEpisodeId, radarrId=radarrId,
                        original_path=path, arr_instance_id=arr_instance_id),
        media_type, saved_subtitles, arr_instance_id)

    subtitle_path = saved_subtitles[0].storage_path

    if hi:
        modifier_string = " HI"
    elif forced:
        modifier_string = " forced"
    else:
        modifier_string = ""

    if hi:
        modifier_code = ":hi"
    elif forced:
        modifier_code = ":forced"
    else:
        modifier_code = ""
    uploaded_language_code3 = language + modifier_code
    uploaded_language = language_from_alpha3(language) + modifier_string
    uploaded_language_code2 = alpha2_from_alpha3(language) + modifier_code

    if use_postprocessing:
        command = pp_replace(postprocessing_cmd, path, subtitle_path, uploaded_language, uploaded_language_code2,
                             uploaded_language_code3, audio_language['name'], audio_language['code2'],
                             audio_language['code3'], 100, "1", "manual", "user", "unknown", sonarrSeriesId,
                             sonarrEpisodeId or radarrId,)
        with subtitle_write_locks(path, subtitle_path):
            if subtitle_source_version(subtitle_path) == source_version:
                with observe_subtitle_change(media_type, path, subtitle_path, 'upload', arr_instance_id):
                    postprocessing(command, path, subtitle_path=subtitle_path)
                    set_chmod(subtitles_path=subtitle_path)
                source_version = subtitle_source_version(subtitle_path)
                source_publication.release()
                source_publication = SubtitlePublication(path, subtitle_path, source_version)

    refresh_subtitles = partial(_refresh_uploaded_subtitles, path, subtitle_path, sonarr_series_id=sonarrSeriesId,
                                sonarr_episode_id=sonarrEpisodeId, radarr_id=radarrId,
                                arr_instance_id=arr_instance_id)
    refresh_subtitles()

    if media_type == 'sports':
        reversed_path = path_mappings.path_replace_reverse_instance(path, arr_instance_id, "sports")
        reversed_subtitles_path = path_mappings.path_replace_reverse_instance(
            subtitle_path, arr_instance_id, "sports")
        event_stream(type='sports', action='update', payload=sportsEventId)
    elif media_type == 'series':
        # Reverse-map through the owning instance's path_mappings (#156); None
        # owner => global mapping (the default/single-instance path), unchanged.
        reversed_path = path_mappings.path_replace_reverse_instance(path, arr_instance_id, "series")
        reversed_subtitles_path = path_mappings.path_replace_reverse_instance(
            subtitle_path, arr_instance_id, "series")
        # Route the rescan at the OWNING instance's server (#156). None owner =
        # default server (legacy single-instance), unchanged.
        event_stream(type='series', action='update', payload=episode_metadata.sonarrSeriesId)
        event_stream(type='episode-wanted', action='delete', payload=episode_metadata.sonarrEpisodeId)
    else:
        # Reverse-map through the owning instance's path_mappings (#156); None
        # owner => global mapping (the default/single-instance path), unchanged.
        reversed_path = path_mappings.path_replace_reverse_instance(path, arr_instance_id, "movie")
        reversed_subtitles_path = path_mappings.path_replace_reverse_instance(
            subtitle_path, arr_instance_id, "movie")
        event_stream(type='movie', action='update', payload=movie_metadata.radarrId)
        event_stream(type='movie-wanted', action='delete', payload=movie_metadata.radarrId)

    result = ProcessSubtitlesResult(message=f"{language_from_alpha3(language)}{modifier_string} Subtitles manually "
                                            "uploaded.",
                                    reversed_path=reversed_path,
                                    downloaded_language_code2=uploaded_language_code2,
                                    downloaded_provider=None,
                                    score=None,
                                    forced=None,
                                    subtitle_id=None,
                                    reversed_subtitles_path=reversed_subtitles_path,
                                    hearing_impaired=None)

    if not result:
        logging.debug(f"BAZARR unable to process subtitles for this {'episode' if media_type == 'series' else 'movie'}:"  # noqa: G004
                      f" {path}")
    else:
        if isinstance(result, tuple) and len(result):
            result = result[0]
        provider = "manual"
        if media_type == 'sports':
            from sportarr.history import sports_history_log

            sports_history_log(4, sportsEventId, arr_instance_id, result)
            if not settings.general.dont_notify_manual_actions:
                _notify_upload("user", send_notifications_sports, sportsEventId, result.message,
                               arr_instance_id=arr_instance_id)
        elif media_type == 'series':
            history_log(4, sonarrSeriesId, sonarrEpisodeId, result, fake_provider=provider,
                        fake_score=MAX_SCORES['episode'], arr_instance_id=arr_instance_id)
            if not settings.general.dont_notify_manual_actions:
                _notify_upload("user", send_notifications, sonarrSeriesId, sonarrEpisodeId, result.message,
                                   arr_instance_id=arr_instance_id)
            if settings.general.use_plex:
                if settings.plex.set_episode_added:
                    _notify_upload("Plex added date", plex_set_episode_added_date_now, episode_metadata)
        else:
            history_log_movie(4, radarrId, result, fake_provider=provider, fake_score=MAX_SCORES['movie'],
                              arr_instance_id=arr_instance_id)
            if not settings.general.dont_notify_manual_actions:
                _notify_upload("user", send_notifications_movie, radarrId, result.message, arr_instance_id=arr_instance_id)
            if settings.general.use_plex:
                if settings.plex.set_movie_added:
                    _notify_upload("Plex added date", plex_set_movie_added_date_now, movie_metadata)

    refresh_consumers = partial(
        _refresh_upload_consumers, media_type,
        sports_metadata if media_type == 'sports'
        else episode_metadata if media_type == 'series' else movie_metadata,
        arr_instance_id)
    refresh_consumers()
    if source_publication is not None:
        sync_subtitles(video_path=path, srt_path=subtitle_path, srt_lang=uploaded_language_code2,
                       percent_score=100, forced=forced, hi=hi, sonarr_series_id=sonarrSeriesId,
                       sonarr_episode_id=sonarrEpisodeId, radarr_id=radarrId,
                       arr_instance_id=arr_instance_id, callback=refresh_subtitles,
                       source_version=source_publication, on_success=refresh_consumers)
    return '', 204

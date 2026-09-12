# coding=utf-8

import os
import logging
from media_servers.events import publication_callback

from subliminal.subtitle import SUBTITLE_EXTENSIONS

from app.event_handler import event_stream
from app.config import settings
from app.database import database, TableShows, TableEpisodes, TableMovies, select
from arr_instances.resolution import scoped, client_for_instance
from languages.get_languages import language_from_alpha2
from utilities.path_mappings import path_mappings
from utilities.autopulse_webhook import call_external_webhook
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.processing import ProcessSubtitlesResult
from subtitles.tools.subsync_engines import (subtitle_write_lock, subtitle_write_locks,
                                            quarantine_sync_outputs_after_mutation)
from sonarr.history import history_log
from radarr.history import history_log_movie
from sonarr.notify import notify_sonarr
from radarr.notify import notify_radarr
from plex.operations import plex_refresh_item, plex_update_sports_library
from jellyfin.operations import jellyfin_refresh_item, jellyfin_update_sports_library


def _delete_subtitle_file(media_path, subtitle_path, on_publish=None):
    with subtitle_write_locks(media_path, subtitle_path):
        state = subtitle_write_lock(media_path, os.path.dirname(subtitle_path))
        try:
            os.remove(subtitle_path)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                state.changed(subtitle_path)
            logging.exception('BAZARR cannot delete subtitles file: %s', subtitle_path)
            return False
        if on_publish:
            on_publish(subtitle_path)
        state.changed(subtitle_path)
        quarantine_sync_outputs_after_mutation(media_path, subtitle_path)
        return True


def delete_subtitles(media_type, language, forced, hi, media_path, subtitles_path, sonarr_series_id=None,
                     sonarr_episode_id=None, radarr_id=None, arr_instance_id=None,
                     sports_event_id=None):
    if not subtitles_path:
        logging.error('No subtitles to delete.')
        return False

    if not os.path.splitext(subtitles_path)[1] in SUBTITLE_EXTENSIONS:  # noqa: E713
        logging.error('BAZARR can only delete subtitles files.')
        return False

    language_log = language
    language_string = language_from_alpha2(language)
    if hi in [True, 'true', 'True']:
        language_log += ':hi'
        language_string += ' HI'
    elif forced in [True, 'true', 'True']:
        language_log += ':forced'
        language_string += ' forced'

    # Honour the owning instance's per-instance path_mappings (#156): a
    # secondary instance can have a different on-disk prefix, so the delete /
    # re-index must resolve paths through that instance's mapping. arr_instance_id
    # None => global mapping (the default/single-instance path), unchanged.
    if media_type == 'sports':
        def pr(p):
            return path_mappings.path_replace_instance(p, arr_instance_id, "sports")

        def prr(p):
            return path_mappings.path_replace_reverse_instance(p, arr_instance_id, "sports")

        # A sports event has no imdb or tvdb id to refresh a media server with,
        # so nothing is looked up here.
        metadata = None
    elif media_type == 'series':
        def pr(p):
            return path_mappings.path_replace_instance(p, arr_instance_id, "series")

        def prr(p):
            return path_mappings.path_replace_reverse_instance(p, arr_instance_id, "series")

        metadata = database.execute(scoped(
            select(TableEpisodes.season, TableEpisodes.episode, TableShows.imdbId, TableShows.tvdbId)
            .join(TableShows)
            .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
            TableEpisodes.arr_instance_id, arr_instance_id)).first()
    else:
        def pr(p):
            return path_mappings.path_replace_instance(p, arr_instance_id, "movie")

        def prr(p):
            return path_mappings.path_replace_reverse_instance(p, arr_instance_id, "movie")

        metadata = database.execute(scoped(
            select(TableMovies.imdbId, TableMovies.tmdbId)
            .where(TableMovies.radarrId == radarr_id),
            TableMovies.arr_instance_id, arr_instance_id)).first()

    result = ProcessSubtitlesResult(message=f"{language_string} subtitles deleted from disk.",
                                    reversed_path=prr(media_path),
                                    downloaded_language_code2=language_log,
                                    downloaded_provider=None,
                                    score=None,
                                    forced=None,
                                    subtitle_id=None,
                                    reversed_subtitles_path=prr(subtitles_path),
                                    hearing_impaired=None)

    if media_type == 'sports':
        from sportarr.history import sports_history_log
        from subtitles.indexer.sports import store_subtitles_sports

        removed = _delete_subtitle_file(media_path, pr(subtitles_path),
                                        publication_callback(media_type, media_path, 'delete', arr_instance_id))
        store_subtitles_sports(sports_event_id, arr_instance_id)
        if not removed:
            return False
        sports_history_log(0, sports_event_id, arr_instance_id, result)
        event_stream(type='sports', action='update', payload=sports_event_id)

        # One whole-library Sportarr rescan per affected owner, behind the
        # per-instance transport and non-blocking; Sportarr exposes only that
        # untargeted scan. The media servers refresh their configured sports
        # libraries instead of an item the event carries no identifier for.
        from sportarr.notify import notify_rescan
        notify_rescan(arr_instance_id)
        if settings.general.use_plex:
            sports_library = settings.plex.sports_library
            if isinstance(sports_library, str):
                sports_library = [sports_library] if sports_library else []
            if sports_library:
                plex_update_sports_library()
        if settings.general.use_jellyfin:
            sports_library_ids = settings.jellyfin.sports_library_ids
            if isinstance(sports_library_ids, str):
                sports_library_ids = [sports_library_ids] if sports_library_ids else []
            if sports_library_ids:
                jellyfin_update_sports_library()

        call_external_webhook(
            subtitle_path=subtitles_path,
            media_path=media_path,
            language=language_log,
            media_type=media_type
        )

        return True

    if media_type == 'series':
        removed = _delete_subtitle_file(media_path, pr(subtitles_path),
                                        publication_callback(media_type, media_path, 'delete', arr_instance_id))
        store_subtitles(prr(media_path), media_path, arr_instance_id=arr_instance_id)
        if not removed:
            return False
        history_log(0, sonarr_series_id, sonarr_episode_id, result, arr_instance_id=arr_instance_id)
        # Route the rescan at the OWNING instance's Sonarr (#156); None
        # owner = default server (legacy single-instance), unchanged.
        notify_sonarr(sonarr_series_id,
                      arr_client=client_for_instance(database, arr_instance_id, enabled_only=False))
        event_stream(type='series', action='update', payload=sonarr_series_id)
        event_stream(type='episode-wanted', action='update', payload=sonarr_episode_id)

        if settings.general.use_plex and settings.plex.update_series_library:
            plex_refresh_item(metadata.imdbId, is_movie=False, season=metadata.season,
                              episode=metadata.episode)
        if settings.general.use_jellyfin and settings.jellyfin.update_series_library:
            jellyfin_refresh_item(metadata.imdbId, is_movie=False, season=metadata.season,
                                  episode=metadata.episode, tvdb_id=metadata.tvdbId)

        # Call external webhook after all processing is complete
        call_external_webhook(
            subtitle_path=subtitles_path,
            media_path=media_path,
            language=language_log,
            media_type=media_type
        )

        return True
    else:
        removed = _delete_subtitle_file(media_path, pr(subtitles_path),
                                        publication_callback(media_type, media_path, 'delete', arr_instance_id))
        store_subtitles_movie(prr(media_path), media_path, arr_instance_id=arr_instance_id)
        if not removed:
            return False
        history_log_movie(0, radarr_id, result, arr_instance_id=arr_instance_id)
        notify_radarr(radarr_id,
                      arr_client=client_for_instance(database, arr_instance_id, enabled_only=False))
        event_stream(type='movie-wanted', action='update', payload=radarr_id)

        if settings.general.use_plex and settings.plex.update_movie_library:
            plex_refresh_item(metadata.imdbId, is_movie=True)
        if settings.general.use_jellyfin and settings.jellyfin.update_movie_library:
            jellyfin_refresh_item(metadata.imdbId, is_movie=True,
                                  tmdb_id=metadata.tmdbId)

        # Call external webhook after all processing is complete
        call_external_webhook(
            subtitle_path=subtitles_path,
            media_path=media_path,
            language=language_log,
            media_type=media_type
        )

        return True

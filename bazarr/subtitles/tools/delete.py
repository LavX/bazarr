# coding=utf-8

import os
import logging
from media_servers.events import publication_callback

from subliminal.subtitle import SUBTITLE_EXTENSIONS

from app.event_handler import event_stream
from app.database import database
from arr_instances.resolution import client_for_instance
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


def _delete_subtitle_file(media_path, subtitle_path, on_publish=None, revalidate=None):
    with subtitle_write_locks(media_path, subtitle_path):
        # Under the directory locks, which is where a caller's ownership check
        # has to be re-asked: the sports indexer takes these same locks before
        # it opens its transaction, so a reconciliation cannot swap the
        # recording out between the check and the removal below. Before the
        # unlink, never after: a refusal here has to mean nothing was deleted.
        if revalidate is not None:
            revalidate()
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
                     sports_event_id=None, revalidate=None):
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

    elif media_type == 'series':
        def pr(p):
            return path_mappings.path_replace_instance(p, arr_instance_id, "series")

        def prr(p):
            return path_mappings.path_replace_reverse_instance(p, arr_instance_id, "series")

    else:
        def pr(p):
            return path_mappings.path_replace_instance(p, arr_instance_id, "movie")

        def prr(p):
            return path_mappings.path_replace_reverse_instance(p, arr_instance_id, "movie")

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

        # The caller's recording check, re-asked under the write locks rather
        # than only before them, so a reconciliation that reassigns the
        # recording cannot make this request take another event's subtitle
        # with it. Deliberately the signature check and not the full
        # publication boundary: that one opens a transaction and locks the
        # event row, and holding those while _delete_subtitle_file waits on the
        # subtitle directory locks inverts the order every other writer uses
        # (staged_subtitle_write takes the locks first and enters the guard
        # inside them), which deadlocks against an indexer mid-probe.
        removed = _delete_subtitle_file(media_path, pr(subtitles_path),
                                        publication_callback(media_type, media_path, 'delete',
                                                             arr_instance_id),
                                        revalidate=revalidate)
        if not removed:
            store_subtitles_sports(sports_event_id, arr_instance_id)
            return False
        try:
            store_subtitles_sports(sports_event_id, arr_instance_id)
        except Exception:
            logging.exception('Sports subtitle deleted, but reindex failed for event %s and owner %s',
                              sports_event_id, arr_instance_id)
        sports_history_log(0, sports_event_id, arr_instance_id, result)
        event_stream(type='sports', action='update', payload=sports_event_id)

        # One whole-library Sportarr rescan per affected owner, behind the
        # per-instance transport and non-blocking; Sportarr exposes only that
        # untargeted scan. The media servers refresh their configured sports
        # libraries through the deletion this path already published, instead
        # of an item the event carries no identifier for.
        from sportarr.notify import notify_rescan
        notify_rescan(arr_instance_id)

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

        # Call external webhook after all processing is complete
        call_external_webhook(
            subtitle_path=subtitles_path,
            media_path=media_path,
            language=language_log,
            media_type=media_type
        )

        return True

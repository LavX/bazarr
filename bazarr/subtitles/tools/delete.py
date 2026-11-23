# coding=utf-8

import ast
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


def _delete_subtitle_file(media_path, subtitle_path, on_publish=None, revalidate=None, after_delete=None,
                          ownership_index=None):
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
        quarantine_sync_outputs_after_mutation(
            media_path, subtitle_path, **({'ownership_index': ownership_index} if ownership_index is not None else {}))
        # A caller's record of the removal, such as a blacklist entry, is
        # written only once the file is really gone and while the locks are
        # still held, so a refused or failed deletion records nothing and a
        # duplicate request, which finds the file already gone, cannot record
        # it twice.
        if after_delete is not None:
            after_delete()
        return True


def delete_subtitles(media_type, language, forced, hi, media_path, subtitles_path, sonarr_series_id=None,
                     sonarr_episode_id=None, radarr_id=None, arr_instance_id=None,
                     sports_event_id=None, revalidate=None, after_delete=None, *, source=None, blacklist=None):
    if not subtitles_path:
        logging.error('No subtitles to delete.')
        return False

    extension = os.path.splitext(subtitles_path)[1]
    if (extension.lower() if source is not None else extension) not in SUBTITLE_EXTENSIONS:
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

    if source is not None:
        return _delete_source_subtitle(source, subtitles_path, language_log, language_string, blacklist)

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
                                        revalidate=revalidate, after_delete=after_delete)
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

        call_external_webhook(
            subtitle_path=subtitles_path,
            media_path=media_path,
            language=language_log,
            media_type=media_type
        )

        return True

    if media_type == 'series':
        removed = _delete_subtitle_file(media_path, pr(subtitles_path),
                                        publication_callback(media_type, media_path, 'delete', arr_instance_id),
                                        revalidate=revalidate, after_delete=after_delete)
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
                                        publication_callback(media_type, media_path, 'delete', arr_instance_id),
                                        revalidate=revalidate, after_delete=after_delete)
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


def _delete_source_subtitle(source, subtitle_path, language_code, language_name, blacklist):
    from media_sources import repository
    from media_sources.history import blacklist_log_source, current_source_release_version, history_log_source
    from media_sources.indexer import SourcePublicationOwnerIndex, index_and_notify_item
    from media_sources.manual import manual_source_publication
    from subtitles.tools.delete_ownership import SubtitleDeletionError
    from subtitles.tools.subsync_engines import SubtitleDestinationChanged, regular_subtitle_version

    ownership_index = SourcePublicationOwnerIndex()
    with subtitle_write_locks(source.path, subtitle_path):
        def require_indexed(item):
            try:
                subtitles = ast.literal_eval(item.subtitles or '[]')
            except (SyntaxError, ValueError):
                subtitles = []
            if not isinstance(subtitles, list) or not any(
                    isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1] == subtitle_path
                    for entry in subtitles):
                raise SubtitleDeletionError('This subtitle is not indexed for the source item.', 403)

        item = repository.get_item(source.item_id)
        if item is None or not source.same_item(item):
            raise SubtitleDeletionError('The source item moved or was removed.', 409)
        require_indexed(item)
        try:
            release = (source.kind, *blacklist[1:]) if blacklist is not None else None
            with manual_source_publication(source, subtitle_path, ownership_index, release=release) as (
                    current, connection):
                require_indexed(repository.get_item(current.item_id, connection))
                revalidate = None
                if blacklist is not None:
                    version = current_source_release_version(current, subtitle_path, language_code, *blacklist,
                                                             target=connection)
                    if version is None:
                        raise SubtitleDeletionError('The selected release is no longer the current subtitle.', 409)

                    def revalidate():
                        if regular_subtitle_version(subtitle_path) != version:
                            raise SubtitleDeletionError('The selected subtitle changed before deletion.', 409)

                removed = _delete_subtitle_file(
                    current.path, subtitle_path,
                    publication_callback(current.publication_type, current.path, 'delete', source=current),
                    revalidate=revalidate, ownership_index=ownership_index)
                if removed:
                    result = ProcessSubtitlesResult(
                        message=f'{language_name} subtitles deleted from disk.', reversed_path=current.path,
                        downloaded_language_code2=language_code, downloaded_provider=None, score=None,
                        forced=None, subtitle_id=None, reversed_subtitles_path=subtitle_path, hearing_impaired=None)
                    history_log_source(0, current, result, target=connection)
                    if blacklist is not None:
                        _, provider, subs_id = blacklist
                        blacklist_log_source(current, provider, subs_id, language_code, target=connection)
        except SubtitleDestinationChanged as error:
            raise SubtitleDeletionError(str(error), 409) from error

    index_and_notify_item(source)
    if removed:
        if blacklist is not None:
            event_stream(type=f'{source.publication_type}-blacklist')
        call_external_webhook(subtitle_path=subtitle_path, media_path=source.path, language=language_code,
                              media_type='sports' if source.kind == 'sports_event' else source.media_type)
    return removed

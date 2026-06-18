# coding=utf-8
# fmt: off

import logging
import gc

from app.config import settings
from app.jobs_queue import jobs_queue
from subtitles.tools.subsyncer import SubSyncer
from subtitles.tools.subsync_engines import (
    DEFAULT_ENABLED_ENGINES,
    OUTPUT_MODE_KEEP_ALL,
    is_sync_engine_output,
    normalize_enabled_engines,
)


def _sync_complete_job_name(srt_path, sync_result):
    if not sync_result:
        return f"Failed to sync {srt_path}"

    if sync_result.success:
        successes = sync_result.successful_results
        if sync_result.output_mode == OUTPUT_MODE_KEEP_ALL:
            count = len(successes)
            noun = "output" if count == 1 else "outputs"
            return f"Generated {count} sync {noun} for {srt_path}"
        engine = successes[0].engine if successes else "sync engine"
        return f"Synced {srt_path} using {engine}"

    if sync_result.skipped_results and not sync_result.failed_results:
        return f"Skipped sync for {srt_path}"

    return f"Failed to sync {srt_path}"


def _sync_progress_total(enabled_engines):
    configured_engines = (
        enabled_engines if enabled_engines is not None
        else getattr(settings.subsync, 'enabled_engines', DEFAULT_ENABLED_ENGINES)
    )
    return max(len(normalize_enabled_engines(configured_engines)), 1)


def _resolve_subsync_overrides(arr_instance_id, is_series, enabled_engines, max_offset_seconds):
    """Resolve the audio-sync settings against the owning instance's per-instance
    overrides (#227), honouring any explicit caller-supplied values.

    ``enabled_engines``/``max_offset_seconds`` of None mean "not supplied", so the
    per-instance value (else the global default) applies; a non-None value is an
    explicit caller override (e.g. the manual sync dialog) and is respected. A
    None instance returns the global values unchanged. ``max_offset_seconds`` is
    coerced to a string to match the legacy parameter shape.
    """
    from arr_instances.resolution import resolve_subtitle_setting as _resolve
    use_subsync = _resolve(arr_instance_id, "subsync.use_subsync", settings.subsync.use_subsync)
    if is_series:
        use_threshold = _resolve(arr_instance_id, "subsync.use_subsync_threshold",
                                 settings.subsync.use_subsync_threshold)
        threshold = _resolve(arr_instance_id, "subsync.subsync_threshold",
                             settings.subsync.subsync_threshold)
    else:
        use_threshold = _resolve(arr_instance_id, "subsync.use_subsync_movie_threshold",
                                 settings.subsync.use_subsync_movie_threshold)
        threshold = _resolve(arr_instance_id, "subsync.subsync_movie_threshold",
                             settings.subsync.subsync_movie_threshold)
    if enabled_engines is None:
        enabled_engines = _resolve(arr_instance_id, "subsync.enabled_engines", None)
    if max_offset_seconds is None:
        max_offset_seconds = str(_resolve(arr_instance_id, "subsync.max_offset_seconds",
                                          settings.subsync.max_offset_seconds))
    return use_subsync, use_threshold, threshold, enabled_engines, max_offset_seconds


def _index_keep_all_outputs(video_path, sonarr_series_id=None, sonarr_episode_id=None, radarr_id=None,
                            arr_instance_id=None):
    if sonarr_episode_id:
        from app.event_handler import event_stream
        from subtitles.indexer.series import store_subtitles
        from utilities.media_ids import local_episode_id
        from utilities.path_mappings import path_mappings

        store_subtitles(path_mappings.path_replace_reverse(video_path), video_path)
        if sonarr_series_id:
            event_stream(type='series', payload=sonarr_series_id)
        # Emit the LOCAL episode id (#156): the frontend caches episode detail by
        # local id; the upstream sonarrEpisodeId is not unique across instances.
        event_stream(type='episode', payload=local_episode_id(sonarr_episode_id, arr_instance_id))
        return

    if radarr_id:
        from app.event_handler import event_stream
        from subtitles.indexer.movies import store_subtitles_movie
        from utilities.path_mappings import path_mappings

        store_subtitles_movie(path_mappings.path_replace_reverse_movie(video_path), video_path)
        event_stream(type='movie', payload=radarr_id)


def _report_progress(job_id, track_job_progress, owns_job_progress, message,
                     value=None, total=None, name=None):
    """Report sync progress to the jobs queue.

    The job owner (a standalone sync job) updates the progress value/max and the
    job name. A sub-step (an auto-sync running inside a download/wanted job)
    sends a message-only update: it still passes through ``update_job_progress``
    -- preserving the cancellation checkpoint that aborts the job when the user
    presses Stop -- but never overwrites the parent job's value/max or name,
    which previously produced >100% progress rings.
    """
    if not (job_id and track_job_progress):
        return
    if owns_job_progress:
        progress = {'job_id': job_id, 'progress_message': message}
        if value is not None:
            progress['progress_value'] = value
        if total is not None:
            progress['progress_max'] = total
        jobs_queue.update_job_progress(**progress)
        if name is not None:
            jobs_queue.update_job_name(job_id=job_id, new_job_name=name)
    else:
        jobs_queue.update_job_progress(job_id=job_id, progress_message=message)


def sync_subtitles(video_path,
                   srt_path,
                   srt_lang,
                   forced,
                   hi,
                   percent_score,
                   sonarr_series_id=None,
                   sonarr_episode_id=None,
                   radarr_id=None,
                   job_id=None,
                   max_offset_seconds=None,
                   gss=settings.subsync.gss,
                   no_fix_framerate=settings.subsync.no_fix_framerate,
                   reference=None,
                   force_sync=False,
                   output_mode=None,
                   enabled_engines=None,
                   callback=None,
                   track_job_progress=True,
                   arr_instance_id=None,
                   owns_job_progress=True):
    # The audio-sync settings resolve against the owning instance (#227); a None
    # owner / unset override yields the global value, so legacy paths are
    # unchanged. The use_subsync gate is evaluated inline via the module-level
    # helper so NO non-signature local is created before add_job_from_function
    # below, which re-passes the frame's locals as kwargs on re-invocation.
    if (not _resolve_subsync_overrides(
            arr_instance_id, bool(sonarr_episode_id), enabled_engines, max_offset_seconds)[0]
            and not force_sync):
        logging.debug('BAZARR automatic syncing is disabled in settings. Skipping sync routine.')
        return False

    if is_sync_engine_output(srt_path):
        logging.debug('BAZARR generated sync output cannot be synchronized again. Skipping: %s', srt_path)
        _report_progress(job_id, track_job_progress, owns_job_progress, 'Sync skipped',
                         value='max', name=f"Skipped sync for {srt_path}")
        return False

    if not job_id and track_job_progress:
        jobs_queue.add_job_from_function(
            f"Syncing {srt_path}",
            is_progress=True,
            progress_max=_sync_progress_total(enabled_engines),
        )
        return False

    # Past the enqueue point it is safe to bind locals. Resolve the per-instance
    # overrides for the real run (the use_subsync gate already passed above).
    (_, use_subsync_threshold, subsync_threshold,
     enabled_engines, max_offset_seconds) = _resolve_subsync_overrides(
        arr_instance_id, bool(sonarr_episode_id), enabled_engines, max_offset_seconds)
    progress_total = _sync_progress_total(enabled_engines)

    def report(message, value=None, total=None, name=None):
        _report_progress(job_id, track_job_progress, owns_job_progress, message, value, total, name)

    report('Preparing synchronization', value=0, total=progress_total, name=f"Syncing {srt_path}")

    def update_progress(message, value, total):
        report(message, value=value, total=total)

    if forced:
        logging.debug('BAZARR cannot sync forced subtitles. Skipping sync routine.')
        report('Sync skipped', value='max', name=f"Skipped sync for {srt_path}")
        return False

    logging.debug(f'BAZARR automatic syncing is enabled in settings. We\'ll try to sync this '  # noqa: G004
                  f'subtitles: {srt_path}.')
    if not use_subsync_threshold or (use_subsync_threshold and percent_score <= float(subsync_threshold)):
        subsync = SubSyncer()
        sync_kwargs = {
            'video_path': video_path,
            'srt_path': srt_path,
            'srt_lang': srt_lang,
            'forced': forced,
            'hi': hi,
            'max_offset_seconds': max_offset_seconds,
            'no_fix_framerate': no_fix_framerate,
            'gss': gss,
            'reference': reference,
            'sonarr_series_id': sonarr_series_id,
            'sonarr_episode_id': sonarr_episode_id,
            'radarr_id': radarr_id,
            'job_id': job_id,
            'force_sync': force_sync,
            'output_mode': output_mode,
            'enabled_engines': enabled_engines,
            'progress_callback': update_progress if track_job_progress else None,
            'arr_instance_id': arr_instance_id,
        }
        sync_result = None
        try:
            sync_result = subsync.sync(**sync_kwargs)
            if sync_result and sync_result.success:
                if callback:
                    callback()
                elif getattr(sync_result, 'output_mode', None) == OUTPUT_MODE_KEEP_ALL:
                    _index_keep_all_outputs(
                        video_path,
                        sonarr_series_id=sonarr_series_id,
                        sonarr_episode_id=sonarr_episode_id,
                        radarr_id=radarr_id,
                        arr_instance_id=arr_instance_id,
                    )
        except Exception:
            logging.exception(f'BAZARR an unhandled exception occurs during the synchronization process for this '  # noqa: G004
                              f'subtitle file: {srt_path}')
            return False
        else:
            return bool(sync_result and sync_result.success)
        finally:
            if sync_result and sync_result.success:
                progress_message = 'Sync complete'
            elif sync_result and sync_result.skipped_results and not sync_result.failed_results:
                progress_message = 'Sync skipped'
            else:
                progress_message = 'Sync failed'
            report(progress_message, value='max', name=_sync_complete_job_name(srt_path, sync_result))
            del subsync
            gc.collect()

    logging.debug(f"BAZARR subsync skipped because subtitles score isn't below this "  # noqa: G004
                  f"threshold value: {subsync_threshold}%")
    report('Sync skipped', value='max', name=f"Skipped sync for {srt_path}")
    return False

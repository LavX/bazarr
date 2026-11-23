# coding=utf-8

import logging
from contextlib import nullcontext
from os.path import normpath
from threading import RLock


from .core.translator_utils import (validate_translation_params, convert_language_codes, get_title,
                                    SourceTranslationChanged, SourceTranslationInputAmbiguous,
                                    source_translation_video_version, translated_destination_path,
                                    validate_source_translation)
from . import pick as _pick
from .availability import runtime_active as _runtime_active, translation_available
from .failure_record import clear_failed_translation, record_failed_translation
from .services.translator_factory import TranslatorFactory
from languages.get_languages import alpha3_from_alpha2
from app.config import settings
from app import activity
from app.jobs_queue import jobs_queue, JobCancelled, JobFailed
from subtitles.tools.subsync_engines import (SubtitleDestinationChanged, SubtitleSourceChanged,
                                            validate_subtitle_versions)


def _failure_record_media_id(media_type, radarr_id, sonarr_episode_id, sports_operation, source=None):
    """The id the failed-translation record files this item under.

    Each queue path names its media the way its own scan does: a movie by its
    upstream id, an episode by its episode id, and a sports event by the id its
    captured operation carries, which is the one the sports scan consults with.
    """
    if source is not None:
        return source.item_id
    if media_type == 'sports':
        return sports_operation.context.event_id if sports_operation is not None else None
    if media_type == 'episode':
        return sonarr_episode_id
    return radarr_id


def _destination_race(error):
    """Whether this failure is the queued destination moving under the job.

    The write guard refuses a destination whose versions no longer match the
    queueing sighting. OpenRouter wraps that refusal in its own service error,
    so the cause chain is walked; the other translators re-raise it as it is.
    """
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (SubtitleSourceChanged, SubtitleDestinationChanged)):
            return True
        error = error.__cause__
    return False


def _report_destination_skip(job_id, from_lang, to_lang):
    """A subtitle that arrived while this job waited is a skip, not a failure.

    Nothing was published, so nothing holds the next scan back, and the job
    drawer says what happened instead of telling the user a translation died.
    """
    logging.info('BAZARR translation skipped: the queued subtitle input or destination '
                 'changed before this job wrote %s', to_lang)
    current_name = jobs_queue.get_job_name(job_id)
    if current_name and 'Translating' in current_name:
        skip_name = current_name.replace('Translating', 'Skipped')
    else:
        skip_name = f'Skipped: {from_lang.upper()} → {to_lang.upper()}'
    jobs_queue.update_job_name(job_id=job_id, new_job_name=skip_name)


# JobFailed reason of a translation that found the process's single OCR slot
# taken while it was reading an image source.
OCR_BUSY_REASON = 'ocr_busy'

# JobFailed reasons of an OCR source that cannot be read for a cause that says
# nothing about whether the item can be translated: the SRT OCR would write is
# already there, or the row no longer lists the track (hidden or rescanned
# since the request). No hold is recorded for them: the hold suppresses auto
# translation of the item, and for a taken name that is the very SRT it would
# read.
OCR_SOURCE_TAKEN_REASON = 'ocr_output_exists'
OCR_SOURCE_UNLISTED_REASON = 'ocr_track_unlisted'
NO_HOLD_REASONS = frozenset({OCR_BUSY_REASON, OCR_SOURCE_TAKEN_REASON, OCR_SOURCE_UNLISTED_REASON,
                            _pick.SWITCHED_OFF_REASON})


# The v2 translation runtime is reached from this module at two points only, the enqueue and the dispatch of the
# job, and only through the functions below. With its switch off none of them imports the runtime package.
_RUNTIME_MODULE = 'subtitles.tools.translate.runtime'
# A source worker may start as soon as the queue publishes it. Wait for the persistent attachment before
# validating and potentially retiring that exact queued job.
_SOURCE_ENQUEUE_LOCK = RLock()


def _source_translation_identity(source, source_srt_file):
    stamp = source.created_at.isoformat() if source.created_at is not None else ''
    # Normalize before namespacing, including custom subtitle folders containing "..".
    return f"{source.item_id}:{stamp}:{normpath(str(source_srt_file))}"


def _source_retry_scope(source, source_srt_file, source_tool=None, from_lang=None):
    identity = _source_translation_identity(source, source_srt_file)
    if source_tool is not None:
        return 'source-editor', f"{from_lang}:{source_tool.sidecar.language}:{identity}"
    return 'source', identity


def _job_source_key(video_path, source_srt_file, embedded_source, source=None, source_video_version=None,
                    source_tool=None, from_lang=None):
    """What the runtime keys a translation job by as its source: the file, or the embedded track it is read from."""
    if source is not None:
        namespace, identity = _source_retry_scope(source, source_srt_file, source_tool, from_lang)
        if source_tool is not None:
            sidecar = ",".join(map(str, source_tool.sidecar.version))
            namespace += f":sidecar:{sidecar}"
        if source_video_version is not None:
            recording = ",".join(map(str, source_video_version))
            namespace += f":video:{recording}"
        # Older queued jobs still use the unversioned namespace when dispatch retires them.
        return f"{namespace}:{identity}"
    if embedded_source:
        return (f"{video_path}#embedded:{embedded_source.get('language') or ''}:"
                f"{'hi' if embedded_source.get('hi') else ''}:{'forced' if embedded_source.get('forced') else ''}")
    return str(source_srt_file)


def _pick_key(video_path, source_srt_file, to_lang, forced, hi, embedded_source, source=None,
              source_video_version=None, source_tool=None, from_lang=None):
    """What a request's pick is kept under: the same for the request, its queued job and a retry of that job."""
    return (_job_source_key(video_path, source_srt_file, embedded_source, source, source_video_version,
                            source_tool, from_lang), to_lang, bool(forced), bool(hi))


def pin_translation(video_path, source_srt_file, to_lang, forced, hi, embedded_source, pin):
    """Record a manual pick (``pick.pin_value``: one back end, or one engine with none) for the translation about to be requested.

    A native manual translate or a pick on the Translation page calls this just before
    ``translate_subtitles_file`` with the same source and target. The runtime makes the job with the pick on it,
    replacing one that has not started, and the request that follows joins that job, so the job runs on that
    back end or engine alone. A job that already runs refuses the pick: ``TranslationJobError`` (a ``ValueError``) carries
    the reason. Without a pick, or with the runtime off, nothing is recorded.

    The native queue payload stays identical when the selection changes. Source tools carry their own selection
    with the captured subtitle and recording instead.
    """
    _pick.forget_expected()
    if pin is None or not _runtime_active():
        return
    from .runtime import enqueue

    enqueue.decide(_job_source_key(video_path, source_srt_file, embedded_source),
                   to_lang, forced, hi, pinned_connection_id=pin)
    # The queue job the request adds next carries the pick.
    _pick.expect()


def _runtime_follow(video_path, source_srt_file, to_lang, forced, hi, embedded_source, source=None,
                    source_video_version=None, source_tool=None, from_lang=None, source_pin=None):
    """The queue job to follow for a request the runtime already has a job for, 0 when there is none to follow
    (the job waits for its retry time and is logged as such, or runs with no queue job), or None when a new queue job is to be added (and always None with the runtime off).

    A source tool passes its pick here with its captured recording and sidecar. A native manual pick already
    exists under its ordinary key.
    """
    if not _runtime_active():
        if source_pin is not None:
            raise ValueError(_pick.SWITCHED_OFF)
        return None
    from .runtime import enqueue
    from .runtime.jobs import TranslationJobError
    try:
        # The request that has just recorded a pick owns it: the job it made is the one it joins.
        key = _job_source_key(video_path, source_srt_file, embedded_source, source, source_video_version,
                              source_tool, from_lang)
        if source is not None and source_video_version is not None:
            if source_tool is None:
                legacy_key = _job_source_key(video_path, source_srt_file, embedded_source, source)
                legacy_job = enqueue.attached_queue_job(legacy_key, to_lang, forced, hi)
                if not _pick.expecting() and legacy_job is not None and jobs_queue.matches_active_job(
                        legacy_job, __name__, 'translate_subtitles_file', source_video_version=source_video_version,
                        source_tool=None):
                    return legacy_job
            scope = _source_retry_scope(source, source_srt_file, source_tool, from_lang)
            enqueue.retire_source_retries(key, scope, to_lang, forced, hi)
        decision = enqueue.decide(key, to_lang, forced, hi, owns_pin=_pick.expecting(),
                                  pinned_connection_id=source_pin)
    except TranslationJobError:
        raise
    except Exception as error:
        if source_pin is not None:
            raise TranslationJobError('The selected translator could not be reserved') from error
        # A fault in the runtime's bookkeeping must not stop the request from being queued.
        logging.exception('BAZARR could not record a translation request in the translation runtime')
        return None
    if decision.action == enqueue.FOLLOW:
        return decision.queue_job_id or 0
    if decision.action == enqueue.QUEUED_FOR_RETRY:
        # The job is queued for a retry and nothing may be queued before its time; there is no queue job to
        # follow yet, which callers read as "nothing queued now".
        logging.info('BAZARR translation is waiting for its retry time (%s) and is not queued again before it',
                     decision.retry_at)
        return 0
    return None


def _runtime_attach(queue_job_id, video_path, source_srt_file, to_lang, forced, hi, embedded_source, source=None,
                    source_video_version=None, source_tool=None, from_lang=None):
    """Tell the runtime which queue job carries the job it made or joined for this request."""
    if not queue_job_id or not _runtime_active():
        return
    from .runtime import enqueue
    try:
        key = _job_source_key(video_path, source_srt_file, embedded_source, source, source_video_version,
                              source_tool, from_lang)
        enqueue.attach_for(queue_job_id, key, to_lang, forced, hi)
    except Exception:
        logging.exception('BAZARR could not attach a queued translation to its job in the translation runtime')


def _runtime_ends_quietly(error):
    """What the runtime's own non-failures are called, or None for any other error.

    A closed gate means nothing ran and the provider search takes over; a decline means every engine asked
    could not take the job. Neither is a failure of the translation.
    """
    if not type(error).__module__.startswith(_RUNTIME_MODULE):
        return None
    from .runtime.dispatch import Declined, GateClosed
    if isinstance(error, GateClosed):
        return 'gate_closed'
    if isinstance(error, Declined):
        return 'declined'
    return None


def _report_runtime_skip(job_id, from_lang, to_lang, why):
    current_name = jobs_queue.get_job_name(job_id)
    if current_name and 'Translating' in current_name:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=current_name.replace('Translating', 'Skipped'))
    else:
        jobs_queue.update_job_name(job_id=job_id, new_job_name=f'Skipped: {from_lang.upper()} → {to_lang.upper()} ({why})')


def _search_providers_instead(media_type, radarr_id, sonarr_episode_id, arr_instance_id, source=None):
    """A translation that cannot run falls back to the normal provider search for the same item."""
    try:
        if source is not None:
            from media_sources.service import search_source_item
            search_source_item(source.item_id, translation_gate=False, expected_source=source)
        elif media_type == 'episode' and sonarr_episode_id:
            from subtitles.wanted.series import wanted_download_subtitles
            wanted_download_subtitles(sonarr_episode_id, arr_instance_id=arr_instance_id, translation_gate=False)
        elif media_type in ('movie', 'movies') and radarr_id:
            from subtitles.wanted.movies import wanted_download_subtitles_movie
            wanted_download_subtitles_movie(radarr_id, arr_instance_id=arr_instance_id, translation_gate=False)
    except JobCancelled:
        raise
    except Exception:
        logging.exception('BAZARR the provider search that replaces a translation failed')


def translate_subtitles_file(video_path, source_srt_file, from_lang, to_lang, forced, hi,
                             media_type, sonarr_series_id, sonarr_episode_id, radarr_id, metadata,
                             job_id=None, arr_instance_id=None, sports_operation=None, embedded_source=None,
                             expected_destination_versions=(), source=None, source_tool=None,
                             source_video_version=None, source_pin=None):
    """Translate a subtitle file, as a queued job.

    ``embedded_source`` names an embedded track to translate instead of a file:
    a dict with ``language`` (alpha2), ``hi`` and ``forced``, and the row's
    ``codec`` when the request knows it. The track is extracted (a text track)
    or read with OCR (a PGS or VobSub track named by its codec) by the job, so
    the request that queues it returns at once.

    ``expected_destination_versions`` is what the auto paths capture with
    ``capture_subtitle_versions`` when they queue this job. The write refuses a
    destination that moved since, so a subtitle that landed while the job
    waited is not replaced, and the job reports that as a skip. A manual
    translation passes nothing and may replace its destination.

    Source jobs also carry the video version from enqueue through publication.

    Called without ``job_id`` it only queues, for every media type, and returns
    the id of the job to follow: the one it queued, or the identical one already
    pending or running. It returns None when nothing could be queued. Called
    inside its own job it returns the translation result.
    """
    if source is not None and (
            media_type != ('episode' if source.kind == 'episode' else 'movies')
            or any(value is not None for value in (arr_instance_id, sports_operation, embedded_source,
                                                   sonarr_series_id, sonarr_episode_id, radarr_id, metadata))):
        raise ValueError('A source translation requires source media metadata')
    if source_tool is not None and (source != source_tool.source or source_srt_file != source_tool.sidecar.path
                                    or video_path != source_tool.source.path):
        raise ValueError('Translation requires the captured source item and subtitle')

    if source_pin is not None and source_tool is None:
        raise ValueError('A source translator pick requires a captured subtitle')

    if not job_id:
        with _SOURCE_ENQUEUE_LOCK if source is not None else nullcontext():
            try:
                if source is None:
                    # The queue binds frame locals. Preserve the existing payload for native deduplication.
                    del source_tool, source_video_version, source_pin
                elif source_video_version is None:
                    source_video_version = source_translation_video_version(video_path)
                elif source_video_version != source_translation_video_version(video_path):
                    _pick.forget_expected()
                    return None
                if source is not None:
                    if source_tool is None:
                        del source_pin
                    else:
                        _pick.forget_expected()
                # With the v2 runtime on, a request for a source and target that already has a job joins it.
                # Reusing ``job_id`` keeps the frame the queue records to the function's own parameters.
                job_id = _runtime_follow(
                    video_path, source_srt_file, to_lang, forced, hi, embedded_source,
                    **({'source': source, 'source_video_version': source_video_version} if source is not None else {}),
                    **({'source_tool': source_tool, 'from_lang': from_lang, 'source_pin': source_pin}
                       if source is not None and source_tool is not None else {}))
                if job_id is not None:
                    # A joined job keeps what it was made for, picked or not.
                    _pick.forget_expected()
                    return job_id or None
                # Build the label inline: add_job_from_function captures the frame,
                # which must contain only function parameters before it is called.
                job_id = jobs_queue.add_job_from_function(
                    (lambda t: f'Translating {t} ({from_lang.upper()} to {to_lang.upper()})' if t else
                     f'Translating {from_lang.upper()} to {to_lang.upper()}')(
                        get_title(media_type, radarr_id, sonarr_series_id, sonarr_episode_id,
                                  arr_instance_id,
                                  **({'sports_context': sports_operation.context} if sports_operation else {}),
                                  **({'source': source} if source is not None else {}))),
                    is_progress=True, return_existing=True)
                _runtime_attach(
                    job_id, video_path, source_srt_file, to_lang, forced, hi, embedded_source,
                    **({'source': source, 'source_video_version': source_video_version} if source is not None else {}),
                    **({'source_tool': source_tool, 'from_lang': from_lang}
                       if source is not None and source_tool is not None else {}))
                # The queue job carries the pick the request noted, if it noted one.
                _pick.bind(_pick_key(video_path, source_srt_file, to_lang, forced, hi, embedded_source, source,
                                     source_video_version if source is not None else None,
                                     source_tool if source is not None else None, from_lang), job_id)
                return job_id or None
            except BaseException:
                # The request never queued its job: its pick must not be lent to the next request on this thread.
                _pick.forget_expected()
                raise

    translator_label = settings.translator.translator_type.replace("_", " ").title()
    with _SOURCE_ENQUEUE_LOCK if source is not None else nullcontext():
        runtime_on = _runtime_active()
        source_key = (_job_source_key(video_path, source_srt_file, embedded_source, source, source_video_version,
                                      source_tool, from_lang) if runtime_on else None)
    failure_owner = {'source_created_at': source.created_at} if source is not None else {}
    owner_type = 'source' if source is not None else media_type
    failure_key = (
        arr_instance_id, owner_type,
        _failure_record_media_id(media_type, radarr_id, sonarr_episode_id, sports_operation, source),
        from_lang, to_lang, forced, hi,
    )
    # Observation only. The queue records a generic completed envelope for every
    # ordinary return, so a typed outcome has to be recorded where publication
    # actually happens, further down, and a partial result has to dominate it.
    observed = activity.register(activity.activity_id_for_job(job_id), operation='translation',
                                 scope_kind='media', arr_instance_id=arr_instance_id,
                                 media_type=owner_type, language=to_lang, source_language=from_lang,
                                 forced=forced, hi=hi,
                                 upstream_series_id=sonarr_series_id,
                                 upstream_episode_id=sonarr_episode_id,
                                 upstream_movie_id=radarr_id)
    try:
        activity.note_scope(observed, title=get_title(media_type, radarr_id, sonarr_series_id,
                                                      sonarr_episode_id, arr_instance_id,
                                                      **({'source': source} if source is not None else {})))
    except Exception:
        logging.debug('Could not record the media title for this translation observation')
    try:
        cancel = None
        if media_type == 'sports':
            from sportarr.workflows import SportsJobSignal
            if sports_operation is None or sports_operation.context.arr_instance_id != arr_instance_id:
                raise ValueError('An exact sports profile operation is required')
            if any(value is not None for value in (sonarr_series_id, sonarr_episode_id, radarr_id, metadata)):
                raise ValueError('Sports translation cannot use native media metadata')
            cancel = SportsJobSignal(arr_instance_id, job_id)
            sports_operation.validate(wanted=True, cancel=cancel)
        if not runtime_on and (source_pin is not None or _pick.noted(
                _pick_key(video_path, source_srt_file, to_lang, forced, hi, embedded_source, source,
                          source_video_version, source_tool, from_lang), job_id)):
            # A pick was made for this job and the new translator was switched off while it waited. Running
            # it on the old translator would drop the pick without a word.
            raise JobFailed(_pick.SWITCHED_OFF, reason=_pick.SWITCHED_OFF_REASON, retryable=False)
        if embedded_source:
            media_id = radarr_id if media_type == "movie" else (
                sonarr_episode_id if media_type == "episode" else None)
            source_srt_file = _extract_embedded_source(video_path, media_type, embedded_source, job_id,
                                                       arr_instance_id, media_id=media_id)
        logging.debug(f'Translation request: video={video_path}, source={source_srt_file}, from={from_lang}, to={to_lang}')  # noqa: G004

        if source_tool is not None:
            try:
                source_tool.validate()
            except SubtitleSourceChanged as error:
                raise SourceTranslationChanged(str(error)) from error
        if source is not None:
            if runtime_on and source_video_version is not None and source_tool is None:
                from .runtime import enqueue

                legacy_key = _job_source_key(video_path, source_srt_file, embedded_source, source)
                if enqueue.attached_queue_job(legacy_key, to_lang, forced, hi) == job_id:
                    source_key = legacy_key
            if source_video_version is None:
                # An inline translation can bind the current file. Older queue entries cannot.
                if jobs_queue.matches_active_job(job_id, __name__, 'translate_subtitles_file', running_only=True):
                    raise SourceTranslationChanged('The queued translation has no source media version')
                source_video_version = source_translation_video_version(video_path)
                if runtime_on:
                    source_key = _job_source_key(video_path, source_srt_file, embedded_source, source,
                                                 source_video_version, source_tool, from_lang)
            elif source_video_version != source_translation_video_version(video_path):
                raise SourceTranslationChanged('The source media file changed after the translation was queued')
            destination = translated_destination_path(video_path, to_lang, forced, hi)
            source = validate_source_translation(source, video_path, source_srt_file, destination, from_lang,
                                                 to_lang, forced, hi, expected_destination_versions)
            if expected_destination_versions and not runtime_on and not translation_available().available:
                _report_runtime_skip(job_id, from_lang, to_lang, 'translation unavailable')
                _search_providers_instead(media_type, None, None, None, source=source)
                return None
        validate_translation_params(video_path, source_srt_file, from_lang, to_lang)
        lang_obj, orig_to_lang = convert_language_codes(to_lang, forced, hi)

        logging.debug(f'BAZARR is translating in {lang_obj} this subtitles {source_srt_file}')  # noqa: G004

        # Both naming sites resolve the destination through one helper: this
        # write path and the sports operation binding, whose bound destination
        # the sports write guard compares against this one. The name carries
        # the regional tag when the target is a custom language that has one,
        # with the forced and HI tags in their usual place behind it.
        dest_srt_file = translated_destination_path(video_path, to_lang, forced, hi)

        # An auto path captures the destination's versions when it queues this
        # job (see _trigger_auto_translation). A subtitle that landed since is
        # the better one and this job publishes nothing: the check refuses the
        # job before any AI work is done, and the write guard refuses again if
        # the file lands mid-run. That second refusal is classified at the end
        # of this function, where a job that did some work lands too.
        if expected_destination_versions:
            try:
                validate_subtitle_versions(expected_destination_versions)
            except (SubtitleSourceChanged, SubtitleDestinationChanged):
                if source is not None:
                    raise SourceTranslationChanged('The subtitle input or destination changed') from None
                _report_destination_skip(job_id, from_lang, to_lang)
                return None

        # The stored setting is what the factory gets. Every spelling of "no
        # translator" is normalized to 'none' at boot and at save time, and the
        # availability gate keeps any of them from reaching this job at all, so
        # there is no default to invent here: a switched off translator is not
        # secretly a Google one.
        translator_type = settings.translator.translator_type
        logging.debug(f'Using translator type: {translator_type}')  # noqa: G004

        translator_arguments = dict(
            source_srt_file=source_srt_file,
            dest_srt_file=dest_srt_file,
            lang_obj=lang_obj,
            from_lang=from_lang,
            to_lang=alpha3_from_alpha2(to_lang),
            media_type=media_type,
            video_path=video_path,
            orig_to_lang=orig_to_lang,
            forced=forced,
            hi=hi,
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_id=radarr_id,
            arr_instance_id=arr_instance_id,
            expected_destination_versions=expected_destination_versions,
            **({'sports_operation': sports_operation, 'cancel': cancel} if sports_operation else {}),
            **({'source': source, 'source_video_version': source_video_version} if source is not None else {}),
        )
        if runtime_on:
            # The selected engine of the runtime, with its failover, fallback and checkpoints, replaces the
            # factory. The service it returns answers the same translate() call.
            from .runtime.service import RuntimeTranslatorService
            translator = RuntimeTranslatorService(
                **translator_arguments, source_key=source_key,
                **({'pinned_connection_id': source_pin} if source_pin is not None else {}))
        else:
            translator = TranslatorFactory.create_translator(translator_type, **translator_arguments)

        if source_tool is not None:
            translator.source_tool = source_tool

        logging.debug(f'Created translator instance: {translator.__class__.__name__}')  # noqa: G004
        result = translator.translate(job_id=job_id)
        if getattr(translator, 'skipped', False) is True:
            # Another job already runs this source and target, or a manual pick replaced this one.
            _report_runtime_skip(job_id, from_lang, to_lang, 'replaced or already running')
            return None
        if result is False:
            raise RuntimeError(f'{translator.__class__.__name__} returned a failed translation result')
        logging.debug(f'BAZARR saved translated subtitles to {dest_srt_file}')  # noqa: G004

        # Call postprocess_subtitles after translation (handles chmod, re-indexing, events)
        # The owning instance goes with it (#156). postprocess_subtitles
        # re-indexes the new file, and that write is scoped now, so without
        # the owner the translated subtitle can land on a sibling instance's
        # row and leave the one the user asked about showing nothing.
        if sports_operation is None:
            from api.subtitles.subtitles import postprocess_subtitles
            if source is not None:
                postprocess_subtitles(dest_srt_file, video_path, "source", None, source.item_id,
                                      source=source)
            else:
                postprocess_subtitles(dest_srt_file, video_path, media_type, metadata,
                                      sonarr_episode_id if media_type == 'episode' else radarr_id,
                                      arr_instance_id=arr_instance_id)

        # The translated file is now on disk. The download-time combine ran before
        # this async translation finished (so it skipped, source missing); build or
        # refresh the combined output now if the profile rule's sources are all
        # present. try_combine_for_video is best-effort and _profile_for accepts the
        # 'episode'/'movies'/'movie' media_type this path uses.
        try:
            from subtitles.tools.combine.main import try_combine_for_video
            try_combine_for_video(
                video_path=video_path,
                media_type=media_type,
                sonarr_series_id=sonarr_series_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_id=radarr_id,
                arr_instance_id=arr_instance_id,
                **({'sports_operation': sports_operation, 'cancel': cancel} if sports_operation else {}),
                **({'source': source} if source is not None else {}),
            )
        except JobCancelled:
            raise
        except Exception:
            if cancel is not None:
                from sportarr.connection import check_cancelled
                check_cancelled(cancel)
            logging.exception("BAZARR combine-after-translate failed for %s", video_path)

        # The file is on disk and post-processing has run, so this is the real
        # publication boundary. A partial translation is not a successful one.
        partial_detail = getattr(translator, 'partial_error', None)
        activity.note_publication(observed, outcome='partial' if partial_detail else 'success',
                                  detail=partial_detail)

        # The item is served now, so a failure on record for it must not hold
        # the next scan back from translating anything else it is asked for.
        clear_failed_translation(*failure_key, **failure_owner)

        # Get current job name (which batch.py already set with title) and mark as done
        completion_label = 'Partially translated' if partial_detail else 'Translated'
        current_name = jobs_queue.get_job_name(job_id)
        if current_name and 'Translating' in current_name:
            done_name = current_name.replace('Translating', completion_label)
        else:
            done_name = f'{completion_label} {from_lang.upper()} \u2192 {to_lang.upper()} using {translator_label}'
        jobs_queue.update_job_name(job_id=job_id, new_job_name=done_name)
        # Done: whatever pick this request had no longer waits on anything.
        _pick.forget(_pick_key(video_path, source_srt_file, to_lang, forced, hi, embedded_source, source,
                               source_video_version, source_tool, from_lang), job_id)
        return result

    except SourceTranslationChanged as error:
        logging.info('BAZARR source translation skipped for item %s: %s', source.item_id, error)
        if runtime_on:
            from .runtime import enqueue

            enqueue.cancel_waiting_for(job_id, source_key, to_lang, forced, hi, str(error))
        _report_runtime_skip(job_id, from_lang, to_lang, str(error))
        if expected_destination_versions and isinstance(error, SourceTranslationInputAmbiguous):
            _search_providers_instead(media_type, None, None, None, source=source)
        return None
    except JobCancelled:
        raise
    except Exception as e:
        quiet = _runtime_ends_quietly(e)
        if quiet == 'gate_closed':
            # The gate closed while this job waited in the queue: nothing ran, and the normal provider search
            # takes over for the item instead of leaving its language missing.
            logging.info('BAZARR translation skipped, the gate closed while it waited: %s', e)
            _report_runtime_skip(job_id, from_lang, to_lang, 'translation unavailable')
            _search_providers_instead(media_type, radarr_id, sonarr_episode_id, arr_instance_id,
                                      **({'source': source} if source is not None else {}))
            return None
        if quiet == 'declined':
            # Every engine asked declined the job; the provider search result stays in place. The item is held
            # back like a failed one, so the next scan does not offer the same job again.
            logging.info('BAZARR translation declined: %s', e)
            record_failed_translation(*failure_key, **failure_owner)
            _report_runtime_skip(job_id, from_lang, to_lang, 'declined')
            return None
        if (expected_destination_versions or source is not None) and _destination_race(e):
            # Only a job an auto path queued with the capture reports the
            # moved destination as a skip. A sports operation's bound versions
            # are its own contract and keep failing loudly there, and a manual
            # translation is never silently dropped.
            _report_destination_skip(job_id, from_lang, to_lang)
            return None
        activity.note_publication(observed, outcome='failed', detail=str(e))
        logging.error(f'Translation failed: {str(e)}', exc_info=True)  # noqa: G004, G201
        # Nothing was published, so the next scan would offer the same doomed
        # job again: hold this item back until the hold expires or the world
        # it described changes. A cancelled job is not a failed translation,
        # and neither is one that found the OCR slot taken, its OCR output
        # name taken or its track unlisted: those say nothing about whether
        # this item can be translated, and the user asked for it by hand.
        if getattr(e, 'reason', None) not in NO_HOLD_REASONS and not getattr(e, 'retry_later', False):
            record_failed_translation(*failure_key, **failure_owner)
        current_name = jobs_queue.get_job_name(job_id)
        if current_name and 'Translating' in current_name:
            fail_name = current_name.replace('Translating', 'Failed')
        else:
            fail_name = f'Failed: {from_lang.upper()} → {to_lang.upper()} using {translator_label}'
        jobs_queue.update_job_name(job_id=job_id, new_job_name=fail_name)
        # A TranslationServiceError is a JobFailed, so its reason becomes the
        # job's error. Anything else keeps its type: synchronous callers answer
        # a ValueError as a 409.
        raise
    finally:
        if runtime_on and source is not None and source_video_version is not None:
            from .runtime import enqueue
            from subtitles.tools.subsync_engines import subtitle_source_version

            current_version = subtitle_source_version(video_path)
            if current_version is not None and current_version != source_video_version:
                enqueue.retire_source_retry(source_key, to_lang, forced, hi)


def _extract_embedded_source(video_path, media_type, embedded_source, job_id, arr_instance_id,
                             media_id=None):
    """The text source this translation reads, inside the job.

    An embedded text track is extracted into the persistent extracted_subs
    cache. An image track (PGS or VobSub) is read with OCR into an ordinary SRT
    next to the video, and that SRT is the translation source: translating
    from an image track is a feature, the row's menu says so before it is
    clicked. OCR runs only when the request names the track's image codec. A
    request without a codec (a row whose format is not known) never starts an
    OCR run by accident: a failed text extraction there keeps its own reason.

    The OCR run happens inside this job, so the job keeps its place in the
    jobs queue (and in the translation lane) for the whole run. That is
    deliberate: one OCR run exists per process, a request that would start a
    second one is refused up front with a 409, and a job that still finds the
    slot taken ends at once with the busy reason instead of waiting. At most
    one translation therefore sits in the lane doing OCR, and it counts as an
    OCR job only until its reading is over. The cost is head of line: with
    ``translator.openrouter_max_concurrent`` at 1 this job fills the whole
    translation lane through its OCR and its translation, and the jobs queued
    behind it wait (the queue starts the job at the front of pending). Nothing
    is reordered for it: the only jobs that go ahead of the front are one a
    person started that waited for a full lane of its own (once that lane has
    room) and one started through ``force_start_pending_job`` while its lane
    has room, and those exceptions apply to every job alike.
    """
    from app.jobs_queue import JobFailed
    from subtitles.new_subtitle import SubtitleFileExists
    from subtitles.tools.embedded_extract import ExtractionError, TrackNotFound
    from subtitles.tools.embedded_ocr import (OcrBusy, job_progress, ocr_embedded, ocr_phase_over,
                                              ocr_phase_started)
    from subtitles.tools.embedded_streams import codec_label, is_bitmap_codec, is_ocr_codec
    from subtitles.tools.ocr.errors import OcrError
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    language = embedded_source.get('language')
    hi = bool(embedded_source.get('hi'))
    forced = bool(embedded_source.get('forced'))
    requested_codec = embedded_source.get('codec')

    if requested_codec and is_bitmap_codec(requested_codec) and not is_ocr_codec(requested_codec):
        raise JobFailed(f'This track is {codec_label(requested_codec)}, an image format that OCR cannot read, '
                        'so there is no text to translate.')

    if not (requested_codec and is_ocr_codec(requested_codec)):
        jobs_queue.update_job_progress(job_id=job_id,
                                       progress_message='Extracting the embedded subtitle track')
        extracted = extract_embedded_subtitle(
            video_path,
            language,
            media_type,
            hi=hi,
            forced=forced,
            arr_instance_id=arr_instance_id,
        )
        if extracted:
            return extracted
        if requested_codec:
            raise JobFailed('Could not extract the embedded subtitle track, or the language track '
                            'was not found.')
        raise JobFailed('Could not extract the embedded subtitle track. If it is an image-based track (PGS or '
                        'VobSub), scan the disk on this title so Bazarr+ knows its format, then translate '
                        'from it again. Otherwise the language track was not found.')

    # An image track the request named. OCR it into an SRT, then translate that file.
    jobs_queue.update_job_progress(job_id=job_id,
                                   progress_message='Reading the source track with OCR')
    ocr_phase_started(job_id)
    try:
        path, _result = ocr_embedded(
            media_type,
            media_id,
            arr_instance_id,
            language,
            hi,
            forced,
            codec=requested_codec,
            progress=job_progress(job_id, language),
        )
    except SubtitleFileExists as error:
        # An SRT at this name may be a download, or OCR of another image track
        # under the same language key. Its origin cannot be proven, so it is
        # never fed to the translator as this source.
        raise JobFailed(f"{error}. Delete it first to read this track with OCR.",
                        reason=OCR_SOURCE_TAKEN_REASON) from error
    except OcrBusy as error:
        # Another OCR run holds the process's slot. Nothing is wrong with this
        # item, so the job's failure handler must not hold it back.
        raise JobFailed(str(error), reason=OCR_BUSY_REASON) from error
    except TrackNotFound as error:
        raise JobFailed(str(error), reason=OCR_SOURCE_UNLISTED_REASON) from error
    except (ExtractionError, OcrError) as error:
        raise JobFailed(str(error)) from error
    finally:
        # However the reading ended, the job is past its OCR phase: it only
        # translates (or is over), and must not keep OCR requests out.
        ocr_phase_over(job_id)
    jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=0,
                                   progress_message='Translating the subtitle')
    return path

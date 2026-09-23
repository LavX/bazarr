# coding=utf-8

"""Subtitle editor translations, run as ordinary Bazarr jobs.

The editor used to hand its lines straight to the AI Subtitle Translator and poll
the service from the browser, so the work never appeared in Jobs and was lost when
the editor closed. It now queues a job that sends the same request the library
translation sends, mirrors the service's progress onto the job, and leaves the
translated lines as the job's returned value for the editor to pick up.
"""

import logging

import requests

from app import activity
from app.jobs_queue import jobs_queue, JobCancelled
from utilities.job_dedupe import enqueue_or_existing

from .services.auth import get_translator_auth_headers
from .services.openrouter_translator import OpenRouterTranslatorService

logger = logging.getLogger(__name__)

EDITOR_TRANSLATION_MODULE = 'subtitles.tools.translate.editor'
EDITOR_TRANSLATION_FUNC = 'translate_editor_lines'


def editor_translation_label(title, source_language, target_language, line_count):
    """The job name, in the queue's "Translating <what> (<from> to <to>)" form.

    It has to start with "Translating": the queue counts a job against the
    translation concurrency lane by that word, and the completion rename below
    swaps it for "Translated".
    """
    languages = f'{source_language or "auto-detected"} to {target_language}'
    if line_count == 1:
        what = f'one line of {title}' if title else 'one line'
    else:
        what = title or 'subtitle'
    return f'Translating {what} in the editor ({languages})'


def enqueue_editor_translation(lines, source_language, target_language, title='', media_type=''):
    """Queue an editor translation and return its job id.

    ``lines`` is the editor's list of ``{"position", "line"}``. Sending the same
    lines to the same language twice while the first is still running follows the
    first job instead of paying for the translation again.
    """
    ordered = sorted(lines, key=lambda item: item['position'])
    kwargs = {
        'lines': [str(item['line']) for item in ordered],
        'positions': [int(item['position']) for item in ordered],
        'source_language': source_language or '',
        'target_language': target_language,
        'title': title or '',
        'media_type': media_type or '',
    }
    label = editor_translation_label(title, source_language, target_language, len(ordered))
    return enqueue_or_existing(label, EDITOR_TRANSLATION_MODULE, EDITOR_TRANSLATION_FUNC, kwargs,
                               is_progress=True, progress_max=100)


def _cancel_remote_job(remote_job_id, base_url):
    if not remote_job_id or not base_url:
        return
    try:
        requests.delete(f'{base_url.rstrip("/")}/api/v1/jobs/{remote_job_id}',
                        headers=get_translator_auth_headers(), timeout=10)
    except requests.exceptions.RequestException:
        logger.debug('Could not cancel AI Subtitle Translator job %s', remote_job_id)


def _rename(job_id, replacement):
    name = jobs_queue.get_job_name(job_id)
    if name and name.startswith('Translating'):
        jobs_queue.update_job_name(job_id=job_id, new_job_name=replacement + name[len('Translating'):])


def translate_editor_lines(lines, positions, source_language, target_language, title, media_type,
                           job_id=None):
    """Translate editor lines through the AI Subtitle Translator.

    Returns ``{"lines": [{"position", "line"}], "partial": <reason or None>}``.
    Raises with the service's reason when nothing usable comes back, so the job is
    recorded as failed.
    """
    from app.config import settings

    activity.register(activity.activity_id_for_job(job_id), operation='translation', scope_kind='editor',
                      title=title or None, language=target_language or None,
                      source_language=source_language or None)
    jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=100,
                                   progress_message='Sending to the AI Subtitle Translator')

    service = OpenRouterTranslatorService(
        source_srt_file=None, dest_srt_file=None, lang_obj=None, to_lang=None, from_lang=None,
        media_type=media_type, video_path=None, orig_to_lang=None, forced=False, hi=False,
        sonarr_series_id=None, sonarr_episode_id=None, radarr_id=None)
    try:
        translated = service.submit_content(lines, source_language, target_language, title,
                                            bazarr_job_id=job_id, mediaType=media_type)
    except JobCancelled:
        _cancel_remote_job(service.remote_job_id, settings.translator.openrouter_url)
        raise
    except Exception as error:
        # The reason goes on the job as well, so the drawer shows why it failed.
        jobs_queue.update_job_progress(job_id=job_id, progress_message=str(error), allow_cancelled=True)
        _rename(job_id, 'Failed')
        raise

    result_lines = []
    for item in translated:
        index = item.get('position') if isinstance(item, dict) else None
        if isinstance(index, int) and 0 <= index < len(positions):
            result_lines.append({'position': positions[index], 'line': item.get('line', '')})

    _rename(job_id, 'Partially translated' if service.partial_error else 'Translated')
    return {'lines': result_lines, 'partial': service.partial_error}


def editor_translation_state(job_id):
    """What the editor needs to know about one of its translation jobs, or None.

    Only editor translation jobs are answered, so this cannot be used to read the
    returned value of some other job.
    """
    job = next(iter(jobs_queue.list_jobs_from_queue(job_id=job_id)), None)
    if not job or job.get('module') != EDITOR_TRANSLATION_MODULE or job.get('func') != EDITOR_TRANSLATION_FUNC:
        return None
    status = job.get('status')
    state = {'jobId': job['job_id'], 'status': status}
    if status == 'completed':
        returned = job.get('job_returned_value')
        if not isinstance(returned, dict):
            # The queue completes a cancelled job without a returned value.
            state['status'] = 'cancelled'
        else:
            state['lines'] = returned.get('lines') or []
            state['partial'] = returned.get('partial')
    elif status == 'failed':
        state['error'] = job.get('error') or job.get('progress_message') or 'Translation failed.'
    return state

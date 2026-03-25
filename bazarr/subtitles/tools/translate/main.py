# coding=utf-8

import logging
import os

from subliminal_patch.core import get_subtitle_path
from subzero.language import Language

from .core.translator_utils import validate_translation_params, convert_language_codes, get_title
from .services.translator_factory import TranslatorFactory
from languages.get_languages import alpha3_from_alpha2
from app.config import settings
from app.jobs_queue import jobs_queue
from app.event_handler import event_stream
from subtitles.indexer.series import store_subtitles
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.indexer.utils import get_external_subtitles_path
from utilities.path_mappings import path_mappings


def translate_subtitles_file(video_path, source_srt_file, from_lang, to_lang, forced, hi,
                             media_type, sonarr_series_id, sonarr_episode_id, radarr_id, job_id=None):
    if not job_id:
        title = get_title(media_type, radarr_id, sonarr_series_id, sonarr_episode_id)
        translator_name = settings.translator.translator_type.replace("_", " ").title()
        job_label = f'Translating {title} ({from_lang.upper()} to {to_lang.upper()})' if title else \
                    f'Translating from {from_lang.upper()} to {to_lang.upper()} using {translator_name}'
        jobs_queue.add_job_from_function(job_label, is_progress=True)
        return

    translator_label = settings.translator.translator_type.replace("_", " ").title()
    try:
        logging.debug(f'Translation request: video={video_path}, source={source_srt_file}, from={from_lang}, to={to_lang}')

        validate_translation_params(video_path, source_srt_file, from_lang, to_lang)
        lang_obj, orig_to_lang = convert_language_codes(to_lang, forced, hi)

        logging.debug(f'BAZARR is translating in {lang_obj} this subtitles {source_srt_file}')

        dest_srt_file_if_alongside_video = get_subtitle_path(
            video_path,
            language=lang_obj if isinstance(lang_obj, Language) else lang_obj.subzero_language(),
            extension='.srt',
            forced_tag=forced,
            hi_tag=hi
        )

        dest_srt_file = get_external_subtitles_path(
            file=video_path,
            subtitle=os.path.basename(dest_srt_file_if_alongside_video)
        )

        translator_type = settings.translator.translator_type or 'google'
        logging.debug(f'Using translator type: {translator_type}')

        translator = TranslatorFactory.create_translator(
            translator_type,
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
            radarr_id=radarr_id
        )

        logging.debug(f'Created translator instance: {translator.__class__.__name__}')
        result = translator.translate(job_id=job_id)
        logging.debug(f'BAZARR saved translated subtitles to {dest_srt_file}')

        # Re-index subtitles so Bazarr's DB picks up the new file
        if media_type == "series" and sonarr_series_id:
            store_subtitles(path_mappings.path_replace_reverse(video_path), video_path)
            event_stream(type='series', payload=sonarr_series_id)
            if sonarr_episode_id:
                event_stream(type='episode', payload=sonarr_episode_id)
        elif media_type == "movies" and radarr_id:
            store_subtitles_movie(path_mappings.path_replace_reverse_movie(video_path), video_path)
            event_stream(type='movie', payload=radarr_id)

        # Get current job name (which batch.py already set with title) and mark as done
        current_name = jobs_queue.get_job_name(job_id)
        if current_name and 'Translating' in current_name:
            done_name = current_name.replace('Translating', 'Translated')
        else:
            done_name = f'Translated {from_lang.upper()} → {to_lang.upper()} using {translator_label}'
        jobs_queue.update_job_name(job_id=job_id, new_job_name=done_name)
        return result

    except Exception as e:
        logging.error(f'Translation failed: {str(e)}', exc_info=True)
        current_name = jobs_queue.get_job_name(job_id)
        if current_name and 'Translating' in current_name:
            fail_name = current_name.replace('Translating', 'Failed')
        else:
            fail_name = f'Failed: {from_lang.upper()} → {to_lang.upper()} using {translator_label}'
        jobs_queue.update_job_name(job_id=job_id, new_job_name=fail_name)
        raise

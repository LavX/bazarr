# coding=utf-8

import logging
import pysubs2
from sportarr.profile_hooks import sports_write_kwargs, finish_translation
from subtitles.tools.subsync_engines import staged_subtitle_write
from media_servers.events import publication_callback

from retry.api import retry
from app.config import settings  # noqa: F401
from ..core.translator_utils import add_translator_info, create_process_result
from sonarr.history import history_log
from radarr.history import history_log_movie
from deep_translator import GoogleTranslator
from concurrent.futures import ThreadPoolExecutor
from utilities.path_mappings import path_mappings  # noqa: F401
from subtitles.processing import ProcessSubtitlesResult  # noqa: F401
from app.jobs_queue import jobs_queue, JobCancelled
from sportarr.connection import check_cancelled
from deep_translator.exceptions import TooManyRequests, RequestError, TranslationNotFound
from languages.get_languages import alpha3_from_alpha2, language_from_alpha2, language_from_alpha3  # noqa: F401

logger = logging.getLogger(__name__)


class GoogleTranslatorService:

    def __init__(self, source_srt_file, dest_srt_file, lang_obj, to_lang, from_lang, media_type,
                 video_path, orig_to_lang, forced, hi, sonarr_series_id, sonarr_episode_id,
                 radarr_id, arr_instance_id=None, sports_operation=None, cancel=None):
        self.source_srt_file = source_srt_file
        self.dest_srt_file = dest_srt_file
        self.lang_obj = lang_obj
        self.to_lang = to_lang
        self.from_lang = from_lang
        self.media_type = media_type
        self.video_path = video_path
        self.orig_to_lang = orig_to_lang
        self.forced = forced
        self.hi = hi
        self.sonarr_series_id = sonarr_series_id
        self.sonarr_episode_id = sonarr_episode_id
        self.radarr_id = radarr_id
        # The owning arr instance (#156): radarrId and sonarrSeriesId are only
        # unique together with it, so every media lookup below carries it.
        self.arr_instance_id = arr_instance_id
        self.sports_operation = sports_operation
        self.cancel = cancel
        self.partial_error = None
        self.language_code_convert_dict = {
            'he': 'iw',
            'zh': 'zh-CN',
            'zt': 'zh-TW',
        }

    def translate(self, job_id):
        self.partial_error = None
        try:
            with staged_subtitle_write(self.video_path, self.dest_srt_file,
                                       on_publish=publication_callback(self.media_type, self.video_path,
                                                                       'translate', self.arr_instance_id),
                                       source_paths=(self.source_srt_file,),
                                       before_publish=lambda: jobs_queue.update_job_progress(job_id=job_id),
                                       allow_empty=not bool(self.sports_operation),
                                       **sports_write_kwargs(self, job_id)) as temporary:
                subs = pysubs2.load(self.source_srt_file, encoding='utf-8')
                if not self.sports_operation:
                    subs.remove_miscellaneous_events()
                lines_list = [x.plaintext for x in subs]
                lines_list_len = len(lines_list)

                jobs_queue.update_job_progress(job_id=job_id, progress_max=lines_list_len,
                                               progress_message=self.source_srt_file)

                translated_lines = []
                completed = set()
                logger.debug(f'starting translation for {self.source_srt_file}')  # noqa: G004

                def translate_line(line_id, subtitle_line):
                    try:
                        translated_text = self._translate_text(subtitle_line, job_id)
                        translated_lines.append({'id': line_id, 'line': translated_text})
                        if subtitle_line.strip() and isinstance(translated_text, str) and translated_text.strip():
                            completed.add(line_id)
                    except TranslationNotFound:
                        logger.debug(f'Unable to translate line {subtitle_line}')  # noqa: G004
                        translated_lines.append({'id': line_id, 'line': subtitle_line})
                    finally:
                        jobs_queue.update_job_progress(job_id=job_id, progress_value=len(translated_lines))

                logger.debug(f'BAZARR is sending {lines_list_len} blocks to Google Translate')  # noqa: G004
                pool = ThreadPoolExecutor(max_workers=10)
                futures = []
                for i, line in enumerate(lines_list):
                    future = pool.submit(translate_line, i, line)
                    futures.append(future)
                pool.shutdown(wait=True)
                for future in futures:
                    try:
                        future.result()
                    except JobCancelled:
                        raise
                    except Exception as e:
                        logger.error(f"Error in translation task: {e}")  # noqa: G004

                if self.sports_operation:
                    check_cancelled(self.cancel)
                    if not completed:
                        raise RuntimeError('Google returned no usable translated text')
                    missing = sum(bool(line.strip()) for line in lines_list) - len(completed)
                    if missing:
                        self.partial_error = f'No translated text was returned for {missing} cues.'

                for i, line in enumerate(translated_lines):
                    if self.sports_operation and line['id'] not in completed:
                        continue
                    lines_list[line['id']] = line['line']

                logger.debug(f'BAZARR saving translated subtitles to {self.dest_srt_file}')  # noqa: G004
                for i, line in enumerate(subs):
                    try:
                        if lines_list[i]:
                            line.plaintext = lines_list[i]
                        else:
                            # we assume that there was nothing to translate if Google returns None. ex.: "♪♪"
                            continue
                    except IndexError:
                        logger.error(f'BAZARR is unable to translate malformed subtitles: {self.source_srt_file}')  # noqa: G004
                        jobs_queue.update_job_progress(job_id=job_id,
                                                       progress_message=f'Translation failed: Unable to translate '
                                                                        f'malformed subtitles for {self.source_srt_file}')
                        raise

                try:
                    subs.save(temporary)
                    add_translator_info(temporary, f"# Subtitles translated with Google Translate # ")  # noqa: F541
                except OSError:
                    logger.error(f'BAZARR is unable to save translated subtitles to {self.dest_srt_file}')  # noqa: G004
                    jobs_queue.update_job_progress(job_id=job_id,
                                                   progress_message=f'Translation failed: Unable to save translated '
                                                                    f'subtitles to {self.dest_srt_file}')
                    raise OSError

            message = f"{language_from_alpha2(self.from_lang)} subtitles translated to {language_from_alpha3(self.to_lang)}."
            result = create_process_result(message, self.video_path, self.orig_to_lang, self.forced, self.hi, self.dest_srt_file, self.media_type,
                                           **({"sports_context": self.sports_operation.context}
                                              if self.sports_operation else {}))

            if finish_translation(self, result):
                return self.dest_srt_file
            if self.media_type == 'episode':
                history_log(action=6, sonarr_series_id=self.sonarr_series_id, sonarr_episode_id=self.sonarr_episode_id, result=result)
            else:
                history_log_movie(action=6, radarr_id=self.radarr_id, result=result)

            return self.dest_srt_file

        except Exception as e:
            logger.error(f'BAZARR encountered an error during translation: {str(e)}')  # noqa: G004
            jobs_queue.update_job_progress(job_id=job_id,
                                           progress_message=f'Google translation failed: {str(e)}')
            raise

    @retry(exceptions=(TooManyRequests, RequestError), tries=6, delay=1, backoff=2, jitter=(0, 1))
    def _translate_text(self, text, job_id):
        try:
            if self.sports_operation:
                jobs_queue.update_job_progress(job_id=job_id)
            return GoogleTranslator(
                source='auto',
                target=self.language_code_convert_dict.get(self.lang_obj.alpha2, self.lang_obj.alpha2)
            ).translate(text=text)
        except (TooManyRequests, RequestError) as e:
            logger.error(f'Google Translate API error after retries: {str(e)}')  # noqa: G004
            jobs_queue.update_job_progress(job_id=job_id,
                                           progress_message=f'Google Translate API error: {str(e)}')
            raise
        except Exception as e:
            logger.error(f'Unexpected error in Google translation: {str(e)}')  # noqa: G004
            jobs_queue.update_job_progress(job_id=job_id,
                                           progress_message=f'Translation error: {str(e)}')
            raise

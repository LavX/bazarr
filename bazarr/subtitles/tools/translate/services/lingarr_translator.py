# coding=utf-8

import logging
import pysubs2
from sportarr.profile_hooks import sports_write_kwargs, finish_translation
from subtitles.tools.subsync_engines import staged_subtitle_write
from media_servers.events import publication_callback
import requests
import time

from retry.api import retry
from deep_translator.exceptions import TooManyRequests, RequestError

from app.config import settings
from app.database import TableShows, TableEpisodes, TableMovies, database, select  # noqa: F401
from app.jobs_queue import jobs_queue, JobCancelled
from sportarr.connection import check_cancelled
from languages.custom_lang import CustomLanguage  # noqa: F401
from languages.get_languages import alpha3_from_alpha2, language_from_alpha2, language_from_alpha3  # noqa: F401
from radarr.history import history_log_movie
from sonarr.history import history_log
from subtitles.processing import ProcessSubtitlesResult  # noqa: F401
from utilities.path_mappings import path_mappings  # noqa: F401

from ..core.translator_utils import add_translator_info, create_process_result, get_title

logger = logging.getLogger(__name__)


class LingarrAuthError(Exception):
    """Raised for authentication failures that should not be retried."""
    pass


class LingarrTranslatorService:
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
            'zh': 'zh-CN',
            'zt': 'zh-TW',
            'pb': 'pt-BR',
        }

    def translate(self, job_id=None):
        try:
            with staged_subtitle_write(self.video_path, self.dest_srt_file,
                                       on_publish=publication_callback(self.media_type, self.video_path,
                                                                       'translate', self.arr_instance_id),
                                       source_paths=(self.source_srt_file,),
                                       before_publish=lambda: jobs_queue.update_job_progress(job_id=job_id),
                                       allow_empty=not bool(self.sports_operation),
                                       **sports_write_kwargs(self, job_id)) as temporary:
                jobs_queue.update_job_progress(job_id=job_id, progress_max=1, progress_message=self.source_srt_file)

                subs = pysubs2.load(self.source_srt_file, encoding='utf-8')
                lines_list = [x.plaintext for x in subs]
                lines_list_len = len(lines_list)

                if lines_list_len == 0:
                    if self.sports_operation:
                        raise RuntimeError('Lingarr source has no subtitle cues')
                    logger.debug('No lines to translate in subtitle file')
                    return self.dest_srt_file

                logger.debug(f'Starting translation for {self.source_srt_file}')  # noqa: G004
                translated_lines = (self._translate_sports_lines(lines_list, job_id)
                                    if self.sports_operation else self._translate_content(lines_list, job_id=job_id))

                if translated_lines is None:
                    logger.error(f'Translation failed for {self.source_srt_file}')  # noqa: G004
                    jobs_queue.update_job_progress(job_id=job_id,
                                                   progress_message=f'Translation failed for {self.source_srt_file}')
                    raise RuntimeError(f'Translation failed for {self.source_srt_file}')

                logger.debug(f'BAZARR saving Lingarr translated subtitles to {self.dest_srt_file}')  # noqa: G004
                translation_map = {}
                for item in translated_lines:
                    if isinstance(item, dict) and 'position' in item and 'line' in item:
                        translation_map[item['position']] = item['line']

                for i, line in enumerate(subs):
                    if i in translation_map and translation_map[i]:
                        line.text = translation_map[i]

                try:
                    subs.save(temporary)
                    add_translator_info(temporary, f"# Subtitles translated with Lingarr # ")  # noqa: F541
                except OSError:
                    logger.error(f'BAZARR is unable to save translated subtitles to {self.dest_srt_file}')  # noqa: G004
                    jobs_queue.update_job_progress(job_id=job_id,
                                                   progress_message=f'Translation failed: Unable to save translated '
                                                                    f'subtitles to {self.dest_srt_file}')
                    raise OSError

            message = (f"{language_from_alpha2(self.from_lang)} subtitles translated to "
                       f"{language_from_alpha3(self.to_lang)} using Lingarr.")
            result = create_process_result(message, self.video_path, self.orig_to_lang, self.forced, self.hi,
                                           self.dest_srt_file, self.media_type,
                                           **({"sports_context": self.sports_operation.context}
                                              if self.sports_operation else {}))

            if finish_translation(self, result):
                return self.dest_srt_file
            if self.media_type == 'episode':
                history_log(action=6,
                            sonarr_series_id=self.sonarr_series_id,
                            sonarr_episode_id=self.sonarr_episode_id,
                            result=result)
            else:
                history_log_movie(action=6,
                                  radarr_id=self.radarr_id,
                                  result=result)

            jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

            return self.dest_srt_file

        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f'BAZARR encountered an error during Lingarr translation: {str(e)}')  # noqa: G004
            jobs_queue.update_job_progress(job_id=job_id, progress_message=f'Lingarr translation failed: {str(e)}')
            raise

    def _check_cancelled(self, job_id):
        jobs_queue.update_job_progress(job_id=job_id)
        check_cancelled(self.cancel)

    def _translate_sports_lines(self, lines, job_id):
        """Lingarr's line endpoint has no native media identity or remote job."""
        self.partial_error = None
        completed = []
        jobs_queue.update_job_progress(job_id=job_id, progress_value=0,
                                       progress_max=sum(bool(line.strip()) for line in lines))
        for position, line in enumerate(lines):
            self._check_cancelled(job_id)
            if not line.strip():
                continue
            payload = {
                'subtitleLine': line,
                'sourceLanguage': self.language_code_convert_dict.get(self.from_lang, self.from_lang),
                'targetLanguage': self.language_code_convert_dict.get(self.orig_to_lang, self.orig_to_lang),
                'contextLinesBefore': lines[max(0, position - 2):position],
                'contextLinesAfter': lines[position + 1:position + 3],
            }
            try:
                translated = self._translate_sports_line(payload, job_id)
            except JobCancelled:
                raise
            except Exception as exc:
                self._check_cancelled(job_id)
                if not completed:
                    raise
                self.partial_error = f'Lingarr stopped after {len(completed)} translated cues: {exc}'[:500]
                logger.warning('%s', self.partial_error)
                break
            completed.append({'position': position, 'line': translated})
            jobs_queue.update_job_progress(job_id=job_id, progress_value=len(completed))
        return completed or None

    def _translate_sports_line(self, payload, job_id):
        headers = {'Content-Type': 'application/json', 'Accept': 'text/plain'}
        if settings.translator.lingarr_token:
            headers['X-Api-Key'] = settings.translator.lingarr_token
        for attempt in range(3):
            self._check_cancelled(job_id)
            try:
                response = requests.post(
                    f"{settings.translator.lingarr_url.rstrip('/')}/api/translate/line",
                    json=payload, headers=headers, timeout=1800,
                )
                self._check_cancelled(job_id)
                if response.status_code == 429 or response.status_code >= 500:
                    raise RequestError(f'Lingarr line service returned HTTP {response.status_code}')
                if response.status_code != 200:
                    raise LingarrAuthError(f'Lingarr line request rejected: HTTP {response.status_code}')
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if content_type == 'application/json':
                    text = response.json()
                elif content_type == 'text/plain':
                    text = response.text
                else:
                    raise ValueError('Lingarr line service did not return text')
                if (not isinstance(text, str) or not text.strip()
                        or text.lstrip().lower().startswith(('<html', '<!doctype'))):
                    raise ValueError('Lingarr line service returned no usable translated text')
                return text
            except (RequestError, requests.exceptions.RequestException):
                if attempt == 2:
                    raise
                deadline = time.monotonic() + 2 ** attempt
                while time.monotonic() < deadline:
                    self._check_cancelled(job_id)
                    if self.cancel is not None:
                        self.cancel.wait(min(0.1, max(0, deadline - time.monotonic())))
                    else:
                        time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    # Retry schedule for transient errors (e.g. 502 during back-translator cold-start ~60s):
    # ~15s -> ~30s -> ~60s -> ~120s -> ~120s (max_delay caps last two intervals)
    # Note: retries block the calling thread. Jobs queue uses N worker threads
    # (settings.general.concurrent_jobs / settings.translator.openrouter_max_concurrent
    # for translation jobs) so other jobs are not starved during retry waits.
    # LingarrAuthError is intentionally NOT in the exceptions tuple so 401 surfaces immediately.
    @retry(exceptions=(TooManyRequests, RequestError, requests.exceptions.RequestException), tries=5, delay=15,
           backoff=2, jitter=(0, 5), max_delay=120)
    def _translate_content(self, lines_list, job_id):
        try:
            source_lang = self.language_code_convert_dict.get(self.from_lang, self.from_lang)
            target_lang = self.language_code_convert_dict.get(self.orig_to_lang, self.orig_to_lang)

            lines_payload = []
            for i, line in enumerate(lines_list):
                lines_payload.append({
                    "position": i,
                    "line": line
                })

            title = get_title(
                media_type=self.media_type,
                radarr_id=self.radarr_id,
                sonarr_series_id=self.sonarr_series_id,
                sonarr_episode_id=self.sonarr_episode_id,
                arr_instance_id=self.arr_instance_id
            )

            if self.media_type == 'episode':
                api_media_type = "Episode"
                arr_media_id = self.sonarr_series_id or 0
            else:
                api_media_type = "Movie"
                arr_media_id = self.radarr_id or 0

            payload = {
                "arrMediaId": arr_media_id,
                "title": title,
                "sourceLanguage": source_lang,
                "targetLanguage": target_lang,
                "mediaType": api_media_type,
                "lines": lines_payload
            }

            logger.debug(f'BAZARR is sending {len(lines_payload)} lines to Lingarr with full media context')  # noqa: G004

            headers = {"Content-Type": "application/json"}
            if settings.translator.lingarr_token:
                headers["X-Api-Key"] = settings.translator.lingarr_token

            response = requests.post(
                f"{settings.translator.lingarr_url}/api/translate/content",
                json=payload,
                headers=headers,
                timeout=1800
            )

            if response.status_code == 200:
                translated_batch = response.json()
                # Validate response
                if isinstance(translated_batch, list):
                    for item in translated_batch:
                        if not isinstance(item, dict) or 'position' not in item or 'line' not in item:
                            logger.error(f'Invalid response format from Lingarr API: {item}')  # noqa: G004
                            return None
                    return translated_batch
                else:
                    logger.error(f'Unexpected response format from Lingarr API: {translated_batch}')  # noqa: G004
                    return None
            elif response.status_code == 401:
                raise LingarrAuthError("Authentication failed: Invalid or missing API key")
            elif response.status_code == 429:
                raise TooManyRequests("Rate limit exceeded")
            elif response.status_code >= 500:
                raise RequestError(f"Server error: {response.status_code}")
            else:
                logger.debug(f'Lingarr API error: {response.status_code} - {response.text}')  # noqa: G004
                return None

        except requests.exceptions.Timeout:
            logger.debug('Lingarr API request timed out')
            raise RequestError("Request timed out")
        except requests.exceptions.ConnectionError:
            logger.debug('Lingarr API connection error')
            raise RequestError("Connection error")
        except requests.exceptions.RequestException as e:
            logger.debug(f'Lingarr API request failed: {str(e)}')  # noqa: G004
            raise
        except (TooManyRequests, RequestError) as e:
            logger.error(f'Lingarr API error after retries: {str(e)}')  # noqa: G004
            jobs_queue.update_job_progress(job_id=job_id, progress_message=f'Lingarr API error: {str(e)}')
            raise
        except Exception as e:
            logger.error(f'Unexpected error in Lingarr translation: {str(e)}')  # noqa: G004
            jobs_queue.update_job_progress(job_id=job_id, progress_message=f'Translation error: {str(e)}')
            raise

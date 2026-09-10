# coding=utf-8

import re
import time
import logging
import pysubs2
from subtitles.tools.subsync_engines import staged_subtitle_write, SubtitleDestinationChanged
import requests
from typing import Optional, List, Dict, Any

from retry.api import retry
from deep_translator.exceptions import TooManyRequests, RequestError
from dynaconf.validator import ValidationError

from app.config import settings, normalize_openrouter_provider_order
from languages.get_languages import language_from_alpha2, language_from_alpha3
from radarr.history import history_log_movie
from sonarr.history import history_log
from app.event_handler import show_progress, hide_progress, show_message
from app.jobs_queue import jobs_queue, JobCancelled

from ..core.translator_utils import add_translator_info, create_process_result, get_title
from .auth import get_translator_auth_headers

logger = logging.getLogger(__name__)

PROVIDER_ROUTING_VALUES = ('throughput', 'nitro', 'price', 'floor', 'latency', 'default', 'smartfast', 'custom')
DEFAULT_PROVIDER_ROUTING = 'smartfast'
# What a routing we cannot make sense of falls back to, which is deliberately not the
# shipped default. A stored value outside PROVIDER_ROUTING_VALUES, or no stored value at
# all, is a config we do not understand, and smartfast refuses outright on an older
# sidecar. The plain sort translates on every version.
UNKNOWN_ROUTING_FALLBACK = 'throughput'
# Sidecars before this version forward provider.sort to OpenRouter verbatim, which
# rejects nitro, floor and default; a selector set to one of those three gets the plain
# sort each value stands for. Custom routing is gated on the same version because that is
# where order, only and allowFallbacks began to be forwarded, but it cannot degrade to a
# plain sort without sending the job to a provider the user excluded, so it refuses instead.
ROUTING_SHORTCUTS_MIN_SIDECAR = (1, 3, 4)
# The release that added smartfast to the sidecar's own provider.sort values. Older ones
# reject the sort outright, and no plain sort stands for "balance speed against price",
# so a selector set to smartfast refuses rather than silently routing some other way.
SMARTFAST_MIN_SIDECAR = (2, 0, 0)
ROUTING_PLAIN_SORT = {'nitro': 'throughput', 'floor': 'price', 'default': 'throughput'}
MODEL_ROUTING_SUFFIXES = ('floor', 'nitro', 'smartfast')
SIDECAR_VERSION_CACHE_SECONDS = 300
# How stale a reading may be when the caller is about to refuse work over it. A user told
# to update the translator will do so and retry within seconds, and answering that retry
# from a five-minute-old reading of the version they just replaced tells them their fix
# did not work. Only the refusing paths ask for a reading this fresh; the paths that
# merely degrade keep the full interval, because re-probing changes nothing they do.
SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS = 10
_sidecar_version_cache = {}

POLL_HARD_CAP_SECONDS = 12 * 3600
POLL_UNREACHABLE_LIMIT_SECONDS = 600
POLL_INTERVAL_SECONDS = 2


class ProviderRoutingError(ValueError):
    """The selected provider routing cannot be honored safely."""


def _model_variants(model_id):
    """A model id split into its base slug and its colon-separated variants."""
    parts = str(model_id or '').strip().split(':')
    return parts[0], parts[1:]


def _typed_routing_suffix(model_id):
    """The routing shortcut typed into a model id, wherever it sits, else None.

    A stacked id carries more than one, and the last one is the one the user typed
    most recently, which is the one the settings page adopts. Reading only the final
    colon segment would miss a shortcut sitting in front of a genuine variant, and
    would then forward that shortcut to OpenRouter, which does not know it.
    """
    shortcuts = [part for part in _model_variants(model_id)[1] if part.lower() in MODEL_ROUTING_SUFFIXES]
    return shortcuts[-1].lower() if shortcuts else None


def _strip_routing_suffixes(model_id):
    """``model_id`` without any routing shortcut, keeping genuine variants such as :free."""
    base, variants = _model_variants(model_id)
    # Empty segments go too. A trailing-colon typo used to survive as "author/model:",
    # which the legacy branch then rebuilt into "author/model::nitro".
    return ':'.join([base] + [part for part in variants
                              if part and part.lower() not in MODEL_ROUTING_SUFFIXES])


def reset_sidecar_version_cache():
    _sidecar_version_cache.clear()


def _parse_version(text):
    """'1.3.4', '1.3.4-rc1' or 'v1.3.4' -> (1, 3, 4); None when it does not start with digits."""
    numbers = re.match(r'v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', str(text or ''))
    if not numbers:
        return None
    return tuple(int(part or 0) for part in numbers.groups())


def sidecar_version(base_url, max_age=None):
    """The AI Subtitle Translator version behind ``base_url``, cached per URL.

    Returns a version tuple, or None when the health endpoint is unreachable or
    does not report a version. The probe is cheap and unauthenticated, and it is
    cached so a job of many batches asks once.

    ``max_age`` is how stale a cached reading the caller will accept, in seconds. A
    caller about to refuse work over the answer passes a short one so the user's retry
    is answered by a fresh probe rather than by the reading that failed them.
    """
    base_url = (base_url or '').rstrip('/')
    max_age = SIDECAR_VERSION_CACHE_SECONDS if max_age is None else max_age
    cached = _sidecar_version_cache.get(base_url)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    version = None
    try:
        response = requests.get(f"{base_url}/health", timeout=5)
        if response.status_code == 200:
            version = _parse_version(response.json().get('version'))
    except (requests.exceptions.RequestException, ValueError, AttributeError) as e:
        logger.debug("Could not read the AI Subtitle Translator version from %s: %s", base_url, e)
    _sidecar_version_cache[base_url] = (time.monotonic(), version)
    return version


def _require_routing_support(routing, minimum, degrade_on_unknown=False):
    """True when the service behind the configured URL can honor ``routing``.

    A version we read that is below the floor is actionable, so it raises and the message
    names the version to install. A version we could not read is not evidence of an old
    one: /health may be hidden behind a reverse proxy, or answer without a version field,
    while the job API works perfectly. Refusing there would fail every translation on a
    service that supports the routing, which is the failure this returns False to avoid.

    ``degrade_on_unknown`` says the caller has a plain sort it can safely fall back to.
    Custom has none: falling back would send the job to a provider the user excluded,
    which is the one outcome that mode exists to prevent, so it asks for a refusal.
    """
    version = sidecar_version(settings.translator.openrouter_url,
                              max_age=SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS)
    required = '.'.join(map(str, minimum))
    if version is None:
        if degrade_on_unknown:
            return False
        raise ProviderRoutingError(
            f"OpenRouter {routing} routing requires AI Subtitle Translator {required} or newer, and its "
            f"version could not be read from the service URL. Check the service URL and the translator.")
    if version < minimum:
        raise ProviderRoutingError(
            f"OpenRouter {routing} routing requires AI Subtitle Translator {required} or newer "
            f"(detected version: {'.'.join(map(str, version))}). Update the translator.")
    return True


def build_routing_config():
    """Resolve the outgoing model and provider settings together for both job APIs.

    Explicit smartfast/custom selections supersede old routing suffixes while
    preserving genuine model variants. Older shortcut selections retain their
    historical compatibility behavior.
    """
    routing = getattr(settings.translator, 'openrouter_provider_routing', None)
    # A missing setting is a weaker signal than an unreadable one, so both land on the
    # fallback rather than on the shipped default, which can refuse.
    understood = routing in PROVIDER_ROUTING_VALUES
    if not understood:
        logger.warning("Unusable OpenRouter provider routing %r, using %s", routing, UNKNOWN_ROUTING_FALLBACK)
        routing = UNKNOWN_ROUTING_FALLBACK
    model = getattr(settings.translator, 'openrouter_model', '')
    typed = _typed_routing_suffix(model)
    # Only a routing we understood may be upgraded by a suffix. Promoting a config we
    # could not read into smartfast would undo the fallback on the next line.
    if understood and routing not in ('smartfast', 'custom') and typed == 'smartfast':
        routing = 'smartfast'
    if routing in ('smartfast', 'custom'):
        if routing == 'custom':
            try:
                order = normalize_openrouter_provider_order(
                    getattr(settings.translator, 'openrouter_provider_order', []))
            except ValidationError as error:
                raise ProviderRoutingError(str(error)) from error
            if not order:
                raise ProviderRoutingError('OpenRouter custom routing requires at least one provider slug.')
            _require_routing_support(routing, ROUTING_SHORTCUTS_MIN_SIDECAR)
            provider = {'sort': 'default', 'order': order, 'only': list(order), 'allowFallbacks': False}
        else:
            if not _require_routing_support(routing, SMARTFAST_MIN_SIDECAR, degrade_on_unknown=True):
                logger.warning(
                    "Could not read the AI Subtitle Translator version, so smartfast routing cannot be "
                    "confirmed; sending %s instead of failing the translation", UNKNOWN_ROUTING_FALLBACK)
                return _strip_routing_suffixes(model), {'sort': UNKNOWN_ROUTING_FALLBACK}
            provider = {'sort': 'smartfast'}
        return _strip_routing_suffixes(model), provider
    # Only a shortcut that stands for a plain sort is honored here. :smartfast reaches
    # this point when the stored routing could not be read, and it has no plain sort, so
    # it comes off the id and the fallback below decides.
    if typed in ROUTING_PLAIN_SORT:
        # The slug already says how to route. A sidecar from 1.3.4 on drops the sort
        # for a typed shortcut anyway; an older one forwards both, so the sort has to
        # agree with the slug rather than with the setting. Only the adopted shortcut
        # goes back on: the sidecar reads a single trailing one, so leaving an earlier
        # shortcut in place would send it to OpenRouter as part of the model id.
        model = f'{_strip_routing_suffixes(model)}:{typed}'
        return model, {'sort': ROUTING_PLAIN_SORT[typed]}
    if routing in ROUTING_PLAIN_SORT:
        version = sidecar_version(settings.translator.openrouter_url)
        if version is None or version < ROUTING_SHORTCUTS_MIN_SIDECAR:
            plain = ROUTING_PLAIN_SORT[routing]
            logger.warning(
                "AI Subtitle Translator %s does not support the '%s' provider routing (needs 1.3.4), sending %s",
                '.'.join(map(str, version)) if version else 'of unknown version', routing, plain)
            routing = plain
    return _strip_routing_suffixes(model), {'sort': routing}


def build_provider_config():
    return build_routing_config()[1]


class OpenRouterTranslatorService:
    """
    Translates subtitles using external AI Subtitle Translator service.
    Uses async job queue for long-running translations.
    """

    def __init__(self, source_srt_file, dest_srt_file, lang_obj, to_lang, from_lang, media_type,
                 video_path, orig_to_lang, forced, hi, sonarr_series_id, sonarr_episode_id,
                 radarr_id, arr_instance_id=None):
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
        self.partial_error = None
        self.routing_error = None
        self.language_code_convert_dict = {
            'he': 'iw',
            'zh': 'zh-CN',
            'zt': 'zh-TW',
        }

    def _build_reasoning_config(self):
        """
        Build reasoning configuration based on Bazarr settings.
        Sends effort level directly to the AI Subtitle Translator service.
        """
        reasoning_mode = getattr(settings.translator, 'openrouter_reasoning', 'disabled')

        if reasoning_mode == 'disabled':
            return None

        return {
            'effort': reasoning_mode,
        }

    def _get_api_key_value(self):
        """Get the API key, encrypted if an encryption key is configured."""
        api_key = settings.translator.openrouter_api_key
        encryption_key = settings.translator.openrouter_encryption_key
        if encryption_key:
            try:
                from .encryption import encrypt_api_key
                api_key = encrypt_api_key(api_key, encryption_key)
            except ValueError as e:
                logger.error(f'Invalid encryption key: {e}')  # noqa: G004
                raise ValueError("Invalid encryption key format. Check your encryption key in Settings.")
        return api_key

    def translate(self, job_id=None):
        self.partial_error = None
        self.routing_error = None
        try:
            with staged_subtitle_write(self.video_path, self.dest_srt_file,
                                       source_paths=(self.source_srt_file,),
                                       before_publish=lambda: jobs_queue.update_job_progress(job_id=job_id),
                                       allow_empty=True) as temporary:
                subs = pysubs2.load(self.source_srt_file, encoding='utf-8')
                lines_list: List[str] = [x.plaintext for x in subs]
                lines_list_len = len(lines_list)

                if lines_list_len == 0:
                    logger.debug('No lines to translate in subtitle file')
                    return False

                logger.debug(f'Starting AI translation for {self.source_srt_file}')  # noqa: G004

                # Submit job and poll for completion
                translated_lines = self._submit_and_poll(lines_list, bazarr_job_id=job_id)

                if translated_lines is None:
                    # A routing refusal names what the user has to change, so it replaces
                    # the generic message rather than arriving alongside it.
                    failure = (f'AI translation failed: {self.routing_error}' if self.routing_error
                               else f'Translation failed for {self.source_srt_file}')
                    logger.error(failure)
                    show_message(failure)
                    return False

                # Process results
                logger.debug(f'BAZARR saving AI translated subtitles to {self.dest_srt_file}')  # noqa: G004
                translation_map = {}
                for item in translated_lines:
                    if isinstance(item, dict) and 'position' in item and 'line' in item:
                        translation_map[item['position']] = item['line']

                missing_lines = sum(bool(source.strip()) and not translation_map.get(i, '').strip()
                                    for i, source in enumerate(lines_list))
                if missing_lines and not self.partial_error:
                    self._mark_partial(f'No translated text was returned for {missing_lines} of {lines_list_len} cues.')

                for i, line in enumerate(subs):
                    if i in translation_map and translation_map[i].strip():
                        line.text = translation_map[i]

                subs.save(temporary)
                translated = 'partially translated' if self.partial_error else 'translated'
                add_translator_info(temporary, f"# Subtitles {translated} with AI Subtitle Translator #")

            message = f"{language_from_alpha2(self.from_lang)} subtitles {translated} to {language_from_alpha3(self.to_lang)} using AI Subtitle Translator."
            if self.partial_error:
                message += f' Some lines may remain in the source language. {self.partial_error}'
            result = create_process_result(message, self.video_path, self.orig_to_lang, self.forced, self.hi, self.dest_srt_file, self.media_type)

            if self.media_type == 'episode':
                history_log(action=6,
                            sonarr_series_id=self.sonarr_series_id,
                            sonarr_episode_id=self.sonarr_episode_id,
                            result=result)
            else:
                history_log_movie(action=6,
                                  radarr_id=self.radarr_id,
                                  result=result)

            return self.dest_srt_file

        except (JobCancelled, SubtitleDestinationChanged):
            raise
        except Exception as e:
            logger.error(f'BAZARR encountered an error during AI translation: {str(e)}')  # noqa: G004
            show_message(f'AI translation failed: {str(e)}')
            hide_progress(id=f'translate_progress_{self.dest_srt_file}')
            return False

    def _submit_and_poll(self, lines_list: List[str], bazarr_job_id=None) -> Optional[List[Dict[str, Any]]]:
        """Submit translation job and poll for completion with progress updates"""
        try:
            # Prepare language codes
            # from_lang should be alpha2 (e.g., "en")
            # orig_to_lang should be alpha2 (e.g., "hu")
            # to_lang is alpha3 (e.g., "hun")
            source_lang = self.from_lang
            target_lang = self.orig_to_lang  # Use original alpha2 code
            
            # Apply any special language code conversions
            source_lang = self.language_code_convert_dict.get(source_lang, source_lang)
            target_lang = self.language_code_convert_dict.get(target_lang, target_lang)

            # Resolve alpha2 codes to full language names for the AI translator prompt
            source_lang = language_from_alpha2(source_lang) or source_lang
            target_lang = language_from_alpha2(target_lang) or target_lang

            logger.debug(f'BAZARR translation language codes: from_lang={self.from_lang}, to_lang={self.to_lang}, '  # noqa: G004
                         f'orig_to_lang={self.orig_to_lang}, final source={source_lang}, final target={target_lang}')

            if not target_lang:
                logger.error(f'Target language is empty! from_lang={self.from_lang}, to_lang={self.to_lang}, orig_to_lang={self.orig_to_lang}')  # noqa: G004
                return None

            model, provider = build_routing_config()
            lines_payload: List[Dict[str, Any]] = [{"position": i, "line": line} for i, line in enumerate(lines_list)]

            title = get_title(
                media_type=self.media_type,
                radarr_id=self.radarr_id,
                sonarr_series_id=self.sonarr_series_id,
                sonarr_episode_id=self.sonarr_episode_id,
                arr_instance_id=self.arr_instance_id
            )

            api_media_type = "Episode" if self.media_type == 'episode' else "Movie"
            arr_media_id = self.sonarr_series_id if self.media_type == 'episode' else self.radarr_id or 0

            payload = {
                "arrMediaId": arr_media_id,
                "title": title,
                "sourceLanguage": source_lang,
                "targetLanguage": target_lang,
                "mediaType": api_media_type,
                "lines": lines_payload,
                # Add configuration from Bazarr settings
                "config": {
                    "apiKey": self._get_api_key_value(),
                    "model": model,
                    "temperature": settings.translator.openrouter_temperature,
                    "maxConcurrentJobs": settings.translator.openrouter_max_concurrent,
                    "parallelBatches": settings.translator.openrouter_parallel_batches,
                    "reasoning": self._build_reasoning_config(),
                    "provider": provider,
                }
            }

            base_url = settings.translator.openrouter_url.rstrip('/')

            # Submit job
            logger.debug(f'BAZARR submitting {len(lines_payload)} lines to AI Subtitle Translator')  # noqa: G004
            submit_response = requests.post(
                f"{base_url}/api/v1/jobs/translate/content",
                json=payload,
                headers={"Content-Type": "application/json", **get_translator_auth_headers()},
                timeout=30
            )

            if submit_response.status_code != 200:
                # Fallback to sync endpoint if job queue not available
                logger.debug('Job queue not available, falling back to sync endpoint')
                return self._translate_sync(lines_list, payload)

            job_data = submit_response.json()
            job_id = job_data.get("jobId")
            if not job_id:
                logger.error("No jobId returned from translation service")
                return None

            logger.debug(f'BAZARR translation job submitted: {job_id}')  # noqa: G004

            # Poll for completion
            return self._poll_job(base_url, job_id, len(lines_payload), bazarr_job_id=bazarr_job_id)

        except ProviderRoutingError as error:
            # Recorded rather than announced here: translate() reports every failed
            # submission, and showing the detail now would put two notifications on
            # screen for one failure, the second of which says less than the first.
            logger.error('AI Subtitle Translator routing error: %s', error)
            self.routing_error = str(error)
            return None
        except requests.exceptions.Timeout:
            logger.error('AI Subtitle Translator request timed out')
            return None
        except requests.exceptions.ConnectionError:
            logger.error('AI Subtitle Translator connection error')
            return None
        except Exception as e:
            logger.error(f'AI Subtitle Translator error: {str(e)}')  # noqa: G004
            return None

    def _mark_partial(self, detail):
        self.partial_error = ' '.join(str(detail).split())[:500] or 'Some translation batches failed.'
        logger.warning("Translation partially completed: %s", self.partial_error)
        show_message('Translation is partial. Some lines may remain in the source language. '
                     f'{self.partial_error}')

    def _poll_job(self, base_url: str, job_id: str, total_lines: int, bazarr_job_id=None) -> Optional[Any]:
        """Poll until a terminal status, subject to reachability and safety limits.

        The sidecar owns request timeouts and retries, so there is no normal total-time cap.
        A slow model with reasoning enabled and a shrunk batch size can take over half an hour.
        The old 30-minute cap discarded a translation that the sidecar finished successfully.
        A 12-hour hard cap remains as a safety net.
        """
        self.partial_error = None
        started_at = time.monotonic()
        last_reachable_at = started_at

        while True:
            now = time.monotonic()
            if now - started_at >= POLL_HARD_CAP_SECONDS:
                reason = "reached the 12-hour polling hard cap"
                user_message = "Translation stopped after 12 hours"
                break

            unreachable_seconds = now - last_reachable_at
            if unreachable_seconds >= POLL_UNREACHABLE_LIMIT_SECONDS:
                unreachable_minutes = int(unreachable_seconds // 60)
                reason = f"status endpoint unreachable for {unreachable_minutes} minutes"
                user_message = f"Translation service unreachable for {unreachable_minutes} minutes"
                break

            try:
                status_response = requests.get(
                    f"{base_url}/api/v1/jobs/{job_id}",
                    headers=get_translator_auth_headers(),
                    timeout=10
                )

                if status_response.status_code != 200:
                    logger.error(f"Error getting job status: {status_response.status_code}")  # noqa: G004
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                last_reachable_at = time.monotonic()
                job_status = status_response.json()
                status = job_status.get("status")
                progress = job_status.get("progress", 0)
                message = job_status.get("message", "")

                # Update progress in Bazarr UI
                show_progress(
                    id=f'translate_progress_{self.dest_srt_file}',
                    header='Translating subtitles with AI...',
                    name=message,
                    value=progress,
                    count=100
                )

                # Sync progress to bazarr jobs queue (for NotificationDrawer)
                if bazarr_job_id:
                    model_used = job_status.get("model_used", settings.translator.openrouter_model or "")
                    jobs_queue.update_job_progress(
                        job_id=bazarr_job_id,
                        progress_value=progress,
                        progress_max=100,
                        progress_message=f'{message} [{model_used}]' if model_used else message
                    )

                if status == "completed":
                    hide_progress(id=f'translate_progress_{self.dest_srt_file}')
                    lines = self._validated_result_lines(job_status.get("result"), total_lines)
                    # An empty list is not a translation: saving it would write every source
                    # line under the target name and record a success in History.
                    if lines:
                        logger.debug(f'Extracted {len(lines)} lines from job result')  # noqa: G004
                        return lines
                    logger.error("Job completed but no result returned")
                    return None

                elif status == "failed":
                    hide_progress(id=f'translate_progress_{self.dest_srt_file}')
                    error = job_status.get("error", "Unknown error")
                    logger.error(f"Translation job failed: {error}")  # noqa: G004
                    show_message(f"Translation failed: {error}")
                    return None

                elif status == "partial":
                    hide_progress(id=f'translate_progress_{self.dest_srt_file}')
                    error = job_status.get("error") or message or "Partial translation"
                    lines = self._validated_result_lines(job_status.get("result"), total_lines)
                    if lines is not None:
                        self._mark_partial(error)
                        return lines
                    logger.error(f"Translation partially failed: {error}")  # noqa: G004
                    show_message(f"Translation failed (partial): {error}")
                    return None

                elif status == "cancelled":
                    hide_progress(id=f'translate_progress_{self.dest_srt_file}')
                    logger.info("Translation job was cancelled")
                    return None

                # Still processing or queued
                time.sleep(POLL_INTERVAL_SECONDS)

            except requests.exceptions.RequestException as e:
                logger.warning(f"Error polling job status: {e}")  # noqa: G004
                time.sleep(POLL_INTERVAL_SECONDS)

        hide_progress(id=f'translate_progress_{self.dest_srt_file}')
        logger.error(f"Translation job {job_id} {reason}")  # noqa: G004
        show_message(user_message)
        return None

    @staticmethod
    def _validated_result_lines(result, total_lines):
        if isinstance(result, dict):
            result = result.get('lines')
        if not isinstance(result, list) or not result:
            return None
        positions = set()
        has_translation = False
        for item in result:
            if not isinstance(item, dict):
                return None
            position = item.get('position')
            line = item.get('line')
            if (type(position) is not int or not 0 <= position < total_lines
                    or position in positions or not isinstance(line, str)):
                return None
            positions.add(position)
            has_translation = has_translation or bool(line.strip())
        return result if has_translation else None

    @retry(exceptions=(TooManyRequests, RequestError, requests.exceptions.RequestException), tries=3, delay=1, backoff=2, jitter=(0, 1))
    def _translate_sync(self, lines_list: List[str], payload: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Fallback synchronous translation (Lingarr-compatible)"""
        base_url = settings.translator.openrouter_url.rstrip('/')

        response = requests.post(
            f"{base_url}/api/v1/translate/content",
            json=payload,
            headers={"Content-Type": "application/json", **get_translator_auth_headers()},
            timeout=1800
        )

        if response.status_code == 200:
            return self._validated_result_lines(response.json(), len(lines_list))
        elif response.status_code == 429:
            raise TooManyRequests("Rate limit exceeded")
        elif response.status_code >= 500:
            raise RequestError(f"Server error: {response.status_code}")
        else:
            logger.error(f'API error: {response.status_code} - {response.text}')  # noqa: G004
            return None

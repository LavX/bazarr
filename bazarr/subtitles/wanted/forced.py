# coding=utf-8
# fmt: off

"""Forced duplicate search for one episode.

A normal wanted search looks only for the languages an episode is missing, and
the library can count an embedded track as present. When that track is broken
the profile looks satisfied and nothing is ever looked up. A forced search
ignores embedded tracks as evidence: it asks every provider for every language
of the episode's profile, adds the best match next to the embedded track and
leaves the embedded track alone.

The search lists with ``list_all_subtitles(on_outcome=...)``, which reports what
each provider said, and downloads per language with the pool's own
``download_best_subtitles``. Nothing in the shared listing or download code is
changed, so an unflagged search behaves exactly as before.

A language ends in one of four states:

* ``downloaded``: a subtitle was saved.
* ``finished``: nothing was downloaded, but at least one provider answered
  (candidates or none) and no provider that applies to the language failed, was
  throttled, was busy or was excluded. The language gets a not-found record,
  like a normal search that came up empty.
* ``cutoff_met``: the external subtitles already satisfy the profile cutoff.
  Not searched, no not-found record.
* ``open``: the search could not give an honest answer. The caller keeps the
  request so the next run tries again.

Credential problems (a rejected login, a provider that needs setting up, or a
backoff recorded for one) count as neither an answer nor a reason to wait: the
user has to fix them, and holding the request forever would not help. They are
logged.

Stop and every exception propagate out of :func:`run_forced_search`, so the
caller never mistakes an interrupted run for a finished one.
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from functools import partial

from arr_instances.resolution import scoped
from app.config import settings
from app.database import TableEpisodes, database, get_audio_profile_languages, get_profiles_list, select, update
from app.event_handler import event_stream
from app.get_providers import (get_providers, is_credential_throttle_reason, provider_is_throttled,
                               snapshot_throttled_providers)
from app.jobs_queue import JobCancelled
from app.notifier import send_notifications
from media_servers.events import publication_callback
from sonarr.history import history_log
import subliminal
from subliminal_patch.core import ListingStopped, _hub_search_languages, save_subtitles
from subliminal_patch.core_persistent import list_all_subtitles
from subliminal_patch.extensions import provider_registry
from subtitles.indexer.series import (desired_subtitles_for_profile, profile_cutoff_met, store_subtitles,
                                      stored_subtitles_list)
from subtitles.tools.subsync_engines import subtitle_write_locks, write_subtitle_file
from utilities.helper import force_unicode, get_target_folder
from utilities.path_mappings import path_mappings

from ..adaptive_searching import updateFailedAttempts
from ..download import _get_language_obj
from ..mismatch import clear_mismatch_for_video
from ..manual import _stop_check
from ..pool import _get_pool, update_pools
from ..processing import process_subtitle
from ..utils import _get_scores, get_video

DOWNLOADED = 'downloaded'
FINISHED = 'finished'
CUTOFF_MET = 'cutoff_met'
OPEN = 'open'

# Its tracks are the ones the forced search exists to get away from, so its
# candidates are never downloaded and its answer counts for nothing.
EXCLUDED_PROVIDER = 'embeddedsubtitles'

_ANSWERED = ('success', 'empty')
_CREDENTIAL_STATUSES = ('authentication_required', 'setup_required')
# A provider that reports one of these does not handle the media or the language
# at all: it neither answered nor failed.
_NOT_APPLICABLE_REASONS = ('unsupported_media', 'excluded_language', 'unsupported_language')


@dataclass
class LanguageResult:
    """How the forced search ended for one profile language.

    ``language`` is written as the missing-subtitles list writes it: ``en``,
    ``en:hi`` or ``en:forced``.
    """
    language: str
    status: str
    reason: str | None = None


@dataclass
class ForcedSearchResult:
    languages: list = field(default_factory=list)

    @property
    def complete(self):
        """True when no language is open, so the caller may drop its request."""
        return all(item.status != OPEN for item in self.languages)

    @property
    def downloaded(self):
        return [item.language for item in self.languages if item.status == DOWNLOADED]


def forced_search_languages(episode):
    """The languages a forced search covers: every profile language after the audio rules.

    Returns ``(code, language_tuple)`` pairs in profile order. ``code`` is the form the missing-subtitles
    list uses and ``language_tuple`` is what ``_get_language_obj`` reads.
    """
    if not episode.profileId:
        return []
    pairs = []
    for item in desired_subtitles_for_profile(episode.profileId, episode.audio_language):
        code = item['language']
        if item['forced'] == 'True':
            code += ':forced'
        elif item['hi'] == 'True':
            code += ':hi'
        pairs.append((code, (item['language'], item['hi'], item['forced'])))
    return pairs


def _external_subtitles_text(episode):
    """The episode's stored subtitles, read again so a download made earlier in the run is in it."""
    arr_instance_id = getattr(episode, 'arr_instance_id', None)
    row = database.execute(
        scoped(select(TableEpisodes.subtitles).where(TableEpisodes.sonarrEpisodeId == episode.sonarrEpisodeId),
               TableEpisodes.arr_instance_id, arr_instance_id)).first()
    return row.subtitles if row else None


def external_cutoff_met(episode):
    """Whether the profile cutoff is satisfied by external subtitles alone.

    A forced run is about the case where an embedded track is not trusted, so an embedded track never
    meets the cutoff here, whatever "Treat embedded subtitles as downloaded" says.
    """
    external = stored_subtitles_list(_external_subtitles_text(episode), include_embedded=False)
    return profile_cutoff_met(episode.profileId, episode.audio_language, external)


def _provider_supports(pool, name, video, language):
    """Whether a provider handles this media type and this language, as the listing decides it."""
    try:
        provider_class = provider_registry[name]
        if not provider_class.check(video):
            return False
        excluded = pool.language_hook(name) if pool.language_hook else None
        if any((el.alpha3, el.country, el.script) == (language.alpha3, language.country, language.script)
               for el in (excluded or ())):
            return False
        search = _hub_search_languages(provider_class, {language})
        return bool(pool.lang_equals.check_set(set(provider_class.languages)) & search)
    except Exception:
        # A provider this cannot read is treated as applicable: holding the request is the safe error.
        logging.debug('BAZARR forced search could not check what %s supports', name, exc_info=True)
        return True


def _throttled_at_start():
    """Enabled providers on a recorded backoff, as ``{name: reason}``. Read only."""
    return {item['provider']: item['reason'] for item in snapshot_throttled_providers()['providers']
            if item['provider'] != EXCLUDED_PROVIDER}


class _Run:
    """What the providers said during one forced search, and how that decides a language."""

    def __init__(self, pool, video, throttled_at_start):
        self.pool = pool
        self.video = video
        self.throttled_at_start = throttled_at_start
        self.outcomes = {}
        self.discarded_before_download = set()

    def collect(self, result):
        if result.provider != EXCLUDED_PROVIDER:
            self.outcomes[result.provider] = result

    def _credential_blocked(self, name):
        reason = self.throttled_at_start.get(name)
        if reason is None:
            reason = {item['provider']: item['reason']
                      for item in snapshot_throttled_providers()['providers']}.get(name)
        return reason is not None and is_credential_throttle_reason(reason)

    def listing_verdict(self, language):
        """``(answered, holds, applicable)`` for one language from the listing and the throttle table.

        ``holds`` lists the reasons the language cannot be called finished: a provider that applies to it
        failed, was throttled, was busy or was excluded. ``applicable`` is whether any enabled provider
        handles the language at all, a provider with a credential problem included.
        """
        answered = False
        applicable = False
        holds = []
        for name, result in self.outcomes.items():
            if result.status == 'skipped' and result.reason in _NOT_APPLICABLE_REASONS:
                continue
            if not _provider_supports(self.pool, name, self.video, language):
                continue
            applicable = True
            if result.status in _ANSWERED:
                answered = True
            elif result.status in _CREDENTIAL_STATUSES:
                logging.info('BAZARR forced search: %s needs its credentials or setup fixed; not counted', name)
            elif result.status == 'skipped' and self._credential_blocked(name):
                logging.info('BAZARR forced search: %s is out for a credential problem; not counted', name)
            else:
                holds.append(f'{name}:{result.status}' + (f'/{result.reason}' if result.reason else ''))
        for name, reason in self.throttled_at_start.items():
            if name in self.outcomes or not _provider_supports(self.pool, name, self.video, language):
                continue
            applicable = True
            if is_credential_throttle_reason(reason):
                logging.info('BAZARR forced search: %s is backed off for a credential problem; not counted', name)
            else:
                holds.append(f'{name}:throttled')
        return answered, holds, applicable

    def download_holds(self, candidates):
        """Reasons the download left a candidate unattempted: a skipped download, or a provider lost since the listing.

        A provider lost to a credential problem is not a reason to wait, as in the listing.
        """
        holds = []
        for subtitle in candidates:
            skip_reason = getattr(subtitle, 'skip_reason', None)
            if skip_reason and not self._credential_blocked(subtitle.provider_name):
                holds.append(f'{subtitle.provider_name}:{skip_reason}')
        lost = self.pool.discarded_providers - self.discarded_before_download
        for name in sorted({subtitle.provider_name for subtitle in candidates}):
            if name not in lost and not provider_is_throttled(name):
                continue
            if self._credential_blocked(name):
                logging.info('BAZARR forced search: %s was lost to a credential problem during the download; '
                             'not counted', name)
            else:
                holds.append(f'{name}:lost_during_download')
        return holds


def _save_and_process(video, subtitles, episode, video_path, language, audio_language, max_score, job_id,
                      subz_mods):
    """Write downloaded subtitles next to the video and process each one. Returns the processed results."""
    arr_instance_id = getattr(episode, 'arr_instance_id', None)
    path = video.original_path
    subtitle_formats = set()
    for subtitle in subtitles:
        subtitle.mods = subz_mods
        subtitle_formats.add(subtitle.format)

    try:
        fld = get_target_folder(video_path)
        chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
            'win') and settings.general.chmod_enabled else None
        with subtitle_write_locks(video_path, os.path.join(fld or os.path.dirname(path), '.destination'),
                                  video_path):
            written_paths = []
            saved = save_subtitles(path, subtitles,
                                   single=settings.general.single_language,
                                   tags=None,
                                   directory=fld,
                                   chmod=chmod,
                                   formats=subtitle_formats,
                                   path_decoder=force_unicode,
                                   write_subtitle=partial(
                                       write_subtitle_file, video_path, written_paths=written_paths,
                                       on_publish=publication_callback(
                                           'series', video_path, 'download', arr_instance_id)))
            saved = [s for s in saved if s.storage_path in written_paths]
    except Exception:
        logging.exception('BAZARR forced search: error saving subtitles for %s', video_path)
        return []

    # Only for a subtitle that really reached disk: a recorded release-type mismatch is cleared once the file
    # is there, never before.
    on_disk = [subtitle for subtitle in saved
               if getattr(subtitle, 'storage_path', None) and os.path.isfile(subtitle.storage_path)]
    if any(str(getattr(subtitle, 'language', None)) == str(language) for subtitle in on_disk):
        clear_mismatch_for_video(video, 'series', language, arr_instance_id=arr_instance_id)

    processed = []
    for subtitle in on_disk:
        if "hash" in subtitle.matches:
            # make matches set cleaner for history purpose when hash matches
            subtitle.matches = {match for match in subtitle.matches if match in ("hash", "hearing_impaired")}
        result = process_subtitle(subtitle=subtitle, media_type='series', audio_language=audio_language,
                                  is_upgrade=False, is_manual=False, path=video_path,
                                  max_score=max_score, job_id=job_id, arr_instance_id=arr_instance_id)
        if not result:
            logging.debug('BAZARR forced search: unable to process this subtitle: %s', subtitle)
            continue
        if isinstance(result, tuple) and len(result):
            result = result[0]
        processed.append(result)
    return processed


def _record_download(episode, result):
    arr_instance_id = getattr(episode, 'arr_instance_id', None)
    store_subtitles(episode.path, path_mappings.path_replace_instance(episode.path, arr_instance_id, 'episode'),
                    arr_instance_id=arr_instance_id)
    history_log(1, episode.sonarrSeriesId, episode.sonarrEpisodeId, result, arr_instance_id=arr_instance_id)
    event_stream(type='series', action='update', payload=episode.sonarrSeriesId)
    event_stream(type='episode-wanted', action='delete', payload=episode.sonarrEpisodeId)
    send_notifications(episode.sonarrSeriesId, episode.sonarrEpisodeId, result.message,
                       arr_instance_id=arr_instance_id)


def _write_not_found(episode, attempts):
    arr_instance_id = getattr(episode, 'arr_instance_id', None)
    database.execute(
        scoped(update(TableEpisodes)
               .values(failedAttempts=attempts)
               .where(TableEpisodes.sonarrEpisodeId == episode.sonarrEpisodeId),
               TableEpisodes.arr_instance_id, arr_instance_id))


def _download_language(pool, candidates, video, language, **options):
    """Download the best candidate for one language, ordered the way a normal search orders it.

    With provider priority on, providers are tried in the pool's order and the first one that delivers wins,
    which is what the priority waterfall does. Otherwise the best score wins whatever the provider.
    """
    if not settings.general.use_provider_priority:
        return pool.download_best_subtitles(candidates, video, {language}, fallback_allowed=False, **options)
    for name in pool.providers:
        own = [subtitle for subtitle in candidates if subtitle.provider_name == name]
        if not own:
            continue
        downloaded = pool.download_best_subtitles(own, video, {language}, fallback_allowed=False, **options)
        if downloaded:
            return downloaded
    return []


def _sync_cache():
    try:
        subliminal.region.backend.sync()
    except Exception:
        logging.debug('BAZARR forced search: the subtitle cache could not be synced', exc_info=True)


def _check_stop(stop):
    """Raise what the caller's stop check raises, in the caller's terms."""
    if stop is None:
        return
    try:
        stop()
    except ListingStopped as stopped:
        raise stopped.original from None


def run_forced_search(episode, job_id=None, cancel=None):
    """Search every provider for every profile language of ``episode`` and report how each language ended.

    ``episode`` carries the columns ``wanted_download_subtitles`` selects: ``path``, ``sonarrEpisodeId``,
    ``sonarrSeriesId``, ``arr_instance_id``, ``audio_language``, ``sceneName``, ``failedAttempts``, ``title``
    (the show), ``profileId`` and ``subtitles``.

    ``cancel`` is the job's stop signal (``is_set()`` returns True to stop or raises ``JobCancelled``). It does
    not touch the request flag: the caller clears it only when :attr:`ForcedSearchResult.complete`.
    """
    return _run(episode, profile_id=episode.profileId, media_type='series', job_id=job_id, cancel=cancel)


@update_pools
def _run(episode, *, profile_id, media_type, job_id, cancel):
    languages = forced_search_languages(episode)
    results = ForcedSearchResult()
    if not languages:
        return results

    stop = _stop_check(cancel)
    _check_stop(stop)

    def all_languages(status, reason=None):
        results.languages = [LanguageResult(code, status, reason) for code, _ in languages]
        return results

    if external_cutoff_met(episode):
        logging.debug('BAZARR forced search: the cutoff is already met by external subtitles for %s', episode.path)
        return all_languages(CUTOFF_MET)

    if not get_providers():
        logging.info('BAZARR All providers are throttled')
        return all_languages(OPEN, 'no_providers')

    arr_instance_id = getattr(episode, 'arr_instance_id', None)
    video_path = path_mappings.path_replace(episode.path)
    pool = _get_pool(media_type, profile_id)
    providers = pool.providers
    if not providers:
        logging.info('BAZARR All providers are throttled')
        return all_languages(OPEN, 'no_providers')

    if settings.general.utf8_encode:
        os.environ["SZ_KEEP_ENCODING"] = ""
    else:
        os.environ["SZ_KEEP_ENCODING"] = "True"

    try:
        video = get_video(force_unicode(video_path), episode.title, str(episode.sceneName),
                          providers=providers, media_type=media_type)
    except ValueError:
        logging.exception('BAZARR forced search: unable to get the video object for %s', video_path)
        video = None
    if not video:
        return all_languages(OPEN, 'no_video')
    if arr_instance_id is not None:
        video.arr_instance_id = arr_instance_id

    language_objects = []
    for code, language_tuple in languages:
        language_objects.append((code, next(iter(_get_language_obj(languages=[language_tuple])))))
    language_set = {language for _, language in language_objects}

    profile = get_profiles_list(profile_id=profile_id)
    original_format = profile['originalFormat']
    hi_required = "force HI" if all(language.hi for language in language_set) else "don't prefer"
    min_score, max_score, _ = _get_scores(media_type, settings.general.minimum_score_movie,
                                          settings.general.minimum_score)
    from subtitles.tools.mods import get_subzero_mods
    subz_mods = get_subzero_mods(arr_instance_id)
    audio_language_list = get_audio_profile_languages(episode.audio_language)
    audio_language = audio_language_list[0]['name'] if len(audio_language_list) > 0 else 'None'

    run = _Run(pool, video, _throttled_at_start())
    try:
        listed = list_all_subtitles([video], language_set, pool, min_score=int(min_score), cancel=stop,
                                    on_outcome=run.collect)
    except ListingStopped as stopped:
        raise stopped.original from None
    candidates = [subtitle for subtitle in listed[video] if subtitle.provider_name != EXCLUDED_PROVIDER]

    attempts = episode.failedAttempts
    for code, language in language_objects:
        _check_stop(stop)

        if external_cutoff_met(episode):
            logging.debug('BAZARR forced search: the cutoff is met, %s is not searched for %s', code, episode.path)
            results.languages.append(LanguageResult(code, CUTOFF_MET))
            continue

        answered, holds, applicable = run.listing_verdict(language)
        language_candidates = [subtitle for subtitle in candidates
                               if subtitle.language.basename == language.basename]
        run.discarded_before_download = set(pool.discarded_providers)
        download_options = dict(min_score=int(min_score),
                                hearing_impaired=hi_required,
                                use_original_format=original_format in (1, "1", "True", True))
        try:
            downloaded = _download_language(pool, language_candidates, video, language, **download_options)
            if not downloaded:
                holds = holds + run.download_holds(language_candidates)
                # Whisper is the costliest source and the one that is not a provider's answer: it runs only for
                # a language every applicable provider answered cleanly, never in place of a candidate that was
                # skipped and could still be fetched on the next run.
                if settings.general.use_whisper_fallback and answered and not holds:
                    downloaded = pool.download_fallback_subtitles(language_candidates, video, {language},
                                                                  hearing_impaired=hi_required,
                                                                  use_original_format=download_options[
                                                                      'use_original_format'])
        except JobCancelled as error:
            if error.job_id == job_id:
                raise
            logging.exception('BAZARR forced search: another job was stopped during the download for %s', episode.path)
            results.languages.append(LanguageResult(code, OPEN, 'download_error'))
            continue

        if downloaded:
            processed = _save_and_process(video, downloaded, episode, video_path, language, audio_language,
                                          max_score, job_id, subz_mods)
            for result in processed:
                _record_download(episode, result)
            if processed:
                results.languages.append(LanguageResult(code, DOWNLOADED))
            else:
                results.languages.append(LanguageResult(code, OPEN, 'save_failed'))
            continue

        if holds:
            logging.info('BAZARR forced search: %s stays open for %s (%s)', code, episode.path, ', '.join(holds))
            results.languages.append(LanguageResult(code, OPEN, ', '.join(holds)))
        elif not answered:
            reason = 'no_provider_answered' if applicable else 'no_applicable_provider'
            logging.info('BAZARR forced search: %s stays open for %s (%s)', code, episode.path, reason)
            results.languages.append(LanguageResult(code, OPEN, reason))
        else:
            attempts = updateFailedAttempts(desired_language=code, attempt_string=attempts)
            _write_not_found(episode, attempts)
            results.languages.append(LanguageResult(code, FINISHED))

    _sync_cache()
    return results

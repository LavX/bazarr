# coding=utf-8
"""The translation entry points of the v2 runtime: a library translation and an editor translation.

``RuntimeTranslatorService`` is built with the arguments the old services take and answers the same call,
``translate(job_id)``, so the job function in ``translate/main.py`` changes by one branch. Where the old
services each talk to their engine and write the file themselves, this one hands the lines to the dispatcher
and writes the file from what comes back: Bazarr+ stays the publisher (staged write, credit line, post
processing, history), whichever engine translated. An engine served by code in Bazarr+ (Lingarr) still
publishes through its own service, which the dispatcher reaches through ``_legacy``.

``translate_lines`` is the editor's entry point: lines in, lines out, no file.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import pysubs2

from app.jobs_queue import JobFailed, jobs_queue
from languages.get_languages import language_from_alpha2, language_from_alpha3
from media_servers.events import publication_callback
from radarr.history import history_log_movie
from sonarr.history import history_log
from sportarr.connection import check_cancelled
from sportarr.profile_hooks import finish_translation
from subtitles.tools.subsync_engines import (SubtitleDestinationChanged, SubtitleSourceChanged,
                                             capture_subtitle_versions, staged_subtitle_write,
                                             validate_subtitle_versions)

from ..core.translator_utils import add_translator_info, create_process_result, get_title, translation_write_kwargs
from .failure import MESSAGES
from .dispatch import DispatchFailed, Dispatcher, JobSpec, LegacyDeclined, LegacyResult
from .jobs import line_digest, target_key

logger = logging.getLogger(__name__)


class TranslationRuntimeError(JobFailed):
    """A translation job the runtime could not finish. ``reason`` is its error class; ``retry_later`` says
    whether it will be tried again, so the item is not held back from the next scan."""

    def __init__(self, message: str, reason: str, retry_later: bool = False):
        super().__init__(message, reason=reason, retryable=retry_later)
        self.retry_later = retry_later


def _unreachable(error: BaseException) -> bool:
    """Whether an in-core service failed because its server could not be reached."""
    import requests
    from deep_translator.exceptions import RequestError

    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    return isinstance(error, RequestError) and str(error).startswith(("Connection error", "Request timed out"))


def _media_context(media_type, radarr_id, sonarr_series_id, sonarr_episode_id, arr_instance_id, sports_operation):
    """The title, year, IMDB id and overview of what is being translated, for the engine's prompt."""
    from app.database import TableMovies, TableShows, database, select

    from ..core.translator_utils import _scope, is_movie_media_type

    title = get_title(media_type, radarr_id, sonarr_series_id, sonarr_episode_id, arr_instance_id,
                      **({"sports_context": sports_operation.context} if sports_operation else {}))
    context: dict[str, Any] = {"type": "movie" if is_movie_media_type(media_type) else str(media_type or ""),
                               "title": title or None}
    row = None
    try:
        if sports_operation is not None:
            from sportarr.profile_hooks import metadata

            context["overview"] = metadata(sports_operation.context)[1] or None
        elif is_movie_media_type(media_type):
            row = database.execute(_scope(select(TableMovies.imdbId, TableMovies.year, TableMovies.overview)
                                          .where(TableMovies.radarrId == radarr_id), TableMovies, arr_instance_id)
                                   ).first()
        else:
            row = database.execute(_scope(select(TableShows.imdbId, TableShows.year, TableShows.overview)
                                          .where(TableShows.sonarrSeriesId == sonarr_series_id), TableShows,
                                          arr_instance_id)).first()
        if sports_operation is None and row is not None:
            year = str(row.year or "").strip()
            context.update({"year": int(year) if year.isdigit() else None, "imdb_id": row.imdbId or None,
                            "overview": row.overview or None})
    except Exception:
        logger.exception("Problem with getting the media information for a translation")
    return {key: value for key, value in context.items() if value is not None}


class RuntimeTranslatorService:
    """A library translation through the runtime. Takes what the old services take, plus the job's key."""

    def __init__(self, source_srt_file, dest_srt_file, lang_obj, to_lang, from_lang, media_type, video_path,
                 orig_to_lang, forced, hi, sonarr_series_id, sonarr_episode_id, radarr_id, arr_instance_id=None,
                 sports_operation=None, cancel=None, expected_destination_versions=(), source_key=None,
                 dispatcher=None):
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
        self.arr_instance_id = arr_instance_id
        self.sports_operation = sports_operation
        self.cancel = cancel
        self.expected_destination_versions = tuple(expected_destination_versions or ())
        self.source_key = source_key or str(source_srt_file)
        self.partial_error = None
        # True when the job found itself replaced or already running elsewhere: nothing was translated.
        self.skipped = False
        self._dispatcher = dispatcher
        self._job_id = None
        self._versions = ()
        self._source_real = None

    def _check(self):
        if self._job_id:
            jobs_queue.raise_if_cancelled(self._job_id)
        check_cancelled(self.cancel)

    def _progress(self, done, total, message):
        if self._job_id:
            jobs_queue.update_job_progress(job_id=self._job_id, progress_value=done, progress_max=total,
                                           progress_message=message)

    def _legacy(self, engine, instance):
        from ..services.translator_factory import TranslatorFactory

        service = TranslatorFactory.create_translator(
            engine.translator_type, source_srt_file=self.source_srt_file, dest_srt_file=self.dest_srt_file,
            lang_obj=self.lang_obj, from_lang=self.from_lang, to_lang=self.to_lang, media_type=self.media_type,
            video_path=self.video_path, orig_to_lang=self.orig_to_lang, forced=self.forced, hi=self.hi,
            sonarr_series_id=self.sonarr_series_id, sonarr_episode_id=self.sonarr_episode_id,
            radarr_id=self.radarr_id, arr_instance_id=self.arr_instance_id,
            expected_destination_versions=self.expected_destination_versions,
            **({"sports_operation": self.sports_operation, "cancel": self.cancel} if self.sports_operation else {}))
        try:
            published = service.translate(job_id=self._job_id)
        except Exception as error:
            if _unreachable(error):
                raise LegacyDeclined(f"The {engine.name} server could not be reached.") from error
            raise
        return LegacyResult(published=published or self.dest_srt_file,
                            partial_error=getattr(service, "partial_error", None))

    def translate(self, job_id=None):
        self._job_id = job_id
        if os.path.islink(self.dest_srt_file):
            # Refused before anything is paid for: a translation replaces the file at the destination, and a
            # link there is not something Bazarr+ replaces. It would never publish, so it is not retried.
            raise TranslationRuntimeError(MESSAGES["destination_is_link"], "destination_is_link")
        # The versions of the source and of the destination as they are when the job starts, before the source
        # is read and before the engine is asked: whatever the remote call took, an edit made meanwhile must not
        # be published over. A source that is a link is followed, as the old services followed it.
        self._source_real = os.path.realpath(self.source_srt_file)
        self._versions = capture_subtitle_versions(self.video_path, (self._source_real, self.dest_srt_file),
                                                   self.cancel)
        subs = pysubs2.load(self.source_srt_file, encoding="utf-8")
        lines = [x.plaintext for x in subs]
        if not lines:
            if self.sports_operation:
                raise RuntimeError("The source has no subtitle cues")
            raise TranslationRuntimeError("The source subtitle has no lines to translate.", "empty_source")
        title = get_title(self.media_type, self.radarr_id, self.sonarr_series_id, self.sonarr_episode_id,
                          self.arr_instance_id,
                          **({"sports_context": self.sports_operation.context} if self.sports_operation else {}))
        media_id = (self.sonarr_episode_id if self.media_type == "episode"
                    else (None if self.media_type == "sports" else self.radarr_id))
        spec = JobSpec(
            source_key=self.source_key, target_key=target_key(self.orig_to_lang, self.forced, self.hi), lines=lines,
            source_language=self.from_lang, target_language=self.orig_to_lang,
            source_name=language_from_alpha2(self.from_lang) or None,
            target_name=language_from_alpha2(self.orig_to_lang) or None,
            media=_media_context(self.media_type, self.radarr_id, self.sonarr_series_id, self.sonarr_episode_id,
                                 self.arr_instance_id, self.sports_operation),
            history_media={"media_type": self.media_type, "media_id": media_id, "media_title": title or None,
                           "arr_instance_id": self.arr_instance_id},
            check_cancel=self._check, progress=self._progress, legacy=self._legacy)
        try:
            outcome = (self._dispatcher or Dispatcher()).run(spec)
        except DispatchFailed as failed:
            raise TranslationRuntimeError(failed.message, failed.error_class, failed.retry_later) from failed
        if outcome.kind == "skipped":
            self.skipped = True
            return None
        self.partial_error = outcome.partial_error
        if outcome.kind == "published":
            return outcome.published
        try:
            translated = self._write_file(subs, lines, outcome, job_id)
        except BaseException as error:
            # The job is not over until the subtitle is written: its batches stay for a retry and the failure is
            # on record. The error raised matches what was recorded, so the queue and the history agree.
            error = self._precise(error)
            recorded = outcome.publication_failed(error) if outcome.publication_failed is not None else None
            if isinstance(error, Exception) and recorded and recorded not in ("cancelled", "failed"):
                raise TranslationRuntimeError(MESSAGES[recorded], recorded, retry_later=True) from error
            raise error
        try:
            return self._record(outcome, translated)
        finally:
            # The file is on disk: whatever the bookkeeping after it does, the translation is done.
            if outcome.complete is not None:
                outcome.complete()

    def _write_file(self, subs, source_lines, outcome, job_id):
        """Write the translated subtitle. Returns the word the credit line and the history message use.

        The versions taken at the start are checked again here, before anything is staged, and handed to the
        staged write, which checks them once more under its locks at the moment of publication.
        """
        self._check_inputs()
        write_kwargs = dict(translation_write_kwargs(self, job_id))
        write_kwargs["expected_versions"] = tuple(write_kwargs.get("expected_versions") or ()) + self._versions
        with staged_subtitle_write(self.video_path, self.dest_srt_file,
                                   on_publish=publication_callback(self.media_type, self.video_path, "translate",
                                                                   self.arr_instance_id),
                                   source_paths=(self.source_srt_file,),
                                   before_publish=lambda: jobs_queue.update_job_progress(job_id=job_id),
                                   allow_empty=not bool(self.sports_operation),
                                   **write_kwargs) as temporary:
            for line, source, text in zip(subs, source_lines, outcome.lines):
                if text.strip() and text != source:
                    line.text = text
            subs.save(temporary)
            translated = "partially translated" if self.partial_error else "translated"
            add_translator_info(temporary, f"# Subtitles {translated} with {outcome.engine_name} #")
        return translated

    def _precise(self, error):
        """The staged write reports any change of its checked versions as a changed source. Say which one it was."""
        if isinstance(error, SubtitleSourceChanged):
            try:
                self._check_inputs()
            except (SubtitleSourceChanged, SubtitleDestinationChanged) as precise:
                return precise
        return error

    def _check_inputs(self):
        """Raise the same error the staged write would when the source or the destination is no longer as it
        was when the job started."""
        source, destination = self._versions
        if os.path.realpath(self.source_srt_file) != self._source_real:
            # A link that was pointed at another file while the engine worked: that is a different source.
            raise SubtitleSourceChanged("The source subtitle is a link that now points elsewhere")
        try:
            validate_subtitle_versions((source,))
        except SubtitleSourceChanged:
            raise SubtitleSourceChanged("The source subtitle changed while the translation ran") from None
        try:
            validate_subtitle_versions((destination,))
        except SubtitleSourceChanged:
            raise SubtitleDestinationChanged("The destination subtitle changed while the translation ran") from None

    def _record(self, outcome, translated):
        """The history, post-processing hooks and result of a subtitle that is on disk."""
        message = (f"{language_from_alpha2(self.from_lang)} subtitles {translated} to "
                   f"{language_from_alpha3(self.to_lang)} using {outcome.engine_name}.")
        if self.partial_error:
            message += f" Some lines may remain in the source language. {self.partial_error}"
        result = create_process_result(
            message, self.video_path, self.orig_to_lang, self.forced, self.hi, self.dest_srt_file, self.media_type,
            **({"sports_context": self.sports_operation.context} if self.sports_operation else {}))
        if finish_translation(self, result):
            return self.dest_srt_file
        if self.media_type == "episode":
            history_log(action=6, sonarr_series_id=self.sonarr_series_id, sonarr_episode_id=self.sonarr_episode_id,
                        result=result, arr_instance_id=self.arr_instance_id)
        else:
            history_log_movie(action=6, radarr_id=self.radarr_id, result=result, arr_instance_id=self.arr_instance_id)
        return self.dest_srt_file


@dataclass
class EditorTranslation:
    lines: list[str]
    partial_error: str | None = None


def translate_lines(lines, source_language, target_language, title="", media_type="", job_id=None,
                    dispatcher=None) -> EditorTranslation | None:
    """Translate lines the subtitle editor sent, through the selected engine with its failover and fallback.

    Returns the lines in the order given, or None when the job found itself replaced or already running. Raises
    ``dispatch.GateClosed`` (the selected engine cannot translate), ``dispatch.Declined`` or
    ``TranslationRuntimeError``.
    """
    def check():
        if job_id:
            jobs_queue.raise_if_cancelled(job_id)

    def progress(done, total, message):
        if job_id:
            jobs_queue.update_job_progress(job_id=job_id, progress_value=done, progress_max=total,
                                           progress_message=message)

    def legacy(engine, instance):
        from ..services.translator_factory import TranslatorFactory

        # Lingarr reads the two languages from the service it is built with.
        service = TranslatorFactory.create_translator(
            engine.translator_type, source_srt_file=None, dest_srt_file=None, lang_obj=None, to_lang=None,
            from_lang=source_language, media_type=media_type, video_path=None, orig_to_lang=target_language,
            forced=False, hi=False, sonarr_series_id=None, sonarr_episode_id=None, radarr_id=None)
        try:
            answered = service._translate_content(list(lines), job_id=job_id)
        except Exception as error:
            if _unreachable(error):
                raise LegacyDeclined(f"The {engine.name} server could not be reached.") from error
            raise
        if not isinstance(answered, list):
            # The service answers None when the reply was not what it expected. That is a failure, not a
            # translation that left every line as it was.
            raise TranslationRuntimeError(f"{engine.name} did not return a usable translation.", "invalid_result")
        by_position = {item["position"]: item["line"] for item in answered
                       if isinstance(item, dict) and isinstance(item.get("line"), str)}
        missing = sum(1 for index, text in enumerate(lines) if text.strip() and index not in by_position)
        return LegacyResult(
            lines=[by_position.get(index, text) for index, text in enumerate(lines)],
            partial_error=f"No translated text was returned for {missing} of {len(lines)} cues." if missing else None)

    spec = JobSpec(
        source_key="editor:" + line_digest(lines), target_key=target_key(target_language), lines=list(lines),
        source_language=source_language or None, target_language=target_language,
        media={"type": media_type or "", "title": title or ""} if (media_type or title) else {},
        history_media={"media_type": media_type or "editor", "media_title": title or None},
        check_cancel=check, progress=progress, legacy=legacy)
    try:
        outcome = (dispatcher or Dispatcher()).run(spec)
    except DispatchFailed as failed:
        raise TranslationRuntimeError(failed.message, failed.error_class, failed.retry_later) from failed
    if outcome.kind == "skipped":
        return None
    if outcome.complete is not None:
        outcome.complete()  # the editor takes the lines: there is no file to write
    return EditorTranslation(list(outcome.lines), outcome.partial_error)

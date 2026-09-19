"""Resolve queued sports uploads at execution and publish under owned file guards."""

import logging
import os
import sys
from contextlib import contextmanager
from functools import partial
from types import SimpleNamespace

from app.config import settings
from app.database import TableSportsEvents, select
from app.jobs_queue import JobCancelled
from app.notifier import send_notifications_sports
from sportarr.output import SportsOutputNamespace, validate_output_path
from sportarr.subtitles import sports_manual_operation, sports_history
from sportarr.workflows import SportsJobSignal
from subtitles.processing import ProcessSubtitlesResult, _postprocessing_config
from subtitles.tools.subsync_engines import (
    SubtitlePublication,
    subtitle_source_version,
    subtitle_write_locks,
    release_subtitle_publication,
    _report_subtitle_publication,
)
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings


def upload_sports_subtitle(event_id, owner, language, forced, hi, subtitle, filename, job_id):
    from subtitles import upload as shared
    from subtitles.sync import _index_sports_outputs
    from subtitles.tools.mods import get_subzero_mods

    signal = SportsJobSignal(owner, job_id)
    with sports_manual_operation(event_id, owner, signal) as (context, validate, guard, path):
        namespace = SportsOutputNamespace(context, shared.database)
        language3 = shared.alpha3_from_alpha2(language)
        custom = shared.CustomLanguage.from_value(language3, 'alpha3')
        lang = custom.subzero_language() if custom is not None else shared.Language(language3)
        lang = shared.Language.rebuild(lang, hi=hi, forced=forced)
        original_format = shared._profile_original_format(context.profile_id)
        sub = shared.Subtitle(lang, mods=get_subzero_mods(owner), original_format=original_format)
        sub.content = subtitle.getvalue()
        if not sub.is_valid():
            sub.mods = None
        if settings.general.utf8_encode:
            sub.set_encoding('utf-8')
        try:
            sub.format = (shared.get_format_identifier(os.path.splitext(filename)[1]),)
        except Exception:
            pass
        formats = (sub.format,) if isinstance(sub.format, str) else sub.format
        chmod = (int(settings.general.chmod, 8)
                 if not sys.platform.startswith('win') and settings.general.chmod_enabled else None)

        @contextmanager
        def publication_guard():
            with guard() as (session, check):
                namespace.validate(session)
                yield session, check

        publications = {}
        retained = []
        transferred = None

        def capture(destination):
            # Called after mutation revisions settle, while the writer still owns
            # the coordinator. No later writer can be adopted as this upload.
            if chmod:
                os.chmod(destination, chmod)
            publication = SubtitlePublication(path, destination, subtitle_source_version(destination))
            retained.append(publication)
            publications[destination] = publication

        def write(destination, content):
            validate_output_path(context, destination)
            shared.write_subtitle_file(
                path, destination, content, written_paths=written,
                publication_guard=publication_guard, after_write=partial(capture, destination))
            _report_subtitle_publication(
                shared.publication_callback('sports', path, 'upload', owner), destination)

        warnings = []
        try:
            validate()
            directory = shared.get_target_folder(path)
            written = []
            saved = shared.save_subtitles(
                path, [sub], single=settings.general.single_language, tags=None,
                directory=directory, chmod=None,
                formats=formats if original_format else ('srt',),
                path_decoder=shared.force_unicode, write_subtitle=write)
            saved = [item for item in saved if item.storage_path in written]
            if not saved:
                raise OSError('Could not save sports subtitle')
            output = saved[0].storage_path
            publication = publications[output]
            modifier = ':hi' if hi else ':forced' if forced else ''
            language_name = shared.language_from_alpha3(language3) + (' HI' if hi else ' forced' if forced else '')
            use_postprocessing, command, _, _ = _postprocessing_config('sports', owner)
            if use_postprocessing:
                audio = shared.database.execute(select(TableSportsEvents.audio_language).where(
                    TableSportsEvents.id == context.event_id,
                    TableSportsEvents.arr_instance_id == owner)).scalar_one()
                audio = shared.get_audio_profile_languages(audio)
                audio = audio[0] if audio and isinstance(audio[0], dict) else dict(name='', code2='', code3='')

                def command_for(destination):
                    return shared.pp_replace(command, path, destination, language_name, language + modifier,
                                             language3 + modifier, audio['name'], audio['code2'], audio['code3'],
                                             100, '1', 'manual', 'user', 'unknown', None, event_id)
                try:
                    validate()
                    shared.postprocessing(
                        command_for(output), path, subtitle_path=output,
                        publication_guard=publication_guard, command_builder=command_for,
                        source_version=publication, after_write=partial(capture, output),
                        on_publish=shared.publication_callback('sports', path, 'upload', owner))
                    publication = publications[output]
                except JobCancelled:
                    raise
                except Exception:
                    logging.exception('BAZARR sports upload post-processing failed')
                    warnings.append('post-processing')

            with subtitle_write_locks(path, output):
                with publication_guard() as (session, check):
                    instance = check()
                    if not publication.source_unchanged():
                        raise ValueError('Uploaded sports subtitle changed before history was recorded')
                    mappings = read_sports_mappings(instance.path_mappings)
                    result = ProcessSubtitlesResult(
                        message=f'{language_name} Subtitles manually uploaded.',
                        reversed_path=context.original_path,
                        downloaded_language_code2=language, downloaded_provider=None,
                        score=None, forced=forced, subtitle_id=None,
                        reversed_subtitles_path=apply_sports_mapping(output, mappings, reverse=True),
                        hearing_impaired=hi)
                    sports_history(session, context, result, action=4)

            def refresh():
                try:
                    _index_sports_outputs(context)
                except Exception:
                    logging.exception('BAZARR sports upload index refresh failed')
                    warnings.append('index')

            try:
                from subtitles.manual import clear_mismatch_after_manual_save
                clear_mismatch_after_manual_save(
                    SimpleNamespace(sportsEventId=event_id, original_path=path, arr_instance_id=owner),
                    'sports', saved, owner)
            except Exception:
                logging.exception('BAZARR sports upload mismatch refresh failed')
                warnings.append('mismatch')
            refresh()
            if not settings.general.dont_notify_manual_actions:
                shared._notify_upload('user', send_notifications_sports, event_id, result.message,
                                      arr_instance_id=owner)
            consumers = partial(shared._refresh_upload_consumers, 'sports', None, owner)
            shared._notify_upload('consumers', consumers)
            try:
                shared.sync_subtitles(
                    video_path=path, srt_path=output, srt_lang=language, percent_score=100,
                    forced=forced, hi=hi, arr_instance_id=owner, callback=refresh,
                    source_version=publication, on_success=consumers,
                    context=context, validate=validate, publication_guard=guard)
                transferred = publication
            except Exception:
                logging.exception('BAZARR sports upload sync could not be queued')
                warnings.append('sync')
            if warnings:
                return {'message': 'Sports subtitle uploaded; follow-up failed: ' + ', '.join(warnings),
                        'published': True}
            return '', 204
        finally:
            for publication in retained:
                if publication is not transferred:
                    release_subtitle_publication(publication)

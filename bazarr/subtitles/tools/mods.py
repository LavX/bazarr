# coding=utf-8

import os
import sys
import logging

from subliminal_patch.subtitle import Subtitle
from subliminal_patch.core import get_subtitle_path
from subzero.language import Language

from app.config import settings, get_array_from
from app.jobs_queue import jobs_queue
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha3_from_alpha2
from subtitles.indexer.utils import get_external_subtitles_path


def has_remove_hi(mods):
    """Whether the Remove HI mod is present, tolerating its parameterized form
    (e.g. remove_HI(keep_lyrics=1)). See https://github.com/LavX/bazarr/issues/225
    """
    return any(m == 'remove_HI' or m.startswith('remove_HI(') for m in (mods or []))


def with_keep_lyrics(mods, arr_instance_id=None):
    """Rewrite the Remove HI mod to preserve song lyrics when the setting is on.

    The preference is encoded as a subzero mod parameter so it travels through
    the (settings-agnostic) subtitle modification pipeline.
    See https://github.com/LavX/bazarr/issues/225

    When ``arr_instance_id`` is given the preserve-lyrics preference resolves
    against that instance's per-instance override, falling back to the global
    setting (#227). A None instance keeps the legacy global-only behaviour.
    """
    from arr_instances.resolution import resolve_subtitle_setting
    keep_lyrics = resolve_subtitle_setting(
        arr_instance_id, "general.subzero_mods_keep_lyrics",
        settings.general.subzero_mods_keep_lyrics)
    if not mods or not keep_lyrics:
        return mods
    return ['remove_HI(keep_lyrics=1)' if m == 'remove_HI' else m for m in mods]


def get_subzero_mods(arr_instance_id=None):
    """Configured subzero mods with the preserve-song-lyrics preference applied.

    With ``arr_instance_id`` the configured mods + keep-lyrics preference resolve
    against that instance's overrides (#227). The per-instance value is stored as
    a list; the global value is a comma-separated string, so both shapes are
    normalised here. A None instance preserves the legacy global behaviour.
    """
    from arr_instances.resolution import resolve_subtitle_setting
    configured = resolve_subtitle_setting(
        arr_instance_id, "general.subzero_mods", settings.general.subzero_mods)
    mods = configured if isinstance(configured, list) else get_array_from(configured)
    return with_keep_lyrics(mods, arr_instance_id)


MOD_LABELS = {
    'remove_HI': 'Remove HI Tags',
    'remove_tags': 'Remove Style Tags',
    'OCR_fixes': 'OCR Fixes',
    'common': 'Common Fixes',
    'fix_uppercase': 'Fix Uppercase',
    'reverse_rtl': 'Reverse RTL',
    'add_color': 'Add Color',
    'change_frame_rate': 'Change Frame Rate',
    'adjust_time': 'Adjust Times',
    'emoji': 'Remove Emoji',
}


def apply_subtitle_mods(language, subtitle_path, mods, video_path,
                         media_type=None, media_id=None, job_id=None, arr_instance_id=None):
    """Job-aware wrapper for subtitles_apply_mods.

    When called without a job_id, queues the work as a backend job and returns
    immediately. When called with a job_id (by the job queue consumer), does the
    actual work and handles post-processing (store_subtitles, event_stream, chmod).
    """
    if not job_id:
        # No local variables can be assigned before add_job_from_function because
        # it introspects the frame and re-passes all locals as kwargs on re-invocation.
        jobs_queue.add_job_from_function(
            (lambda m, p: f'{MOD_LABELS.get(m, m)}: {os.path.basename(p)}')(
                mods[0] if mods else 'mods', subtitle_path),
            is_progress=False,
        )
        return

    mod_label = MOD_LABELS.get(mods[0], mods[0]) if mods else 'Apply Mods'
    filename = os.path.basename(subtitle_path)

    try:
        subtitles_apply_mods(language=language, subtitle_path=subtitle_path,
                             mods=mods, video_path=video_path)
    except Exception:
        jobs_queue.update_job_name(
            job_id=job_id,
            new_job_name=f'Failed {mod_label}: {filename}',
        )
        raise

    # apply chmod if required
    chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
        'win') and settings.general.chmod_enabled else None
    if chmod and os.path.exists(subtitle_path):
        os.chmod(subtitle_path, chmod)

    # re-index subtitles so Bazarr's DB picks up the changes
    from subtitles.indexer.series import store_subtitles
    from subtitles.indexer.movies import store_subtitles_movie
    from app.event_handler import event_stream
    from utilities.path_mappings import path_mappings

    if media_type == 'episode':
        store_subtitles(path_mappings.path_replace_reverse(video_path), video_path)
    elif media_type == 'movie':
        store_subtitles_movie(path_mappings.path_replace_reverse_movie(video_path), video_path)

    if media_id and media_type:
        if media_type == 'episode':
            from app.database import TableEpisodes, database, select
            from arr_instances.resolution import scoped
            # Scope the lookup to the owning instance (#156): media_id is the
            # upstream sonarrEpisodeId, not unique across instances. Emit the
            # LOCAL episode id so the frontend invalidates the right cached row.
            metadata = database.execute(
                scoped(select(TableEpisodes.id, TableEpisodes.sonarrSeriesId)
                       .where(TableEpisodes.sonarrEpisodeId == media_id),
                       TableEpisodes.arr_instance_id, arr_instance_id)
            ).first()
            if metadata:
                event_stream(type='series', payload=metadata.sonarrSeriesId)
            event_stream(type='episode', payload=metadata.id if metadata else media_id)
        else:
            event_stream(type='movie', payload=media_id)

    jobs_queue.update_job_name(
        job_id=job_id,
        new_job_name=f'{mod_label}: {filename}',
    )


def subtitles_apply_mods(language, subtitle_path, mods, video_path):
    mods = with_keep_lyrics(mods)
    language = alpha3_from_alpha2(language)
    custom = CustomLanguage.from_value(language, "alpha3")
    if custom is None:
        lang_obj = Language(language)
    else:
        lang_obj = custom.subzero_language()
    single = settings.general.single_language

    sub = Subtitle(lang_obj, mods=mods, original_format=True)
    with open(subtitle_path, 'rb') as f:
        sub.content = f.read()

    if not sub.is_valid():
        logging.exception(f'BAZARR Invalid subtitle file: {subtitle_path}')  # noqa: G004
        return

    content = sub.get_modified_content(format=sub.format)
    if content:
        if hasattr(sub, 'mods') and isinstance(sub.mods, list) and has_remove_hi(sub.mods):
            # get the modded subtitles path if the subtitles are alongside the video
            modded_subtitles_path_if_alongside_video = get_subtitle_path(
                video_path,
                language=None if single else sub.language,
                forced_tag=sub.language.forced,
                hi_tag=False,
                tags=[],
                extension=f".{sub.format}"
            )

            # get the real modded subtitles path taking into account if the user set up Bazarr to store external
            # subtitles in a custom folder or relative folder
            modded_subtitles_path = get_external_subtitles_path(
                file=video_path,
                subtitle=os.path.basename(modded_subtitles_path_if_alongside_video)
            )
        else:
            modded_subtitles_path = subtitle_path

        if os.path.exists(subtitle_path):
            os.remove(subtitle_path)

        if os.path.exists(modded_subtitles_path):
            os.remove(modded_subtitles_path)

        with open(modded_subtitles_path, 'wb') as f:
            f.write(content)

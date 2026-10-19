# coding=utf-8
"""Extract to file: an embedded subtitle track written next to its video.

The track is pulled out of the container with the extractors translation
already uses (text as SRT, image-based tracks as they are) and written as a
new external subtitle, named the way a downloaded subtitle for that language
would be, in the folder downloads go to. It never overwrites a file. The
write gets its own history row (action 8, Extracted), the media is indexed
again, and from then on the file is an ordinary external subtitle every tool
works on.

``materialise_embedded_track`` is the primitive. The queued jobs below run it
on its own (Extract to file) or ahead of a tool the user picked on the
embedded row (the tool then works on the new file). OCR will write its result
through ``publish_new_subtitle`` with its own provider.
"""

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace

from subliminal_patch.core import SUBTITLE_EXTENSIONS, get_subtitle_path
from subliminal_patch.score import MAX_SCORES
from subzero.language import Language

from app.config import settings
from app.database import (TableArrInstances, TableEpisodes, TableHistory, TableHistoryMovie,
                          TableHistorySports, TableMovies, TableShows, TableSportsEvents, database, select)
from app.event_handler import event_stream
from app.jobs_queue import JobFailed, jobs_queue
from arr_instances.resolution import scoped
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha3_from_alpha2, language_from_alpha2
from subtitles.new_subtitle import SubtitleFileExists, write_new_subtitle
from subtitles.tools.embedded_streams import (OUTPUT_EXTENSIONS, TEXT_OUTPUT_EXTENSIONS, bitmap_family,
                                              output_extensions)
from utilities.helper import get_target_folder
from utilities.path_mappings import path_mappings

logger = logging.getLogger(__name__)

EXTRACTED = 8
PROVIDER = "embedded"


class ExtractionError(Exception):
    """A reason the user can act on. ``status`` is what the API answers."""

    status = 400


class TrackNotFound(ExtractionError):
    status = 404


class NotExtractable(ExtractionError):
    status = 415


@dataclass(frozen=True)
class EmbeddedTrack:
    """One embedded track as the row shows it, with where its video is."""

    media_type: str          # "movie", "episode" or "sports"
    media_id: int            # radarrId, sonarrEpisodeId, or the local sports event id
    arr_instance_id: int | None
    language: str            # alpha2, as the row's language key starts
    hi: bool
    forced: bool
    video_path: str          # the local path, through the owner's mapping
    db_path: str             # the path as the row stores it
    local_id: int
    series_id: int | None    # sonarrSeriesId of an episode
    title: str

    @property
    def language_code(self):
        """The language as history stores it: hi wins over forced."""
        return self.language + (":hi" if self.hi else ":forced" if self.forced else "")

    @property
    def label(self):
        name = language_from_alpha2(self.language) or self.language.upper()
        return name + (" (HI)" if self.hi else " (forced)" if self.forced else "")


def resolve_track(media_type, media_id, arr_instance_id, language, hi=False, forced=False):
    """The row behind a request, scoped to its owner. Raises TrackNotFound."""
    if media_type == "sports":
        row = database.execute(scoped(
            select(TableSportsEvents.id, TableSportsEvents.path, TableSportsEvents.arr_instance_id,
                   TableSportsEvents.title)
            # An enabled Sportarr owner only: its mapping has no global
            # fallback and refuses a disabled one.
            .join(TableArrInstances, TableSportsEvents.arr_instance_id == TableArrInstances.id)
            .where(TableSportsEvents.id == media_id, TableArrInstances.kind == "sportarr",
                   TableArrInstances.enabled == 1),
            TableSportsEvents.arr_instance_id, arr_instance_id)).first()
        if not row:
            raise TrackNotFound("Sports event not found")
        owner = row.arr_instance_id
        local_id, series_id, title = row.id, None, row.title
    elif media_type == "episode":
        row = database.execute(scoped(
            select(TableEpisodes.id, TableEpisodes.path, TableEpisodes.arr_instance_id,
                   TableEpisodes.sonarrSeriesId, TableEpisodes.season, TableEpisodes.episode,
                   TableShows.title)
            .join(TableShows, TableEpisodes.series_id == TableShows.id, isouter=True)
            .where(TableEpisodes.sonarrEpisodeId == media_id),
            TableEpisodes.arr_instance_id, arr_instance_id)).first()
        if not row:
            raise TrackNotFound("Episode not found")
        owner = row.arr_instance_id
        local_id, series_id = row.id, row.sonarrSeriesId
        title = f"{row.title or 'Episode'} S{row.season or 0:02d}E{row.episode or 0:02d}"
    elif media_type == "movie":
        row = database.execute(scoped(
            select(TableMovies.id, TableMovies.path, TableMovies.arr_instance_id, TableMovies.title)
            .where(TableMovies.radarrId == media_id),
            TableMovies.arr_instance_id, arr_instance_id)).first()
        if not row:
            raise TrackNotFound("Movie not found")
        owner = row.arr_instance_id
        local_id, series_id, title = row.id, None, row.title
    else:
        raise ExtractionError(f"Unknown media type: {media_type}")
    return EmbeddedTrack(
        media_type=media_type, media_id=media_id, arr_instance_id=owner, language=language,
        hi=bool(hi), forced=bool(forced),
        video_path=path_mappings.path_replace_instance(row.path, owner, media_type),
        db_path=row.path, local_id=local_id, series_id=series_id, title=title)


def _subtitle_language(language, hi, forced):
    """The Language a downloaded subtitle in this language would carry."""
    alpha3 = alpha3_from_alpha2(language)
    custom = CustomLanguage.from_value(alpha3, "alpha3") if alpha3 else None
    lang_obj = custom.subzero_language() if custom is not None else Language(alpha3)
    return Language.rebuild(lang_obj, hi=hi, forced=forced)


def output_paths(video_path, language, hi, forced, extensions, create=False):
    """Where the extracted files go: named and placed like a downloaded subtitle.

    ``<video>.<language>[.<hi extension>|.forced].<ext>`` in the configured
    subtitle folder, or next to the video when none is set. The HI tag is the
    one the HI Extension setting names, as for downloads.
    """
    lang_obj = _subtitle_language(language, hi, forced)
    folder = get_target_folder(video_path, create=create) or os.path.dirname(video_path)
    return [os.path.join(folder, os.path.basename(
        get_subtitle_path(video_path, language=lang_obj, extension=extension,
                          forced_tag=forced, hi_tag=hi)))
            for extension in extensions]


def twins(video_path, paths):
    """The same names next to the video, where the indexer also reads them."""
    folder = os.path.dirname(video_path)
    return [os.path.join(folder, os.path.basename(path)) for path in paths
            if os.path.dirname(path) != folder]


def existing_output(video_path, paths):
    """The first of ``paths`` (or of its twins next to the video) already on disk."""
    return next((path for path in (*paths, *twins(video_path, paths)) if os.path.isfile(path)), None)


def single_language_paths(video_path, extensions):
    """The language-less names a subtitle takes in single-language mode, when ``extensions`` write text.

    In that mode downloads and uploads are saved as ``<video>.<ext>``, with no
    language in the name, and keep their own format (``<video>.ass``, say).
    Such a file already is the subtitle of the one language, so a text
    extraction counts every such name the indexer lists as taken rather than
    write a second ``<video>.<language>.srt`` beside it.
    """
    if not settings.general.single_language or not set(extensions) & set(TEXT_OUTPUT_EXTENSIONS):
        return []
    folder = get_target_folder(video_path, create=False) or os.path.dirname(video_path)
    return [os.path.join(folder, os.path.basename(get_subtitle_path(video_path, extension=extension)))
            for extension in SUBTITLE_EXTENSIONS]


def taken_names(track, extensions):
    """Every name that, as a file, stops an extraction of ``track`` to ``extensions``."""
    return (output_paths(track.video_path, track.language, track.hi, track.forced, extensions)
            + single_language_paths(track.video_path, extensions))


def refuse_taken_name(track, extensions):
    """Raise SubtitleFileExists when a name an extraction of ``track`` would take is a file."""
    taken = existing_output(track.video_path, taken_names(track, extensions))
    if taken:
        raise SubtitleFileExists(taken)


def _extract_files(track, codec, work_dir, text_only=False):
    """(extensions, files) pulled out of the video for ``track``.

    Only the track the row names: its language with exactly its HI and forced
    flags. Translate's language-only fallback would write another variant's
    text under this row's name (the full English track as Film.en.forced.srt,
    which players then show as forced subtitles), so it is off here.

    Text is tried first unless the row showed an image-based codec: a row
    indexed before the codec was stored has none, and its track may still be
    image-based, so that case falls back to copying it out as it is, unless
    the caller needs text. An image copy reads the whole container, and its
    files are never listed (so a second mass extract collects the same track
    again): the names it would take are checked once the track is chosen,
    before ffmpeg runs.
    """
    from subtitles.tools.translate.batch import extract_embedded_bitmap, extract_embedded_subtitle

    if not bitmap_family(codec):
        # A text codec picks between text tracks of one language key.
        text = extract_embedded_subtitle(track.video_path, track.language, track.media_type,
                                         hi=track.hi, forced=track.forced,
                                         arr_instance_id=track.arr_instance_id, exact=True, codec=codec)
        if text:
            return TEXT_OUTPUT_EXTENSIONS, [text]
    if text_only:
        raise NotExtractable(
            f"The {track.label} track could not be read as text, and the tools need text. If it is "
            f"image-based, Extract to file copies it out as it is, for players.")
    files = extract_embedded_bitmap(track.video_path, track.language, track.media_type, work_dir,
                                    hi=track.hi, forced=track.forced,
                                    arr_instance_id=track.arr_instance_id, codec=codec,
                                    before_copy=lambda family: refuse_taken_name(track, OUTPUT_EXTENSIONS[family]))
    if files:
        return output_extensions(_family_of(files)), files
    raise NotExtractable(
        f"Could not extract the {track.label} track. The language track was not found, or ffmpeg "
        f"could not read it (see the log).")


def _family_of(files):
    return {".sup": "pgs", ".idx": "vobsub", ".mks": "dvbsub"}[os.path.splitext(files[0])[1]]


def _source_score(track):
    """The score of the track's own action 7 row, else the media type's maximum."""
    if track.media_type == "sports":
        table, where = TableHistorySports, [TableHistorySports.event_id == track.local_id]
    elif track.media_type == "episode":
        table, where = TableHistory, [TableHistory.sonarrEpisodeId == track.media_id]
    else:
        table, where = TableHistoryMovie, [TableHistoryMovie.radarrId == track.media_id]
    score = database.execute(scoped(
        select(table.score).where(*where, table.language == track.language_code, table.action == 7)
        .order_by(table.timestamp.desc()),
        table.arr_instance_id, track.arr_instance_id)).scalar()
    return score or MAX_SCORES["episode" if track.media_type == "episode" else "movie"]


def log_extracted(track, subtitle_path, *, provider=PROVIDER, description=None, sports_context=None,
                  score=None):
    """Record history action 8 for a file written from ``track``.

    ``score`` defaults to the track's own embedded score. The file's content
    is remembered too, so the upgrade job can tell this file from one put
    under its name later (extraction_provenance).
    """
    from subtitles.tools.extraction_provenance import record

    score = _source_score(track) if score is None else score
    description = description or f"{track.label} subtitles extracted from the video file."
    kind = "sports" if track.media_type == "sports" else track.media_type
    reverse = path_mappings.path_replace_reverse_instance
    record(track.arr_instance_id, reverse(subtitle_path, track.arr_instance_id, kind), subtitle_path)
    if track.media_type == "sports":
        from sportarr.subtitles import sports_history

        result = SimpleNamespace(message=description, path=sports_context.original_path,
                                 language_code=track.language_code, provider=provider, score=score,
                                 subs_id=None, subs_path=reverse(subtitle_path, track.arr_instance_id, kind),
                                 matched=[], not_matched=[])
        sports_history(database, sports_context, result, action=EXTRACTED)
        database.commit()
        return
    from subtitles.processing import ProcessSubtitlesResult

    result = ProcessSubtitlesResult(
        message=description, reversed_path=track.db_path, downloaded_language_code2=track.language,
        downloaded_provider=provider, score=score, forced=track.forced, subtitle_id=None,
        reversed_subtitles_path=reverse(subtitle_path, track.arr_instance_id, kind),
        hearing_impaired=track.hi)
    if track.media_type == "episode":
        from sonarr.history import history_log

        history_log(EXTRACTED, track.series_id, track.media_id, result, fake_provider=provider,
                    fake_score=score, arr_instance_id=track.arr_instance_id)
    else:
        from radarr.history import history_log_movie

        history_log_movie(EXTRACTED, track.media_id, result, fake_provider=provider, fake_score=score,
                          arr_instance_id=track.arr_instance_id)


def reindex(track):
    """Index the media again and push the events the other writes push."""
    if track.media_type == "sports":
        from subtitles.indexer.sports import store_subtitles_sports

        # Sports subtitle writes ask Sportarr for no rescan: the file
        # publication already reached the media servers.
        try:
            store_subtitles_sports(track.local_id, track.arr_instance_id)
        except Exception:
            # The file is written and published; the next index lists it.
            logger.exception("BAZARR could not index sports event %s after extracting a track",
                             track.local_id)
        event_stream(type="sports", action="update", payload=track.local_id)
    elif track.media_type == "episode":
        from subtitles.indexer.series import store_subtitles

        # The video did not change, so its cached probe still holds.
        store_subtitles(track.db_path, track.video_path, arr_instance_id=track.arr_instance_id)
        event_stream(type="series", payload=track.series_id)
        event_stream(type="episode", payload=track.local_id)
    else:
        from subtitles.indexer.movies import store_subtitles_movie

        store_subtitles_movie(track.db_path, track.video_path, arr_instance_id=track.arr_instance_id)
        event_stream(type="movie", payload=track.local_id)


def publish_new_subtitle(track, outputs, *, provider=PROVIDER, description=None, publication_guard=None,
                         sports_context=None):
    """Write ``outputs`` ([(path, bytes or source file)]) for ``track``, log action 8, index again.

    Raises SubtitleFileExists when a file of that name is already there, or,
    in single-language mode, the language-less text name a download takes.
    """
    destinations = [path for path, _data in outputs]
    single = [path for path in single_language_paths(track.video_path,
                                                     [os.path.splitext(path)[1] for path in destinations])
              if path not in destinations]
    paths = write_new_subtitle(track.video_path, outputs, track.media_type, track.arr_instance_id,
                               operation="extract", publication_guard=publication_guard,
                               also_check=single + twins(track.video_path, destinations + single))
    log_extracted(track, paths[0], provider=provider, description=description,
                  sports_context=sports_context)
    reindex(track)
    return paths


def materialise_embedded_track(media_type, media_id, arr_instance_id, language, hi=False, forced=False,
                               codec=None, *, text_only=False, cancel=None):
    """Extract one embedded track to a new subtitle file next to its video.

    Returns the written paths (the .idx first for VobSub). Raises
    ExtractionError or SubtitleFileExists, having written nothing.
    """
    track = resolve_track(media_type, media_id, arr_instance_id, language, hi, forced)
    return materialise_track(track, codec, text_only=text_only, cancel=cancel)


@contextmanager
def publication_scope(track, cancel=None):
    """``(track, publication_guard, sports_context)`` to write a new file for ``track`` under.

    Sports writes inside the owned boundary that pins the recording, so work
    that started before a resync replaced it cannot publish; the track's video
    path becomes the one that operation resolved. The other media types need
    neither and get None for both.
    """
    if track.media_type != "sports":
        yield track, None, None
        return
    from sportarr.subtitles import sports_manual_operation

    with sports_manual_operation(track.local_id, track.arr_instance_id, cancel) as (
            context, _validate, guard, video_path):
        yield replace(track, video_path=video_path), guard, context


def materialise_track(track, codec=None, *, text_only=False, cancel=None):
    """materialise_embedded_track for a track already resolved."""
    with publication_scope(track, cancel) as (scoped_track, guard, sports_context):
        return _materialise(scoped_track, codec, guard, sports_context, text_only)


def _materialise(track, codec, guard, sports_context, text_only):
    from subtitles.tools.translate.batch import extracted_subtitles_dir

    os.makedirs(extracted_subtitles_dir(), exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=".bitmap-", dir=extracted_subtitles_dir())
    try:
        extensions, files = _extract_files(track, codec, work_dir, text_only)
        destinations = output_paths(track.video_path, track.language, track.hi, track.forced, extensions,
                                    create=True)
        return publish_new_subtitle(track, list(zip(destinations, files)), publication_guard=guard,
                                    sports_context=sports_context)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def extract_embedded_track(media_type, media_id, arr_instance_id, language, hi=False, forced=False,
                           codec=None, label=None, job_id=None):
    """Extract to file, as a queued job. Returns the job id when it queues one."""
    if not job_id:
        # No local may be assigned before this call: the queue re-invokes the
        # function with the frame's locals.
        return jobs_queue.add_job_from_function(f"Extract to file: {label or language.upper()}",
                                                is_progress=False, return_existing=True)
    try:
        written = materialise_embedded_track(media_type, media_id, arr_instance_id, language, hi, forced,
                                             codec)
    except (ExtractionError, SubtitleFileExists) as error:
        raise JobFailed(str(error)) from error
    jobs_queue.update_job_name(job_id=job_id, new_job_name=f"Extracted {os.path.basename(written[0])}")
    return written


def embedded_tool_allowed(action):
    """Whether ``action`` is a tool that can run on an embedded track once extracted."""
    if action == "sync":
        return True
    from subzero.modification import registry

    return (action or "").split("(", 1)[0] in registry.mods


def tool_name(action):
    from subtitles.tools.mods import MOD_LABELS

    if action == "sync":
        return "Sync"
    base = action.split("(", 1)[0]
    return MOD_LABELS.get(base) or {"shift_offset": "Adjust Times", "change_FPS": "Change Frame Rate",
                                     "two_point_fit": "Two-Point Fit", "color": "Add Color"}.get(base, base)


def tool_output_paths(track, action):
    """Where a tool leaves its result when that is not the file extraction wrote.

    Remove HI saves under the language's plain name, with no HI or forced tag
    (and no language at all in single-language mode), and drops the file it
    read, as it does for any subtitle. That name has to be free as well, or
    the tool would replace a file the user never picked.
    """
    from subtitles.tools.mods import has_remove_hi

    if not has_remove_hi([action]):
        return []
    language = None if settings.general.single_language else _subtitle_language(track.language, False, False)
    folder = get_target_folder(track.video_path, create=False) or os.path.dirname(track.video_path)
    name = os.path.basename(get_subtitle_path(track.video_path, language=language, extension=".srt"))
    return [os.path.join(folder, name)]


def run_tool_on_embedded_track(action, media_type, media_id, arr_instance_id, language, hi=False,
                               forced=False, options=None, label=None, job_id=None, codec=None):
    """Extract a text track to a file, then run the tool the user picked on that file.

    One job, named after the tool; extraction is its first step. The tool
    works on the new file next to the video, never on the extraction cache.
    ``codec`` is the text codec the row showed, which picks between text
    tracks of one language key.
    """
    if not job_id:
        return jobs_queue.add_job_from_function(f"{tool_name(action)}: {label or language.upper()}",
                                                is_progress=True, return_existing=True)
    jobs_queue.update_job_progress(job_id=job_id, progress_message="Extracting the embedded subtitle track")
    try:
        track = resolve_track(media_type, media_id, arr_instance_id, language, hi, forced)
        subtitle_path = materialise_track(track, codec, text_only=True)[0]
    except (ExtractionError, SubtitleFileExists) as error:
        raise JobFailed(str(error)) from error
    # The request checked this name; a file may have appeared since. The mod
    # checks it again under its own lock and never replaces it.
    replaced = existing_output(track.video_path,
                               [path for path in tool_output_paths(track, action) if path != subtitle_path])
    if replaced:
        raise JobFailed(_taken_by_another_file(replaced, subtitle_path))
    if action == "sync":
        return _sync(track, subtitle_path, options or {}, job_id)
    from subtitles.tools.mods import apply_subtitle_mods

    try:
        output_path = apply_subtitle_mods(
            language=track.language, subtitle_path=subtitle_path, mods=[action], video_path=track.video_path,
            media_type=track.media_type,
            media_id=track.local_id if track.media_type == "sports" else track.media_id,
            job_id=job_id, arr_instance_id=track.arr_instance_id, never_replace=True)
    except JobFailed as failure:
        if isinstance(failure.__cause__, SubtitleFileExists):
            raise JobFailed(_taken_by_another_file(failure.__cause__.path, subtitle_path)) from failure.__cause__
        raise
    if output_path and output_path != subtitle_path:
        _log_renamed_extraction(track, output_path, action)
    return output_path


def _log_renamed_extraction(track, output_path, action):
    """Carry the extraction over to the name a tool saved its result under.

    Remove HI saves under the language's plain name and drops the file it
    read, so the action 8 row of the extracted file now names a file that is
    gone. Without a row for the new one, an older download of the plain
    language whose file was deleted stayed an upgrade candidate, adopted the
    new file as its own and replaced it.
    """
    plain = replace(track, hi=False, forced=False)
    description = f"{track.label} subtitles extracted from the video file, then {tool_name(action)}."
    with publication_scope(track) as (_scoped, _guard, sports_context):
        log_extracted(plain, output_path, description=description, sports_context=sports_context,
                      score=_source_score(track))


def _taken_by_another_file(taken, subtitle_path):
    return (f"A subtitle file named {os.path.basename(taken)} already exists. Use the tool on that file's "
            f"row instead. {os.path.basename(subtitle_path)} was extracted and kept.")


def _sync(track, subtitle_path, options, job_id):
    from subtitles.sync import sync_subtitles

    arguments = dict(
        srt_path=subtitle_path, srt_lang=track.language, hi=track.hi, forced=track.forced,
        percent_score=0, job_id=job_id, force_sync=True, arr_instance_id=track.arr_instance_id,
        max_offset_seconds=options.get("max_offset_seconds"),
        no_fix_framerate=bool(options.get("no_fix_framerate")), gss=bool(options.get("gss")),
        output_mode=options.get("output_mode"), enabled_engines=options.get("enabled_engines"))
    if track.media_type == "sports":
        from sportarr.subtitles import sports_manual_operation

        with sports_manual_operation(track.local_id, track.arr_instance_id) as (
                context, validate, guard, video_path):
            return sync_subtitles(video_path=video_path, reference=options.get("reference") or video_path,
                                  context=context, validate=validate, publication_guard=guard, **arguments)
    from api.subtitles.subtitles import postprocess_subtitles

    episode = track.media_type == "episode"
    metadata = SimpleNamespace(sonarrSeriesId=track.series_id)
    return sync_subtitles(
        video_path=track.video_path, reference=options.get("reference") or track.video_path,
        sonarr_series_id=track.series_id if episode else None,
        sonarr_episode_id=track.media_id if episode else None,
        radarr_id=None if episode else track.media_id,
        callback=lambda: postprocess_subtitles(subtitle_path, track.video_path, track.media_type, metadata,
                                               track.media_id, arr_instance_id=track.arr_instance_id),
        **arguments)

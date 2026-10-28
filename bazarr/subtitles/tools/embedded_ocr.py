# coding=utf-8
"""OCR to text: an embedded image-based track read into an SRT next to its video.

The cues are rendered with ffmpeg and read by Tesseract in a worker process
(``subtitles.tools.ocr``), and the text is written the way Extract to file
writes a new subtitle: named like a downloaded subtitle for the language, in
the folder downloads go to, never over an existing file, logged as history
action 8 with the provider ``ocr-<engine>``, and indexed, so the result is an
ordinary external subtitle every tool works on.

Manual only: the row menu, the mass action and a manual Translate from an
image track start it; no search and no automatic translation does. PGS and
VobSub only. The row's stored codec decides whether a track can be read at
all; the probe decides which stream of the video is read.
"""

import ast
import logging
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager

from app.database import TableEpisodes, TableMovies, TableSportsEvents, database, select
from app.jobs_queue import JobFailed, jobs_queue
from languages.get_languages import alpha3_from_alpha2
from subtitles.indexer.entries import entry_codec, normalize_codec
from subtitles.new_subtitle import SubtitleFileExists
from subtitles.tools.embedded_extract import (ExtractionError, NotExtractable, TrackNotFound, output_paths,
                                              publication_scope, publish_new_subtitle, refuse_taken_name,
                                              resolve_track)
from utilities.helper import get_target_folder
from subtitles.tools.embedded_streams import (TEXT_OUTPUT_EXTENSIONS, bitmap_family, codec_label, is_bitmap_codec,
                                              is_ocr_codec)
from subtitles.tools.ocr.errors import OcrError

logger = logging.getLogger(__name__)

# History provider of an OCR'd file: "ocr-" and the engine's name.
PROVIDER_PREFIX = "ocr-"

# The picture size a track is drawn at when ffprobe cannot tell, which is the
# case for a PGS track whose first cue comes after the few seconds ffprobe
# reads. ffmpeg then draws on the video's frame: on a cropped or downscaled
# encode that cuts cues off or shrinks them past reading. A canvas larger than
# the track's own only enlarges the text.
DEFAULT_CANVAS = {"pgs": (1920, 1080), "vobsub": (720, 576)}
PROBE_TIMEOUT = 120

_TABLES = {"movie": TableMovies, "episode": TableEpisodes, "sports": TableSportsEvents}

# One OCR run at a time in this process. A run keeps a core busy for minutes on
# a NAS. A second run never waits for it inside its job: a waiting job would
# hold one of the jobs queue's running slots for the whole first run, and with
# two of them (a two-core NAS) every other job, syncs, searches and backups
# included, would queue behind. So a request is refused while an OCR job is
# queued or running, and a run that finds the slot taken anyway ends at once.
_RUN_SLOT = threading.Lock()
BUSY = ("Another OCR run is queued or running. Try again when it finishes, or use OCR Image Subtitles to read "
        "several tracks one after another.")


class OcrBusy(ExtractionError):
    status = 409


def stored_codecs(track):
    """The codecs of the embedded tracks the row lists under the track's language key.

    The listing is the row's own record of its tracks: a hidden track is not in
    it, and a row indexed before codecs were stored lists None.
    """
    table = _TABLES[track.media_type]
    raw = database.execute(select(table.subtitles).where(table.id == track.local_id)).scalar()
    try:
        entries = ast.literal_eval(raw) if raw else []
    except (ValueError, SyntaxError):
        entries = []
    return [entry_codec(entry) for entry in entries
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and not entry[1]
            and entry[0] == track.language_code]


def _und_default_language():
    """The alpha3 an undefined-language track takes, or None when none is configured."""
    from app.config import settings

    setting = settings.general.default_und_embedded_subtitles_lang
    return alpha3_from_alpha2(setting) if setting else None


def probed_codecs(track):
    """The codecs the video's own tracks report for this language and variant.

    A row indexed before codecs were stored lists ``None``. Probing here keeps
    OCR and OCR-first translation working without a manual Scan Disk, while a
    language absent from the row (a hidden track) still returns [].

    A probed track counts for the row when the indexer would file it under the
    row's language (``embedded_track_language``: custom codes such as pb,
    undefined-language tracks, a bare three-letter tag, and commentary left
    out) with the row's own HI and forced flags.
    """
    from subtitles.tools.translate.batch import embedded_subtitle_tracks
    from utilities.video_analyzer import _title_is_forced, embedded_track_language

    tracks = embedded_subtitle_tracks(track.video_path, track.media_type, track.arr_instance_id) or []
    wanted = alpha3_from_alpha2(track.language)
    und_default = _und_default_language()
    matches = []
    for item in tracks:
        if not item.get("format") or embedded_track_language(item, und_default) != wanted:
            continue
        forced = bool(item.get("forced")) or _title_is_forced((item.get("name") or "").lower())
        hi = bool(item.get("hearing_impaired"))
        if forced == track.forced and hi == track.hi:
            matches.append(normalize_codec(item.get("format")))
    return matches


def ocr_codec(track, requested=None):
    """The stored codec of the track OCR reads for ``track``.

    ``requested`` is the codec the row showed, which picks between tracks that
    share a language key (an SRT and a PGS English on one remux). Raises
    TrackNotFound when the row lists no such track, and NotExtractable (415)
    when the track is not PGS or VobSub, naming what it is.
    """
    codecs = stored_codecs(track)
    if codecs and all(codec is None for codec in codecs):
        # The row knows the track but predates codec storage; the video itself
        # can still say what it is.
        codecs = probed_codecs(track) or codecs
    requested = normalize_codec(requested)
    if not codecs or (requested is not None and requested not in codecs):
        kind = f" {codec_label(requested)}" if requested else ""
        raise TrackNotFound(f"No embedded {track.label}{kind} track is listed for this title. Scan the disk on "
                            f"this title, then try again.")
    codec = requested if requested is not None else next((found for found in codecs if is_ocr_codec(found)),
                                                           codecs[0])
    if is_ocr_codec(codec):
        return codec
    if codec is None:
        reason = (f"Bazarr+ does not know the format of this {track.label} track yet. Scan the disk on this "
                  f"title, then try again.")
    elif is_bitmap_codec(codec):
        reason = f"this {track.label} track is {codec_label(codec)}. Extract to file copies it out as it is."
    else:
        reason = (f"this {track.label} track is {codec_label(codec)}, which is text already. Extract to file "
                  f"writes it as an SRT.")
    raise NotExtractable(f"OCR reads PGS and VobSub tracks only, and {reason}")


def ocr_stream(track, codec):
    """Which of the video's subtitle streams (ffmpeg's ``0:s:N``) OCR reads for ``track``.

    Only a track of the row's own image family with exactly its HI and forced
    flags: the text is written under that row's name.
    """
    from subtitles.tools.embedded_streams import select_subtitle_stream
    from subtitles.tools.translate.batch import embedded_subtitle_tracks

    family = bitmap_family(codec)
    tracks = embedded_subtitle_tracks(track.video_path, track.media_type, track.arr_instance_id) or []
    index = select_subtitle_stream(
        tracks, alpha3_from_alpha2(track.language), hi=track.hi, forced=track.forced, exact=True,
        und_default_language=_und_default_language(),
        accept=lambda found: is_ocr_codec(found) and bitmap_family(found) == family)
    if index is None:
        raise NotExtractable(f"The video has no {track.label} {codec_label(codec)} track to read. Scan the disk "
                             f"on this title, then try again.")
    return index


def subtitle_canvas(video_path, stream_index, family, ffprobe=None):
    """None when ffprobe knows the stream's picture size, else the usual size of its kind."""
    if ffprobe is None:
        from utilities.binaries import get_binary

        try:
            ffprobe = get_binary("ffprobe")
        except Exception:
            return DEFAULT_CANVAS[family]
    command = [ffprobe, "-v", "error", "-select_streams", f"s:{stream_index}",
               "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   errors="replace", timeout=PROBE_TIMEOUT)
        width, height = (int(part) for part in completed.stdout.strip().split(",")[:2])
    except (OSError, subprocess.SubprocessError, ValueError):
        return DEFAULT_CANVAS[family]
    return None if width > 0 and height > 0 else DEFAULT_CANVAS[family]


def progress_message(step, value, maximum, pack=None):
    """What a job says while OCR runs: the pack download, the render, each cue, the write."""
    if step == "pack" and maximum > 1:
        name = f"{pack}.traineddata" if pack else "for this language"
        return f"Downloading the OCR language pack {name} ({value / 1e6:.1f} of {maximum / 1e6:.1f} MB)"
    if step == "pack":
        # The pack is loaded; the render starts now. ffmpeg reports its own
        # progress only once a second, so a short render would say nothing.
        return "Rendering the image subtitles"
    if step == "render":
        if maximum > 0:
            percent = max(0, min(100, int(value * 100 / maximum)))
            return f"Rendering the image subtitles ({percent}%)"
        return "Rendering the image subtitles"
    if step == "recognise":
        return f"Reading cue {value} of {maximum}"
    if step == "write":
        return "Writing the SRT next to the video"
    return step


# Translation jobs whose OCR phase is over. A translation of an image track is
# an OCR job while it waits to start and while it reads the track; after that
# it only translates, which is the translation lane's business and no reason
# to refuse OCR. Ids, guarded by _ENQUEUE_LOCK (defined below) because the
# check that reads them and the queueing it guards are one step. Ids of jobs
# that left the queue are dropped on the next check.
_OCR_PHASE_OVER = set()


def ocr_phase_started(job_id):
    """A translation job begins reading its source: it counts as OCR again (a retried job keeps its id)."""
    with _ENQUEUE_LOCK:
        _OCR_PHASE_OVER.discard(job_id)


def ocr_phase_over(job_id):
    """A translation job finished reading its source, or failed to: from now on it does not count as OCR."""
    with _ENQUEUE_LOCK:
        _OCR_PHASE_OVER.add(job_id)


def _reads_with_ocr(job):
    """Whether a queued job reads an image track with OCR, now or still to come.

    Three kinds do: a row's OCR to text, a mass OCR, and a manual translation
    whose embedded source names an image codec (it reads that track with OCR
    inside its own job before it translates). The translation stops counting
    once its reading is over (see ``ocr_phase_over``).

    Head of line: a translation that reads an image track holds a place in the
    translation lane for its whole life. With ``translator.openrouter_max_concurrent``
    at 1 that is the only place, and the jobs queued behind it wait for its
    OCR and its translation; the queue starts jobs strictly in order, so this
    does not reorder them.
    """
    func = job.get("func")
    kwargs = job.get("kwargs") or {}
    if func == "ocr_embedded_track":
        return True
    if func == "mass_batch_operation":
        return kwargs.get("action") == "ocr"
    if func == "translate_subtitles_file":
        return (is_ocr_codec((kwargs.get("embedded_source") or {}).get("codec"))
                and job.get("job_id") not in _OCR_PHASE_OVER)
    return False


def ocr_job_waiting_or_running(exclude=None):
    """Whether a job that reads with OCR is queued or running.

    A row's OCR to text, a mass OCR and a translation of an image source that
    has not finished reading it all count. ``exclude`` is the asking job's own
    id. Callers hold ``ocr_queue_lock`` for the check and the queueing that
    follows it; a job's own thread can call this without it (the lock is
    reentrant).
    """
    jobs = jobs_queue.list_jobs_from_queue()
    with _ENQUEUE_LOCK:
        _OCR_PHASE_OVER.intersection_update(job.get("job_id") for job in jobs)
        for job in jobs:
            if job.get("job_id") == exclude or job.get("status") not in ("pending", "running"):
                continue
            if _reads_with_ocr(job):
                return True
    return False


@contextmanager
def one_run_at_a_time():
    """Hold the process's OCR slot, or raise OcrBusy at once when another run has it."""
    if not _RUN_SLOT.acquire(blocking=False):
        raise OcrBusy(BUSY)
    try:
        yield
    finally:
        _RUN_SLOT.release()


# Serialises looking for a queued OCR job with queueing another one. Two
# requests (or a request and a mass OCR) that both read an empty queue would
# otherwise both queue a job, and the queue would hold two OCR runs where one
# slot exists. Reentrant: the check takes it again to read the phase marks.
_ENQUEUE_LOCK = threading.RLock()


@contextmanager
def ocr_queue_lock():
    """Hold the lock that makes the one-OCR check and the queueing one step."""
    with _ENQUEUE_LOCK:
        yield


def refuse_unwritable_folder(folder):
    """Raise ExtractionError when a new file cannot be created in ``folder``.

    Checked before anything is read, so a read-only library or a user without
    write access fails in seconds instead of after the whole run.
    """
    try:
        handle, probe = tempfile.mkstemp(prefix=".bazarr-ocr-", suffix=".tmp", dir=folder)
        os.close(handle)
        os.remove(probe)
    except OSError as error:
        raise ExtractionError(f"Bazarr+ cannot write subtitles into {folder} ({error.strerror or error}). "
                              f"Nothing was read.") from error


def ocr_engine():
    """The engine OCR reads with. One for now; the pipeline takes any engine."""
    from subtitles.tools.ocr.tesseract import TesseractEngine

    return TesseractEngine()


def ocr_track(track, codec, *, progress=None, engine=None):
    """Read ``track`` with OCR and write the SRT next to its video.

    Returns ``(path, OcrResult)``. Raises ExtractionError, OcrError or
    SubtitleFileExists having written nothing. The name and the folder are
    checked before the container is read, and the name again under the write
    lock; a sports recording is pinned for the whole run, so one replaced
    meanwhile publishes nothing.
    """
    from subtitles.tools.ocr.pipeline import run_ocr
    from subtitles.tools.ocr.tessdata import pack_name

    report = progress or (lambda step, value, maximum: None)
    alpha3 = alpha3_from_alpha2(track.language)
    # A language without a pack fails here, before anything is read.
    pack_name(alpha3)
    with publication_scope(track) as (scoped, guard, sports_context):
        refuse_taken_name(scoped, TEXT_OUTPUT_EXTENSIONS)
        from app.config import settings
        configured = get_target_folder(scoped.video_path, create=True)
        custom = str(settings.general.subfolder_custom or "").strip()
        if settings.general.subfolder != "current" and custom and configured is None:
            # get_target_folder falls back to the video's folder after it fails to
            # create a configured one. Writing there would ignore the setting and
            # the path the request answered, so fail before anything is read.
            wanted = custom if settings.general.subfolder == "absolute" else os.path.join(
                os.path.dirname(scoped.video_path), custom)
            refuse_unwritable_folder(wanted)
        destination = output_paths(scoped.video_path, scoped.language, scoped.hi, scoped.forced,
                                   TEXT_OUTPUT_EXTENSIONS, create=True)[0]
        refuse_unwritable_folder(os.path.dirname(destination))
        with one_run_at_a_time():
            index = ocr_stream(scoped, codec)
            canvas = subtitle_canvas(scoped.video_path, index, bitmap_family(codec))
            result = run_ocr(scoped.video_path, index, alpha3, engine or ocr_engine(), progress=report,
                             canvas_size=canvas)
        report("write", 1, 1)
        description = (f"{scoped.label} subtitles read from the {codec_label(codec)} track with OCR "
                       f"({result.cues} cues, {result.dropped} empty).")
        try:
            paths = publish_new_subtitle(scoped, [(destination, result.srt.encode("utf-8"))],
                                         provider=PROVIDER_PREFIX + result.engine, description=description,
                                         publication_guard=guard, sports_context=sports_context)
        except OSError as error:
            if error.filename and os.path.dirname(error.filename) != os.path.dirname(destination):
                raise
            # Writing into a folder that was writable before the run: a disk
            # that filled up, or access that changed since.
            raise ExtractionError(f"Bazarr+ could not write {destination} ({error.strerror or error}).") from error
    return paths[0], result


def ocr_embedded(media_type, media_id, arr_instance_id, language, hi=False, forced=False, codec=None, *,
                 progress=None):
    """ocr_track for the row a request or a mass item names."""
    track = resolve_track(media_type, media_id, arr_instance_id, language, hi, forced)
    # Re-read the row's listing at run time: a track hidden after the request was
    # queued disappears from it, and must not be read from the video anyway.
    codec = ocr_codec(track, codec)
    return ocr_track(track, codec, progress=progress)


def job_progress(job_id, language):
    """A progress callback that reports to the job and stops the run when the job is cancelled."""
    from subtitles.tools.ocr.tessdata import pack_name

    try:
        pack = pack_name(alpha3_from_alpha2(language))
    except Exception:
        # A display label; an unloaded language table or an unknown code must
        # not stop the job's progress callback from existing.
        pack = None

    def report(step, value, maximum):
        message = progress_message(step, value, maximum, pack=pack)
        if step in ("recognise", "render") or (step == "pack" and maximum > 1):
            jobs_queue.update_job_progress(job_id=job_id, progress_value=value, progress_max=maximum,
                                           progress_message=message)
        elif step == "write":
            jobs_queue.update_job_progress(job_id=job_id, progress_value="max", progress_message=message)
        else:
            jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_message=message)

    return report


def ocr_embedded_track(media_type, media_id, arr_instance_id, language, hi=False, forced=False, codec=None,
                       label=None, job_id=None):
    """OCR to text, as a queued job. Returns the job id when it queues one.

    Stopping the job stops the run at its next step (between cues, or within
    a second while ffmpeg renders): the Tesseract worker ends and the rendered
    frames are removed.
    """
    if not job_id:
        # No local may be assigned before this call: the queue re-invokes the
        # function with the frame's locals.
        return jobs_queue.add_job_from_function(f"OCR to text: {label or language.upper()}", is_progress=True)
    try:
        path, result = ocr_embedded(media_type, media_id, arr_instance_id, language, hi, forced, codec,
                                    progress=job_progress(job_id, language))
    except SubtitleFileExists as error:
        raise JobFailed(f"{error}. Delete it first to read this track with OCR.") from error
    except (ExtractionError, OcrError) as error:
        raise JobFailed(str(error)) from error
    jobs_queue.update_job_name(job_id=job_id,
                               new_job_name=f"OCR to text: wrote {os.path.basename(path)} ({result.cues} cues)")
    return path

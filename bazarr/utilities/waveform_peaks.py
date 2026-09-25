# coding=utf-8

"""Waveform peaks for the subtitle editor, generated as a job and cached on disk.

Reading a film's whole audio track through ffmpeg used to happen inside the GET
that asked for the peaks, which held the request for as long as ffmpeg ran. Now
the GET serves the cached peaks when they exist and otherwise queues one job per
media file and audio track, which writes the cache for the next GET to find.
"""

import hashlib
import json
import logging
import os
import struct
import subprocess
import tempfile
import threading

from app.get_args import args
from app.jobs_queue import JobCancelled, JobFailed, jobs_queue
from utilities.job_dedupe import enqueue_or_existing

logger = logging.getLogger(__name__)

PEAKS_CACHE_DIR = os.path.join(args.config_dir, 'cache', 'peaks')
# Peaks are about 10 floats per second of audio, so a two-hour film is well under
# a megabyte of JSON. This keeps a few hundred of them.
PEAKS_CACHE_MAX_BYTES = 200 * 1024 * 1024

PEAKS_MODULE = 'utilities.waveform_peaks'
PEAKS_FUNC = 'generate_waveform_peaks'

SAMPLE_RATE = 800
PEAKS_PER_SECOND = 10
# Report progress once per this many peaks, i.e. per minute of audio.
PROGRESS_EVERY_PEAKS = PEAKS_PER_SECOND * 60
# How often a running ffmpeg is checked for a Stop, in seconds.
CANCEL_POLL_SECONDS = 0.5


def peaks_cache_file(video_path, audio_track):
    """Where the peaks for this file and track live. A changed file gets a new name.

    Whole seconds were not enough: a replacement written within the same second,
    or copied with its modification time kept, reused the old name and was served
    the old file's peaks. The nanosecond time and the size together tell them apart.
    """
    stat = os.stat(video_path)
    path_hash = hashlib.md5(video_path.encode()).hexdigest()
    track_suffix = f'_t{audio_track}' if audio_track > 0 else ''
    return os.path.join(PEAKS_CACHE_DIR,
                        f'{path_hash}_{stat.st_mtime_ns}_{stat.st_size}{track_suffix}.json')


def read_cached_peaks(video_path, audio_track):
    """The cached peaks for this file and track, or None."""
    cache_file = peaks_cache_file(video_path, audio_track)
    try:
        with open(cache_file, 'r') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # A read counts as use, so eviction removes what nobody opens any more.
    try:
        os.utime(cache_file, None)
    except OSError:
        pass
    return data


def prune_peaks_cache(max_bytes=PEAKS_CACHE_MAX_BYTES, keep=None):
    """Delete the least recently used peak files until the cache fits in ``max_bytes``."""
    try:
        names = os.listdir(PEAKS_CACHE_DIR)
    except OSError:
        return
    entries = []
    for name in names:
        # Only finished files: a .tmp belongs to a job that is still writing it.
        if not name.endswith('.json'):
            continue
        path = os.path.join(PEAKS_CACHE_DIR, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if os.path.isfile(path):
            entries.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= max_bytes:
            break
        if path == keep:
            continue
        try:
            os.remove(path)
            total -= size
        except OSError:
            logger.debug('Could not evict waveform cache file %s', path)


def request_peaks(video_path, audio_track):
    """Answer ``("ready", peaks)`` from the cache, or ``("queued", job_id)``.

    A second request for the same file and track while its job is pending or
    running gets that job's id rather than a second ffmpeg. ``("queued", None)``
    means the job could not be queued.
    """
    for _ in range(2):
        cached = read_cached_peaks(video_path, audio_track)
        if cached is not None:
            return 'ready', cached
        job_id = enqueue_or_existing(f'Generating waveform for {os.path.basename(video_path)}',
                                     PEAKS_MODULE, PEAKS_FUNC,
                                     {'video_path': video_path, 'audio_track': audio_track},
                                     is_progress=True, progress_max=100)
        if job_id:
            _start_in_waveform_lane(job_id)
            return 'queued', job_id
        # The matching job finished between the two looks: its cache is there now.
    return 'queued', None


def _start_in_waveform_lane(job_id):
    """Start ``job_id`` now, unless another waveform is already running.

    The editor is open and waiting on this, so it does not queue behind a
    library scan. Force-starting skips the concurrent jobs limit, though, so
    only one waveform at a time may do it; the next one waits in the normal
    queue instead of starting another ffmpeg beside the first. A job that is
    already running is left as it is.
    """
    with jobs_queue._queue_lock:
        if any(job.module == PEAKS_MODULE and job.func == PEAKS_FUNC
               for job in jobs_queue.jobs_running_queue):
            return False
        return jobs_queue.force_start_pending_job(job_id)


def _job_cancelled(job_id):
    return any(job.job_id == job_id and job.cancelled for job in list(jobs_queue.jobs_running_queue))


def _kill_when_cancelled(process, job_id, finished):
    """Kill ``process`` once the job is cancelled, until ``finished`` is set.

    The read loop only sees a Stop between reads, and an ffmpeg stuck on a
    damaged file or a stalled mount never hands it another one.
    """
    while not finished.wait(CANCEL_POLL_SECONDS):
        if _job_cancelled(job_id):
            process.kill()
            return


def _duration(video_path):
    from utilities.binaries import get_binary
    try:
        ffprobe = get_binary('ffprobe')
    except Exception:
        ffprobe = None
    if not ffprobe:
        raise JobFailed('ffprobe is not available, so the waveform cannot be generated.')
    result = subprocess.run([ffprobe, '-v', 'quiet', '-print_format', 'json', '-show_format', video_path],
                            capture_output=True, timeout=30)
    try:
        return float(json.loads(result.stdout)['format']['duration'])
    except (ValueError, KeyError, TypeError):
        raise JobFailed(f'Could not read the duration of {os.path.basename(video_path)}.')


def _extract_peaks(video_path, audio_track, duration, job_id):
    from utilities.binaries import get_binary
    try:
        ffmpeg = get_binary('ffmpeg')
    except Exception:
        ffmpeg = None
    if not ffmpeg:
        raise JobFailed('ffmpeg is not available, so the waveform cannot be generated.')

    samples_per_peak = max(1, SAMPLE_RATE // PEAKS_PER_SECOND)
    chunk_bytes = samples_per_peak * 4
    cmd = [ffmpeg, '-i', video_path, '-map', f'0:a:{audio_track}', '-ac', '1', '-ar', str(SAMPLE_RATE),
           '-f', 'f32le', '-v', 'error', 'pipe:1']

    peaks = []
    # stderr goes to a file, not a pipe. Only stdout is read while ffmpeg runs,
    # so a damaged track that wrote more errors than a pipe holds blocked ffmpeg
    # on stderr and this loop on stdout, and the job could not even be stopped.
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file)
        finished = threading.Event()
        if job_id is not None:
            threading.Thread(target=_kill_when_cancelled, args=(process, job_id, finished),
                             daemon=True).start()
        try:
            while True:
                data = process.stdout.read(chunk_bytes)
                if not data:
                    break
                n = len(data) // 4
                if n == 0:
                    break
                chunk = struct.unpack(f'<{n}f', data[:n * 4])
                peaks.append(round(max(chunk, key=abs), 4))
                if len(peaks) % PROGRESS_EVERY_PEAKS == 0 and duration > 0:
                    # Also the cancellation point: a cancelled job raises here.
                    done = min(99, int(len(peaks) / PEAKS_PER_SECOND / duration * 100))
                    jobs_queue.update_job_progress(job_id=job_id, progress_value=done, progress_max=100,
                                                   progress_message='Reading the audio track')
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        finally:
            finished.set()
            if process.poll() is None:
                process.kill()
                process.wait()
        stderr_file.seek(0)
        stderr_out = stderr_file.read(500).decode(errors='replace')

    # A Stop that killed ffmpeg ends the read like the end of the file does, so
    # what was read so far is not a waveform.
    if job_id is not None and _job_cancelled(job_id):
        raise JobCancelled(f'Generating the waveform for {os.path.basename(video_path)} was cancelled.')
    if process.returncode not in (0, None):
        logger.error('ffmpeg peaks generation failed: %s', stderr_out)
    if not peaks:
        if stderr_out:
            raise JobFailed(f'ffmpeg could not read audio track {audio_track + 1}: {stderr_out.strip()}')
        raise JobFailed(f'No audio data found in {os.path.basename(video_path)}.')
    return peaks


def generate_waveform_peaks(video_path, audio_track, job_id=None):
    """Generate and cache the peaks for one media file and audio track."""
    if not os.path.isfile(video_path):
        raise JobFailed(f'{os.path.basename(video_path)} is no longer on disk.')
    if read_cached_peaks(video_path, audio_track) is not None:
        return

    jobs_queue.update_job_progress(job_id=job_id, progress_value=0, progress_max=100,
                                   progress_message='Reading the audio track')
    # Named for the file as it was before ffprobe and ffmpeg read it. A file
    # replaced while they ran gets a new name, and these peaks are not its.
    cache_file = peaks_cache_file(video_path, audio_track)
    duration = _duration(video_path)
    peaks = _extract_peaks(video_path, audio_track, duration, job_id)
    try:
        unchanged = peaks_cache_file(video_path, audio_track) == cache_file
    except OSError:
        unchanged = False
    if not unchanged:
        raise JobFailed(f'{os.path.basename(video_path)} changed while its waveform was being read. '
                        'Open it again to read the new file.')

    max_abs = max(abs(p) for p in peaks)
    if max_abs > 0:
        peaks = [round(p / max_abs, 4) for p in peaks]

    temporary = f'{cache_file}.{job_id or 0}.tmp'
    try:
        os.makedirs(PEAKS_CACHE_DIR, exist_ok=True)
        with open(temporary, 'w') as f:
            json.dump({'peaks': peaks, 'duration': round(duration, 2), 'sampleRate': PEAKS_PER_SECOND}, f)
        os.replace(temporary, cache_file)
    except OSError as error:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise JobFailed(f'Could not save the waveform: {error.strerror or error}')
    prune_peaks_cache(keep=cache_file)
    jobs_queue.update_job_progress(job_id=job_id, progress_value=100, progress_max=100,
                                   progress_message='Waveform ready')

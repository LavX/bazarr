# coding=utf-8

import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import time

from flask import Response, request, send_file
from flask_restx import Namespace, Resource

from app.database import TableEpisodes, TableMovies, TableShows, database, select  # noqa: F401
from app.get_args import args
from utilities.path_mappings import path_mappings
from api.subtitles.content import resolve_subtitle_path  # noqa: F401

from ..utils import authenticate

logger = logging.getLogger(__name__)

api_ns_editor = Namespace('Editor', description='Video editor streaming and metadata')

PEAKS_CACHE_DIR = os.path.join(args.config_dir, 'cache', 'peaks')
HLS_CACHE_DIR = os.path.join(args.config_dir, 'cache', 'hls')

# Idle TTL after which an HLS cache directory becomes eligible for eviction.
# Each request to the playlist or any segment refreshes the directory's atime,
# so this only fires for sessions the user has actually walked away from.
HLS_IDLE_TTL_SECONDS = 30 * 60

# Cap on how long we wait inside a request for ffmpeg's first segment to land
# before responding with whatever exists. hls.js will refetch if the playlist
# is empty or short.
HLS_FIRST_SEGMENT_WAIT_SECONDS = 8.0

# Whitelist of files we'll serve from an HLS cache directory.
HLS_FILENAME_RE = re.compile(r'^(playlist\.m3u8|init\.mp4|segment_\d{1,6}\.m4s)$')

# Tracks ffmpeg encoder threads keyed by cache directory so we don't
# double-spawn for concurrent first requests on the same session.
_hls_encoder_locks: dict[str, threading.Lock] = {}
_hls_encoder_locks_guard = threading.Lock()

# Tracks live ffmpeg subprocesses keyed by cache directory so we can terminate
# encoders for sessions the user has walked away from. Without this, switching
# tracks or seeking-before-session-start leaves the original ffmpeg encoding the
# rest of the file in the background, accumulating CPU + disk pressure.
_hls_encoder_processes: dict[str, subprocess.Popen] = {}
_hls_encoder_processes_guard = threading.Lock()

# How long a session must be untouched (no segment requests) before its ffmpeg
# is considered abandoned and gets killed. Short enough that a track switch
# clears the old encoder quickly; long enough that a user briefly seeking
# elsewhere doesn't lose the original session.
HLS_ENCODER_IDLE_TIMEOUT_SECONDS = 60


def _resolve_video_path(media_type, media_id):
    """Look up the video file path from the database and apply path mappings.

    Returns the mapped file path on success, or a (message, status_code) tuple on failure.
    """
    if media_type == 'episode':
        row = database.execute(
            select(TableEpisodes.path)
            .where(TableEpisodes.sonarrEpisodeId == media_id)
        ).first()
        if not row:
            return 'Episode not found', 404
        return path_mappings.path_replace(row.path)

    elif media_type == 'movie':
        row = database.execute(
            select(TableMovies.path)
            .where(TableMovies.radarrId == media_id)
        ).first()
        if not row:
            return 'Movie not found', 404
        return path_mappings.path_replace_movie(row.path)

    return 'Invalid media type, must be "episode" or "movie"', 400


def _get_ffmpeg():
    """Return the path to the ffmpeg binary."""
    from utilities.binaries import get_binary
    return get_binary("ffmpeg")


def _get_ffprobe():
    """Return the path to the ffprobe binary."""
    from utilities.binaries import get_binary
    return get_binary("ffprobe")


def _probe_video(video_path):
    """Run ffprobe and return parsed JSON metadata for the file."""
    ffprobe = _get_ffprobe()
    cmd = [
        ffprobe,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        logger.error('ffprobe failed for %s: %s', video_path, result.stderr.decode(errors='replace'))
        return None
    return json.loads(result.stdout)


def _parse_range_header(range_header, file_size):
    """Parse an HTTP Range header. Returns (start, end) or None."""
    if not range_header or not range_header.startswith('bytes='):
        return None
    try:
        range_spec = range_header[6:]
        start_str, end_str = range_spec.split('-', 1)
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        if start < 0 or start >= file_size or end >= file_size or start > end:
            return None
        return start, end
    except (ValueError, IndexError):
        return None


def _serve_file_with_ranges(file_path, mimetype):
    """Serve a file with HTTP Range request support."""
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get('Range')
    range_tuple = _parse_range_header(range_header, file_size)

    if range_tuple:
        start, end = range_tuple
        length = end - start + 1

        def generate():
            with open(file_path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(64 * 1024, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        response = Response(
            generate(),
            status=206,
            mimetype=mimetype,
            direct_passthrough=True,
        )
        response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response.headers['Content-Length'] = length
        response.headers['Accept-Ranges'] = 'bytes'
        return response

    response = send_file(file_path, mimetype=mimetype)
    response.headers['Accept-Ranges'] = 'bytes'
    return response


def _validate_params():
    """Extract and validate mediaType and mediaId from query parameters.

    Returns (media_type, media_id) on success, or a (message, status_code) tuple on failure.
    """
    media_type = request.args.get('mediaType')
    media_id = request.args.get('mediaId')

    if not media_type or media_type not in ('episode', 'movie'):
        return 'mediaType must be "episode" or "movie"', 400
    if not media_id:
        return 'mediaId is required', 400
    try:
        media_id = int(media_id)
    except (ValueError, TypeError):
        return 'mediaId must be an integer', 400

    return media_type, media_id


def _resolve_or_abort():
    """Validate params and resolve video path. Returns (video_path,) or a Flask error tuple."""
    params = _validate_params()
    # _validate_params returns (str, int) on success or (error_msg, status_code) on failure.
    # On success media_type is "episode" or "movie", on error it's a longer message.
    if isinstance(params[0], str) and params[0] not in ('episode', 'movie'):
        return params

    media_type, media_id = params
    result = _resolve_video_path(media_type, media_id)
    if isinstance(result, tuple):
        return result

    video_path = result
    if not os.path.isfile(video_path):
        return 'Video file not found on disk', 404

    return (video_path,)


def _hls_cache_dir(media_type, media_id, audio_track_idx, start_time_sec, mtime):
    """Stable cache dir per (media, audio track, start time, file mtime) tuple.

    start_time_sec lets callers spawn fresh sessions that begin somewhere other
    than the start of the file (e.g., after a track switch at minute 30). Each
    distinct start point gets its own cache so concurrent sessions don't race.
    """
    raw = (
        f"{media_type}:{media_id}:{audio_track_idx}:{int(start_time_sec)}:{int(mtime)}"
    )
    key = hashlib.md5(raw.encode()).hexdigest()[:16]
    return os.path.join(HLS_CACHE_DIR, key)


def _hls_encoder_lock(cache_dir):
    """Per-cache-dir lock so concurrent first requests don't race on ffmpeg spawn."""
    with _hls_encoder_locks_guard:
        lock = _hls_encoder_locks.get(cache_dir)
        if lock is None:
            lock = threading.Lock()
            _hls_encoder_locks[cache_dir] = lock
        return lock


def _kill_idle_hls_encoders():
    """Terminate ffmpeg HLS encoders whose cache dirs haven't been touched recently.

    Each segment request bumps the cache directory's atime via os.utime, so the
    ffmpeg keeps running as long as hls.js is fetching segments. When the user
    switches sessions (track change, seek-before-start), the old session goes
    silent and its ffmpeg is killed here on the next sweep.
    """
    cutoff = time.time() - HLS_ENCODER_IDLE_TIMEOUT_SECONDS
    with _hls_encoder_processes_guard:
        items = list(_hls_encoder_processes.items())
    for cache_dir, process in items:
        if process.poll() is not None:
            # Encoder exited on its own.
            with _hls_encoder_processes_guard:
                _hls_encoder_processes.pop(cache_dir, None)
            continue
        try:
            atime = os.stat(cache_dir).st_atime
        except OSError:
            atime = 0
        if atime < cutoff:
            try:
                process.terminate()
                logger.info('Terminated idle HLS encoder for %s', cache_dir)
            except Exception:
                logger.exception('Failed to terminate ffmpeg for %s', cache_dir)
            with _hls_encoder_processes_guard:
                _hls_encoder_processes.pop(cache_dir, None)


def _evict_stale_hls_dirs():
    """Remove HLS cache directories that have been idle longer than the TTL."""
    if not os.path.isdir(HLS_CACHE_DIR):
        return
    cutoff = time.time() - HLS_IDLE_TTL_SECONDS
    for entry in os.listdir(HLS_CACHE_DIR):
        path = os.path.join(HLS_CACHE_DIR, entry)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if not os.path.isdir(path):
            continue
        # Skip dirs with an active encoding marker; ffmpeg might still be writing.
        if os.path.isfile(os.path.join(path, '.encoding')):
            continue
        if stat.st_atime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            with _hls_encoder_locks_guard:
                _hls_encoder_locks.pop(path, None)
            with _hls_encoder_processes_guard:
                _hls_encoder_processes.pop(path, None)


def _spawn_hls_encoder(video_path, audio_track_idx, start_time_sec, cache_dir):
    """Start ffmpeg writing HLS to cache_dir in a background thread.

    start_time_sec is applied as an input seek (-ss before -i), so ffmpeg
    snaps to the nearest preceding keyframe and begins encoding from there.
    The resulting playlist's segment 0 corresponds to source time
    keyframe_at_or_before(start_time_sec); the frontend treats playlist time
    zero as start_time_sec for display purposes (small offset error tolerated).

    Idempotent: returns immediately if a complete playlist already exists or
    another thread is currently encoding.
    """
    playlist = os.path.join(cache_dir, 'playlist.m3u8')
    encoding_marker = os.path.join(cache_dir, '.encoding')

    # Already finished encoding (playlist has #EXT-X-ENDLIST, no encoding marker).
    if os.path.isfile(playlist) and not os.path.isfile(encoding_marker):
        return

    lock = _hls_encoder_lock(cache_dir)
    if not lock.acquire(blocking=False):
        # Another thread is spawning ffmpeg; let it finish.
        return

    try:
        # Re-check inside the lock.
        if os.path.isfile(playlist) and not os.path.isfile(encoding_marker):
            return
        if os.path.isfile(encoding_marker):
            return

        os.makedirs(cache_dir, exist_ok=True)
        # Touch the marker before spawning so concurrent requests see it.
        with open(encoding_marker, 'w') as f:
            f.write(str(int(time.time())))

        probe_data = _probe_video(video_path)
        video_codec = None
        if probe_data:
            for stream in probe_data.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_codec = stream.get('codec_name', '').lower()
                    break

        if video_codec in ('h264', 'avc'):
            video_args = ['-c:v', 'copy']
            extra_video_tags = []
        elif video_codec in ('hevc', 'h265'):
            # Stream-copy HEVC and tag with hvc1 so browsers that support it
            # (Safari natively, recent Chrome/Edge with hardware decode) can
            # play the fmp4 segments. If the browser can't decode HEVC,
            # transcode is the next option but we leave that to a future
            # iteration; surfacing the failure to the user is fine here.
            video_args = ['-c:v', 'copy']
            extra_video_tags = ['-tag:v', 'hvc1']
        else:
            video_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28']
            extra_video_tags = []

        has_audio = False
        if probe_data:
            audio_streams = [
                s for s in probe_data.get('streams', []) if s.get('codec_type') == 'audio'
            ]
            has_audio = audio_track_idx < len(audio_streams)

        if has_audio:
            # Always emit 2-channel AAC. Browser support for multichannel AAC
            # (5.1 / 7.1) is patchy: Chrome silently downmixes, Firefox can
            # produce no audio, Safari is hit-or-miss. Forcing -ac 2 here
            # guarantees a single playback path that works everywhere. EAC3
            # / AC3 / DTS sources that would otherwise come out as 6-channel
            # AAC are downmixed to stereo at the encoder.
            audio_args = [
                '-map', f'0:a:{audio_track_idx}',
                '-c:a', 'aac',
                '-ac', '2',
                '-b:a', '128k',
            ]
        else:
            audio_args = ['-an']

        # Input seek to start_time_sec. Placed before -i so ffmpeg uses the
        # demuxer's keyframe-aware seek (fast, avoids the frozen-image hang
        # that an output-side seek would cause with -c:v copy).
        pre_input = ['-ss', str(start_time_sec)] if start_time_sec > 0 else []

        ffmpeg = _get_ffmpeg()
        cmd = [
            ffmpeg,
            *pre_input,
            '-i', video_path,
            '-map', '0:v:0',
            *audio_args,
            *video_args,
            *extra_video_tags,
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '0',
            '-hls_segment_type', 'fmp4',
            '-hls_fmp4_init_filename', 'init.mp4',
            '-hls_segment_filename', os.path.join(cache_dir, 'segment_%03d.m4s'),
            '-v', 'error',
            '-y',
            playlist,
        ]

        def encode():
            process = None
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                with _hls_encoder_processes_guard:
                    _hls_encoder_processes[cache_dir] = process
                try:
                    stderr_data, _ = process.communicate(timeout=7200)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    logger.warning('HLS encode timed out for %s', video_path)
                    return
                if process.returncode != 0 and process.returncode != -15:
                    # rc=-15 is SIGTERM, which is what _kill_idle_hls_encoders
                    # sends; not an error.
                    logger.error(
                        'HLS encode failed for %s (rc=%d): %s',
                        video_path, process.returncode,
                        stderr_data.decode(errors='replace')[:500],
                    )
            except Exception:
                logger.exception('HLS encoding error for %s', video_path)
            finally:
                if process is not None:
                    with _hls_encoder_processes_guard:
                        # Pop only if it's still us (a newer encoder for the
                        # same dir might have replaced the entry).
                        if _hls_encoder_processes.get(cache_dir) is process:
                            _hls_encoder_processes.pop(cache_dir, None)
                try:
                    os.unlink(encoding_marker)
                except OSError:
                    pass

        threading.Thread(target=encode, daemon=True).start()
    finally:
        lock.release()


def _wait_for_first_segment(cache_dir, deadline):
    """Block (briefly) until the playlist has at least one segment listed."""
    playlist = os.path.join(cache_dir, 'playlist.m3u8')
    while time.time() < deadline:
        if os.path.isfile(playlist):
            try:
                with open(playlist, 'r') as f:
                    if any(line.strip().endswith('.m4s') for line in f):
                        return True
            except OSError:
                pass
        time.sleep(0.1)
    return False


@api_ns_editor.route(
    'editor/hls/<string:media_type>/<int:media_id>/<int:audio_track>/<int:start_time>/<string:filename>'
)
class EditorHls(Resource):
    @authenticate
    def get(self, media_type, media_id, audio_track, start_time, filename):
        """HLS playlist + segments served from a per-session cache directory.

        URL path encodes (mediaType, mediaId, audioTrack, startTime) so segment
        URLs in the playlist (which are relative) inherit the same session.
        startTime is the source-time offset in seconds where ffmpeg begins
        encoding; the frontend uses it to create new sessions for track switches
        or seek-before-current-session-start without restarting from t=0.

        First request to playlist.m3u8 spawns ffmpeg; segments are served as
        ffmpeg writes them. hls.js handles segment fetching, buffer management,
        and seek-back within the cached portion.
        """
        if media_type not in ('episode', 'movie'):
            return 'mediaType must be "episode" or "movie"', 400
        if not HLS_FILENAME_RE.match(filename):
            return 'Invalid HLS filename', 400
        if audio_track < 0:
            return 'audioTrack must be >= 0', 400
        if start_time < 0:
            return 'startTime must be >= 0', 400

        resolved = _resolve_video_path(media_type, media_id)
        if isinstance(resolved, tuple):
            return resolved
        video_path = resolved
        if not os.path.isfile(video_path):
            return 'Video file not found on disk', 404

        try:
            mtime = os.stat(video_path).st_mtime
        except OSError:
            return 'Video file not accessible', 404

        cache_dir = _hls_cache_dir(media_type, media_id, audio_track, start_time, mtime)
        target = os.path.join(cache_dir, filename)

        # Refuse anything that escaped the whitelist via realpath shenanigans.
        if os.path.realpath(target) != target:
            return 'Invalid HLS path', 400

        if filename == 'playlist.m3u8':
            # Lazy-sweep on every playlist request: kill any ffmpeg encoders
            # whose sessions have gone idle (user switched tracks / sessions),
            # then evict directories that have been cold long enough.
            try:
                _kill_idle_hls_encoders()
            except Exception:
                logger.exception('HLS encoder sweep error')
            try:
                _evict_stale_hls_dirs()
            except OSError:
                logger.exception('HLS cache eviction error')

            try:
                _spawn_hls_encoder(video_path, audio_track, start_time, cache_dir)
            except Exception:
                logger.exception('Failed to spawn HLS encoder for %s', video_path)
                return 'ffmpeg not available', 500

            # Wait briefly for ffmpeg to write the first segment so the player
            # gets a useful playlist on the first response.
            _wait_for_first_segment(
                cache_dir, time.time() + HLS_FIRST_SEGMENT_WAIT_SECONDS,
            )

            if not os.path.isfile(target):
                return 'Encoding starting, retry shortly', 503

            response = send_file(target, mimetype='application/vnd.apple.mpegurl')
            # Tell hls.js this is a live-ish playlist that may grow until ENDLIST.
            response.headers['Cache-Control'] = 'no-cache'
            return response

        # Init segment or media segment.
        if not os.path.isfile(target):
            # Segment may not have been written yet by ffmpeg; brief wait.
            deadline = time.time() + 5.0
            while time.time() < deadline and not os.path.isfile(target):
                time.sleep(0.1)
            if not os.path.isfile(target):
                return 'Segment not yet available', 404

        mimetype = 'video/mp4' if filename.endswith('.mp4') else 'video/iso.segment'
        response = _serve_file_with_ranges(target, mimetype)
        # Touch parent dir so eviction TTL tracks last access.
        try:
            os.utime(cache_dir, None)
        except OSError:
            pass
        return response


@api_ns_editor.route('editor/peaks')
class EditorPeaks(Resource):
    @authenticate
    def get(self):
        """Return pre-generated waveform peaks as JSON for wavesurfer.js."""
        resolved = _resolve_or_abort()
        if len(resolved) == 2:
            return resolved

        video_path = resolved[0]

        audio_track = request.args.get('audioTrack', '0')
        try:
            audio_track_idx = int(audio_track)
        except (ValueError, TypeError):
            audio_track_idx = 0

        # Build a cache key from the file path and modification time
        stat = os.stat(video_path)
        # Use a stable filename derived from the video path
        import hashlib
        path_hash = hashlib.md5(video_path.encode()).hexdigest()
        track_suffix = f'_t{audio_track_idx}' if audio_track_idx > 0 else ''
        cache_file = os.path.join(PEAKS_CACHE_DIR, f'{path_hash}_{int(stat.st_mtime)}{track_suffix}.json')

        # Check cache
        if os.path.isfile(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                # Corrupted cache, regenerate
                pass

        # Get duration first
        probe_data = _probe_video(video_path)
        if not probe_data:
            return 'Failed to probe video file', 500

        duration = None
        fmt = probe_data.get('format', {})
        if 'duration' in fmt:
            try:
                duration = float(fmt['duration'])
            except (ValueError, TypeError):
                pass

        if duration is None:
            return 'Could not determine video duration', 500

        # Generate peaks by having ffmpeg output low-rate PCM and processing in chunks.
        # Use 800Hz sample rate (much less data than 8000Hz) and produce ~10 peaks/sec.
        try:
            ffmpeg = _get_ffmpeg()
        except Exception:
            logger.exception('ffmpeg binary not available')
            return 'ffmpeg not found', 500

        sample_rate = 800
        target_rate = 10
        samples_per_peak = max(1, sample_rate // target_rate)  # 80 samples per peak
        chunk_bytes = samples_per_peak * 4  # 320 bytes per peak

        cmd = [
            ffmpeg,
            '-i', video_path,
            '-map', f'0:a:{audio_track_idx}',
            '-ac', '1',
            '-ar', str(sample_rate),
            '-f', 'f32le',
            '-v', 'error',
            'pipe:1',
        ]

        peaks = []
        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            while True:
                data = process.stdout.read(chunk_bytes)
                if not data:
                    break
                n = len(data) // 4
                if n == 0:
                    break
                chunk = struct.unpack(f'<{n}f', data[:n * 4])
                max_val = max(chunk, key=abs)
                peaks.append(round(max_val, 4))
            process.wait(timeout=10)
            if process.returncode != 0:
                stderr_out = process.stderr.read().decode(errors='replace')[:500]
                logger.error('ffmpeg peaks generation failed: %s', stderr_out)
                if not peaks:
                    return 'Failed to generate audio peaks', 500
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()
            if not peaks:
                return 'Peak generation timed out', 500

        if not peaks:
            return 'No audio data found in file', 500

        # Normalize to [-1, 1]
        max_abs = max(abs(p) for p in peaks)
        if max_abs > 0:
            peaks = [round(p / max_abs, 4) for p in peaks]

        response_data = {
            'peaks': peaks,
            'duration': round(duration, 2),
            'sampleRate': target_rate,
        }

        # Cache the result
        try:
            os.makedirs(PEAKS_CACHE_DIR, exist_ok=True)
            with open(cache_file, 'w') as f:
                json.dump(response_data, f)
        except OSError:
            logger.warning('Failed to cache peaks for %s', video_path)

        return response_data


@api_ns_editor.route('editor/info')
class EditorInfo(Resource):
    @authenticate
    def get(self):
        """Return video metadata for the editor UI."""
        resolved = _resolve_or_abort()
        if len(resolved) == 2:
            return resolved

        video_path = resolved[0]

        probe_data = _probe_video(video_path)
        if not probe_data:
            return 'Failed to probe video file', 500

        duration = None
        fmt = probe_data.get('format', {})
        if 'duration' in fmt:
            try:
                duration = float(fmt['duration'])
            except (ValueError, TypeError):
                pass

        video_codec = None
        audio_codec = None
        resolution = None
        audio_tracks = []
        audio_index = 0

        for stream in probe_data.get('streams', []):
            codec_type = stream.get('codec_type')
            if codec_type == 'video' and video_codec is None:
                video_codec = stream.get('codec_name')
                width = stream.get('width')
                height = stream.get('height')
                if width and height:
                    resolution = f'{width}x{height}'
            elif codec_type == 'audio':
                if audio_codec is None:
                    audio_codec = stream.get('codec_name')
                tags = stream.get('tags', {})
                lang = tags.get('language', '')
                title = tags.get('title', '')
                codec = stream.get('codec_name', '')
                channels = stream.get('channels', 0)
                label_parts = []
                if lang:
                    label_parts.append(lang)
                if title:
                    label_parts.append(title)
                label_parts.append(codec)
                if channels:
                    ch_label = {1: 'Mono', 2: 'Stereo', 6: '5.1', 8: '7.1'}.get(channels, f'{channels}ch')
                    label_parts.append(ch_label)
                audio_tracks.append({
                    'index': audio_index,
                    'codec': codec,
                    'language': lang,
                    'title': title,
                    'channels': channels,
                    'label': ' - '.join(label_parts),
                })
                audio_index += 1

        container = os.path.splitext(video_path)[1].lstrip('.').lower()

        return {
            'duration': round(duration, 2) if duration else None,
            'videoCodec': video_codec,
            'audioCodec': audio_codec,
            'resolution': resolution,
            'container': container,
            'audioTracks': audio_tracks,
        }


@api_ns_editor.route('editor/subtitles')
class EditorSubtitles(Resource):
    @authenticate
    def get(self):
        """Return available subtitle files for a media item."""
        import ast
        params = _validate_params()
        if isinstance(params[0], str) and params[0] not in ('episode', 'movie'):
            return params

        media_type, media_id = params

        if media_type == 'episode':
            row = database.execute(
                select(TableEpisodes.subtitles)
                .where(TableEpisodes.sonarrEpisodeId == media_id)
            ).first()
        else:
            row = database.execute(
                select(TableMovies.subtitles)
                .where(TableMovies.radarrId == media_id)
            ).first()

        if not row or not row.subtitles:
            return {'subtitles': []}

        try:
            subtitles_list = ast.literal_eval(row.subtitles)
        except (ValueError, SyntaxError):
            return {'subtitles': []}

        if not isinstance(subtitles_list, list):
            return {'subtitles': []}

        result = []
        for item in subtitles_list:
            if isinstance(item, list) and len(item) >= 2 and item[1]:
                lang_code = item[0]  # e.g. "en", "en:hi", "hu"
                file_path = item[1]
                ext = os.path.splitext(file_path)[1].lower().lstrip('.')
                result.append({
                    'language': lang_code,
                    'format': ext,
                })

        return {'subtitles': result}


_editor_sync_jobs = {}  # job_key -> {status, content, message}


def run_editor_sync(job_key, video_path, tmp_in, tmp_out, encoding, max_offset, gss, reference,
                    no_fix_framerate=True, vad=None, job_id=None):
    """Background sync worker. Called by jobs_queue."""
    from app.jobs_queue import jobs_queue

    def update_progress(name, value, count):
        _editor_sync_jobs[job_key]['message'] = name
        if job_id:
            jobs_queue.update_job_progress(job_id, progress_value=value, progress_max=count, progress_message=name)

    try:
        update_progress('Extracting audio...', 0, 3)
        logger.info('Editor sync starting: video=%s, srt=%s, max_offset=%s, gss=%s, ref=%s, vad=%s, no_fix_framerate=%s',
                     video_path, tmp_in, max_offset, gss, reference, vad, no_fix_framerate)
        from subtitles.tools.subsyncer import SubSyncer
        subsync = SubSyncer()
        if vad:
            subsync.vad = vad
        try:
            update_progress('Running ffsubsync...', 1, 3)
            subsync.sync(
                video_path=video_path,
                srt_path=tmp_in,
                srt_lang='und',
                forced=False,
                hi=False,
                max_offset_seconds=max_offset,
                no_fix_framerate=no_fix_framerate,
                gss=gss,
                reference=reference,
                force_sync=True,
            )
        finally:
            del subsync

        logger.info('Editor sync ffsubsync finished, reading result...')
        update_progress('Reading result...', 2, 3)
        synced_path = tmp_out if os.path.isfile(tmp_out) else tmp_in
        logger.info('Editor sync result path: %s (exists=%s)', synced_path, os.path.isfile(synced_path))
        if not os.path.isfile(synced_path):
            raise FileNotFoundError(f'Synced subtitle file not found (expected at {synced_path})')
        with open(synced_path, 'r', encoding=encoding, errors='replace') as f:
            synced_content = f.read()

        if not synced_content.strip():
            raise ValueError('Synced subtitle file is empty')

        update_progress('Sync complete', 3, 3)
        _editor_sync_jobs[job_key] = {'status': 'completed', 'content': synced_content, 'message': 'Sync complete'}

    except Exception as e:
        logger.exception('Editor sync failed')
        _editor_sync_jobs[job_key] = {'status': 'failed', 'content': None, 'message': str(e)[:500]}
    finally:
        # Clean up in-memory result after 10 minutes
        import threading
        def cleanup():
            _editor_sync_jobs.pop(job_key, None)
            # Clean up temp files only after result has been consumed
            for p in (tmp_in, tmp_out):
                if p and os.path.isfile(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        threading.Timer(600, cleanup).start()


@api_ns_editor.route('editor/sync')
class EditorSync(Resource):
    @authenticate
    def post(self):
        """Start syncing editor content via ffsubsync. Returns a job key to poll."""
        import tempfile
        import hashlib

        from app.jobs_queue import jobs_queue

        data = request.get_json(silent=True) or {}
        media_type = data.get('mediaType')
        media_id = data.get('mediaId')
        content = data.get('content', '')
        encoding = data.get('encoding', 'utf-8')
        fmt = data.get('format', 'srt')
        max_offset = str(data.get('maxOffsetSeconds', 120))
        gss = data.get('gss', False)
        reference = data.get('reference', 'a:0')
        no_fix_framerate = data.get('noFixFramerate', True)
        vad = data.get('vad', None)
        if vad and vad not in ('subs_then_webrtc', 'subs_then_auditok', 'webrtc', 'auditok'):
            return 'Invalid vad option', 400

        if not media_type or media_type not in ('episode', 'movie'):
            return 'mediaType must be "episode" or "movie"', 400
        if not media_id:
            return 'mediaId is required', 400
        if not content:
            return 'content is required', 400

        # Whitelist the format so we never feed attacker-controlled characters
        # (e.g. path-traversal, shell metachars) into the tempfile suffix.
        if fmt not in ('srt', 'vtt', 'ass', 'ssa', 'sub', 'smi', 'mpl', 'txt'):
            return 'Invalid format; must be one of srt, vtt, ass, ssa, sub, smi, mpl, txt', 400

        try:
            media_id = int(media_id)
        except (ValueError, TypeError):
            return 'mediaId must be an integer', 400

        video_path = _resolve_video_path(media_type, media_id)
        if isinstance(video_path, tuple):
            return video_path

        if not os.path.isfile(video_path):
            return 'Video file not found', 404

        ext = f'.{fmt}'
        fd, tmp_in = tempfile.mkstemp(suffix=ext, prefix='bazarr_sync_')
        os.write(fd, content.encode(encoding))
        os.close(fd)
        tmp_out = tmp_in.replace(ext, f'.synced{ext}')

        import threading

        job_key = f'editor_sync_{hashlib.md5(tmp_in.encode()).hexdigest()[:8]}'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': 'Starting sync...'}

        # Submit to the jobs queue for visibility in Jobs Manager
        queue_job_id = jobs_queue.feed_jobs_pending_queue(
            job_name='Editor Sync',
            module='api.editor.editor',
            func='run_editor_sync',
            kwargs={
                'job_key': job_key,
                'video_path': video_path,
                'tmp_in': tmp_in,
                'tmp_out': tmp_out,
                'encoding': encoding,
                'max_offset': max_offset,
                'gss': gss,
                'reference': reference,
                'no_fix_framerate': no_fix_framerate,
                'vad': vad,
            },
            is_progress=True,
            progress_max=3,
        )

        # Force-start in a separate thread so it doesn't wait behind other queued jobs
        threading.Thread(
            target=jobs_queue.force_start_pending_job,
            args=(queue_job_id,),
            daemon=True,
        ).start()

        return {'jobKey': job_key, 'status': 'running'}, 202

    @authenticate
    def get(self):
        """Poll sync job status. Returns synced content when complete."""
        job_key = request.args.get('jobKey')
        if not job_key or job_key not in _editor_sync_jobs:
            logger.debug('Editor sync poll: key=%s not found (known keys: %s)', job_key, list(_editor_sync_jobs.keys()))
            return {'status': 'not_found'}, 404

        job = _editor_sync_jobs[job_key]
        if job['status'] == 'running':
            return {'status': 'running', 'message': job.get('message', '')}, 200

        if job['status'] == 'completed':
            content = job['content']
            logger.info('Editor sync poll: returning completed content (%d chars) for key=%s', len(content) if content else 0, job_key)
            _editor_sync_jobs.pop(job_key, None)
            return {'status': 'completed', 'content': content}, 200

        # Failed
        msg = job.get('message', 'Unknown error')
        _editor_sync_jobs.pop(job_key, None)
        return {'status': 'failed', 'message': msg}, 200

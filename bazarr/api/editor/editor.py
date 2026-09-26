# coding=utf-8

import codecs
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

from flask import Response, request, send_file
from flask_restx import Namespace, Resource, fields

from arr_instances.resolution import scoped
from app.database import (TableArrInstances, TableEpisodes, TableMovies, TableShows,  # noqa: F401
                          TableSportsEvents, database, select)
from app.get_args import args
from utilities.path_mappings import path_mappings
from utilities.waveform_peaks import request_peaks
from api.subtitles.content import resolve_subtitle_path  # noqa: F401
from subtitles.tools.subsync_engines import (create_preview_workspace, discard_preview_workspace,
                                             is_sync_engine_language_key)

from ..utils import authenticate

logger = logging.getLogger(__name__)

api_ns_editor = Namespace('Editor', description='Video editor streaming and metadata')

HLS_CACHE_DIR = os.path.join(args.config_dir, 'cache', 'hls')

# Bump this whenever the HLS encoding strategy changes in a way that makes
# existing cache directories unsafe to reuse.
HLS_CACHE_VERSION = 'hls-v2'

# Idle TTL after which an HLS cache directory becomes eligible for eviction.
# Each request to the playlist or any segment refreshes the directory's atime,
# so this only fires for sessions the user has actually walked away from.
HLS_IDLE_TTL_SECONDS = 30 * 60

# Cap on how long we wait inside a request for ffmpeg's first segment to land
# before responding with whatever exists. hls.js will refetch if the playlist
# is empty or short.
HLS_FIRST_SEGMENT_WAIT_SECONDS = 8.0

# How long a segment request waits for ffmpeg to write a segment it lacks.
HLS_SEGMENT_WAIT_SECONDS = 5.0

# Whitelist of files we'll serve from an HLS cache directory.
HLS_FILENAME_RE = re.compile(r'playlist\.m3u8|init\.mp4|segment_\d{1,6}\.m4s')

# Tracks ffmpeg encoder threads keyed by cache directory so we don't
# double-spawn for concurrent first requests on the same session.
_hls_encoder_locks: dict[str, threading.Lock] = {}
_hls_encoder_locks_guard = threading.Lock()


class _HlsEncoder:
    """One live ffmpeg HLS encoder and the session state the reaper needs."""

    __slots__ = ('process', 'players', 'last_access')

    def __init__(self, process, player, last_access):
        self.process = process
        # Players that requested this session, with when they last did. Tabs
        # on the same file, track and start point share one session.
        self.players = {player: last_access} if player else {}
        self.last_access = last_access


# Live ffmpeg encoders keyed by cache directory. Every encoder is registered
# here from the moment it starts, so the reaper can find the ones the preview
# has walked away from. Without this, a closed preview or a track switch left
# ffmpeg transcoding the rest of the file in the background.
_hls_encoders: dict[str, _HlsEncoder] = {}
_hls_encoders_guard = threading.Lock()

# Sessions a newer session of the same player replaced, with when. A late
# segment request from the torn-down player must not start the old session
# again and displace the new one. Guarded by _hls_encoders_guard.
_hls_replaced_dirs: dict[str, float] = {}

# Sessions whose encoder is being stopped and whose directory is about to be
# deleted. Nothing may start a new encoder there until that is done, or the
# deletion would take the new encoder's files with it. Guarded by
# _hls_encoders_guard.
_hls_stopping_dirs: set[str] = set()

# How long a session may go without a playlist or segment request before its
# ffmpeg is considered abandoned and stopped. hls.js refreshes the playlist
# every few seconds while the preview is open, even when paused, so this only
# fires once the preview is gone. The margin covers browsers that throttle
# timers in background tabs.
HLS_ENCODER_IDLE_TIMEOUT_SECONDS = 90

# How often the background reaper looks for idle encoders and stale caches.
HLS_SWEEP_INTERVAL_SECONDS = 20

# At most this many encoders run at once. Each one can use a couple of CPU
# cores, and a single preview only ever needs one.
HLS_MAX_ENCODERS = 2

# How long a stopped ffmpeg gets to exit after SIGTERM before it is killed.
HLS_ENCODER_STOP_GRACE_SECONDS = 5.0

# The preview sends a random id per player, so a new session only replaces
# encoders that player was using. Other tabs keep their own sessions.
HLS_PLAYER_RE = re.compile(r'[A-Za-z0-9]{1,32}')

_hls_reaper_thread = None
_hls_reaper_stop = threading.Event()
_hls_reaper_guard = threading.Lock()


def _now():
    """Monotonic clock for encoder idle tracking. Tests replace it."""
    return time.monotonic()


def _optional_int(value, field_name):
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return f'{field_name} must be an integer', 400


def _request_arr_instance_id():
    return _optional_int(request.args.get('arr_instance_id'), 'arr_instance_id')


def _payload_arr_instance_id(data):
    value = data.get('arrInstanceId')
    if value in (None, ''):
        value = data.get('arr_instance_id')
    return _optional_int(value, 'arrInstanceId')


# The media types the editor works on. One tuple rather than six hardcoded
# pairs: the copies are what left sports rejected at five separate gates while
# the sixth already understood it.
MEDIA_TYPES = ('episode', 'movie', 'sports')
MEDIA_TYPE_ERROR = 'mediaType must be one of "episode", "movie", "sports"'
# Maps an accepted request value to the tuple's own string, so validation
# hands back a constant and never the request's copy of it.
_CANONICAL_MEDIA_TYPES = {media_type: media_type for media_type in MEDIA_TYPES}


def _resolve_video_path(media_type, media_id, arr_instance_id=None):
    """Look up the video file path from the database and apply path mappings.

    Returns the mapped file path on success, or a (message, status_code) tuple on failure.
    """
    if media_type == 'episode':
        row = database.execute(
            scoped(
                select(TableEpisodes.path).where(TableEpisodes.sonarrEpisodeId == media_id),
                TableEpisodes.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not row:
            return 'Episode not found', 404
        return path_mappings.path_replace(row.path)

    elif media_type == 'movie':
        row = database.execute(
            scoped(
                select(TableMovies.path).where(TableMovies.radarrId == media_id),
                TableMovies.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not row:
            return 'Movie not found', 404
        return path_mappings.path_replace_movie(row.path)

    elif media_type == 'sports':
        # media_id is the local event id, a primary key, so scoping is about
        # enforcing ownership rather than resolving a collision. The row's own
        # owner drives the mapping: sports mappings are per instance and the
        # caller is allowed to omit arr_instance_id.
        # Joined to an ENABLED sportarr owner: the sports mapping below refuses
        # a disabled one and would raise a 500 out of this handler instead.
        row = database.execute(
            scoped(
                select(TableSportsEvents.path, TableSportsEvents.arr_instance_id)
                .join(TableArrInstances,
                      TableSportsEvents.arr_instance_id == TableArrInstances.id)
                .where(TableSportsEvents.id == media_id,
                       TableArrInstances.kind == 'sportarr',
                       TableArrInstances.enabled == 1),
                TableSportsEvents.arr_instance_id,
                arr_instance_id,
            )
        ).first()
        if not row:
            return 'Sports event not found', 404
        return path_mappings.path_replace_instance(row.path, row.arr_instance_id, 'sports')

    return MEDIA_TYPE_ERROR, 400


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


def _can_direct_play(video_path, probe_data=None):
    """Check if the video can be served directly to the browser."""
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in ('.mp4', '.m4v'):
        return False

    if probe_data is None:
        probe_data = _probe_video(video_path)
    if not probe_data:
        return False

    for stream in probe_data.get('streams', []):
        if stream.get('codec_type') == 'video':
            codec = stream.get('codec_name', '').lower()
            return codec in ('h264', 'avc')
    return False


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
    # Keep the canonical constant, not the request string. _resolve_or_abort
    # returns this tuple unchanged when it holds an error, and a static
    # analyser cannot tell that branch from success, so a request string here
    # reads as flowing into the ffprobe command line.
    media_type = _CANONICAL_MEDIA_TYPES.get(request.args.get('mediaType'))
    media_id = request.args.get('mediaId')

    if media_type is None:
        return MEDIA_TYPE_ERROR, 400
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
    # On success media_type is one of MEDIA_TYPES, on error it's a longer message.
    if isinstance(params[0], str) and params[0] not in MEDIA_TYPES:
        return params

    media_type, media_id = params
    arr_instance_id = _request_arr_instance_id()
    if isinstance(arr_instance_id, tuple):
        return arr_instance_id

    result = _resolve_video_path(media_type, media_id, arr_instance_id=arr_instance_id)
    if isinstance(result, tuple):
        return result

    video_path = result
    if not os.path.isfile(video_path):
        return 'Video file not found on disk', 404

    return (video_path,)


def _hls_cache_dir(media_type, media_id, arr_instance_id, audio_track_idx, start_time_sec, mtime):
    """Stable cache dir per (media, audio track, start time, file mtime) tuple.

    start_time_sec lets callers spawn fresh sessions that begin somewhere other
    than the start of the file (e.g., after a track switch at minute 30). Each
    distinct start point gets its own cache so concurrent sessions don't race.

    The start time is keyed at millisecond precision (3 decimals). Truncating
    to whole seconds would let a request for 30.789 reuse an already-finished
    playlist generated for 30.123 in the same int-second bucket -- the stream
    contents wouldn't actually start at 30.789, so the user-facing clock
    (startSec + video.currentTime) would drift by up to ~999 ms.
    """
    raw = (
        f"{HLS_CACHE_VERSION}:{media_type}:{media_id}:{arr_instance_id or ''}:{audio_track_idx}:"
        f"{start_time_sec:.3f}:{int(mtime)}"
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


def _is_playlist_complete(playlist_path):
    """A finished HLS VOD playlist ends with #EXT-X-ENDLIST. Anything else
    (an empty file, a manifest still being appended, or a manifest from an
    encoder that was killed mid-stream) is partial."""
    try:
        with open(playlist_path, 'rb') as f:
            return b'#EXT-X-ENDLIST' in f.read()
    except OSError:
        return False


def _remove_hls_dir(cache_dir):
    shutil.rmtree(cache_dir, ignore_errors=True)
    with _hls_encoder_locks_guard:
        # Drop the per-dir lock only when nobody holds it. A spawn holding it
        # must stay the only one for this directory.
        lock = _hls_encoder_locks.get(cache_dir)
        if lock is not None and lock.acquire(blocking=False):
            _hls_encoder_locks.pop(cache_dir, None)
            lock.release()


def _stop_hls_encoder(cache_dir, encoder, reason, remove_dir=True):
    """Stop one encoder that is already out of the registry, then drop its cache.

    SIGTERM first, SIGKILL after a grace period, and always a wait, so no
    zombie is left behind. The segment directory goes too: it holds a
    truncated session that nothing will reuse.
    """
    process = encoder.process
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=HLS_ENCODER_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=HLS_ENCODER_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning('HLS encoder for %s did not exit after SIGKILL', cache_dir)
        logger.info('Terminated %s HLS encoder for %s', reason, cache_dir)
    if remove_dir:
        _remove_hls_dir(cache_dir)
        with _hls_encoders_guard:
            _hls_stopping_dirs.discard(cache_dir)


def _stop_hls_encoders(displaced):
    for cache_dir, encoder, reason, remove_dir in displaced:
        try:
            _stop_hls_encoder(cache_dir, encoder, reason, remove_dir=remove_dir)
        except Exception:
            logger.exception('Failed to stop HLS encoder for %s', cache_dir)
            with _hls_encoders_guard:
                _hls_stopping_dirs.discard(cache_dir)


def _register_hls_encoder(cache_dir, process, player=None):
    """Register a new encoder and return the ones it displaces.

    A player only ever plays one session, so its new session (an audio-track
    switch, a seek outside the current session, another file) replaces the
    sessions it was using, unless another player used one of them recently.
    Past that, the least recently used encoders make room so no more than
    HLS_MAX_ENCODERS run at once. The caller stops the returned encoders
    outside the lock, with _stop_hls_encoders.
    """
    now = _now()
    displaced = []

    def displace(key, reason):
        displaced.append((key, _hls_encoders.pop(key), reason, key != cache_dir))
        if key != cache_dir:
            _hls_stopping_dirs.add(key)

    with _hls_encoders_guard:
        if cache_dir in _hls_encoders:
            displace(cache_dir, 'replaced')
        if player:
            for other_dir, other in list(_hls_encoders.items()):
                if other.players.pop(player, None) is None:
                    continue
                shared = any(now - seen <= HLS_ENCODER_IDLE_TIMEOUT_SECONDS
                             for seen in other.players.values())
                if not shared:
                    displace(other_dir, 'replaced')
                    _hls_replaced_dirs[other_dir] = now
        while True:
            # An encoder that already exited does not hold a slot; its waiter
            # thread is about to unregister it.
            live = [key for key, encoder in _hls_encoders.items() if encoder.process.poll() is None]
            if len(live) < HLS_MAX_ENCODERS:
                break
            displace(min(live, key=lambda key: _hls_encoders[key].last_access), 'over-limit')
        _hls_encoders[cache_dir] = _HlsEncoder(process, player, now)
        _hls_replaced_dirs.pop(cache_dir, None)
    return displaced


def _hls_encoder_is_live(cache_dir):
    with _hls_encoders_guard:
        encoder = _hls_encoders.get(cache_dir)
        return encoder is not None and encoder.process.poll() is None


def _hls_dir_is_stopping(cache_dir):
    with _hls_encoders_guard:
        return cache_dir in _hls_stopping_dirs


def _hls_segment_miss_needs_encoder(cache_dir):
    """Whether a missing segment should start the session's encoder again."""
    with _hls_encoders_guard:
        encoder = _hls_encoders.get(cache_dir)
        if encoder is not None and encoder.process.poll() is None:
            return False
        if cache_dir in _hls_stopping_dirs:
            return False
        replaced_at = _hls_replaced_dirs.get(cache_dir)
        return replaced_at is None or _now() - replaced_at > HLS_ENCODER_IDLE_TIMEOUT_SECONDS


def _touch_hls_session(cache_dir, player=None):
    """Record a playlist or segment request, which keeps the session alive."""
    with _hls_encoders_guard:
        encoder = _hls_encoders.get(cache_dir)
        if encoder is not None:
            encoder.last_access = _now()
            if player:
                encoder.players[player] = encoder.last_access
    # The directory timestamp drives cache eviction for finished sessions.
    try:
        os.utime(cache_dir, None)
    except OSError:
        pass


def _reap_idle_hls_encoders():
    """Stop encoders whose session has had no request for the idle timeout."""
    cutoff = _now() - HLS_ENCODER_IDLE_TIMEOUT_SECONDS
    idle = []
    with _hls_encoders_guard:
        for cache_dir, replaced_at in list(_hls_replaced_dirs.items()):
            if replaced_at < cutoff:
                _hls_replaced_dirs.pop(cache_dir, None)
        for cache_dir, encoder in list(_hls_encoders.items()):
            # An encoder that exited on its own is unregistered by its waiter
            # thread, which also clears the encoding marker.
            if encoder.process.poll() is None and encoder.last_access < cutoff:
                idle.append((cache_dir, _hls_encoders.pop(cache_dir), 'idle', True))
                _hls_stopping_dirs.add(cache_dir)
    _stop_hls_encoders(idle)


def _evict_stale_hls_dirs():
    """Remove HLS cache directories that have been idle longer than the TTL."""
    if not os.path.isdir(HLS_CACHE_DIR):
        return
    cutoff = time.time() - HLS_IDLE_TTL_SECONDS
    with _hls_encoders_guard:
        live = set(_hls_encoders) | _hls_stopping_dirs
    for entry in os.listdir(HLS_CACHE_DIR):
        path = os.path.join(HLS_CACHE_DIR, entry)
        if path in live or not os.path.isdir(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        # Requests touch the directory, and a new session's marker and
        # segments bump its mtime, so both times are fresh while in use.
        if max(stat.st_atime, stat.st_mtime) < cutoff:
            _remove_hls_dir(path)


def _sweep_hls_cache():
    """One reaper pass: stop idle encoders, then evict stale cache dirs."""
    try:
        _reap_idle_hls_encoders()
    except Exception:
        logger.exception('HLS encoder sweep error')
    try:
        _evict_stale_hls_dirs()
    except Exception:
        logger.exception('HLS cache eviction error')


def _hls_reaper_loop(stop_event):
    while not stop_event.wait(HLS_SWEEP_INTERVAL_SECONDS):
        _sweep_hls_cache()


def _ensure_hls_reaper_started():
    """Start the background reaper once. It runs whether or not requests come in."""
    global _hls_reaper_thread
    with _hls_reaper_guard:
        if _hls_reaper_thread is not None and _hls_reaper_thread.is_alive():
            return
        _hls_reaper_thread = threading.Thread(
            target=_hls_reaper_loop, args=(_hls_reaper_stop,),
            name='hls-encoder-reaper', daemon=True,
        )
        _hls_reaper_thread.start()


def _remove_stale_hls_dirs():
    """Startup cleanup: no encoder survives a restart, so drop partial sessions.

    A directory still marked as encoding, or without a finished playlist, was
    left by an encoder from a previous run. Removing it also makes any orphaned
    ffmpeg from that run fail its next write and exit. Finished sessions stay
    reusable until the normal idle eviction removes them.
    """
    if not os.path.isdir(HLS_CACHE_DIR):
        return
    with _hls_encoders_guard:
        live = set(_hls_encoders)
    for entry in os.listdir(HLS_CACHE_DIR):
        path = os.path.join(HLS_CACHE_DIR, entry)
        if path in live or not os.path.isdir(path):
            continue
        if (
            os.path.isfile(os.path.join(path, '.encoding'))
            or not _is_playlist_complete(os.path.join(path, 'playlist.m3u8'))
        ):
            _remove_hls_dir(path)
    _evict_stale_hls_dirs()


def start_hls_housekeeping():
    """Clean the HLS cache left by a previous run and start the reaper."""
    try:
        _remove_stale_hls_dirs()
    except Exception:
        # A damaged cache must never keep Bazarr+ from starting.
        logger.exception('HLS cache startup cleanup error')
    _ensure_hls_reaper_started()


def _build_hls_ffmpeg_command(
    ffmpeg,
    video_path,
    audio_track_idx,
    start_time_sec,
    cache_dir,
    probe_data,
):
    """Build the ffmpeg command used for one editor HLS session."""
    video_codec = None
    if probe_data:
        for stream in probe_data.get('streams', []):
            if stream.get('codec_type') == 'video':
                video_codec = stream.get('codec_name', '').lower()
                break

    exact_start = start_time_sec > 0
    if exact_start:
        video_args = [
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '28',
            '-g', '48',
            '-keyint_min', '48',
            '-sc_threshold', '0',
        ]
        extra_video_tags = []
    elif video_codec in ('h264', 'avc'):
        video_args = ['-c:v', 'copy']
        extra_video_tags = []
    elif video_codec in ('hevc', 'h265'):
        video_args = ['-c:v', 'copy']
        extra_video_tags = ['-tag:v', 'hvc1']
    else:
        video_args = [
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '28',
            '-g', '48',
            '-keyint_min', '48',
            '-sc_threshold', '0',
        ]
        extra_video_tags = []

    has_audio = False
    if probe_data:
        audio_streams = [
            s for s in probe_data.get('streams', []) if s.get('codec_type') == 'audio'
        ]
        has_audio = audio_track_idx < len(audio_streams)

    if has_audio:
        audio_args = [
            '-map', f'0:a:{audio_track_idx}',
            '-c:a', 'aac',
            '-ac', '2',
            '-b:a', '128k',
        ]
    else:
        audio_args = ['-an']

    pre_input = ['-ss', str(start_time_sec)] if start_time_sec > 0 else []
    return [
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
        # ffmpeg closes the playlist even when it is stopped early, which
        # would make a cut-off session look finished. The waiter closes it.
        '-hls_flags', 'omit_endlist',
        '-hls_segment_type', 'fmp4',
        '-hls_fmp4_init_filename', 'init.mp4',
        '-hls_segment_filename', os.path.join(cache_dir, 'segment_%03d.m4s'),
        '-v', 'error',
        '-y',
        os.path.join(cache_dir, 'playlist.m3u8'),
    ]


def _wait_for_hls_encoder(cache_dir, process, video_path, encoding_marker):
    """Waiter thread: collect ffmpeg's exit and clean up after a natural exit.

    There is no wall-clock timeout. A 4-hour movie at libx264 ultrafast can
    legitimately take hours on slow hardware, and a hard kill would truncate a
    session that is still being watched. The reaper stops encoders that are
    abandoned, and HLS_MAX_ENCODERS bounds how many run at once.
    """
    stderr_data = b''
    try:
        # stdout is DEVNULL, so only the stderr half carries anything.
        _, stderr_data = process.communicate()
    except Exception:
        logger.exception('HLS encoding error for %s', video_path)
        try:
            process.wait()
        except Exception:
            pass
    returncode = process.returncode
    # ffmpeg runs with omit_endlist, so the session is closed here, and only
    # while this encoder is still the registered one: a stopped or replaced
    # encoder's directory belongs to whoever stopped it. A clean exit, or an
    # error that would recur at the same point, ends the session where it is.
    # An outside signal (255 after SIGTERM or SIGINT, or a negative code when
    # killed) leaves it open, so the next request encodes it again.
    interrupted = returncode is None or returncode < 0 or returncode == 255
    playlist = os.path.join(cache_dir, 'playlist.m3u8')
    with _hls_encoders_guard:
        encoder = _hls_encoders.get(cache_dir)
        still_registered = encoder is not None and encoder.process is process
        if still_registered:
            if not interrupted and os.path.isfile(playlist):
                try:
                    with open(playlist, 'a') as f:
                        f.write('#EXT-X-ENDLIST\n')
                except OSError:
                    logger.exception('Failed to close HLS playlist for %s', cache_dir)
            _hls_encoders.pop(cache_dir, None)
    if not still_registered:
        return
    if returncode != 0:
        logger.error(
            'HLS encode failed for %s (rc=%s): %s',
            video_path, returncode,
            (stderr_data or b'').decode(errors='replace')[:500],
        )
    try:
        os.unlink(encoding_marker)
    except OSError:
        pass


def _spawn_hls_encoder(video_path, audio_track_idx, start_time_sec, cache_dir, player=None):
    """Start ffmpeg writing HLS to cache_dir, with a thread waiting on it.

    start_time_sec is applied as an input seek (-ss before -i). Sessions that
    start after zero re-encode video, which lets ffmpeg accurately decode from
    the previous keyframe and discard frames before start_time_sec. That keeps
    the frontend clock, visible video, and subtitle overlay aligned without
    forcing the browser to seek into a non-keyframe fMP4 fragment.

    Idempotent: returns immediately if a complete playlist already exists or
    another thread is currently encoding.
    """
    playlist = os.path.join(cache_dir, 'playlist.m3u8')
    encoding_marker = os.path.join(cache_dir, '.encoding')

    # Reusable cache: a complete playlist (closed with #EXT-X-ENDLIST) and no
    # active encoding marker. Anything else falls through to a fresh spawn.
    if (
        os.path.isfile(playlist)
        and not os.path.isfile(encoding_marker)
        and _is_playlist_complete(playlist)
    ):
        return

    lock = _hls_encoder_lock(cache_dir)
    if not lock.acquire(blocking=False):
        # Another thread is spawning ffmpeg; let it finish.
        return

    try:
        # Re-check inside the lock. A live encoder already owns the session,
        # and one being stopped is about to have its directory deleted. A
        # marker without either was left by an encoder that is gone, so it no
        # longer blocks reuse or a fresh spawn.
        if _hls_encoder_is_live(cache_dir) or _hls_dir_is_stopping(cache_dir):
            return
        if _is_playlist_complete(playlist):
            try:
                os.unlink(encoding_marker)
            except OSError:
                pass
            return

        os.makedirs(cache_dir, exist_ok=True)

        probe_data = _probe_video(video_path)
        ffmpeg = _get_ffmpeg()
        cmd = _build_hls_ffmpeg_command(
            ffmpeg, video_path, audio_track_idx, start_time_sec, cache_dir, probe_data,
        )

        # The marker goes down before ffmpeg starts, so a concurrent request
        # sees the session as encoding rather than spawning a second encoder.
        # It is written only after the probe, ffmpeg lookup and command build
        # succeeded, so a failure there never strands the session behind a
        # stale marker.
        with open(encoding_marker, 'w') as f:
            f.write(str(int(time.time())))
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception:
            try:
                os.unlink(encoding_marker)
            except OSError:
                pass
            raise
        displaced = _register_hls_encoder(cache_dir, process, player)
        try:
            # The waiter starts first so the new ffmpeg's stderr is drained
            # while the displaced encoders are being stopped.
            threading.Thread(
                target=_wait_for_hls_encoder,
                args=(cache_dir, process, video_path, encoding_marker),
                name='hls-encoder-wait',
                daemon=True,
            ).start()
        finally:
            _stop_hls_encoders(displaced)
        _ensure_hls_reaper_started()
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
    'editor/hls/<string:media_type>/<int:media_id>/<int:audio_track>/<string:start_time>/<string:filename>'
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

        startTime is parsed as a float so the frontend can preserve sub-second
        precision (e.g., switching tracks at 30.567s passes through unrounded
        instead of snapping the user-facing clock to a whole second).

        First request to playlist.m3u8 spawns ffmpeg; segments are served as
        ffmpeg writes them. hls.js handles segment fetching, buffer management,
        and seek-back within the cached portion.
        """
        if media_type not in MEDIA_TYPES:
            return MEDIA_TYPE_ERROR, 400
        if not HLS_FILENAME_RE.fullmatch(filename):
            return 'Invalid HLS filename', 400
        if audio_track < 0:
            return 'audioTrack must be >= 0', 400
        try:
            start_time_sec = float(start_time)
        except ValueError:
            return 'startTime must be a number', 400
        if start_time_sec < 0 or not (start_time_sec == start_time_sec):  # rejects NaN
            return 'startTime must be >= 0', 400

        arr_instance_id = _request_arr_instance_id()
        if isinstance(arr_instance_id, tuple):
            return arr_instance_id
        player = request.args.get('player')
        if not player or not HLS_PLAYER_RE.fullmatch(player):
            player = None

        resolved = _resolve_video_path(media_type, media_id, arr_instance_id=arr_instance_id)
        if isinstance(resolved, tuple):
            return resolved
        video_path = resolved
        if not os.path.isfile(video_path):
            return 'Video file not found on disk', 404

        try:
            mtime = os.stat(video_path).st_mtime
        except OSError:
            return 'Video file not accessible', 404

        cache_dir = _hls_cache_dir(media_type, media_id, arr_instance_id, audio_track, start_time_sec, mtime)

        # Defense-in-depth: the filename regex above already blocks path
        # traversal, but we also normalise via realpath (resolves symlinks,
        # collapses ..) and require the result to be strictly inside
        # HLS_CACHE_DIR before any file operation. Reassigning `target` to
        # the normalised path also lets CodeQL prove the path is sanitised
        # at every downstream sink. cache_dir is always a sub-directory of
        # HLS_CACHE_DIR so target never equals the root, which is why the
        # check uses startswith only.
        real_cache_root = os.path.realpath(HLS_CACHE_DIR) + os.sep
        target = os.path.realpath(os.path.join(cache_dir, filename))
        if not target.startswith(real_cache_root):
            return 'Invalid HLS path', 400

        if filename == 'playlist.m3u8':
            # Idle encoders and stale directories are handled by the
            # background reaper, which does not depend on requests arriving.
            try:
                _spawn_hls_encoder(video_path, audio_track, start_time_sec, cache_dir, player=player)
            except Exception:
                logger.exception('Failed to spawn HLS encoder for %s', video_path)
                return 'ffmpeg not available', 500
            _touch_hls_session(cache_dir, player)

            # Wait briefly for ffmpeg to write the first segment so the player
            # gets a useful playlist on the first response.
            _wait_for_first_segment(
                cache_dir, time.time() + HLS_FIRST_SEGMENT_WAIT_SECONDS,
            )

            if not os.path.isfile(target):
                return 'Encoding starting, retry shortly', 503

            # Relative segment and #EXT-X-MAP URLs do not inherit the
            # playlist query string. Carry the validated instance scope so
            # every request resolves the same media and stream cache. Native
            # HLS clients also need query authentication because they cannot
            # inject headers into segment requests.
            resource_query = []
            if arr_instance_id is not None:
                resource_query.append(f'arr_instance_id={arr_instance_id}')
            if player is not None:
                resource_query.append(f'player={player}')
            apikey_query = request.args.get('apikey')
            if apikey_query:
                resource_query.append(f'apikey={quote(apikey_query, safe="")}')
            if resource_query:
                try:
                    with open(target) as f:
                        manifest = f.read()
                except OSError:
                    return 'Encoding starting, retry shortly', 503
                query_string = '&'.join(resource_query)
                rewritten = []
                for line in manifest.splitlines(keepends=True):
                    stripped = line.rstrip('\n').rstrip('\r')
                    if stripped.startswith('#EXT-X-MAP:'):
                        rewritten.append(
                            re.sub(
                                r'URI="([^"?]+)"',
                                f'URI="\\1?{query_string}"',
                                line,
                            )
                        )
                    elif stripped and not stripped.startswith('#'):
                        sep = '\n' if line.endswith('\n') else ''
                        rewritten.append(f'{stripped}?{query_string}{sep}')
                    else:
                        rewritten.append(line)
                response = Response(
                    ''.join(rewritten),
                    mimetype='application/vnd.apple.mpegurl',
                )
            else:
                response = send_file(target, mimetype='application/vnd.apple.mpegurl')
            # Tell clients this is a live-ish playlist that may grow until
            # ENDLIST appears, so they refetch instead of caching it.
            response.headers['Cache-Control'] = 'no-cache'
            return response

        # Init segment or media segment. Any request keeps the session alive.
        _touch_hls_session(cache_dir, player)
        if not os.path.isfile(target) and _hls_segment_miss_needs_encoder(cache_dir):
            # The player still holds this session, but its encoder was
            # stopped or its finished cache evicted while nothing was
            # requested, for example during a long pause. Encode it again
            # instead of answering 404 for the rest of the session.
            try:
                _spawn_hls_encoder(video_path, audio_track, start_time_sec, cache_dir, player=player)
            except Exception:
                logger.exception('Failed to respawn HLS encoder for %s', video_path)
        if not os.path.isfile(target):
            # Segment may not have been written yet by ffmpeg; brief wait.
            deadline = time.time() + HLS_SEGMENT_WAIT_SECONDS
            while time.time() < deadline and not os.path.isfile(target):
                time.sleep(0.1)
            if not os.path.isfile(target):
                return 'Segment not yet available', 404

        mimetype = 'video/mp4' if filename.endswith('.mp4') else 'video/iso.segment'
        return _serve_file_with_ranges(target, mimetype)


@api_ns_editor.route('editor/peaks')
class EditorPeaks(Resource):
    get_pending_model = api_ns_editor.model('EditorPeaksPending', {
        'jobId': fields.Integer(description='Id of the job generating the peaks'),
        'status': fields.String(description='Always "pending"'),
    })

    @authenticate
    @api_ns_editor.response(200, 'Cached waveform peaks')
    @api_ns_editor.response(202, 'Peaks are being generated by a job. Fetch again once it finishes.',
                            get_pending_model)
    @api_ns_editor.response(400, 'Invalid mediaType, mediaId or arr_instance_id')
    @api_ns_editor.response(401, 'Not Authenticated')
    @api_ns_editor.response(404, 'Media or video file not found')
    @api_ns_editor.response(503, 'Waveform generation could not be queued')
    def get(self):
        """Return waveform peaks as JSON for wavesurfer.js, or 202 with the job generating them."""
        resolved = _resolve_or_abort()
        if len(resolved) == 2:
            return resolved

        video_path = resolved[0]

        audio_track = request.args.get('audioTrack', '0')
        try:
            audio_track_idx = max(0, int(audio_track))
        except (ValueError, TypeError):
            audio_track_idx = 0

        # Cached peaks are served at once. Otherwise one job per file and track
        # generates them, and the editor fetches again when that job finishes.
        state, value = request_peaks(video_path, audio_track_idx)
        if state == 'ready':
            return value
        if not value:
            return 'Waveform generation could not be queued', 503
        return {'jobId': value, 'status': 'pending'}, 202


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
        if isinstance(params[0], str) and params[0] not in MEDIA_TYPES:
            return params

        media_type, media_id = params
        arr_instance_id = _request_arr_instance_id()
        if isinstance(arr_instance_id, tuple):
            return arr_instance_id

        if media_type == 'episode':
            row = database.execute(
                scoped(
                    select(TableEpisodes.subtitles).where(TableEpisodes.sonarrEpisodeId == media_id),
                    TableEpisodes.arr_instance_id,
                    arr_instance_id,
                )
            ).first()
        elif media_type == 'sports':
            row = database.execute(
                scoped(
                    select(TableSportsEvents.subtitles).where(TableSportsEvents.id == media_id),
                    TableSportsEvents.arr_instance_id,
                    arr_instance_id,
                )
            ).first()
        else:
            row = database.execute(
                scoped(
                    select(TableMovies.subtitles).where(TableMovies.radarrId == media_id),
                    TableMovies.arr_instance_id,
                    arr_instance_id,
                )
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
                    no_fix_framerate=True, vad=None, job_id=None, output_mode='keep_all', enabled_engines=None,
                    preview_workspace=None):
    """Background sync worker. Called by jobs_queue."""
    from app.jobs_queue import jobs_queue, JobCancelled

    def update_progress(name, value, count):
        _editor_sync_jobs[job_key]['message'] = name
        if job_id:
            jobs_queue.update_job_progress(job_id, progress_value=value, progress_max=count, progress_message=name)

    cleanup_paths = [tmp_in, tmp_out]
    try:
        update_progress('Extracting audio...', 0, 3)
        logger.info('Editor sync starting: video=%s, srt=%s, max_offset=%s, gss=%s, ref=%s, vad=%s, no_fix_framerate=%s',
                     video_path, tmp_in, max_offset, gss, reference, vad, no_fix_framerate)
        from subtitles.tools.subsyncer import SubSyncer
        subsync = SubSyncer()
        if vad:
            subsync.vad = vad
        try:
            update_progress('Running synchronization...', 1, 3)
            sync_result = subsync.sync(
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
                output_mode=output_mode,
                enabled_engines=enabled_engines,
                write_history=False,
            )
        finally:
            del subsync

        logger.info('Editor sync finished, reading result...')
        update_progress('Reading result...', 2, 3)

        if sync_result is not None and getattr(sync_result, 'success', True) is False:
            messages = []
            for result_group in ('failed_results', 'skipped_results'):
                results = getattr(sync_result, result_group, None)
                if not isinstance(results, (list, tuple)):
                    continue
                for engine_result in results:
                    message = getattr(engine_result, 'message', None)
                    engine = getattr(engine_result, 'engine', None)
                    if message and engine:
                        messages.append(f'{engine}: {message}')
                    elif message:
                        messages.append(message)
            raise RuntimeError('; '.join(messages) or 'No synchronization engine produced output')

        engine_results = []
        for engine_result in getattr(sync_result, 'successful_results', []) or []:
            result_path = getattr(engine_result, 'output_path', None)
            engine = getattr(engine_result, 'engine', None)
            if result_path:
                cleanup_paths.append(result_path)
            if not result_path or not engine or not os.path.isfile(result_path):
                continue
            with open(result_path, 'r', encoding=encoding, errors='replace') as f:
                result_content = f.read()
            if result_content.strip():
                engine_results.append({'engine': engine, 'content': result_content})

        if engine_results:
            synced_content = engine_results[0]['content']
        else:
            synced_path = tmp_out
            logger.info('Editor sync result path: %s (exists=%s)', synced_path, os.path.isfile(synced_path))
            if not os.path.isfile(synced_path):
                raise FileNotFoundError(f'Synced subtitle file not found (expected at {synced_path})')
            with open(synced_path, 'r', encoding=encoding, errors='replace') as f:
                synced_content = f.read()

        if not synced_content.strip():
            raise ValueError('Synced subtitle file is empty')

        update_progress('Sync complete', 3, 3)
        completed_job = {'status': 'completed', 'content': synced_content, 'message': 'Sync complete'}
        if engine_results:
            completed_job['results'] = engine_results
        _editor_sync_jobs[job_key] = completed_job

    except JobCancelled:
        _editor_sync_jobs[job_key] = {'status': 'failed', 'content': None, 'message': 'Cancelled by user'}
        raise
    except Exception as e:
        logger.exception('Editor sync failed')
        message = str(e)[:500] or 'Sync failed'
        _editor_sync_jobs[job_key] = {'status': 'failed', 'content': None, 'message': message}
        # The editor reads the outcome from the side store above; the job
        # itself has to fail too, or the Jobs drawer lists it as completed.
        from app.jobs_queue import JobFailed
        raise JobFailed(f'Editor sync failed: {message}') from e
    finally:
        # Clean up in-memory result after 10 minutes
        import threading
        def cleanup():
            _editor_sync_jobs.pop(job_key, None)
            # Clean up temp files only after result has been consumed
            for p in cleanup_paths:
                if p and os.path.isfile(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            # Engine outputs land beside the input, so the workspace goes too.
            discard_preview_workspace(preview_workspace)
        threading.Timer(600, cleanup).start()


@api_ns_editor.route('editor/sync')
class EditorSync(Resource):
    @authenticate
    def post(self):
        """Start syncing editor content. Returns a job key to poll."""
        import tempfile
        import hashlib

        from app.jobs_queue import jobs_queue

        data = request.get_json(silent=True) or {}
        media_type = data.get('mediaType')
        media_id = data.get('mediaId')
        content = data.get('content', '')
        encoding = data.get('encoding', 'utf-8')
        fmt = data.get('format', 'srt')
        language = data.get('language')
        max_offset = str(data.get('maxOffsetSeconds', 120))
        gss = data.get('gss', False)
        reference = data.get('reference', 'a:0')
        no_fix_framerate = data.get('noFixFramerate', True)
        vad = data.get('vad', None)
        output_mode = data.get('outputMode') or data.get('output_mode') or 'keep_all'
        enabled_engines = data.get('enabledEngines') or data.get('enabled_engines')
        if vad and vad not in ('subs_then_webrtc', 'subs_then_auditok', 'webrtc', 'auditok'):
            return 'Invalid vad option', 400

        if not media_type or media_type not in MEDIA_TYPES:
            return MEDIA_TYPE_ERROR, 400
        if not media_id:
            return 'mediaId is required', 400
        if not content:
            return 'content is required', 400
        if is_sync_engine_language_key(language):
            return 'Generated sync output files cannot be synchronized again.', 400

        # Whitelist the format so we never feed attacker-controlled characters
        # (e.g. path-traversal, shell metachars) into the tempfile suffix.
        if fmt not in ('srt', 'vtt', 'ass', 'ssa', 'sub', 'smi', 'mpl', 'txt'):
            return 'Invalid format; must be one of srt, vtt, ass, ssa, sub, smi, mpl, txt', 400

        # The body names the codec its content is written in, and it is also what the
        # worker reads the aligned result back with. An unknown name is the caller's
        # mistake, so it is refused here rather than raising LookupError later, once
        # the temporary files already exist.
        try:
            codecs.lookup(encoding)
        except (LookupError, TypeError):
            return 'Invalid encoding', 400

        try:
            media_id = int(media_id)
        except (ValueError, TypeError):
            return 'mediaId must be an integer', 400

        arr_instance_id = _payload_arr_instance_id(data)
        if isinstance(arr_instance_id, tuple):
            return arr_instance_id

        video_path = _resolve_video_path(media_type, media_id, arr_instance_id=arr_instance_id)
        if isinstance(video_path, tuple):
            return video_path

        if not os.path.isfile(video_path):
            return 'Video file not found', 404

        import threading

        ext = f'.{fmt}'
        # The editor previews an alignment: nothing here is a library subtitle until
        # the user saves the content back. Input and engine outputs share a directory
        # of Bazarr's own, which keeps generated names off the media folder and gives
        # the destinations an owner that does not depend on the video's stem.
        preview_workspace = create_preview_workspace()
        job_key = None
        # Once the workspace exists, only the queued job's cleanup timer removes it,
        # so everything up to a successful hand-off unwinds here instead. Content the
        # named codec cannot represent is the realistic way in.
        try:
            fd, tmp_in = tempfile.mkstemp(suffix=ext, prefix='bazarr_sync_', dir=preview_workspace)
            try:
                os.write(fd, content.encode(encoding))
            finally:
                os.close(fd)
            tmp_out = tmp_in.replace(ext, f'.synced{ext}')

            job_key = f'editor_sync_{hashlib.md5(tmp_in.encode()).hexdigest()[:8]}'
            _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': 'Starting sync...'}

            # Submit to the jobs queue for visibility in Jobs Manager.
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
                    'output_mode': output_mode,
                    'enabled_engines': enabled_engines,
                    'preview_workspace': preview_workspace,
                },
                is_progress=True,
                progress_max=3,
            )
        except BaseException:
            _editor_sync_jobs.pop(job_key, None)
            discard_preview_workspace(preview_workspace)
            raise

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
            response = {'status': 'completed', 'content': content}
            if job.get('results'):
                response['results'] = job['results']
            return response, 200

        # Failed
        msg = job.get('message', 'Unknown error')
        _editor_sync_jobs.pop(job_key, None)
        return {'status': 'failed', 'message': msg}, 200

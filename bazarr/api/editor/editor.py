# coding=utf-8

import json
import logging
import os
import struct
import subprocess

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


def _can_direct_play(video_path, probe_data=None):
    """Check if the video can be served directly to the browser (h264 in mp4/m4v container)."""
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
            if codec in ('h264', 'avc'):
                return True
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
        response.headers['X-Accel-Buffering'] = 'no'
        return response

    response = send_file(file_path, mimetype=mimetype)
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


def _stream_ffmpeg(cmd, mimetype):
    """Run an ffmpeg command and stream its stdout as a Flask Response."""
    def generate():
        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
            process.wait()
            if process.returncode != 0:
                logger.error('ffmpeg exited with code %d for cmd: %s', process.returncode, ' '.join(cmd[:5]))
        except Exception:
            logger.exception('Error during ffmpeg streaming')
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait()

    return Response(
        generate(),
        mimetype=mimetype,
        headers={
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'none',
            'X-Accel-Buffering': 'no',
        },
    )


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


@api_ns_editor.route('editor/video')
class EditorVideo(Resource):
    @authenticate
    def get(self):
        """Stream video as fragmented MP4 for browser playback."""
        resolved = _resolve_or_abort()
        if len(resolved) == 2:
            return resolved

        video_path = resolved[0]

        # If frontend says the browser can play this file natively, serve it directly
        # This covers h264, hevc (on Safari/Edge), vp9 (Chrome), av1, etc.
        if request.args.get('direct') == '1':
            ext = os.path.splitext(video_path)[1].lower()
            mime_map = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska'}
            mime = mime_map.get(ext, 'video/mp4')
            return _serve_file_with_ranges(video_path, mime)

        # Remux or transcode on the fly with ffmpeg
        try:
            ffmpeg = _get_ffmpeg()
        except Exception:
            logger.exception('ffmpeg binary not available')
            return 'ffmpeg not found', 500

        # If frontend says remux=1, the browser can play the video codec natively,
        # just need audio transcoded to AAC. Copy video stream (very fast).
        if request.args.get('remux') == '1':
            video_args = ['-c:v', 'copy']
        else:
            # Check if the video codec can be copied or needs full transcode
            probe_data = _probe_video(video_path)
            video_codec = None
            if probe_data:
                for stream in probe_data.get('streams', []):
                    if stream.get('codec_type') == 'video':
                        video_codec = stream.get('codec_name', '').lower()
                        break

            if video_codec in ('h264', 'avc'):
                video_args = ['-c:v', 'copy']
            else:
                video_args = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-tune', 'fastdecode']

        # Support seeking via ?t= parameter (seconds)
        # Use -ss before -i for fast keyframe seek, then -ss after -i for precise frame
        pre_seek = []
        post_seek = []
        start_time = request.args.get('t')
        if start_time:
            try:
                t = float(start_time)
                if t > 0:
                    # Fast seek to 5s before target, then precise seek to exact frame
                    pre_seek = ['-ss', str(max(0, t - 5))]
                    post_seek = ['-ss', str(min(5, t))]
            except (ValueError, TypeError):
                pass

        audio_track = request.args.get('audioTrack', '0')
        try:
            audio_track_idx = int(audio_track)
        except (ValueError, TypeError):
            audio_track_idx = 0

        # Check if the requested audio track exists
        probe_data = _probe_video(video_path)
        has_audio = False
        if probe_data:
            audio_streams = [s for s in probe_data.get('streams', []) if s.get('codec_type') == 'audio']
            has_audio = audio_track_idx < len(audio_streams)

        audio_args = []
        if has_audio:
            audio_args = ['-map', f'0:a:{audio_track_idx}', '-c:a', 'aac']
        else:
            audio_args = ['-an']

        cmd = [
            ffmpeg,
            *pre_seek,
            '-i', video_path,
            *post_seek,
            '-map', '0:v:0',
            *audio_args,
            *video_args,
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            '-f', 'mp4',
            '-v', 'error',
            'pipe:1',
        ]
        return _stream_ffmpeg(cmd, 'video/mp4')


@api_ns_editor.route('editor/audio')
class EditorAudio(Resource):
    @authenticate
    def get(self):
        """Extract audio track as AAC, cached as file for native seeking."""
        resolved = _resolve_or_abort()
        if len(resolved) == 2:
            return resolved

        video_path = resolved[0]

        import hashlib
        import threading
        audio_track = request.args.get('audioTrack', '0')
        try:
            audio_track_idx = int(audio_track)
        except (ValueError, TypeError):
            audio_track_idx = 0
        file_stat = os.stat(video_path)
        path_hash = hashlib.md5(video_path.encode()).hexdigest()
        audio_cache_dir = os.path.join(PEAKS_CACHE_DIR, 'audio')
        os.makedirs(audio_cache_dir, exist_ok=True)
        track_suffix = f'_t{audio_track_idx}' if audio_track_idx > 0 else ''
        cache_file = os.path.join(audio_cache_dir, f'{path_hash}_{int(file_stat.st_mtime)}{track_suffix}.m4a')
        tmp_file = os.path.join(audio_cache_dir, f'{path_hash}_{int(file_stat.st_mtime)}{track_suffix}.extracting.m4a')

        # Already cached: serve immediately
        if os.path.isfile(cache_file):
            return _serve_file_with_ranges(cache_file, 'audio/mp4')

        # Extraction in progress (tmp file exists): tell frontend to wait
        if os.path.isfile(tmp_file):
            return {'status': 'extracting'}, 202

        # Start extraction in background thread
        def extract():
            try:
                ffmpeg = _get_ffmpeg()
                cmd = [
                    ffmpeg,
                    '-i', video_path,
                    '-vn',
                    '-map', f'0:a:{audio_track_idx}',
                    '-c:a', 'aac',
                    '-b:a', '24k',
                    '-ac', '1',
                    '-ar', '16000',
                    '-movflags', '+faststart',
                    '-v', 'error',
                    '-y',
                    tmp_file,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=600)
                if result.returncode == 0 and os.path.isfile(tmp_file):
                    os.replace(tmp_file, cache_file)
                    logger.info('Audio cache ready: %s (%d bytes)', cache_file, os.path.getsize(cache_file))
                else:
                    stderr_out = result.stderr.decode(errors='replace')[:500]
                    logger.error('Audio extraction failed: %s', stderr_out)
                    try:
                        os.unlink(tmp_file)
                    except OSError:
                        pass
            except Exception:
                logger.exception('Audio extraction error')
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

        threading.Thread(target=extract, daemon=True).start()
        return {'status': 'extracting'}, 202


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


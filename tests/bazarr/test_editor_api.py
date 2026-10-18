# coding=utf-8

"""Unit tests for the subtitle editor backend API (bazarr/api/editor/editor.py).

These tests mock all database queries, file system operations, and subprocess
calls so they run fast and without any external dependencies.
"""

import json
import logging
import os
import struct  # noqa: F401
import sys
import threading
import time
import types
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch, mock_open  # noqa: F401

import pytest
from flask import Blueprint, Flask, g, request

_SYS_BEFORE = dict(sys.modules)


# ---------------------------------------------------------------------------
# We need to mock heavy imports before importing the module under test.
# The editor module pulls in app.database, app.get_args, utilities, etc.
# ---------------------------------------------------------------------------

# Minimal stand-in for app.get_args.args
_mock_args = MagicMock()
_mock_args.config_dir = '/tmp/bazarr_test'

# Pass-through flask_restx stand-ins so the class decorators
# (@api_ns_editor.route(...), @Resource, etc.) do not replace the real
# methods with MagicMocks at import time.
def _passthrough_decorator(*args, **kwargs):
    """Return a decorator that yields the target unchanged."""
    def wrap(target):
        return target
    return wrap


class _FakeNamespace:
    def __init__(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        return _passthrough_decorator()

    def doc(self, *args, **kwargs):
        return _passthrough_decorator()

    def expect(self, *args, **kwargs):
        return _passthrough_decorator()

    def marshal_with(self, *args, **kwargs):
        return _passthrough_decorator()

    def __getattr__(self, name):
        # Any other attribute (model, parser, response, etc.) becomes
        # a harmless MagicMock. Keeps downstream module imports happy
        # without having to enumerate flask_restx's full surface.
        return MagicMock()


class _FakeResource:
    """Minimal Resource base so classes like EditorSync subclass cleanly."""
    pass


_fake_flask_restx = MagicMock()
_fake_flask_restx.Namespace = _FakeNamespace
_fake_flask_restx.Resource = _FakeResource
_fake_flask_restx.fields = MagicMock()

# Patch modules that would otherwise require a running application.
# `init` is patched because importing `bazarr.api.*` triggers
# `bazarr/init.py` which runs `os.environ["SZ_HI_EXTENSION"] = settings.general.hi_extension`
# during import. When `app.config` is mocked, that returns a MagicMock
# and `os.environ.__setitem__` rejects anything that is not a string.
_api_utils_mock = MagicMock()
_api_utils_mock.authenticate = lambda fn: fn
_api_utils_mock.authenticate_header_only = lambda fn: fn
_api_utils_mock.authenticate_header_or_stream_token = lambda fn: fn

_patches = {
    'app.get_args': MagicMock(args=_mock_args),
    'app.config': MagicMock(),
    'app.database': MagicMock(),
    'utilities.path_mappings': MagicMock(),
    'utilities.binaries': MagicMock(),
    'api.utils': _api_utils_mock,
    'api.utils': _api_utils_mock,  # noqa: F601
    'api.subtitles.content': MagicMock(),
    'flask_restx': _fake_flask_restx,
    'init': MagicMock(startTime=0),
}

_preexisting_modules = {mod_name: sys.modules.get(mod_name) for mod_name in _patches}
for mod_name, mock_obj in _patches.items():
    sys.modules[mod_name] = mock_obj

def _ensure_pkg_stub(name, pkg_path):
    mod = types.ModuleType(name)
    mod.__path__ = pkg_path
    mod.__package__ = name
    sys.modules[name] = mod

_bazarr_root = os.path.join(os.path.dirname(__file__), '../../bazarr')
_ensure_pkg_stub('api', [os.path.join(_bazarr_root, 'api')])
_ensure_pkg_stub('api.editor', [os.path.join(_bazarr_root, 'api', 'editor')])
sys.modules.pop('api.editor.editor', None)

# Now safe to import
from api.editor.editor import (  # noqa: E402
    _resolve_video_path,
    _probe_video,
    _validate_params,
    _resolve_or_abort,
    _can_direct_play,
    _parse_range_header,
    _editor_sync_jobs,
    run_editor_sync,
)

# Re-import database and path_mappings as the module sees them
from api.editor import editor as editor_module  # noqa: E402

# The real token module the editor's own imports resolved to, kept alive
# past the stub cleanup below: patching its settings is what moves the
# signing key, because the editor holds the module's functions directly.
_token_module = sys.modules['api.editor.stream_token']

for _k in list(sys.modules):
    if _k not in _SYS_BEFORE:
        del sys.modules[_k]
for _k, _v in _SYS_BEFORE.items():
    sys.modules[_k] = _v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Row = namedtuple('Row', ['path'])
SubRow = namedtuple('SubRow', ['subtitles'])


def _make_probe_json(video_codec='h264', audio_codec='aac',
                     width=1920, height=1080, duration='3600.5',
                     audio_lang='eng', audio_channels=6, video_pix_fmt='yuv420p',
                     video_profile=None):
    """Build a fake ffprobe JSON structure."""
    return {
        'format': {'duration': duration},
        'streams': [
            {
                'codec_type': 'video',
                'codec_name': video_codec,
                'pix_fmt': video_pix_fmt,
                'profile': video_profile,
                'width': width,
                'height': height,
            },
            {
                'codec_type': 'audio',
                'codec_name': audio_codec,
                'channels': audio_channels,
                'tags': {'language': audio_lang, 'title': 'Surround'},
            },
        ],
    }


# ---------------------------------------------------------------------------
# _resolve_video_path
# ---------------------------------------------------------------------------

class TestResolveVideoPath:
    """Tests for _resolve_video_path helper."""

    def test_episode_found(self):
        mock_row = Row(path='/tv/show/s01e01.mkv')
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        with patch.object(editor_module, 'database') as db, \
             patch.object(editor_module, 'path_mappings') as pm:
            db.execute.return_value = mock_result
            pm.path_replace.return_value = '/mapped/tv/show/s01e01.mkv'

            result = _resolve_video_path('episode', 123)
            assert result == '/mapped/tv/show/s01e01.mkv'
            pm.path_replace.assert_called_once_with('/tv/show/s01e01.mkv')

    def test_movie_found(self):
        mock_row = Row(path='/movies/movie.mp4')
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        with patch.object(editor_module, 'database') as db, \
             patch.object(editor_module, 'path_mappings') as pm:
            db.execute.return_value = mock_result
            pm.path_replace_movie.return_value = '/mapped/movies/movie.mp4'

            result = _resolve_video_path('movie', 456)
            assert result == '/mapped/movies/movie.mp4'
            pm.path_replace_movie.assert_called_once_with('/movies/movie.mp4')

    def test_episode_not_found(self):
        mock_result = MagicMock()
        mock_result.first.return_value = None

        with patch.object(editor_module, 'database') as db:
            db.execute.return_value = mock_result
            result = _resolve_video_path('episode', 999)
            assert result == ('Episode not found', 404)

    def test_movie_not_found(self):
        mock_result = MagicMock()
        mock_result.first.return_value = None

        with patch.object(editor_module, 'database') as db:
            db.execute.return_value = mock_result
            result = _resolve_video_path('movie', 999)
            assert result == ('Movie not found', 404)

    def test_invalid_media_type(self):
        result = _resolve_video_path('podcast', 1)
        assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)

    def test_invalid_media_type_empty(self):
        result = _resolve_video_path('', 1)
        assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)


# ---------------------------------------------------------------------------
# _probe_video
# ---------------------------------------------------------------------------

class TestProbeVideo:
    """Tests for _probe_video helper."""

    def test_successful_probe(self):
        probe_data = _make_probe_json()
        mock_completed = MagicMock()
        mock_completed.returncode = 0
        mock_completed.stdout = json.dumps(probe_data).encode()

        with patch.object(editor_module, '_get_ffprobe', return_value='/usr/bin/ffprobe'), \
             patch('subprocess.run', return_value=mock_completed) as mock_run:
            result = _probe_video('/video/test.mkv')

            assert result is not None
            assert result['format']['duration'] == '3600.5'
            assert result['streams'][0]['codec_name'] == 'h264'
            assert result['streams'][1]['codec_name'] == 'aac'

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == '/usr/bin/ffprobe'
            assert '/video/test.mkv' in cmd

    def test_probe_failure_returns_none(self):
        mock_completed = MagicMock()
        mock_completed.returncode = 1
        mock_completed.stderr = b'error: file not found'

        with patch.object(editor_module, '_get_ffprobe', return_value='/usr/bin/ffprobe'), \
             patch('subprocess.run', return_value=mock_completed):
            result = _probe_video('/video/missing.mkv')
            assert result is None

    def test_probe_parses_resolution(self):
        probe_data = _make_probe_json(width=3840, height=2160)
        mock_completed = MagicMock()
        mock_completed.returncode = 0
        mock_completed.stdout = json.dumps(probe_data).encode()

        with patch.object(editor_module, '_get_ffprobe', return_value='/usr/bin/ffprobe'), \
             patch('subprocess.run', return_value=mock_completed):
            result = _probe_video('/video/4k.mkv')
            video_stream = [s for s in result['streams'] if s['codec_type'] == 'video'][0]
            assert video_stream['width'] == 3840
            assert video_stream['height'] == 2160

    def test_probe_parses_multiple_audio_tracks(self):
        probe_data = {
            'format': {'duration': '120.0'},
            'streams': [
                {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080},
                {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
                 'tags': {'language': 'eng', 'title': 'Stereo'}},
                {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6,
                 'tags': {'language': 'hun', 'title': '5.1 Surround'}},
            ],
        }
        mock_completed = MagicMock()
        mock_completed.returncode = 0
        mock_completed.stdout = json.dumps(probe_data).encode()

        with patch.object(editor_module, '_get_ffprobe', return_value='/usr/bin/ffprobe'), \
             patch('subprocess.run', return_value=mock_completed):
            result = _probe_video('/video/multi_audio.mkv')
            audio_streams = [s for s in result['streams'] if s['codec_type'] == 'audio']
            assert len(audio_streams) == 2
            assert audio_streams[0]['tags']['language'] == 'eng'
            assert audio_streams[1]['tags']['language'] == 'hun'


# ---------------------------------------------------------------------------
# _can_direct_play
# ---------------------------------------------------------------------------

class TestCanDirectPlay:
    """Tests for _can_direct_play helper."""

    def test_h264_mp4_is_direct_playable(self):
        probe = _make_probe_json(video_codec='h264')
        with patch.object(editor_module, '_probe_video', return_value=probe):
            assert _can_direct_play('/video/test.mp4', probe_data=probe) is True

    def test_h264_m4v_is_direct_playable(self):
        probe = _make_probe_json(video_codec='h264')
        assert _can_direct_play('/video/test.m4v', probe_data=probe) is True

    def test_mkv_is_not_direct_playable(self):
        probe = _make_probe_json(video_codec='h264')
        assert _can_direct_play('/video/test.mkv', probe_data=probe) is False

    def test_hevc_mp4_is_not_direct_playable(self):
        probe = _make_probe_json(video_codec='hevc')
        assert _can_direct_play('/video/test.mp4', probe_data=probe) is False

    def test_no_probe_data_returns_false(self):
        with patch.object(editor_module, '_probe_video', return_value=None):
            assert _can_direct_play('/video/test.mp4') is False


# ---------------------------------------------------------------------------
# _parse_range_header
# ---------------------------------------------------------------------------

class TestParseRangeHeader:
    """Tests for HTTP Range header parsing."""

    def test_valid_range(self):
        assert _parse_range_header('bytes=0-499', 1000) == (0, 499)

    def test_range_to_end(self):
        assert _parse_range_header('bytes=500-', 1000) == (500, 999)

    def test_range_from_start(self):
        assert _parse_range_header('bytes=0-', 1000) == (0, 999)

    def test_no_header(self):
        assert _parse_range_header(None, 1000) is None

    def test_empty_header(self):
        assert _parse_range_header('', 1000) is None

    def test_invalid_prefix(self):
        assert _parse_range_header('chars=0-499', 1000) is None

    def test_start_beyond_file_size(self):
        assert _parse_range_header('bytes=1000-1500', 1000) is None

    def test_start_greater_than_end(self):
        assert _parse_range_header('bytes=500-100', 1000) is None

    def test_malformed_range(self):
        assert _parse_range_header('bytes=abc-def', 1000) is None


# ---------------------------------------------------------------------------
# HLS command building
# ---------------------------------------------------------------------------

class TestHlsCommandBuilding:
    """Tests for ffmpeg HLS command construction."""

    def test_nonzero_start_transcodes_video_for_frame_accurate_seek(self, tmp_path):
        probe = _make_probe_json(video_codec='hevc', audio_codec='ac3')
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg='/usr/bin/ffmpeg',
            video_path='/video/movie.mkv',
            audio_track_idx=0,
            start_time_sec=3533.35,
            cache_dir=str(tmp_path),
            probe_data=probe,
        )

        assert cmd[:4] == ['/usr/bin/ffmpeg', '-ss', '3533.35', '-i']
        assert _value_after(cmd, '-c:v') == 'libx264'
        assert _value_after(cmd, '-pix_fmt') == 'yuv420p'
        assert '-tag:v' not in cmd

    def test_zero_start_keeps_hevc_stream_copy_for_native(self, tmp_path):
        probe = _make_probe_json(video_codec='hevc', audio_codec='ac3',
                                 video_pix_fmt='yuv420p10le',
                                 video_profile='Main 10')
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg='/usr/bin/ffmpeg',
            video_path='/video/movie.mkv',
            audio_track_idx=0,
            start_time_sec=0,
            cache_dir=str(tmp_path),
            probe_data=probe,
            native_hls=True,
        )

        assert '-ss' not in cmd
        assert _value_after(cmd, '-c:v') == 'copy'
        assert _value_after(cmd, '-tag:v') == 'hvc1'
        assert '-pix_fmt' not in cmd

    def test_zero_start_transcodes_unknown_hevc_profile_for_native(self, tmp_path):
        probe = _make_probe_json(video_codec='hevc', audio_codec='ac3',
                                 video_pix_fmt='yuv420p10le')
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg='/usr/bin/ffmpeg',
            video_path='/video/movie.mkv',
            audio_track_idx=0,
            start_time_sec=0,
            cache_dir=str(tmp_path),
            probe_data=probe,
            native_hls=True,
        )

        assert _value_after(cmd, '-c:v') == 'libx264'
        assert _value_after(cmd, '-pix_fmt') == 'yuv420p'
        assert '-tag:v' not in cmd

    def test_zero_start_transcodes_hevc_for_mse(self, tmp_path):
        probe = _make_probe_json(video_codec='hevc', audio_codec='ac3',
                                 video_pix_fmt='yuv420p10le',
                                 video_profile='Main 10')
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg='/usr/bin/ffmpeg',
            video_path='/video/movie.mkv',
            audio_track_idx=0,
            start_time_sec=0,
            cache_dir=str(tmp_path),
            probe_data=probe,
        )

        assert '-ss' not in cmd
        assert _value_after(cmd, '-c:v') == 'libx264'
        assert _value_after(cmd, '-pix_fmt') == 'yuv420p'
        assert '-tag:v' not in cmd

    def test_zero_start_transcodes_ten_bit_h264(self, tmp_path):
        probe = _make_probe_json(video_codec='h264', audio_codec='aac',
                                 video_pix_fmt='yuv420p10le')
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg='/usr/bin/ffmpeg',
            video_path='/video/movie.mkv',
            audio_track_idx=0,
            start_time_sec=0,
            cache_dir=str(tmp_path),
            probe_data=probe,
        )

        assert _value_after(cmd, '-c:v') == 'libx264'
        assert _value_after(cmd, '-pix_fmt') == 'yuv420p'
        assert '-tag:v' not in cmd

    def test_zero_start_transcodes_h264_with_unknown_pixel_format(self, tmp_path):
        probe = _make_probe_json(video_codec='h264', audio_codec='aac',
                                 video_pix_fmt=None)
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg='/usr/bin/ffmpeg',
            video_path='/video/movie.mkv',
            audio_track_idx=0,
            start_time_sec=0,
            cache_dir=str(tmp_path),
            probe_data=probe,
        )

        assert _value_after(cmd, '-c:v') == 'libx264'
        assert _value_after(cmd, '-pix_fmt') == 'yuv420p'
        assert '-tag:v' not in cmd

    def test_cache_dir_separates_native_and_mse_sessions(self):
        mse = editor_module._hls_cache_dir('movie', 50, None, 0, 0.0, 1)
        native = editor_module._hls_cache_dir('movie', 50, None, 0, 0.0, 1,
                                              native_hls=True)
        assert mse != native


def _value_after(items, key):
    idx = items.index(key)
    return items[idx + 1]


# ---------------------------------------------------------------------------
# _validate_params
# ---------------------------------------------------------------------------

class TestValidateParams:
    """Tests for _validate_params query parameter extraction."""

    def test_valid_episode_params(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': '42'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
            assert result == ('episode', 42)

    def test_valid_movie_params(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'movie', 'mediaId': '7'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
            assert result == ('movie', 7)

    def test_media_type_is_the_canonical_constant(self):
        # A real request carries its own string object. Validation must hand
        # back the MEDIA_TYPES entry instead, so nothing request-derived rides
        # along in the tuple _resolve_or_abort may return.
        request_value = ''.join(['spo', 'rts'])
        assert request_value is not editor_module.MEDIA_TYPES[2]
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': request_value, 'mediaId': '3'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
        assert result == ('sports', 3)
        assert result[0] is editor_module.MEDIA_TYPES[2]

    def test_missing_media_type(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaId': '1'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
            assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)

    def test_invalid_media_type(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'podcast', 'mediaId': '1'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
            assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)

    def test_missing_media_id(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
            assert result == ('mediaId is required', 400)

    def test_non_integer_media_id(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': 'abc'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _validate_params()
            assert result == ('mediaId must be an integer', 400)


# ---------------------------------------------------------------------------
# _resolve_or_abort
# ---------------------------------------------------------------------------

class TestResolveOrAbort:
    """Tests for _resolve_or_abort combining validation and path resolution."""

    def test_successful_resolution(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': '1'}.get(key)

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value='/video/ep.mkv'), \
             patch.object(os.path, 'isfile', return_value=True):
            result = _resolve_or_abort()
            assert result == ('/video/ep.mkv',)

    def test_validation_failure_propagates(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'bad'}.get(key)

        with patch.object(editor_module, 'request', mock_request):
            result = _resolve_or_abort()
            assert len(result) == 2
            assert result[1] == 400

    def test_db_not_found_propagates(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'movie', 'mediaId': '999'}.get(key)

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value=('Movie not found', 404)):
            result = _resolve_or_abort()
            assert result == ('Movie not found', 404)

    def test_file_not_on_disk(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': '1'}.get(key)

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value='/video/missing.mkv'), \
             patch.object(os.path, 'isfile', return_value=False):
            result = _resolve_or_abort()
            assert result == ('Video file not found on disk', 404)


# ---------------------------------------------------------------------------
# run_editor_sync
# ---------------------------------------------------------------------------

class _JobCancelled(Exception):
    """Stands in for app.jobs_queue.JobCancelled, which the worker catches."""


class _JobFailed(Exception):
    """Stands in for app.jobs_queue.JobFailed, which fails the sync's job."""


def _run_expecting_failure(**kwargs):
    """A failed editor sync fails its job as well as the side store."""
    with pytest.raises(_JobFailed) as raised:
        run_editor_sync(**kwargs)
    assert str(raised.value).startswith('Editor sync failed: ')
    return raised.value


class TestRunEditorSync:
    """Tests for the background sync worker function."""

    def setup_method(self):
        _editor_sync_jobs.clear()

    def test_successful_sync(self, tmp_path):
        """Sync completes and stores the synced content."""
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')

        # Write fake input and output files
        with open(tmp_in, 'w') as f:
            f.write('1\n00:00:01,000 --> 00:00:02,000\nOriginal\n')
        synced_content = '1\n00:00:01,500 --> 00:00:02,500\nSynced\n'
        with open(tmp_out, 'w') as f:
            f.write(synced_content)

        job_key = 'test_sync_ok'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):  # prevent cleanup timer

            run_editor_sync(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
            )

        assert _editor_sync_jobs[job_key]['status'] == 'completed'
        assert _editor_sync_jobs[job_key]['content'] == synced_content

    def test_successful_sync_returns_multiple_engine_results(self, tmp_path):
        """Multiple engine outputs are returned for the editor choice prompt."""
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')
        ffsubsync_out = str(tmp_path / 'input.ffsubsync.srt')
        alass_out = str(tmp_path / 'input.alass.srt')

        with open(tmp_in, 'w') as f:
            f.write('original')
        with open(ffsubsync_out, 'w') as f:
            f.write('ffsubsync content')
        with open(alass_out, 'w') as f:
            f.write('alass content')

        sync_result = SimpleNamespace(successful_results=[
            SimpleNamespace(engine='ffsubsync', output_path=ffsubsync_out),
            SimpleNamespace(engine='alass', output_path=alass_out),
        ])

        job_key = 'test_sync_multiple'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync.sync.return_value = sync_result
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):

            run_editor_sync(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
            )

        assert _editor_sync_jobs[job_key]['status'] == 'completed'
        assert _editor_sync_jobs[job_key]['content'] == 'ffsubsync content'
        assert _editor_sync_jobs[job_key]['results'] == [
            {'engine': 'ffsubsync', 'content': 'ffsubsync content'},
            {'engine': 'alass', 'content': 'alass content'},
        ]

    def test_sync_failure_stores_error(self, tmp_path):
        """When SubSyncer raises, the job should be marked as failed."""
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')

        with open(tmp_in, 'w') as f:
            f.write('dummy content')

        job_key = 'test_sync_fail'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync.sync.side_effect = RuntimeError('ffsubsync crashed')
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):
            _run_expecting_failure(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
            )

        assert _editor_sync_jobs[job_key]['status'] == 'failed'
        assert 'ffsubsync crashed' in _editor_sync_jobs[job_key]['message']

    def test_unsuccessful_sync_result_does_not_return_original_content(self, tmp_path):
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')

        original_content = '1\n00:00:01,000 --> 00:00:02,000\nOriginal\n'
        with open(tmp_in, 'w') as f:
            f.write(original_content)

        sync_result = SimpleNamespace(
            success=False,
            successful_results=[],
            failed_results=[SimpleNamespace(engine='alass', message='missing binary')],
            skipped_results=[],
        )

        job_key = 'test_sync_unsuccessful_result'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync.sync.return_value = sync_result
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):
            _run_expecting_failure(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
            )

        assert _editor_sync_jobs[job_key]['status'] == 'failed'
        assert _editor_sync_jobs[job_key]['content'] is None
        assert 'alass: missing binary' in _editor_sync_jobs[job_key]['message']

    def test_sync_without_output_does_not_return_original_content(self, tmp_path):
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')

        original_content = '1\n00:00:01,000 --> 00:00:02,000\nOriginal\n'
        with open(tmp_in, 'w') as f:
            f.write(original_content)

        sync_result = SimpleNamespace(
            success=True,
            successful_results=[],
            failed_results=[],
            skipped_results=[],
        )

        job_key = 'test_sync_no_output'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync.sync.return_value = sync_result
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):
            _run_expecting_failure(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
            )

        assert _editor_sync_jobs[job_key]['status'] == 'failed'
        assert _editor_sync_jobs[job_key]['content'] is None
        assert 'Synced subtitle file not found' in _editor_sync_jobs[job_key]['message']

    def test_sync_updates_progress(self, tmp_path):
        """Progress updates should be sent to the jobs_queue."""
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')

        with open(tmp_in, 'w') as f:
            f.write('1\n00:00:01,000 --> 00:00:02,000\nHello\n')
        with open(tmp_out, 'w') as f:
            f.write('1\n00:00:01,000 --> 00:00:02,000\nHello\n')

        job_key = 'test_sync_progress'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):
            run_editor_sync(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
                job_id='job_123',
            )

        # Should have called update_job_progress multiple times
        assert mock_jobs_queue.update_job_progress.call_count >= 3

    def test_a_cancelled_sync_stays_cancelled(self, tmp_path):
        """Cancellation is re-raised as itself, so the queue files it under
        cancelled rather than turning it into a failure."""
        tmp_in = str(tmp_path / 'input.srt')
        with open(tmp_in, 'w') as f:
            f.write('dummy content')
        job_key = 'test_sync_cancel'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}
        mock_subsync_cls = MagicMock()
        mock_jobs_queue = MagicMock()
        mock_jobs_queue.update_job_progress.side_effect = _JobCancelled('stop')

        with patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):
            with pytest.raises(_JobCancelled):
                run_editor_sync(job_key=job_key, video_path='/video/test.mkv', tmp_in=tmp_in,
                                tmp_out=str(tmp_path / 'input.synced.srt'), encoding='utf-8',
                                max_offset='120', gss=False, reference='a:0', job_id=5)

        assert _editor_sync_jobs[job_key]['status'] == 'failed'
        assert _editor_sync_jobs[job_key]['message'] == 'Cancelled by user'

    def test_sync_empty_output_fails(self, tmp_path):
        """An empty synced file should be treated as a failure."""
        tmp_in = str(tmp_path / 'input.srt')
        tmp_out = str(tmp_path / 'input.synced.srt')

        with open(tmp_in, 'w') as f:
            f.write('1\n00:00:01,000 --> 00:00:02,000\nHello\n')
        # Empty output
        with open(tmp_out, 'w') as f:
            f.write('')

        job_key = 'test_sync_empty'
        _editor_sync_jobs[job_key] = {'status': 'running', 'content': None, 'message': ''}

        mock_subsync = MagicMock()
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with patch('api.editor.editor.SubSyncer', mock_subsync_cls, create=True), \
             patch.dict('sys.modules', {'subtitles.tools.subsyncer': MagicMock(SubSyncer=mock_subsync_cls)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('threading.Timer'):
            _run_expecting_failure(
                job_key=job_key,
                video_path='/video/test.mkv',
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding='utf-8',
                max_offset='120',
                gss=False,
                reference='a:0',
            )

        assert _editor_sync_jobs[job_key]['status'] == 'failed'
        assert 'empty' in _editor_sync_jobs[job_key]['message'].lower()


# ---------------------------------------------------------------------------
# EditorSync GET (job polling)
# ---------------------------------------------------------------------------

class TestEditorSyncGet:
    """Tests for the EditorSync GET endpoint (job status polling)."""

    def setup_method(self):
        _editor_sync_jobs.clear()

    def test_running_job_returns_status(self):
        _editor_sync_jobs['key1'] = {'status': 'running', 'content': None, 'message': 'Extracting audio...'}

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'jobKey': 'key1'}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.get()

        assert result == ({'status': 'running', 'message': 'Extracting audio...'}, 200)

    def test_completed_job_returns_content_and_cleans_up(self):
        _editor_sync_jobs['key2'] = {'status': 'completed', 'content': 'synced srt data', 'message': 'done'}

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'jobKey': 'key2'}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.get()

        body, status = result
        assert status == 200
        assert body['status'] == 'completed'
        assert body['content'] == 'synced srt data'
        # Job should be removed after retrieval
        assert 'key2' not in _editor_sync_jobs

    def test_completed_job_returns_engine_results(self):
        _editor_sync_jobs['key_multi'] = {
            'status': 'completed',
            'content': 'ffsubsync content',
            'message': 'done',
            'results': [
                {'engine': 'ffsubsync', 'content': 'ffsubsync content'},
                {'engine': 'alass', 'content': 'alass content'},
            ],
        }

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'jobKey': 'key_multi'}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.get()

        body, status = result
        assert status == 200
        assert body['status'] == 'completed'
        assert body['results'][1]['engine'] == 'alass'
        assert 'key_multi' not in _editor_sync_jobs

    def test_failed_job_returns_error_and_cleans_up(self):
        _editor_sync_jobs['key3'] = {'status': 'failed', 'content': None, 'message': 'ffsubsync crashed'}

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'jobKey': 'key3'}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.get()

        body, status = result
        assert status == 200
        assert body['status'] == 'failed'
        assert 'ffsubsync crashed' in body['message']
        assert 'key3' not in _editor_sync_jobs

    def test_unknown_job_key_returns_404(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'jobKey': 'nonexistent'}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.get()

        assert result == ({'status': 'not_found'}, 404)

    def test_missing_job_key_returns_404(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: None

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.get()

        assert result == ({'status': 'not_found'}, 404)


# ---------------------------------------------------------------------------
# EditorSync POST (parameter validation)
# ---------------------------------------------------------------------------

class TestEditorSyncPost:
    """Tests for EditorSync POST parameter validation."""

    def _make_post_request(self, data):
        mock_request = MagicMock()
        mock_request.get_json.return_value = data
        return mock_request

    def test_missing_media_type(self):
        mock_request = self._make_post_request({'mediaId': '1', 'content': 'data'})
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)

    def test_invalid_media_type(self):
        mock_request = self._make_post_request({
            'mediaType': 'podcast', 'mediaId': '1', 'content': 'data',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)

    def test_missing_media_id(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode', 'content': 'data',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('mediaId is required', 400)

    def test_missing_content(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode', 'mediaId': '1',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('content is required', 400)

    def test_non_integer_media_id(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode', 'mediaId': 'abc', 'content': 'data',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('mediaId must be an integer', 400)

    def test_invalid_vad_option(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode', 'mediaId': '1', 'content': 'data',
            'vad': 'invalid_vad',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('Invalid vad option', 400)

    def test_rejects_generated_sync_output_language(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode',
            'mediaId': '1',
            'content': 'data',
            'language': 'hu:sync-ffsubsync',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value=('Should not resolve', 404)) as mock_resolve:
            result = sync_resource.post()

        assert result == ('Generated sync output files cannot be synchronized again.', 400)
        mock_resolve.assert_not_called()

    def test_rejects_generated_sync_output_language_with_existing_variant(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode',
            'mediaId': '1',
            'content': 'data',
            'language': 'hu:hi:sync-ffsubsync',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value=('Should not resolve', 404)) as mock_resolve:
            result = sync_resource.post()

        assert result == ('Generated sync output files cannot be synchronized again.', 400)
        mock_resolve.assert_not_called()

    def test_valid_vad_options_accepted(self):
        """All valid VAD options should pass validation (may fail later on missing video)."""
        valid_vads = ['subs_then_webrtc', 'subs_then_auditok', 'webrtc', 'auditok']
        for vad in valid_vads:
            mock_request = self._make_post_request({
                'mediaType': 'episode', 'mediaId': '1', 'content': 'data', 'vad': vad,
            })
            sync_resource = editor_module.EditorSync()
            with patch.object(editor_module, 'request', mock_request), \
                 patch.object(editor_module, '_resolve_video_path', return_value=('Not found', 404)):
                result = sync_resource.post()
            # Should get past VAD validation (fails on resolve instead)
            assert result != ('Invalid vad option', 400), f'vad={vad} was incorrectly rejected'

    def test_unknown_encoding_is_refused_before_any_workspace_exists(self):
        """A bogus codec name is a bad request, not a temporary directory nobody frees."""
        from subtitles.tools import subsync_engines

        mock_request = self._make_post_request({
            'mediaType': 'episode', 'mediaId': '1', 'content': 'data', 'encoding': 'not-a-codec',
        })
        sync_resource = editor_module.EditorSync()
        registered = set(subsync_engines._preview_workspaces)

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path') as mock_resolve:
            result = sync_resource.post()

        assert result == ('Invalid encoding', 400)
        mock_resolve.assert_not_called()
        assert set(subsync_engines._preview_workspaces) == registered

    def test_successful_post_starts_job(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode', 'mediaId': '1', 'content': 'subtitle data',
        })
        sync_resource = editor_module.EditorSync()

        mock_jobs_queue = MagicMock()
        mock_jobs_queue.feed_jobs_pending_queue.return_value = 'queue_id_1'

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value='/video/ep.mkv'), \
             patch.object(os.path, 'isfile', return_value=True), \
             patch('tempfile.mkstemp', return_value=(99, '/tmp/bazarr_sync_xyz.srt')), \
             patch('os.write'), \
             patch('os.close'), \
             patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}), \
             patch('api.editor.editor.jobs_queue', mock_jobs_queue, create=True), \
             patch('threading.Thread') as mock_thread:  # noqa: F841

            # Need to also patch the import inside the method
            with patch.dict('sys.modules', {'app.jobs_queue': MagicMock(jobs_queue=mock_jobs_queue, JobCancelled=_JobCancelled, JobFailed=_JobFailed)}):
                result = sync_resource.post()

            body, status = result
            assert status == 202
            assert body['status'] == 'running'
            assert 'jobKey' in body
            assert body['jobId'] == 'queue_id_1'

    def test_video_not_found_on_disk(self):
        mock_request = self._make_post_request({
            'mediaType': 'episode', 'mediaId': '1', 'content': 'data',
        })
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, '_resolve_video_path', return_value='/video/missing.mkv'), \
             patch.object(os.path, 'isfile', return_value=False):
            result = sync_resource.post()
        assert result == ('Video file not found', 404)

    def test_null_json_body(self):
        """When request body is not valid JSON, get_json returns None."""
        mock_request = MagicMock()
        mock_request.get_json.return_value = None
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, 'request', mock_request):
            result = sync_resource.post()
        assert result == ('mediaType must be one of "episode", "movie", "sports"', 400)


# ---------------------------------------------------------------------------
# EditorInfo GET
# ---------------------------------------------------------------------------

class TestEditorInfo:
    """Tests for EditorInfo GET endpoint."""

    def test_returns_video_metadata(self):
        probe_data = _make_probe_json(
            video_codec='hevc', audio_codec='ac3',
            width=3840, height=2160, duration='7200.0',
            audio_lang='hun', audio_channels=6,
        )

        info_resource = editor_module.EditorInfo()

        with patch.object(editor_module, '_resolve_or_abort', return_value=('/video/test.mkv',)), \
             patch.object(editor_module, '_probe_video', return_value=probe_data):
            result = info_resource.get()

        assert result['duration'] == 7200.0
        assert result['videoCodec'] == 'hevc'
        assert result['audioCodec'] == 'ac3'
        assert result['resolution'] == '3840x2160'
        assert result['container'] == 'mkv'
        assert len(result['audioTracks']) == 1
        assert result['audioTracks'][0]['language'] == 'hun'
        assert result['audioTracks'][0]['channels'] == 6

    def test_probe_failure_returns_500(self):
        info_resource = editor_module.EditorInfo()

        with patch.object(editor_module, '_resolve_or_abort', return_value=('/video/test.mkv',)), \
             patch.object(editor_module, '_probe_video', return_value=None):
            result = info_resource.get()

        assert result == ('Failed to probe video file', 500)

    def test_validation_error_propagates(self):
        info_resource = editor_module.EditorInfo()

        with patch.object(editor_module, '_resolve_or_abort', return_value=('mediaId is required', 400)):
            result = info_resource.get()

        assert result == ('mediaId is required', 400)

    def test_multiple_audio_tracks(self):
        probe_data = {
            'format': {'duration': '100.0'},
            'streams': [
                {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080},
                {'codec_type': 'audio', 'codec_name': 'aac', 'channels': 2,
                 'tags': {'language': 'eng', 'title': 'Stereo'}},
                {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6,
                 'tags': {'language': 'hun', 'title': '5.1'}},
                {'codec_type': 'audio', 'codec_name': 'dts', 'channels': 8,
                 'tags': {'language': 'jpn', 'title': '7.1'}},
            ],
        }

        info_resource = editor_module.EditorInfo()
        with patch.object(editor_module, '_resolve_or_abort', return_value=('/video/test.mkv',)), \
             patch.object(editor_module, '_probe_video', return_value=probe_data):
            result = info_resource.get()

        assert len(result['audioTracks']) == 3
        assert result['audioTracks'][0]['index'] == 0
        assert result['audioTracks'][1]['index'] == 1
        assert result['audioTracks'][2]['index'] == 2
        assert result['audioTracks'][2]['language'] == 'jpn'
        assert result['audioTracks'][2]['channels'] == 8

    def test_no_duration_in_format(self):
        probe_data = {'format': {}, 'streams': [
            {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080},
        ]}

        info_resource = editor_module.EditorInfo()
        with patch.object(editor_module, '_resolve_or_abort', return_value=('/video/test.mkv',)), \
             patch.object(editor_module, '_probe_video', return_value=probe_data):
            result = info_resource.get()

        assert result['duration'] is None


# ---------------------------------------------------------------------------
# EditorSubtitles GET
# ---------------------------------------------------------------------------

class TestEditorSubtitles:
    """Tests for EditorSubtitles GET endpoint."""

    def test_returns_subtitle_list(self):
        subtitles_data = "[['en', '/subs/ep.en.srt'], ['hu', '/subs/ep.hu.ass']]"
        mock_row = SubRow(subtitles=subtitles_data)
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': '1'}.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, 'database') as db:
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert len(result['subtitles']) == 2
        assert result['subtitles'][0] == {'language': 'en', 'format': 'srt'}
        assert result['subtitles'][1] == {'language': 'hu', 'format': 'ass'}

    def test_no_subtitles_returns_empty(self):
        mock_row = SubRow(subtitles=None)
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'movie', 'mediaId': '1'}.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, 'database') as db:
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert result == {'subtitles': []}

    def test_row_not_found_returns_empty(self):
        mock_result = MagicMock()
        mock_result.first.return_value = None

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': '999'}.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, 'database') as db:
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert result == {'subtitles': []}

    def test_malformed_subtitles_string(self):
        mock_row = SubRow(subtitles='not valid python')
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'episode', 'mediaId': '1'}.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with patch.object(editor_module, 'request', mock_request), \
             patch.object(editor_module, 'database') as db:
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert result == {'subtitles': []}

    def test_validation_error_propagates(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {'mediaType': 'bad'}.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with patch.object(editor_module, 'request', mock_request):
            result = subs_resource.get()

        assert result[1] == 400


# ---------------------------------------------------------------------------
# Editor stream tokens (bazarr/api/editor/stream_token.py)
# ---------------------------------------------------------------------------

def _load_standalone(name, path, monkeypatch):
    """Load one real module by file path, outside the stubbed api package.

    The module is registered through `monkeypatch`, so it leaves sys.modules
    when the test ends.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _stream_token_settings(monkeypatch, apikey='token-test-key'):
    settings = SimpleNamespace(auth=SimpleNamespace(apikey=apikey))
    monkeypatch.setattr(_token_module, 'settings', settings)
    return settings


class TestStreamTokenLifetime:
    """The lifetime rule: full duration plus 30 minutes, 1h..12h, 4h unknown."""

    def test_minimum_lifetime_is_one_hour(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        assert _token_module.stream_token_lifetime(600) == 3600
        assert _token_module.stream_token_lifetime(0) == 3600

    def test_lifetime_is_duration_plus_thirty_minutes(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        assert _token_module.stream_token_lifetime(7200) == 9000

    def test_maximum_lifetime_is_twelve_hours(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        assert _token_module.stream_token_lifetime(200000) == 43200

    def test_unknown_duration_is_four_hours(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        assert _token_module.stream_token_lifetime(None) == 14400

    def test_override_shortens_the_lifetime(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        monkeypatch.setenv('BAZARR_EDITOR_STREAM_TOKEN_MAX_SECONDS', '360')
        assert _token_module.stream_token_lifetime(7200) == 360
        monkeypatch.setenv('BAZARR_EDITOR_STREAM_TOKEN_MAX_SECONDS', '180')
        assert _token_module.stream_token_lifetime(7200) == 180

    def test_override_above_the_rule_is_ignored(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        monkeypatch.setenv('BAZARR_EDITOR_STREAM_TOKEN_MAX_SECONDS', '99999')
        assert _token_module.stream_token_lifetime(7200) == 9000

    @pytest.mark.parametrize('value', ['179', '60', '0', '-5', 'abc', '2.5', ''])
    def test_override_below_180_or_not_an_integer_is_ignored(self, monkeypatch, value):
        _stream_token_settings(monkeypatch)
        monkeypatch.setenv('BAZARR_EDITOR_STREAM_TOKEN_MAX_SECONDS', value)
        assert _token_module.stream_token_lifetime(7200) == 9000


class TestStreamTokenMintVerify:
    """Minting, scope binding, expiry and the signing key."""

    def _mint(self, monkeypatch, **kwargs):
        _stream_token_settings(monkeypatch)
        params = {
            'media_type': 'movie', 'media_id': 50, 'arr_instance_id': None,
            'audio_track': 0, 'lifetime_seconds': 3600,
        }
        params.update(kwargs)
        return _token_module.mint_stream_token(**params)

    def test_round_trip_verifies_for_the_minted_scope(self, monkeypatch):
        token = self._mint(monkeypatch)
        assert _token_module.verify_stream_token(token, 'movie', 50, None, 0)

    def test_token_binds_every_scope_field(self, monkeypatch):
        token = self._mint(monkeypatch)
        verify = _token_module.verify_stream_token
        assert not verify(token, 'episode', 50, None, 0)
        assert not verify(token, 'movie', 51, None, 0)
        assert not verify(token, 'movie', 50, 2, 0)
        assert not verify(token, 'movie', 50, None, 1)
        assert not verify(token, 'movie', 50, None, None)

    def test_token_binds_the_arr_instance(self, monkeypatch):
        token = self._mint(monkeypatch, arr_instance_id=2)
        assert _token_module.verify_stream_token(token, 'movie', 50, 2, 0)
        assert not _token_module.verify_stream_token(token, 'movie', 50, None, 0)
        assert not _token_module.verify_stream_token(token, 'movie', 50, 3, 0)

    def test_expired_token_is_rejected(self, monkeypatch):
        token = self._mint(monkeypatch, lifetime_seconds=-10)
        assert not _token_module.verify_stream_token(token, 'movie', 50, None, 0)

    def test_api_key_change_invalidates_every_token(self, monkeypatch):
        token = self._mint(monkeypatch)
        _stream_token_settings(monkeypatch, apikey='rotated-key')
        assert not _token_module.verify_stream_token(token, 'movie', 50, None, 0)

    def test_compat_stream_token_is_rejected(self, monkeypatch):
        import base64
        import hashlib as _hashlib
        import hmac as _hmac
        _stream_token_settings(monkeypatch)
        # A token shaped like the compat endpoint's mint_stream_token output,
        # signed with the compat secret rather than the editor's key.
        payload = {'p': 'provider', 'i': '50', 'exp': int(time.time()) + 300, 't': 's'}
        p_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        sig = _hmac.new(b'compat-secret-' + b'x' * 20, p_bytes, _hashlib.sha256).digest()
        compat_token = base64.urlsafe_b64encode(p_bytes + b'.' + sig).decode().rstrip('=')
        assert not _token_module.verify_stream_token(compat_token, 'movie', 50, None, 0)

    def test_garbage_tokens_are_rejected(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        verify = _token_module.verify_stream_token
        for bad in ['', 'nonsense', '####', 'a.b', 'a' * 200]:
            assert not verify(bad, 'movie', 50, None, 0)

    def test_tampered_payload_is_refused(self, monkeypatch):
        import base64
        token = self._mint(monkeypatch)
        padded = token + ('=' * (-len(token) % 4))
        raw = base64.urlsafe_b64decode(padded.encode())
        payload = json.loads(raw[:-33])
        payload['i'] = 51
        p_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        # The signature is untouched: it still covers the original payload.
        forged = base64.urlsafe_b64encode(p_bytes + raw[-33:]).decode().rstrip('=')
        assert not _token_module.verify_stream_token(forged, 'movie', 50, None, 0)
        assert not _token_module.verify_stream_token(forged, 'movie', 51, None, 0)

    def test_tampered_signature_is_refused(self, monkeypatch):
        import base64
        token = self._mint(monkeypatch)
        padded = token + ('=' * (-len(token) % 4))
        raw = bytearray(base64.urlsafe_b64decode(padded.encode()))
        raw[-1] ^= 0x01
        forged = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('=')
        assert not _token_module.verify_stream_token(forged, 'movie', 50, None, 0)

    def test_same_key_forgery_from_a_fresh_process_is_refused(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        # A second import of the module generates its own per-process
        # secret, so a token it mints under the same API key must not
        # verify with the key of this module's process.
        st = _load_standalone(
            'fresh_stream_token_copy',
            os.path.join(_bazarr_root, 'api', 'editor', 'stream_token.py'),
            monkeypatch,
        )
        st.settings = SimpleNamespace(auth=SimpleNamespace(apikey='token-test-key'))
        token = st.mint_stream_token(media_type='movie', media_id=50, arr_instance_id=None,
                                     audio_track=0, lifetime_seconds=3600)
        assert st.verify_stream_token(token, 'movie', 50, None, 0)
        assert not _token_module.verify_stream_token(token, 'movie', 50, None, 0)

    def test_correctly_signed_token_with_the_wrong_type_tag_is_refused(self, monkeypatch):
        import base64
        import hashlib as _hashlib
        import hmac as _hmac
        _stream_token_settings(monkeypatch)
        # Signed with this process's own key, so only the type tag can fail:
        # a compat endpoint token must not open the editor's HLS route.
        payload = {'k': 's', 'm': 'movie', 'i': 50, 'a': None, 't': 0,
                   'e': int(time.time()) + 300}
        p_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        sig = _hmac.new(_token_module._signing_key(), p_bytes, _hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(p_bytes + b'.' + sig).decode().rstrip('=')
        assert not _token_module.verify_stream_token(token, 'movie', 50, None, 0)

    def test_eight_first_mints_released_together_all_verify(self, monkeypatch):
        _stream_token_settings(monkeypatch)
        # A freshly imported module, so the signing secret is brand new.
        st = _load_standalone(
            'barrier_fresh_stream_token',
            os.path.join(_bazarr_root, 'api', 'editor', 'stream_token.py'),
            monkeypatch,
        )
        st.settings = SimpleNamespace(auth=SimpleNamespace(apikey='barrier-key'))
        workers = 8
        barrier = threading.Barrier(workers)
        results = []
        errors = []

        def mint_one():
            try:
                barrier.wait(timeout=10)
                token = st.mint_stream_token(media_type='movie', media_id=50, arr_instance_id=None,
                                             audio_track=0, lifetime_seconds=3600)
                results.append(st.verify_stream_token(token, 'movie', 50, None, 0))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=mint_one) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert errors == []
        assert results == [True] * workers


class TestEditorStreamTokenRoute:
    """GET /api/editor/stream-token."""

    def _get(self, query, monkeypatch, video_path='/video/test.mkv', probe=None):
        monkeypatch.setattr(editor_module, '_resolve_video_path', lambda *a, **k: video_path)
        if probe is None:
            probe_fn = lambda _path: _make_probe_json(duration='12.3')  # noqa: E731
        elif callable(probe):
            probe_fn = probe
        else:
            probe_fn = lambda _path: probe  # noqa: E731
        monkeypatch.setattr(editor_module, '_probe_video', probe_fn)
        monkeypatch.setattr(os.path, 'isfile', lambda _path: True)
        _stream_token_settings(monkeypatch)
        app = Flask(__name__)
        with app.test_request_context(f'/api/editor/stream-token{query}'):
            return editor_module.EditorStreamToken().get()

    def _headers(self, response):
        return (response.headers.get('Cache-Control'),
                response.headers.get('X-Bazarr-Stream-Token-Route'))

    def test_returns_token_and_lifetime(self, monkeypatch):
        response = self._get('?mediaType=movie&mediaId=50', monkeypatch)
        assert response.status_code == 200
        assert self._headers(response) == ('no-store', '1')
        body = json.loads(response.get_data(as_text=True))
        assert body['lifetime'] == 3600
        assert _token_module.verify_stream_token(body['token'], 'movie', 50, None, 0)

    def test_binds_the_requested_scope(self, monkeypatch):
        response = self._get('?mediaType=episode&mediaId=7&arr_instance_id=2&audioTrack=3', monkeypatch)
        body = json.loads(response.get_data(as_text=True))
        assert _token_module.verify_stream_token(body['token'], 'episode', 7, 2, 3)
        assert not _token_module.verify_stream_token(body['token'], 'episode', 7, None, 3)

    def test_unknown_duration_mints_four_hours(self, monkeypatch):
        probe = {'format': {}, 'streams': []}
        response = self._get('?mediaType=movie&mediaId=50', monkeypatch, probe=probe)
        body = json.loads(response.get_data(as_text=True))
        assert body['lifetime'] == 14400

    @pytest.mark.parametrize('query, message, status', [
        ('', 'mediaType must be one of "episode", "movie", "sports"', 400),
        ('?mediaType=podcast&mediaId=50', 'mediaType must be one of "episode", "movie", "sports"', 400),
        ('?mediaType=movie', 'mediaId is required', 400),
        ('?mediaType=movie&mediaId=abc', 'mediaId must be an integer', 400),
        ('?mediaType=movie&mediaId=50&audioTrack=abc', 'audioTrack must be an integer', 400),
        ('?mediaType=movie&mediaId=50&audioTrack=-1', 'audioTrack must be >= 0', 400),
        ('?mediaType=movie&mediaId=50&arr_instance_id=bad', 'arr_instance_id must be an integer', 400),
    ])
    def test_bad_parameters_carry_the_headers(self, monkeypatch, query, message, status):
        response = self._get(query, monkeypatch)
        assert response.status_code == status
        assert self._headers(response) == ('no-store', '1')
        assert json.loads(response.get_data(as_text=True))['message'] == message

    def test_unresolvable_media_is_refused_with_the_headers(self, monkeypatch):
        response = self._get('?mediaType=movie&mediaId=999', monkeypatch,
                             video_path=('Movie not found', 404))
        assert response.status_code == 404
        assert self._headers(response) == ('no-store', '1')

    def test_missing_video_file_is_refused_with_the_headers(self, monkeypatch):
        monkeypatch.setattr(editor_module, '_resolve_video_path', lambda *a, **k: '/video/missing.mkv')
        monkeypatch.setattr(os.path, 'isfile', lambda _path: False)
        _stream_token_settings(monkeypatch)
        app = Flask(__name__)
        with app.test_request_context('/api/editor/stream-token?mediaType=movie&mediaId=50'):
            response = editor_module.EditorStreamToken().get()
        assert response.status_code == 404
        assert self._headers(response) == ('no-store', '1')

    def test_probe_failure_is_refused_with_the_headers(self, monkeypatch):
        response = self._get('?mediaType=movie&mediaId=50', monkeypatch,
                             probe=lambda _path: None)
        assert response.status_code == 500
        assert self._headers(response) == ('no-store', '1')

    def test_probe_exception_is_refused_with_the_headers(self, monkeypatch):
        def exploding_probe(_path):
            raise RuntimeError('ffprobe crashed')

        response = self._get('?mediaType=movie&mediaId=50', monkeypatch,
                             probe=exploding_probe)
        assert response.status_code == 500
        assert self._headers(response) == ('no-store', '1')
        assert json.loads(response.get_data(as_text=True))['message'] == (
            'Failed to probe video file'
        )


class TestEditorHlsStreamToken:
    """The HLS route accepting st and rewriting segment and map URIs with it."""

    MANIFEST = (
        '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n'
        '#EXT-X-MAP:URI="init.mp4"\n'
        '#EXTINF:4.000000,\nsegment_000000.m4s\n'
        '#EXTINF:4.000000,\nsegment_000001.m4s\n#EXT-X-ENDLIST\n'
    )

    @pytest.fixture
    def hls_env(self, tmp_path, monkeypatch):
        video_path = tmp_path / 'video.mkv'
        video_path.write_bytes(b'video bytes')
        monkeypatch.setattr(editor_module, 'HLS_CACHE_DIR', str(tmp_path / 'hls'))
        for native_hls in (False, True):
            cache_dir = Path(editor_module._hls_cache_dir(
                'movie', 50, None, 0, 0.0, video_path.stat().st_mtime,
                native_hls=native_hls))
            cache_dir.mkdir(parents=True)
            mode = b'native' if native_hls else b'mse'
            (cache_dir / 'playlist.m3u8').write_text(self.MANIFEST)
            (cache_dir / 'init.mp4').write_bytes(b'init ' + mode)
            (cache_dir / 'segment_000000.m4s').write_bytes(b'segment zero ' + mode)
            (cache_dir / 'segment_000001.m4s').write_bytes(b'segment one ' + mode)
        monkeypatch.setattr(editor_module, '_resolve_video_path', lambda *a, **k: str(video_path))
        monkeypatch.setattr(editor_module, '_spawn_hls_encoder', lambda *a, **k: None)
        monkeypatch.setattr(editor_module, '_touch_hls_session', lambda *a, **k: None)
        monkeypatch.setattr(editor_module, '_wait_for_first_segment', lambda *a, **k: True)
        return video_path

    def _get(self, filename, query='', headers=None, monkeypatch=None, hls_env=None):
        app = Flask(__name__)
        url = f'/api/editor/hls/movie/50/0/0.000/{filename}{query}'
        with app.test_request_context(url, headers=headers or {}):
            # The handler echoes only a token the decorator verified; the
            # direct call stands in for that verification here.
            st = request.args.get('st')
            if st:
                g.editor_hls_stream_token = st
            return editor_module.EditorHls().get(
                media_type='movie', media_id=50, audio_track=0,
                start_time='0.000', filename=filename,
            )

    def test_playlist_drops_a_st_the_route_did_not_verify(self, hls_env):
        app = Flask(__name__)
        # A st that merely rode along, malformed at that: the handler was
        # reached with the header, and the playlist must not echo it.
        url = '/api/editor/hls/movie/50/0/0.000/playlist.m3u8?st=abc%5Cq'
        with app.test_request_context(url):
            response = editor_module.EditorHls().get(
                media_type='movie', media_id=50, audio_track=0,
                start_time='0.000', filename='playlist.m3u8',
            )
        assert response.status_code == 200
        # send_file answers in direct passthrough mode; the bytes are read
        # the way the route's own range tests read them.
        body = b''.join(response.response).decode()
        assert 'st=' not in body
        assert 'URI="init.mp4"' in body
        assert 'segment_000000.m4s\n' in body

    def test_playlist_with_st_rewrites_segment_and_map_uris(self, hls_env):
        response = self._get('playlist.m3u8', query='?st=token-one')
        assert response.status_code == 200
        assert response.mimetype == 'application/vnd.apple.mpegurl'
        body = response.get_data(as_text=True)
        assert 'URI="init.mp4?st=token-one"' in body
        assert 'segment_000000.m4s?st=token-one' in body
        assert 'segment_000001.m4s?st=token-one' in body
        assert 'apikey' not in body

    def test_playlist_response_is_never_cacheable(self, hls_env):
        for query in ('', '?st=token-one'):
            response = self._get('playlist.m3u8', query=query)
            assert response.headers['Cache-Control'] == 'no-store'

    def test_segment_200_response_is_never_cacheable(self, hls_env):
        response = self._get('segment_000000.m4s', query='?st=token-one')
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'

    def test_segment_206_response_is_never_cacheable(self, hls_env):
        response = self._get('segment_000000.m4s', query='?st=token-one',
                             headers={'Range': 'bytes=0-3'})
        assert response.status_code == 206
        assert response.headers['Cache-Control'] == 'no-store'
        assert b''.join(response.response) == b'segm'

    def test_segment_bytes_come_from_the_mode_cache(self, hls_env):
        native = self._get('segment_000000.m4s', query='?st=token-one')
        mse = self._get('segment_000000.m4s')
        assert b'native' in b''.join(native.response)
        assert b'mse' in b''.join(mse.response)
        assert b'native' not in b''.join(mse.response)

    def test_init_segment_response_is_never_cacheable(self, hls_env):
        response = self._get('init.mp4', query='?st=token-one')
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-store'


class TestEditorHeaderOnlyAuth:
    """The real decorators, loaded outside the stubbed api package."""

    def _make_auth_app(self, monkeypatch, explode=False):
        utils_path = os.path.join(_bazarr_root, 'api', 'utils.py')
        token_path = os.path.join(_bazarr_root, 'api', 'editor', 'stream_token.py')
        utils_mod = _load_standalone('standalone_api_utils', utils_path, monkeypatch)
        token_mod = _load_standalone('standalone_editor_stream_token', token_path, monkeypatch)
        api_key = 'header-only-key'
        settings = SimpleNamespace(auth=SimpleNamespace(apikey=api_key))
        monkeypatch.setattr(utils_mod, 'settings', settings)
        monkeypatch.setattr(token_mod, 'settings', settings)

        # The decorators import the token module lazily through the api
        # package, so the standalone module is handed to them under the
        # names that import resolves.
        fake_api = types.ModuleType('api')
        fake_editor = types.ModuleType('api.editor')
        fake_editor.stream_token = token_mod
        fake_api.editor = fake_editor
        monkeypatch.setitem(sys.modules, 'api', fake_api)
        monkeypatch.setitem(sys.modules, 'api.editor', fake_editor)

        app = Flask(__name__)
        app.config['TESTING'] = True
        # The production wiring: the api package registers the no-store
        # hook on the api blueprint, so the fixture registers it the same
        # way, through the same registration function, instead of
        # installing the hook by hand.
        api_bp = Blueprint('api', __name__, url_prefix='/api')
        utils_mod.register_editor_no_store(api_bp)
        app.register_blueprint(api_bp)
        if explode:
            # The bare-500 path: the framework builds the answer from the
            # unhandled exception, so no error handler of this test may
            # intercept it. The two routes that explode sit on the same
            # restx Api surface production serves them from: that surface's
            # error handling is the one that builds the 500.
            app.config['PROPAGATE_EXCEPTIONS'] = False
            from flask_restx import Api, Namespace, Resource

            namespace = Namespace('editor')
            Api(app, doc=False).add_namespace(namespace, '/api/')

            @namespace.route('editor/stream-token')
            class StreamTokenRoute(Resource):
                @utils_mod.authenticate_header_only
                def get(self):
                    raise RuntimeError('token route exploded')

            @namespace.route(
                'editor/hls/<string:media_type>/<int:media_id>'
                '/<int:audio_track>/<string:filename>'
            )
            class HlsRoute(Resource):
                @utils_mod.authenticate_header_or_stream_token
                def get(self, media_type, media_id, audio_track, filename):
                    raise RuntimeError('hls route exploded')
        else:
            @app.route('/api/editor/info')
            @utils_mod.authenticate_header_only
            def info():
                return {'ok': True}

            @app.route('/api/editor/sync', methods=['POST'])
            @utils_mod.authenticate_header_only
            def sync():
                return {'ok': True}

            @app.route('/api/editor/stream-token')
            @utils_mod.authenticate_header_only
            def stream_token():
                return {'ok': True}

            @app.route('/api/editor/hls/<string:media_type>/<int:media_id>/<int:audio_track>/<string:filename>')
            @utils_mod.authenticate_header_or_stream_token
            def hls(media_type, media_id, audio_track, filename):
                return {'media': media_type, 'id': media_id, 'track': audio_track}

        return app.test_client(), token_mod, api_key

    @pytest.fixture
    def auth_app(self, monkeypatch):
        return self._make_auth_app(monkeypatch)

    @pytest.fixture
    def exploding_app(self, monkeypatch):
        return self._make_auth_app(monkeypatch, explode=True)

    def test_header_key_passes(self, auth_app):
        client, _, api_key = auth_app
        response = client.get('/api/editor/info', headers={'X-API-KEY': api_key})
        assert response.status_code == 200

    def test_query_key_is_refused_without_a_deprecation_warning(self, auth_app, caplog):
        client, _, api_key = auth_app
        caplog.set_level(logging.WARNING)
        response = client.get('/api/editor/info', query_string={'apikey': api_key})
        assert response.status_code == 401
        assert 'deprecated' not in caplog.text

    def test_form_key_is_refused(self, auth_app, caplog):
        client, _, api_key = auth_app
        caplog.set_level(logging.WARNING)
        response = client.post('/api/editor/sync', data={'apikey': api_key},
                               content_type='application/x-www-form-urlencoded')
        assert response.status_code == 401
        assert 'deprecated' not in caplog.text

    def test_wrong_header_key_is_refused(self, auth_app):
        client, _, _api_key = auth_app
        response = client.get('/api/editor/info', headers={'X-API-KEY': 'wrong'})
        assert response.status_code == 401

    def test_a_valid_header_key_with_a_query_apikey_is_refused(self, auth_app):
        client, _, api_key = auth_app
        # A valid header credential does not buy the key a place in the
        # URL: an apikey in the query is refused before either credential
        # is read, so it can never ride along unrefused.
        response = client.get('/api/editor/stream-token',
                               query_string={'apikey': api_key},
                               headers={'X-API-KEY': api_key})
        assert response.status_code == 401

    def test_a_valid_header_key_with_a_query_apikey_is_refused_on_the_hls_route(self, auth_app):
        client, _, api_key = auth_app
        response = client.get('/api/editor/hls/movie/50/0/playlist.m3u8',
                              query_string={'apikey': api_key},
                              headers={'X-API-KEY': api_key})
        assert response.status_code == 401

    def test_a_valid_stream_token_with_a_query_apikey_is_refused(self, auth_app):
        client, token_mod, api_key = auth_app
        token = token_mod.mint_stream_token(media_type='movie', media_id=50,
                                            arr_instance_id=None, audio_track=0,
                                            lifetime_seconds=3600)
        response = client.get('/api/editor/hls/movie/50/0/playlist.m3u8',
                              query_string={'st': token, 'apikey': api_key})
        assert response.status_code == 401

    def test_unauthorized_token_route_answer_carries_no_store_and_the_marker(self, auth_app):
        client, _, _api_key = auth_app
        response = client.get('/api/editor/stream-token')
        assert response.status_code == 401
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Bazarr-Stream-Token-Route'] == '1'

    def test_valid_stream_token_passes_the_hls_route(self, auth_app):
        client, token_mod, _api_key = auth_app
        token = token_mod.mint_stream_token(media_type='movie', media_id=50,
                                            arr_instance_id=None, audio_track=0,
                                            lifetime_seconds=3600)
        response = client.get(f'/api/editor/hls/movie/50/0/playlist.m3u8?st={token}')
        assert response.status_code == 200
        assert response.get_json() == {'media': 'movie', 'id': 50, 'track': 0}

    def test_invalid_stream_token_is_refused(self, auth_app):
        client, _, _api_key = auth_app
        response = client.get('/api/editor/hls/movie/50/0/playlist.m3u8?st=nonsense')
        assert response.status_code == 401

    def test_expired_stream_token_is_refused(self, auth_app):
        client, token_mod, _api_key = auth_app
        token = token_mod.mint_stream_token(media_type='movie', media_id=50,
                                            arr_instance_id=None, audio_track=0,
                                            lifetime_seconds=-10)
        response = client.get(f'/api/editor/hls/movie/50/0/playlist.m3u8?st={token}')
        assert response.status_code == 401

    def test_stream_token_for_another_scope_is_refused(self, auth_app):
        client, token_mod, _api_key = auth_app
        token = token_mod.mint_stream_token(media_type='episode', media_id=7,
                                            arr_instance_id=None, audio_track=0,
                                            lifetime_seconds=3600)
        response = client.get(f'/api/editor/hls/movie/50/0/playlist.m3u8?st={token}')
        assert response.status_code == 401

    def test_stream_token_for_another_arr_instance_is_refused(self, auth_app):
        client, token_mod, _api_key = auth_app
        token = token_mod.mint_stream_token(media_type='movie', media_id=50,
                                            arr_instance_id=1, audio_track=0,
                                            lifetime_seconds=3600)
        response = client.get(f'/api/editor/hls/movie/50/0/playlist.m3u8?st={token}')
        assert response.status_code == 401
        response = client.get(f'/api/editor/hls/movie/50/0/playlist.m3u8?arr_instance_id=1&st={token}')
        assert response.status_code == 200

    def test_api_key_rotation_refuses_outstanding_tokens(self, auth_app, monkeypatch):
        client, token_mod, _api_key = auth_app
        token = token_mod.mint_stream_token(media_type='movie', media_id=50,
                                            arr_instance_id=None, audio_track=0,
                                            lifetime_seconds=3600)
        # A rotation changes the signing key: the outstanding token is refused.
        token_mod.settings = SimpleNamespace(auth=SimpleNamespace(apikey='rotated'))
        response = client.get(f'/api/editor/hls/movie/50/0/playlist.m3u8?st={token}')
        assert response.status_code == 401

    def test_options_and_unsupported_method_answers_carry_the_headers(self, auth_app):
        # The router builds the automatic OPTIONS answer and the 405 of an
        # unsupported method before any view is dispatched, so the
        # decorators' no-store finalizer never sees them. The blueprint's
        # app-wide hook, registered in the fixture the way production
        # registers it, is what marks those answers, and the token route's
        # router-built answers carry the route marker as well.
        client, _, _api_key = auth_app
        hls_path = '/api/editor/hls/movie/50/0/playlist.m3u8'
        for path in ('/api/editor/stream-token', hls_path):
            response = client.options(path)
            assert response.status_code == 200
            assert response.headers['Cache-Control'] == 'no-store'

            response = client.post(path)
            assert response.status_code == 405
            assert response.headers['Cache-Control'] == 'no-store'

        for method in ('options', 'post'):
            response = getattr(client, method)('/api/editor/stream-token')
            assert response.headers['X-Bazarr-Stream-Token-Route'] == '1'
            response = getattr(client, method)(hls_path)
            assert 'X-Bazarr-Stream-Token-Route' not in response.headers

    def test_an_unhandled_error_in_the_token_route_is_no_store_and_marked(self, exploding_app):
        client, _, api_key = exploding_app
        response = client.get('/api/editor/stream-token', headers={'X-API-KEY': api_key})
        assert response.status_code == 500
        # The 500 the framework builds from the exception is no-store like
        # every other answer of the route, and carries the marker too: the
        # app-wide hook marks whatever the token route answers, whatever
        # built the answer, so the frontend never mistakes this 500 for a
        # reverse proxy answering while Bazarr is down.
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Bazarr-Stream-Token-Route'] == '1'

    def test_an_unhandled_error_in_the_hls_route_is_no_store(self, exploding_app):
        client, _, api_key = exploding_app
        response = client.get('/api/editor/hls/movie/50/0/playlist.m3u8',
                              headers={'X-API-KEY': api_key})
        assert response.status_code == 500
        assert response.headers['Cache-Control'] == 'no-store'
        assert 'X-Bazarr-Stream-Token-Route' not in response.headers


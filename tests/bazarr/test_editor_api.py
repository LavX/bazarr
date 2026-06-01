# coding=utf-8

"""Unit tests for the subtitle editor backend API (bazarr/api/editor/editor.py).

These tests mock all database queries, file system operations, and subprocess
calls so they run fast and without any external dependencies.
"""

import json
import os
import struct  # noqa: F401
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch, mock_open  # noqa: F401

import pytest  # noqa: F401


# ---------------------------------------------------------------------------
# We need to mock heavy imports before importing the module under test.
# The editor module pulls in app.database, app.get_args, utilities, etc.
# ---------------------------------------------------------------------------

# Minimal stand-in for app.get_args.args
_mock_args = MagicMock()
_mock_args.config_dir = "/tmp/bazarr_test"


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

_patches = {
    "app.get_args": MagicMock(args=_mock_args),
    "app.config": MagicMock(),
    "app.database": MagicMock(),
    "utilities.path_mappings": MagicMock(),
    "utilities.binaries": MagicMock(),
    "api.utils": _api_utils_mock,
    "api.utils": _api_utils_mock,  # noqa: F601
    "api.subtitles.content": MagicMock(),
    "flask_restx": _fake_flask_restx,
    "init": MagicMock(startTime=0),
}

import sys  # noqa: E402

_preexisting_modules = {mod_name: sys.modules.get(mod_name) for mod_name in _patches}
for mod_name, mock_obj in _patches.items():
    sys.modules.setdefault(mod_name, mock_obj)

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

for _mod_name in (
    "app.config",
    "app.database",
    "utilities.path_mappings",
    "utilities.binaries",
):
    if _preexisting_modules.get(_mod_name) is None and sys.modules.get(
        _mod_name
    ) is _patches.get(_mod_name):
        sys.modules.pop(_mod_name, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Row = namedtuple("Row", ["path"])
SubRow = namedtuple("SubRow", ["subtitles"])


def _make_probe_json(
    video_codec="h264",
    audio_codec="aac",
    width=1920,
    height=1080,
    duration="3600.5",
    audio_lang="eng",
    audio_channels=6,
):
    """Build a fake ffprobe JSON structure."""
    return {
        "format": {"duration": duration},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": video_codec,
                "width": width,
                "height": height,
            },
            {
                "codec_type": "audio",
                "codec_name": audio_codec,
                "channels": audio_channels,
                "tags": {"language": audio_lang, "title": "Surround"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# _resolve_video_path
# ---------------------------------------------------------------------------


class TestResolveVideoPath:
    """Tests for _resolve_video_path helper."""

    def test_episode_found(self):
        mock_row = Row(path="/tv/show/s01e01.mkv")
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        with (
            patch.object(editor_module, "database") as db,
            patch.object(editor_module, "path_mappings") as pm,
        ):
            db.execute.return_value = mock_result
            pm.path_replace.return_value = "/mapped/tv/show/s01e01.mkv"

            result = _resolve_video_path("episode", 123)
            assert result == "/mapped/tv/show/s01e01.mkv"
            pm.path_replace.assert_called_once_with("/tv/show/s01e01.mkv")

    def test_movie_found(self):
        mock_row = Row(path="/movies/movie.mp4")
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        with (
            patch.object(editor_module, "database") as db,
            patch.object(editor_module, "path_mappings") as pm,
        ):
            db.execute.return_value = mock_result
            pm.path_replace_movie.return_value = "/mapped/movies/movie.mp4"

            result = _resolve_video_path("movie", 456)
            assert result == "/mapped/movies/movie.mp4"
            pm.path_replace_movie.assert_called_once_with("/movies/movie.mp4")

    def test_episode_not_found(self):
        mock_result = MagicMock()
        mock_result.first.return_value = None

        with patch.object(editor_module, "database") as db:
            db.execute.return_value = mock_result
            result = _resolve_video_path("episode", 999)
            assert result == ("Episode not found", 404)

    def test_movie_not_found(self):
        mock_result = MagicMock()
        mock_result.first.return_value = None

        with patch.object(editor_module, "database") as db:
            db.execute.return_value = mock_result
            result = _resolve_video_path("movie", 999)
            assert result == ("Movie not found", 404)

    def test_invalid_media_type(self):
        result = _resolve_video_path("podcast", 1)
        assert result == ('Invalid media type, must be "episode" or "movie"', 400)

    def test_invalid_media_type_empty(self):
        result = _resolve_video_path("", 1)
        assert result == ('Invalid media type, must be "episode" or "movie"', 400)


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

        with (
            patch.object(
                editor_module, "_get_ffprobe", return_value="/usr/bin/ffprobe"
            ),
            patch("subprocess.run", return_value=mock_completed) as mock_run,
        ):
            result = _probe_video("/video/test.mkv")

            assert result is not None
            assert result["format"]["duration"] == "3600.5"
            assert result["streams"][0]["codec_name"] == "h264"
            assert result["streams"][1]["codec_name"] == "aac"

            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "/usr/bin/ffprobe"
            assert "/video/test.mkv" in cmd

    def test_probe_failure_returns_none(self):
        mock_completed = MagicMock()
        mock_completed.returncode = 1
        mock_completed.stderr = b"error: file not found"

        with (
            patch.object(
                editor_module, "_get_ffprobe", return_value="/usr/bin/ffprobe"
            ),
            patch("subprocess.run", return_value=mock_completed),
        ):
            result = _probe_video("/video/missing.mkv")
            assert result is None

    def test_probe_parses_resolution(self):
        probe_data = _make_probe_json(width=3840, height=2160)
        mock_completed = MagicMock()
        mock_completed.returncode = 0
        mock_completed.stdout = json.dumps(probe_data).encode()

        with (
            patch.object(
                editor_module, "_get_ffprobe", return_value="/usr/bin/ffprobe"
            ),
            patch("subprocess.run", return_value=mock_completed),
        ):
            result = _probe_video("/video/4k.mkv")
            video_stream = [s for s in result["streams"] if s["codec_type"] == "video"][
                0
            ]
            assert video_stream["width"] == 3840
            assert video_stream["height"] == 2160

    def test_probe_parses_multiple_audio_tracks(self):
        probe_data = {
            "format": {"duration": "120.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "tags": {"language": "eng", "title": "Stereo"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "tags": {"language": "hun", "title": "5.1 Surround"},
                },
            ],
        }
        mock_completed = MagicMock()
        mock_completed.returncode = 0
        mock_completed.stdout = json.dumps(probe_data).encode()

        with (
            patch.object(
                editor_module, "_get_ffprobe", return_value="/usr/bin/ffprobe"
            ),
            patch("subprocess.run", return_value=mock_completed),
        ):
            result = _probe_video("/video/multi_audio.mkv")
            audio_streams = [s for s in result["streams"] if s["codec_type"] == "audio"]
            assert len(audio_streams) == 2
            assert audio_streams[0]["tags"]["language"] == "eng"
            assert audio_streams[1]["tags"]["language"] == "hun"


# ---------------------------------------------------------------------------
# _can_direct_play
# ---------------------------------------------------------------------------


class TestCanDirectPlay:
    """Tests for _can_direct_play helper."""

    def test_h264_mp4_is_direct_playable(self):
        probe = _make_probe_json(video_codec="h264")
        with patch.object(editor_module, "_probe_video", return_value=probe):
            assert _can_direct_play("/video/test.mp4", probe_data=probe) is True

    def test_h264_m4v_is_direct_playable(self):
        probe = _make_probe_json(video_codec="h264")
        assert _can_direct_play("/video/test.m4v", probe_data=probe) is True

    def test_mkv_is_not_direct_playable(self):
        probe = _make_probe_json(video_codec="h264")
        assert _can_direct_play("/video/test.mkv", probe_data=probe) is False

    def test_hevc_mp4_is_not_direct_playable(self):
        probe = _make_probe_json(video_codec="hevc")
        assert _can_direct_play("/video/test.mp4", probe_data=probe) is False

    def test_no_probe_data_returns_false(self):
        with patch.object(editor_module, "_probe_video", return_value=None):
            assert _can_direct_play("/video/test.mp4") is False


# ---------------------------------------------------------------------------
# _parse_range_header
# ---------------------------------------------------------------------------


class TestParseRangeHeader:
    """Tests for HTTP Range header parsing."""

    def test_valid_range(self):
        assert _parse_range_header("bytes=0-499", 1000) == (0, 499)

    def test_range_to_end(self):
        assert _parse_range_header("bytes=500-", 1000) == (500, 999)

    def test_range_from_start(self):
        assert _parse_range_header("bytes=0-", 1000) == (0, 999)

    def test_no_header(self):
        assert _parse_range_header(None, 1000) is None

    def test_empty_header(self):
        assert _parse_range_header("", 1000) is None

    def test_invalid_prefix(self):
        assert _parse_range_header("chars=0-499", 1000) is None

    def test_start_beyond_file_size(self):
        assert _parse_range_header("bytes=1000-1500", 1000) is None

    def test_start_greater_than_end(self):
        assert _parse_range_header("bytes=500-100", 1000) is None

    def test_malformed_range(self):
        assert _parse_range_header("bytes=abc-def", 1000) is None


# ---------------------------------------------------------------------------
# HLS command building
# ---------------------------------------------------------------------------


class TestHlsCommandBuilding:
    """Tests for ffmpeg HLS command construction."""

    def test_nonzero_start_transcodes_video_for_frame_accurate_seek(self, tmp_path):
        probe = _make_probe_json(video_codec="hevc", audio_codec="ac3")
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg="/usr/bin/ffmpeg",
            video_path="/video/movie.mkv",
            audio_track_idx=0,
            start_time_sec=3533.35,
            cache_dir=str(tmp_path),
            probe_data=probe,
        )

        assert cmd[:4] == ["/usr/bin/ffmpeg", "-ss", "3533.35", "-i"]
        assert _value_after(cmd, "-c:v") == "libx264"
        assert "-tag:v" not in cmd

    def test_zero_start_keeps_hevc_stream_copy(self, tmp_path):
        probe = _make_probe_json(video_codec="hevc", audio_codec="ac3")
        cmd = editor_module._build_hls_ffmpeg_command(
            ffmpeg="/usr/bin/ffmpeg",
            video_path="/video/movie.mkv",
            audio_track_idx=0,
            start_time_sec=0,
            cache_dir=str(tmp_path),
            probe_data=probe,
        )

        assert "-ss" not in cmd
        assert _value_after(cmd, "-c:v") == "copy"
        assert _value_after(cmd, "-tag:v") == "hvc1"


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
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "42",
        }.get(key)

        with patch.object(editor_module, "request", mock_request):
            result = _validate_params()
            assert result == ("episode", 42)

    def test_valid_movie_params(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"mediaType": "movie", "mediaId": "7"}.get(
            key
        )

        with patch.object(editor_module, "request", mock_request):
            result = _validate_params()
            assert result == ("movie", 7)

    def test_missing_media_type(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"mediaId": "1"}.get(key)

        with patch.object(editor_module, "request", mock_request):
            result = _validate_params()
            assert result == ('mediaType must be "episode" or "movie"', 400)

    def test_invalid_media_type(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "podcast",
            "mediaId": "1",
        }.get(key)

        with patch.object(editor_module, "request", mock_request):
            result = _validate_params()
            assert result == ('mediaType must be "episode" or "movie"', 400)

    def test_missing_media_id(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"mediaType": "episode"}.get(key)

        with patch.object(editor_module, "request", mock_request):
            result = _validate_params()
            assert result == ("mediaId is required", 400)

    def test_non_integer_media_id(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "abc",
        }.get(key)

        with patch.object(editor_module, "request", mock_request):
            result = _validate_params()
            assert result == ("mediaId must be an integer", 400)


# ---------------------------------------------------------------------------
# _resolve_or_abort
# ---------------------------------------------------------------------------


class TestResolveOrAbort:
    """Tests for _resolve_or_abort combining validation and path resolution."""

    def test_successful_resolution(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "1",
        }.get(key)

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module, "_resolve_video_path", return_value="/video/ep.mkv"
            ),
            patch.object(os.path, "isfile", return_value=True),
        ):
            result = _resolve_or_abort()
            assert result == ("/video/ep.mkv",)

    def test_validation_failure_propagates(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"mediaType": "bad"}.get(key)

        with patch.object(editor_module, "request", mock_request):
            result = _resolve_or_abort()
            assert len(result) == 2
            assert result[1] == 400

    def test_db_not_found_propagates(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "movie",
            "mediaId": "999",
        }.get(key)

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module,
                "_resolve_video_path",
                return_value=("Movie not found", 404),
            ),
        ):
            result = _resolve_or_abort()
            assert result == ("Movie not found", 404)

    def test_file_not_on_disk(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "1",
        }.get(key)

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module, "_resolve_video_path", return_value="/video/missing.mkv"
            ),
            patch.object(os.path, "isfile", return_value=False),
        ):
            result = _resolve_or_abort()
            assert result == ("Video file not found on disk", 404)


# ---------------------------------------------------------------------------
# run_editor_sync
# ---------------------------------------------------------------------------


class TestRunEditorSync:
    """Tests for the background sync worker function."""

    def setup_method(self):
        _editor_sync_jobs.clear()

    def test_successful_sync(self, tmp_path):
        """Sync completes and stores the synced content."""
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")

        # Write fake input and output files
        with open(tmp_in, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nOriginal\n")
        synced_content = "1\n00:00:01,500 --> 00:00:02,500\nSynced\n"
        with open(tmp_out, "w") as f:
            f.write(synced_content)

        job_key = "test_sync_ok"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):  # prevent cleanup timer
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
            )

        assert _editor_sync_jobs[job_key]["status"] == "completed"
        assert _editor_sync_jobs[job_key]["content"] == synced_content

    def test_successful_sync_returns_multiple_engine_results(self, tmp_path):
        """Multiple engine outputs are returned for the editor choice prompt."""
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")
        ffsubsync_out = str(tmp_path / "input.ffsubsync.srt")
        alass_out = str(tmp_path / "input.alass.srt")

        with open(tmp_in, "w") as f:
            f.write("original")
        with open(ffsubsync_out, "w") as f:
            f.write("ffsubsync content")
        with open(alass_out, "w") as f:
            f.write("alass content")

        sync_result = SimpleNamespace(
            successful_results=[
                SimpleNamespace(engine="ffsubsync", output_path=ffsubsync_out),
                SimpleNamespace(engine="alass", output_path=alass_out),
            ]
        )

        job_key = "test_sync_multiple"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync.sync.return_value = sync_result
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
            )

        assert _editor_sync_jobs[job_key]["status"] == "completed"
        assert _editor_sync_jobs[job_key]["content"] == "ffsubsync content"
        assert _editor_sync_jobs[job_key]["results"] == [
            {"engine": "ffsubsync", "content": "ffsubsync content"},
            {"engine": "alass", "content": "alass content"},
        ]

    def test_sync_failure_stores_error(self, tmp_path):
        """When SubSyncer raises, the job should be marked as failed."""
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")

        with open(tmp_in, "w") as f:
            f.write("dummy content")

        job_key = "test_sync_fail"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync.sync.side_effect = RuntimeError("ffsubsync crashed")
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
            )

        assert _editor_sync_jobs[job_key]["status"] == "failed"
        assert "ffsubsync crashed" in _editor_sync_jobs[job_key]["message"]

    def test_unsuccessful_sync_result_does_not_return_original_content(self, tmp_path):
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")

        original_content = "1\n00:00:01,000 --> 00:00:02,000\nOriginal\n"
        with open(tmp_in, "w") as f:
            f.write(original_content)

        sync_result = SimpleNamespace(
            success=False,
            successful_results=[],
            failed_results=[SimpleNamespace(engine="alass", message="missing binary")],
            skipped_results=[],
        )

        job_key = "test_sync_unsuccessful_result"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync.sync.return_value = sync_result
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
            )

        assert _editor_sync_jobs[job_key]["status"] == "failed"
        assert _editor_sync_jobs[job_key]["content"] is None
        assert "alass: missing binary" in _editor_sync_jobs[job_key]["message"]

    def test_sync_without_output_does_not_return_original_content(self, tmp_path):
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")

        original_content = "1\n00:00:01,000 --> 00:00:02,000\nOriginal\n"
        with open(tmp_in, "w") as f:
            f.write(original_content)

        sync_result = SimpleNamespace(
            success=True,
            successful_results=[],
            failed_results=[],
            skipped_results=[],
        )

        job_key = "test_sync_no_output"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync.sync.return_value = sync_result
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
            )

        assert _editor_sync_jobs[job_key]["status"] == "failed"
        assert _editor_sync_jobs[job_key]["content"] is None
        assert "Synced subtitle file not found" in _editor_sync_jobs[job_key]["message"]

    def test_sync_updates_progress(self, tmp_path):
        """Progress updates should be sent to the jobs_queue."""
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")

        with open(tmp_in, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        with open(tmp_out, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")

        job_key = "test_sync_progress"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
                job_id="job_123",
            )

        # Should have called update_job_progress multiple times
        assert mock_jobs_queue.update_job_progress.call_count >= 3

    def test_sync_empty_output_fails(self, tmp_path):
        """An empty synced file should be treated as a failure."""
        tmp_in = str(tmp_path / "input.srt")
        tmp_out = str(tmp_path / "input.synced.srt")

        with open(tmp_in, "w") as f:
            f.write("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        # Empty output
        with open(tmp_out, "w") as f:
            f.write("")

        job_key = "test_sync_empty"
        _editor_sync_jobs[job_key] = {
            "status": "running",
            "content": None,
            "message": "",
        }

        mock_subsync = MagicMock()
        mock_subsync_cls = MagicMock(return_value=mock_subsync)
        mock_jobs_queue = MagicMock()

        with (
            patch("api.editor.editor.SubSyncer", mock_subsync_cls, create=True),
            patch.dict(
                "sys.modules",
                {"subtitles.tools.subsyncer": MagicMock(SubSyncer=mock_subsync_cls)},
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("threading.Timer"),
        ):
            run_editor_sync(
                job_key=job_key,
                video_path="/video/test.mkv",
                tmp_in=tmp_in,
                tmp_out=tmp_out,
                encoding="utf-8",
                max_offset="120",
                gss=False,
                reference="a:0",
            )

        assert _editor_sync_jobs[job_key]["status"] == "failed"
        assert "empty" in _editor_sync_jobs[job_key]["message"].lower()


# ---------------------------------------------------------------------------
# EditorSync GET (job polling)
# ---------------------------------------------------------------------------


class TestEditorSyncGet:
    """Tests for the EditorSync GET endpoint (job status polling)."""

    def setup_method(self):
        _editor_sync_jobs.clear()

    def test_running_job_returns_status(self):
        _editor_sync_jobs["key1"] = {
            "status": "running",
            "content": None,
            "message": "Extracting audio...",
        }

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"jobKey": "key1"}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.get()

        assert result == ({"status": "running", "message": "Extracting audio..."}, 200)

    def test_completed_job_returns_content_and_cleans_up(self):
        _editor_sync_jobs["key2"] = {
            "status": "completed",
            "content": "synced srt data",
            "message": "done",
        }

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"jobKey": "key2"}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.get()

        body, status = result
        assert status == 200
        assert body["status"] == "completed"
        assert body["content"] == "synced srt data"
        # Job should be removed after retrieval
        assert "key2" not in _editor_sync_jobs

    def test_completed_job_returns_engine_results(self):
        _editor_sync_jobs["key_multi"] = {
            "status": "completed",
            "content": "ffsubsync content",
            "message": "done",
            "results": [
                {"engine": "ffsubsync", "content": "ffsubsync content"},
                {"engine": "alass", "content": "alass content"},
            ],
        }

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"jobKey": "key_multi"}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.get()

        body, status = result
        assert status == 200
        assert body["status"] == "completed"
        assert body["results"][1]["engine"] == "alass"
        assert "key_multi" not in _editor_sync_jobs

    def test_failed_job_returns_error_and_cleans_up(self):
        _editor_sync_jobs["key3"] = {
            "status": "failed",
            "content": None,
            "message": "ffsubsync crashed",
        }

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"jobKey": "key3"}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.get()

        body, status = result
        assert status == 200
        assert body["status"] == "failed"
        assert "ffsubsync crashed" in body["message"]
        assert "key3" not in _editor_sync_jobs

    def test_unknown_job_key_returns_404(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"jobKey": "nonexistent"}.get(key)

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.get()

        assert result == ({"status": "not_found"}, 404)

    def test_missing_job_key_returns_404(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: None

        sync_resource = editor_module.EditorSync()
        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.get()

        assert result == ({"status": "not_found"}, 404)


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
        mock_request = self._make_post_request({"mediaId": "1", "content": "data"})
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ('mediaType must be "episode" or "movie"', 400)

    def test_invalid_media_type(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "podcast",
                "mediaId": "1",
                "content": "data",
            }
        )
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ('mediaType must be "episode" or "movie"', 400)

    def test_missing_media_id(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "content": "data",
            }
        )
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ("mediaId is required", 400)

    def test_missing_content(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "1",
            }
        )
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ("content is required", 400)

    def test_non_integer_media_id(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "abc",
                "content": "data",
            }
        )
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ("mediaId must be an integer", 400)

    def test_invalid_vad_option(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "1",
                "content": "data",
                "vad": "invalid_vad",
            }
        )
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ("Invalid vad option", 400)

    def test_rejects_generated_sync_output_language(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "1",
                "content": "data",
                "language": "hu:sync-ffsubsync",
            }
        )
        sync_resource = editor_module.EditorSync()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module,
                "_resolve_video_path",
                return_value=("Should not resolve", 404),
            ) as mock_resolve,
        ):
            result = sync_resource.post()

        assert result == (
            "Generated sync output files cannot be synchronized again.",
            400,
        )
        mock_resolve.assert_not_called()

    def test_rejects_generated_sync_output_language_with_existing_variant(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "1",
                "content": "data",
                "language": "hu:hi:sync-ffsubsync",
            }
        )
        sync_resource = editor_module.EditorSync()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module,
                "_resolve_video_path",
                return_value=("Should not resolve", 404),
            ) as mock_resolve,
        ):
            result = sync_resource.post()

        assert result == (
            "Generated sync output files cannot be synchronized again.",
            400,
        )
        mock_resolve.assert_not_called()

    def test_valid_vad_options_accepted(self):
        """All valid VAD options should pass validation (may fail later on missing video)."""
        valid_vads = ["subs_then_webrtc", "subs_then_auditok", "webrtc", "auditok"]
        for vad in valid_vads:
            mock_request = self._make_post_request(
                {
                    "mediaType": "episode",
                    "mediaId": "1",
                    "content": "data",
                    "vad": vad,
                }
            )
            sync_resource = editor_module.EditorSync()
            with (
                patch.object(editor_module, "request", mock_request),
                patch.object(
                    editor_module,
                    "_resolve_video_path",
                    return_value=("Not found", 404),
                ),
            ):
                result = sync_resource.post()
            # Should get past VAD validation (fails on resolve instead)
            assert result != ("Invalid vad option", 400), (
                f"vad={vad} was incorrectly rejected"
            )

    def test_successful_post_starts_job(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "1",
                "content": "subtitle data",
            }
        )
        sync_resource = editor_module.EditorSync()

        mock_jobs_queue = MagicMock()
        mock_jobs_queue.feed_jobs_pending_queue.return_value = "queue_id_1"

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module, "_resolve_video_path", return_value="/video/ep.mkv"
            ),
            patch.object(os.path, "isfile", return_value=True),
            patch("tempfile.mkstemp", return_value=(99, "/tmp/bazarr_sync_xyz.srt")),
            patch("os.write"),
            patch("os.close"),
            patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ),
            patch("api.editor.editor.jobs_queue", mock_jobs_queue, create=True),
            patch("threading.Thread") as mock_thread,
        ):  # noqa: F841
            # Need to also patch the import inside the method
            with patch.dict(
                "sys.modules", {"app.jobs_queue": MagicMock(jobs_queue=mock_jobs_queue)}
            ):
                result = sync_resource.post()

            body, status = result
            assert status == 202
            assert body["status"] == "running"
            assert "jobKey" in body

    def test_video_not_found_on_disk(self):
        mock_request = self._make_post_request(
            {
                "mediaType": "episode",
                "mediaId": "1",
                "content": "data",
            }
        )
        sync_resource = editor_module.EditorSync()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(
                editor_module, "_resolve_video_path", return_value="/video/missing.mkv"
            ),
            patch.object(os.path, "isfile", return_value=False),
        ):
            result = sync_resource.post()
        assert result == ("Video file not found", 404)

    def test_null_json_body(self):
        """When request body is not valid JSON, get_json returns None."""
        mock_request = MagicMock()
        mock_request.get_json.return_value = None
        sync_resource = editor_module.EditorSync()

        with patch.object(editor_module, "request", mock_request):
            result = sync_resource.post()
        assert result == ('mediaType must be "episode" or "movie"', 400)


# ---------------------------------------------------------------------------
# EditorInfo GET
# ---------------------------------------------------------------------------


class TestEditorInfo:
    """Tests for EditorInfo GET endpoint."""

    def test_returns_video_metadata(self):
        probe_data = _make_probe_json(
            video_codec="hevc",
            audio_codec="ac3",
            width=3840,
            height=2160,
            duration="7200.0",
            audio_lang="hun",
            audio_channels=6,
        )

        info_resource = editor_module.EditorInfo()

        with (
            patch.object(
                editor_module, "_resolve_or_abort", return_value=("/video/test.mkv",)
            ),
            patch.object(editor_module, "_probe_video", return_value=probe_data),
        ):
            result = info_resource.get()

        assert result["duration"] == 7200.0
        assert result["videoCodec"] == "hevc"
        assert result["audioCodec"] == "ac3"
        assert result["resolution"] == "3840x2160"
        assert result["container"] == "mkv"
        assert len(result["audioTracks"]) == 1
        assert result["audioTracks"][0]["language"] == "hun"
        assert result["audioTracks"][0]["channels"] == 6

    def test_probe_failure_returns_500(self):
        info_resource = editor_module.EditorInfo()

        with (
            patch.object(
                editor_module, "_resolve_or_abort", return_value=("/video/test.mkv",)
            ),
            patch.object(editor_module, "_probe_video", return_value=None),
        ):
            result = info_resource.get()

        assert result == ("Failed to probe video file", 500)

    def test_validation_error_propagates(self):
        info_resource = editor_module.EditorInfo()

        with patch.object(
            editor_module,
            "_resolve_or_abort",
            return_value=("mediaId is required", 400),
        ):
            result = info_resource.get()

        assert result == ("mediaId is required", 400)

    def test_multiple_audio_tracks(self):
        probe_data = {
            "format": {"duration": "100.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "tags": {"language": "eng", "title": "Stereo"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "tags": {"language": "hun", "title": "5.1"},
                },
                {
                    "codec_type": "audio",
                    "codec_name": "dts",
                    "channels": 8,
                    "tags": {"language": "jpn", "title": "7.1"},
                },
            ],
        }

        info_resource = editor_module.EditorInfo()
        with (
            patch.object(
                editor_module, "_resolve_or_abort", return_value=("/video/test.mkv",)
            ),
            patch.object(editor_module, "_probe_video", return_value=probe_data),
        ):
            result = info_resource.get()

        assert len(result["audioTracks"]) == 3
        assert result["audioTracks"][0]["index"] == 0
        assert result["audioTracks"][1]["index"] == 1
        assert result["audioTracks"][2]["index"] == 2
        assert result["audioTracks"][2]["language"] == "jpn"
        assert result["audioTracks"][2]["channels"] == 8

    def test_no_duration_in_format(self):
        probe_data = {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                },
            ],
        }

        info_resource = editor_module.EditorInfo()
        with (
            patch.object(
                editor_module, "_resolve_or_abort", return_value=("/video/test.mkv",)
            ),
            patch.object(editor_module, "_probe_video", return_value=probe_data),
        ):
            result = info_resource.get()

        assert result["duration"] is None


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
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "1",
        }.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(editor_module, "database") as db,
        ):
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert len(result["subtitles"]) == 2
        assert result["subtitles"][0] == {"language": "en", "format": "srt"}
        assert result["subtitles"][1] == {"language": "hu", "format": "ass"}

    def test_no_subtitles_returns_empty(self):
        mock_row = SubRow(subtitles=None)
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"mediaType": "movie", "mediaId": "1"}.get(
            key
        )

        subs_resource = editor_module.EditorSubtitles()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(editor_module, "database") as db,
        ):
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert result == {"subtitles": []}

    def test_row_not_found_returns_empty(self):
        mock_result = MagicMock()
        mock_result.first.return_value = None

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "999",
        }.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(editor_module, "database") as db,
        ):
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert result == {"subtitles": []}

    def test_malformed_subtitles_string(self):
        mock_row = SubRow(subtitles="not valid python")
        mock_result = MagicMock()
        mock_result.first.return_value = mock_row

        mock_request = MagicMock()
        mock_request.args.get = lambda key: {
            "mediaType": "episode",
            "mediaId": "1",
        }.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with (
            patch.object(editor_module, "request", mock_request),
            patch.object(editor_module, "database") as db,
        ):
            db.execute.return_value = mock_result
            result = subs_resource.get()

        assert result == {"subtitles": []}

    def test_validation_error_propagates(self):
        mock_request = MagicMock()
        mock_request.args.get = lambda key: {"mediaType": "bad"}.get(key)

        subs_resource = editor_module.EditorSubtitles()

        with patch.object(editor_module, "request", mock_request):
            result = subs_resource.get()

        assert result[1] == 400

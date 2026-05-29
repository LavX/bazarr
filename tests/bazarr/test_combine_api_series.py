# coding=utf-8

"""Unit tests for the series batch combine REST endpoint.

Pattern matches test_combine_api_episodes.py: patch sys.modules before import,
then call resource methods directly on instantiated objects. No Flask test
client needed.
"""

import sys
import types
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Patch heavy dependencies before importing the module under test.
# Same approach as test_combine_api_episodes.py.
# ---------------------------------------------------------------------------

def _passthrough_decorator(*args, **kwargs):
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

    def response(self, *args, **kwargs):
        return _passthrough_decorator()

    def __getattr__(self, name):
        return MagicMock()


class _FakeResource:
    pass


_fake_flask_restx = MagicMock()
_fake_flask_restx.Namespace = _FakeNamespace
_fake_flask_restx.Resource = _FakeResource
_fake_flask_restx.fields = MagicMock()
_fake_flask_restx.reqparse = MagicMock()

_api_utils_mock = MagicMock()
_api_utils_mock.authenticate = lambda fn: fn

_patches = {
    'flask_restx': _fake_flask_restx,
    'app.get_args': MagicMock(args=MagicMock(config_dir='/tmp/bazarr_test')),
    'app.config': MagicMock(),
    'app.database': MagicMock(),
    'app.event_handler': MagicMock(),
    'app.jobs_queue': MagicMock(),
    'utilities.path_mappings': MagicMock(),
    'utilities.binaries': MagicMock(),
    'api.utils': _api_utils_mock,
    'api.swaggerui': MagicMock(),
    'subliminal_patch.core': MagicMock(SUBTITLE_EXTENSIONS=['.srt', '.ass']),
    'subtitles.upload': MagicMock(),
    'subtitles.mass_download': MagicMock(),
    'subtitles.mass_download.series': MagicMock(),
    'subtitles.download': MagicMock(),
    'subtitles.tools.delete': MagicMock(),
    'subtitles.tools.combine': MagicMock(),
    'subtitles.tools.combine.main': MagicMock(),
    'subtitles.indexer.series': MagicMock(),
    'subtitles.wanted': MagicMock(),
    'sonarr.sync.series': MagicMock(),
    'sqlalchemy': MagicMock(),
    'sqlalchemy.exc': MagicMock(),
    'init': MagicMock(startTime=0),
    'flask': MagicMock(),
    'werkzeug.datastructures': MagicMock(),
}

_preexisting = {k: sys.modules.get(k) for k in _patches}
for _mod, _obj in _patches.items():
    # Force-install (not setdefault) so that even when an earlier test loaded
    # the real api.utils etc., the mock replaces them at the critical moment
    # the module under test imports its decorators.
    sys.modules[_mod] = _obj

# Stub the `api` package itself so api/__init__.py (which imports badges, badges
# imports signalr_client, etc.) never runs.  The sub-package api.series must also
# be stubbed so Python resolves `api.series.series` without executing any
# intermediate __init__ files.
def _ensure_pkg_stub(name, pkg_path=None):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        mod.__path__ = pkg_path or []
        mod.__package__ = name
        sys.modules[name] = mod

# Stub the `api` package itself so api/__init__.py never runs.
# Provide the real paths so submodule file lookup still works.
import os as _os
_bazarr_root = _os.path.join(_os.path.dirname(__file__), '../../bazarr')
_ensure_pkg_stub('api', [_os.path.join(_bazarr_root, 'api')])
_ensure_pkg_stub('api.series', [_os.path.join(_bazarr_root, 'api', 'series')])

# Drop any cached api.series.series so the re-import below resolves
# `authenticate` against the mocked api.utils, not the real one loaded
# eagerly via api/__init__.py during an earlier test.
sys.modules.pop('api.series.series', None)

import api.series.series as series_module  # noqa: E402

# Restore sys.modules: pop entries we added, put back originals we replaced.
# This lets later test files re-resolve real modules.
for _mod in _patches:
    _orig = _preexisting.get(_mod)
    if _orig is None:
        sys.modules.pop(_mod, None)
    else:
        sys.modules[_mod] = _orig


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@patch.object(series_module, '_list_series_episodes')
@patch.object(series_module, 'try_combine_for_video')
@patch.object(series_module, 'path_mappings')
def test_series_batch_combine(mock_paths, mock_combine, mock_list):
    mock_paths.path_replace.side_effect = lambda p: p
    mock_list.return_value = [
        {'sonarrEpisodeId': 1, 'path': '/tv/Show/S01E01.mkv', 'sonarrSeriesId': 5},
        {'sonarrEpisodeId': 2, 'path': '/tv/Show/S01E02.mkv', 'sonarrSeriesId': 5},
        {'sonarrEpisodeId': 3, 'path': '/tv/Show/S01E03.mkv', 'sonarrSeriesId': 5},
    ]
    mock_combine.side_effect = [
        type('R', (), {'status': 'built', 'path': '/x1.srt',
                       'alignment': 'ok', 'error': '', 'reason': ''})(),
        type('R', (), {'status': 'skipped', 'path': '',
                       'reason': 'missing source', 'alignment': '', 'error': ''})(),
        type('R', (), {'status': 'built', 'path': '/x3.srt',
                       'alignment': 'ok', 'error': '', 'reason': ''})(),
    ]
    resource = series_module.SeriesSubtitlesCombine()
    with patch.object(series_module, 'request') as mock_request:
        mock_request.get_json.return_value = {}
        body, status = resource.post(5)
    assert status == 200
    assert body['status'] == 'batch_complete'
    assert body['built'] == 2
    assert body['skipped'] == 1
    assert body['failed'] == 0
    assert len(body['details']) == 3


@patch.object(series_module, '_list_series_episodes')
def test_series_combine_not_found(mock_list):
    mock_list.return_value = []
    resource = series_module.SeriesSubtitlesCombine()
    with patch.object(series_module, 'request') as mock_request:
        mock_request.get_json.return_value = {}
        body, status = resource.post(5)
    assert status == 404
    assert body['status'] == 'not_found'

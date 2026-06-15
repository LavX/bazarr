# coding=utf-8

"""Unit tests for the series batch combine REST endpoint.

Pattern matches test_combine_api_episodes.py: patch sys.modules before import,
then call resource methods directly on instantiated objects. No Flask test
client needed.
"""

import sys
import types
from unittest.mock import MagicMock, patch

# Snapshot sys.modules so we can fully restore it after importing the module
# under test, preventing mock/transitive-import leakage into later test files.
_SYS_BEFORE = dict(sys.modules)


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
    'app.get_providers': MagicMock(),
    'app.jobs_queue': MagicMock(),
    'app.scheduler': MagicMock(),
    'app.signalr_client': MagicMock(),
    'utilities.path_mappings': MagicMock(),
    'utilities.binaries': MagicMock(),
    'api.utils': _api_utils_mock,
    'api.swaggerui': MagicMock(),
    'subliminal_patch': MagicMock(),
    'subliminal_patch.core': MagicMock(SUBTITLE_EXTENSIONS=['.srt', '.ass']),
    'subliminal_patch.core_persistent': MagicMock(),
    'subliminal_patch.exceptions': MagicMock(),
    'subliminal_patch.extensions': MagicMock(),
    'subliminal_patch.score': MagicMock(MAX_SCORES={'movie': 100, 'episode': 100}),
    'subliminal_patch.subtitle': MagicMock(),
    'subtitles.indexer.movies': MagicMock(),
    'subtitles.manual': MagicMock(),
    'subtitles.upload': MagicMock(),
    'subtitles.mass_download': MagicMock(),
    'subtitles.mass_download.series': MagicMock(),
    'subtitles.download': MagicMock(),
    'subtitles.tools.delete': MagicMock(),
    'subtitles.tools.combine': MagicMock(),
    'subtitles.tools.combine.main': MagicMock(),
    'subtitles.indexer.series': MagicMock(),
    'subtitles.upgrade': MagicMock(),
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
import os as _os  # noqa: E402
_bazarr_root = _os.path.join(_os.path.dirname(__file__), '../../bazarr')
_ensure_pkg_stub('api', [_os.path.join(_bazarr_root, 'api')])
_ensure_pkg_stub('api.series', [_os.path.join(_bazarr_root, 'api', 'series')])

# Drop any cached api.series.series so the re-import below resolves
# `authenticate` against the mocked api.utils, not the real one loaded
# eagerly via api/__init__.py during an earlier test.
sys.modules.pop('api.series.series', None)

import api.series.series as series_module  # noqa: E402

# Fully restore sys.modules to its pre-import state: drop everything this module
# added (mocks + transitive imports) and put back any originals we replaced.
for _k in list(sys.modules):
    if _k not in _SYS_BEFORE:
        del sys.modules[_k]
for _k, _v in _SYS_BEFORE.items():
    sys.modules[_k] = _v


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
        mock_request.args.get.return_value = 8
        body, status = resource.post(5)
    assert status == 200
    assert body['status'] == 'batch_complete'
    assert body['built'] == 2
    assert body['skipped'] == 1
    assert body['failed'] == 0
    assert len(body['details']) == 3
    mock_list.assert_called_once_with(5, arr_instance_id=8)


@patch.object(series_module, '_list_series_episodes')
def test_series_combine_not_found(mock_list):
    mock_list.return_value = []
    resource = series_module.SeriesSubtitlesCombine()
    with patch.object(series_module, 'request') as mock_request:
        mock_request.get_json.return_value = {}
        mock_request.args.get.return_value = None
        body, status = resource.post(5)
    assert status == 404
    assert body['status'] == 'not_found'

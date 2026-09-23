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
from tests.bazarr import _module_isolation

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
_module_isolation.restore(_SYS_BEFORE)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@patch.object(series_module, '_list_series_episodes')
@patch.object(series_module, 'jobs_queue')
@patch.object(series_module, 'database')
def test_series_combine_queues_one_job(mock_db, mock_jobs, mock_list):
    """The route no longer loops the episodes in the request: it queues one job
    for the series and answers with its id."""
    mock_list.return_value = [
        {'sonarrEpisodeId': 1, 'path': '/tv/Show/S01E01.mkv', 'sonarrSeriesId': 5, 'arr_instance_id': 7},
        {'sonarrEpisodeId': 2, 'path': '/tv/Show/S01E02.mkv', 'sonarrSeriesId': 5, 'arr_instance_id': 7},
    ]
    mock_db.execute.return_value.first.return_value = type('Row', (), {'title': 'Show'})()
    mock_jobs.feed_jobs_pending_queue.return_value = 31
    resource = series_module.SeriesSubtitlesCombine()
    with patch.object(series_module, 'request') as mock_request:
        mock_request.get_json.return_value = {'languages': ['en', 'hu'], 'format': 'ass'}
        mock_request.args.get.return_value = 8
        body, status = resource.post(5)
    assert status == 202
    assert body == {'status': 'queued', 'job_id': 31}
    mock_list.assert_called_once_with(5, arr_instance_id=8)
    kwargs = mock_jobs.feed_jobs_pending_queue.call_args.kwargs
    assert kwargs['job_name'] == 'Combining subtitles for Show'
    assert kwargs['module'] == 'subtitles.tools.combine.batch'
    assert kwargs['func'] == 'combine_series_subtitles'
    assert kwargs['kwargs'] == {'series_id': 5, 'languages': ['en', 'hu'], 'format': 'ass',
                                'arr_instance_id': 8}
    assert kwargs['is_progress'] is True
    assert kwargs['progress_max'] == 2


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

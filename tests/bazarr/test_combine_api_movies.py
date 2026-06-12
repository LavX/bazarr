# coding=utf-8

"""Unit tests for the movie combine REST endpoint.

Pattern matches test_editor_api.py: patch sys.modules before import, then call
resource methods directly on instantiated objects. No Flask test client needed.
"""

import sys
from unittest.mock import MagicMock, patch

# Snapshot sys.modules so we can fully restore it after importing the module
# under test. Without this, the mocks and transitively-imported modules leak
# into later test files (e.g. test_editor_api) and break them.
_SYS_BEFORE = dict(sys.modules)


# ---------------------------------------------------------------------------
# Patch heavy dependencies before importing the module under test.
# Same approach as test_editor_api.py.
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
    'subliminal_patch': MagicMock(),
    'subliminal_patch.core': MagicMock(SUBTITLE_EXTENSIONS=['.srt', '.ass']),
    'subliminal_patch.core_persistent': MagicMock(),
    'subliminal_patch.exceptions': MagicMock(),
    'subliminal_patch.extensions': MagicMock(),
    'subliminal_patch.score': MagicMock(MAX_SCORES={'movie': 100, 'episode': 100}),
    'subliminal_patch.subtitle': MagicMock(),
    'subtitles.indexer.movies': MagicMock(),
    'subtitles.indexer.series': MagicMock(),
    'subtitles.manual': MagicMock(),
    'subtitles.upload': MagicMock(),
    'subtitles.mass_download.movies': MagicMock(),
    'subtitles.upgrade': MagicMock(),
    'subtitles.download': MagicMock(),
    'subtitles.tools.delete': MagicMock(),
    'subtitles.tools.combine': MagicMock(),
    'subtitles.tools.combine.main': MagicMock(),
    'init': MagicMock(startTime=0),
    'flask': MagicMock(),
}

_preexisting = {k: sys.modules.get(k) for k in _patches}
for _mod, _obj in _patches.items():
    # Force-install (not setdefault) so that even when an earlier test loaded
    # the real api.utils etc., the mock replaces them at the critical moment
    # the module under test imports its decorators.
    sys.modules[_mod] = _obj

# Drop any cached api.movies.movies_subtitles so the re-import below
# resolves `authenticate` against the mocked api.utils, not the real one
# loaded eagerly via api/__init__.py during an earlier test.
sys.modules.pop('api.movies.movies_subtitles', None)

import api.movies.movies_subtitles as movies_subtitles_module  # noqa: E402

# Fully restore sys.modules to its pre-import state: drop everything this module
# added (mocks + transitive imports) and put back any originals we replaced.
# The captured `movies_subtitles_module` reference stays valid for our tests.
for _k in list(sys.modules):
    if _k not in _SYS_BEFORE:
        del sys.modules[_k]
for _k, _v in _SYS_BEFORE.items():
    sys.modules[_k] = _v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(status, path="", alignment="", reason="", error=""):
    return type("CombineResult", (), {
        "status": status,
        "path": path,
        "alignment": alignment,
        "reason": reason,
        "error": error,
    })()


def _make_db_row(path="/movies/Movie.mkv"):
    return type("Row", (), {"path": path})()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMoviesSubtitlesCombinePost:

    def _make_request(self, payload):
        mock_request = MagicMock()
        mock_request.get_json.return_value = payload
        mock_request.args.get.return_value = None
        return mock_request

    def test_post_combine_uses_profile_rule(self):
        """Empty payload: languages and format should both be None (profile-driven)."""
        resource = movies_subtitles_module.MoviesSubtitlesCombine()

        mock_db_result = MagicMock()
        mock_db_result.first.return_value = _make_db_row()

        with patch.object(movies_subtitles_module, 'database') as mock_db, \
             patch.object(movies_subtitles_module, 'path_mappings') as mock_pm, \
             patch.object(movies_subtitles_module, 'try_combine_for_video') as mock_combine, \
             patch.object(movies_subtitles_module, 'request', self._make_request({})):

            mock_db.execute.return_value = mock_db_result
            mock_pm.path_replace_movie.return_value = '/mapped/movies/Movie.mkv'
            mock_combine.return_value = _make_result(
                status='built',
                path='/movies/Movie.en.combined-hu.srt',
                alignment='ok',
            )

            body, status_code = resource.post(radarr_id=42)

        assert status_code == 200
        assert body['status'] == 'built'
        assert body['path'].endswith('combined-hu.srt')
        mock_combine.assert_called_once()
        kwargs = mock_combine.call_args.kwargs
        assert kwargs['languages'] is None
        assert kwargs['format'] is None

    def test_post_combine_with_explicit_languages(self):
        """Explicit languages and format are forwarded to try_combine_for_video."""
        resource = movies_subtitles_module.MoviesSubtitlesCombine()

        mock_db_result = MagicMock()
        mock_db_result.first.return_value = _make_db_row()

        payload = {'languages': ['en', 'hu'], 'format': 'ass'}

        with patch.object(movies_subtitles_module, 'database') as mock_db, \
             patch.object(movies_subtitles_module, 'path_mappings') as mock_pm, \
             patch.object(movies_subtitles_module, 'try_combine_for_video') as mock_combine, \
             patch.object(movies_subtitles_module, 'request', self._make_request(payload)):

            mock_db.execute.return_value = mock_db_result
            mock_pm.path_replace_movie.return_value = '/mapped/movies/Movie.mkv'
            mock_combine.return_value = _make_result(
                status='built',
                path='/x.srt',
                alignment='ok',
            )

            body, status_code = resource.post(radarr_id=42)

        assert status_code == 200
        kwargs = mock_combine.call_args.kwargs
        assert kwargs['languages'] == ['en', 'hu']
        assert kwargs['format'] == 'ass'

    def test_post_combine_skipped(self):
        """A skipped result returns HTTP 200 with status=skipped."""
        resource = movies_subtitles_module.MoviesSubtitlesCombine()

        mock_db_result = MagicMock()
        mock_db_result.first.return_value = _make_db_row()

        with patch.object(movies_subtitles_module, 'database') as mock_db, \
             patch.object(movies_subtitles_module, 'path_mappings') as mock_pm, \
             patch.object(movies_subtitles_module, 'try_combine_for_video') as mock_combine, \
             patch.object(movies_subtitles_module, 'request', self._make_request({})):

            mock_db.execute.return_value = mock_db_result
            mock_pm.path_replace_movie.return_value = '/mapped/movies/Movie.mkv'
            mock_combine.return_value = _make_result(
                status='skipped',
                reason='missing source(s)',
            )

            body, status_code = resource.post(radarr_id=42)

        assert status_code == 200
        assert body['status'] == 'skipped'
        assert 'missing' in body['reason']

    def test_post_combine_failed(self):
        """A failed result returns HTTP 500."""
        resource = movies_subtitles_module.MoviesSubtitlesCombine()

        mock_db_result = MagicMock()
        mock_db_result.first.return_value = _make_db_row()

        with patch.object(movies_subtitles_module, 'database') as mock_db, \
             patch.object(movies_subtitles_module, 'path_mappings') as mock_pm, \
             patch.object(movies_subtitles_module, 'try_combine_for_video') as mock_combine, \
             patch.object(movies_subtitles_module, 'request', self._make_request({})):

            mock_db.execute.return_value = mock_db_result
            mock_pm.path_replace_movie.return_value = '/mapped/movies/Movie.mkv'
            mock_combine.return_value = _make_result(
                status='failed',
                error='bad SRT',
            )

            body, status_code = resource.post(radarr_id=42)

        assert status_code == 500
        assert 'bad SRT' in body['error']

    def test_post_combine_movie_not_found(self):
        """Missing movie in DB returns HTTP 404."""
        resource = movies_subtitles_module.MoviesSubtitlesCombine()

        mock_db_result = MagicMock()
        mock_db_result.first.return_value = None

        with patch.object(movies_subtitles_module, 'database') as mock_db, \
             patch.object(movies_subtitles_module, 'request', self._make_request({})):

            mock_db.execute.return_value = mock_db_result

            body, status_code = resource.post(radarr_id=999)

        assert status_code == 404

# coding=utf-8

"""Unit tests for the subtitle download endpoints (single files and zip bundles).

Pattern matches test_combine_api_movies.py: patch heavy dependencies in
sys.modules before importing the module under test, keep a reference, then
fully restore sys.modules so nothing leaks into later test files.
"""

import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.bazarr import _module_isolation

_SYS_BEFORE = dict(sys.modules)


# ---------------------------------------------------------------------------
# Patch heavy dependencies before importing the module under test.
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
    sys.modules[_mod] = _obj

# Drop cached copies so the re-import resolves against the mocks above.
sys.modules.pop('api.subtitles.download', None)
sys.modules.pop('api.subtitles.content', None)

import api.subtitles.download as download_module  # noqa: E402

_module_isolation.restore(_SYS_BEFORE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _IdentityPathMappings:
    """path_replace_instance stub that records calls and returns the path."""

    def __init__(self):
        self.calls = []

    def path_replace_instance(self, path, arr_instance_id, media_type):
        self.calls.append((path, arr_instance_id, media_type))
        return path


@pytest.fixture
def identity_mappings(monkeypatch):
    stub = _IdentityPathMappings()
    monkeypatch.setattr(download_module, 'path_mappings', stub)
    return stub


def _episode_row(season, subtitles, arr_instance_id=None):
    return SimpleNamespace(season=season, subtitles=subtitles,
                           arr_instance_id=arr_instance_id)


def _movie_row(subtitles, arr_instance_id=None, title='Movie'):
    return SimpleNamespace(subtitles=subtitles, arr_instance_id=arr_instance_id,
                           title=title)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestSafeFilenameComponent:

    def test_strips_forbidden_characters(self):
        assert download_module.safe_filename_component(
            'A/B\\C:D*E?F"G<H>I|J', 'x') == 'A_B_C_D_E_F_G_H_I_J'

    def test_collapses_whitespace_and_trims_dots(self):
        assert download_module.safe_filename_component('  A   B. ', 'x') == 'A B'

    def test_fallback_on_empty(self):
        assert download_module.safe_filename_component('', 'series') == 'series'
        assert download_module.safe_filename_component(None, 'movie') == 'movie'
        assert download_module.safe_filename_component('...', 'x') == 'x'


class TestUniqueArcname:

    def test_first_use_passes_through(self):
        used = set()
        assert download_module.unique_arcname(used, 'a/b.srt') == 'a/b.srt'

    def test_collisions_get_numbered(self):
        used = set()
        download_module.unique_arcname(used, 'a/b.srt')
        assert download_module.unique_arcname(used, 'a/b.srt') == 'a/b (2).srt'
        assert download_module.unique_arcname(used, 'a/b.srt') == 'a/b (3).srt'


class TestIterExternalSubtitles:

    def test_parses_pairs_and_skips_embedded(self):
        raw = "[['en', '/sub/a.en.srt'], ['hu', None], ['de', ''], ['fr', '/sub/a.fr.srt', 123]]"
        assert download_module.iter_external_subtitles(raw) == [
            ('en', '/sub/a.en.srt'),
            ('fr', '/sub/a.fr.srt'),
        ]

    def test_bad_input(self):
        assert download_module.iter_external_subtitles(None) == []
        assert download_module.iter_external_subtitles('') == []
        assert download_module.iter_external_subtitles('not a literal [') == []
        assert download_module.iter_external_subtitles("{'a': 1}") == []


class TestMatchesLanguage:

    def test_no_filter_matches_everything(self):
        assert download_module.matches_language('en:hi', None)
        assert download_module.matches_language('en', '')

    def test_base_match_includes_variants(self):
        assert download_module.matches_language('en', 'en')
        assert download_module.matches_language('en:hi', 'en')
        assert download_module.matches_language('en:forced', 'en')
        assert download_module.matches_language('en:sync-ffsubsync', 'EN')
        assert download_module.matches_language('pt-BR', 'pt-br')

    def test_other_language_rejected(self):
        assert not download_module.matches_language('hu', 'en')
        assert not download_module.matches_language('en:hi', 'hu')


class TestLanguageFilterRe:

    def test_accepts_base_codes(self):
        assert download_module._LANGUAGE_FILTER_RE.match('en')
        assert download_module._LANGUAGE_FILTER_RE.match('pt-BR')
        assert download_module._LANGUAGE_FILTER_RE.match('zho')

    def test_rejects_modifiers_and_traversal(self):
        assert not download_module._LANGUAGE_FILTER_RE.match('en:hi')
        assert not download_module._LANGUAGE_FILTER_RE.match('../evil')
        assert not download_module._LANGUAGE_FILTER_RE.match('en/..')
        assert not download_module._LANGUAGE_FILTER_RE.match('')


# ---------------------------------------------------------------------------
# Entry collection
# ---------------------------------------------------------------------------

class TestCollectSeriesBundleEntries:

    def test_groups_by_season_folder(self, identity_mappings):
        rows = [
            _episode_row(1, "[['en', '/tv/S01E01.en.srt']]"),
            _episode_row(2, "[['en', '/tv/S02E01.en.srt'], ['hu', '/tv/S02E01.hu.srt']]"),
        ]
        entries = download_module.collect_series_bundle_entries(rows)
        assert entries == [
            ('Season 01/S01E01.en.srt', '/tv/S01E01.en.srt'),
            ('Season 02/S02E01.en.srt', '/tv/S02E01.en.srt'),
            ('Season 02/S02E01.hu.srt', '/tv/S02E01.hu.srt'),
        ]

    def test_season_and_language_filters(self, identity_mappings):
        rows = [
            _episode_row(1, "[['en', '/tv/S01E01.en.srt'], ['hu', '/tv/S01E01.hu.srt']]"),
            _episode_row(2, "[['en:hi', '/tv/S02E01.en.hi.srt']]"),
        ]
        only_s2 = download_module.collect_series_bundle_entries(rows, season=2)
        assert only_s2 == [('Season 02/S02E01.en.hi.srt', '/tv/S02E01.en.hi.srt')]

        only_en = download_module.collect_series_bundle_entries(rows, language='en')
        assert [arc for arc, _ in only_en] == [
            'Season 01/S01E01.en.srt',
            'Season 02/S02E01.en.hi.srt',
        ]

    def test_paths_map_through_owning_instance(self, identity_mappings):
        rows = [_episode_row(3, "[['en', '/tv/S03E01.en.srt']]", arr_instance_id=7)]
        download_module.collect_series_bundle_entries(rows)
        assert identity_mappings.calls == [('/tv/S03E01.en.srt', 7, 'episode')]


class TestCollectMovieBundleEntries:

    def test_collects_and_filters(self, identity_mappings):
        row = _movie_row("[['en', '/movies/M.en.srt'], ['hu', '/movies/M.hu.srt'], ['de', None]]",
                         arr_instance_id=2)
        all_entries = download_module.collect_movie_bundle_entries(row)
        assert all_entries == [
            ('M.en.srt', '/movies/M.en.srt'),
            ('M.hu.srt', '/movies/M.hu.srt'),
        ]
        hu_only = download_module.collect_movie_bundle_entries(row, language='hu')
        assert hu_only == [('M.hu.srt', '/movies/M.hu.srt')]
        assert ('/movies/M.en.srt', 2, 'movie') in identity_mappings.calls


# ---------------------------------------------------------------------------
# Zip assembly
# ---------------------------------------------------------------------------

class TestBuildSubtitleBundle:

    def test_zips_existing_files_with_arcnames(self, tmp_path):
        a = tmp_path / 'a.en.srt'
        b = tmp_path / 'b.en.srt'
        a.write_text('1\n00:00:01,000 --> 00:00:02,000\nA\n')
        b.write_text('1\n00:00:01,000 --> 00:00:02,000\nB\n')
        entries = [
            ('Season 01/a.en.srt', str(a)),
            ('Season 02/b.en.srt', str(b)),
            ('Season 02/missing.srt', str(tmp_path / 'missing.srt')),
        ]
        buffer = download_module.build_subtitle_bundle(entries)
        assert buffer is not None
        with zipfile.ZipFile(buffer) as archive:
            assert sorted(archive.namelist()) == [
                'Season 01/a.en.srt', 'Season 02/b.en.srt']
            assert archive.read('Season 01/a.en.srt').endswith(b'A\n')

    def test_duplicate_arcnames_are_deduplicated(self, tmp_path):
        a = tmp_path / 'a.srt'
        a.write_text('x')
        entries = [('same.srt', str(a)), ('same.srt', str(a))]
        buffer = download_module.build_subtitle_bundle(entries)
        with zipfile.ZipFile(buffer) as archive:
            assert sorted(archive.namelist()) == ['same (2).srt', 'same.srt']

    def test_nothing_to_add_returns_none(self, tmp_path):
        entries = [('gone.srt', str(tmp_path / 'gone.srt'))]
        assert download_module.build_subtitle_bundle(entries) is None
        assert download_module.build_subtitle_bundle([]) is None

    def test_size_cap_raises(self, tmp_path):
        big = tmp_path / 'big.srt'
        big.write_text('x' * 1024)
        with pytest.raises(download_module.BundleTooLargeError):
            download_module.build_subtitle_bundle(
                [('big.srt', str(big))], max_total_size=100)


class TestBundleDownloadName:

    def test_variants(self):
        f = download_module.bundle_download_name
        assert f('Show') == 'Show - subtitles.zip'
        assert f('Show', season=2) == 'Show - Season 02 - subtitles.zip'
        assert f('Show', language='EN') == 'Show - en - subtitles.zip'
        assert f('Show', season=10, language='hu') == \
            'Show - Season 10 - hu - subtitles.zip'


# ---------------------------------------------------------------------------
# Route handlers (module-level attributes patched per test)
# ---------------------------------------------------------------------------

def _fake_request(args=None):
    values = args or {}

    class _Args:
        def get(self, key, default=None, type=None):  # flask-like signature
            value = values.get(key, default)
            if value is not None and type is not None:
                try:
                    return type(value)
                except (TypeError, ValueError):
                    return default
            return value

    return SimpleNamespace(args=_Args())


class TestSingleFileDownload:

    def test_error_tuple_passes_through(self, monkeypatch):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'resolve_subtitle_path',
                            lambda *a, **k: ('No subtitle found for requested language', 404))
        resource = download_module.EpisodeSubtitleFileDownload()
        assert resource.get(12, 'en') == ('No subtitle found for requested language', 404)

    def test_success_sends_attachment(self, monkeypatch):
        sent = {}

        def fake_send_file(path, **kwargs):
            sent['path'] = path
            sent.update(kwargs)
            return 'SENT'

        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'arr_instance_id': 3}))
        captured = {}

        def fake_resolve(media_type, media_id, language_code, arr_instance_id=None):
            captured['args'] = (media_type, media_id, language_code, arr_instance_id)
            return ('/media/Movie (2020)/Movie.en.srt', {'mediaTitle': 'Movie'})

        monkeypatch.setattr(download_module, 'resolve_subtitle_path', fake_resolve)
        monkeypatch.setattr(download_module, 'send_file', fake_send_file)

        resource = download_module.MovieSubtitleFileDownload()
        assert resource.get(654, 'en:hi') == 'SENT'
        assert captured['args'] == ('movie', 654, 'en:hi', 3)
        assert sent['path'] == '/media/Movie (2020)/Movie.en.srt'
        assert sent['as_attachment'] is True
        assert sent['download_name'] == 'Movie.en.srt'


class _FakeDatabase:
    """Returns queued results for successive execute() calls."""

    def __init__(self, results):
        self._results = list(results)

    def execute(self, query):
        result = self._results.pop(0)

        class _Result:
            def first(self):
                return result if not isinstance(result, list) else (
                    result[0] if result else None)

            def all(self):
                return result if isinstance(result, list) else [result]

        return _Result()


class TestSeriesBundleDownload:

    def test_series_not_found(self, monkeypatch):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([None]))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(99) == ('Series not found', 404)

    def test_invalid_language_filter_rejected(self, monkeypatch):
        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'language': 'en:hi'}))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('Invalid language filter', 400)

    def test_bundles_matching_episodes(self, monkeypatch, tmp_path, identity_mappings):
        sub = tmp_path / 'S01E01.en.srt'
        sub.write_text('x')
        series_row = SimpleNamespace(title='My Show', arr_instance_id=None)
        episode_rows = [_episode_row(1, f"[['en', '{sub}']]")]
        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'language': 'en'}))
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([series_row, episode_rows]))
        sent = {}

        def fake_send_file(buffer, **kwargs):
            sent['names'] = zipfile.ZipFile(buffer).namelist()
            sent.update(kwargs)
            return 'SENT'

        monkeypatch.setattr(download_module, 'send_file', fake_send_file)
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == 'SENT'
        assert sent['names'] == ['Season 01/S01E01.en.srt']
        assert sent['download_name'] == 'My Show - en - subtitles.zip'
        assert sent['mimetype'] == 'application/zip'

    def test_no_files_is_404(self, monkeypatch, identity_mappings):
        series_row = SimpleNamespace(title='My Show', arr_instance_id=None)
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([series_row, []]))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('No subtitle files found', 404)


class TestMovieBundleDownload:

    def test_movie_not_found(self, monkeypatch):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([None]))
        resource = download_module.MovieSubtitleBundleDownload()
        assert resource.get(1) == ('Movie not found', 404)

    def test_bundles_movie_files(self, monkeypatch, tmp_path, identity_mappings):
        sub = tmp_path / 'Movie.en.srt'
        sub.write_text('x')
        movie_row = _movie_row(f"[['en', '{sub}']]", title='Movie: The Sequel')
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([movie_row]))
        sent = {}

        def fake_send_file(buffer, **kwargs):
            sent['names'] = zipfile.ZipFile(buffer).namelist()
            sent.update(kwargs)
            return 'SENT'

        monkeypatch.setattr(download_module, 'send_file', fake_send_file)
        resource = download_module.MovieSubtitleBundleDownload()
        assert resource.get(654) == 'SENT'
        assert sent['names'] == ['Movie.en.srt']
        assert sent['download_name'] == 'Movie_ The Sequel - subtitles.zip'

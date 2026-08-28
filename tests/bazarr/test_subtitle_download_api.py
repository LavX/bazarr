# coding=utf-8

"""Unit tests for the subtitle download endpoints (single files and zip bundles).

Pattern matches test_combine_api_movies.py: patch heavy dependencies in
sys.modules before importing the module under test, keep a reference, then
fully restore sys.modules so nothing leaks into later test files.
"""

import os
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

for _mod, _obj in _patches.items():
    sys.modules[_mod] = _obj

# Drop cached copies so the re-import resolves against the mocks above.
sys.modules.pop('api.subtitles.download', None)
sys.modules.pop('api.subtitles.content', None)

import api.subtitles.download as download_module  # noqa: E402
import api.subtitles.content as content_module  # noqa: E402

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
def no_instance_param(monkeypatch):
    # _request_arr_instance_id lives in content.py and reads content's own
    # flask request; pin it directly for route tests.
    monkeypatch.setattr(download_module, '_request_arr_instance_id', lambda: None)


@pytest.fixture
def identity_mappings(monkeypatch):
    stub = _IdentityPathMappings()
    monkeypatch.setattr(download_module, 'path_mappings', stub)
    # The subfolder target depends on live settings; the tests pin it off.
    monkeypatch.setattr(download_module, 'get_target_folder', lambda path: None)
    return stub


def _episode_row(season, subtitles, arr_instance_id=None, path='/tv/video.mkv'):
    return SimpleNamespace(season=season, subtitles=subtitles,
                           arr_instance_id=arr_instance_id, path=path)


def _movie_row(subtitles, arr_instance_id=None, title='Movie',
               path='/movies/Movie.mkv'):
    return SimpleNamespace(subtitles=subtitles, arr_instance_id=arr_instance_id,
                           title=title, path=path)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestSafeFilenameComponent:

    def test_strips_forbidden_characters(self):
        assert download_module.safe_filename_component(
            'A/B\\C:D*E?F"G<H>I|J;K', 'x') == 'A_B_C_D_E_F_G_H_I_J_K'

    def test_collapses_whitespace_and_trims_dots(self):
        assert download_module.safe_filename_component('  A   B. ', 'x') == 'A B'

    def test_fallback_on_empty(self):
        assert download_module.safe_filename_component('', 'series') == 'series'
        assert download_module.safe_filename_component(None, 'movie') == 'movie'
        assert download_module.safe_filename_component('...', 'x') == 'x'


class TestSanitizeArcnameComponent:

    def test_neutralizes_separators(self):
        f = download_module.sanitize_arcname_component
        assert f(r'..\..\evil.srt') == '.._.._evil.srt'
        assert f('plain.en.srt') == 'plain.en.srt'


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


class TestDedupeLanguageEntries:

    def test_keeps_newest_for_duplicate_language(self, tmp_path):
        older = tmp_path / 'old.en.ass'
        newer = tmp_path / 'new.en.srt'
        older.write_text('old')
        newer.write_text('new')
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        pairs = [('en', str(older)), ('en', str(newer)), ('hu', str(older))]
        result = dict(download_module.dedupe_language_entries(pairs, lambda p: p))
        assert result == {'en': str(newer), 'hu': str(older)}


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

    def test_content_grammar_rejects_trailing_newline(self):
        # The single-file route validates through content.py's full grammar;
        # \Z (not $) so an encoded trailing newline cannot pass.
        assert not content_module._LANGUAGE_CODE_RE.match('en\n')
        assert not content_module._LANGUAGE_CODE_RE.match('en:hi\n')
        assert content_module._LANGUAGE_CODE_RE.match('en:hi')

    def test_rejects_modifiers_traversal_and_newlines(self):
        assert not download_module._LANGUAGE_FILTER_RE.match('en:hi')
        assert not download_module._LANGUAGE_FILTER_RE.match('../evil')
        assert not download_module._LANGUAGE_FILTER_RE.match('en/..')
        assert not download_module._LANGUAGE_FILTER_RE.match('')
        # $ would accept a trailing newline; \Z must not.
        assert not download_module._LANGUAGE_FILTER_RE.match('en\n')


# ---------------------------------------------------------------------------
# Containment barrier
# ---------------------------------------------------------------------------

class TestResolveBundlePath:

    def test_accepts_file_under_trusted_root(self, tmp_path):
        sub = tmp_path / 'a.en.srt'
        sub.write_text('x')
        assert download_module.resolve_bundle_path(
            str(sub), [str(tmp_path)]) == str(sub)

    def test_rejects_symlink_escaping_the_root(self, tmp_path):
        outside = tmp_path / 'outside'
        media = tmp_path / 'media'
        outside.mkdir()
        media.mkdir()
        secret = outside / 'secret.conf'
        secret.write_text('apikey')
        link = media / 'movie.en.srt'
        link.symlink_to(secret)
        assert download_module.resolve_bundle_path(str(link), [str(media)]) is None

    def test_rejects_symlink_to_in_root_non_subtitle(self, tmp_path):
        # The extension allowlist must run on the RESOLVED path: an indexed
        # .srt that is a symlink to Movie.nfo inside the root must not pass.
        nfo = tmp_path / 'Movie.nfo'
        nfo.write_text('metadata')
        link = tmp_path / 'movie.en.srt'
        link.symlink_to(nfo)
        assert download_module.resolve_bundle_path(str(link), [str(tmp_path)]) is None

    def test_rejects_path_outside_all_roots(self, tmp_path):
        assert download_module.resolve_bundle_path(
            '/etc/passwd.srt', [str(tmp_path)]) is None

    def test_rejects_unknown_extension(self, tmp_path):
        exe = tmp_path / 'a.exe'
        exe.write_text('x')
        assert download_module.resolve_bundle_path(
            str(exe), [str(tmp_path)]) is None


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
        assert ('/tv/S03E01.en.srt', 7, 'episode') in identity_mappings.calls
        assert ('/tv/video.mkv', 7, 'episode') in identity_mappings.calls

    def test_entry_outside_media_dir_is_dropped(self, identity_mappings):
        rows = [_episode_row(1, "[['en', '/elsewhere/S01E01.en.srt']]")]
        assert download_module.collect_series_bundle_entries(rows) == []

    def test_mapping_resolved_once_per_unique_path(self, identity_mappings):
        # Two rows sharing one video path (a multi-episode file): the mapping
        # must not be recomputed per row.
        rows = [
            _episode_row(1, "[['en', '/tv/S01E01E02.en.srt']]"),
            _episode_row(1, "[['en', '/tv/S01E01E02.en.srt']]"),
        ]
        download_module.collect_series_bundle_entries(rows)
        assert identity_mappings.calls.count(('/tv/S01E01E02.en.srt', None, 'episode')) == 1


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

    def test_same_disk_path_is_added_once(self, tmp_path):
        a = tmp_path / 'a.srt'
        a.write_text('x')
        entries = [('Season 01/a.srt', str(a)), ('Season 01/a.srt', str(a))]
        buffer = download_module.build_subtitle_bundle(entries)
        with zipfile.ZipFile(buffer) as archive:
            assert archive.namelist() == ['Season 01/a.srt']

    def test_distinct_files_with_same_arcname_get_suffixed(self, tmp_path):
        one = tmp_path / 'one'
        two = tmp_path / 'two'
        one.mkdir()
        two.mkdir()
        (one / 'same.srt').write_text('1')
        (two / 'same.srt').write_text('2')
        entries = [('same.srt', str(one / 'same.srt')),
                   ('same.srt', str(two / 'same.srt'))]
        buffer = download_module.build_subtitle_bundle(entries)
        with zipfile.ZipFile(buffer) as archive:
            assert sorted(archive.namelist()) == ['same (2).srt', 'same.srt']

    def test_nothing_to_add_returns_none(self, tmp_path):
        entries = [('gone.srt', str(tmp_path / 'gone.srt'))]
        assert download_module.build_subtitle_bundle(entries) is None
        assert download_module.build_subtitle_bundle([]) is None

    def test_size_cap_enforced_while_reading(self, tmp_path):
        big = tmp_path / 'big.srt'
        big.write_text('x' * 1024)
        with pytest.raises(download_module.BundleTooLargeError):
            download_module.build_subtitle_bundle(
                [('big.srt', str(big))], max_total_size=100)

    def test_pre_1980_mtime_is_clamped_not_fatal(self, tmp_path):
        old = tmp_path / 'old.srt'
        old.write_text('x')
        os.utime(old, (0, 0))
        buffer = download_module.build_subtitle_bundle([('old.srt', str(old))])
        with zipfile.ZipFile(buffer) as archive:
            info = archive.getinfo('old.srt')
            assert info.date_time[0] == 1980

    def test_zip_date_time_clamps_both_ends(self):
        assert download_module._zip_date_time(0) == download_module._ZIP_EPOCH
        assert download_module._zip_date_time(-1) == download_module._ZIP_EPOCH
        # Year 2110: beyond the zip date field's 2107 ceiling.
        assert download_module._zip_date_time(4418064000) == \
            download_module._ZIP_LAST_TIMESTAMP
        assert download_module._zip_date_time(10**18) == \
            download_module._ZIP_EPOCH

    def test_directory_entry_is_skipped(self, tmp_path):
        sub = tmp_path / 'dir.srt'
        sub.mkdir()
        ok = tmp_path / 'ok.srt'
        ok.write_text('x')
        buffer = download_module.build_subtitle_bundle(
            [('dir.srt', str(sub)), ('ok.srt', str(ok))])
        with zipfile.ZipFile(buffer) as archive:
            assert archive.namelist() == ['ok.srt']


class TestBundleDownloadName:

    def test_variants(self):
        f = download_module.bundle_download_name
        assert f('Show') == 'Show - subtitles.zip'
        assert f('Show', season=2) == 'Show - Season 02 - subtitles.zip'
        assert f('Show', season=0) == 'Show - Season 00 - subtitles.zip'
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


class _FakeResponse:
    def __init__(self):
        self.headers = {}


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


class TestSingleFileDownload:

    @pytest.fixture(autouse=True)
    def _unambiguous(self, monkeypatch):
        monkeypatch.setattr(download_module, '_ambiguous_media_error',
                            lambda *a, **k: None)

    def test_error_tuple_passes_through(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'resolve_subtitle_path',
                            lambda *a, **k: ('No subtitle found for requested language', 404))
        resource = download_module.EpisodeSubtitleFileDownload()
        assert resource.get(12, 'en') == ('No subtitle found for requested language', 404)

    def test_success_sends_attachment_with_nosniff(self, monkeypatch):
        sent = {}

        def fake_send_file(path, **kwargs):
            sent['path'] = path
            sent.update(kwargs)
            return _FakeResponse()

        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, '_request_arr_instance_id', lambda: 3)
        captured = {}

        def fake_resolve(media_type, media_id, language_code, arr_instance_id=None):
            captured['args'] = (media_type, media_id, language_code, arr_instance_id)
            return ('/media/Movie (2020)/Movie.en.srt', {'mediaTitle': 'Movie'})

        monkeypatch.setattr(download_module, 'resolve_subtitle_path', fake_resolve)
        monkeypatch.setattr(download_module, 'send_file', fake_send_file)

        resource = download_module.MovieSubtitleFileDownload()
        response = resource.get(654, 'en:hi')
        assert captured['args'] == ('movie', 654, 'en:hi', 3)
        assert sent['path'] == '/media/Movie (2020)/Movie.en.srt'
        assert sent['as_attachment'] is True
        assert sent['download_name'] == 'Movie.en.srt'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'

    def test_file_deleted_after_resolution_is_404(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'resolve_subtitle_path',
                            lambda *a, **k: ('/media/gone.srt', {}))

        def raising_send_file(path, **kwargs):
            raise FileNotFoundError(path)

        monkeypatch.setattr(download_module, 'send_file', raising_send_file)
        resource = download_module.EpisodeSubtitleFileDownload()
        assert resource.get(12, 'en') == ('Subtitle file or directory not found', 404)


class TestSingleFileAmbiguity:

    def test_two_instance_rows_without_param_is_400(self, monkeypatch, no_instance_param):
        rows = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([rows]))
        resource = download_module.EpisodeSubtitleFileDownload()
        assert resource.get(12, 'en') == \
            ('Ambiguous Sonarr episode ID; pass arr_instance_id', 400)

    def test_movie_two_rows_is_400(self, monkeypatch, no_instance_param):
        rows = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([rows]))
        resource = download_module.MovieSubtitleFileDownload()
        assert resource.get(654, 'en') == \
            ('Ambiguous Radarr movie ID; pass arr_instance_id', 400)

    def test_single_row_proceeds_to_resolution(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([[SimpleNamespace(id=1)]]))
        monkeypatch.setattr(download_module, 'resolve_subtitle_path',
                            lambda *a, **k: ('missing', 404))
        resource = download_module.EpisodeSubtitleFileDownload()
        assert resource.get(12, 'en') == ('missing', 404)


class TestSeriesBundleDownload:

    def test_series_not_found(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([[]]))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(99) == ('Series not found', 404)

    def test_ambiguous_series_without_instance_is_400(self, monkeypatch, no_instance_param):
        rows = [SimpleNamespace(title='A', arr_instance_id=1),
                SimpleNamespace(title='B', arr_instance_id=2)]
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([rows]))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(5) == ('Ambiguous Sonarr series ID; pass arr_instance_id', 400)

    def test_invalid_language_filter_rejected(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'language': 'en:hi'}))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('Invalid language filter', 400)

    def test_invalid_season_filter_rejected(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'season': 'abc'}))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('Invalid season filter', 400)

    def test_huge_season_digit_string_rejected(self, monkeypatch, no_instance_param):
        # isdigit passes but int() would hit the conversion limit: still 400.
        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'season': '9' * 5000}))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('Invalid season filter', 400)

    def test_episode_query_scoped_to_series_rows_instance(self, monkeypatch,
                                                          identity_mappings):
        # No arr_instance_id from the caller: the matched series row's owner
        # must scope the episode query.
        scoped_calls = []

        def fake_scoped(stmt, column, arr_instance_id):
            scoped_calls.append(arr_instance_id)
            return stmt

        monkeypatch.setattr(download_module, 'scoped', fake_scoped)
        monkeypatch.setattr(download_module, '_request_arr_instance_id', lambda: None)
        series_row = SimpleNamespace(title='My Show', arr_instance_id=7)
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([[series_row], []]))
        resource = download_module.SeriesSubtitleBundleDownload()
        resource.get(1)
        assert scoped_calls == [None, 7]

    def test_bundles_matching_episodes(self, monkeypatch, no_instance_param, tmp_path, identity_mappings):
        sub = tmp_path / 'S01E01.en.srt'
        sub.write_text('x')
        series_row = SimpleNamespace(title='My Show', arr_instance_id=None)
        episode_rows = [_episode_row(1, f"[['en', '{sub}']]",
                                     path=str(tmp_path / 'S01E01.mkv'))]
        monkeypatch.setattr(download_module, 'request',
                            _fake_request({'language': 'en'}))
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([[series_row], episode_rows]))
        sent = {}

        def fake_send_file(buffer, **kwargs):
            sent['names'] = zipfile.ZipFile(buffer).namelist()
            sent.update(kwargs)
            return _FakeResponse()

        monkeypatch.setattr(download_module, 'send_file', fake_send_file)
        resource = download_module.SeriesSubtitleBundleDownload()
        response = resource.get(1)
        assert sent['names'] == ['Season 01/S01E01.en.srt']
        assert sent['download_name'] == 'My Show - en - subtitles.zip'
        assert sent['mimetype'] == 'application/zip'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'

    def test_no_files_is_404(self, monkeypatch, no_instance_param, identity_mappings):
        series_row = SimpleNamespace(title='My Show', arr_instance_id=None)
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([[series_row], []]))
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('No subtitle files found', 404)

    def test_oversized_bundle_is_413(self, monkeypatch, no_instance_param, identity_mappings):
        series_row = SimpleNamespace(title='My Show', arr_instance_id=None)
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([[series_row], []]))

        def too_big(entries, **kwargs):
            raise download_module.BundleTooLargeError

        monkeypatch.setattr(download_module, 'build_subtitle_bundle', too_big)
        resource = download_module.SeriesSubtitleBundleDownload()
        assert resource.get(1) == ('Subtitle bundle too large', 413)


class TestMovieBundleDownload:

    def test_movie_not_found(self, monkeypatch, no_instance_param):
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([[]]))
        resource = download_module.MovieSubtitleBundleDownload()
        assert resource.get(1) == ('Movie not found', 404)

    def test_ambiguous_movie_without_instance_is_400(self, monkeypatch, no_instance_param):
        rows = [_movie_row('[]', arr_instance_id=1),
                _movie_row('[]', arr_instance_id=2)]
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database', _FakeDatabase([rows]))
        resource = download_module.MovieSubtitleBundleDownload()
        assert resource.get(1) == ('Ambiguous Radarr movie ID; pass arr_instance_id', 400)

    def test_bundles_movie_files(self, monkeypatch, no_instance_param, tmp_path, identity_mappings):
        sub = tmp_path / 'Movie.en.srt'
        sub.write_text('x')
        movie_row = _movie_row(f"[['en', '{sub}']]", title='Movie: The Sequel',
                               path=str(tmp_path / 'Movie.mkv'))
        monkeypatch.setattr(download_module, 'request', _fake_request())
        monkeypatch.setattr(download_module, 'database',
                            _FakeDatabase([[movie_row]]))
        sent = {}

        def fake_send_file(buffer, **kwargs):
            sent['names'] = zipfile.ZipFile(buffer).namelist()
            sent.update(kwargs)
            return _FakeResponse()

        monkeypatch.setattr(download_module, 'send_file', fake_send_file)
        resource = download_module.MovieSubtitleBundleDownload()
        resource.get(654)
        assert sent['names'] == ['Movie.en.srt']
        assert sent['download_name'] == 'Movie_ The Sequel - subtitles.zip'

# -*- coding: utf-8 -*-
import pytest
from unittest.mock import patch, MagicMock
from subliminal_patch.providers.opensubtitles_scraper import OpenSubtitlesScraperMixin, _LANG_3_TO_2
from subliminal_patch.providers.opensubtitles import OpenSubtitlesSubtitle, _OpenSubtitlesSubtitle  # noqa: F401
from subliminal_patch.exceptions import APIThrottled
from subliminal.exceptions import ServiceUnavailable
from subliminal_patch.core import Movie, Episode
from subzero.language import Language


class FakeProvider(OpenSubtitlesScraperMixin):
    """Fake provider to test the mixin."""
    subtitle_class = OpenSubtitlesSubtitle
    scraper_service_url = 'http://localhost:8000'
    skip_wrong_fps = False


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def mock_movie():
    return Movie(
        "Interstellar.2014.1080p.BluRay.x264.mkv",
        "Interstellar",
        year=2014,
        imdb_id="tt0816692",
    )


@pytest.fixture
def mock_episode():
    return Episode(
        "Breaking.Bad.S01E01.720p.BluRay.mkv",
        "Breaking Bad",
        1, 1,
        series_imdb_id="tt0903747",
    )


@pytest.fixture
def search_result_movie():
    return {
        'results': [{
            'title': 'Interstellar',
            'year': 2014,
            'imdb_id': 'tt0816692',
            'url': 'https://www.opensubtitles.org/en/search/sublanguageid-all/idmovie-182630',
            'subtitle_count': 524,
            'kind': 'movie',
        }],
        'total': 1,
        'query': 'Interstellar',
    }


@pytest.fixture
def search_result_tv():
    return {
        'results': [{
            'title': 'Breaking Bad',
            'year': 2008,
            'imdb_id': 'tt0903747',
            'url': 'https://www.opensubtitles.org/en/search/sublanguageid-all/idmovie-52645',
            'subtitle_count': 3200,
            'kind': 'episode',
        }],
        'total': 1,
        'query': 'Breaking Bad',
    }


@pytest.fixture
def subtitle_response():
    return {
        'subtitles': [
            {
                'subtitle_id': '6703813',
                'language': 'en',
                'filename': 'Interstellar.2014.1080p.BluRay.x264.YIFY.en.srt',
                'release_name': 'Interstellar.2014.1080p.BluRay.x264.YIFY',
                'uploader': 'authentic',
                'download_count': 474233,
                'rating': 8.5,
                'hearing_impaired': False,
                'forced': False,
                'fps': 23.976,
                'download_url': 'https://www.opensubtitles.org/en/subtitles/6703813',
            },
            {
                'subtitle_id': '6080089',
                'language': 'en',
                'filename': 'Interstellar.2014.720p.BluRay.x264-DAA.en.srt',
                'release_name': 'Interstellar.2014.720p.BluRay.x264-DAA',
                'uploader': 'Luis-subs',
                'download_count': 2260372,
                'rating': 3.5,
                'hearing_impaired': False,
                'forced': False,
                'fps': 23.976,
                'download_url': 'https://www.opensubtitles.org/en/subtitles/6080089',
            },
            {
                'subtitle_id': '9999999',
                'language': 'hu',
                'filename': 'Interstellar.2014.hun.srt',
                'release_name': 'Interstellar.2014',
                'uploader': 'someone',
                'download_count': 100,
                'rating': 0.0,
                'hearing_impaired': False,
                'forced': False,
                'fps': None,
                'download_url': 'https://www.opensubtitles.org/en/subtitles/9999999',
            },
        ],
        'total': 3,
        'movie_url': 'https://www.opensubtitles.org/en/search/sublanguageid-all/idmovie-182630',
    }


class TestSelectBestResult:
    def test_exact_imdb_match(self, provider):
        results = [
            {'title': 'Wrong Movie', 'imdb_id': 'tt1111111', 'year': 2014, 'subtitle_count': 999},
            {'title': 'Interstellar', 'imdb_id': 'tt0816692', 'year': 2014, 'subtitle_count': 524},
        ]
        best = provider._select_best_result(results, 'tt0816692', 'Interstellar', 2014)
        assert best['imdb_id'] == 'tt0816692'

    def test_title_and_year_match(self, provider):
        results = [
            {'title': 'Interstellar', 'year': 2014, 'subtitle_count': 524},
            {'title': 'Interstellar Journey', 'year': 2020, 'subtitle_count': 10},
        ]
        best = provider._select_best_result(results, None, 'Interstellar', 2014)
        assert best['title'] == 'Interstellar'
        assert best['year'] == 2014

    def test_no_results(self, provider):
        assert provider._select_best_result([], 'tt0816692', 'Interstellar', 2014) is None

    def test_fallback_to_first(self, provider):
        results = [{'title': 'Something', 'subtitle_count': 0}]
        best = provider._select_best_result(results, None, 'Nomatch', 2050)
        assert best['title'] == 'Something'

    def test_prefers_higher_subtitle_count(self, provider):
        results = [
            {'title': 'Dune', 'year': 2021, 'subtitle_count': 10},
            {'title': 'Dune', 'year': 2021, 'subtitle_count': 500},
        ]
        best = provider._select_best_result(results, None, 'Dune', 2021)
        assert best['subtitle_count'] == 500


class TestQueryScraper:
    @patch.object(FakeProvider, '_scraper_request')
    def test_movie_search_flow(self, mock_request, provider, mock_movie, search_result_movie, subtitle_response):
        mock_request.side_effect = [search_result_movie, subtitle_response]
        languages = {Language.fromietf('en')}

        subtitles = provider._query_scraper(
            mock_movie, languages, imdb_id='tt0816692',
            query=['Interstellar']
        )

        # Verify search was called with correct endpoint and data
        search_call = mock_request.call_args_list[0]
        assert search_call[0][0] == '/api/v1/search/movies'
        assert search_call[0][1]['query'] == 'Interstellar'
        assert search_call[0][1]['imdb_id'] == 'tt0816692'
        assert search_call[0][1]['year'] == 2014

        # Verify subtitles endpoint called with movie_url
        subs_call = mock_request.call_args_list[1]
        assert subs_call[0][0] == '/api/v1/subtitles'
        assert 'idmovie-182630' in subs_call[0][1]['movie_url']
        assert 'en' in subs_call[0][1]['languages']

        # Should return English subtitles only
        assert len(subtitles) == 2
        assert all(str(s.language) == 'en' for s in subtitles)

    @patch.object(FakeProvider, '_scraper_request')
    def test_tv_search_flow(self, mock_request, provider, mock_episode, search_result_tv, subtitle_response):
        mock_request.side_effect = [search_result_tv, subtitle_response]
        languages = {Language.fromietf('en')}

        subtitles = provider._query_scraper(
            mock_episode, languages, imdb_id='tt0903747',
            query=['Breaking Bad'], season=1, episode=1
        )

        # Verify TV endpoint used
        search_call = mock_request.call_args_list[0]
        assert search_call[0][0] == '/api/v1/search/tv'
        assert search_call[0][1]['kind'] == 'episode'

        assert len(subtitles) == 2

    @patch.object(FakeProvider, '_scraper_request')
    def test_no_search_results(self, mock_request, provider, mock_movie):
        mock_request.side_effect = [
            {'results': [], 'total': 0, 'query': 'Inexistent'},
            {'results': [], 'total': 0, 'query': 'Inexistent'},
        ]
        languages = {Language.fromietf('en')}

        subtitles = provider._query_scraper(
            mock_movie, languages, query=['Inexistent Movie 99999']
        )
        assert subtitles == []

    @patch.object(FakeProvider, '_scraper_request')
    def test_throttled_raises(self, mock_request, provider, mock_movie):
        mock_request.side_effect = APIThrottled("busy")
        languages = {Language.fromietf('en')}

        with pytest.raises(APIThrottled):
            provider._query_scraper(mock_movie, languages, query=['Test'])

    @patch.object(FakeProvider, '_scraper_request')
    def test_passes_video_title_as_query(self, mock_request, provider, mock_movie, search_result_movie, subtitle_response):
        """Verify the video title is passed, not an empty string."""
        mock_request.side_effect = [search_result_movie, subtitle_response]
        languages = {Language.fromietf('en')}

        provider._query_scraper(mock_movie, languages, imdb_id='tt0816692')

        search_call = mock_request.call_args_list[0]
        # Should use video.title as fallback when query is None
        assert search_call[0][1]['query'] == 'Interstellar'


class TestParseV1Subtitles:
    def test_builds_quoted_movie_name_for_episodes(self, provider, mock_episode, subtitle_response):
        search_result = {
            'title': 'Breaking Bad', 'year': 2008, 'imdb_id': 'tt0903747',
        }
        languages = {Language.fromietf('en')}

        subtitles = provider._parse_v1_subtitles(
            subtitle_response['subtitles'], search_result, languages,
            season=1, episode=1, only_foreign=False, also_foreign=False,
            video=mock_episode, imdb_id='tt0903747'
        )

        # Check movie_name has quoted format for series matching
        for sub in subtitles:
            assert sub.movie_name.startswith('"Breaking Bad"')

    def test_filters_by_language(self, provider, mock_movie, subtitle_response):
        search_result = {'title': 'Interstellar', 'year': 2014, 'imdb_id': 'tt0816692'}
        languages = {Language.fromietf('hu')}

        subtitles = provider._parse_v1_subtitles(
            subtitle_response['subtitles'], search_result, languages,
            season=None, episode=None, only_foreign=False, also_foreign=False,
            video=mock_movie, imdb_id='tt0816692'
        )

        assert len(subtitles) == 1
        assert str(subtitles[0].language) == 'hu'

    def test_stores_download_url(self, provider, mock_movie, subtitle_response):
        search_result = {'title': 'Interstellar', 'year': 2014, 'imdb_id': 'tt0816692'}
        languages = {Language.fromietf('en')}

        subtitles = provider._parse_v1_subtitles(
            subtitle_response['subtitles'], search_result, languages,
            season=None, episode=None, only_foreign=False, also_foreign=False,
            video=mock_movie, imdb_id='tt0816692'
        )

        for sub in subtitles:
            assert hasattr(sub, 'scraper_download_url')
            assert sub.scraper_download_url.startswith('https://www.opensubtitles.org')

    def test_filters_foreign_only(self, provider, mock_movie):
        search_result = {'title': 'Test', 'year': 2024, 'imdb_id': None}
        subs = [
            {'subtitle_id': '1', 'language': 'en', 'filename': 'a.srt', 'release_name': 'a',
             'uploader': 'x', 'forced': True, 'hearing_impaired': False},
            {'subtitle_id': '2', 'language': 'en', 'filename': 'b.srt', 'release_name': 'b',
             'uploader': 'x', 'forced': False, 'hearing_impaired': False},
        ]
        languages = {Language.fromietf('en'), Language('eng', forced=True)}

        # only_foreign=True should return only forced subs
        result = provider._parse_v1_subtitles(
            subs, search_result, languages,
            season=None, episode=None, only_foreign=True, also_foreign=False,
            video=mock_movie, imdb_id=None
        )
        assert len(result) == 1
        assert result[0].subtitle_id == 1


class TestDownloadSubtitle:
    @patch.object(FakeProvider, '_scraper_request')
    def test_download_with_url(self, mock_request, provider):
        mock_request.return_value = {
            'content': 'SGVsbG8gV29ybGQ=',  # base64 "Hello World"
            'filename': 'test.srt',
            'size': 11,
        }

        subtitle = MagicMock()
        subtitle.subtitle_id = 12345
        subtitle.scraper_download_url = 'https://www.opensubtitles.org/en/subtitles/12345'

        provider._download_subtitle_scraper(subtitle)

        # Verify correct v1 endpoint used
        call_args = mock_request.call_args
        assert call_args[0][0] == '/api/v1/download/subtitle'
        assert call_args[0][1]['subtitle_id'] == '12345'
        assert call_args[0][1]['download_url'] == 'https://www.opensubtitles.org/en/subtitles/12345'

        assert subtitle.content == b'Hello World'

    @patch.object(FakeProvider, '_scraper_request')
    def test_download_no_content_raises(self, mock_request, provider):
        mock_request.return_value = {'content': None}

        subtitle = MagicMock()
        subtitle.subtitle_id = 99999
        subtitle.scraper_download_url = 'https://example.com/sub/99999'

        with pytest.raises(ServiceUnavailable):
            provider._download_subtitle_scraper(subtitle)


class TestScraperRequest:
    @patch('subliminal_patch.providers.opensubtitles_scraper.requests.post')
    def test_handles_503_as_throttled(self, mock_post, provider):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.headers = {'Retry-After': '15'}
        mock_post.return_value = mock_response

        with pytest.raises(APIThrottled, match='retry after 15s'):
            provider._scraper_request('/api/v1/search', {'query': 'test'})

    @patch('subliminal_patch.providers.opensubtitles_scraper.requests.post')
    def test_handles_429_as_throttled(self, mock_post, provider):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        mock_post.return_value = mock_response

        with pytest.raises(APIThrottled):
            provider._scraper_request('/api/v1/search', {'query': 'test'})

    @patch('subliminal_patch.providers.opensubtitles_scraper.requests.post')
    def test_adds_base_url(self, mock_post, provider):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'results': []}
        mock_post.return_value = mock_response

        provider._scraper_request('/api/v1/search', {'query': 'test'})

        called_url = mock_post.call_args[0][0]
        assert called_url == 'http://localhost:8000/api/v1/search'


class TestLangMapping:
    def test_common_languages_mapped(self):
        assert _LANG_3_TO_2['eng'] == 'en'
        assert _LANG_3_TO_2['hun'] == 'hu'
        assert _LANG_3_TO_2['spa'] == 'es'
        assert _LANG_3_TO_2['fre'] == 'fr'
        assert _LANG_3_TO_2['ger'] == 'de'
        assert _LANG_3_TO_2['jpn'] == 'ja'

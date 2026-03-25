# coding=utf-8

from unittest.mock import patch, MagicMock, call


class TestMassBatchOperationValidation:
    """Test mass_batch_operation input validation and routing."""

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_invalid_action_returns_error(self, mock_jobs_queue):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        result = mass_batch_operation(items=[], action='invalid_action', job_id='test')
        assert 'error' in result
        assert 'invalid_action' in result['error'].lower() or 'invalid' in result['error'].lower()

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_empty_items_returns_zeros(self, mock_jobs_queue):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        result = mass_batch_operation(items=[], action='sync', job_id='test')
        assert result['queued'] == 0
        assert result['skipped'] == 0

    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_sync_action_calls_collect_subtitle_items(self, mock_jobs_queue, mock_collect):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([], 0)
        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        mass_batch_operation(items=items, action='sync', job_id='test')
        mock_collect.assert_called_once()

    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_mod_action_calls_collect_subtitle_items(self, mock_jobs_queue, mock_collect):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([], 0)
        items = [{'type': 'movie', 'radarrId': 10}]
        mass_batch_operation(items=items, action='remove_HI', job_id='test')
        mock_collect.assert_called_once()

    @patch('bazarr.subtitles.mass_operations._process_media_action')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_scan_disk_calls_process_media_action(self, mock_jobs_queue, mock_process):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_process.return_value = {'queued': 0, 'skipped': 0, 'errors': []}
        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        mass_batch_operation(items=items, action='scan-disk', job_id='test')
        mock_process.assert_called_once()

    @patch('bazarr.subtitles.mass_operations._process_media_action')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_search_missing_calls_process_media_action(self, mock_jobs_queue, mock_process):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_process.return_value = {'queued': 0, 'skipped': 0, 'errors': []}
        items = [{'type': 'movie', 'radarrId': 10}]
        mass_batch_operation(items=items, action='search-missing', job_id='test')
        mock_process.assert_called_once()


class TestCollectSubtitleItems:
    """Test _collect_subtitle_items function."""

    def _make_episode(self, ep_id=1, series_id=10, path='/video/ep1.mkv',
                      subtitles="[['en', '/subs/ep1.en.srt']]"):
        ep = MagicMock()
        ep.sonarrEpisodeId = ep_id
        ep.sonarrSeriesId = series_id
        ep.path = path
        ep.subtitles = subtitles
        return ep

    def _make_movie(self, radarr_id=10, path='/video/movie.mkv',
                    subtitles="[['en', '/subs/movie.en.srt']]"):
        movie = MagicMock()
        movie.radarrId = radarr_id
        movie.path = path
        movie.subtitles = subtitles
        return movie

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_collects_episode_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                         mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 1
        assert items[0]['sonarr_episode_id'] == 1
        assert items[0]['sonarr_series_id'] == 10
        assert items[0]['srt_path'] == '/subs/ep1.en.srt'

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_collects_movie_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                       mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items_list = [{'type': 'movie', 'radarrId': 10}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 1
        assert items[0]['radarr_id'] == 10
        assert items[0]['srt_path'] == '/subs/movie.en.srt'

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_forced_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                     mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': True, 'hi': False}

        episode = self._make_episode(subtitles="[['en:forced', '/subs/ep1.en.forced.srt']]")
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=False)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_missing_files(self, mock_settings, mock_db, mock_synced_mov,
                                  mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 0
        assert skipped == 1


class TestProcessMediaActions:
    """Test _process_media_action for scan-disk and search-missing."""

    @patch('bazarr.subtitles.mass_operations.series_scan_subtitles')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_scan_disk_series(self, mock_jobs_queue, mock_scan):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(1)
        assert result['queued'] == 1

    @patch('bazarr.subtitles.mass_operations.movies_scan_subtitles')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_scan_disk_movies(self, mock_jobs_queue, mock_scan):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'movie', 'radarrId': 10}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(10)
        assert result['queued'] == 1

    @patch('bazarr.subtitles.mass_operations.series_download_subtitles')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_search_missing_series(self, mock_jobs_queue, mock_download):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = _process_media_action(items, action='search-missing', job_id='test')

        mock_download.assert_called_once_with(1)
        assert result['queued'] == 1

    @patch('bazarr.subtitles.mass_operations.movies_download_subtitles')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_search_missing_movies(self, mock_jobs_queue, mock_download):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'movie', 'radarrId': 10}]
        result = _process_media_action(items, action='search-missing', job_id='test')

        mock_download.assert_called_once_with(10)
        assert result['queued'] == 1

    @patch('bazarr.subtitles.mass_operations.series_scan_subtitles')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_error_handling(self, mock_jobs_queue, mock_scan):
        from bazarr.subtitles.mass_operations import _process_media_action

        mock_scan.side_effect = RuntimeError("scan failed")
        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        assert len(result['errors']) == 1
        assert 'scan failed' in result['errors'][0]


class TestSchedulerIntegration:
    """Test scheduler integration when items=None."""

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_items_none_without_job_id_schedules_job(self, mock_jobs_queue):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        result = mass_batch_operation(items=None, action='sync', job_id=None)
        mock_jobs_queue.add_job_from_function.assert_called_once()
        assert result is None

    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_items_none_with_job_id_syncs_entire_library(self, mock_settings, mock_jobs_queue,
                                                          mock_collect):
        from bazarr.subtitles.mass_operations import mass_batch_operation

        mock_settings.general.use_sonarr = True
        mock_settings.general.use_radarr = True
        mock_collect.return_value = ([], 0)

        result = mass_batch_operation(items=None, action='sync', job_id='test')

        # When items=None, _collect_subtitle_items should be called with items=None
        mock_collect.assert_called_once()
        args = mock_collect.call_args
        assert args[0][0] is None  # items arg should be None

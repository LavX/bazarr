# coding=utf-8

from unittest.mock import patch, MagicMock, call


class TestParseSubtitlesColumn:
    """Test _parse_subtitles_column helper."""

    def test_empty_input_none(self):
        from bazarr.subtitles.mass_operations import _parse_subtitles_column
        assert _parse_subtitles_column(None) == []

    def test_empty_input_string(self):
        from bazarr.subtitles.mass_operations import _parse_subtitles_column
        assert _parse_subtitles_column('') == []

    def test_valid_subtitles(self):
        from bazarr.subtitles.mass_operations import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr:forced', '/path/to/sub.fr.srt']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 2
        assert result[0] == ('en', '/path/to/sub.srt')
        assert result[1] == ('fr:forced', '/path/to/sub.fr.srt')

    def test_invalid_syntax(self):
        from bazarr.subtitles.mass_operations import _parse_subtitles_column
        assert _parse_subtitles_column('not valid python') == []

    def test_skips_entries_without_path(self):
        from bazarr.subtitles.mass_operations import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr', '']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 1
        assert result[0] == ('en', '/path/to/sub.srt')

    def test_short_entries(self):
        from bazarr.subtitles.mass_operations import _parse_subtitles_column
        raw = "[['en']]"
        assert _parse_subtitles_column(raw) == []


class TestProcessSubtitleItem:
    """Test _process_subtitle_item for all action branches."""

    def _make_item(self, sonarr_series_id=10, sonarr_episode_id=1, radarr_id=None):
        return {
            'video_path': '/video/test.mkv',
            'srt_path': '/subs/test.en.srt',
            'srt_lang': 'en',
            'forced': False,
            'hi': False,
            'sonarr_series_id': sonarr_series_id,
            'sonarr_episode_id': sonarr_episode_id,
            'radarr_id': radarr_id,
            'max_offset_seconds': '60',
            'no_fix_framerate': True,
            'gss': True,
        }

    @patch('bazarr.subtitles.mass_operations.sync_subtitles', return_value=True)
    def test_sync_action(self, mock_sync):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'sync', {}, 'test_job')
        assert result is True
        mock_sync.assert_called_once_with(
            video_path='/video/test.mkv',
            srt_path='/subs/test.en.srt',
            srt_lang='en',
            forced=False,
            hi=False,
            percent_score=0,
            sonarr_series_id=10,
            sonarr_episode_id=1,
            radarr_id=None,
            max_offset_seconds='60',
            no_fix_framerate=True,
            gss=True,
            force_sync=True,
            job_id='test_job',
        )

    @patch('bazarr.subtitles.mass_operations.subtitles_apply_mods')
    def test_mod_action_remove_hi(self, mock_mods):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'remove_HI', {}, 'test_job')
        assert result is True
        mock_mods.assert_called_once_with('en', '/subs/test.en.srt', ['remove_HI'], '/video/test.mkv')

    @patch('bazarr.subtitles.mass_operations.subtitles_apply_mods')
    def test_mod_action_ocr_fixes(self, mock_mods):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'OCR_fixes', {}, 'test_job')
        assert result is True
        mock_mods.assert_called_once_with('en', '/subs/test.en.srt', ['OCR_fixes'], '/video/test.mkv')

    @patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=True)
    def test_translate_action(self, mock_translate):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'translate', {'from_lang': 'en', 'to_lang': 'hu'}, 'test_job')
        assert result is True
        # translate is called without job_id so it queues as its own job
        mock_translate.assert_called_once_with(
            video_path='/video/test.mkv',
            source_srt_file='/subs/test.en.srt',
            from_lang='en',
            to_lang='hu',
            forced=False,
            hi=False,
            media_type='series',
            sonarr_series_id=10,
            sonarr_episode_id=1,
            radarr_id=None,
        )

    @patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=True)
    def test_translate_action_movie(self, mock_translate):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = self._make_item(sonarr_series_id=None, sonarr_episode_id=None, radarr_id=5)
        result = _process_subtitle_item(item, 'translate', {'from_lang': 'en', 'to_lang': 'fr'}, 'test_job')
        assert result is True
        mock_translate.assert_called_once()
        call_kwargs = mock_translate.call_args[1]
        assert call_kwargs['media_type'] == 'movies'
        assert call_kwargs['radarr_id'] == 5
        assert 'job_id' not in call_kwargs

    def test_unknown_action_returns_false(self):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'nonexistent', {}, 'test_job')
        assert result is False


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

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_media_action_with_empty_items(self, mock_jobs_queue):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        result = mass_batch_operation(items=[], action='scan-disk', job_id='test')
        assert result['queued'] == 0
        assert result['skipped'] == 0
        assert result['errors'] == []


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

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths')
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_already_synced_episodes(self, mock_settings, mock_db, mock_synced_mov,
                                            mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_synced_ep.return_value = {'/subs/ep1.en.srt'}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths')
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_already_synced_movies(self, mock_settings, mock_db, mock_synced_mov,
                                          mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_synced_mov.return_value = {'/subs/movie.en.srt'}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items_list = [{'type': 'movie', 'radarrId': 10}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_forced_movie_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                           mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': True, 'hi': False}

        movie = self._make_movie(subtitles="[['en:forced', '/subs/movie.en.forced.srt']]")
        mock_db.execute.return_value.all.return_value = [movie]

        items_list = [{'type': 'movie', 'radarrId': 10}]
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
    def test_skips_missing_movie_files(self, mock_settings, mock_db, mock_synced_mov,
                                        mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items_list = [{'type': 'movie', 'radarrId': 10}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_series_type_collects_episodes(self, mock_settings, mock_db, mock_synced_mov,
                                            mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        """Passing type='series' with sonarrSeriesId collects all episodes for that series."""
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode(ep_id=5, series_id=42)
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'series', 'sonarrSeriesId': 42}]
        items, skipped = _collect_subtitle_items(items_list, action='remove_HI', options={})

        assert len(items) == 1
        assert items[0]['sonarr_series_id'] == 42
        assert items[0]['sonarr_episode_id'] == 5


class TestGetSyncedPaths:
    """Test _get_synced_episode_paths and _get_synced_movie_paths."""

    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.select')
    def test_get_synced_episode_paths(self, mock_select, mock_db):
        from bazarr.subtitles.mass_operations import _get_synced_episode_paths

        row1 = MagicMock()
        row1.subtitles_path = '/subs/ep1.srt'
        row2 = MagicMock()
        row2.subtitles_path = None
        row3 = MagicMock()
        row3.subtitles_path = '/subs/ep2.srt'
        mock_db.execute.return_value.all.return_value = [row1, row2, row3]

        result = _get_synced_episode_paths()
        assert result == {'/subs/ep1.srt', '/subs/ep2.srt'}

    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.select')
    def test_get_synced_movie_paths(self, mock_select, mock_db):
        from bazarr.subtitles.mass_operations import _get_synced_movie_paths

        row1 = MagicMock()
        row1.subtitles_path = '/subs/movie1.srt'
        row2 = MagicMock()
        row2.subtitles_path = ''
        mock_db.execute.return_value.all.return_value = [row1, row2]

        result = _get_synced_movie_paths()
        # Empty string is falsy, so it should be excluded
        assert result == {'/subs/movie1.srt'}


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

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_unknown_type_skipped_scan_disk(self, mock_jobs_queue):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'unknown', 'id': 1}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        assert result['skipped'] == 1
        assert result['queued'] == 0

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_unknown_type_skipped_search_missing(self, mock_jobs_queue):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'unknown', 'id': 1}]
        result = _process_media_action(items, action='search-missing', job_id='test')

        assert result['skipped'] == 1
        assert result['queued'] == 0


class TestForceResync:
    """Test that force_resync=True collects already-synced subtitles."""

    def _make_episode(self, ep_id=1, series_id=10, path='/video/ep1.mkv',
                      subtitles="[['en', '/subs/ep1.en.srt']]"):
        ep = MagicMock()
        ep.sonarrEpisodeId = ep_id
        ep.sonarrSeriesId = series_id
        ep.path = path
        ep.subtitles = subtitles
        return ep

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations._get_synced_episode_paths')
    @patch('bazarr.subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_force_resync_collects_already_synced(self, mock_settings, mock_db, mock_synced_mov,
                                                   mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_synced_ep.return_value = {'/subs/ep1.en.srt'}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={'force_resync': True})

        # With force_resync=True, the already-synced subtitle should still be collected
        assert len(items) == 1
        assert skipped == 0


class TestTranslateSkipsExistingLang:
    """Test translate action skips episodes/movies that already have the target language."""

    def _make_episode(self, ep_id=1, series_id=10, path='/video/ep1.mkv',
                      subtitles="[['en', '/subs/ep1.en.srt'], ['hu', '/subs/ep1.hu.srt']]"):
        ep = MagicMock()
        ep.sonarrEpisodeId = ep_id
        ep.sonarrSeriesId = series_id
        ep.path = path
        ep.subtitles = subtitles
        return ep

    def _make_movie(self, radarr_id=1, path='/video/movie1.mkv',
                    subtitles="[['en', '/subs/movie1.en.srt'], ['hu', '/subs/movie1.hu.srt']]"):
        movie = MagicMock()
        movie.radarrId = radarr_id
        movie.path = path
        movie.subtitles = subtitles
        return movie

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_episode_when_target_lang_exists(self, mock_settings, mock_db,
                                                    mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = False
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        # Episode already has 'hu' subtitle
        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        # Translate to 'hu' which already exists
        collected, skipped = _collect_subtitle_items(items, 'translate', {'to_lang': 'hu'})

        assert len(collected) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_collects_episode_when_target_lang_missing(self, mock_settings, mock_db,
                                                        mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = False
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        # Episode has 'en' but NOT 'hu'
        episode = self._make_episode(subtitles="[['en', '/subs/ep1.en.srt']]")
        mock_db.execute.return_value.all.return_value = [episode]

        items = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        collected, skipped = _collect_subtitle_items(items, 'translate', {'to_lang': 'hu'})

        assert len(collected) == 1
        assert skipped == 0

    @patch('bazarr.subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_operations.path_mappings')
    @patch('bazarr.subtitles.mass_operations.database')
    @patch('bazarr.subtitles.mass_operations.settings')
    def test_skips_movie_when_target_lang_exists(self, mock_settings, mock_db,
                                                  mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = False
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items = [{'type': 'movie', 'radarrId': 1}]
        collected, skipped = _collect_subtitle_items(items, 'translate', {'to_lang': 'hu'})

        assert len(collected) == 0
        assert skipped == 1


class TestTranslateDefaultOptions:
    """Test translate action uses defaults when options omit from_lang/to_lang."""

    @patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=True)
    def test_translate_default_options(self, mock_translate):
        from bazarr.subtitles.mass_operations import _process_subtitle_item
        item = {
            'video_path': '/video/test.mkv',
            'srt_path': '/subs/test.en.srt',
            'srt_lang': 'en',
            'forced': False,
            'hi': False,
            'sonarr_series_id': 10,
            'sonarr_episode_id': 1,
            'radarr_id': None,
            'max_offset_seconds': '60',
            'no_fix_framerate': True,
            'gss': True,
        }
        result = _process_subtitle_item(item, 'translate', {}, 'test_job')
        assert result is True
        call_kwargs = mock_translate.call_args[1]
        # from_lang defaults to item's srt_lang
        assert call_kwargs['from_lang'] == 'en'
        # to_lang defaults to 'en'
        assert call_kwargs['to_lang'] == 'en'


class TestMediaActionEpisodeType:
    """Test scan-disk with type='episode'."""

    @patch('bazarr.subtitles.mass_operations.series_scan_subtitles')
    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    def test_scan_disk_episode_type(self, mock_jobs_queue, mock_scan):
        from bazarr.subtitles.mass_operations import _process_media_action

        items = [{'type': 'episode', 'sonarrSeriesId': 5}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(5)
        assert result['queued'] == 1
        assert result['skipped'] == 0


class TestSchedulerIntegration:
    """Test scheduler integration when items=None."""

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


class TestMassBatchOperationProcessing:
    """Test mass_batch_operation end-to-end processing loop."""

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    @patch('bazarr.subtitles.mass_operations._process_subtitle_item', return_value=True)
    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    def test_processes_collected_items(self, mock_collect, mock_process, mock_jq):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([
            {'srt_path': '/subs/test.srt', 'video_path': '/video/test.mkv'},
        ], 0)
        result = mass_batch_operation(
            items=[{'type': 'movie', 'radarrId': 1}],
            action='remove_HI',
            job_id='test',
        )
        assert result['queued'] == 1
        assert result['skipped'] == 0
        mock_process.assert_called_once()

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    @patch('bazarr.subtitles.mass_operations._process_subtitle_item', side_effect=Exception("failed"))
    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    def test_handles_processing_errors(self, mock_collect, mock_process, mock_jq):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([
            {'srt_path': '/subs/test.srt', 'video_path': '/video/test.mkv'},
        ], 0)
        result = mass_batch_operation(
            items=[{'type': 'movie', 'radarrId': 1}],
            action='remove_HI',
            job_id='test',
        )
        assert result['queued'] == 0
        assert len(result['errors']) == 1
        assert 'failed' in result['errors'][0]

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    @patch('bazarr.subtitles.mass_operations._process_subtitle_item', return_value=False)
    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    def test_counts_failed_processing(self, mock_collect, mock_process, mock_jq):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([
            {'srt_path': '/subs/test.srt', 'video_path': '/video/test.mkv'},
        ], 0)
        result = mass_batch_operation(
            items=[{'type': 'movie', 'radarrId': 1}],
            action='sync',
            job_id='test',
        )
        # When _process_subtitle_item returns False, it counts as failed (added to skipped)
        assert result['queued'] == 0
        assert result['skipped'] == 1

    @patch('bazarr.subtitles.mass_operations.jobs_queue')
    @patch('bazarr.subtitles.mass_operations._process_subtitle_item', return_value=True)
    @patch('bazarr.subtitles.mass_operations._collect_subtitle_items')
    def test_processes_multiple_items(self, mock_collect, mock_process, mock_jq):
        from bazarr.subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([
            {'srt_path': '/subs/test1.srt', 'video_path': '/video/test1.mkv'},
            {'srt_path': '/subs/test2.srt', 'video_path': '/video/test2.mkv'},
            {'srt_path': '/subs/test3.srt', 'video_path': '/video/test3.mkv'},
        ], 2)
        result = mass_batch_operation(
            items=[{'type': 'episode', 'sonarrEpisodeId': 1}],
            action='sync',
            job_id='test',
        )
        assert result['queued'] == 3
        assert result['skipped'] == 2  # the 2 skipped from _collect_subtitle_items
        assert mock_process.call_count == 3

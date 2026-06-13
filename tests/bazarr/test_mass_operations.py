# coding=utf-8

from unittest.mock import patch, MagicMock, call  # noqa: F401


class TestParseSubtitlesColumn:
    """Test _parse_subtitles_column helper."""

    def test_empty_input_none(self):
        from subtitles.mass_operations import _parse_subtitles_column
        assert _parse_subtitles_column(None) == []

    def test_empty_input_string(self):
        from subtitles.mass_operations import _parse_subtitles_column
        assert _parse_subtitles_column('') == []

    def test_valid_subtitles(self):
        from subtitles.mass_operations import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr:forced', '/path/to/sub.fr.srt']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 2
        assert result[0] == ('en', '/path/to/sub.srt')
        assert result[1] == ('fr:forced', '/path/to/sub.fr.srt')

    def test_invalid_syntax(self):
        from subtitles.mass_operations import _parse_subtitles_column
        assert _parse_subtitles_column('not valid python') == []

    def test_skips_entries_without_path(self):
        from subtitles.mass_operations import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr', '']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 1
        assert result[0] == ('en', '/path/to/sub.srt')

    def test_short_entries(self):
        from subtitles.mass_operations import _parse_subtitles_column
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
            'metadata': MagicMock(),
        }

    @patch('subtitles.mass_operations.sync_subtitles', return_value=True)
    def test_sync_action(self, mock_sync):
        from subtitles.mass_operations import _process_subtitle_item
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
            track_job_progress=False,
            arr_instance_id=None,
        )

    @patch('subtitles.mass_operations.subtitles_apply_mods')
    def test_mod_action_remove_hi(self, mock_mods):
        from subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'remove_HI', {}, 'test_job')
        assert result is True
        mock_mods.assert_called_once_with('en', '/subs/test.en.srt', ['remove_HI'], '/video/test.mkv')

    @patch('subtitles.mass_operations.subtitles_apply_mods')
    def test_mod_action_ocr_fixes(self, mock_mods):
        from subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'OCR_fixes', {}, 'test_job')
        assert result is True
        mock_mods.assert_called_once_with('en', '/subs/test.en.srt', ['OCR_fixes'], '/video/test.mkv')

    @patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=True)
    def test_translate_action(self, mock_translate):
        from subtitles.mass_operations import _process_subtitle_item
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
            media_type='episode',
            sonarr_series_id=10,
            sonarr_episode_id=1,
            radarr_id=None,
            metadata=item['metadata'],
        )

    @patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=True)
    def test_translate_action_movie(self, mock_translate):
        from subtitles.mass_operations import _process_subtitle_item
        item = self._make_item(sonarr_series_id=None, sonarr_episode_id=None, radarr_id=5)
        result = _process_subtitle_item(item, 'translate', {'from_lang': 'en', 'to_lang': 'fr'}, 'test_job')
        assert result is True
        mock_translate.assert_called_once()
        call_kwargs = mock_translate.call_args[1]
        assert call_kwargs['media_type'] == 'movies'
        assert call_kwargs['radarr_id'] == 5
        assert call_kwargs['metadata'] == item['metadata']
        assert 'job_id' not in call_kwargs

    def test_unknown_action_returns_false(self):
        from subtitles.mass_operations import _process_subtitle_item
        item = self._make_item()
        result = _process_subtitle_item(item, 'nonexistent', {}, 'test_job')
        assert result is False


class TestMassBatchOperationValidation:
    """Test mass_batch_operation input validation and routing."""

    @patch('subtitles.mass_operations.jobs_queue')
    def test_invalid_action_returns_error(self, mock_jobs_queue):
        from subtitles.mass_operations import mass_batch_operation
        result = mass_batch_operation(items=[], action='invalid_action', job_id='test')
        assert result['queued'] == 0
        assert result['skipped'] == 0
        assert len(result['errors']) == 1
        assert 'invalid' in result['errors'][0].lower()

    @patch('subtitles.mass_operations.jobs_queue')
    def test_empty_items_returns_zeros(self, mock_jobs_queue):
        from subtitles.mass_operations import mass_batch_operation
        result = mass_batch_operation(items=[], action='sync', job_id='test')
        assert result['queued'] == 0
        assert result['skipped'] == 0

    @patch('subtitles.mass_operations._collect_subtitle_items')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_sync_action_calls_collect_subtitle_items(self, mock_jobs_queue, mock_collect):
        from subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([], 0)
        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        mass_batch_operation(items=items, action='sync', job_id='test')
        mock_collect.assert_called_once()

    @patch('subtitles.mass_operations._collect_subtitle_items')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_mod_action_calls_collect_subtitle_items(self, mock_jobs_queue, mock_collect):
        from subtitles.mass_operations import mass_batch_operation
        mock_collect.return_value = ([], 0)
        items = [{'type': 'movie', 'radarrId': 10}]
        mass_batch_operation(items=items, action='remove_HI', job_id='test')
        mock_collect.assert_called_once()

    @patch('subtitles.mass_operations._process_media_action')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_scan_disk_calls_process_media_action(self, mock_jobs_queue, mock_process):
        from subtitles.mass_operations import mass_batch_operation
        mock_process.return_value = {'queued': 0, 'skipped': 0, 'errors': []}
        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        mass_batch_operation(items=items, action='scan-disk', job_id='test')
        mock_process.assert_called_once()

    @patch('subtitles.mass_operations._process_media_action')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_search_missing_calls_process_media_action(self, mock_jobs_queue, mock_process):
        from subtitles.mass_operations import mass_batch_operation
        mock_process.return_value = {'queued': 0, 'skipped': 0, 'errors': []}
        items = [{'type': 'movie', 'radarrId': 10}]
        mass_batch_operation(items=items, action='search-missing', job_id='test')
        mock_process.assert_called_once()

    @patch('subtitles.mass_operations.jobs_queue')
    def test_media_action_with_empty_items(self, mock_jobs_queue):
        from subtitles.mass_operations import mass_batch_operation
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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_collects_episode_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                         mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_collects_movie_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                       mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_collects_duplicate_movie_upstream_ids_by_instance(self, mock_settings, mock_db, mock_synced_mov,
                                                               mock_synced_ep, mock_path_map, mock_isfile,
                                                               mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie_a = self._make_movie(path="/movies/a.mkv", subtitles="[['en', '/subs/a.en.srt']]")
        movie_a.arr_instance_id = 1
        movie_b = self._make_movie(path="/movies/b.mkv", subtitles="[['en', '/subs/b.en.srt']]")
        movie_b.arr_instance_id = 2
        mock_db.execute.return_value.all.return_value = [movie_a, movie_b]

        items_list = [
            {'type': 'movie', 'radarrId': 10, 'arr_instance_id': 1},
            {'type': 'movie', 'radarrId': 10, 'arr_instance_id': 2},
        ]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert skipped == 0
        assert [item['arr_instance_id'] for item in items] == [1, 2]
        assert [item['srt_path'] for item in items] == ['/subs/a.en.srt', '/subs/b.en.srt']

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_collects_duplicate_episode_upstream_ids_by_instance(self, mock_settings, mock_db, mock_synced_mov,
                                                                 mock_synced_ep, mock_path_map, mock_isfile,
                                                                 mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode_a = self._make_episode(path="/series/a.mkv", subtitles="[['en', '/subs/a.en.srt']]")
        episode_a.arr_instance_id = 1
        episode_b = self._make_episode(path="/series/b.mkv", subtitles="[['en', '/subs/b.en.srt']]")
        episode_b.arr_instance_id = 2
        mock_db.execute.return_value.all.return_value = [episode_a, episode_b]

        items_list = [
            {'type': 'episode', 'sonarrEpisodeId': 1, 'arr_instance_id': 1},
            {'type': 'episode', 'sonarrEpisodeId': 1, 'arr_instance_id': 2},
        ]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert skipped == 0
        assert [item['arr_instance_id'] for item in items] == [1, 2]
        assert [item['srt_path'] for item in items] == ['/subs/a.en.srt', '/subs/b.en.srt']

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_collects_duplicate_series_upstream_ids_by_instance(self, mock_settings, mock_db, mock_synced_mov,
                                                                mock_synced_ep, mock_path_map, mock_isfile,
                                                                mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode_a = self._make_episode(ep_id=1, series_id=42, path="/series/a.mkv",
                                       subtitles="[['en', '/subs/a.en.srt']]")
        episode_a.arr_instance_id = 1
        episode_b = self._make_episode(ep_id=2, series_id=42, path="/series/b.mkv",
                                       subtitles="[['en', '/subs/b.en.srt']]")
        episode_b.arr_instance_id = 2
        mock_db.execute.return_value.all.return_value = [episode_a, episode_b]

        items_list = [
            {'type': 'series', 'sonarrSeriesId': 42, 'arr_instance_id': 1},
            {'type': 'series', 'sonarrSeriesId': 42, 'arr_instance_id': 2},
        ]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={})

        assert skipped == 0
        assert [item['arr_instance_id'] for item in items] == [1, 2]
        assert [item['sonarr_episode_id'] for item in items] == [1, 2]

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_forced_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                     mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=False)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_missing_files(self, mock_settings, mock_db, mock_synced_mov,
                                  mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths')
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_already_synced_episodes(self, mock_settings, mock_db, mock_synced_mov,
                                            mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_already_synced_movies(self, mock_settings, mock_db, mock_synced_mov,
                                          mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_forced_movie_subtitles(self, mock_settings, mock_db, mock_synced_mov,
                                           mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_forced_subs_allowed_for_mod_actions(self, mock_settings, mock_db, mock_synced_mov,
                                                  mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        """Forced subtitles should be processed by mod actions like OCR_fixes, not skipped."""
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': True, 'hi': False}

        episode = self._make_episode(subtitles="[['en:forced', '/subs/ep1.en.forced.srt']]")
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='OCR_fixes', options={})

        assert len(items) == 1
        assert skipped == 0
        assert items[0]['forced'] is True

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=False)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_missing_movie_files(self, mock_settings, mock_db, mock_synced_mov,
                                        mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_series_type_collects_episodes(self, mock_settings, mock_db, mock_synced_mov,
                                            mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        """Passing type='series' with sonarrSeriesId collects all episodes for that series."""
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.select')
    def test_get_synced_episode_paths(self, mock_select, mock_db):
        from subtitles.mass_operations import _get_synced_episode_paths

        row1 = MagicMock()
        row1.subtitles_path = '/subs/ep1.srt'
        row2 = MagicMock()
        row2.subtitles_path = None
        row3 = MagicMock()
        row3.subtitles_path = '/subs/ep2.srt'
        mock_db.execute.return_value.all.return_value = [row1, row2, row3]

        result = _get_synced_episode_paths()
        assert result == {'/subs/ep1.srt', '/subs/ep2.srt'}

    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.select')
    def test_get_synced_movie_paths(self, mock_select, mock_db):
        from subtitles.mass_operations import _get_synced_movie_paths

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

    @patch('subtitles.mass_operations.series_scan_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_scan_disk_series(self, mock_jobs_queue, mock_scan):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(1)
        assert result['queued'] == 1

    @patch('subtitles.mass_operations.movies_scan_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_scan_disk_movies(self, mock_jobs_queue, mock_scan):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'movie', 'radarrId': 10}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(10)
        assert result['queued'] == 1

    @patch('subtitles.mass_operations.series_download_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_search_missing_series(self, mock_jobs_queue, mock_download):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = _process_media_action(items, action='search-missing', job_id='test')

        mock_download.assert_called_once_with(1, arr_instance_id=None)
        assert result['queued'] == 1

    @patch('subtitles.mass_operations.movies_download_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_search_missing_movies(self, mock_jobs_queue, mock_download):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'movie', 'radarrId': 10}]
        result = _process_media_action(items, action='search-missing', job_id='test')

        mock_download.assert_called_once_with(10, arr_instance_id=None)
        assert result['queued'] == 1

    @patch('subtitles.mass_operations.series_scan_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_scan_disk_series_passes_arr_instance_id(self, mock_jobs_queue, mock_scan):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'series', 'sonarrSeriesId': 1, 'arr_instance_id': 7}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(1, arr_instance_id=7)
        assert result['queued'] == 1

    @patch('subtitles.mass_operations.movies_scan_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_scan_disk_movies_passes_arr_instance_id(self, mock_jobs_queue, mock_scan):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'movie', 'radarrId': 10, 'arr_instance_id': 8}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(10, arr_instance_id=8)
        assert result['queued'] == 1

    @patch('subtitles.mass_operations.upgrade_movies_subtitles')
    @patch('subtitles.mass_operations.upgrade_episodes_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_upgrade_passes_instance_scoped_filters(self, mock_jobs_queue, mock_upgrade_series, mock_upgrade_movies):
        from subtitles.mass_operations import _process_media_action

        items = [
            {'type': 'series', 'sonarrSeriesId': 1, 'arr_instance_id': 2},
            {'type': 'series', 'sonarrSeriesId': 1, 'arr_instance_id': 3},
            {'type': 'movie', 'radarrId': 10, 'arr_instance_id': 4},
            {'type': 'movie', 'radarrId': 10, 'arr_instance_id': 5},
        ]
        result = _process_media_action(items, action='upgrade', job_id='test')

        mock_upgrade_series.assert_called_once_with(
            job_id='test',
            sonarr_series_filters=[(1, 2), (1, 3)],
        )
        mock_upgrade_movies.assert_called_once_with(
            job_id='test',
            radarr_filters=[(10, 4), (10, 5)],
        )
        assert result['queued'] == 4
        assert result['errors'] == []

    @patch('subtitles.mass_operations.series_scan_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_error_handling(self, mock_jobs_queue, mock_scan):
        from subtitles.mass_operations import _process_media_action

        mock_scan.side_effect = RuntimeError("scan failed")
        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        assert len(result['errors']) == 1
        assert 'scan failed' in result['errors'][0]

    @patch('subtitles.mass_operations.jobs_queue')
    def test_unknown_type_skipped_scan_disk(self, mock_jobs_queue):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'unknown', 'id': 1}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        assert result['skipped'] == 1
        assert result['queued'] == 0

    @patch('subtitles.mass_operations.jobs_queue')
    def test_unknown_type_skipped_search_missing(self, mock_jobs_queue):
        from subtitles.mass_operations import _process_media_action

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths')
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_force_resync_collects_already_synced(self, mock_settings, mock_db, mock_synced_mov,
                                                   mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations._get_synced_episode_paths', return_value=set())
    @patch('subtitles.mass_operations._get_synced_movie_paths', return_value=set())
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_force_resync_still_skips_generated_sync_outputs(self, mock_settings, mock_db, mock_synced_mov,
                                                             mock_synced_ep, mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode(subtitles="[['en:sync-ffsubsync', '/subs/ep1.en.ffsubsync.srt']]")
        mock_db.execute.return_value.all.return_value = [episode]

        items_list = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        items, skipped = _collect_subtitle_items(items_list, action='sync', options={'force_resync': True})

        assert items == []
        assert skipped == 1


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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_episode_when_target_lang_exists(self, mock_settings, mock_db,
                                                    mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_collects_episode_when_target_lang_missing(self, mock_settings, mock_db,
                                                        mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_skips_movie_when_target_lang_exists(self, mock_settings, mock_db,
                                                  mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

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


class TestTranslateSourceLanguageFilter:
    """Test that batch translate only queues subtitles matching the requested source language."""

    def _make_episode(self, ep_id=1, series_id=10, path='/video/ep1.mkv',
                      subtitles="[['en', '/subs/ep1.en.srt'], ['fr', '/subs/ep1.fr.srt']]"):
        ep = MagicMock()
        ep.sonarrEpisodeId = ep_id
        ep.sonarrSeriesId = series_id
        ep.path = path
        ep.subtitles = subtitles
        return ep

    def _make_movie(self, radarr_id=10, path='/video/movie.mkv',
                    subtitles="[['en', '/subs/movie.en.srt'], ['fr', '/subs/movie.fr.srt']]"):
        movie = MagicMock()
        movie.radarrId = radarr_id
        movie.path = path
        movie.subtitles = subtitles
        return movie

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_translate_only_queues_source_language_episodes(self, mock_settings, mock_db,
                                                            mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = False
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        # Episode has both EN and FR subtitles
        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        # Request EN->HU translation: only EN subtitle should be queued
        collected, skipped = _collect_subtitle_items(items, 'translate', {'from_lang': 'en', 'to_lang': 'hu'})

        assert len(collected) == 1
        assert collected[0]['srt_lang'] == 'en'
        assert skipped == 1  # FR subtitle skipped

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_translate_only_queues_source_language_movies(self, mock_settings, mock_db,
                                                          mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = False
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        # Movie has both EN and FR subtitles
        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items = [{'type': 'movie', 'radarrId': 10}]
        # Request EN->HU: only EN should be queued
        collected, skipped = _collect_subtitle_items(items, 'translate', {'from_lang': 'en', 'to_lang': 'hu'})

        assert len(collected) == 1
        assert collected[0]['srt_lang'] == 'en'
        assert skipped == 1  # FR skipped

    @patch('subtitles.mass_operations.languages_from_colon_seperated_string')
    @patch('subtitles.mass_operations.os.path.isfile', return_value=True)
    @patch('subtitles.mass_operations.path_mappings')
    @patch('subtitles.mass_operations.database')
    @patch('subtitles.mass_operations.settings')
    def test_translate_without_source_lang_queues_all(self, mock_settings, mock_db,
                                                       mock_path_map, mock_isfile, mock_lang):
        from subtitles.mass_operations import _collect_subtitle_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = False
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items = [{'type': 'episode', 'sonarrEpisodeId': 1}]
        # No from_lang specified: all subtitles should be queued
        collected, skipped = _collect_subtitle_items(items, 'translate', {'to_lang': 'hu'})

        assert len(collected) == 2
        assert skipped == 0


class TestTranslateDefaultOptions:
    """Test translate action uses defaults when options omit from_lang/to_lang."""

    @patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=True)
    def test_translate_default_options(self, mock_translate):
        from subtitles.mass_operations import _process_subtitle_item
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
            'metadata': MagicMock(),
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

    @patch('subtitles.mass_operations.series_scan_subtitles')
    @patch('subtitles.mass_operations.jobs_queue')
    def test_scan_disk_episode_type(self, mock_jobs_queue, mock_scan):
        from subtitles.mass_operations import _process_media_action

        items = [{'type': 'episode', 'sonarrSeriesId': 5}]
        result = _process_media_action(items, action='scan-disk', job_id='test')

        mock_scan.assert_called_once_with(5)
        assert result['queued'] == 1
        assert result['skipped'] == 0


class TestSchedulerIntegration:
    """Test scheduler integration when items=None."""

    @patch('subtitles.mass_operations._collect_subtitle_items')
    @patch('subtitles.mass_operations.jobs_queue')
    @patch('subtitles.mass_operations.settings')
    def test_items_none_with_job_id_syncs_entire_library(self, mock_settings, mock_jobs_queue,
                                                          mock_collect):
        from subtitles.mass_operations import mass_batch_operation

        mock_settings.general.use_sonarr = True
        mock_settings.general.use_radarr = True
        mock_collect.return_value = ([], 0)

        result = mass_batch_operation(items=None, action='sync', job_id='test')  # noqa: F841

        # When items=None, _collect_subtitle_items should be called with items=None
        mock_collect.assert_called_once()
        args = mock_collect.call_args
        assert args[0][0] is None  # items arg should be None

    @patch('subtitles.mass_operations.jobs_queue')
    def test_no_job_id_requeues_via_jobs_queue(self, mock_jobs_queue):
        """When called without job_id (e.g. from scheduler), should re-queue itself
        via add_job_from_function instead of processing inline."""
        from subtitles.mass_operations import mass_batch_operation

        result = mass_batch_operation(items=None, action='sync', job_id=None)

        # Should return None (re-queued, no inline processing)
        assert result is None
        # Should have called add_job_from_function to re-queue with a real job_id
        mock_jobs_queue.add_job_from_function.assert_called_once()
        call_args = mock_jobs_queue.add_job_from_function.call_args
        assert 'Mass Sync' in call_args[0][0]
        assert call_args[1]['is_progress'] is True


class TestMassBatchOperationProcessing:
    """Test mass_batch_operation end-to-end processing loop."""

    @patch('subtitles.mass_operations.jobs_queue')
    @patch('subtitles.mass_operations._process_subtitle_item', return_value=True)
    @patch('subtitles.mass_operations._collect_subtitle_items')
    def test_processes_collected_items(self, mock_collect, mock_process, mock_jq):
        from subtitles.mass_operations import mass_batch_operation
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

    @patch('subtitles.mass_operations.jobs_queue')
    @patch('subtitles.mass_operations._collect_subtitle_items')
    def test_sync_progress_does_not_complete_before_item_finishes(self, mock_collect, mock_jq):
        from subtitles.mass_operations import mass_batch_operation

        mock_collect.return_value = ([
            {'srt_path': '/subs/test.srt', 'video_path': '/video/test.mkv'},
        ], 0)
        progress_calls_before_processing = []

        def process_item(*args, **kwargs):
            progress_calls_before_processing.extend(mock_jq.update_job_progress.call_args_list)
            return True

        with patch('subtitles.mass_operations._process_subtitle_item', side_effect=process_item):
            result = mass_batch_operation(
                items=[{'type': 'movie', 'radarrId': 1}],
                action='sync',
                job_id='test',
            )

        assert result['queued'] == 1
        assert any(
            progress_call.kwargs.get('progress_value') == 0
            and progress_call.kwargs.get('progress_message') == 'sync: test.srt (1/1)'
            for progress_call in progress_calls_before_processing
        )
        assert not any(
            progress_call.kwargs.get('progress_value') == 1
            for progress_call in progress_calls_before_processing
        )

    @patch('subtitles.mass_operations.jobs_queue')
    @patch('subtitles.mass_operations._process_subtitle_item', side_effect=Exception("failed"))
    @patch('subtitles.mass_operations._collect_subtitle_items')
    def test_handles_processing_errors(self, mock_collect, mock_process, mock_jq):
        from subtitles.mass_operations import mass_batch_operation
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

    @patch('subtitles.mass_operations.jobs_queue')
    @patch('subtitles.mass_operations._process_subtitle_item', return_value=False)
    @patch('subtitles.mass_operations._collect_subtitle_items')
    def test_counts_failed_processing(self, mock_collect, mock_process, mock_jq):
        from subtitles.mass_operations import mass_batch_operation
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

    @patch('subtitles.mass_operations.jobs_queue')
    @patch('subtitles.mass_operations._process_subtitle_item', return_value=True)
    @patch('subtitles.mass_operations._collect_subtitle_items')
    def test_processes_multiple_items(self, mock_collect, mock_process, mock_jq):
        from subtitles.mass_operations import mass_batch_operation
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

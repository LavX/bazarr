# coding=utf-8

from unittest.mock import patch, MagicMock


class TestParseSubtitlesColumn:
    """Test _parse_subtitles_column function."""

    def test_empty_input(self):
        from bazarr.subtitles.mass_sync import _parse_subtitles_column
        assert _parse_subtitles_column(None) == []
        assert _parse_subtitles_column('') == []

    def test_valid_subtitles(self):
        from bazarr.subtitles.mass_sync import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr:forced', '/path/to/sub.fr.srt']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 2
        assert result[0] == ('en', '/path/to/sub.srt')
        assert result[1] == ('fr:forced', '/path/to/sub.fr.srt')

    def test_skips_entries_without_path(self):
        from bazarr.subtitles.mass_sync import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr', '']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 1
        assert result[0] == ('en', '/path/to/sub.srt')

    def test_skips_entries_with_none_path(self):
        from bazarr.subtitles.mass_sync import _parse_subtitles_column
        raw = "[['en', '/path/to/sub.srt'], ['fr', None]]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 1

    def test_invalid_syntax(self):
        from bazarr.subtitles.mass_sync import _parse_subtitles_column
        assert _parse_subtitles_column('not valid python') == []

    def test_short_entries(self):
        from bazarr.subtitles.mass_sync import _parse_subtitles_column
        raw = "[['en']]"
        result = _parse_subtitles_column(raw)
        assert len(result) == 0


class TestCollectEpisodeItems:
    """Test _collect_episode_items function."""

    def _make_episode(self, ep_id=1, series_id=10, path='/video/ep1.mkv',
                      subtitles="[['en', '/subs/ep1.en.srt']]"):
        ep = MagicMock()
        ep.sonarrEpisodeId = ep_id
        ep.sonarrSeriesId = series_id
        ep.path = path
        ep.subtitles = subtitles
        return ep

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_collects_valid_subtitle(self, mock_settings, mock_db, mock_synced,
                                      mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1])

        assert len(items) == 1
        assert skipped == 0
        assert items[0]['srt_path'] == '/subs/ep1.en.srt'
        assert items[0]['sonarr_episode_id'] == 1
        assert items[0]['sonarr_series_id'] == 10
        assert items[0]['radarr_id'] is None

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_forced_subtitles(self, mock_settings, mock_db, mock_synced,
                                     mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': True, 'hi': False}

        episode = self._make_episode(subtitles="[['en:forced', '/subs/ep1.en.forced.srt']]")
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1])

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=False)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_missing_files(self, mock_settings, mock_db, mock_synced,
                                  mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1])

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_already_synced(self, mock_settings, mock_db, mock_synced,
                                   mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_synced.return_value = {'/subs/ep1.en.srt'}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1])

        assert len(items) == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_force_resync_ignores_history(self, mock_settings, mock_db, mock_synced,
                                           mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1], force_resync=True)

        assert len(items) == 1
        mock_synced.assert_not_called()

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_options_override_settings(self, mock_settings, mock_db, mock_synced,
                                        mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = False
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        options = {'max_offset_seconds': 120, 'gss': False, 'no_fix_framerate': True}
        items, _ = _collect_episode_items(episode_ids=[1], options=options)

        assert items[0]['max_offset_seconds'] == '120'
        assert items[0]['gss'] is False
        assert items[0]['no_fix_framerate'] is True

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_multiple_subtitles_per_episode(self, mock_settings, mock_db, mock_synced,
                                             mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode(
            subtitles="[['en', '/subs/ep1.en.srt'], ['fr', '/subs/ep1.fr.srt']]"
        )
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1])

        assert len(items) == 2

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_no_subtitles_returns_empty(self, mock_settings, mock_db, mock_synced,
                                         mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_episode_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x

        episode = self._make_episode(subtitles="[]")
        mock_db.execute.return_value.all.return_value = [episode]

        items, skipped = _collect_episode_items(episode_ids=[1])

        assert len(items) == 0
        assert skipped == 0


class TestCollectMovieItems:
    """Test _collect_movie_items function."""

    def _make_movie(self, radarr_id=10, path='/video/movie.mkv',
                    subtitles="[['en', '/subs/movie.en.srt']]"):
        movie = MagicMock()
        movie.radarrId = radarr_id
        movie.path = path
        movie.subtitles = subtitles
        return movie

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_collects_valid_subtitle(self, mock_settings, mock_db, mock_synced,
                                      mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_movie_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items, skipped = _collect_movie_items(movie_ids=[10])

        assert len(items) == 1
        assert skipped == 0
        assert items[0]['srt_path'] == '/subs/movie.en.srt'
        assert items[0]['radarr_id'] == 10
        assert items[0]['sonarr_series_id'] is None
        assert items[0]['sonarr_episode_id'] is None

    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_force_resync_ignores_history(self, mock_settings, mock_db, mock_synced,
                                           mock_path_map, mock_isfile, mock_lang):
        from bazarr.subtitles.mass_sync import _collect_movie_items

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        items, skipped = _collect_movie_items(movie_ids=[10], force_resync=True)

        assert len(items) == 1
        mock_synced.assert_not_called()


class TestMassSyncSubtitles:
    """Test mass_sync_subtitles entry point."""

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    def test_schedules_job_when_no_job_id_and_no_items(self, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        result = mass_sync_subtitles(items=None, options=None, job_id=None)
        mock_jobs_queue.add_job_from_function.assert_called_once_with(
            "Mass Syncing All Subtitles", is_progress=True
        )
        assert result is None

    @patch('bazarr.subtitles.mass_sync.sync_subtitles', return_value=True)
    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_syncs_entire_library(self, mock_settings, mock_collect_ep, mock_collect_mov,
                                   mock_jobs_queue, mock_sync):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_settings.general.use_sonarr = True
        mock_settings.general.use_radarr = True

        ep_items = [{'video_path': '/v1.mkv', 'srt_path': '/s1.srt', 'srt_lang': 'en',
                     'forced': False, 'hi': False, 'sonarr_series_id': 1,
                     'sonarr_episode_id': 1, 'radarr_id': None,
                     'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True}]
        mov_items = [{'video_path': '/v2.mkv', 'srt_path': '/s2.srt', 'srt_lang': 'en',
                      'forced': False, 'hi': False, 'sonarr_series_id': None,
                      'sonarr_episode_id': None, 'radarr_id': 1,
                      'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True}]
        mock_collect_ep.return_value = (ep_items, 2)
        mock_collect_mov.return_value = (mov_items, 1)

        result = mass_sync_subtitles(items=None, options={}, job_id='test')

        assert result['queued'] == 2  # 2 synced successfully
        assert result['skipped'] == 3  # 2 + 1 from collection phase
        assert mock_sync.call_count == 2

    @patch('bazarr.subtitles.mass_sync.sync_subtitles', return_value=True)
    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_sonarr_when_disabled(self, mock_settings, mock_collect_ep, mock_collect_mov,
                                         mock_jobs_queue, mock_sync):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = True
        mock_collect_mov.return_value = ([], 0)

        mass_sync_subtitles(items=None, options={}, job_id='test')
        mock_collect_ep.assert_not_called()
        mock_collect_mov.assert_called_once()

    @patch('bazarr.subtitles.mass_sync.sync_subtitles', return_value=True)
    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    def test_routes_items_by_type(self, mock_collect_ep, mock_collect_mov,
                                   mock_jobs_queue, mock_sync):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_collect_ep.return_value = ([], 0)
        mock_collect_mov.return_value = ([], 0)

        items = [
            {'type': 'series', 'sonarrSeriesId': 1},
            {'type': 'movie', 'radarrId': 10},
            {'type': 'episode', 'sonarrEpisodeId': 100},
        ]
        mass_sync_subtitles(items=items, options={}, job_id='test')

        call_kwargs = mock_collect_ep.call_args[1]
        assert call_kwargs['series_ids'] == [1]
        assert call_kwargs['episode_ids'] == [100]

        call_kwargs = mock_collect_mov.call_args[1]
        assert call_kwargs['movie_ids'] == [10]

    @patch('bazarr.subtitles.mass_sync.sync_subtitles')
    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    def test_counts_failed_syncs(self, mock_collect_ep, mock_collect_mov,
                                  mock_jobs_queue, mock_sync):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_sync.side_effect = [True, False]  # first succeeds, second fails
        mock_collect_ep.return_value = ([
            {'video_path': '/v1.mkv', 'srt_path': '/s1.srt', 'srt_lang': 'en',
             'forced': False, 'hi': False, 'sonarr_series_id': 1,
             'sonarr_episode_id': 1, 'radarr_id': None,
             'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True},
            {'video_path': '/v2.mkv', 'srt_path': '/s2.srt', 'srt_lang': 'en',
             'forced': False, 'hi': False, 'sonarr_series_id': 1,
             'sonarr_episode_id': 2, 'radarr_id': None,
             'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True},
        ], 0)

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')

        assert result['queued'] == 1  # 1 synced
        assert result['skipped'] == 1  # 1 failed

    @patch('bazarr.subtitles.mass_sync.sync_subtitles')
    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    def test_handles_sync_exception(self, mock_collect_ep, mock_collect_mov,
                                     mock_jobs_queue, mock_sync):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_sync.side_effect = RuntimeError("ffsubsync crashed")
        mock_collect_ep.return_value = ([
            {'video_path': '/v1.mkv', 'srt_path': '/s1.srt', 'srt_lang': 'en',
             'forced': False, 'hi': False, 'sonarr_series_id': 1,
             'sonarr_episode_id': 1, 'radarr_id': None,
             'max_offset_seconds': '60', 'no_fix_framerate': True, 'gss': True},
        ], 0)

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')

        assert result['queued'] == 0
        assert len(result['errors']) == 1
        assert 'ffsubsync crashed' in result['errors'][0]

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    def test_updates_progress(self, mock_collect_ep, mock_collect_mov, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_collect_ep.return_value = ([], 0)

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        mass_sync_subtitles(items=items, options={}, job_id='test')

        mock_jobs_queue.update_job_progress.assert_called()

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync._collect_movie_items')
    @patch('bazarr.subtitles.mass_sync._collect_episode_items')
    def test_unknown_item_type_is_ignored(self, mock_collect_ep, mock_collect_mov, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles

        items = [{'type': 'unknown', 'id': 99}]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')

        mock_collect_ep.assert_not_called()
        mock_collect_mov.assert_not_called()
        assert result['queued'] == 0

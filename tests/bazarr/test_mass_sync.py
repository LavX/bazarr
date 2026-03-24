# coding=utf-8

import pytest
from unittest.mock import patch, MagicMock, call


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


class TestMassSyncSubtitles:
    """Test mass_sync_subtitles entry point."""

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    def test_schedules_job_when_no_job_id_and_no_items(self, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        result = mass_sync_subtitles(items=None, options=None, job_id=None)
        mock_jobs_queue.add_job_from_function.assert_called_once_with(
            "Mass Syncing All Subtitles", is_progress=False
        )
        assert result is None

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_syncs_entire_library_when_no_items(self, mock_settings, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_settings.general.use_sonarr = True
        mock_settings.general.use_radarr = True
        mock_proc_ep.return_value = (5, 2, [])
        mock_proc_mov.return_value = (3, 1, [])

        result = mass_sync_subtitles(items=None, options={}, job_id='test')
        assert result == {'queued': 8, 'skipped': 3, 'errors': []}
        mock_proc_ep.assert_called_once()
        mock_proc_mov.assert_called_once()

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_sonarr_when_disabled(self, mock_settings, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_settings.general.use_sonarr = False
        mock_settings.general.use_radarr = True
        mock_proc_mov.return_value = (1, 0, [])

        result = mass_sync_subtitles(items=None, options={}, job_id='test')
        mock_proc_ep.assert_not_called()
        mock_proc_mov.assert_called_once()
        assert result['queued'] == 1

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_radarr_when_disabled(self, mock_settings, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_settings.general.use_sonarr = True
        mock_settings.general.use_radarr = False
        mock_proc_ep.return_value = (2, 0, [])

        result = mass_sync_subtitles(items=None, options={}, job_id='test')
        mock_proc_mov.assert_not_called()
        mock_proc_ep.assert_called_once()
        assert result['queued'] == 2

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    def test_routes_items_by_type(self, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_proc_ep.return_value = (2, 0, [])
        mock_proc_mov.return_value = (1, 0, [])

        items = [
            {'type': 'series', 'sonarrSeriesId': 1},
            {'type': 'movie', 'radarrId': 10},
            {'type': 'episode', 'sonarrEpisodeId': 100},
        ]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')

        mock_proc_ep.assert_called_once()
        call_kwargs = mock_proc_ep.call_args
        assert call_kwargs[1]['series_ids'] == [1]
        assert call_kwargs[1]['episode_ids'] == [100]

        mock_proc_mov.assert_called_once()
        call_kwargs = mock_proc_mov.call_args
        assert call_kwargs[1]['movie_ids'] == [10]

        assert result['queued'] == 3

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    def test_passes_force_resync_option(self, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_proc_ep.return_value = (0, 0, [])

        items = [{'type': 'series', 'sonarrSeriesId': 1}]
        mass_sync_subtitles(items=items, options={'force_resync': True}, job_id='test')

        call_kwargs = mock_proc_ep.call_args
        assert call_kwargs[1]['force_resync'] is True

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    def test_aggregates_errors(self, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_proc_ep.return_value = (1, 0, ['error1'])
        mock_proc_mov.return_value = (1, 0, ['error2'])

        items = [
            {'type': 'series', 'sonarrSeriesId': 1},
            {'type': 'movie', 'radarrId': 1},
        ]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')
        assert result['errors'] == ['error1', 'error2']

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    def test_only_episodes_no_movies_called_for_series_items(self, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles
        mock_proc_ep.return_value = (1, 0, [])

        items = [{'type': 'series', 'sonarrSeriesId': 5}]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')

        mock_proc_ep.assert_called_once()
        mock_proc_mov.assert_not_called()
        assert result['queued'] == 1

    @patch('bazarr.subtitles.mass_sync._process_movies')
    @patch('bazarr.subtitles.mass_sync._process_episodes')
    def test_unknown_item_type_is_ignored(self, mock_proc_ep, mock_proc_mov):
        from bazarr.subtitles.mass_sync import mass_sync_subtitles

        items = [{'type': 'unknown', 'id': 99}]
        result = mass_sync_subtitles(items=items, options={}, job_id='test')

        mock_proc_ep.assert_not_called()
        mock_proc_mov.assert_not_called()
        assert result == {'queued': 0, 'skipped': 0, 'errors': []}


class TestProcessEpisodes:
    """Test _process_episodes function."""

    def _make_episode(self, ep_id=1, series_id=10, path='/video/ep1.mkv',
                      subtitles="[['en', '/subs/ep1.en.srt']]"):
        ep = MagicMock()
        ep.sonarrEpisodeId = ep_id
        ep.sonarrSeriesId = series_id
        ep.path = path
        ep.subtitles = subtitles
        return ep

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_queues_sync_for_valid_subtitle(self, mock_settings, mock_db, mock_synced,
                                             mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 1
        assert skipped == 0
        assert errors == []
        mock_jobs_queue.feed_jobs_pending_queue.assert_called_once()
        kwargs = mock_jobs_queue.feed_jobs_pending_queue.call_args[1]['kwargs']
        assert kwargs['srt_path'] == '/subs/ep1.en.srt'
        assert kwargs['force_sync'] is True
        assert kwargs['percent_score'] == 0
        assert kwargs['sonarr_episode_id'] == 1
        assert kwargs['sonarr_series_id'] == 10
        assert kwargs['radarr_id'] is None

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_forced_subtitles(self, mock_settings, mock_db, mock_synced,
                                     mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': True, 'hi': False}

        episode = self._make_episode(subtitles="[['en:forced', '/subs/ep1.en.forced.srt']]")
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 0
        assert skipped == 1
        mock_jobs_queue.feed_jobs_pending_queue.assert_not_called()

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=False)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_missing_files(self, mock_settings, mock_db, mock_synced,
                                  mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 0
        assert skipped == 1
        mock_jobs_queue.feed_jobs_pending_queue.assert_not_called()

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_already_synced(self, mock_settings, mock_db, mock_synced,
                                   mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_synced.return_value = {'/subs/ep1.en.srt'}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 0
        assert skipped == 1
        mock_jobs_queue.feed_jobs_pending_queue.assert_not_called()

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_force_resync_ignores_history(self, mock_settings, mock_db, mock_synced,
                                           mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1], force_resync=True)

        assert queued == 1
        mock_synced.assert_not_called()

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_options_override_settings(self, mock_settings, mock_db, mock_synced,
                                        mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = False
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        options = {'max_offset_seconds': 120, 'gss': False, 'no_fix_framerate': True}
        _process_episodes(episode_ids=[1], options=options)

        kwargs = mock_jobs_queue.feed_jobs_pending_queue.call_args[1]['kwargs']
        assert kwargs['max_offset_seconds'] == '120'
        assert kwargs['gss'] is False
        assert kwargs['no_fix_framerate'] is True

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_queue_exception_adds_to_errors(self, mock_settings, mock_db, mock_synced,
                                             mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x
        mock_path_map.path_replace_reverse.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_jobs_queue.feed_jobs_pending_queue.side_effect = RuntimeError("queue full")

        episode = self._make_episode()
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 0
        assert skipped == 1
        assert len(errors) == 1
        assert 'queue full' in errors[0]

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_multiple_subtitles_per_episode(self, mock_settings, mock_db, mock_synced,
                                             mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

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

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 2
        assert mock_jobs_queue.feed_jobs_pending_queue.call_count == 2

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_episode_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_no_subtitles_returns_zero_counts(self, mock_settings, mock_db, mock_synced,
                                               mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_episodes

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace.side_effect = lambda x: x

        episode = self._make_episode(subtitles="[]")
        mock_db.execute.return_value.all.return_value = [episode]

        queued, skipped, errors = _process_episodes(episode_ids=[1])

        assert queued == 0
        assert skipped == 0
        assert errors == []
        mock_jobs_queue.feed_jobs_pending_queue.assert_not_called()


class TestProcessMovies:
    """Test _process_movies function."""

    def _make_movie(self, radarr_id=10, path='/video/movie.mkv',
                    subtitles="[['en', '/subs/movie.en.srt']]"):
        movie = MagicMock()
        movie.radarrId = radarr_id
        movie.path = path
        movie.subtitles = subtitles
        return movie

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_queues_sync_for_valid_subtitle(self, mock_settings, mock_db, mock_synced,
                                             mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_movies

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        queued, skipped, errors = _process_movies(movie_ids=[10])

        assert queued == 1
        assert skipped == 0
        assert errors == []
        mock_jobs_queue.feed_jobs_pending_queue.assert_called_once()
        kwargs = mock_jobs_queue.feed_jobs_pending_queue.call_args[1]['kwargs']
        assert kwargs['srt_path'] == '/subs/movie.en.srt'
        assert kwargs['radarr_id'] == 10
        assert kwargs['sonarr_series_id'] is None
        assert kwargs['sonarr_episode_id'] is None

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_forced_subtitles(self, mock_settings, mock_db, mock_synced,
                                     mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_movies

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': True, 'hi': False}

        movie = self._make_movie(subtitles="[['en:forced', '/subs/movie.en.forced.srt']]")
        mock_db.execute.return_value.all.return_value = [movie]

        queued, skipped, errors = _process_movies(movie_ids=[10])

        assert queued == 0
        assert skipped == 1
        mock_jobs_queue.feed_jobs_pending_queue.assert_not_called()

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=False)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths', return_value=set())
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_missing_files(self, mock_settings, mock_db, mock_synced,
                                  mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_movies

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        queued, skipped, errors = _process_movies(movie_ids=[10])

        assert queued == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_skips_already_synced(self, mock_settings, mock_db, mock_synced,
                                   mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_movies

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}
        mock_synced.return_value = {'/subs/movie.en.srt'}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        queued, skipped, errors = _process_movies(movie_ids=[10])

        assert queued == 0
        assert skipped == 1

    @patch('bazarr.subtitles.mass_sync.jobs_queue')
    @patch('bazarr.subtitles.mass_sync.languages_from_colon_seperated_string')
    @patch('bazarr.subtitles.mass_sync.os.path.isfile', return_value=True)
    @patch('bazarr.subtitles.mass_sync.path_mappings')
    @patch('bazarr.subtitles.mass_sync._get_synced_movie_paths')
    @patch('bazarr.subtitles.mass_sync.database')
    @patch('bazarr.subtitles.mass_sync.settings')
    def test_force_resync_ignores_history(self, mock_settings, mock_db, mock_synced,
                                           mock_path_map, mock_isfile, mock_lang, mock_jobs_queue):
        from bazarr.subtitles.mass_sync import _process_movies

        mock_settings.subsync.max_offset_seconds = 60
        mock_settings.subsync.gss = True
        mock_settings.subsync.no_fix_framerate = True
        mock_path_map.path_replace_movie.side_effect = lambda x: x
        mock_path_map.path_replace_reverse_movie.side_effect = lambda x: x
        mock_lang.return_value = {'language': 'en', 'forced': False, 'hi': False}

        movie = self._make_movie()
        mock_db.execute.return_value.all.return_value = [movie]

        queued, skipped, errors = _process_movies(movie_ids=[10], force_resync=True)

        assert queued == 1
        mock_synced.assert_not_called()

# coding=utf-8

from types import SimpleNamespace
from unittest.mock import Mock, patch


def _missing_language(alpha3, hi=False, forced=False):
    return SimpleNamespace(alpha3=alpha3, hi=hi, forced=forced)


def test_downloaded_series_subtitle_queues_episode_translation():
    from subtitles.processing import _trigger_auto_translation

    mock_database = Mock()
    mock_database.execute.return_value.first.return_value = SimpleNamespace(
        sonarrSeriesId=10,
        season=1,
        episode=1,
        imdbId='tt1',
        tvdbId='1',
        profileId=1,
    )

    with (
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profile_id', return_value=1),
        patch('app.database.get_profiles_list') as mock_profiles,
        patch('app.database.database', mock_database),
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.processing.alpha2_from_alpha3', return_value='hu'),
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 0
        mock_profiles.return_value = {
            'items': [
                {
                    'language': 'hu',
                    'translate_from': 'en',
                    'forced': 'False',
                    'hi': 'False',
                }
            ]
        }
        mock_missing.return_value = [_missing_language('hun')]

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.srt',
            video_path='/video/episode.mkv',
            media_type='series',
            series_id=10,
            episode_id=20,
            source_score_percent=100,
        )

    mock_translate.assert_called_once()
    assert mock_translate.call_args.kwargs['media_type'] == 'episode'


def test_downloaded_subtitle_does_not_translate_for_mismatched_target_variant():
    from subtitles.processing import _trigger_auto_translation

    mock_database = Mock()
    mock_database.execute.return_value.first.return_value = SimpleNamespace(
        imdbId='tt2',
        tmdbId='2',
        profileId=1,
    )

    with (
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profile_id', return_value=1),
        patch('app.database.get_profiles_list') as mock_profiles,
        patch('app.database.database', mock_database),
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.processing.alpha2_from_alpha3', return_value='hu'),
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 0
        mock_profiles.return_value = {
            'items': [
                {
                    'language': 'hu',
                    'translate_from': 'en',
                    'forced': 'True',
                    'hi': 'False',
                }
            ]
        }
        mock_missing.return_value = [_missing_language('hun')]

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.srt',
            video_path='/video/movie.mkv',
            media_type='movie',
            radarr_id=30,
            source_score_percent=100,
        )

    mock_translate.assert_not_called()


def test_downloaded_series_subtitle_scopes_translation_metadata_to_instance(schema_session, monkeypatch):
    import app.database as database_module
    from app.database import TableEpisodes, TableLanguagesProfiles, TableShows
    from subtitles.processing import _trigger_auto_translation

    schema_session.add_all([
        TableLanguagesProfiles(profileId=1, name='Default', items='[]'),
        TableLanguagesProfiles(profileId=2, name='Anime', items='[]'),
        TableShows(
            id=100,
            sonarrSeriesId=10,
            arr_instance_id=1,
            path='/series/default',
            title='Default',
            imdbId='tt-default',
            tvdbId=100,
            profileId=1,
            tags='[]',
        ),
        TableShows(
            id=200,
            sonarrSeriesId=10,
            arr_instance_id=2,
            path='/series/anime',
            title='Anime',
            imdbId='tt-anime',
            tvdbId=200,
            profileId=2,
            tags='[]',
        ),
    ])
    schema_session.flush()
    schema_session.add_all([
        TableEpisodes(
            id=101,
            series_id=100,
            sonarrSeriesId=10,
            sonarrEpisodeId=20,
            arr_instance_id=1,
            path='/series/default/s01e01.mkv',
            title='Pilot',
            season=1,
            episode=1,
            monitored='True',
            subtitles='[]',
        ),
        TableEpisodes(
            id=201,
            series_id=200,
            sonarrSeriesId=10,
            sonarrEpisodeId=20,
            arr_instance_id=2,
            path='/series/anime/s01e01.mkv',
            title='Pilot',
            season=1,
            episode=1,
            monitored='True',
            subtitles='[]',
        ),
    ])
    schema_session.flush()
    monkeypatch.setattr(database_module, 'database', schema_session)

    with (
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profiles_list') as mock_profiles,
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.processing.alpha2_from_alpha3', return_value='hu'),
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 0
        mock_profiles.return_value = {
            'items': [
                {
                    'language': 'hu',
                    'translate_from': 'en',
                    'forced': 'False',
                    'hi': 'False',
                }
            ]
        }
        mock_missing.return_value = [_missing_language('hun')]

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.srt',
            video_path='/series/anime/s01e01.mkv',
            media_type='series',
            series_id=10,
            episode_id=20,
            source_score_percent=100,
            arr_instance_id=2,
        )

    mock_profiles.assert_called_once_with(profile_id=2)
    mock_translate.assert_called_once()
    metadata = mock_translate.call_args.kwargs['metadata']
    assert metadata.imdbId == 'tt-anime'
    assert metadata.tvdbId == 200


def test_wanted_series_translation_uses_exact_profile_variant():
    from subtitles.wanted.series import _wanted_episode

    episode = SimpleNamespace(
        audio_language='English',
        failedAttempts='[]',
        missing_subtitles="['hu:forced']",
        path='/series/episode.mkv',
        profileId=1,
        sceneName='Scene.Name',
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        subtitles="[['en', '/subs/source.en.srt', 100], ['fr', '/subs/source.fr.srt', 100]]",
        title='Episode title',
    )

    def source_path(_subtitles, source_lang, path_replace_fn=None):
        return f'/subs/source.{source_lang}.srt'

    with (
        patch('subtitles.wanted.series.get_audio_profile_languages',
              return_value=[{'name': 'English'}]),
        patch('subtitles.wanted.series.get_profiles_list') as mock_profiles,
        patch('subtitles.wanted.series._find_existing_subtitle_path',
              side_effect=source_path),
        patch('subtitles.wanted.series.path_mappings') as mock_path_mappings,
        patch('subtitles.wanted.series.settings') as mock_settings,
        patch('subtitles.wanted.series.database') as mock_database,
        patch('subtitles.wanted.series.jobs_queue') as mock_jobs_queue,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
        patch('subtitles.wanted.series.generate_subtitles', return_value=[]),
    ):
        mock_profiles.return_value = {
            'items': [
                {
                    'language': 'hu',
                    'translate_from': 'fr',
                    'forced': 'True',
                    'hi': 'False',
                },
                {
                    'language': 'hu',
                    'translate_from': 'en',
                    'forced': 'False',
                    'hi': 'False',
                },
            ]
        }
        mock_path_mappings.path_replace.side_effect = lambda value: value
        mock_settings.translator.min_source_score = 0
        # 3 queries inside _wanted_episode when auto-translate fires:
        # source-score lookup, already-translated guard, and the metadata
        # fetch for postprocess_subtitles.
        mock_database.execute.return_value.first.side_effect = [
            None,
            None,
            SimpleNamespace(sonarrSeriesId=10, season=1, episode=1,
                            imdbId='tt1', tvdbId='1'),
        ]
        mock_jobs_queue._is_an_existing_job.return_value = False

        _wanted_episode(episode, providers_list=[])

    mock_translate.assert_called_once()
    translate_kwargs = mock_translate.call_args.kwargs
    assert translate_kwargs['from_lang'] == 'fr'
    assert translate_kwargs['to_lang'] == 'hu'
    assert translate_kwargs['forced'] is True
    assert translate_kwargs['hi'] is False
    assert translate_kwargs['media_type'] == 'episode'

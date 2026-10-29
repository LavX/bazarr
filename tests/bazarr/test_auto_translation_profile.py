# coding=utf-8

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


def _missing_language(alpha3, hi=False, forced=False):
    return SimpleNamespace(alpha3=alpha3, hi=hi, forced=forced)


pytestmark = pytest.mark.usefixtures('languages_loaded')


@pytest.mark.parametrize('source_score_percent', [90, 100, None])
def test_downloaded_series_subtitle_queues_episode_translation(source_score_percent):
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
        mock_settings.translator.min_source_score = 90
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
            source_score_percent=source_score_percent,
        )

    mock_translate.assert_called_once()
    assert mock_translate.call_args.kwargs['media_type'] == 'episode'
    # The queueing sighting: the destination did not exist when this job was
    # queued, and the job refuses to publish over a subtitle that lands there
    # before it writes.
    expected = mock_translate.call_args.kwargs['expected_destination_versions']
    assert len(expected) == 1
    assert expected[0].path.endswith('episode.hu.srt')
    assert expected[0].version is None


def test_downloaded_subtitle_target_already_on_disk_is_not_queued(tmp_path, caplog):
    """A target that is on disk but not indexed yet is still served.

    The database's missing list is the indexer's view of the media, and the
    download that just landed the target's file may not be in it yet. The
    queueing sighting checks the exact destination so that file wins.
    """
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
    video_path = tmp_path / 'episode.mkv'
    destination = tmp_path / 'episode.hu.srt'
    destination.write_text('already here')

    with (
        caplog.at_level(logging.INFO),
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profile_id', return_value=1),
        patch('app.database.get_profiles_list') as mock_profiles,
        patch('app.database.database', mock_database),
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.processing.alpha2_from_alpha3', return_value='hu'),
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 90
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
            subtitle_path=str(tmp_path / 'episode.en.srt'),
            video_path=str(video_path),
            media_type='series',
            series_id=10,
            episode_id=20,
            source_score_percent=100,
        )

    mock_translate.assert_not_called()
    assert [record for record in caplog.records
            if 'auto-translate skipped' in record.getMessage()]


def test_downloaded_subtitle_target_captured_as_present_is_not_queued(caplog):
    """The skip decision comes from the capture itself, not a separate isfile
    check: a file landing in between cannot become the job's baseline."""
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
        caplog.at_level(logging.INFO),
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profile_id', return_value=1),
        patch('app.database.get_profiles_list') as mock_profiles,
        patch('app.database.database', mock_database),
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.processing.alpha2_from_alpha3', return_value='hu'),
        patch('subtitles.tools.subsync_engines.capture_subtitle_versions') as mock_capture,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 90
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
        mock_capture.return_value = (SimpleNamespace(path='/video/episode.hu.srt',
                                                     version=('dev', 1, 10, 1, 1)),)

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.srt',
            video_path='/video/episode.mkv',
            media_type='series',
            series_id=10,
            episode_id=20,
            source_score_percent=100,
        )

    mock_translate.assert_not_called()
    assert [record for record in caplog.records
            if 'auto-translate skipped' in record.getMessage()]


@pytest.mark.parametrize('profile', [
    None,
    {'items': []},
    {'items': [
        {'language': 'en', 'translate_from': None},
        {'language': 'hr', 'translate_from': None},
    ]},
    {'items': [{'language': 'hu', 'translate_from': 'fr'}]},
    {'items': [{'language': 'en', 'translate_from': 'en'}]},
], ids=['no-profile', 'empty-profile', 'disabled', 'different-source', 'same-language'])
def test_downloaded_subtitle_without_matching_translation_target_stays_silent(profile, caplog):
    from subtitles.processing import _trigger_auto_translation

    mock_database = Mock()
    mock_database.execute.return_value.first.return_value = SimpleNamespace(
        imdbId='tt2', tmdbId='2', profileId=1,
    )

    with (
        caplog.at_level(logging.INFO),
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profiles_list', return_value=profile),
        patch('app.database.database', mock_database),
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 90

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.srt',
            video_path='/video/movie.mkv',
            media_type='movie',
            radarr_id=30,
            source_score_percent=80,
        )

    assert not [record for record in caplog.records if 'auto-translate' in record.getMessage()]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    mock_missing.assert_not_called()
    mock_translate.assert_not_called()


def test_low_score_subtitle_logs_once_for_multiple_matching_translation_targets(caplog):
    from subtitles.processing import _trigger_auto_translation

    mock_database = Mock()
    mock_database.execute.return_value.first.return_value = SimpleNamespace(
        imdbId='tt2', tmdbId='2', profileId=1,
    )

    with (
        caplog.at_level(logging.INFO),
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.get_profiles_list') as mock_profiles,
        patch('app.database.database', mock_database),
        patch('subtitles.download.check_missing_languages') as mock_missing,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 90
        mock_profiles.return_value = {
            'items': [
                {'language': 'hu', 'translate_from': 'en'},
                {'language': 'hr', 'translate_from': 'en'},
            ]
        }

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.srt',
            video_path='/video/movie.mkv',
            media_type='movie',
            radarr_id=30,
            source_score_percent=80,
        )

    score_logs = [record for record in caplog.records if 'below threshold' in record.getMessage()]
    assert len(score_logs) == 1
    assert score_logs[0].levelno == logging.INFO
    assert score_logs[0].args == (80, 90, '/video/movie.mkv')
    mock_missing.assert_not_called()
    mock_translate.assert_not_called()


@pytest.mark.parametrize('source_score_percent', [80, 100])
def test_forced_source_subtitle_stays_silent_and_does_not_translate(source_score_percent, caplog):
    from subtitles.processing import _trigger_auto_translation

    with (
        caplog.at_level(logging.INFO),
        patch('subtitles.processing.settings') as mock_settings,
        patch('app.database.database') as mock_database,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
    ):
        mock_settings.translator.min_source_score = 90

        _trigger_auto_translation(
            downloaded_lang='en',
            subtitle_path='/subs/source.en.forced.srt',
            video_path='/video/movie.mkv',
            media_type='movie',
            radarr_id=30,
            source_score_percent=source_score_percent,
            forced=True,
        )

    assert not [record for record in caplog.records if 'auto-translate' in record.getMessage()]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    mock_database.execute.assert_not_called()
    mock_translate.assert_not_called()


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
        # The wanted scan reads local_subtitle_score; a bare mock would not compare.
        mock_settings.general.local_subtitle_score = 0
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
    # The wanted scan captures the destination's versions when it queues, so a
    # subtitle that lands there while the job waits is not replaced.
    expected = translate_kwargs['expected_destination_versions']
    assert len(expected) == 1
    assert expected[0].path.endswith('episode.hu.forced.srt')
    assert expected[0].version is None


def test_wanted_series_does_not_queue_over_a_subtitle_already_on_disk(tmp_path):
    """The index can still call a language missing after its file landed. The
    wanted scan must not queue a translation over that file: it is the
    provider's and is the better track."""
    from subtitles.wanted.series import _wanted_episode

    video = tmp_path / 'episode.mkv'
    destination = tmp_path / 'episode.hu.forced.srt'
    destination.write_text('already here')
    episode = SimpleNamespace(
        audio_language='English',
        failedAttempts='[]',
        missing_subtitles="['hu:forced']",
        path=str(video),
        profileId=1,
        sceneName='Scene.Name',
        sonarrEpisodeId=20,
        sonarrSeriesId=10,
        subtitles="[['fr', '/subs/source.fr.srt', 100]]",
        title='Episode title',
    )

    with (
        patch('subtitles.wanted.series.get_audio_profile_languages',
              return_value=[{'name': 'English'}]),
        patch('subtitles.wanted.series.get_profiles_list') as mock_profiles,
        patch('subtitles.wanted.series._find_existing_subtitle_path',
              return_value='/subs/source.fr.srt'),
        patch('subtitles.wanted.series.path_mappings') as mock_path_mappings,
        patch('subtitles.wanted.series.settings') as mock_settings,
        patch('subtitles.wanted.series.database') as mock_database,
        patch('subtitles.wanted.series.jobs_queue') as mock_jobs_queue,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
        patch('subtitles.wanted.series.generate_subtitles', return_value=[]),
    ):
        mock_profiles.return_value = {'items': [
            {'language': 'hu', 'translate_from': 'fr', 'forced': 'True', 'hi': 'False'},
        ]}
        mock_path_mappings.path_replace.side_effect = lambda value: value
        mock_settings.translator.min_source_score = 0
        # The wanted scan reads local_subtitle_score; a bare mock would not compare.
        mock_settings.general.local_subtitle_score = 0
        mock_database.execute.return_value.first.side_effect = [
            None,
            None,
            SimpleNamespace(sonarrSeriesId=10, season=1, episode=1,
                            imdbId='tt1', tvdbId='1'),
        ]
        mock_jobs_queue._is_an_existing_job.return_value = False

        _wanted_episode(episode, providers_list=[])

    mock_translate.assert_not_called()
    assert destination.read_text() == 'already here'


def test_wanted_series_skips_a_destination_the_sighting_captured_as_present():
    """Same rule, pinned at the capture: whatever the capture saw is the
    baseline, so a file that was there when the sighting held the lock is
    never translated over."""
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
        subtitles="[['fr', '/subs/source.fr.srt', 100]]",
        title='Episode title',
    )

    with (
        patch('subtitles.wanted.series.get_audio_profile_languages',
              return_value=[{'name': 'English'}]),
        patch('subtitles.wanted.series.get_profiles_list') as mock_profiles,
        patch('subtitles.wanted.series._find_existing_subtitle_path',
              return_value='/subs/source.fr.srt'),
        patch('subtitles.wanted.series.path_mappings') as mock_path_mappings,
        patch('subtitles.wanted.series.settings') as mock_settings,
        patch('subtitles.wanted.series.database') as mock_database,
        patch('subtitles.wanted.series.jobs_queue') as mock_jobs_queue,
        patch('subtitles.tools.subsync_engines.capture_subtitle_versions') as mock_capture,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
        patch('subtitles.wanted.series.generate_subtitles', return_value=[]),
    ):
        mock_profiles.return_value = {'items': [
            {'language': 'hu', 'translate_from': 'fr', 'forced': 'True', 'hi': 'False'},
        ]}
        mock_path_mappings.path_replace.side_effect = lambda value: value
        mock_settings.translator.min_source_score = 0
        # The wanted scan reads local_subtitle_score; a bare mock would not compare.
        mock_settings.general.local_subtitle_score = 0
        mock_database.execute.return_value.first.side_effect = [
            None,
            None,
            SimpleNamespace(sonarrSeriesId=10, season=1, episode=1,
                            imdbId='tt1', tvdbId='1'),
        ]
        mock_jobs_queue._is_an_existing_job.return_value = False
        mock_capture.return_value = (SimpleNamespace(path='/series/episode.hu.forced.srt',
                                                     version=('dev', 1, 10, 1, 1)),)

        _wanted_episode(episode, providers_list=[])

    mock_translate.assert_not_called()


def test_wanted_movie_does_not_queue_over_a_subtitle_already_on_disk(tmp_path):
    """Same guard on the movie scan: a provider file the index has not seen
    yet ends the translation for that target."""
    from subtitles.wanted.movies import _wanted_movie

    video = tmp_path / 'movie.mkv'
    destination = tmp_path / 'movie.hu.srt'
    destination.write_text('already here')
    movie = SimpleNamespace(
        arr_instance_id=2,
        audio_language='English',
        failedAttempts='[]',
        missing_subtitles="['hu']",
        path=str(video),
        profileId=1,
        radarrId=30,
        sceneName='Scene.Name',
        subtitles="[['fr', '/subs/source.fr.srt', 100]]",
        title='Movie title',
    )

    with (
        patch('subtitles.wanted.movies.get_audio_profile_languages',
              return_value=[{'name': 'English'}]),
        patch('subtitles.wanted.movies.get_profiles_list') as mock_profiles,
        patch('subtitles.wanted.movies._find_existing_subtitle_path',
              return_value='/subs/source.fr.srt'),
        patch('subtitles.wanted.movies.path_mappings') as mock_path_mappings,
        patch('subtitles.wanted.movies.settings') as mock_settings,
        patch('subtitles.wanted.movies.database') as mock_database,
        patch('subtitles.wanted.movies.jobs_queue') as mock_jobs_queue,
        patch('subtitles.tools.translate.main.translate_subtitles_file') as mock_translate,
        patch('subtitles.wanted.movies.generate_subtitles', return_value=[]),
    ):
        mock_profiles.return_value = {'items': [
            {'language': 'hu', 'translate_from': 'fr', 'forced': 'False', 'hi': 'False'},
        ]}
        mock_path_mappings.path_replace_movie.side_effect = lambda value: value
        mock_settings.translator.min_source_score = 0
        # The wanted scan reads local_subtitle_score; a bare mock would not compare.
        mock_settings.general.local_subtitle_score = 0
        mock_database.execute.return_value.first.side_effect = [
            None,
            None,
            SimpleNamespace(imdbId='tt1', tmdbId='2'),
        ]
        mock_jobs_queue._is_an_existing_job.return_value = False

        _wanted_movie(movie, providers_list=[])

    mock_translate.assert_not_called()
    assert destination.read_text() == 'already here'

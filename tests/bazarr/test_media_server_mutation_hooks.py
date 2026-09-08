"""Native notifications follow real file publication, not secondary callbacks."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest

from test_upload_background_sync import upload_flow as upload_flow, review_translator as review_translator


@pytest.fixture
def mutations(monkeypatch):
    from media_servers import dispatcher
    events = []
    monkeypatch.setattr(dispatcher, 'notify_subtitle_mutation', events.append)
    return events


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_upload_and_later_background_sync_are_two_publications(upload_flow, mutations, media_type):
    flow = upload_flow
    flow.submit(media_type, job_id='upload')
    assert len(mutations) == 1
    assert mutations[0].operation == 'upload'
    flow.release_engine.set()
    job, thread = flow.start()
    thread.join(3)
    assert job.status == 'completed'
    assert [event.operation for event in mutations] == ['upload', 'sync']
    assert all(event.video_path == str(flow.video) and event.arr_instance_id == 7 for event in mutations)
    assert all(event.media_type == ('episode' if media_type == 'series' else 'movie') for event in mutations)


@pytest.mark.parametrize('failure', ['write', 'index'])
def test_upload_requires_write_but_survives_later_index_failure(upload_flow, mutations, monkeypatch, failure):
    from subtitles import upload
    from subtitles.tools import subsync_engines
    if failure == 'write':
        monkeypatch.setattr(subsync_engines.os, 'replace', Mock(side_effect=OSError('controlled write failure')))
        upload_flow.submit('movie', job_id='upload')
        assert mutations == []
    else:
        monkeypatch.setattr(upload, '_refresh_uploaded_subtitles', Mock(side_effect=RuntimeError('controlled index failure')))
        with pytest.raises(RuntimeError, match='controlled index failure'):
            upload_flow.submit('movie', job_id='upload')
        assert len(mutations) == 1
        assert mutations[0].operation == 'upload'


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_delete_notifies_once_only_when_removed_before_index(upload_flow, mutations, monkeypatch, media_type):
    from subtitles.tools import delete
    target = upload_flow.video.with_suffix('.en.srt')
    target.write_text('Original')
    name = 'store_subtitles' if media_type == 'series' else 'store_subtitles_movie'
    monkeypatch.setattr(delete, name, Mock(side_effect=RuntimeError('controlled index failure')))
    with pytest.raises(RuntimeError):
        upload_flow.remove(media_type)
    assert not target.exists()
    assert len(mutations) == 1
    assert mutations[0].operation == 'delete'
    with pytest.raises(RuntimeError):
        upload_flow.remove(media_type)
    assert len(mutations) == 1


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('later_failure', [False, True])
def test_sync_uses_video_not_explicit_subtitle_reference(upload_flow, mutations, monkeypatch, media_type, later_failure):
    from subtitles import sync
    flow = upload_flow
    source = flow.video.with_suffix('.en.srt')
    source.write_text('Original')
    reference = flow.video.parent / 'reference.srt'
    reference.write_text('Distinct subtitle reference')
    flow.release_engine.set()
    callback = Mock(side_effect=RuntimeError('controlled callback failure') if later_failure else None)
    sync.sync_subtitles(video_path=str(flow.video), srt_path=str(source), srt_lang='en', forced=False, hi=False,
                        percent_score=0, job_id='sync', reference=str(reference), force_sync=True,
                        sonarr_episode_id=20 if media_type == 'episode' else None,
                        radarr_id=30 if media_type == 'movie' else None, arr_instance_id=7,
                        track_job_progress=False, callback=callback)
    assert 'Synced' in source.read_text()
    assert len(mutations) == 1
    assert (mutations[0].video_path, mutations[0].subtitle_path, mutations[0].arr_instance_id) == (
        str(flow.video), str(source), 7)


@pytest.mark.parametrize('cancel_after_publish', [False, True])
def test_sync_cancellation_retains_each_already_published_output(upload_flow, mutations, monkeypatch,
                                                              cancel_after_publish):
    from app.config import settings
    from app.jobs_queue import JobCancelled
    from subtitles.tools import subsyncer
    flow = upload_flow
    monkeypatch.setattr(settings.subsync, 'output_mode', 'keep_all')
    monkeypatch.setattr(settings.subsync, 'enabled_engines', ['ffsubsync', 'alass'])

    def cancel(self, **kwargs):
        flow.queue.cancel_running_job(self.job_id)
        raise JobCancelled()

    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine' if cancel_after_publish else '_run_ffsubsync_engine',
                        cancel)
    flow.submit('movie', job_id='upload')
    flow.release_engine.set()
    _job, thread = flow.start()
    thread.join(3)
    assert [event.operation for event in mutations] == (['upload', 'sync'] if cancel_after_publish else ['upload'])
    if cancel_after_publish:
        assert mutations[-1].subtitle_path.endswith('.ffsubsync.srt')


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_bulk_sync_reaches_publication_hook(upload_flow, mutations, media_type):
    from subtitles.mass_operations import _process_subtitle_item
    flow = upload_flow
    source = flow.video.with_suffix('.en.srt')
    source.write_text('Original')
    flow.release_engine.set()
    item = dict(video_path=str(flow.video), srt_path=str(source), srt_lang='en', forced=False, hi=False,
                sonarr_series_id=10 if media_type == 'episode' else None,
                sonarr_episode_id=20 if media_type == 'episode' else None,
                radarr_id=30 if media_type == 'movie' else None, max_offset_seconds=60,
                no_fix_framerate=True, gss=False, arr_instance_id=7)
    assert _process_subtitle_item(item, 'sync', {}, 'bulk')
    assert len(mutations) == 1 and mutations[0].operation == 'sync'


@pytest.mark.parametrize('review_translator', ['google', 'lingarr', 'gemini', 'openrouter'], indirect=True)
def test_translation_service_publishes_before_later_history_failure(review_translator, mutations, monkeypatch):
    import sys
    run = review_translator
    module = sys.modules[type(run.service).__module__]
    run.service.arr_instance_id = 7
    monkeypatch.setattr(module, 'history_log_movie', Mock(side_effect=RuntimeError('controlled history failure')))
    run.release.set()
    run.worker.join(3)
    assert 'Translated text' in run.destination.read_text()
    assert len(mutations) == 1
    assert (mutations[0].operation, mutations[0].video_path, mutations[0].arr_instance_id) == (
        'translate', str(run.flow.video), 7)


@pytest.mark.parametrize('operation', ['edit', 'create', 'promote'])
@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_content_publication_precedes_chmod_failure(tmp_path, monkeypatch, mutations, operation, media_type):
    from api.subtitles import content
    video = tmp_path / 'Actual.mkv'
    video.touch()
    subtitle = video.with_suffix('.en.srt')
    source = video.with_suffix('.en.ffsubsync.srt')
    source.write_text('Aligned')
    if operation != 'create':
        subtitle.write_text('Original')
    metadata = {'mediaPath': '/upstream/Actual.mkv', 'arrInstanceId': 7}
    monkeypatch.setattr(content.path_mappings, 'path_replace_instance', lambda path, owner, kind: str(video))
    monkeypatch.setattr(content, 'resolve_subtitle_path', lambda kind, ident, language, **kwargs:
                        (str(source if ':sync-' in language else subtitle), metadata))
    monkeypatch.setattr(content, '_apply_subtitle_chmod', Mock(side_effect=RuntimeError('controlled chmod failure')))
    monkeypatch.setattr(content, 'get_target_folder', lambda path: None)
    row = Mock(path='/upstream/Actual.mkv', id=4, arr_instance_id=7)
    monkeypatch.setattr(content, 'database', Mock(execute=Mock(return_value=Mock(first=Mock(return_value=row)))))
    app = Flask(__name__)
    with app.test_request_context(json={'content': 'Aligned', 'language': 'en', 'format': 'srt'}):
        with pytest.raises(RuntimeError, match='controlled chmod failure'):
            if operation == 'edit':
                content._save_subtitle_content(media_type, 42, 'en', arr_instance_id=7)
            elif operation == 'create':
                content._create_subtitle(media_type, 42, arr_instance_id=7)
            else:
                content.promote_sync_subtitle(media_type, 42, 'en', 'en:sync-ffsubsync', arr_instance_id=7)
    assert subtitle.read_text() == 'Aligned'
    assert len(mutations) == 1
    assert (mutations[0].video_path, mutations[0].subtitle_path, mutations[0].arr_instance_id,
            mutations[0].operation) == (str(video), str(subtitle), 7, 'sync' if operation == 'promote' else 'edit')


def test_runner_publishes_before_failure_store_and_never_replays_callback(tmp_path, mutations):
    from media_servers.events import publication_callback
    from subtitles.tools.subsync_engines import InMemorySubsyncFailureStore, SubsyncEngineRunner
    subtitle = tmp_path / 'Movie.en.srt'
    subtitle.write_text('Original')
    store = InMemorySubsyncFailureStore()
    store.record_success = Mock(side_effect=RuntimeError('controlled accounting failure'))
    runner = SubsyncEngineRunner(store)
    runner.run(str(subtitle), 'overwrite', ['ffsubsync'],
               lambda engine, path: path.write_text('Aligned'),
               on_publish=publication_callback('movie', str(tmp_path / 'Movie.mkv'), 'sync', 7))
    assert subtitle.read_text() == 'Aligned'
    assert len(mutations) == 1


@pytest.fixture
def colliding_media(schema_session, tmp_path, monkeypatch):
    from app.database import TableEpisodes, TableShows, TableMovies, TableLanguagesProfiles
    from utilities.path_mappings import path_mappings
    roots = {owner: tmp_path / str(owner) for owner in (1, 2)}
    for owner, root in roots.items():
        root.mkdir()
        (root / 'Video.mkv').touch()
        schema_session.add(TableLanguagesProfiles(profileId=owner * 11, name=f'Profile {owner}', items='[]'))
    schema_session.flush()
    for owner in roots:
        schema_session.add(TableMovies(id=owner, arr_instance_id=owner, radarrId=42, path='/upstream/Video.mkv',
                                       title='Movie', year=2025, tmdbId=str(owner), profileId=owner * 11,
                                       subtitles='[]', missing_subtitles="['en']", monitored=True, tags='[]'))
        schema_session.add(TableShows(id=100 + owner, arr_instance_id=owner, sonarrSeriesId=99,
                                      title='Show', path='/upstream', profileId=owner * 11, tags='[]'))
    schema_session.flush()
    for owner in roots:
        schema_session.add(TableEpisodes(id=200 + owner, arr_instance_id=owner, series_id=100 + owner,
                                         sonarrEpisodeId=42, sonarrSeriesId=99, title='Episode',
                                         path='/upstream/Video.mkv', season=1, episode=1, subtitles='[]',
                                         missing_subtitles="['en']", monitored=True))
    schema_session.flush()
    monkeypatch.setattr(path_mappings, 'path_replace_instance',
                        lambda path, owner, kind: path.replace('/upstream', str(roots[owner or 1])))
    monkeypatch.setattr(path_mappings, 'path_replace_reverse_instance',
                        lambda path, owner, kind: path.replace(str(roots[owner or 1]), '/upstream'))
    monkeypatch.setattr(path_mappings, 'path_replace_movie', lambda path: path.replace('/upstream', str(roots[1])))
    monkeypatch.setattr(path_mappings, 'path_replace', lambda path: path.replace('/upstream', str(roots[1])))
    return SimpleNamespace(db=schema_session, roots=roots, video=str(roots[2] / 'Video.mkv'))


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('entry', ['manual', 'specific', 'missing'])
def test_download_adapters_preserve_distinct_owner_path_and_profile(colliding_media, monkeypatch, media_type, entry):
    from subtitles import manual
    from subtitles.mass_download import movies, series
    state = colliding_media
    module = manual if entry == 'manual' else movies if media_type == 'movie' else series
    monkeypatch.setattr(module, 'database', state.db)
    monkeypatch.setattr(module, 'get_audio_profile_languages', lambda value: [])
    monkeypatch.setattr(module, 'jobs_queue', Mock())
    seen = []

    def download(*args, **kwargs):
        seen.append((args, kwargs))
        return []

    if entry == 'manual':
        monkeypatch.setattr(module, 'manual_download_subtitle', download)
        if media_type == 'movie':
            module.movie_manually_download_specific_subtitle(42, False, False, False, 'fixture', 'subtitle',
                                                             job_id='download', arr_instance_id=2)
        else:
            module.episode_manually_download_specific_subtitle(99, 42, False, False, False, 'fixture', 'subtitle',
                                                               job_id='download', arr_instance_id=2)
    else:
        monkeypatch.setattr(module, 'generate_subtitles', download)
        monkeypatch.setattr(module, 'event_stream', Mock())
        monkeypatch.setattr(module, 'get_exclusion_clause', lambda kind: [])
        monkeypatch.setattr(module, 'get_providers', lambda: ['fixture'])
        if entry == 'specific' and media_type == 'movie':
            module.movie_download_specific_subtitles(42, 'en', 'False', 'False', job_id='download', arr_instance_id=2)
        elif entry == 'specific':
            module.episode_download_specific_subtitles(99, 42, 'en', 'False', 'False', job_id='download', arr_instance_id=2)
        elif media_type == 'movie':
            module.movies_download_subtitles(42, job_id='download', arr_instance_id=2)
        else:
            module.episode_download_subtitles(42, job_id='download', arr_instance_id=2)
    assert len(seen) == 1
    args, kwargs = seen[0]
    assert args[0] == state.video
    assert kwargs['arr_instance_id'] == 2
    assert (args[6] if entry == 'missing' else kwargs['profile_id']) == 22


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_combine_create_and_rebuild_use_exact_owner_and_file(colliding_media, monkeypatch, mutations, media_type):
    from subtitles.tools.combine import main
    from app import database
    from api.subtitles import subtitles
    state = colliding_media
    monkeypatch.setattr(database, 'database', state.db)
    monkeypatch.setattr(database, 'get_profiles_list', lambda profile_id:
                        {'profileId': profile_id, 'items': [{'language': 'en'}, {'language': 'hu'}],
                         'combine': {'languages': ['en', 'hu'], 'format': 'srt'}} if profile_id == 22 else None)
    postprocess = Mock()
    monkeypatch.setattr(subtitles, 'postprocess_subtitles', postprocess)
    for language in ('en', 'hu'):
        Path(state.video).with_suffix(f'.{language}.srt').write_text(
            f'1\n00:00:01,000 --> 00:00:02,000\n{language}\n')
    for _attempt in range(2):
        result = main.try_combine_for_video(state.video, media_type, sonarr_series_id=99,
                                            sonarr_episode_id=42, radarr_id=42, arr_instance_id=2)
        assert result.status == 'built', result
    assert len(mutations) == 2
    assert all(event.operation == 'combine' and event.arr_instance_id == 2 and event.video_path == state.video
               for event in mutations)
    assert postprocess.call_count == 2
    assert postprocess.call_args.kwargs['arr_instance_id'] == 2
    assert postprocess.call_args.args[3].profileId == 22
    assert not list(state.roots[1].glob('*.srt'))


def test_editor_alignment_is_temporary_until_real_content_save(upload_flow, mutations, monkeypatch):
    from api.editor import editor
    from api.subtitles import content
    flow = upload_flow
    library_subtitle = flow.video.with_suffix('.en.srt')
    library_subtitle.write_text('Original library subtitle')
    temporary = flow.video.parent / 'bazarr_sync_fixture.srt'
    temporary.write_text('Editor input')
    reference = flow.video.parent / 'reference.srt'
    reference.write_text('Separate reference')
    flow.release_engine.set()
    monkeypatch.setattr('threading.Timer', Mock())
    key = 'native-publication-test'
    editor._editor_sync_jobs[key] = {'status': 'running'}
    try:
        editor.run_editor_sync(key, str(flow.video), str(temporary), str(temporary.with_suffix('.synced.srt')),
                               'utf-8', '60', False, str(reference), enabled_engines=['ffsubsync'], output_mode='overwrite')
        result = editor._editor_sync_jobs[key]
        assert result['status'] == 'completed', result
        assert mutations == []
        assert library_subtitle.read_text() == 'Original library subtitle'
        monkeypatch.setattr(content, 'resolve_subtitle_path', lambda *args, **kwargs:
                            (str(library_subtitle), {'mediaPath': str(flow.video), 'arrInstanceId': 7}))
        monkeypatch.setattr(content, '_refresh_media_subtitles', Mock())
        app = Flask(__name__)
        with app.test_request_context(json={'content': result['content']},
                                      headers={'If-Match': content.generate_etag(str(library_subtitle))}):
            assert content._save_subtitle_content('movie', 30, 'en', arr_instance_id=7).status_code == 204
        assert len(mutations) == 1 and mutations[0].operation == 'edit'
        assert mutations[0].video_path == str(flow.video)
    finally:
        editor._editor_sync_jobs.pop(key, None)


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_download_missing_language_gate_uses_actual_owner(colliding_media, monkeypatch, media_type):
    from app.database import TableEpisodes, TableMovies
    from subtitles import download
    from subzero.language import Language
    state = colliding_media
    table = TableMovies if media_type == 'movie' else TableEpisodes
    state.db.query(table).filter(table.arr_instance_id == 1).update({'missing_subtitles': '[]'})
    state.db.flush()
    monkeypatch.setattr(download, 'database', state.db)
    monkeypatch.setattr(download, '_get_language_obj', lambda languages: {Language('eng')} if languages else set())
    kind = 'series' if media_type == 'episode' else 'movie'
    assert download.check_missing_languages(state.video, kind, 2) == {Language('eng')}
    assert download.check_missing_languages(str(state.roots[1] / 'Video.mkv'), kind, 1) == set()


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('automatic_sync', [False, True])
@pytest.mark.parametrize('entrypoint', ['manual', 'automatic'])
def test_actual_manual_download_and_autosync_writes_each_notify_once(colliding_media, upload_flow,
                                                                   monkeypatch, mutations, media_type, automatic_sync,
                                                                   entrypoint):
    from subtitles import manual, processing, download, pool
    from subzero.language import Language
    from subliminal_patch.subtitle import Subtitle
    from subtitles.tools import mods
    state = colliding_media
    flow = upload_flow

    class DownloadedSubtitle(Subtitle):
        provider_name = 'fixture'
        id = 'subtitle'

    subtitle = DownloadedSubtitle(Language('eng'))
    subtitle.content = b'1\n00:00:01,000 --> 00:00:02,000\nDownloaded\n'
    subtitle.format = 'srt'
    subtitle.score = 100
    subtitle.matches = set()
    monkeypatch.setattr(mods, 'get_subzero_mods', lambda owner: [])
    monkeypatch.setattr(manual.subtitle_cache, 'get', lambda key: subtitle)
    monkeypatch.setattr(manual, 'get_video', lambda path, *args, **kwargs: SimpleNamespace(original_path=path))
    monkeypatch.setattr(manual, 'download_subtitles', lambda *args: None)
    monkeypatch.setattr(manual, '_get_pool', lambda *args: None)
    monkeypatch.setattr(manual, 'get_target_folder', lambda path: None)
    monkeypatch.setattr(processing, 'database', state.db)
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: automatic_sync)
    monkeypatch.setattr(processing, '_trigger_auto_translation', Mock())
    monkeypatch.setattr(processing, '_trigger_combine', Mock())
    monkeypatch.setattr(processing, 'notify_radarr', Mock())
    monkeypatch.setattr(processing, 'notify_sonarr', Mock())
    monkeypatch.setattr(processing, 'client_for_instance', Mock())
    monkeypatch.setattr(processing, 'event_stream', Mock())
    monkeypatch.setattr(processing, 'call_external_webhook', Mock())
    # upload_flow has its own identity mapping; restore the two distinct owners
    # for this real processing lookup and publication.
    monkeypatch.setattr(processing.path_mappings, 'path_replace_reverse_instance',
                        lambda path, owner, kind: path.replace(str(state.roots[owner]), '/upstream'))
    flow.release_engine.set()
    if entrypoint == 'manual':
        result = manual.manual_download_subtitle(state.video, 'English', 'False', 'False', 'cached', 'fixture',
                                                 'None', 'Video', 'series' if media_type == 'episode' else 'movie',
                                                 False, 22, job_id='download', arr_instance_id=2)
    else:
        video = type('Video', (), {'original_path': state.video})()
        monkeypatch.setattr(pool, '_update_pool', lambda *args, **kwargs: False)
        monkeypatch.setattr(download, 'get_video', lambda *args, **kwargs: video)
        monkeypatch.setattr(download, '_get_pool', lambda *args: SimpleNamespace(
            providers=['fixture'], discarded_providers=[]))
        monkeypatch.setattr(download, 'get_profiles_list', lambda profile_id: {'originalFormat': False})
        monkeypatch.setattr(download, '_get_language_obj', lambda languages: {Language('eng')})
        monkeypatch.setattr(download, 'database', state.db)
        monkeypatch.setattr(download, 'download_best_subtitles', lambda **kwargs: {video: [subtitle]})
        monkeypatch.setattr(download, 'clear_mismatch_for_video', Mock())
        monkeypatch.setattr(download.subliminal, 'region', Mock())
        result = list(download.generate_subtitles(
            state.video, [('en', 'False', 'False')], 'English', 'None', 'Video',
            'series' if media_type == 'episode' else 'movie', 22, job_id='download', arr_instance_id=2,
            check_if_still_required=True))
    assert result
    assert [event.operation for event in mutations] == ['download'] * (2 if automatic_sync else 1)
    assert all(event.video_path == state.video and event.arr_instance_id == 2 and event.media_type == media_type
               for event in mutations)
    actual = Path(state.video).with_suffix('.en.srt')
    assert ('Synced' if automatic_sync else 'Downloaded') in actual.read_text()


@pytest.mark.parametrize('review_translator', ['openrouter'], indirect=True)
def test_saved_partial_translation_is_a_publication(review_translator, mutations):
    run = review_translator
    run.service.partial_error = 'One remote batch did not finish'
    run.release.set()
    run.worker.join(3)
    assert run.service.partial_error
    assert 'Translated text' in run.destination.read_text()
    assert len(mutations) == 1 and mutations[0].operation == 'translate'


def test_publication_callback_failure_cannot_replay_or_undo_saved_output(tmp_path):
    from subtitles.tools.subsync_engines import staged_subtitle_write
    destination = tmp_path / 'Video.en.srt'
    calls = []

    def callback(path):
        calls.append(path)
        raise RuntimeError('controlled publication callback failure')

    with staged_subtitle_write(str(tmp_path / 'Video.mkv'), str(destination), on_publish=callback) as temporary:
        Path(temporary).write_text('Published')
    assert destination.read_text() == 'Published'
    assert calls == [str(destination)]


@pytest.mark.parametrize('changed', [False, True])
def test_upload_postprocessing_change_survives_later_failure(upload_flow, mutations, monkeypatch, changed):
    from subtitles import upload, processing
    flow = upload_flow
    monkeypatch.setattr(processing, '_postprocessing_config', lambda *args: (True, 'fixture', False, 0))
    monkeypatch.setattr(upload, 'pp_replace', lambda *args: 'fixture')

    def postprocess(command, path, subtitle_path):
        if changed:
            import os
            original = Path(subtitle_path).read_bytes()
            before = os.stat(subtitle_path)
            Path(subtitle_path).write_bytes(original.replace(b'Uploaded', b'Modified'))
            os.utime(subtitle_path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert os.stat(subtitle_path).st_size == before.st_size
        raise RuntimeError('controlled postprocessing failure')

    monkeypatch.setattr(upload, 'postprocessing', postprocess)
    with pytest.raises(RuntimeError, match='controlled postprocessing failure'):
        flow.submit('movie', job_id='upload')
    assert len(mutations) == (2 if changed else 1)
    assert all(event.operation == 'upload' for event in mutations)


@pytest.fixture
def download_postprocessing(colliding_media, upload_flow, monkeypatch):
    from app.config import settings
    from subtitles import processing
    from subzero.language import Language

    state = colliding_media
    destination = state.roots[2].parent / '0-configured-destination'
    destination.mkdir()
    subtitle_folder = state.roots[2].parent / '3-subtitles'
    subtitle_folder.mkdir()
    sidecar = subtitle_folder / 'Video.en.srt'
    sidecar.write_bytes(b'Original')
    monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
    monkeypatch.setattr(settings.general, 'subfolder_custom', str(destination))
    monkeypatch.setattr(processing, 'database', state.db)
    monkeypatch.setattr(processing, '_defaul_sync_checker', lambda subtitle: False)
    monkeypatch.setattr(processing, '_postprocessing_config', lambda *args: (True, 'fixture', False, 0))
    monkeypatch.setattr(processing, 'pp_replace', lambda *args: 'fixture')
    monkeypatch.setattr(processing, 'set_chmod', lambda **kwargs: None)
    monkeypatch.setattr(processing.path_mappings, 'path_replace_reverse_instance',
                        lambda path, owner, kind: path.replace(str(state.roots[owner]), '/upstream'))
    for name in ('_trigger_auto_translation', '_trigger_combine', 'notify_radarr', 'notify_sonarr',
                 'client_for_instance', 'event_stream', 'call_external_webhook'):
        monkeypatch.setattr(processing, name, Mock())
    subtitle = SimpleNamespace(provider_name='fixture', uploader='', release_info='', language=Language('eng'),
                               storage_path=str(sidecar), id='fixture', score=100, matches=set())

    def process(media_type='movie'):
        return processing.process_subtitle(subtitle, media_type, 'English', state.video, 100, arr_instance_id=2)

    return SimpleNamespace(video=state.video, sidecar=sidecar, process=process)


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_download_noop_postprocessing_does_not_duplicate_concurrent_publication(
        download_postprocessing, mutations, monkeypatch, media_type):
    from threading import Event, Thread, current_thread
    from subtitles import post_processing
    from subtitles.tools import subsync_engines
    from media_servers.events import publication_callback

    run = download_postprocessing
    held, observer_waiting = Event(), Event()
    failures = []
    observer = current_thread()
    original_lock = subsync_engines.subtitle_write_lock

    def observed_lock(*args):
        if current_thread() is observer:
            observer_waiting.set()
        return original_lock(*args)

    monkeypatch.setattr(subsync_engines, 'subtitle_write_lock', observed_lock)
    monkeypatch.setattr(post_processing, '_postprocessing_locked', lambda command, path: None)

    def publish():
        try:
            with subsync_engines.subtitle_write_locks(run.video, str(run.sidecar)):
                held.set()
                assert observer_waiting.wait(5)
                subsync_engines.write_subtitle_file(
                    run.video, str(run.sidecar), b'Published concurrently',
                    on_publish=publication_callback(media_type, run.video, 'download', 2))
        except BaseException as error:
            failures.append(error)

    writer = Thread(target=publish)
    writer.start()
    try:
        assert held.wait(5)
        assert run.process(media_type)
    finally:
        observer_waiting.set()
        writer.join(5)
    assert not writer.is_alive() and not failures
    assert run.sidecar.read_bytes() == b'Published concurrently'
    assert len(mutations) == 1
    assert mutations[0].operation == 'download' and mutations[0].arr_instance_id == 2
    assert mutations[0].video_path == run.video and mutations[0].subtitle_path == str(run.sidecar)


@pytest.mark.parametrize('media_type', ['movie', 'series'])
def test_download_keeps_resolved_postprocessing_directories_when_settings_change(
        download_postprocessing, mutations, monkeypatch, media_type):
    from contextlib import contextmanager
    from threading import Event, Thread, current_thread
    from app.config import settings
    from media_servers.events import publication_callback
    from subtitles import processing, post_processing
    from subtitles.tools import subsync_engines

    run = download_postprocessing
    new_destination = Path(run.video).parent.parent / '1-new-destination'
    new_destination.mkdir()
    writer_owns_destination, writer_waits_video = Event(), Event()
    download_thread = current_thread()
    original_lock = subsync_engines.subtitle_write_lock
    original_observer = processing.observe_subtitle_change
    failures = []

    @contextmanager
    def tracked_lock(video_path, directory):
        state = original_lock(video_path, directory)
        if current_thread() is download_thread and directory == str(new_destination):
            # Detect the actual cycle without leaving deadlocked test threads.
            assert state.lock.acquire(blocking=False), 'nested destination resolution would deadlock the download'
        else:
            if current_thread() is writer and directory == str(Path(run.video).parent):
                writer_waits_video.set()
            state.lock.acquire()
        try:
            if current_thread() is writer and directory == str(new_destination):
                writer_owns_destination.set()
            yield state
        finally:
            state.lock.release()

    def command(command, path):
        if current_thread() is writer:
            subsync_engines.write_subtitle_file(
                run.video, str(run.sidecar), b'Published under new destination settings',
                on_publish=publication_callback(media_type, run.video, 'download', 2))

    def publish():
        try:
            # A legacy caller resolves the newly configured destination once.
            post_processing.postprocessing('write', run.video, subtitle_path=str(run.sidecar))
        except BaseException as error:
            failures.append(error)

    writer = Thread(target=publish)

    @contextmanager
    def change_destination(*args):
        settings.general.subfolder_custom = str(new_destination)
        writer.start()
        assert writer_owns_destination.wait(5) and writer_waits_video.wait(5)
        with original_observer(*args):
            yield

    monkeypatch.setattr(subsync_engines, 'subtitle_write_lock', tracked_lock)
    monkeypatch.setattr(post_processing, '_postprocessing_locked', command)
    monkeypatch.setattr(processing, 'observe_subtitle_change', change_destination)
    try:
        assert run.process(media_type)
    finally:
        if writer.ident is not None:
            writer.join(5)
    assert not writer.is_alive() and not failures
    assert run.sidecar.read_bytes() == b'Published under new destination settings'
    assert len(mutations) == 1
    assert mutations[0].video_path == run.video and mutations[0].arr_instance_id == 2


@pytest.mark.parametrize('change', ['none', 'bytes', 'restored_mtime'])
@pytest.mark.parametrize('later_failure', [False, True])
def test_download_postprocessing_reports_only_its_actual_change(
        download_postprocessing, mutations, monkeypatch, change, later_failure):
    import os
    from subtitles import processing, post_processing

    run = download_postprocessing

    def command(command, path):
        if change == 'bytes':
            run.sidecar.write_bytes(b'Changed subtitle bytes')
        elif change == 'restored_mtime':
            before = run.sidecar.stat()
            run.sidecar.write_bytes(b'Modified')
            os.utime(run.sidecar, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert run.sidecar.stat().st_size == before.st_size

    def chmod(**kwargs):
        if later_failure:
            raise OSError('controlled chmod failure after postprocessing')

    monkeypatch.setattr(post_processing, '_postprocessing_locked', command)
    monkeypatch.setattr(processing, 'set_chmod', chmod)
    if later_failure:
        with pytest.raises(OSError, match='controlled chmod failure'):
            run.process()
    else:
        assert run.process()
    assert len(mutations) == (0 if change == 'none' else 1)
    assert all(event.operation == 'download' and event.arr_instance_id == 2 and event.video_path == run.video
               for event in mutations)
    assert run.sidecar.read_bytes() == {
        'none': b'Original', 'bytes': b'Changed subtitle bytes', 'restored_mtime': b'Modified'}[change]


def test_combine_cleanup_notifies_each_successful_removal(tmp_path, mutations):
    from subtitles.tools.combine.main import _remove_stale_combined_siblings
    from media_servers.events import publication_callback
    video = tmp_path / 'Video.mkv'
    video.touch()
    destination = tmp_path / 'Video.en.combined-hu.srt'
    destination.write_text('New')
    stale = destination.with_suffix('.ass')
    stale.write_text('Old')
    callback = publication_callback('movie', str(video), 'combine', 7)
    _remove_stale_combined_siblings(str(destination), str(video), callback)
    _remove_stale_combined_siblings(str(destination), str(video), callback)
    assert not stale.exists()
    assert len(mutations) == 1 and mutations[0].subtitle_path == str(stale)

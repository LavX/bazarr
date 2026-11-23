"""Use the public subtitle toolbox and editor on source items on SQLite and PostgreSQL."""
import ast
import gzip
import json
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from flask import Flask
from flask_compress import Compress
from requests.utils import DEFAULT_ACCEPT_ENCODING

from media_source_helpers import ARR_AND_SPORTS_TABLES, table_counts, video
import test_media_sources_pipeline as pipeline

world = pipeline.world
isolated_jobs = pipeline.isolated_jobs
HEADERS = {'X-API-KEY': 'synthetic-bazarr-key'}
SRT = '1\n00:00:00,000 --> 00:00:01,000\n<i>Original cue</i>\n'
SYNCED = SRT.replace('00:00:00,000', '00:00:00,250')


@pytest.fixture
def toolbox(world, isolated_jobs, monkeypatch):
    from api import api_bp
    from api.subtitles import content, subtitles
    from app.config import settings
    from media_sources import analysis
    from subtitles import sync
    from subtitles.tools import mods, subsyncer
    from subtitles.tools.translate.services import google_translator

    monkeypatch.setitem(settings.auth, 'apikey', HEADERS['X-API-KEY'])
    monkeypatch.setattr(settings.general, 'subfolder', 'current')
    monkeypatch.setattr(settings.general, 'chmod_enabled', False)
    monkeypatch.setattr(settings.general, 'single_language', False)
    monkeypatch.setattr(settings.subsync, 'debug', False)
    monkeypatch.setattr(analysis.video_analyzer, 'parse_video_metadata',
                        lambda *args, **kwargs: {'ffprobe': {'audio': []}})
    monkeypatch.setattr(sync, '_resolve_subsync_overrides',
                        lambda owner, series, engines, offset: (True, False, 0, engines, offset or '60'))
    monkeypatch.setattr(settings.translator, 'runtime_v2', False)
    monkeypatch.setattr(settings.translator, 'translator_type', 'google_translate')
    monkeypatch.setattr(settings.translator, 'translator_info', False)
    monkeypatch.setattr(subtitles, 'translation_available', lambda **kwargs: SimpleNamespace(available=True))
    for module in (sync, mods, subsyncer, google_translator, content):
        if hasattr(module, 'jobs_queue'):
            monkeypatch.setattr(module, 'jobs_queue', isolated_jobs)
    transport = Mock()
    transport.return_value.translate.return_value = 'Translated cue'
    monkeypatch.setattr(google_translator, 'GoogleTranslator', transport)

    def no_arr(*args, **kwargs):
        raise AssertionError('Source tool fell through to Arr history, indexing or path mapping')

    for module, name in ((subtitles, 'store_subtitles'), (subtitles, 'store_subtitles_movie'),
                         (content, 'history_log'), (content, 'history_log_movie'),
                         (subsyncer, 'history_log'), (subsyncer, 'history_log_movie')):
        monkeypatch.setattr(module, name, no_arr)
    from subtitles.indexer import series, movies, sports

    monkeypatch.setattr(movies, 'store_subtitles_movie', no_arr)
    monkeypatch.setattr(series, 'store_subtitles', no_arr)
    monkeypatch.setattr(sports, 'store_subtitles_sports', no_arr)
    monkeypatch.setattr(subtitles.path_mappings, 'path_replace_instance', no_arr)
    monkeypatch.setattr(subtitles.path_mappings, 'path_replace_reverse_instance', no_arr)

    app = Flask(__name__)
    app.register_blueprint(api_bp)
    return SimpleNamespace(client=app.test_client(), db=world.db, jobs=isolated_jobs, world=world, transport=transport)


def _sidecars(toolbox, context):
    from media_sources.indexer import index_item

    video_path = Path(context.path)
    path = video_path.with_suffix('.en.srt')
    reference = video_path.with_suffix('.de.srt')
    for subtitle in (path, reference):
        subtitle.write_text(SRT)
    assert index_item(context.item_id) is not None
    return path, reference


def _request(toolbox, source, subtitle, action, **extra):
    options = dict(type='source', id=source.item_id, path=str(subtitle), action=action, language='en')
    return toolbox.client.patch('/api/subtitles', headers=HEADERS, json={**options, **extra})


def _run(toolbox, response):
    assert response.status_code == 202, response.get_data(as_text=True)
    job = toolbox.jobs._reserve_next_job()
    assert job.job_id == response.get_json()['job_id']
    assert toolbox.jobs._run_job(job)
    assert toolbox.jobs.get_job_status(job.job_id) == 'completed'
    return job


@pytest.mark.parametrize(('build', 'kind', 'publication'), [
    (pipeline._movie, 'movie', 'movie'), (pipeline._episode, 'episode', 'episode'),
    (pipeline._sports, 'sports_event', 'sports'),
])
def test_source_tools_write_files_history_and_index_for_each_media_kind(toolbox, monkeypatch, build, kind, publication):
    from media_servers import events
    from media_sources import repository
    from media_sources.subtitle_tools import SOURCE_SUBTITLE_EDITED_ACTION
    from subtitles.tools import subsyncer

    seen = []
    monkeypatch.setattr(events, 'notify_subtitle_mutation', seen.append)
    context = build(toolbox.world)
    path, ref = _sidecars(toolbox, context)
    before = table_counts(toolbox.db, ARR_AND_SPORTS_TABLES)
    forbidden_tables = set(ARR_AND_SPORTS_TABLES)
    writes = []

    @sa.event.listens_for(toolbox.db.get_bind(), 'before_cursor_execute')
    def inspect_writes(connection, cursor, statement, parameters, execution_context, executemany):
        target = re.match(r'^\s*(?:INSERT INTO|UPDATE|DELETE FROM)\s+["`]?([A-Za-z_]+)', statement, re.IGNORECASE)
        if target is not None and target[1].lower() in forbidden_tables:
            writes.append(target[1])

    def align(self, engine, output_path, video_path, no_fix_framerate):
        assert video_path == context.path
        output_path.write_text(SYNCED)
        return {'offset_seconds': 0.25, 'framerate_scale_factor': 1.0, 'quality_of_fit': 0.9}

    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
    references = toolbox.client.get('/api/subtitles', headers=HEADERS,
                                    query_string={'sourceItemId': context.item_id, 'subtitlesPath': str(path)})
    assert references.status_code == 200
    assert [r['path'] for r in references.json['data']['external_subtitles_tracks']] == [str(ref)]

    response = _request(toolbox, context, path, 'sync', reference=str(ref), output_mode='overwrite',
                        enabled_engines='alass')
    _run(toolbox, response)
    assert path.read_text() == SYNCED

    response = _request(toolbox, context, path, 'remove_tags')
    _run(toolbox, response)
    assert 'Original cue' in path.read_text() and '<i>' not in path.read_text()

    response = _request(toolbox, context, path, 'translate', language='hu')
    job = _run(toolbox, response)
    assert job.kwargs['source'].item_id == context.item_id and job.kwargs['arr_instance_id'] is None
    translated = Path(context.path).with_suffix('.hu.srt')
    assert 'Translated cue' in translated.read_text()

    listing = toolbox.client.get('/api/editor/subtitles', headers=HEADERS,
                                 query_string={'mediaType': 'source', 'mediaId': context.item_id})
    assert listing.status_code == 200
    assert {entry['language'] for entry in listing.json['subtitles']} == {'en', 'de', 'hu'}
    url = f'/api/sources/items/{context.item_id}/subtitles/en/content'
    opened = toolbox.client.get(url, headers=HEADERS)
    assert opened.status_code == 200
    assert opened.json['sourceKind'] == kind and opened.json['content'] == path.read_text()
    assert toolbox.client.get(url, headers={**HEADERS, 'If-None-Match': opened.headers['ETag']}).status_code == 304
    saved = toolbox.client.put(url, headers={**HEADERS, 'If-Match': opened.headers['ETag']}, json={'content': SRT})
    assert saved.status_code == 204
    assert path.read_text() == SRT and saved.headers['ETag'] != opened.headers['ETag']
    stale = toolbox.client.put(url, headers={**HEADERS, 'If-Match': opened.headers['ETag']},
                               json={'content': 'must not replace'})
    assert stale.status_code == 412 and path.read_text() == SRT

    rows = toolbox.db.execute(sa.select(repository.History).order_by(repository.History.c.id)).all()
    assert [row.action for row in rows] == [5, SOURCE_SUBTITLE_EDITED_ACTION, 6, SOURCE_SUBTITLE_EDITED_ACTION]
    assert {(row.item_id, row.source_id, row.item_kind, row.video_path) for row in rows} == {
        (context.item_id, context.source_id, kind, context.path)}
    assert [row.subtitles_path for row in rows] == [str(path), str(path), str(translated), str(path)]
    assert [row.operation for row in seen] == ['sync', 'edit', 'translate', 'edit']
    assert {(row.media_type, row.source_item_id, row.arr_instance_id) for row in seen} == {
        (publication, context.item_id, None)}
    assert table_counts(toolbox.db, ARR_AND_SPORTS_TABLES) == before
    assert writes == []
    assert {entry[0] for entry in ast.literal_eval(repository.get_item(context.item_id).subtitles)} == {'en', 'de', 'hu'}
    status = toolbox.client.get(f'/api/sources/items/{context.item_id}/subtitles/en/sync-status', headers=HEADERS)
    assert status.status_code == 200 and status.json['synced'] is True


@pytest.mark.parametrize('quoted', [True, False])
def test_source_editor_saves_with_compressed_get_etag(toolbox, quoted):
    app = toolbox.client.application
    app.config['COMPRESS_ALGORITHM'] = 'gzip'
    Compress(app)
    toolbox.client.environ_base['HTTP_ACCEPT_ENCODING'] = DEFAULT_ACCEPT_ENCODING

    context = pipeline._movie(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    document = '\n'.join(
        f'{index + 1}\n00:{index // 60:02}:{index % 60:02},000 --> 00:{index // 60:02}:{index % 60:02},900\n'
        f'Original dialogue for cue {index + 1}.\n' for index in range(300)
    )
    path.write_text(document)
    url = f'/api/sources/items/{context.item_id}/subtitles/en/content'
    opened = toolbox.client.get(url, headers=HEADERS)
    assert opened.status_code == 200
    assert opened.headers['Content-Encoding'] == 'gzip'
    assert json.loads(gzip.decompress(opened.data))['content'] == document
    read_etag = opened.headers['ETag']
    assert read_etag.endswith(':gzip"')

    cached = toolbox.client.get(url, headers={**HEADERS, 'If-None-Match': read_etag})
    assert cached.status_code == 304
    assert cached.headers['ETag'] == read_etag
    assert 'Accept-Encoding' in cached.vary
    assert not cached.data

    for invalid_etag in (f'W/{read_etag}', f'"{read_etag}"', f'{read_etag[:-1]}:gzip"',
                         read_etag.replace(':gzip', ':br'), '*'):
        refused = toolbox.client.put(url, headers={**HEADERS, 'If-Match': invalid_etag},
                                     json={'content': 'must not replace'})
        assert refused.status_code == 412
        assert path.read_text() == document

    write_etag = read_etag if quoted else read_etag[1:-1]
    saved = toolbox.client.put(url, headers={**HEADERS, 'If-Match': write_etag},
                               json={'content': document})
    assert saved.status_code == 204
    assert path.read_text() == document

    stale = toolbox.client.put(url, headers={**HEADERS, 'If-Match': write_etag},
                               json={'content': 'competing edit'})
    assert stale.status_code == 412
    assert path.read_text() == document

    reopened = toolbox.client.get(url, headers={**HEADERS, 'If-None-Match': read_etag})
    assert reopened.status_code == 200
    assert reopened.headers['ETag'] != read_etag
    assert reopened.headers['ETag'] == f'{saved.headers["ETag"][:-1]}:gzip"'


def test_source_editor_rejects_an_edit_after_capturing_the_sidecar(toolbox, monkeypatch):
    from media_sources.subtitle_tools import SourceSubtitleTool

    context = pipeline._movie(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    url = f'/api/sources/items/{context.item_id}/subtitles/en/content'
    opened = toolbox.client.get(url, headers=HEADERS)
    assert opened.status_code == 200
    capture = SourceSubtitleTool.capture

    def capture_then_edit(*args):
        operation = capture(*args)
        operation.write(SYNCED.encode(), 'edit')
        return operation

    monkeypatch.setattr(SourceSubtitleTool, 'capture', capture_then_edit)
    stale = toolbox.client.put(url, headers={**HEADERS, 'If-Match': opened.headers['ETag']},
                               json={'content': 'must not replace'})
    assert stale.status_code == 412
    assert path.read_text() == SYNCED


def test_source_sync_status_uses_edit_history_within_mtime_tolerance(toolbox):
    from media_sources import repository
    from media_sources.subtitle_tools import SOURCE_SUBTITLE_EDITED_ACTION

    context = pipeline._movie(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    history = dict(item_id=context.item_id, item_kind=context.kind, subtitles_path=str(path),
                   timestamp=datetime.fromtimestamp(path.stat().st_mtime), description='Test history')
    url = f'/api/sources/items/{context.item_id}/subtitles/en/sync-status'
    repository.insert_history(toolbox.db, action=5, **history)
    assert toolbox.client.get(url, headers=HEADERS).json['confirmed'] is True

    saved = toolbox.client.put(url.replace('sync-status', 'content'), headers=HEADERS, json={'content': SYNCED})
    assert saved.status_code == 204
    # Equal timestamps still preserve the order of the two recorded operations.
    toolbox.db.execute(sa.update(repository.History).where(repository.History.c.action == SOURCE_SUBTITLE_EDITED_ACTION)
                       .values(timestamp=history['timestamp']))
    timestamp = history['timestamp'].timestamp()
    os.utime(path, (timestamp, timestamp))
    status = toolbox.client.get(url, headers=HEADERS).json
    assert status['synced'] is True and status['editedAfterSync'] is True and status['confirmed'] is False
    assert status['lastSyncTimestamp'] == history['timestamp'].isoformat()

    repository.insert_history(toolbox.db, action=5, **history)
    assert toolbox.client.get(url, headers=HEADERS).json['confirmed'] is True


@pytest.mark.parametrize('changed', [None, 'video', 'reference', 'sidecar', 'during alignment'])
def test_source_editor_sync_previews_only_with_current_indexed_files(toolbox, monkeypatch, changed):
    from api.editor import editor
    from media_sources import repository
    from subtitles.tools import subsyncer

    context = pipeline._movie(toolbox.world)
    path, reference = _sidecars(toolbox, context)
    aligned = []

    def align(self, engine, output_path, video_path, no_fix_framerate):
        aligned.append(self.reference)
        assert video_path == context.path
        assert Path(self.srtin).read_text() == 'Editor draft'
        if changed == 'during alignment':
            reference.write_text('Reference edited while running')
        output_path.write_text(SYNCED)
        return {'offset_seconds': 0.25}

    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
    monkeypatch.setattr(toolbox.jobs, 'force_start_pending_job', Mock())
    cleanup = []
    monkeypatch.setattr(editor.threading, 'Timer', lambda _delay, callback: SimpleNamespace(
        start=lambda: cleanup.append(callback)))
    url = '/api/editor/sync'
    body = dict(mediaType='source', mediaId=context.item_id, language='en', content='Editor draft',
                outputMode='overwrite', enabledEngines=['alass'])
    response = toolbox.client.post(url, headers=HEADERS, json={**body, 'reference': str(reference)})
    assert response.status_code == 202
    job = toolbox.jobs._reserve_next_job()
    assert job.job_id == response.json['jobId']
    if changed in ('video', 'reference', 'sidecar'):
        changed_path = {'video': Path(context.path), 'reference': reference, 'sidecar': path}[changed]
        changed_path.write_text('File edited after enqueue')
    try:
        toolbox.jobs._run_job(job)
        poll = toolbox.client.get(url, headers=HEADERS, query_string={'jobKey': response.json['jobKey']})
        assert poll.status_code == 200
        if changed is None:
            assert poll.json['status'] == 'completed' and poll.json['content'] == SYNCED
            assert toolbox.jobs.get_job_status(job.job_id) == 'completed'
        else:
            assert poll.json['status'] == 'failed' and 'changed' in poll.json['message']
            assert toolbox.jobs.get_job_status(job.job_id) == 'failed'
        assert aligned == ([str(reference)] if changed in (None, 'during alignment') else [])
        assert path.read_text() == ('File edited after enqueue' if changed == 'sidecar' else SRT)
        assert not toolbox.db.execute(sa.select(repository.History)).all()
    finally:
        for callback in cleanup:
            callback()


def test_source_editor_sync_refuses_reference_from_another_item(toolbox, monkeypatch):
    from api.editor import editor

    context = pipeline._movie(toolbox.world)
    _sidecars(toolbox, context)
    other_subtitle, _ = _sidecars(toolbox, pipeline._episode(toolbox.world))
    workspace = Mock(side_effect=AssertionError('Rejected requests do not need a workspace'))
    monkeypatch.setattr(editor, 'create_preview_workspace', workspace)
    body = dict(mediaType='source', mediaId=context.item_id, language='en', content=SRT,
                reference=str(other_subtitle))

    response = toolbox.client.post('/api/editor/sync', headers=HEADERS, json=body)

    assert response.status_code == 403 and not toolbox.jobs.jobs_pending_queue
    workspace.assert_not_called()


def test_source_tools_refuse_unindexed_sidecars_and_foreign_instances(toolbox):
    from media_sources import repository

    context = pipeline._movie(toolbox.world)
    path, reference = _sidecars(toolbox, context)
    unrelated = path.with_name('Different.en.srt')
    unrelated.write_text('Unrelated')
    assert _request(toolbox, context, unrelated, 'remove_tags').status_code == 403
    assert _request(toolbox, context, path, 'sync', reference=str(unrelated)).status_code == 403
    assert _request(toolbox, context, path, 'remove_tags', arr_instance_id=17).status_code == 400
    assert _request(toolbox, context, path, 'ocr').status_code == 400
    assert _request(toolbox, context, '', 'translate', language='hu').status_code == 400
    path.unlink()
    path.symlink_to(unrelated)
    url = f'/api/sources/items/{context.item_id}/subtitles/en/content'
    assert toolbox.client.get(url, headers=HEADERS).status_code == 409
    assert toolbox.client.put(url, headers=HEADERS, json={'content': SRT}).status_code == 409
    assert not toolbox.jobs.jobs_pending_queue
    assert unrelated.read_text() == 'Unrelated'
    assert reference.read_text() == SRT
    assert not toolbox.db.execute(sa.select(repository.History)).all()


def test_source_stream_token_cannot_reopen_a_replaced_video(toolbox, monkeypatch):
    from api.editor import editor
    from media_sources import repository

    old = pipeline._movie(toolbox.world)
    monkeypatch.setattr(editor, '_probe_video', lambda path: {'format': {'duration': 30}})
    params = {'mediaType': 'source', 'mediaId': old.item_id}
    response = toolbox.client.get('/api/editor/stream-token', headers=HEADERS, query_string=params)
    assert response.status_code == 200
    token = response.json['token']
    replacement = video(toolbox.world.root / 'Replacement.mkv', size=1200)
    repository.update_item(old.item_id, toolbox.db, path=str(replacement))
    playlist = f'/api/editor/hls/source/{old.item_id}/0/0/playlist.m3u8'
    refused = toolbox.client.get(playlist, query_string={'st': token})
    assert refused.status_code == 401


@pytest.mark.parametrize('changed', ['sync reference', 'translation input'])
def test_tool_skips_publication_when_the_captured_input_changes(toolbox, monkeypatch, changed):
    from media_sources import repository
    from subtitles.tools import subsyncer

    context = pipeline._movie(toolbox.world)
    path, reference = _sidecars(toolbox, context)
    if changed == 'sync reference':
        def align(self, engine, output_path, video_path, no_fix_framerate):
            reference.write_text('A new reference')
            output_path.write_text(SYNCED)
            return {'offset_seconds': 0.25}

        monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
        request = _request(toolbox, context, path, 'sync', reference=str(reference), output_mode='overwrite',
                           enabled_engines='alass')
    else:
        def translate(text):
            path.write_text('A new subtitle')
            return 'Translated cue'

        toolbox.transport.return_value.translate.side_effect = translate
        request = _request(toolbox, context, path, 'translate', language='hu')
    _run(toolbox, request)
    assert path.read_text() == (SRT if changed == 'sync reference' else 'A new subtitle')
    assert not Path(context.path).with_suffix('.hu.srt').exists()
    assert not toolbox.db.execute(sa.select(repository.History)).all()


def test_a_generated_source_sync_output_can_be_promoted(toolbox, monkeypatch):
    from media_sources import repository
    from subtitles.tools import subsyncer

    context = pipeline._movie(toolbox.world)
    path, _ = _sidecars(toolbox, context)

    def align(self, engine, output_path, video_path, no_fix_framerate):
        output_path.write_text(SYNCED)
        return {'offset_seconds': 0.25}

    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
    _run(toolbox, _request(toolbox, context, path, 'sync', output_mode='keep_all', enabled_engines='alass'))
    generated = path.with_suffix('.alass.srt')
    assert path.read_text() == SRT and generated.read_text() == SYNCED
    url = f'/api/sources/items/{context.item_id}/subtitles/en/promote'
    response = toolbox.client.post(url, headers=HEADERS, json={'sourceLanguage': 'en:sync-alass'})
    assert response.status_code == 200
    assert path.read_text() == SYNCED
    rows = toolbox.db.execute(sa.select(repository.History.c.action, repository.History.c.subtitles_path)
                              .order_by(repository.History.c.id)).all()
    assert rows == [(5, str(generated)), (5, str(path))]


@pytest.mark.parametrize('completion', ['overwrite', 'cancelled', 'generated file changed', 'generated edited during engine'])
def test_source_sync_overwrite_retires_only_unchanged_generated_outputs(toolbox, monkeypatch, completion):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from media_sources import repository
    from subtitles.tools import subsyncer

    context = pipeline._movie(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    generated = path.with_suffix('.alass.srt')
    new_sibling = path.with_suffix('.ffsubsync.srt')
    replacement = SYNCED.replace('Original cue', 'Overwritten cue')
    edited = SYNCED.replace('Original cue', 'Edited generated cue')
    output = SYNCED
    engine_started, resume_engine = Event(), Event()

    def align(self, engine, output_path, video_path, no_fix_framerate):
        if completion == 'generated edited during engine' and output == replacement:
            engine_started.set()
            assert resume_engine.wait(5)
        output_path.write_text(output)
        return {'offset_seconds': 0.25}

    monkeypatch.setattr(subsyncer.SubSyncer, '_run_external_engine', align)
    _run(toolbox, _request(toolbox, context, path, 'sync', output_mode='keep_all', enabled_engines='alass'))
    assert generated.read_text() == SYNCED and path.read_text() == SRT

    output = replacement
    response = _request(toolbox, context, path, 'sync', output_mode='overwrite', enabled_engines='alass')
    assert response.status_code == 202
    job_id = response.json['job_id']
    job = toolbox.jobs._reserve_next_job()
    assert job.job_id == job_id
    report = subsyncer.SubSyncer._report_progress
    at_publication = []

    def edit_generated():
        updated = generated.with_suffix('.incoming')
        updated.write_text(edited)
        updated.replace(generated)

    def change_before_publication(self, message, value, total):
        if message == 'Saving synchronized subtitle':
            at_publication.append(True)
            if completion == 'cancelled':
                assert toolbox.jobs.cancel_running_job(job_id)
            elif completion == 'generated file changed':
                edit_generated()
        report(self, message, value, total)

    monkeypatch.setattr(subsyncer.SubSyncer, '_report_progress', change_before_publication)
    if completion == 'generated edited during engine':
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(toolbox.jobs._run_job, job)
            try:
                assert engine_started.wait(5)
                edit_generated()
                new_sibling.write_text(edited)
            finally:
                resume_engine.set()
            assert running.result(timeout=10)
        assert new_sibling.read_text() == edited
    else:
        assert toolbox.jobs._run_job(job) is (completion != 'cancelled')
    assert at_publication == [True]
    rows = toolbox.db.execute(sa.select(repository.History.c.action, repository.History.c.subtitles_path)
                              .order_by(repository.History.c.id)).all()
    listing = ast.literal_eval(repository.get_item(context.item_id).subtitles)
    assert rows[0] == (5, str(generated))
    if completion == 'cancelled':
        assert toolbox.jobs.get_job_status(job_id) == 'cancelled'
        assert path.read_text() == SRT and generated.read_text() == SYNCED
        assert len(rows) == 1
    else:
        assert toolbox.jobs.get_job_status(job_id) == 'completed'
        assert path.read_text() == replacement and rows == [(5, str(generated)), (5, str(path))]
        if completion.startswith('generated'):
            assert generated.read_text() == edited
        else:
            assert not generated.exists()
            assert not any(entry[1] == str(generated) for entry in listing)
            retired, = path.parent.glob('.bazarr-sync-obsolete-*.bak')
            assert retired.read_text() == SYNCED


@pytest.fixture
def toolbox_runtime(toolbox, monkeypatch):
    from app import database
    from app.config import settings
    from subtitles.tools.translate import runtime
    from subtitles.tools.translate.runtime import gate, service
    from translator_dispatch_helpers import Env

    monkeypatch.setattr(settings.translator, "runtime_v2", True)
    monkeypatch.setattr(database, "engine", toolbox.db.get_bind())
    monkeypatch.setattr(runtime, "_tables_present", False)
    monkeypatch.setattr(runtime, "_absent_until", 0.0)
    env = Env(toolbox.db, toolbox.world.tmp_path)
    try:
        monkeypatch.setattr(gate, "translation_gate", lambda: gate.evaluate(env.store, env.holds, env.workers))
        monkeypatch.setattr(service, "Dispatcher", env.dispatcher)
        yield env
    finally:
        env.close()


def test_manual_source_translation_dedup_uses_its_exact_sidecar(toolbox, toolbox_runtime):
    from subtitles.tools.subsync_engines import capture_subtitle_versions
    from subtitles.tools.translate import main

    env = toolbox_runtime
    env.ai_engine()
    context = pipeline._episode(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    target = Path(context.path).with_suffix(".hu.srt")
    auto = main.translate_subtitles_file(
        context.path, str(path), "en", "hu", False, False, "episode", None, None, None, None, source=context,
        expected_destination_versions=capture_subtitle_versions(context.path, (str(path), str(target))))
    first = _request(toolbox, context, path, "translate", language="hu")
    assert first.status_code == 202
    manual = first.json["job_id"]
    assert manual != auto
    assert _request(toolbox, context, path, "translate", language="hu").json["job_id"] == manual
    path.write_text(SRT.replace("Original cue", "Edited cue"))
    changed = _request(toolbox, context, path, "translate", language="hu")
    assert changed.status_code == 202 and changed.json["job_id"] not in (auto, manual)
    assert toolbox.jobs.remove_job_from_pending_queue(job_id=auto)
    stale = toolbox.jobs._reserve_next_job()
    assert stale.job_id == manual and toolbox.jobs._run_job(stale)
    assert not target.exists() and not env.requests()
    _run(toolbox, changed)
    assert "[hu] Edited cue" in target.read_text()


@pytest.mark.parametrize("touch_metadata", [False, True])
def test_manual_source_translation_pin_binds_at_single_video_capture(
        toolbox, toolbox_runtime, monkeypatch, touch_metadata):
    from app.database import TableTranslatorJob
    from subtitles.tools.translate import main

    env = toolbox_runtime
    instance = env.ai_engine()
    default = env.connection(instance, model="default")
    picked = env.connection(instance, model="selected")
    context = pipeline._episode(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    video = Path(context.path)
    version = main.source_translation_video_version
    captures = []

    def capture(path):
        current = version(path)
        captures.append(current)
        if touch_metadata and len(captures) == 1:
            video.chmod(0o600)
        return current

    monkeypatch.setattr(main, "source_translation_video_version", capture)
    response = _request(toolbox, context, path, "translate", language="hu", connection_id=picked.id)
    assert response.status_code == 202
    assert len(captures) == 1
    (row,) = toolbox.db.execute(sa.select(TableTranslatorJob)).scalars().all()
    assert (row.queue_job_id, row.pinned_connection_id) == (response.json["job_id"], picked.id)
    _run(toolbox, response)
    if touch_metadata:
        assert not env.requests()
    else:
        assert env.served() == [str(picked.id)]
        assert default.id != picked.id


def test_manual_source_translation_refuses_a_video_removed_at_capture(toolbox, monkeypatch):
    from subtitles.tools.translate import main

    context = pipeline._episode(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    version = main.source_translation_video_version

    def removed(video_path):
        Path(video_path).unlink()
        return version(video_path)

    monkeypatch.setattr(main, "source_translation_video_version", removed)
    response = _request(toolbox, context, path, "translate", language="hu")
    assert response.status_code == 409
    assert not toolbox.jobs.jobs_pending_queue


@pytest.mark.parametrize("encoding", ["utf-16", "cp1252"])
@pytest.mark.parametrize("runtime_on", [False, True], ids=["legacy", "runtime"])
def test_manual_source_translation_decodes_the_captured_editor_text(
        toolbox, toolbox_runtime, monkeypatch, encoding, runtime_on):
    from app.config import settings

    env = toolbox_runtime
    env.ai_engine()
    monkeypatch.setattr(settings.translator, "runtime_v2", runtime_on)
    context = pipeline._episode(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    cue = "Café"
    path.write_bytes(SRT.replace("<i>Original cue</i>", cue).encode(encoding))
    loaded = toolbox.client.get(f"/api/sources/items/{context.item_id}/subtitles/en/content", headers=HEADERS)
    assert loaded.status_code == 200
    assert cue in loaded.json["content"]
    _run(toolbox, _request(toolbox, context, path, "translate", language="hu"))
    target = Path(context.path).with_suffix(".hu.srt")
    if runtime_on:
        assert f"[hu] {cue}" in target.read_text()
    else:
        assert "Translated cue" in target.read_text()
        assert cue in str(toolbox.transport.return_value.translate.call_args)


def test_manual_source_translation_runs_through_v2_runtime(toolbox, monkeypatch):
    from app import database
    from app.config import settings
    from media_sources import repository
    from media_sources.indexer import SourcePublicationOwnerIndex
    from subtitles.tools.translate import main, runtime
    from subtitles.tools.translate.runtime import gate, service
    from translator_dispatch_helpers import Env

    context = pipeline._episode(toolbox.world)
    path, _ = _sidecars(toolbox, context)
    monkeypatch.setattr(settings.translator, 'runtime_v2', True)
    monkeypatch.setattr(database, 'engine', toolbox.db.get_bind())
    monkeypatch.setattr(runtime, '_tables_present', False)
    monkeypatch.setattr(runtime, '_absent_until', 0.0)
    assert runtime.runtime_v2_enabled()
    monkeypatch.setattr(main.TranslatorFactory, 'create_translator',
                        Mock(side_effect=AssertionError('Expected the configured runtime')))
    load_owners = Mock(side_effect=AssertionError('A sidecar beside its video needs only local ownership'))
    monkeypatch.setattr(SourcePublicationOwnerIndex, '_load_owners', load_owners)
    env = Env(toolbox.db, toolbox.world.tmp_path)
    try:
        env.ai_engine()
        monkeypatch.setattr(gate, 'translation_gate', lambda: gate.evaluate(env.store, env.holds, env.workers))
        monkeypatch.setattr(service, 'Dispatcher', env.dispatcher)
        _run(toolbox, _request(toolbox, context, path, 'translate', language='hu'))
        (request,) = env.requests()
        assert request['request']['media']['type'] == 'episode'
        translation = env.history(env.last_job())
        assert (translation['media_type'], translation['media_id'], translation['status']) == (
            'source', context.item_id, 'completed')
    finally:
        env.close()
    target = Path(context.path).with_suffix('.hu.srt')
    assert '[hu] Original cue' in target.read_text()
    (row,) = toolbox.db.execute(sa.select(repository.History)).all()
    assert (row.action, row.item_id, row.subtitles_path) == (6, context.item_id, str(target))
    load_owners.assert_not_called()


@pytest.mark.parametrize('change_during', ['budget_wait', 'worker_admission', 'worker_admission_edit'])
def test_source_translation_revalidates_input_before_worker_send(toolbox, monkeypatch, change_during):
    from app import database
    from app.config import settings
    from media_sources import repository
    from media_sources.indexer import index_item
    from subtitles.tools.translate import runtime
    from subtitles.tools.translate.runtime import gate, service
    from subtitles.tools.translate.runtime.jobs import TranslationJobs
    from subtitles.tools.translate.runtime.usage import CostLimits, UsageLedger
    from translator_dispatch_helpers import Env, usd

    context = pipeline._movie(toolbox.world)
    video = Path(context.path)
    folder = video.parent / 'Subs'
    folder.mkdir()
    path = folder / video.with_suffix('.en.srt').name
    path.write_text(SRT)
    monkeypatch.setattr(settings.general, 'subfolder', 'relative')
    monkeypatch.setattr(settings.general, 'subfolder_custom', folder.name)
    assert index_item(context.item_id) is not None
    monkeypatch.setattr(settings.translator, 'runtime_v2', True)
    monkeypatch.setattr(database, 'engine', toolbox.db.get_bind())
    monkeypatch.setattr(runtime, '_tables_present', False)
    monkeypatch.setattr(runtime, '_absent_until', 0.0)
    env = Env(toolbox.db, toolbox.world.tmp_path)
    try:
        instance = env.ai_engine()
        ledger = UsageLedger(env.session, clock=env.clock)
        monkeypatch.setattr(gate, 'translation_gate', lambda: gate.evaluate(env.store, env.holds, env.workers))
        moved = toolbox.world.tmp_path / 'other-subtitles'
        moved.mkdir()

        def change_input():
            if change_during == 'worker_admission_edit':
                path.write_text(SRT.replace('Original cue', 'A newer cue'))
            else:
                monkeypatch.setattr(settings.general, 'subfolder', 'absolute')
                monkeypatch.setattr(settings.general, 'subfolder_custom', str(moved))

        changed = []
        if change_during == 'budget_wait':
            env.limits = CostLimits(month_cap=usd(1), translation_limit=1)
            jobs = TranslationJobs(env.session, clock=env.clock)
            other = jobs.request('/other.en.srt', 'hu').job_id
            jobs.start(other, source_digest='other', engine_instance_id=instance.id, settings_revision=1)
            ledger.reserve_job_budget(str(other), env.limits)

            def release_budget(_seconds):
                change_input()
                changed.append(True)
                ledger.release_job(str(other))

            monkeypatch.setattr(service, 'Dispatcher', lambda: env.dispatcher(sleep=release_budget))
        else:
            lease = env.pool._lease

            def admit_worker(key):
                slot = lease(key)
                change_input()
                changed.append(True)
                return slot

            monkeypatch.setattr(env.pool, '_lease', admit_worker)
            monkeypatch.setattr(service, 'Dispatcher', env.dispatcher)

        _run(toolbox, _request(toolbox, context, path, 'translate', language='hu'))
        assert changed == [True]
        assert env.requests() == []
        job = env.last_job()
        assert (job.state, job.error_class) == ('cancelled', 'source_changed')
        history = env.history(job)
        assert (history['status'], history['error_class']) == ('cancelled', 'source_changed')
        assert ledger.job_hold(str(job.id)) == 0
    finally:
        env.close()
    target_name = video.with_suffix('.hu.srt').name
    assert not (folder / target_name).exists()
    assert not (moved / target_name).exists()
    assert toolbox.db.execute(sa.select(repository.History)).all() == []

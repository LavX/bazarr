"""Translation progress through submission, polling and subtitle publication.

The AI translation reports progress on its Bazarr job and fails by raising, so the
jobs queue records the outcome. It no longer drives the old progress and message
socket channels, which put a second, disconnected notification on screen.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app import event_handler
from subtitles.tools.translate.services import openrouter_translator as module


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    source = tmp_path / 'source.en.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n', encoding='utf-8')
    monkeypatch.setattr(module.settings.translator, 'translator_info', False)
    monkeypatch.setattr(module.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(module, 'get_title', lambda **kwargs: 'Example')
    monkeypatch.setattr(module, 'get_translator_auth_headers', lambda: {})
    monkeypatch.setattr(module, 'language_from_alpha2', lambda code: {'en': 'English', 'hu': 'Hungarian'}[code])
    monkeypatch.setattr(module, 'language_from_alpha3', lambda code: 'Hungarian')
    progress = []
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kwargs: progress.append(kwargs))
    monkeypatch.setattr(module, 'history_log', lambda **kwargs: None)
    events = []
    monkeypatch.setattr(event_handler.socketio, 'emit', lambda event, payload: events.append(payload))
    service = module.OpenRouterTranslatorService(
        str(source), str(tmp_path / 'output.hu.srt'), None, 'hun', 'en', 'episode',
        str(tmp_path / 'video.mkv'), 'hu', False, False, 1, 2, None)
    monkeypatch.setattr(service, '_get_api_key_value', lambda: '')
    payloads = []

    def post(url, *, json, **kwargs):
        payloads.append(json)
        return response({'jobId': 'sidecar-job'})

    monkeypatch.setattr(module.requests, 'post', post)
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
    return SimpleNamespace(service=service, events=events, payloads=payloads, clock=clock, progress=progress)


def response(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload)


def assert_no_legacy_channels(events):
    assert [event for event in events if event['type'] in ('progress', 'message')] == []


@pytest.mark.parametrize('mode, expected', [
    ('disabled', {'enabled': False}), ('low', {'effort': 'low'}),
    ('high', {'effort': 'high'}), (None, {'enabled': False}),
])
def test_reasoning_selection_reaches_sidecar_request(lifecycle, monkeypatch, mode, expected):
    monkeypatch.setattr(module.settings.translator, 'openrouter_reasoning', mode)
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({'status': 'cancelled'}))
    with pytest.raises(module.TranslationServiceError):
        lifecycle.service.translate()
    assert lifecycle.payloads[0]['config']['reasoning'] == expected


@pytest.mark.parametrize('host_id', [None, 41])
@pytest.mark.parametrize('exit_kind, reason', [
    ('completed', None), ('partial', None), ('sync', None),
    ('failed', 'Fixture failure'), ('cancelled', 'cancelled'), ('hard_cap', '12 hours'),
    ('unreachable', 'unreachable'), ('invalid_status', 'AI translation failed'),
    ('host_cancelled', None), ('submit_error', 'Cannot connect'), ('write_error', 'Fixture save failure'),
])
def test_every_exit_reports_through_the_job_only(lifecycle, monkeypatch, host_id, exit_kind, reason):
    lines = [{'position': 0, 'line': 'Szia'}]
    responses = iter([
        response({'status': 'processing', 'progress': 100, 'message': 'Finishing'}),
        response({'status': exit_kind, 'progress': 100, 'result': {'lines': lines}, 'error': 'Fixture failure'}),
    ])
    calls = 0

    def get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return next(responses)
        if exit_kind == 'unreachable':
            raise requests.ConnectionError('Fixture unavailable')
        if exit_kind == 'invalid_status':
            return response(None)
        if exit_kind == 'host_cancelled':
            raise module.JobCancelled()
        if exit_kind == 'write_error':
            return response({'status': 'completed', 'result': {'lines': lines}})
        return next(responses)

    monkeypatch.setattr(module.requests, 'get', get)
    if exit_kind == 'hard_cap':
        monkeypatch.setattr(module, 'POLL_HARD_CAP_SECONDS', 2)
    if exit_kind == 'unreachable':
        monkeypatch.setattr(module, 'POLL_UNREACHABLE_LIMIT_SECONDS', 3)
    if exit_kind == 'submit_error':
        def fail_post(*args, **kwargs):
            raise requests.ConnectionError('Fixture submit failure')
        monkeypatch.setattr(module.requests, 'post', fail_post)
    if exit_kind == 'sync':
        posts = iter([SimpleNamespace(status_code=404), response(lines)])
        monkeypatch.setattr(module.requests, 'post', lambda *args, **kwargs: next(posts))
    if exit_kind == 'write_error':
        def fail_save(*args, **kwargs):
            raise OSError('Fixture save failure')
        monkeypatch.setattr(module.pysubs2.SSAFile, 'save', fail_save)

    if exit_kind == 'host_cancelled':
        with pytest.raises(module.JobCancelled):
            lifecycle.service.translate(job_id=host_id)
    elif reason:
        with pytest.raises(module.TranslationServiceError, match=reason):
            lifecycle.service.translate(job_id=host_id)
        assert not Path(lifecycle.service.dest_srt_file).exists()
    else:
        result = lifecycle.service.translate(job_id=host_id)
        assert result == lifecycle.service.dest_srt_file
        assert 'Szia' in Path(result).read_text(encoding='utf-8')
    assert_no_legacy_channels(lifecycle.events)


def test_remote_progress_is_mirrored_onto_the_host_job(lifecycle, monkeypatch):
    polls = iter([
        response({'status': 'processing', 'progress': 40, 'message': 'Batch 2 of 5', 'model_used': 'm'}),
        response({'status': 'completed', 'progress': 100, 'result': {'lines': [{'position': 0, 'line': 'Szia'}]}}),
    ])
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: next(polls))

    lifecycle.service.translate(job_id=41)

    mirrored = [update for update in lifecycle.progress if 'progress_value' in update]
    assert mirrored[0] == {'job_id': 41, 'progress_value': 40, 'progress_max': 100,
                           'progress_message': 'Batch 2 of 5 [m]'}
    assert_no_legacy_channels(lifecycle.events)


def test_real_host_queue_cancellation_propagates(lifecycle, monkeypatch):
    queue = module.jobs_queue
    monkeypatch.setattr(queue, 'update_job_progress', type(queue).update_job_progress.__get__(queue))
    monkeypatch.setattr(queue, 'jobs_running_queue', [SimpleNamespace(
        job_id=41, job_name='Fixture translation', cancelled=True)])
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({
        'status': 'processing', 'progress': 100, 'message': 'Finalizing'}))

    with pytest.raises(module.JobCancelled):
        lifecycle.service.translate(job_id=41)

    assert not Path(lifecycle.service.dest_srt_file).exists()
    assert_no_legacy_channels(lifecycle.events)


@pytest.mark.parametrize('host_id', [None, 41])
@pytest.mark.parametrize('exit_kind', ['success', 'save_error', 'cancel_before_publish', 'publish_error'])
def test_subtitle_publication_order_and_failures(lifecycle, monkeypatch, host_id, exit_kind):
    from subtitles.tools import subsync_engines

    destination = Path(lifecycle.service.dest_srt_file)
    destination.write_text('Existing subtitle', encoding='utf-8')
    observations = []

    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({
        'status': 'completed', 'progress': 100,
        'result': {'lines': [{'position': 0, 'line': 'Szia'}]},
    }))
    real_save = module.pysubs2.SSAFile.save
    real_replace = subsync_engines.os.replace

    def save(subtitles, path, *args, **kwargs):
        observations.append('save')
        if exit_kind == 'save_error':
            raise OSError('Fixture save failure')
        return real_save(subtitles, path, *args, **kwargs)

    def host_progress(**kwargs):
        if 'progress_value' not in kwargs:
            observations.append('before_publish')
            if exit_kind == 'cancel_before_publish':
                raise module.JobCancelled()

    def replace(source, target):
        observations.append('replace')
        if exit_kind == 'publish_error':
            raise OSError('Fixture publication failure')
        real_replace(source, target)
        assert 'Szia' in destination.read_text(encoding='utf-8')
        observations.append('published')

    monkeypatch.setattr(module.pysubs2.SSAFile, 'save', save)
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', host_progress)
    monkeypatch.setattr(subsync_engines.os, 'replace', replace)
    if exit_kind == 'cancel_before_publish':
        with pytest.raises(module.JobCancelled):
            lifecycle.service.translate(job_id=host_id)
    elif exit_kind == 'success':
        assert lifecycle.service.translate(job_id=host_id) == str(destination)
    else:
        with pytest.raises(module.TranslationServiceError, match='Fixture'):
            lifecycle.service.translate(job_id=host_id)

    assert_no_legacy_channels(lifecycle.events)
    assert not list(destination.parent.glob('.bazarr-write-*'))
    if exit_kind == 'success':
        assert observations == ['save', 'before_publish', 'replace', 'published']
    else:
        assert destination.read_text(encoding='utf-8') == 'Existing subtitle'

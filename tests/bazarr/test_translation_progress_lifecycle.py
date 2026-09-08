"""Translation progress through submission, polling and subtitle publication."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
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
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    monkeypatch.setattr(module, 'history_log', lambda **kwargs: None)
    monkeypatch.setattr(module, 'show_message', lambda message: None)
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
    return SimpleNamespace(service=service, events=events, payloads=payloads, clock=clock)


def response(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload)


def assert_lifecycle(events):
    progress = [event for event in events if event['type'] == 'progress']
    updates = [event['payload'] for event in progress if event['action'] == 'update']
    deletes = [event['payload'] for event in progress if event['action'] == 'delete']
    assert updates
    identity = updates[0]['id']
    assert identity.startswith('translate_progress_')
    assert all(update['id'] == identity for update in updates)
    assert deletes == [identity]
    assert progress[-1]['action'] == 'delete'
    return identity


@pytest.mark.parametrize('mode, expected', [
    ('disabled', {'enabled': False}), ('low', {'effort': 'low'}),
    ('high', {'effort': 'high'}), (None, None),
])
def test_reasoning_selection_reaches_sidecar_request(lifecycle, monkeypatch, mode, expected):
    monkeypatch.setattr(module.settings.translator, 'openrouter_reasoning', mode)
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({'status': 'cancelled'}))
    lifecycle.service.translate()
    assert lifecycle.payloads[0]['config']['reasoning'] == expected


@pytest.mark.parametrize('host_id', [None, 41])
@pytest.mark.parametrize('exit_kind', [
    'completed', 'partial', 'failed', 'cancelled', 'hard_cap', 'unreachable',
    'invalid_status', 'host_cancelled', 'submit_error', 'write_error', 'sync',
])
def test_progress_cleanup_covers_real_translation_exits(lifecycle, monkeypatch, host_id, exit_kind):
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
    else:
        result = lifecycle.service.translate(job_id=host_id)
        if exit_kind in ('completed', 'partial', 'sync'):
            assert result == lifecycle.service.dest_srt_file
            assert 'Szia' in Path(result).read_text(encoding='utf-8')
        else:
            assert result is False
    assert_lifecycle(lifecycle.events)


@pytest.mark.parametrize('host_id', [None, 41])
def test_four_operations_with_same_destination_have_independent_notifications(lifecycle, monkeypatch, host_id):
    barrier = Barrier(4)
    lock = Lock()
    arrivals = []

    def get(*args, **kwargs):
        with lock:
            arrivals.append(1)
        barrier.wait(timeout=5)
        return response({'status': 'completed', 'result': {'lines': [{'position': 0, 'line': 'Szia'}]}})

    monkeypatch.setattr(module.requests, 'get', get)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(lifecycle.service._submit_and_poll, ['Hello'], host_id) for _ in range(4)]
        assert all(future.result(timeout=10) == [{'position': 0, 'line': 'Szia'}] for future in futures)
    assert len(arrivals) == 4
    updates = [event['payload']['id'] for event in lifecycle.events if event['action'] == 'update']
    deletes = [event['payload'] for event in lifecycle.events if event['action'] == 'delete']
    assert len(set(updates)) == 4
    assert len(deletes) == 4
    assert set(deletes) == set(updates)
    for identity in set(updates):
        assert_lifecycle([event for event in lifecycle.events if (
            event['payload'] == identity or isinstance(event['payload'], dict) and event['payload']['id'] == identity)])


def test_reusing_service_allocates_a_new_operation_identity(lifecycle, monkeypatch):
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({'status': 'cancelled'}))
    lifecycle.service.translate()
    first = assert_lifecycle(lifecycle.events)
    lifecycle.events.clear()
    lifecycle.service.translate()
    assert assert_lifecycle(lifecycle.events) != first


def test_standalone_poll_cleans_up_on_unexpected_error(lifecycle, monkeypatch):
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response(None))
    with pytest.raises(AttributeError):
        lifecycle.service._poll_job('http://fixture', 'job', 1)
    assert_lifecycle(lifecycle.events)


def test_real_host_queue_cancellation_propagates_and_cleans_progress(lifecycle, monkeypatch):
    queue = module.jobs_queue
    monkeypatch.setattr(queue, 'update_job_progress', type(queue).update_job_progress.__get__(queue))
    monkeypatch.setattr(queue, 'jobs_running_queue', [SimpleNamespace(
        job_id=41, job_name='Fixture translation', cancelled=True)])
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({
        'status': 'processing', 'progress': 100, 'message': 'Finalizing'}))

    with pytest.raises(module.JobCancelled):
        lifecycle.service.translate(job_id=41)

    assert not Path(lifecycle.service.dest_srt_file).exists()
    assert_lifecycle(lifecycle.events)


@pytest.mark.parametrize('host_id', [None, 41])
@pytest.mark.parametrize('exit_kind', ['success', 'save_error', 'cancel_before_publish', 'publish_error'])
def test_notification_stays_active_through_subtitle_publication(lifecycle, monkeypatch, host_id, exit_kind):
    from subtitles.tools import subsync_engines

    destination = Path(lifecycle.service.dest_srt_file)
    destination.write_text('Existing subtitle', encoding='utf-8')
    observations = []

    def observe(stage):
        deletes = [event for event in lifecycle.events
                   if event['type'] == 'progress' and event['action'] == 'delete']
        observations.append((stage, len(deletes)))

    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: response({
        'status': 'completed', 'progress': 100,
        'result': {'lines': [{'position': 0, 'line': 'Szia'}]},
    }))
    real_save = module.pysubs2.SSAFile.save
    real_replace = subsync_engines.os.replace

    def save(subtitles, path, *args, **kwargs):
        observe('save')
        if exit_kind == 'save_error':
            raise OSError('Fixture save failure')
        return real_save(subtitles, path, *args, **kwargs)

    def host_progress(**kwargs):
        if 'progress_value' not in kwargs:
            observe('before_publish')
            if exit_kind == 'cancel_before_publish':
                raise module.JobCancelled()

    def replace(source, target):
        observe('replace')
        if exit_kind == 'publish_error':
            raise OSError('Fixture publication failure')
        real_replace(source, target)
        assert 'Szia' in destination.read_text(encoding='utf-8')
        observe('published')

    monkeypatch.setattr(module.pysubs2.SSAFile, 'save', save)
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', host_progress)
    monkeypatch.setattr(subsync_engines.os, 'replace', replace)
    if exit_kind == 'cancel_before_publish':
        with pytest.raises(module.JobCancelled):
            lifecycle.service.translate(job_id=host_id)
    else:
        result = lifecycle.service.translate(job_id=host_id)
        assert result == (str(destination) if exit_kind == 'success' else False)

    assert_lifecycle(lifecycle.events)
    assert observations
    assert all(deletes == 0 for stage, deletes in observations), observations
    assert not list(destination.parent.glob('.bazarr-write-*'))
    if exit_kind == 'success':
        assert [stage for stage, deletes in observations] == ['save', 'before_publish', 'replace', 'published']
    else:
        assert destination.read_text(encoding='utf-8') == 'Existing subtitle'

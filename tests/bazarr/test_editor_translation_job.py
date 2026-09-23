# coding=utf-8
"""Subtitle editor translations run through the jobs queue.

The editor used to submit straight to the AI Subtitle Translator and poll it from
the browser, so its work never showed in Jobs and a failure only reached the open
panel. It now queues a job with progress, the job raises the service's reason when
it fails, and the editor reads the translated lines back from the finished job.
"""
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

from api.translator import translator as api_mod
from app import jobs_queue as jobs_queue_module
from subtitles.tools.translate import editor
from subtitles.tools.translate.services import openrouter_translator
from utilities import job_dedupe

LINES = [{'position': 0, 'line': 'Hello'}, {'position': 1, 'line': 'World'}]


@pytest.fixture
def queue(monkeypatch):
    fresh = jobs_queue_module.JobsQueue()
    events = []
    monkeypatch.setattr(jobs_queue_module, 'event_stream', lambda **kwargs: events.append(kwargs))
    for module in (editor, job_dedupe, openrouter_translator):
        monkeypatch.setattr(module, 'jobs_queue', fresh)
    fresh.events = events
    return fresh


@pytest.fixture
def sidecar(monkeypatch):
    for key, value in {
        'openrouter_url': 'http://sidecar:8765',
        'openrouter_api_key': 'sk-or-key',
        'openrouter_encryption_key': '',
        'openrouter_model': 'deepseek/example',
        'openrouter_provider_routing': 'throughput',
    }.items():
        monkeypatch.setattr(openrouter_translator.settings.translator, key, value)
    openrouter_translator.reset_sidecar_version_cache()
    monkeypatch.setattr(openrouter_translator, 'get_translator_auth_headers', lambda: {})
    monkeypatch.setattr(editor, 'get_translator_auth_headers', lambda: {})
    monkeypatch.setattr(openrouter_translator.time, 'sleep', lambda seconds: None)
    calls = SimpleNamespace(posts=[], deletes=[], polls=[])

    def post(url, json=None, **kwargs):
        calls.posts.append(json)
        return SimpleNamespace(status_code=200, json=lambda: {'jobId': 'remote-1'})

    def get(url, **kwargs):
        if url.endswith('/health'):
            return SimpleNamespace(status_code=200, json=lambda: {'status': 'healthy', 'version': '2.0.0'})
        status = calls.polls.pop(0)
        return SimpleNamespace(status_code=200, json=lambda: status)

    monkeypatch.setattr(openrouter_translator.requests, 'post', post)
    monkeypatch.setattr(openrouter_translator.requests, 'get', get)
    monkeypatch.setattr(editor.requests, 'delete', lambda url, **kwargs: calls.deletes.append(url))
    return calls


def _only_job(queue):
    jobs = queue.list_jobs_from_queue()
    assert len(jobs) == 1
    return jobs[0]


def _run(queue, job_id):
    job = next(job for job in queue.jobs_pending_queue if job.job_id == job_id)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)
    queue._run_job(job)
    return job


def test_an_editor_translation_is_queued_with_progress_and_a_readable_name(queue):
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian', title='Example')

    job = _only_job(queue)
    assert job['job_id'] == job_id
    assert job['job_name'] == 'Translating Example in the editor (English to Hungarian)'
    assert job['module'] == 'subtitles.tools.translate.editor'
    assert job['func'] == 'translate_editor_lines'
    assert job['is_progress'] is True
    assert queue.events[-1]['payload'] == {'job_id': job_id, 'progress_value': None, 'status': 'pending'}
    # The kwargs hold the lines only, never the API key.
    assert 'sk-or-key' not in repr(job['kwargs'])


def test_a_single_line_names_itself(queue):
    editor.enqueue_editor_translation([{'position': 0, 'line': 'Hi'}], '', 'Hungarian')
    assert _only_job(queue)['job_name'] == 'Translating one line in the editor (auto-detected to Hungarian)'


def test_the_same_translation_follows_the_job_already_queued(queue):
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    second = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    other = editor.enqueue_editor_translation(LINES, 'English', 'German')

    assert second == first
    assert other != first
    assert len(queue.list_jobs_from_queue()) == 2


def test_a_finished_job_hands_the_lines_back_at_their_editor_positions(queue, sidecar):
    shifted = [{'position': 4, 'line': 'Hello'}, {'position': 7, 'line': 'World'}]
    job_id = editor.enqueue_editor_translation(shifted, 'English', 'Hungarian', title='Example')
    sidecar.polls = [
        {'status': 'processing', 'progress': 50, 'message': 'Batch 1 of 2'},
        {'status': 'completed', 'progress': 100,
         'result': {'lines': [{'position': 0, 'line': 'Szia'}, {'position': 1, 'line': 'Világ'}]}},
    ]

    job = _run(queue, job_id)

    assert job.status == 'completed'
    # The service's progress was mirrored onto the job the drawer and editor read.
    assert (job.progress_value, job.progress_max) == (100, 100)
    assert job.job_name.startswith('Translated Example')
    assert sidecar.posts[0]['lines'] == [{'position': 0, 'line': 'Hello'}, {'position': 1, 'line': 'World'}]
    assert sidecar.posts[0]['title'] == 'Example'
    assert editor.editor_translation_state(job_id) == {
        'jobId': job_id, 'status': 'completed', 'partial': None,
        'lines': [{'position': 4, 'line': 'Szia'}, {'position': 7, 'line': 'Világ'}],
    }


def test_an_unreachable_service_fails_the_job_with_the_reason(queue, sidecar, monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.exceptions.ConnectionError('refused')

    monkeypatch.setattr(openrouter_translator.requests, 'post', refuse)
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')

    job = _run(queue, job_id)

    assert job.status == 'failed'
    assert job in queue.jobs_failed_queue
    assert job.job_name.startswith('Failed')
    state = editor.editor_translation_state(job_id)
    assert state['status'] == 'failed'
    assert 'Cannot connect to the AI Subtitle Translator' in state['error']
    assert [event for event in queue.events if event['type'] in ('progress', 'message')] == []


def test_a_service_side_failure_is_the_reason(queue, sidecar):
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    sidecar.polls = [{'status': 'failed', 'error': 'OpenRouter rejected the key'}]

    job = _run(queue, job_id)

    assert job.status == 'failed'
    assert 'OpenRouter rejected the key' in editor.editor_translation_state(job_id)['error']


def test_cancelling_the_job_cancels_the_remote_translation(queue, sidecar, monkeypatch):
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')

    def get(url, **kwargs):
        # The user presses cancel while the service is still working.
        for job in queue.jobs_running_queue:
            job.cancelled = True
        return SimpleNamespace(status_code=200, json=lambda: {'status': 'processing', 'progress': 10})

    monkeypatch.setattr(openrouter_translator, 'build_routing_config', lambda: ('deepseek/example', {}))
    monkeypatch.setattr(openrouter_translator.requests, 'get', get)

    job = _run(queue, job_id)

    assert job.status == 'completed'
    assert job.progress_message == 'Cancelled by user'
    assert sidecar.deletes == ['http://sidecar:8765/api/v1/jobs/remote-1']
    assert editor.editor_translation_state(job_id)['status'] == 'cancelled'


def test_the_state_answers_only_for_editor_translation_jobs(queue):
    other = queue.feed_jobs_pending_queue('Something else', 'utilities.cache', 'cache_maintenance')
    assert editor.editor_translation_state(other) is None
    assert editor.editor_translation_state(999) is None


class TestEditorTranslationEndpoint:
    @staticmethod
    def _call(method, **context):
        app = Flask(__name__)
        with app.test_request_context('/api/translator/editor', method=method.upper(), **context):
            resource = api_mod.TranslatorEditorJobs()
            return getattr(api_mod.TranslatorEditorJobs, method).__wrapped__(resource)

    def test_post_queues_and_answers_202_with_the_job(self, queue, sidecar):
        body, status = self._call('post', json={'lines': LINES, 'targetLanguage': 'Hungarian',
                                                'sourceLanguage': 'English', 'title': 'Example'})
        assert status == 202
        assert body == {'jobId': _only_job(queue)['job_id']}
        # Nothing reached the service during the request.
        assert sidecar.posts == []

    def test_post_refuses_a_request_without_lines_or_language(self, queue, sidecar):
        assert self._call('post', json={'lines': [], 'targetLanguage': 'hu'})[1] == 400
        assert self._call('post', json={'lines': LINES})[1] == 400
        assert self._call('post', json={'lines': ['bare'], 'targetLanguage': 'hu'})[1] == 400
        assert queue.list_jobs_from_queue() == []

    def test_post_without_a_configured_service_is_unavailable(self, queue, monkeypatch):
        monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_url', '')
        assert self._call('post', json={'lines': LINES, 'targetLanguage': 'hu'})[1] == 503

    def test_get_reads_the_job_state(self, queue, sidecar):
        job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
        body, status = self._call('get', query_string={'jobId': job_id})
        assert status == 200
        assert body == {'jobId': job_id, 'status': 'pending'}
        assert self._call('get', query_string={'jobId': 12345})[1] == 404
        assert self._call('get', query_string={'jobId': 'x'})[1] == 400

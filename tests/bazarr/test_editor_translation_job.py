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
    # A fresh queue numbers its jobs from 1 again, so earlier results must go.
    monkeypatch.setattr(editor, '_results', type(editor._results)())
    monkeypatch.setattr(editor, '_followers', type(editor._followers)())
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
    monkeypatch.setattr(openrouter_translator.requests, 'delete',
                        lambda url, **kwargs: calls.deletes.append(url))
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
    assert queue.events[-1]['payload'] == {'job_id': job_id, 'progress_value': None, 'status': 'pending',
                                           'session': job['session']}
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


def test_the_same_translation_follows_the_first_job_even_when_it_ends_at_once(queue, monkeypatch):
    """The duplicate is matched and its id read in one step, under the queue
    lock. The id used to be looked up afterwards, and when the first job had
    finished in between the editor was told the translation could not be
    queued."""
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    running = queue.jobs_pending_queue.popleft()
    queue.jobs_running_queue.append(running)
    feed = queue.feed_jobs_pending_queue

    def first_job_ends_right_after_the_answer(*args, **kwargs):
        answer = feed(*args, **kwargs)
        with queue._queue_lock:
            queue.jobs_running_queue.remove(running)
        running.status = 'completed'
        queue.jobs_completed_queue.append(running)
        return answer

    monkeypatch.setattr(queue, 'feed_jobs_pending_queue', first_job_ends_right_after_the_answer)

    assert editor.enqueue_editor_translation(LINES, 'English', 'Hungarian') == first
    assert list(queue.jobs_pending_queue) == []


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
    assert job.error['message'] == state['error']
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

    assert job.status == 'cancelled'
    assert job.progress_message == 'Cancelled by user'
    assert sidecar.deletes == ['http://sidecar:8765/api/v1/jobs/remote-1']
    assert editor.editor_translation_state(job_id)['status'] == 'cancelled'


def test_a_stop_after_the_synchronous_fallback_returned_still_ends_the_job_cancelled(queue, monkeypatch):
    """The service's synchronous fallback has no cancel check of its own, so a Stop pressed while it ran
    is only seen once it returns. The lines it brought back must not be kept as a finished translation."""
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')

    def fallback(self, lines, source_language, target_language, title, bazarr_job_id=None, **fields):
        for running in queue.jobs_running_queue:
            running.cancelled = True
        return [{'position': 0, 'line': 'Szia'}, {'position': 1, 'line': 'Vilag'}]

    monkeypatch.setattr(editor.OpenRouterTranslatorService, 'submit_content', fallback)

    job = _run(queue, job_id)

    assert job.status == 'cancelled'
    # It was not renamed as translated, which would say the lines had landed.
    assert job.job_name.startswith('Translating ')
    assert editor._retained_result(job_id) is None
    assert editor.editor_translation_state(job_id)['status'] == 'cancelled'


def test_one_client_stopping_a_shared_translation_leaves_it_running_for_the_other(queue):
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    second = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    assert second == first

    # The first client lets go; the second is still waiting on the same job.
    assert editor.cancel_editor_translation(first) is True
    assert [pending.job_id for pending in queue.jobs_pending_queue] == [first]

    # The last one to let go stops it.
    assert editor.cancel_editor_translation(first) is True
    assert queue.jobs_pending_queue == type(queue.jobs_pending_queue)()
    assert editor.cancel_editor_translation(first) is False


def test_a_shared_running_translation_is_stopped_by_the_last_client_only(queue):
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    job = next(job for job in queue.jobs_pending_queue if job.job_id == first)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)

    assert editor.cancel_editor_translation(first) is True
    assert job.cancelled is False
    assert editor.cancel_editor_translation(first) is True
    assert job.cancelled is True


def test_an_editor_that_only_closes_does_not_leave_the_last_stop_hollow(queue):
    """A client that closes the editor without pressing Stop lets go of the translation. If it stayed counted,
    the one still waiting would press Stop, be told it worked, and the translation would run on and spend
    credits."""
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    job = next(job for job in queue.jobs_pending_queue if job.job_id == first)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)

    # One editor closes: its release is not a Stop, and the job runs on for the other.
    assert editor.release_editor_translation(first) is True
    assert job.cancelled is False

    # The one still waiting presses Stop and it is the stop.
    assert editor.cancel_editor_translation(first) is True
    assert job.cancelled is True


def test_the_last_editor_closing_leaves_the_translation_running(queue):
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    job = next(job for job in queue.jobs_pending_queue if job.job_id == first)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)

    assert editor.release_editor_translation(first) is True

    # Closing is not stopping: the work was queued to outlive the editor.
    assert job.cancelled is False
    assert [running.job_id for running in queue.jobs_running_queue] == [first]
    # Whoever asks for it next is its only follower, and their Stop is the stop.
    assert editor.enqueue_editor_translation(LINES, 'English', 'Hungarian') == first
    assert editor.cancel_editor_translation(first) is True
    assert job.cancelled is True


def test_only_an_editor_translation_can_be_released(queue):
    other = queue.feed_jobs_pending_queue('Something else', 'utilities.cache', 'cache_maintenance')
    assert editor.release_editor_translation(other) is False
    assert editor.release_editor_translation(999) is False


def test_the_stop_route_can_release_instead_of_stopping(queue):
    from flask import Flask

    from api.translator.translator import TranslatorEditorJobs

    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    job = next(job for job in queue.jobs_pending_queue if job.job_id == first)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)
    resource = TranslatorEditorJobs()

    with Flask(__name__).test_request_context(f'/?jobId={first}&release=true', method='DELETE'):
        assert TranslatorEditorJobs.delete.__wrapped__(resource) == ('', 204)
    assert job.cancelled is False

    with Flask(__name__).test_request_context(f'/?jobId={first}', method='DELETE'):
        assert TranslatorEditorJobs.delete.__wrapped__(resource) == ('', 204)
    assert job.cancelled is True

    with Flask(__name__).test_request_context('/?jobId=999&release=true', method='DELETE'):
        assert TranslatorEditorJobs.delete.__wrapped__(resource)[1] == 404


def test_a_translation_asked_for_again_after_it_ended_starts_with_one_client(queue, sidecar):
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    sidecar.polls.append({'status': 'completed', 'result': {'lines': [{'position': 0, 'line': 'a'}]}})
    _run(queue, first)

    again = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')

    assert again != first
    # One client, so its stop is the stop.
    assert editor.cancel_editor_translation(again) is True
    assert queue.jobs_pending_queue == type(queue.jobs_pending_queue)()


def test_stopping_a_shared_translation_that_already_ended_finds_nothing_to_stop(queue, sidecar):
    """Two editors share the job and it finishes. Their followers are cleared with
    it, so a late Stop answers that there is nothing left. A follower count that
    survived the job would make the Stop look like one editor letting go of a job
    that still runs, and answer success for a stop that did nothing."""
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    assert editor.enqueue_editor_translation(LINES, 'English', 'Hungarian') == first
    sidecar.polls.append({'status': 'completed', 'result': {'lines': [{'position': 0, 'line': 'a'}]}})
    _run(queue, first)

    assert editor.cancel_editor_translation(first) is False


def test_stopping_a_shared_translation_that_failed_finds_nothing_to_stop(queue, sidecar):
    first = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    sidecar.polls.append({'status': 'failed', 'error': 'The model refused'})
    assert _run(queue, first).status == 'failed'

    assert editor.cancel_editor_translation(first) is False


def test_the_state_answers_only_for_editor_translation_jobs(queue):
    other = queue.feed_jobs_pending_queue('Something else', 'utilities.cache', 'cache_maintenance')
    assert editor.editor_translation_state(other) is None
    assert editor.editor_translation_state(999) is None


def test_cancel_stops_a_queued_or_a_running_editor_translation(queue):
    queued = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    running = editor.enqueue_editor_translation(LINES, 'English', 'German')
    job = next(job for job in queue.jobs_pending_queue if job.job_id == running)
    queue.jobs_pending_queue.remove(job)
    queue.jobs_running_queue.append(job)
    other = queue.feed_jobs_pending_queue('Something else', 'utilities.cache', 'cache_maintenance')

    assert editor.cancel_editor_translation(queued) is True
    assert all(pending.job_id != queued for pending in queue.jobs_pending_queue)
    assert editor.cancel_editor_translation(running) is True
    assert job.cancelled is True
    # Only editor translations can be stopped through this door.
    assert editor.cancel_editor_translation(other) is False
    assert editor.cancel_editor_translation(999) is False


def test_a_result_outlives_its_job_leaving_the_bounded_queue(queue, sidecar):
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    sidecar.polls = [{'status': 'completed', 'progress': 100,
                      'result': {'lines': [{'position': 0, 'line': 'Szia'}, {'position': 1, 'line': 'Világ'}]}}]
    _run(queue, job_id)
    # Ten unrelated jobs finishing push it out of the queue's completed list.
    queue.jobs_completed_queue.clear()

    state = editor.editor_translation_state(job_id)

    assert state['status'] == 'completed'
    assert state['lines'] == [{'position': 0, 'line': 'Szia'}, {'position': 1, 'line': 'Világ'}]


def test_a_failure_outlives_its_job_leaving_the_bounded_queue(queue, sidecar):
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    sidecar.polls = [{'status': 'failed', 'error': 'OpenRouter rejected the key'}]
    job = _run(queue, job_id)
    queue.jobs_failed_queue.clear()

    state = editor.editor_translation_state(job_id)

    assert state == {'jobId': job_id, 'status': 'failed', 'error': job.error['message']}
    assert 'OpenRouter rejected the key' in state['error']


def test_an_unexpected_failure_keeps_the_same_message_the_job_shows(queue, sidecar, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError('/secret/path in a provider payload')

    monkeypatch.setattr(openrouter_translator.OpenRouterTranslatorService, 'submit_content', broken)
    job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
    job = _run(queue, job_id)
    queue.jobs_failed_queue.clear()

    state = editor.editor_translation_state(job_id)

    assert state['status'] == 'failed'
    assert state['error'] == job.error['message'] == jobs_queue_module.UNEXPECTED_JOB_ERROR['message']


def test_a_library_translation_failure_reason_lands_on_its_job(monkeypatch):
    from subtitles.tools.translate import main

    recorded = []
    monkeypatch.setattr(main.jobs_queue, 'update_job_progress', lambda **kwargs: recorded.append(kwargs))
    monkeypatch.setattr(main.jobs_queue, 'get_job_name', lambda job_id: 'Translating Example')
    monkeypatch.setattr(main.jobs_queue, 'update_job_name', lambda **kwargs: None)
    monkeypatch.setattr(main, 'get_title', lambda *args, **kwargs: 'Example')

    def refuse(*args, **kwargs):
        raise openrouter_translator.TranslationServiceError('Cannot connect to the AI Subtitle Translator service.')

    monkeypatch.setattr(main, 'validate_translation_params', refuse)

    from app.jobs_queue import JobFailed

    # The service error is the queue's JobFailed, so its reason is the job's
    # error; the progress message is left alone.
    assert issubclass(openrouter_translator.TranslationServiceError, JobFailed)
    with pytest.raises(openrouter_translator.TranslationServiceError,
                       match='Cannot connect to the AI Subtitle Translator service.'):
        main.translate_subtitles_file('/v.mkv', '/v.en.srt', 'en', 'hu', False, False, 'movie', None, None, 1, {},
                                      job_id=5)

    assert not any('progress_message' in call and 'Cannot connect' in str(call['progress_message'])
                   for call in recorded)


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

    def test_post_without_a_configured_service_refuses_with_the_gate_reason(self, queue,
                                                                             monkeypatch):
        """The editor asks the translation gate about the AI Subtitle Translator
        service it actually uses, so the URL check it used to make answers with
        the gate's reason rather than its own."""
        monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_url', '')
        assert self._call('post', json={'lines': LINES, 'targetLanguage': 'hu'}) == (
            {'error': 'The AI Subtitle Translator service URL is not configured'}, 409)
        assert queue.list_jobs_from_queue() == []

    def test_post_with_a_malformed_encryption_key_refuses_with_the_gate_reason(
            self, queue, sidecar, monkeypatch):
        """The client cannot encrypt anything for a key that is not exactly 64
        hexadecimal characters, so the gate refuses before a job is queued."""
        monkeypatch.setattr(openrouter_translator.settings.translator,
                            'openrouter_encryption_key', 'zz')
        assert self._call('post', json={'lines': LINES, 'targetLanguage': 'hu'}) == (
            {'error': 'The AI Subtitle Translator encryption key must be exactly '
                      '64 hexadecimal characters'}, 409)
        assert queue.list_jobs_from_queue() == []

    def test_post_with_a_sidecar_key_and_no_api_key_still_queues(self, queue, sidecar,
                                                                 monkeypatch):
        """The sidecar can hold its own OpenRouter key, so the editor refuses a
        translation the service would happily run when the gate demands a
        Bazarr-side API key."""
        monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_api_key', '')
        monkeypatch.setattr(openrouter_translator.settings.translator,
                            'openrouter_encryption_key', '0123456789abcdef' * 4)
        body, status = self._call('post', json={'lines': LINES, 'targetLanguage': 'Hungarian'})
        assert status == 202
        assert body == {'jobId': _only_job(queue)['job_id']}

    def test_post_asks_about_the_service_not_the_configured_engine(self, queue, sidecar,
                                                                  monkeypatch):
        """The editor translates through the AI Subtitle Translator service
        whatever the settings page has selected, so a switched-off engine does
        not refuse the editor its own translation."""
        monkeypatch.setattr(openrouter_translator.settings.translator, 'translator_type', 'none')
        body, status = self._call('post', json={'lines': LINES, 'targetLanguage': 'Hungarian'})
        assert status == 202
        assert body == {'jobId': _only_job(queue)['job_id']}

    def test_delete_stops_the_job(self, queue, sidecar):
        job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
        assert self._call('delete', query_string={'jobId': job_id}) == ('', 204)
        assert queue.list_jobs_from_queue() == []
        assert self._call('delete', query_string={'jobId': job_id})[1] == 404

    def test_delete_of_a_shared_translation_that_already_finished_answers_404(self, queue, sidecar):
        job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
        editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
        sidecar.polls.append({'status': 'completed', 'result': {'lines': [{'position': 0, 'line': 'a'}]}})
        _run(queue, job_id)

        assert self._call('delete', query_string={'jobId': job_id})[1] == 404

    def test_get_reads_the_job_state(self, queue, sidecar):
        job_id = editor.enqueue_editor_translation(LINES, 'English', 'Hungarian')
        body, status = self._call('get', query_string={'jobId': job_id})
        assert status == 200
        assert body == {'jobId': job_id, 'status': 'pending'}
        assert self._call('get', query_string={'jobId': 12345})[1] == 404
        assert self._call('get', query_string={'jobId': 'x'})[1] == 400

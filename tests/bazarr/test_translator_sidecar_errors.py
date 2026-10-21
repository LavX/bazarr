# coding=utf-8
"""A sidecar refusal reaches the user as a reason they can act on.

The AI Subtitle Translator answers a refusal with FastAPI's
``{"detail": {"error": code, "message": text}}`` envelope. Reporting the status
alone told a user whose encryption key was missing or stale that the service
"refused the request (HTTP 401)", which reads like a wrong OpenRouter API key and
sends them to the wrong setting. A blank encryption key is still valid when the
translator runs its documented encryption-off mode, so the answer is the reason,
not a refusal to send the request. The sync endpoint is not a fallback: every
failure there has already been reported by the job endpoint.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from api.translator import translator as api_mod
from subtitles.tools.translate.services import openrouter_translator as module


@pytest.fixture
def service(tmp_path, monkeypatch):
    source = tmp_path / 'source.en.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:02,000\nOne\n\n'
                      '2\n00:00:03,000 --> 00:00:04,000\nTwo\n', encoding='utf-8')
    monkeypatch.setattr(module.settings.translator, 'translator_info', False)
    monkeypatch.setattr(module.settings.translator, 'openrouter_url', 'http://sidecar:8765')
    monkeypatch.setattr(module.settings.translator, 'openrouter_api_key', 'sk-or-v1-fixture-key')
    monkeypatch.setattr(module.settings.translator, 'openrouter_encryption_key', '')
    monkeypatch.setattr(module.settings.translator, 'openrouter_provider_routing', 'throughput')
    monkeypatch.setattr(module, 'get_title', lambda **kwargs: 'Example')
    monkeypatch.setattr(module, 'get_translator_auth_headers', lambda: {})
    monkeypatch.setattr(module, 'language_from_alpha2', lambda code: {'en': 'English', 'hu': 'Hungarian'}[code])
    monkeypatch.setattr(module, 'language_from_alpha3', lambda code: 'Hungarian')
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    translator = module.OpenRouterTranslatorService(
        source_srt_file=str(source), dest_srt_file=str(tmp_path / 'output.hu.srt'),
        lang_obj=None, to_lang='hun', from_lang='en', media_type='episode',
        video_path=str(tmp_path / 'video.mkv'), orig_to_lang='hu', forced=False, hi=False,
        sonarr_series_id=1, sonarr_episode_id=2, radarr_id=None,
    )
    return translator


def _refusal(status, detail=None, *, message=None):
    body = {'detail': detail} if detail is not None else {'detail': message}
    return SimpleNamespace(status_code=status, text='', json=lambda: body)


def _posts(monkeypatch, refusal):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return refusal

    monkeypatch.setattr(module.requests, 'post', post)
    return calls


def test_a_missing_encryption_key_names_the_key_and_regenerate_key(service, monkeypatch):
    calls = _posts(monkeypatch, _refusal(401, {
        'error': 'unauthorized', 'message': 'Invalid or missing X-Auth-Token header'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'encryption key' in reason.lower()
    assert 'regenerate-key' in reason
    # The request was sent, because a blank key is the sidecar's documented
    # encryption-off mode, and it was sent once: the sync endpoint is gone.
    assert [url for url, _ in calls] == ['http://sidecar:8765/api/v1/jobs/translate/content']
    assert calls[0][1]['json']['config']['apiKey'] == 'sk-or-v1-fixture-key'
    assert not Path(service.dest_srt_file).exists()


def test_a_stale_encryption_key_names_the_key_and_regenerate_key(service, monkeypatch):
    _posts(monkeypatch, _refusal(400, {
        'error': 'decryption_failed', 'message': 'Failed to decrypt API key'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'encryption key does not match' in reason.lower()
    assert 'regenerate-key' in reason


def test_a_missing_openrouter_key_names_the_key_setting(service, monkeypatch):
    calls = _posts(monkeypatch, _refusal(401, {
        'error': 'configuration_error',
        'message': 'OpenRouter API key is not configured.'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'OpenRouter API key' in reason
    assert len(calls) == 1


def test_a_full_job_queue_is_reported_as_busy(service, monkeypatch):
    _posts(monkeypatch, _refusal(429, {
        'error': 'queue_full', 'message': 'Translation queue is full'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    assert 'queue is full' in str(raised.value).lower()


def test_a_service_error_keeps_the_sidecars_own_reason(service, monkeypatch):
    _posts(monkeypatch, _refusal(500, {
        'error': 'translation_failed', 'message': 'Provider overloaded'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'Provider overloaded' in reason
    assert 'HTTP 500' in reason


def test_a_service_without_the_job_api_is_not_retried_on_the_sync_endpoint(service, monkeypatch):
    calls = _posts(monkeypatch, _refusal(404, message='Not Found'))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'translation API' in reason
    assert [url for url, _ in calls] == ['http://sidecar:8765/api/v1/jobs/translate/content']


@pytest.mark.parametrize('status, body, expected', [
    (401, {'detail': {'error': 'unauthorized', 'message': 'no token'}}, 'encryption key'),
    (401, {'detail': {'error': 'configuration_error', 'message': 'no key'}}, 'OpenRouter API key'),
    (400, {'detail': {'error': 'encryption_disabled', 'message': 'off'}}, 'encryption disabled'),
    (400, {'detail': {'error': 'encryption_required', 'message': 'strict'}}, 'requires encrypted'),
    (422, {'detail': [{'loc': ['body'], 'msg': 'field required'}]}, 'HTTP 422'),
    (502, {'error': 'bad_gateway'}, 'HTTP 502'),
    (503, '<html>unavailable</html>', 'HTTP 503'),
])
def test_every_refusal_shape_has_a_reason(status, body, expected):
    response = SimpleNamespace(status_code=status, json=lambda: body)

    assert expected.lower() in module.describe_sidecar_refusal(response).lower()


def _call_test_endpoint(monkeypatch, refusal, **settings_overrides):
    values = {
        'openrouter_url': 'http://sidecar:8765',
        'openrouter_api_key': 'sk-or-v1-fixture-key',
        'openrouter_encryption_key': '',
    }
    values.update(settings_overrides)
    for key, value in values.items():
        monkeypatch.setattr(module.settings.translator, key, value)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return refusal

    monkeypatch.setattr(api_mod.requests, 'post', post)
    app = Flask(__name__)
    with app.test_request_context('/api/translator/test', method='POST', json={}):
        body, status = api_mod.TranslatorTest.post.__wrapped__(api_mod.TranslatorTest())
    return body, status, calls


def test_the_settings_test_button_explains_a_rejected_token(monkeypatch):
    body, status, calls = _call_test_endpoint(monkeypatch, _refusal(401, {
        'error': 'unauthorized', 'message': 'Invalid or missing X-Auth-Token header'}))

    assert status == 502
    assert 'encryption key' in body['error'].lower()
    assert 'regenerate-key' in body['error']
    assert [url for url, _ in calls] == ['http://sidecar:8765/api/v1/test']


def test_the_settings_test_button_explains_a_missing_openrouter_key(monkeypatch):
    body, status, _ = _call_test_endpoint(monkeypatch, _refusal(401, {
        'error': 'configuration_error', 'message': 'OpenRouter API key is not configured.'}))

    assert status == 502
    assert 'OpenRouter API key' in body['error']


def test_the_settings_test_button_keeps_a_working_answer(monkeypatch):
    body, status, _ = _call_test_endpoint(monkeypatch, SimpleNamespace(
        status_code=200, json=lambda: {'encryption': None, 'apiKey': {'status': 'ok'}}))

    assert status == 200
    assert body == {'encryption': None, 'apiKey': {'status': 'ok'}}

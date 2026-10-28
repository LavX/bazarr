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
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from api.translator import translator as api_mod
from app.logger import FileHandlerFormatter
from secret_store.scrub import clear_secret_values, register_secret_values, registered_secret_values
from subtitles.tools.translate import editor
from subtitles.tools.translate.services import openrouter_translator as module

# What an unhelpful 500 can echo back. The key has the real shape, the first
# ciphertext is the ``enc:`` envelope Bazarr submits, and the second is the
# ``enc:v1:`` form the settings hold at rest.
ECHOED_API_KEY = 'sk-or-v1-' + '0123456789abcdef' * 4  # pragma: allowlist secret
SUBMITTED_CIPHERTEXT = 'enc:' + 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo='  # pragma: allowlist secret
STORED_CIPHERTEXT = 'enc:v1:' + 'gAAAAAB' + 'A' * 64  # pragma: allowlist secret
ECHOED_SECRETS = (ECHOED_API_KEY, SUBMITTED_CIPHERTEXT, STORED_CIPHERTEXT)


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


def _leaking_refusal():
    message = (f'upstream 500 echoed apiKey={ECHOED_API_KEY}, request={SUBMITTED_CIPHERTEXT}, '
               f'stored={STORED_CIPHERTEXT}')
    return _refusal(500, {'error': 'translation_failed', 'message': message})


def _leaking_poll_failure():
    return SimpleNamespace(
        status_code=200,
        json=lambda: {
            'status': 'failed',
            'error': f'upstream rejected request={SUBMITTED_CIPHERTEXT} stored={STORED_CIPHERTEXT}',
        },
    )


def _assert_no_echoed_secret(text):
    for secret in ECHOED_SECRETS:
        assert secret not in text
    assert 'sk-or-v1-' not in text
    assert 'enc:' not in text


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


def test_a_refusal_that_echoes_a_key_or_ciphertext_does_not_raise_it(service, monkeypatch):
    _posts(monkeypatch, _leaking_refusal())

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'upstream 500 echoed' in reason
    assert 'HTTP 500' in reason
    _assert_no_echoed_secret(reason)


def test_a_registered_plaintext_credential_in_a_refusal_is_masked(service, monkeypatch):
    custom_key = 'custom-provider-credential-0123456789'  # pragma: allowlist secret
    previous = registered_secret_values()
    clear_secret_values()
    try:
        register_secret_values([custom_key])
        _posts(monkeypatch, _refusal(500, {
            'error': 'translation_failed', 'message': f'upstream rejected {custom_key}'}))

        with pytest.raises(module.TranslationServiceError) as raised:
            service.translate()
    finally:
        register_secret_values(previous)

    assert custom_key not in str(raised.value)
    assert 'upstream rejected' in str(raised.value)


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


def _call_test_endpoint(monkeypatch, refusal, request_json=None, **settings_overrides):
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
    with app.test_request_context('/api/translator/test', method='POST', json=request_json or {}):
        body, status = api_mod.TranslatorTest.post.__wrapped__(api_mod.TranslatorTest())
    return body, status, calls


def test_the_settings_test_button_explains_a_rejected_token(monkeypatch):
    body, status, calls = _call_test_endpoint(monkeypatch, _refusal(401, {
        'error': 'unauthorized', 'message': 'Invalid or missing X-Auth-Token header'}))

    assert status == 502
    assert 'encryption key' in body['error'].lower()
    assert 'regenerate-key' in body['error']
    assert [url for url, _ in calls] == ['http://sidecar:8765/api/v1/test']


def test_an_emptied_encryption_key_field_sends_the_test_without_the_saved_key(monkeypatch):
    # The form says encryption is off. The key in the payload and the key behind the
    # auth header must agree: a plaintext key with a token derived from the saved key
    # would authenticate as an installation the user just told us not to use.
    saved_key = 'ab' * 32
    body, status, calls = _call_test_endpoint(
        monkeypatch, SimpleNamespace(status_code=200, json=lambda: {'encryption': None}),
        request_json={'encryptionKey': ''}, openrouter_encryption_key=saved_key)

    assert status == 200
    (_, kwargs), = calls
    assert kwargs['json'] == {'apiKey': 'sk-or-v1-fixture-key'}
    assert 'X-Auth-Token' not in kwargs['headers']


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


def test_the_settings_test_button_does_not_return_an_echoed_secret(monkeypatch):
    body, status, _ = _call_test_endpoint(monkeypatch, _leaking_refusal())

    assert status == 502
    assert 'HTTP 500' in body['error']
    _assert_no_echoed_secret(body['error'])


def test_the_editor_facing_error_does_not_carry_an_echoed_secret(service, monkeypatch):
    monkeypatch.setattr(editor, '_results', type(editor._results)())
    _posts(monkeypatch, _leaking_refusal())

    with pytest.raises(module.TranslationServiceError):
        editor.translate_editor_lines(['One'], [7], 'English', 'Hungarian', 'Example',
                                      'episode', job_id=984001)

    state = editor.editor_translation_state(984001)
    assert state['status'] == 'failed'
    assert 'HTTP 500' in state['error']
    _assert_no_echoed_secret(state['error'])


def test_the_file_log_formatter_masks_echoed_ciphertext_on_its_own():
    formatter = FileHandlerFormatter('%(message)s|')
    record = logging.LogRecord(
        'bazarr', logging.ERROR, '', 0,
        f'service echoed request={SUBMITTED_CIPHERTEXT} stored={STORED_CIPHERTEXT}', (), None)

    assert formatter.format(record) == 'service echoed request=*** stored=***|'


def test_a_refusal_is_scrubbed_before_the_file_log_formatter_sees_it(service, monkeypatch):
    _posts(monkeypatch, _leaking_refusal())

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    record = logging.LogRecord(
        'bazarr', logging.ERROR, '', 0,
        'AI Subtitle Translator refused the job: %s', (raised.value,), None)

    _assert_no_echoed_secret(FileHandlerFormatter('%(message)s|').format(record))


def test_an_editor_poll_failure_does_not_carry_an_echoed_secret(service, monkeypatch):
    monkeypatch.setattr(editor, '_results', type(editor._results)())
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    _posts(monkeypatch, SimpleNamespace(
        status_code=200, json=lambda: {'jobId': 'remote-1'}))

    def get(url, **kwargs):
        if url.endswith('/health'):
            return SimpleNamespace(
                status_code=200, json=lambda: {'status': 'healthy', 'version': '2.0.0'})
        return _leaking_poll_failure()

    monkeypatch.setattr(module.requests, 'get', get)

    with pytest.raises(module.TranslationServiceError):
        editor.translate_editor_lines(['One'], [7], 'English', 'Hungarian', 'Example',
                                      'episode', job_id=984002)

    state = editor.editor_translation_state(984002)
    assert state['status'] == 'failed'
    _assert_no_echoed_secret(state['error'])


def test_a_refusal_echoing_a_short_openrouter_token_is_masked(service, monkeypatch):
    short_token = 'sk-or-v1-short-token-not-64-hex'  # pragma: allowlist secret
    _posts(monkeypatch, _refusal(500, {
        'error': 'translation_failed', 'message': f'upstream rejected token {short_token}'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert short_token not in reason
    assert 'upstream rejected token' in reason
    assert '***' in reason


def test_a_refusal_with_a_non_string_error_code_falls_through(service, monkeypatch):
    _posts(monkeypatch, _refusal(500, {
        'error': ['not', 'hashable'], 'message': 'Provider overloaded'}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'Provider overloaded' in reason
    assert 'HTTP 500' in reason


def test_a_very_long_service_message_is_truncated(service, monkeypatch):
    long_message = 'x' * 2000
    _posts(monkeypatch, _refusal(500, {
        'error': 'translation_failed', 'message': long_message}))

    with pytest.raises(module.TranslationServiceError) as raised:
        service.translate()

    reason = str(raised.value)
    assert 'HTTP 500' in reason
    assert reason.count('x') == 500

# coding=utf-8
"""OpenRouter provider routing reaches the AI Subtitle Translator as ``config.provider``.

The sidecar sorts OpenRouter providers by throughput unless it is told otherwise,
so a user who wants the cheapest provider (OpenRouter's ``:floor``) or the
priority tier (``:nitro``) needs a setting that Bazarr forwards on every request:
the async translate job and the manual content endpoint alike.
"""
from types import SimpleNamespace

import pytest
from flask import Flask

from app import config
from subtitles.tools.translate.services import openrouter_translator

ROUTING_VALUES = ['throughput', 'nitro', 'price', 'floor', 'latency', 'default']


def _routing_validator():
    return next(
        v for v in config.validators
        if v.names == ('translator.openrouter_provider_routing',)
    )


def test_setting_defaults_to_smartfast_and_pins_the_allowed_values():
    # A fresh install lets the translator weigh speed against price rather than
    # taking the fastest endpoint at whatever it costs.
    validator = _routing_validator()
    assert validator.default == 'smartfast'
    assert list(validator.operations.get('is_in')) == ROUTING_VALUES + ['smartfast', 'custom']


def test_an_unreadable_stored_routing_falls_back_to_a_sort_any_sidecar_serves():
    # Not the shipped default: smartfast refuses outright below 2.0.0, and a value we
    # cannot read is a config we do not understand, so it must not also refuse.
    assert openrouter_translator.UNKNOWN_ROUTING_FALLBACK == 'throughput'
    assert openrouter_translator.UNKNOWN_ROUTING_FALLBACK in ROUTING_VALUES


def _sidecar_health(monkeypatch, version):
    """Answer the sidecar's /health probe with the given version (None = unreachable)."""
    openrouter_translator.reset_sidecar_version_cache()
    calls = []

    def _get(url, *args, **kwargs):
        calls.append(url)
        if version is None:
            raise openrouter_translator.requests.exceptions.ConnectionError('down')
        return SimpleNamespace(status_code=200, json=lambda: {'status': 'healthy', 'version': version})

    monkeypatch.setattr(openrouter_translator.requests, 'get', _get)
    return calls


@pytest.mark.parametrize('value', ROUTING_VALUES)
def test_build_provider_config_forwards_the_setting(monkeypatch, value):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', value)
    assert openrouter_translator.build_provider_config() == {'sort': value}


def test_build_provider_config_falls_back_to_throughput_for_unknown_values(monkeypatch):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'cheapest')
    assert openrouter_translator.build_provider_config() == {'sort': 'throughput'}


@pytest.mark.parametrize('value, expected', [
    ('floor', 'price'),
    ('nitro', 'throughput'),
    ('default', 'throughput'),
    ('price', 'price'),
    ('latency', 'latency'),
])
def test_older_sidecars_get_the_plain_sort(monkeypatch, value, expected):
    # Before 1.3.4 the sidecar forwards whatever it gets as provider.sort, and
    # OpenRouter rejects nitro/floor/default there.
    _sidecar_health(monkeypatch, '1.3.3')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', value)
    assert openrouter_translator.build_provider_config() == {'sort': expected}


def test_unreachable_sidecar_gets_the_plain_sort(monkeypatch):
    _sidecar_health(monkeypatch, None)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'floor')
    assert openrouter_translator.build_provider_config() == {'sort': 'price'}


def test_sidecar_version_is_probed_once_per_url(monkeypatch):
    calls = _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_url', 'http://sidecar:8765/')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'floor')
    assert openrouter_translator.build_provider_config() == {'sort': 'floor'}
    assert openrouter_translator.build_provider_config() == {'sort': 'floor'}
    assert calls == ['http://sidecar:8765/health']


@pytest.mark.parametrize('version', ['1.3.3', '1.3.4', None])
@pytest.mark.parametrize('suffix, setting, expected', [
    ('floor', 'throughput', 'price'),
    ('nitro', 'floor', 'throughput'),
    ('floor', 'floor', 'price'),
])
def test_typed_routing_suffix_aligns_the_sort(monkeypatch, version, suffix, setting, expected):
    # The slug says how to route; the sort Bazarr sends must never contradict it,
    # whatever the setting says and whatever the sidecar version is.
    _sidecar_health(monkeypatch, version)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', f'deepseek/deepseek-v4-flash:{suffix}')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', setting)
    assert openrouter_translator.build_provider_config() == {'sort': expected}


def test_other_model_variants_do_not_touch_the_sort(monkeypatch):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', 'deepseek/deepseek-chat:thinking')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'floor')
    assert openrouter_translator.build_provider_config() == {'sort': 'floor'}


@pytest.mark.parametrize('version', ['1.3.4', '1.4.0', '2.0.0', '1.3.10', '1.3.4-rc1'])
def test_versions_from_1_3_4_support_the_shortcuts(monkeypatch, version):
    _sidecar_health(monkeypatch, version)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'nitro')
    assert openrouter_translator.build_provider_config() == {'sort': 'nitro'}


def _build_service():
    return openrouter_translator.OpenRouterTranslatorService(
        source_srt_file='input.srt',
        dest_srt_file='output.srt',
        lang_obj=None,
        to_lang='hun',
        from_lang='en',
        media_type='movie',
        video_path='/tmp/video.mkv',
        orig_to_lang='hu',
        forced=False,
        hi=False,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=7,
    )


def _translator_settings(monkeypatch, routing):
    # Every key below exists in the settings box already (the validators give them
    # defaults), so a plain setattr restores cleanly. Never patch a settings box with
    # create=True: the restore deletes the key and later tests lose it.
    for key, value in {
        'openrouter_url': 'http://sidecar:8765',
        'openrouter_api_key': 'sk-or-key',
        'openrouter_encryption_key': '',
        'openrouter_model': 'deepseek/deepseek-v4-flash',
        'openrouter_temperature': 0.3,
        'openrouter_max_concurrent': 2,
        'openrouter_parallel_batches': 4,
        'openrouter_reasoning': 'disabled',
        'openrouter_provider_routing': routing,
    }.items():
        monkeypatch.setattr(openrouter_translator.settings.translator, key, value)


def test_translate_job_sends_the_routing_in_the_config(mocker, monkeypatch):
    _translator_settings(monkeypatch, 'floor')
    _sidecar_health(monkeypatch, '1.3.4')
    mocker.patch.object(openrouter_translator, 'get_title', return_value='Some Movie')
    mocker.patch.object(openrouter_translator, 'language_from_alpha2', lambda code: code)
    mocker.patch.object(openrouter_translator, 'language_from_alpha3', lambda code: code)
    mocker.patch.object(openrouter_translator, 'get_translator_auth_headers', return_value={})
    post = mocker.patch.object(
        openrouter_translator.requests,
        'post',
        return_value=SimpleNamespace(status_code=200, json=lambda: {'jobId': 'job-1'}),
    )
    service = _build_service()
    mocker.patch.object(service, '_poll_job', return_value=[{'index': 0, 'content': 'Szia'}])

    result = service._submit_and_poll(['Hi'])

    assert result == [{'index': 0, 'content': 'Szia'}]
    payload = post.call_args.kwargs['json']
    assert payload['config']['provider'] == {'sort': 'floor'}
    assert payload['config']['model'] == 'deepseek/deepseek-v4-flash'


def test_content_endpoint_sends_the_routing_in_the_config(mocker, monkeypatch):
    from api.translator import translator as api_mod

    _translator_settings(monkeypatch, 'nitro')
    _sidecar_health(monkeypatch, '1.3.4')
    mocker.patch.object(api_mod, 'get_translator_auth_headers', return_value={})
    post = mocker.patch.object(
        api_mod.requests,
        'post',
        return_value=SimpleNamespace(status_code=200, json=lambda: {'jobId': 'job-2'}),
    )

    app = Flask(__name__)
    with app.test_request_context(
        '/api/translator/jobs', method='POST', json={'lines': ['Hi'], 'targetLanguage': 'hu'}
    ):
        body, status = api_mod.TranslatorJobs.post.__wrapped__(api_mod.TranslatorJobs())

    assert status == 200
    assert body == {'jobId': 'job-2'}
    payload = post.call_args.kwargs['json']
    assert payload['config']['provider'] == {'sort': 'nitro'}


@pytest.fixture
def submit_request(mocker, monkeypatch):
    from api.translator import translator as api_mod

    _translator_settings(monkeypatch, 'throughput')
    mocker.patch.object(openrouter_translator, 'get_title', return_value='Some Movie')
    mocker.patch.object(openrouter_translator, 'language_from_alpha2', lambda code: code)
    mocker.patch.object(openrouter_translator, 'get_translator_auth_headers', return_value={})
    mocker.patch.object(api_mod, 'get_translator_auth_headers', return_value={})
    post = mocker.patch.object(
        openrouter_translator.requests, 'post',
        return_value=SimpleNamespace(status_code=200, json=lambda: {'jobId': 'selection-job'}),
    )
    service = _build_service()
    mocker.patch.object(service, '_poll_job', return_value=[{'position': 0, 'line': 'Szia'}])
    messages = []
    monkeypatch.setattr(openrouter_translator, 'show_message', messages.append)

    def send(boundary):
        if boundary == 'queued':
            return service._submit_and_poll(['Hi'])
        app = Flask(__name__)
        with app.test_request_context(
            '/api/translator/jobs', method='POST', json={'lines': ['Hi'], 'targetLanguage': 'hu'}
        ):
            return api_mod.TranslatorJobs.post.__wrapped__(api_mod.TranslatorJobs())

    return SimpleNamespace(send=send, post=post, messages=messages, service=service)


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('model', [
    'deepseek/example', 'deepseek/example:free', 'deepseek/example:thinking',
    'deepseek/example:nitro', 'deepseek/example:free:floor',
])
def test_smartfast_request_preserves_variants_and_clears_custom_providers(
        monkeypatch, submit_request, boundary, model):
    _sidecar_health(monkeypatch, '2.0.0-rc1')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'smartfast')
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', model)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['stale-provider'])

    submit_request.send(boundary)

    payload = submit_request.post.call_args.kwargs['json']['config']
    expected_model = {
        'deepseek/example:nitro': 'deepseek/example',
        'deepseek/example:free:floor': 'deepseek/example:free',
    }.get(model, model)
    assert payload['model'] == expected_model
    assert payload['provider'] == {'sort': 'smartfast'}


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('order', [['deepinfra'], [' DeepInfra ', 'deepinfra', 'PARASAIL/fp8']])
@pytest.mark.parametrize('suffix', ['nitro', 'floor', 'smartfast'])
def test_custom_request_keeps_only_selected_providers_in_order(
        monkeypatch, submit_request, boundary, order, suffix):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', order)
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', f'deepseek/example:free:{suffix}')

    submit_request.send(boundary)

    payload = submit_request.post.call_args.kwargs['json']['config']
    expected_order = ['deepinfra'] if len(order) == 1 else ['deepinfra', 'parasail/fp8']
    assert payload['model'] == 'deepseek/example:free'
    assert payload['provider'] == {
        'sort': 'default', 'order': expected_order, 'only': expected_order, 'allowFallbacks': False,
    }
    assert config.settings.translator.openrouter_model == f'deepseek/example:free:{suffix}'


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('suffix', ['smartfast', 'SMARTFAST'])
def test_typed_smartfast_selects_smartfast_with_legacy_routing(monkeypatch, submit_request, boundary, suffix):
    _sidecar_health(monkeypatch, '2.0.0')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'floor')
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', f'deepseek/example:{suffix}')

    submit_request.send(boundary)

    payload = submit_request.post.call_args.kwargs['json']['config']
    assert payload['model'] == 'deepseek/example'
    assert payload['provider'] == {'sort': 'smartfast'}


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('routing', ['smartfast', 'custom'])
@pytest.mark.parametrize('suffix', ['NITRO', 'FLOOR', 'SMARTFAST'])
def test_selection_strips_routing_tokens_case_insensitively_without_changing_variants(
        monkeypatch, submit_request, boundary, routing, suffix):
    _sidecar_health(monkeypatch, '2.0.0')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', routing)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', f'DeepSeek/Example:Free:{suffix}:Thinking')

    submit_request.send(boundary)

    payload = submit_request.post.call_args.kwargs['json']['config']
    assert payload['model'] == 'DeepSeek/Example:Free:Thinking'
    assert payload['provider']['sort'] == ('default' if routing == 'custom' else 'smartfast')
    assert config.settings.translator.openrouter_model == f'DeepSeek/Example:Free:{suffix}:Thinking'


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('suffix, expected_model, expected_sort', [
    ('NITRO', 'DeepSeek/Example:Free:nitro', 'throughput'),
    ('FLOOR', 'DeepSeek/Example:Free:floor', 'price'),
])
def test_typed_legacy_routing_canonicalizes_only_the_routing_token(
        monkeypatch, submit_request, boundary, suffix, expected_model, expected_sort):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', f'DeepSeek/Example:Free:{suffix}')

    submit_request.send(boundary)

    payload = submit_request.post.call_args.kwargs['json']['config']
    assert payload['model'] == expected_model
    assert payload['provider'] == {'sort': expected_sort}


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('routing, version, minimum', [
    # A version we READ and that is too old is actionable: name the version to install.
    # An unreadable version only refuses for custom, which has no safe fallback.
    ('smartfast', '1.3.4', '2.0.0'), ('custom', '1.3.3', '1.3.4'),
    ('custom', None, '1.3.4'), ('custom', 'unknown', '1.3.4'),
])
def test_unsupported_selection_fails_clearly_without_submission(
        monkeypatch, submit_request, boundary, routing, version, minimum):
    _sidecar_health(monkeypatch, version)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', routing)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    result = submit_request.send(boundary)

    submit_request.post.assert_not_called()
    if boundary == 'manual':
        body, status = result
        assert status == 400
        message = body['error']
    else:
        assert result is None
        assert submit_request.messages == []
        message = submit_request.service.routing_error
    assert minimum in message
    assert routing in message.lower()


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
@pytest.mark.parametrize('order', [[], [' '], ['invalid provider'], ['https://provider.example'], ['../provider'],
                                  ['deepinfra', 1], 'deepinfra', ['provider'] * 21, ['p' * 161]])
def test_invalid_custom_order_fails_before_any_network(
        monkeypatch, submit_request, boundary, order):
    health_calls = _sidecar_health(monkeypatch, '2.0.0')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', order)

    result = submit_request.send(boundary)

    assert health_calls == []
    submit_request.post.assert_not_called()
    if boundary == 'manual':
        assert result[1] == 400
        assert 'provider' in result[0]['error'].lower()
    else:
        assert result is None
        assert submit_request.messages == []
        assert 'provider' in (submit_request.service.routing_error or '').lower()


def test_encrypted_queued_custom_request_preserves_routing(monkeypatch, submit_request):
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _sidecar_health(monkeypatch, '2.0.0')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['parasail/fp8', 'deepinfra'])
    monkeypatch.setattr(config.settings.translator, 'openrouter_encryption_key', 'ab' * 32)

    submit_request.send('queued')

    payload = submit_request.post.call_args.kwargs['json']['config']
    encrypted = payload['apiKey']
    assert encrypted.startswith('enc:')
    raw = base64.b64decode(encrypted[4:])
    assert AESGCM(bytes.fromhex('ab' * 32)).decrypt(raw[:12], raw[12:], None) == b'sk-or-key'
    assert payload['provider'] == {
        'sort': 'default', 'order': ['parasail/fp8', 'deepinfra'],
        'only': ['parasail/fp8', 'deepinfra'], 'allowFallbacks': False,
    }


@pytest.mark.parametrize('routing, expected', [
    ('smartfast', {'sort': 'smartfast'}),
    ('custom', {'sort': 'default', 'order': ['deepinfra'], 'only': ['deepinfra'], 'allowFallbacks': False}),
])
def test_sync_endpoint_fallback_preserves_selected_routing(monkeypatch, submit_request, routing, expected):
    _sidecar_health(monkeypatch, '2.0.0')
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', routing)
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_order', ['deepinfra'])
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', 'deepseek/example:free:nitro')
    submit_request.post.side_effect = [
        SimpleNamespace(status_code=404),
        SimpleNamespace(status_code=200, json=lambda: [{'position': 0, 'line': 'Szia'}]),
    ]

    assert submit_request.send('queued') == [{'position': 0, 'line': 'Szia'}]

    assert [call.args[0] for call in submit_request.post.call_args_list] == [
        'http://sidecar:8765/api/v1/jobs/translate/content',
        'http://sidecar:8765/api/v1/translate/content',
    ]
    for call in submit_request.post.call_args_list:
        assert call.kwargs['json']['config']['provider'] == expected
        assert call.kwargs['json']['config']['model'] == 'deepseek/example:free'


def test_smartfast_support_uses_version_even_when_upstream_is_unhealthy(monkeypatch):
    openrouter_translator.reset_sidecar_version_cache()
    monkeypatch.setattr(config.settings.translator, 'openrouter_provider_routing', 'smartfast')
    monkeypatch.setattr(openrouter_translator.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        status_code=200,
        json=lambda: {'status': 'unhealthy', 'version': '2.0.0', 'openrouterConfigured': True},
    ))

    assert openrouter_translator.build_provider_config() == {'sort': 'smartfast'}


@pytest.mark.parametrize('boundary', ['queued', 'manual'])
def test_old_service_cannot_receive_a_typed_smartfast_model(monkeypatch, submit_request, boundary):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(config.settings.translator, 'openrouter_model', 'deepseek/example:smartfast')

    result = submit_request.send(boundary)

    submit_request.post.assert_not_called()
    if boundary == 'manual':
        assert result[1] == 400
        assert '2.0.0' in result[0]['error']
    else:
        assert result is None
        assert submit_request.messages == []
        assert '2.0.0' in (submit_request.service.routing_error or '')


@pytest.mark.parametrize('model, expected_model, expected_sort', [
    # The last shortcut is the one the settings page adopted, and it is the only one that
    # may survive: the sidecar reads a single trailing shortcut, so an earlier one left in
    # place goes to OpenRouter as part of the model id and is rejected there.
    ('deepseek/example:smartfast:nitro', 'deepseek/example:nitro', 'throughput'),
    ('deepseek/example:nitro:floor', 'deepseek/example:floor', 'price'),
    ('deepseek/example:nitro:free', 'deepseek/example:free:nitro', 'throughput'),
    ('deepseek/example:free:floor', 'deepseek/example:free:floor', 'price'),
])
def test_a_stacked_model_id_keeps_only_the_adopted_shortcut(monkeypatch, model, expected_model, expected_sort):
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', model)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'throughput')

    assert openrouter_translator.build_routing_config() == (expected_model, {'sort': expected_sort})


def test_a_smartfast_shortcut_in_front_of_a_variant_still_selects_smartfast(monkeypatch):
    # Reading only the final colon segment missed this and forwarded :smartfast to
    # OpenRouter, which has never heard of it.
    _sidecar_health(monkeypatch, '2.0.0')
    monkeypatch.setattr(openrouter_translator.settings.translator,
                        'openrouter_model', 'deepseek/example:smartfast:thinking')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'throughput')

    assert openrouter_translator.build_routing_config() == (
        'deepseek/example:thinking', {'sort': 'smartfast'})


@pytest.mark.parametrize('routing, model, expected', [
    ('throughput', '  deepseek/example:nitro  ', 'deepseek/example:nitro'),
    ('throughput', '  deepseek/example  ', 'deepseek/example'),
    ('price', ' deepseek/example:free ', 'deepseek/example:free'),
])
def test_a_padded_model_id_is_trimmed_before_it_is_sent(monkeypatch, routing, model, expected):
    # A leading space still matched the old trailing-suffix test, so a padded id was
    # detected, rewritten and sent with the padding intact.
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', model)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', routing)

    assert openrouter_translator.build_routing_config()[0] == expected


def test_an_unknown_version_is_probed_again_soon(monkeypatch):
    # Both new routings refuse outright on an unknown version, so a restarting sidecar's
    # silence must not keep failing translations for the full five-minute interval.
    calls = _sidecar_health(monkeypatch, None)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_url', 'http://sidecar:8765')
    now = [0]
    monkeypatch.setattr(openrouter_translator.time, 'monotonic', lambda: now[0])

    assert openrouter_translator.sidecar_version('http://sidecar:8765') is None
    now[0] = openrouter_translator.SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS + 1
    assert openrouter_translator.sidecar_version(
        'http://sidecar:8765',
        max_age=openrouter_translator.SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS) is None

    assert calls == ['http://sidecar:8765/health', 'http://sidecar:8765/health']
    assert (openrouter_translator.SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS
            < openrouter_translator.SIDECAR_VERSION_CACHE_SECONDS)


def test_a_known_version_is_still_cached_for_the_full_interval(monkeypatch):
    calls = _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_url', 'http://sidecar:8765')
    now = [0]
    monkeypatch.setattr(openrouter_translator.time, 'monotonic', lambda: now[0])

    assert openrouter_translator.sidecar_version('http://sidecar:8765') == (1, 3, 4)
    now[0] = openrouter_translator.SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS + 1
    assert openrouter_translator.sidecar_version('http://sidecar:8765') == (1, 3, 4)

    assert calls == ['http://sidecar:8765/health']


@pytest.mark.parametrize('version', [None, 'unknown'])
def test_smartfast_degrades_rather_than_failing_when_the_version_cannot_be_read(monkeypatch, version):
    # /health may be hidden behind a reverse proxy, or answer without a version, while the
    # job API works. Refusing there failed every translation on a service that supports
    # smartfast, and throughput is the default for installs that never chose anything.
    _sidecar_health(monkeypatch, version)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'smartfast')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', 'deepseek/example:free')

    assert openrouter_translator.build_routing_config() == (
        'deepseek/example:free', {'sort': openrouter_translator.UNKNOWN_ROUTING_FALLBACK})


def test_custom_still_refuses_when_the_version_cannot_be_read(monkeypatch):
    # Custom has no safe fallback: degrading would send the job to a provider the user
    # excluded, which is the one outcome the mode exists to prevent.
    _sidecar_health(monkeypatch, None)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'custom')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_order', ['deepinfra'])

    with pytest.raises(openrouter_translator.ProviderRoutingError):
        openrouter_translator.build_routing_config()


def test_a_refusal_reads_a_fresh_version_so_the_user_s_fix_is_seen(monkeypatch):
    # The user is told to update the translator. Answering their retry from a five-minute
    # old reading of the version they just replaced says their fix did not work.
    openrouter_translator.reset_sidecar_version_cache()
    reported = ['1.3.4']
    calls = []

    def _get(url, *args, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {'version': reported[0]})

    monkeypatch.setattr(openrouter_translator.requests, 'get', _get)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'smartfast')
    now = [0]
    monkeypatch.setattr(openrouter_translator.time, 'monotonic', lambda: now[0])

    with pytest.raises(openrouter_translator.ProviderRoutingError):
        openrouter_translator.build_routing_config()

    reported[0] = '2.0.0'
    now[0] = openrouter_translator.SIDECAR_VERSION_ACTIONABLE_CACHE_SECONDS + 1
    assert openrouter_translator.build_routing_config()[1] == {'sort': 'smartfast'}
    assert len(calls) == 2


@pytest.mark.parametrize('stored', [None, '', 'a-routing-we-removed'])
def test_a_routing_we_cannot_read_never_lands_on_one_that_refuses(monkeypatch, stored):
    # Both an absent and an unusable value are configs we do not understand, and a
    # :smartfast suffix must not promote either back out of the safe fallback.
    _sidecar_health(monkeypatch, '1.3.4')
    if stored is None:
        monkeypatch.delattr(openrouter_translator.settings.translator, 'openrouter_provider_routing',
                            raising=False)
    else:
        monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', stored)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', 'deepseek/example:smartfast')

    model, provider = openrouter_translator.build_routing_config()

    assert provider == {'sort': openrouter_translator.UNKNOWN_ROUTING_FALLBACK}
    assert model == 'deepseek/example'


@pytest.mark.parametrize('model, expected', [
    ('deepseek/example:nitro:', 'deepseek/example:nitro'),
    ('deepseek/example::nitro', 'deepseek/example:nitro'),
    ('deepseek/example:', 'deepseek/example'),
])
def test_an_empty_colon_segment_never_reaches_the_wire(monkeypatch, model, expected):
    # A trailing-colon typo used to survive stripping as "author/model:", which the
    # legacy branch then rebuilt into "author/model::nitro".
    _sidecar_health(monkeypatch, '1.3.4')
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_model', model)
    monkeypatch.setattr(openrouter_translator.settings.translator, 'openrouter_provider_routing', 'throughput')

    assert openrouter_translator.build_routing_config()[0] == expected

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


def test_setting_defaults_to_throughput_and_pins_the_allowed_values():
    validator = _routing_validator()
    assert validator.default == 'throughput'
    assert list(validator.operations.get('is_in')) == ROUTING_VALUES
    assert config.settings.translator.openrouter_provider_routing == 'throughput'


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
    mocker.patch.object(openrouter_translator, 'show_progress')
    mocker.patch.object(openrouter_translator, 'hide_progress')
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

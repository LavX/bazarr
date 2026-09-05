from pathlib import Path
from types import SimpleNamespace

import pysubs2
import pytest

from subtitles.tools.translate.services import openrouter_translator as module


@pytest.fixture
def service(tmp_path, monkeypatch):
    source = tmp_path / 'source.en.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:02,000\nOne\n\n'
                      '2\n00:00:03,000 --> 00:00:04,000\nTwo\n\n'
                      '3\n00:00:05,000 --> 00:00:06,000\nThree\n', encoding='utf-8')
    monkeypatch.setattr(module.settings.translator, 'translator_info', False)
    monkeypatch.setattr(module, 'get_title', lambda **kwargs: 'Example')
    monkeypatch.setattr(module, 'get_translator_auth_headers', lambda: {})
    monkeypatch.setattr(module, 'language_from_alpha2', lambda code: {'en': 'English', 'hu': 'Hungarian'}[code])
    monkeypatch.setattr(module, 'language_from_alpha3', lambda code: 'Hungarian')
    monkeypatch.setattr(module.requests, 'post', lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {'jobId': 'fixture-job'}))
    monkeypatch.setattr(module, 'show_progress', lambda **kwargs: None)
    monkeypatch.setattr(module, 'hide_progress', lambda **kwargs: None)
    monkeypatch.setattr(module.jobs_queue, 'update_job_progress', lambda **kwargs: None)
    translator = module.OpenRouterTranslatorService(
        source_srt_file=str(source), dest_srt_file=str(tmp_path / 'output.hu.srt'),
        lang_obj=None, to_lang='hun', from_lang='en', media_type='episode',
        video_path=str(tmp_path / 'video.mkv'), orig_to_lang='hu', forced=False, hi=False,
        sonarr_series_id=1, sonarr_episode_id=2, radarr_id=None,
    )
    monkeypatch.setattr(translator, '_get_api_key_value', lambda: '')
    return translator


@pytest.fixture
def events(monkeypatch):
    captured = SimpleNamespace(messages=[], history=[])
    monkeypatch.setattr(module, 'show_message', captured.messages.append)
    monkeypatch.setattr(module, 'history_log', lambda **kwargs: captured.history.append(kwargs))
    monkeypatch.setattr(module, 'history_log_movie', lambda **kwargs: captured.history.append(kwargs))
    return captured


def _response(monkeypatch, status, result=None, **fields):
    payload = {'status': status, 'result': result, **fields}
    monkeypatch.setattr(module.requests, 'get', lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: payload))


@pytest.mark.parametrize('structured', [True, False])
@pytest.mark.parametrize('status', ['partial', 'completed'])
def test_poll_preserves_usable_lines(service, events, monkeypatch, status, structured):
    lines = [{'position': 2, 'line': 'Három'}, {'position': 0, 'line': 'Egy'}]
    _response(monkeypatch, status, {'lines': lines} if structured else lines,
              error='One batch failed')

    assert service._poll_job('http://fixture', 'job', 3) == lines
    if status == 'partial':
        assert any('partial' in message.lower() for message in events.messages)


@pytest.mark.parametrize('status', ['partial', 'completed'])
@pytest.mark.parametrize('result', [None, [], {}, {'lines': []}, {'lines': None},
                                    {'lines': 'invalid'}, 'invalid'])
def test_poll_rejects_missing_or_empty_lines(service, events, monkeypatch, status, result):
    _response(monkeypatch, status, result)
    assert service._poll_job('http://fixture', 'job', 3) is None


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_poll_does_not_return_failed_output(service, events, monkeypatch, status):
    _response(monkeypatch, status, [{'position': 0, 'line': 'Egy'}], error='Unavailable')
    assert service._poll_job('http://fixture', 'job', 3) is None


@pytest.mark.parametrize('media_type', ['episode', 'movie'])
def test_partial_translation_writes_ordered_cues_and_records_warning(service, events, monkeypatch, media_type):
    service.media_type = media_type
    service.radarr_id = 3 if media_type == 'movie' else None
    source = Path(service.source_srt_file).read_bytes()
    _response(monkeypatch, 'partial', {'lines': [
        {'position': 2, 'line': 'Három'}, {'position': 0, 'line': 'Egy\nMásik'},
    ]}, error='One batch failed: malformed response')

    assert service.translate(job_id='bazarr-job') == service.dest_srt_file

    output = pysubs2.load(service.dest_srt_file)
    assert [cue.plaintext for cue in output] == ['Egy\nMásik', 'Two', 'Három']
    assert [(cue.start, cue.end) for cue in output] == [(1000, 2000), (3000, 4000), (5000, 6000)]
    assert Path(service.source_srt_file).read_bytes() == source
    assert len(events.history) == 1
    message = events.history[0]['result'].message
    assert 'partial' in message.lower()
    assert 'source language' in message.lower()
    assert 'malformed response' in message
    assert any('partial' in message.lower() for message in events.messages)


@pytest.mark.parametrize('lines', [
    [], [{'position': 0}], [{'position': -1, 'line': 'Invalid'}],
    [{'position': 3, 'line': 'Invalid'}], [{'position': True, 'line': 'Invalid'}],
    [{'position': '0', 'line': 'Invalid'}], [{'position': 0, 'line': None}],
    [{'position': 0, 'line': ''}], [{'position': 0, 'line': '   '}],
    [{'position': 0, 'line': 'Valid'}, {'position': 0, 'line': 'Duplicate'}],
    [{'position': 0, 'line': 'Valid'}, {'position': 1, 'line': ['Invalid']}],
])
@pytest.mark.parametrize('status', ['partial', 'completed'])
def test_invalid_results_do_not_overwrite_files(service, events, monkeypatch, status, lines):
    destination = Path(service.dest_srt_file)
    destination.write_bytes(b'existing subtitle')
    source = Path(service.source_srt_file).read_bytes()
    _response(monkeypatch, status, {'lines': lines})

    assert service.translate() is False

    assert destination.read_bytes() == b'existing subtitle'
    assert Path(service.source_srt_file).read_bytes() == source
    assert events.history == []


def test_partial_detail_is_bounded_and_cleared_for_next_operation(service, events, monkeypatch):
    lines = [{'position': 0, 'line': 'Egy'}]
    _response(monkeypatch, 'partial', lines, error='Failure detail ' + 'x' * 2000)
    assert service.translate() == service.dest_srt_file
    assert len(events.history[-1]['result'].message) < 1000

    complete_lines = [{'position': i, 'line': line} for i, line in enumerate(['Egy', 'Kettő', 'Három'])]
    _response(monkeypatch, 'completed', complete_lines)
    assert service.translate() == service.dest_srt_file
    assert 'partial' not in events.history[-1]['result'].message.lower()
    assert not service.partial_error

    _response(monkeypatch, 'partial', lines, error='Failure detail')
    assert service.translate() == service.dest_srt_file
    _response(monkeypatch, 'failed', error='Unavailable')
    assert service.translate() is False
    assert not service.partial_error


def test_partial_without_error_detail_still_marks_the_saved_output(service, events, monkeypatch):
    monkeypatch.setattr(module.settings.translator, 'translator_info', True)
    _response(monkeypatch, 'partial', [{'position': 0, 'line': 'Egy'}], error='   ')

    assert service.translate() == service.dest_srt_file
    assert 'partially translated' in pysubs2.load(service.dest_srt_file)[-1].plaintext.lower()
    assert 'partial' in events.history[-1]['result'].message.lower()


def test_empty_source_does_not_report_a_saved_translation(service, events):
    Path(service.source_srt_file).write_text('', encoding='utf-8')
    assert service.translate() is False
    assert not Path(service.dest_srt_file).exists()
    assert events.history == []


@pytest.mark.parametrize('valid', [True, False])
def test_sync_fallback_uses_the_same_result_validation(service, events, monkeypatch, valid):
    lines = [{'position': 0, 'line': 'Egy'}, {'position': 1, 'line': ''}]
    if not valid:
        lines[0]['position'] = 9
    responses = iter([
        SimpleNamespace(status_code=404),
        SimpleNamespace(status_code=200, json=lambda: lines),
    ])
    monkeypatch.setattr(module.requests, 'post', lambda *args, **kwargs: next(responses))

    result = service.translate()
    if valid:
        assert result == service.dest_srt_file
        assert [cue.plaintext for cue in pysubs2.load(result)] == ['Egy', 'Two', 'Three']
        assert 'partial' in events.history[-1]['result'].message.lower()
    else:
        assert result is False
        assert not Path(service.dest_srt_file).exists()
        assert events.history == []


@pytest.mark.parametrize('status', ['partial', 'completed'])
def test_save_failure_preserves_destination_and_cleans_temporary_file(service, events, monkeypatch, status):
    destination = Path(service.dest_srt_file)
    destination.write_bytes(b'existing subtitle')
    before = set(destination.parent.iterdir())
    _response(monkeypatch, status, [{'position': 0, 'line': 'Egy'}])

    def fail_save(subtitles, filename, *args, **kwargs):
        Path(filename).write_bytes(b'incomplete write')
        raise OSError('write failed')

    monkeypatch.setattr(pysubs2.SSAFile, 'save', fail_save)

    assert service.translate() is False
    assert destination.read_bytes() == b'existing subtitle'
    assert set(destination.parent.iterdir()) == before
    assert events.history == []


@pytest.mark.parametrize('synchronous', [False, True])
def test_missing_or_blank_cues_are_reported_as_partial(service, events, monkeypatch, synchronous):
    lines = [{'position': 0, 'line': 'Egy'}, {'position': 1, 'line': '  '}]
    monkeypatch.setattr(module.settings.translator, 'translator_info', True)
    if synchronous:
        responses = iter([SimpleNamespace(status_code=404),
                          SimpleNamespace(status_code=200, json=lambda: lines)])
        monkeypatch.setattr(module.requests, 'post', lambda *args, **kwargs: next(responses))
    else:
        _response(monkeypatch, 'completed', {'lines': lines})

    assert service.translate() == service.dest_srt_file
    output = pysubs2.load(service.dest_srt_file)
    assert [cue.plaintext for cue in output[:3]] == ['Egy', 'Two', 'Three']
    assert 'partially translated' in output[-1].plaintext.lower()
    assert 'partial' in events.history[-1]['result'].message.lower()
    assert any('partial' in message.lower() for message in events.messages)


def test_long_destination_filename_retains_atomic_saving_and_permissions(service, events, monkeypatch):
    destination = Path(service.dest_srt_file).with_name('x' * 220 + '.hu.srt')
    destination.write_bytes(b'existing subtitle')
    destination.chmod(0o640)
    service.dest_srt_file = str(destination)
    before = set(destination.parent.iterdir())
    lines = [{'position': i, 'line': line} for i, line in enumerate(['Egy', 'Kettő', 'Három'])]
    _response(monkeypatch, 'completed', lines)

    assert service.translate() == str(destination)
    assert [cue.plaintext for cue in pysubs2.load(destination)] == ['Egy', 'Kettő', 'Három']
    assert destination.stat().st_mode & 0o777 == 0o640
    assert set(destination.parent.iterdir()) == before
    assert 'partial' not in events.history[-1]['result'].message.lower()


def test_existing_staging_file_is_never_overwritten_or_removed(service, events, monkeypatch):
    destination = Path(service.dest_srt_file)
    destination.write_bytes(b'existing subtitle')
    staging = destination.with_name('.bazarr-translate-fixture-collision.srt')
    staging.write_bytes(b'other operation')
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex='fixture-collision'))
    _response(monkeypatch, 'completed', [{'position': 0, 'line': 'Egy'}])

    assert service.translate() is False
    assert staging.read_bytes() == b'other operation'
    assert destination.read_bytes() == b'existing subtitle'
    assert not events.history

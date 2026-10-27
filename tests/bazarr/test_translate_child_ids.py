# coding=utf-8
"""A mass translation publishes the ids of the translation jobs it queued.

The batch job only queues one translation job per item and finishes, so the
tables it refreshes at the end were read before any child translation had run.
The batch publishes its children's ids as its job result and the client waits
on them. These tests cover both halves on the backend: translate_subtitles_file
hands back the id of the job it queued for every media type (it used to answer
for episodes only), and the batch publishes those ids without ever waiting on
them inside the queue.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _queue_one(monkeypatch, media_type, embedded_source=None, queued_id=77):
    from subtitles.tools.translate import main as translate_main

    calls = []
    monkeypatch.setattr(translate_main, 'get_title', lambda *a, **k: 'A Title')
    monkeypatch.setattr(
        translate_main.jobs_queue, 'add_job_from_function',
        lambda *a, **k: calls.append((a, k)) or queued_id)
    returned = translate_main.translate_subtitles_file(
        video_path='/video/x.mkv', source_srt_file='/subs/x.en.srt', from_lang='en', to_lang='hu',
        forced=False, hi=False, media_type=media_type, sonarr_series_id=None, sonarr_episode_id=None,
        radarr_id=None, metadata=None, job_id=None, arr_instance_id=1, embedded_source=embedded_source)
    return returned, calls


@pytest.mark.parametrize('media_type', ['movie', 'movies', 'episode', 'sports'])
def test_every_media_type_hands_back_the_id_of_the_job_it_queued(monkeypatch, media_type):
    returned, calls = _queue_one(monkeypatch, media_type)

    assert returned == 77
    assert len(calls) == 1


@pytest.mark.parametrize('media_type', ['movie', 'movies', 'episode', 'sports'])
def test_a_translation_that_reads_an_image_track_hands_back_its_id_too(monkeypatch, media_type):
    returned, calls = _queue_one(
        monkeypatch, media_type, embedded_source={'language': 'en', 'hi': False, 'forced': False, 'codec': 'pgs'})

    assert returned == 77
    assert len(calls) == 1


@pytest.mark.parametrize('media_type', ['movies', 'episode', 'sports'])
def test_every_media_type_follows_an_identical_job_already_queued(monkeypatch, media_type):
    """The id of the job that is already pending is the one to wait on."""
    returned, calls = _queue_one(monkeypatch, media_type)

    assert returned == 77
    assert calls[0][1]['return_existing'] is True


def test_a_job_that_could_not_be_queued_hands_back_nothing(monkeypatch):
    returned, _ = _queue_one(monkeypatch, 'movies', queued_id=False)

    assert returned is None


def _item(episode_id):
    return {'video_path': f'/video/ep{episode_id}.mkv', 'srt_path': f'/subs/ep{episode_id}.en.srt',
            'srt_lang': 'en', 'forced': False, 'hi': False, 'sonarr_series_id': 10,
            'sonarr_episode_id': episode_id, 'radarr_id': None, 'metadata': None}


def test_the_item_reports_the_child_job_it_queued(monkeypatch):
    from subtitles.mass_operations import _process_subtitle_item

    children = []
    with patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=41):
        assert _process_subtitle_item(_item(1), 'translate', {'from_lang': 'en', 'to_lang': 'hu'},
                                      5, children=children) is True

    assert children == [41]


@pytest.mark.parametrize('answer', [None, False, True])
def test_a_translation_that_queued_no_job_adds_no_child(answer):
    from subtitles.mass_operations import _process_subtitle_item

    children = []
    with patch('subtitles.tools.translate.main.translate_subtitles_file', return_value=answer):
        _process_subtitle_item(_item(1), 'translate', {'from_lang': 'en', 'to_lang': 'hu'}, 5,
                               children=children)

    assert children == []


@pytest.fixture
def batch(monkeypatch):
    from subtitles import mass_operations
    from subtitles.tools.translate import availability

    published = []
    monkeypatch.setattr(mass_operations, 'jobs_queue', SimpleNamespace(
        update_job_progress=lambda **kwargs: None, update_job_name=lambda **kwargs: None,
        publish_job_result=lambda job_id, result: published.append((job_id, result)) or True))
    monkeypatch.setattr(mass_operations, '_translation_gate_refusal', lambda job_id=None: None)
    monkeypatch.setattr(availability, 'translation_available',
                        lambda *a, **k: availability.TranslationAvailability(True, ''))
    monkeypatch.setattr(mass_operations, '_collect_subtitle_items',
                        lambda items, action, options: ([_item(1), _item(2), _item(3)], 0))
    return SimpleNamespace(published=published, run=mass_operations.mass_batch_operation)


def test_a_mass_translation_publishes_the_ids_of_its_children(batch):
    ids = iter([41, 42, 43])
    with patch('subtitles.tools.translate.main.translate_subtitles_file', side_effect=lambda **k: next(ids)):
        batch.run(items=[{'type': 'episode'}], action='translate', options={'to_lang': 'hu'}, job_id=5)

    assert batch.published[-1] == (5, {'child_jobs': [41, 42, 43]})


def test_the_ids_are_published_as_they_are_queued_so_a_failed_batch_keeps_them(batch):
    answers = iter([41, RuntimeError('boom'), 43])

    def translate(**kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    from app.jobs_queue import JobFailed

    with patch('subtitles.tools.translate.main.translate_subtitles_file', side_effect=translate):
        with pytest.raises(JobFailed):
            batch.run(items=[{'type': 'episode'}], action='translate', options={'to_lang': 'hu'}, job_id=5)

    assert batch.published[-1] == (5, {'child_jobs': [41, 43]})


def test_an_id_two_items_share_is_published_once_and_never_the_batch_itself(batch):
    ids = iter([41, 41, 5])
    with patch('subtitles.tools.translate.main.translate_subtitles_file', side_effect=lambda **k: next(ids)):
        batch.run(items=[{'type': 'episode'}], action='translate', options={'to_lang': 'hu'}, job_id=5)

    assert batch.published[-1] == (5, {'child_jobs': [41]})


def test_other_batches_publish_nothing(batch):
    # Four parameters only: the other actions are not handed a children list.
    with patch('subtitles.mass_operations._process_subtitle_item',
               side_effect=lambda item, action, options, job_id: True):
        batch.run(items=[{'type': 'episode'}], action='sync', options={}, job_id=5)

    assert batch.published == []

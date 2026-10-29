# coding=utf-8
"""Mass translation prefetches embedded sources without reordering its jobs."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock, Thread, current_thread
from time import sleep
from types import SimpleNamespace

import pytest


def _item(number, **overrides):
    item = {
        'video_path': f'/video/ep{number}.mkv',
        'srt_path': None,
        'srt_lang': 'en',
        'forced': False,
        'hi': False,
        'sonarr_series_id': 10,
        'sonarr_episode_id': number,
        'radarr_id': None,
        'arr_instance_id': 7,
        'metadata': None,
        'embedded': True,
    }
    item.update(overrides)
    return item


@pytest.fixture
def mass(monkeypatch):
    from subtitles import mass_operations
    from subtitles.tools.translate import availability, batch, main

    state = SimpleNamespace(
        module=mass_operations,
        items=[_item(number) for number in range(1, 5)],
        translated=[], published=[], progress=[], names=[],
        general=SimpleNamespace(mass_embedded_extract_workers=2),
    )
    monkeypatch.setattr(mass_operations, 'settings', SimpleNamespace(general=state.general))
    monkeypatch.setattr(mass_operations, '_collect_subtitle_items',
                        lambda items, action, options: (list(state.items), 0))
    monkeypatch.setattr(availability, 'translation_available',
                        lambda: availability.TranslationAvailability(True, ''))
    monkeypatch.setattr(mass_operations, 'jobs_queue', SimpleNamespace(
        update_job_progress=lambda **kwargs: state.progress.append(kwargs),
        update_job_name=lambda **kwargs: state.names.append(kwargs['new_job_name']),
        publish_job_result=lambda job_id, result: state.published.append((job_id, result)),
    ))

    def extract(video_path, language, media_type, **kwargs):
        return video_path.replace('/video/', '/extracted/').replace('.mkv', '.en.srt')

    def translate(**kwargs):
        state.translated.append(kwargs)
        return 100 + kwargs['sonarr_episode_id']

    monkeypatch.setattr(batch, 'extract_embedded_subtitle', extract)
    monkeypatch.setattr(main, 'translate_subtitles_file', translate)
    state.extract_module = batch
    state.translate_module = main
    state.run = lambda action='translate': mass_operations.mass_batch_operation(
        items=[{'type': 'episode', 'sonarrEpisodeId': 1}], action=action,
        options={'to_lang': 'hu'}, job_id=5)
    return state


class _TrackedFuture(Future):
    def __init__(self, pool, number):
        super().__init__()
        self.pool = pool
        self.number = number
        self.consumed = False

    def result(self, timeout=None):
        if not self.consumed:
            self.pool.events.append(('consume', self.number))
            self.pool.unconsumed -= 1
            self.consumed = True
        if not self.done():
            raise AssertionError('a pending extraction was consumed after cancellation')
        return super().result(timeout=timeout)


class _ImmediateExecutor:
    """Finish immediately so completed, unread futures still fill the window."""

    def __init__(self, *, max_workers, thread_name_prefix):
        self.workers = max_workers
        self.prefix = thread_name_prefix
        self.events = []
        self.futures = []
        self.unconsumed = 0
        self.maximum_unconsumed = 0
        self.shutdown_calls = []

    def submit(self, function, item):
        number = item['sonarr_episode_id']
        self.events.append(('submit', number))
        self.unconsumed += 1
        self.maximum_unconsumed = max(self.maximum_unconsumed, self.unconsumed)
        future = _TrackedFuture(self, number)
        self.futures.append(future)
        try:
            future.set_result(function(item))
        except Exception as error:
            future.set_exception(error)
        return future

    def shutdown(self, *, wait=True, cancel_futures=False):
        self.shutdown_calls.append((wait, cancel_futures))
        if cancel_futures:
            for future in self.futures:
                future.cancel()


def _use_executor(monkeypatch, mass, executor_class=_ImmediateExecutor):
    pools = []

    def make_pool(**kwargs):
        pool = executor_class(**kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(mass.module, 'ThreadPoolExecutor', make_pool, raising=False)
    return pools


def test_out_of_order_extractions_keep_translation_order_and_matching_sources(mass, monkeypatch):
    mass.items = mass.items[:3]
    second_started = Event()
    second_finished = Event()
    lock = Lock()
    active = 0
    maximum_active = 0
    completed = []
    thread_names = []

    def extract(video_path, language, media_type, **kwargs):
        nonlocal active, maximum_active
        number = int(video_path.rsplit('ep', 1)[1].split('.')[0])
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            thread_names.append(current_thread().name)
        try:
            if number == 1:
                assert second_started.wait(2), 'the second extraction did not start concurrently'
            elif number == 2:
                second_started.set()
            sleep(0.02 / number)
            if number == 1:
                assert second_finished.wait(2)
            with lock:
                completed.append(number)
            if number == 2:
                second_finished.set()
            return f'/extracted/ep{number}.en.srt'
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', extract)

    assert mass.run() == {'queued': 3, 'skipped': 0, 'errors': []}

    assert completed[:2] == [2, 1]
    assert maximum_active == 2
    assert all(name.startswith('mass-extract') for name in thread_names)
    assert [(item['video_path'], item['source_srt_file']) for item in mass.translated] == [
        ('/video/ep1.mkv', '/extracted/ep1.en.srt'),
        ('/video/ep2.mkv', '/extracted/ep2.en.srt'),
        ('/video/ep3.mkv', '/extracted/ep3.en.srt'),
    ]
    assert mass.published[-1] == (5, {'child_jobs': [101, 102, 103]})


@pytest.mark.parametrize('workers', [1, 2])
def test_completed_unconsumed_extractions_fill_the_window_and_refill_before_translation(
        mass, monkeypatch, workers):
    mass.general.mass_embedded_extract_workers = workers
    pools = _use_executor(monkeypatch, mass)
    translate = mass.translate_module.translate_subtitles_file

    def record_translation(**kwargs):
        if pools:
            pools[0].events.append(('translate', kwargs['sonarr_episode_id']))
        return translate(**kwargs)

    monkeypatch.setattr(mass.translate_module, 'translate_subtitles_file', record_translation)

    result = mass.run()

    assert len(pools) == 1
    pool = pools[0]
    assert pool.workers == workers
    assert pool.prefix == 'mass-extract'
    assert pool.maximum_unconsumed == workers
    assert all(future.done() and future.consumed for future in pool.futures)
    assert pool.shutdown_calls == [(False, True)]
    assert result == {'queued': 4, 'skipped': 0, 'errors': []}
    assert [item['sonarr_episode_id'] for item in mass.translated] == [1, 2, 3, 4]
    assert [item['source_srt_file'] for item in mass.translated] == [
        '/extracted/ep1.en.srt', '/extracted/ep2.en.srt',
        '/extracted/ep3.en.srt', '/extracted/ep4.en.srt',
    ]
    if workers == 2:
        assert pool.events == [
            ('submit', 1), ('submit', 2),
            ('consume', 1), ('submit', 3), ('translate', 1),
            ('consume', 2), ('submit', 4), ('translate', 2),
            ('consume', 3), ('translate', 3),
            ('consume', 4), ('translate', 4),
        ]
    else:
        assert pool.events == [
            ('submit', 1), ('consume', 1), ('submit', 2), ('translate', 1),
            ('consume', 2), ('submit', 3), ('translate', 2),
            ('consume', 3), ('submit', 4), ('translate', 3),
            ('consume', 4), ('translate', 4),
        ]


def test_none_and_exception_extractions_fail_only_their_items_and_publish_survivors(
        mass, monkeypatch, caplog):
    from app.jobs_queue import JobFailed

    mass.items.append(_item(5))
    pools = _use_executor(monkeypatch, mass)
    translate = mass.translate_module.translate_subtitles_file

    def extract(video_path, language, media_type, **kwargs):
        if video_path == '/video/ep1.mkv':
            return None
        if video_path == '/video/ep3.mkv':
            raise RuntimeError('broken source track')
        return video_path.replace('/video/', '/extracted/').replace('.mkv', '.en.srt')

    def record_translation(**kwargs):
        if pools:
            pools[0].events.append(('translate', kwargs['sonarr_episode_id']))
        return translate(**kwargs)

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', extract)
    monkeypatch.setattr(mass.translate_module, 'translate_subtitles_file', record_translation)
    with pytest.raises(JobFailed) as failure:
        mass.run()

    message = str(failure.value)
    assert '2 of 5 items failed (3 done, 0 skipped)' in message
    assert 'ep1.mkv (en track) (the embedded track could not be extracted)' in message
    assert 'ep3.mkv (en track) (broken source track)' in message
    assert [item['sonarr_episode_id'] for item in mass.translated] == [2, 4, 5]
    assert mass.published == [
        (5, {'child_jobs': [102]}),
        (5, {'child_jobs': [102, 104]}),
        (5, {'child_jobs': [102, 104, 105]}),
    ]
    assert 'could not extract the embedded en track from /video/ep1.mkv, skipping translation' in caplog.text
    assert len(pools) == 1
    assert pools[0].maximum_unconsumed == 2
    assert pools[0].events == [
        ('submit', 1), ('submit', 2), ('consume', 1), ('submit', 3),
        ('consume', 2), ('submit', 4), ('translate', 2),
        ('consume', 3), ('submit', 5),
        ('consume', 4), ('translate', 4), ('consume', 5), ('translate', 5),
    ]
    assert pools[0].shutdown_calls == [(False, True)]


class _PendingExecutor(_ImmediateExecutor):
    def submit(self, function, item):
        if not self.futures:
            return super().submit(function, item)
        number = item['sonarr_episode_id']
        self.events.append(('submit', number))
        self.unconsumed += 1
        self.maximum_unconsumed = max(self.maximum_unconsumed, self.unconsumed)
        future = _TrackedFuture(self, number)
        self.futures.append(future)
        return future


@pytest.mark.parametrize('executor_class', [_ImmediateExecutor, _PendingExecutor])
def test_gate_closure_stops_extraction_and_fails_remaining_items(mass, monkeypatch, executor_class):
    from app.jobs_queue import JobFailed
    from subtitles.tools.translate import availability

    pools = _use_executor(monkeypatch, mass, executor_class)
    gate = availability.TranslationAvailability(True, '')

    def progress(**kwargs):
        nonlocal gate
        mass.progress.append(kwargs)
        if kwargs.get('progress_value') == 1 and '(1/4)' in kwargs.get('progress_message', ''):
            gate = availability.TranslationAvailability(False, 'Translation was switched off')
            pools[0].events.append(('gate_closed', 1))

    monkeypatch.setattr(availability, 'translation_available', lambda: gate)
    monkeypatch.setattr(mass.module.jobs_queue, 'update_job_progress', progress)

    with pytest.raises(JobFailed) as failure:
        mass.run()

    pool = pools[0]
    closed_at = pool.events.index(('gate_closed', 1))
    assert not any(event == 'submit' for event, _ in pool.events[closed_at + 1:])
    assert [item['sonarr_episode_id'] for item in mass.translated] == [1]
    assert str(failure.value) == (
        'Mass translate: 3 of 4 items failed (1 done, 0 skipped). '
        'ep2.mkv (en track) (Translation was switched off); '
        'ep3.mkv (en track) (Translation was switched off); '
        'ep4.mkv (en track) (Translation was switched off)'
    )
    assert [number for event, number in pool.events if event == 'consume'] == [1]
    assert pool.shutdown_calls == [(False, True)]
    if executor_class is _PendingExecutor:
        assert [future.cancelled() for future in pool.futures] == [False, True, True]
    assert mass.published == [(5, {'child_jobs': [101]})]


def test_cancellation_from_an_extraction_result_propagates_and_cancels_pending_work(mass, monkeypatch):
    from app.jobs_queue import JobCancelled

    pools = _use_executor(monkeypatch, mass, _PendingExecutor)

    def extract(*args, **kwargs):
        raise JobCancelled('extraction stopped', job_id=5)

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', extract)
    with pytest.raises(JobCancelled, match='extraction stopped'):
        mass.run()

    assert pools[0].shutdown_calls == [(False, True)]
    assert [future.cancelled() for future in pools[0].futures] == [False, True, True]
    assert mass.translated == []
    assert mass.published == []


def test_failure_updating_completed_item_progress_still_cancels_pending_work(mass, monkeypatch):
    pools = _use_executor(monkeypatch, mass, _PendingExecutor)

    def progress(**kwargs):
        if kwargs.get('progress_value') == 1 and '(1/4)' in kwargs.get('progress_message', ''):
            raise RuntimeError('progress failed after translation')

    monkeypatch.setattr(mass.module.jobs_queue, 'update_job_progress', progress)
    with pytest.raises(RuntimeError, match='progress failed after translation'):
        mass.run()

    assert pools[0].shutdown_calls == [(False, True)]
    assert [future.cancelled() for future in pools[0].futures] == [False, True, True]
    assert [item['sonarr_episode_id'] for item in mass.translated] == [1]


@pytest.mark.parametrize('error_type', ['cancelled', 'progress'])
def test_progress_interruptions_cancel_pending_extractions_without_waiting(
        mass, monkeypatch, error_type):
    from app.jobs_queue import JobCancelled

    pools = _use_executor(monkeypatch, mass, _PendingExecutor)
    error = JobCancelled('stopped', job_id=5) if error_type == 'cancelled' else RuntimeError('progress failed')

    def progress(**kwargs):
        mass.progress.append(kwargs)
        if kwargs.get('progress_value') == 1 and '(2/4)' in kwargs.get('progress_message', ''):
            raise error

    monkeypatch.setattr(mass.module.jobs_queue, 'update_job_progress', progress)
    with pytest.raises(type(error), match=str(error)):
        mass.run()

    assert len(pools) == 1
    pool = pools[0]
    assert pool.shutdown_calls == [(False, True)]
    assert [number for event, number in pool.events if event == 'submit'] == [1, 2, 3]
    assert [number for event, number in pool.events if event == 'consume'] == [1]
    assert [future.cancelled() for future in pool.futures] == [False, True, True]
    assert [item['sonarr_episode_id'] for item in mass.translated] == [1]
    assert mass.published == [(5, {'child_jobs': [101]})]


def test_cancellation_returns_while_an_extraction_is_still_running(mass, monkeypatch):
    from app.jobs_queue import JobCancelled

    second_started = Event()
    release_second = Event()
    second_finished = Event()
    batch_finished = Event()
    errors = []
    pools = []

    class ObservedExecutor(ThreadPoolExecutor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.shutdown_calls = []
            self.futures = []
            pools.append(self)

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.futures.append(future)
            return future

        def shutdown(self, *, wait=True, cancel_futures=False):
            self.shutdown_calls.append((wait, cancel_futures))
            return super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def extract(video_path, language, media_type, **kwargs):
        if video_path == '/video/ep1.mkv':
            assert second_started.wait(5)
        elif video_path == '/video/ep2.mkv':
            second_started.set()
            try:
                assert release_second.wait(5), 'test cleanup did not release the running extraction'
            finally:
                second_finished.set()
        return video_path.replace('/video/', '/extracted/').replace('.mkv', '.en.srt')

    def progress(**kwargs):
        if kwargs.get('progress_value') == 1 and '(2/4)' in kwargs.get('progress_message', ''):
            raise JobCancelled('stopped', job_id=5)

    def run():
        try:
            mass.run()
        except BaseException as error:
            errors.append(error)
        finally:
            batch_finished.set()

    monkeypatch.setattr(mass.module, 'ThreadPoolExecutor', ObservedExecutor, raising=False)
    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', extract)
    monkeypatch.setattr(mass.module.jobs_queue, 'update_job_progress', progress)
    runner = Thread(target=run, name='test-mass-translation')
    runner.start()
    try:
        assert second_started.wait(5)
        assert batch_finished.wait(2), 'cancellation waited for the running extraction'
        assert not second_finished.is_set()
        assert len(errors) == 1 and isinstance(errors[0], JobCancelled)
        assert pools[0].futures[1].running()
        assert pools[0].shutdown_calls == [(False, True)]
        assert [item['sonarr_episode_id'] for item in mass.translated] == [1]
        assert mass.published == [(5, {'child_jobs': [101]})]
    finally:
        release_second.set()
        runner.join(timeout=5)
        for pool in pools:
            ThreadPoolExecutor.shutdown(pool, wait=True, cancel_futures=True)
    assert not runner.is_alive()
    assert [item['sonarr_episode_id'] for item in mass.translated] == [1]


@pytest.mark.parametrize('overrides, media_type', [
    ({}, 'episode'),
    ({'sonarr_series_id': None, 'sonarr_episode_id': None, 'radarr_id': 30}, 'movies'),
    ({'sonarr_series_id': None, 'sonarr_episode_id': None, 'sports_event_id': 61}, 'sports'),
])
def test_extraction_helper_preserves_track_flags_media_type_and_owner(
        mass, monkeypatch, overrides, media_type):
    calls = []

    def extract(*args, **kwargs):
        calls.append((args, kwargs))
        return '/extracted/track.fr.hi.forced.srt'

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', extract)
    item = _item(1, srt_lang='fr', hi=True, forced=True, **overrides)

    assert mass.module._extract_translate_source(item) == '/extracted/track.fr.hi.forced.srt'
    assert calls == [(('/video/ep1.mkv', 'fr', media_type),
                      {'hi': True, 'forced': True, 'arr_instance_id': 7})]


def test_direct_call_without_prefetched_source_still_extracts_inline(mass, monkeypatch):
    calls = []

    def extract(*args, **kwargs):
        calls.append((args, kwargs))
        return '/extracted/direct.en.srt'

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', extract)
    children = []

    assert mass.module._process_subtitle_item(
        _item(1), 'translate', {'to_lang': 'hu'}, 5, children=children) is True
    assert len(calls) == 1
    assert mass.translated[0]['source_srt_file'] == '/extracted/direct.en.srt'
    assert children == [101]


def test_mixed_file_and_embedded_sources_keep_their_order(mass, monkeypatch):
    mass.items = [_item(1), _item(2, embedded=False, srt_path='/subs/ep2.en.srt'), _item(3)]
    pools = _use_executor(monkeypatch, mass)

    assert mass.run() == {'queued': 3, 'skipped': 0, 'errors': []}

    assert [number for event, number in pools[0].events if event == 'submit'] == [1, 3]
    assert [(item['sonarr_episode_id'], item['source_srt_file']) for item in mass.translated] == [
        (1, '/extracted/ep1.en.srt'), (2, '/subs/ep2.en.srt'), (3, '/extracted/ep3.en.srt'),
    ]
    assert mass.published[-1] == (5, {'child_jobs': [101, 102, 103]})


def test_sports_embedded_source_uses_the_pool_and_keeps_its_owner_and_order(mass, monkeypatch):
    from sportarr import profile_hooks

    mass.items = [_item(1),
                  _item(2, video_path='/video/event2.mkv', sonarr_series_id=None,
                        sonarr_episode_id=None, sports_event_id=61),
                  _item(3)]
    pools = _use_executor(monkeypatch, mass)
    calls = []
    extract = mass.extract_module.extract_embedded_subtitle

    def record_extraction(video_path, language, media_type, **kwargs):
        calls.append((video_path, media_type, kwargs['arr_instance_id']))
        return extract(video_path, language, media_type, **kwargs)

    def translate(**kwargs):
        mass.translated.append(kwargs)
        return 100 + len(mass.translated)

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', record_extraction)
    monkeypatch.setattr(mass.translate_module, 'translate_subtitles_file', translate)
    monkeypatch.setattr(profile_hooks, 'manual_translation_operation', lambda *args, **kwargs: None)

    assert mass.run() == {'queued': 3, 'skipped': 0, 'errors': []}

    assert len(pools) == 1
    assert [number for event, number in pools[0].events if event == 'submit'] == [1, None, 3]
    assert calls == [('/video/ep1.mkv', 'episode', 7),
                     ('/video/event2.mkv', 'sports', 7),
                     ('/video/ep3.mkv', 'episode', 7)]
    assert [(item['video_path'], item['source_srt_file']) for item in mass.translated] == [
        ('/video/ep1.mkv', '/extracted/ep1.en.srt'),
        ('/video/event2.mkv', '/extracted/event2.en.srt'),
        ('/video/ep3.mkv', '/extracted/ep3.en.srt'),
    ]
    assert mass.translated[1]['media_type'] == 'sports'
    assert mass.translated[1]['arr_instance_id'] == 7
    assert mass.published[-1] == (5, {'child_jobs': [101, 102, 103]})


def test_file_only_translation_uses_existing_paths_without_extraction(mass, monkeypatch):
    mass.items = [_item(1, embedded=False, srt_path='/subs/ep1.en.srt'),
                  _item(2, embedded=False, srt_path='/subs/ep2.en.srt')]
    pools = _use_executor(monkeypatch, mass)

    def unexpected_extraction(*args, **kwargs):
        pytest.fail('an existing subtitle file must not be extracted')

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', unexpected_extraction)

    assert mass.run() == {'queued': 2, 'skipped': 0, 'errors': []}

    assert [item['source_srt_file'] for item in mass.translated] == ['/subs/ep1.en.srt', '/subs/ep2.en.srt']
    assert len(pools) == 1
    assert all(pool.futures == [] for pool in pools)
    assert mass.published[-1] == (5, {'child_jobs': [101, 102]})


@pytest.mark.parametrize('action', ['sync', 'remove_HI', 'extract'])
def test_other_subtitle_actions_do_not_create_an_extraction_pool(mass, monkeypatch, action):
    processed = []

    def unexpected_pool(**kwargs):
        pytest.fail('only translation may create the extraction pool')

    def process(item, requested_action, options, job_id):
        processed.append((item['sonarr_episode_id'], requested_action, job_id))
        return True

    monkeypatch.setattr(mass.module, 'ThreadPoolExecutor', unexpected_pool, raising=False)
    monkeypatch.setattr(mass.module, '_process_subtitle_item', process)

    assert mass.run(action) == {'queued': 4, 'skipped': 0, 'errors': []}

    assert processed == [(1, action, 5), (2, action, 5), (3, action, 5), (4, action, 5)]
    assert mass.published == []


@pytest.mark.parametrize('source', ['/already/extracted.en.srt', None])
def test_a_prefetched_source_is_used_without_extracting_again(mass, monkeypatch, source):
    def unexpected_extraction(*args, **kwargs):
        pytest.fail('a supplied source must not be extracted twice')

    monkeypatch.setattr(mass.extract_module, 'extract_embedded_subtitle', unexpected_extraction)
    children = []

    result = mass.module._process_subtitle_item(
        _item(1), 'translate', {'to_lang': 'hu'}, 5,
        children=children, source_srt_file=source)

    assert result is (source is not None)
    if source is None:
        assert mass.translated == []
        assert children == []
    else:
        assert mass.translated[0]['source_srt_file'] == '/already/extracted.en.srt'
        assert children == [101]


def _worker_settings():
    from app import config
    from dynaconf import Dynaconf

    worker_validators = [validator for validator in config.validators
                         if validator.names == ('general.mass_embedded_extract_workers',)]
    assert len(worker_validators) == 1, 'the extraction worker setting needs a validator'
    return Dynaconf(core_loaders=['YAML'], envvar_prefix=False, load_dotenv=False,
                    validators=worker_validators)


def test_extraction_workers_default_to_two():
    settings = _worker_settings()
    settings.validators.validate()
    assert settings.general.mass_embedded_extract_workers == 2
    assert type(settings.general.mass_embedded_extract_workers) is int


@pytest.mark.parametrize('workers', [0, 5, 'two', 1.5, [2], None])
def test_extraction_workers_reject_invalid_types_and_bounds(workers):
    from dynaconf.validator import ValidationError

    settings = _worker_settings()
    settings.set('general.mass_embedded_extract_workers', workers)
    with pytest.raises(ValidationError):
        settings.validators.validate()


@pytest.mark.parametrize('workers', [1, 2, 4])
def test_extraction_workers_accept_the_supported_integer_range(workers):
    settings = _worker_settings()
    settings.set('general.mass_embedded_extract_workers', workers)
    settings.validators.validate()
    assert settings.general.mass_embedded_extract_workers == workers

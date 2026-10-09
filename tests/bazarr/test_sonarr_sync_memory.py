# coding=utf-8
"""The bulk Sonarr sync must not hold the whole library's episode JSON at once.

The sync prefetches each series' episode list in parallel, which is the right
shape: the HTTP round trips dominate a full sync. It then walked the series
serially and read each result out of the futures dict with ``[...]``, leaving
every consumed future, and therefore every episode payload it had already
finished with, referenced until the executor block ended.

So peak memory scaled with the size of the library rather than with how much of
it was in flight, and it did so once an hour per configured instance. On a small
library that is invisible. On a large one it is the largest single allocation
the process ever makes.

This test drives the real loop and asserts the payloads become collectable as it
goes, rather than asserting the shape of the code.
"""
import gc
import weakref
from types import SimpleNamespace

import pytest


class _Payload:
    """A stand-in episode list. Weak-referenceable, unlike a plain list."""

    def __init__(self, series_id):
        self.series_id = series_id


@pytest.fixture(params=[3])
def sync_harness(request, monkeypatch):
    import sonarr.sync.series as series_mod

    shows = [{'id': n, 'title': f'Show {n}', 'monitored': True}
             for n in range(1, request.param + 1)]
    payloads = {}
    refs = {}

    def _episodes(apikey_sonarr=None, series_id=None, arr_client=None):
        payload = _Payload(series_id)
        payloads[series_id] = payload
        refs[series_id] = weakref.ref(payload)
        return payload

    monkeypatch.setattr(series_mod, 'get_episodes_from_sonarr_api', _episodes)
    monkeypatch.setattr(series_mod, 'get_series_from_sonarr_api', lambda **kw: shows)
    monkeypatch.setattr(series_mod, 'check_sonarr_rootfolder', lambda **kw: None)
    monkeypatch.setattr(series_mod, 'get_profile_list', lambda **kw: [])
    monkeypatch.setattr(series_mod, 'get_tags', lambda **kw: [])
    monkeypatch.setattr(series_mod, 'update_one_series', lambda *a, **kw: None)
    monkeypatch.setattr(series_mod, 'event_stream', lambda *a, **kw: None)
    monkeypatch.setattr(series_mod, 'jobs_queue',
                        SimpleNamespace(update_job_progress=lambda **kw: None,
                                        update_job_name=lambda **kw: None,
                                        add_job_from_function=lambda *a, **kw: None))
    monkeypatch.setattr(series_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(series_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    return series_mod, shows, payloads, refs


def test_a_consumed_episode_payload_becomes_collectable(sync_harness, monkeypatch,
                                                        schema_session):
    series_mod, shows, payloads, refs = sync_harness
    observed = []

    def _sync_episodes(series_id=None, episodes_data=None, **kw):
        # Drop our own strong reference to the payload we were handed, the way
        # the real consumer does once it has written the rows.
        payloads.pop(series_id, None)
        if series_id == shows[-1]['id']:
            # Processing the LAST series: everything before it is finished with.
            gc.collect()
            observed.append([sid for sid in (shows[0]['id'], shows[1]['id'])
                             if refs[sid]() is not None])

    monkeypatch.setattr(series_mod, 'sync_episodes', _sync_episodes)
    monkeypatch.setattr(series_mod, 'database', schema_session)

    series_mod.update_series(job_id='job-1')

    assert observed, 'the loop never reached the last series'
    assert observed[0] == [], (
        'episode payloads for series already processed were still referenced '
        f'while the last one was being handled: {observed[0]}. Peak memory '
        'therefore scales with the whole library rather than with what is in '
        'flight.')


@pytest.mark.parametrize('sync_harness', [60], indirect=True)
def test_read_ahead_is_bounded_by_the_window(sync_harness, monkeypatch, schema_session):
    """Popping consumed futures is not on its own a bound.

    The fetchers run in parallel and finish far ahead of a consumer that writes
    to the database serially, so submitting every series up front leaves the
    completed payloads queued regardless of how promptly each one is popped, and
    peak memory still tracks the size of the library. What bounds it is not
    submitting them all.
    """
    series_mod, shows, payloads, refs = sync_harness
    submitted = []
    consumed = []

    real_episodes = series_mod.get_episodes_from_sonarr_api

    def _counting_episodes(**kwargs):
        submitted.append(kwargs['series_id'])
        return real_episodes(**kwargs)

    monkeypatch.setattr(series_mod, 'get_episodes_from_sonarr_api', _counting_episodes)

    outstanding_seen = []

    def _sync_episodes(series_id=None, episodes_data=None, **kw):
        payloads.pop(series_id, None)
        consumed.append(series_id)
        # Submitted but not yet consumed, i.e. results that may be in memory.
        outstanding_seen.append(len(submitted) - len(consumed))

    monkeypatch.setattr(series_mod, 'sync_episodes', _sync_episodes)
    monkeypatch.setattr(series_mod, 'database', schema_session)

    series_mod.update_series(job_id='job-1')

    assert len(consumed) == len(shows), 'every series should still be processed'
    assert consumed == [s['id'] for s in shows], 'series order was not preserved'
    assert max(outstanding_seen) <= series_mod.SONARR_PREFETCH_WINDOW, (
        f'up to {max(outstanding_seen)} episode payloads were outstanding at '
        f'once against a window of {series_mod.SONARR_PREFETCH_WINDOW}; the '
        'read-ahead is not bounded'
    )


def _capture_job_names(series_mod, monkeypatch):
    names = []
    monkeypatch.setattr(series_mod, 'jobs_queue',
                        SimpleNamespace(update_job_progress=lambda **kw: None,
                                        update_job_name=lambda **kw: names.append(kw['new_job_name']),
                                        add_job_from_function=lambda *a, **kw: None))
    return names


def _sonarr_warnings(caplog):
    return [r for r in caplog.records
            if r.levelname == 'WARNING' and 'did not return episodes' in r.getMessage()]


@pytest.mark.parametrize('sync_harness', [5], indirect=True)
def test_a_failed_prefetch_is_skipped_once_and_reported(sync_harness, monkeypatch,
                                                        schema_session, caplog):
    """A series whose episode fetch failed is not handed on as "fetch it yourself".

    sync_episodes treats a None payload as a request to ask Sonarr again, so a
    series that just failed was requested a second time, serially, against a
    Sonarr that was already struggling. It was then skipped without a word and
    the job still ended as fully synced.
    """
    series_mod, shows, payloads, refs = sync_harness
    real_episodes = series_mod.get_episodes_from_sonarr_api
    fetched = []

    def _episodes(**kwargs):
        fetched.append(kwargs['series_id'])
        if kwargs['series_id'] in (2, 3):
            return None
        return real_episodes(**kwargs)

    synced = []
    monkeypatch.setattr(series_mod, 'get_episodes_from_sonarr_api', _episodes)
    monkeypatch.setattr(series_mod, 'sync_episodes',
                        lambda series_id=None, **kw: synced.append(series_id))
    monkeypatch.setattr(series_mod, 'database', schema_session)
    names = _capture_job_names(series_mod, monkeypatch)

    series_mod.update_series(job_id='job-1')

    assert sorted(fetched) == [1, 2, 3, 4, 5]
    assert synced == [1, 4, 5]
    warnings = _sonarr_warnings(caplog)
    assert len(warnings) == 1
    assert '2 of 5' in warnings[0].getMessage()
    assert names == ['Synced series with Sonarr (2 skipped)']


@pytest.mark.parametrize('sync_harness', [60], indirect=True)
def test_the_prefetch_stops_after_consecutive_failures(sync_harness, monkeypatch,
                                                      schema_session, caplog):
    series_mod, shows, payloads, refs = sync_harness
    fetched = []

    def _episodes(**kwargs):
        fetched.append(kwargs['series_id'])
        return None

    synced = []
    updated = []
    monkeypatch.setattr(series_mod, 'get_episodes_from_sonarr_api', _episodes)
    monkeypatch.setattr(series_mod, 'sync_episodes',
                        lambda series_id=None, **kw: synced.append(series_id))
    monkeypatch.setattr(series_mod, 'update_one_series',
                        lambda series_id, *a, **kw: updated.append(series_id))
    monkeypatch.setattr(series_mod, 'database', schema_session)
    names = _capture_job_names(series_mod, monkeypatch)

    series_mod.update_series(job_id='job-1')

    limit = series_mod.SONARR_PREFETCH_WINDOW + series_mod.SONARR_PREFETCH_FAILURE_LIMIT
    assert len(fetched) <= limit, (
        f'{len(fetched)} episode requests went to a failing Sonarr; the prefetch '
        f'should stop after {series_mod.SONARR_PREFETCH_FAILURE_LIMIT} failures in a row')
    assert synced == []
    # The series rows still come from the bulk payload, which needs no request.
    assert updated == [s['id'] for s in shows]
    warnings = _sonarr_warnings(caplog)
    assert len(warnings) == 1
    assert '60 of 60' in warnings[0].getMessage()
    assert names == ['Synced series with Sonarr (60 skipped)']


@pytest.mark.parametrize('sync_harness', [5], indirect=True)
def test_a_clean_sync_keeps_its_name_and_logs_no_warning(sync_harness, monkeypatch,
                                                         schema_session, caplog):
    series_mod, shows, payloads, refs = sync_harness
    synced = []
    monkeypatch.setattr(series_mod, 'sync_episodes',
                        lambda series_id=None, **kw: synced.append(series_id))
    monkeypatch.setattr(series_mod, 'database', schema_session)
    names = _capture_job_names(series_mod, monkeypatch)

    series_mod.update_series(job_id='job-1')

    assert synced == [1, 2, 3, 4, 5]
    assert _sonarr_warnings(caplog) == []
    assert names == ['Synced series with Sonarr']


@pytest.mark.parametrize('sync_harness', [5], indirect=True)
def test_a_series_skipped_for_an_unreadable_episode_file_list_is_counted(
        sync_harness, monkeypatch, schema_session, caplog):
    """A series whose episode-file answer cannot be decoded is skipped whole by
    sync_episodes, which used to be indistinguishable from a sync that ran: the
    skipped count, the warning and the job name all said the series was synced.
    The sentinel return lets the bulk sync count it the way a failed episode
    fetch is counted."""
    from sonarr.sync.episodes import SYNC_SKIPPED_UNREADABLE_EPISODE_FILES

    series_mod, shows, payloads, refs = sync_harness
    synced = []

    def _sync(series_id=None, **kw):
        synced.append(series_id)
        if series_id == 2:
            return SYNC_SKIPPED_UNREADABLE_EPISODE_FILES
        return None

    monkeypatch.setattr(series_mod, 'sync_episodes', _sync)
    monkeypatch.setattr(series_mod, 'database', schema_session)
    names = _capture_job_names(series_mod, monkeypatch)

    series_mod.update_series(job_id='job-1')

    assert synced == [1, 2, 3, 4, 5]
    warnings = _sonarr_warnings(caplog)
    assert len(warnings) == 1
    assert '1 of 5' in warnings[0].getMessage()
    assert 'Series ids: 2' in warnings[0].getMessage()
    assert names == ['Synced series with Sonarr (1 skipped)']


def test_a_failed_series_list_fetch_skips_the_run_loudly(monkeypatch, schema_session, caplog):
    """A bulk series list that never arrives must not end the sync silently.

    A failed list fetch ended the run through the broad guard around the
    call, with nothing counted and no word to the user. Treating the missing
    list as an empty library instead would remove every show from the
    database, so the run ends counted as skipped, with one warning and a job
    name that says so, and touches nothing.
    """
    import sonarr.sync.series as series_mod

    monkeypatch.setattr(series_mod, 'get_series_from_sonarr_api', lambda **kw: None)
    monkeypatch.setattr(series_mod, 'get_episodes_from_sonarr_api',
                        lambda **kw: pytest.fail('no episode fetch should happen'))
    monkeypatch.setattr(series_mod, 'update_one_series',
                        lambda *a, **kw: pytest.fail('no series row should be written or removed'))
    monkeypatch.setattr(series_mod, 'check_sonarr_rootfolder', lambda **kw: None)
    monkeypatch.setattr(series_mod, 'get_profile_list', lambda **kw: [])
    monkeypatch.setattr(series_mod, 'get_tags', lambda **kw: [])
    monkeypatch.setattr(series_mod, 'database', schema_session)
    monkeypatch.setattr(series_mod.settings.sonarr, 'apikey', 'x', raising=False)
    monkeypatch.setattr(series_mod.settings.sonarr, 'sync_only_monitored_series', False,
                        raising=False)
    names = []
    monkeypatch.setattr(series_mod, 'jobs_queue',
                        SimpleNamespace(update_job_progress=lambda **kw: None,
                                        update_job_name=lambda **kw: names.append(kw['new_job_name']),
                                        add_job_from_function=lambda *a, **kw: None))

    series_mod.update_series(job_id='job-1')

    assert names == ['Synced series with Sonarr (series list skipped)']
    warnings = [r for r in caplog.records
                if r.levelname == 'WARNING' and 'usable series list' in r.getMessage()]
    assert len(warnings) == 1

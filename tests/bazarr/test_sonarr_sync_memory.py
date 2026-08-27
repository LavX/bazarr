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
from unittest.mock import patch

import pytest


class _Payload:
    """A stand-in episode list. Weak-referenceable, unlike a plain list."""

    def __init__(self, series_id):
        self.series_id = series_id


@pytest.fixture
def sync_harness(monkeypatch):
    import sonarr.sync.series as series_mod

    shows = [{'id': n, 'title': f'Show {n}', 'monitored': True} for n in (1, 2, 3)]
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

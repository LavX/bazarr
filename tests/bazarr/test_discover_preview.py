"""Preview and attachment use one guarded exact-result byte source."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest

import test_discover_download as fixtures

authenticated_client = fixtures.authenticated_client
providers = fixtures.providers
choices = fixtures.choices
retrieval_database = fixtures.retrieval_database
resolved_episode = fixtures.resolved_episode
post = fixtures.post

LITERAL = b'1\n00:00:01,000 --> 00:00:02,000\nHello <script>alert(1)</script>\n\n'


def preview(client, row, **kwargs):
    return client.get('/api/discover/preview', query_string={
        'result_id': row['id'], 'search_id': row['search_id'], **kwargs},
        headers={'X-API-KEY': 'discover-test-key'})


@pytest.mark.parametrize('timing', [
    '00:00:01,000 --> 00:00:02,000',
    '00:00:01,000  -->  00:00:02,000',
    '00:00:01,000 --> 00:00:02,000 X1:10 X2:20 Y1:30 Y2:40',
], ids=['canonical', 'arrow-spacing', 'position-metadata'])
@pytest.mark.parametrize('download_first', [False, True], ids=['preview-first', 'download-first'])
def test_preview_and_download_share_literal_bytes_without_database_writes(
    authenticated_client, choices, providers, retrieval_database, monkeypatch, timing, download_first,
):
    import sqlalchemy as sa
    from app import database as db
    engine, session = retrieval_database
    before = {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables}
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][1]
    content = f'1\n{timing}\nHello <script>alert(1)</script>\n\n'.encode()
    calls, sql = [], []

    def download(subtitle):
        calls.append(subtitle.worker_id)
        subtitle.content = content

    def statement(*args):
        sql.append(args[2])

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', download)
    sa.event.listen(engine, 'before_cursor_execute', statement)
    try:
        if download_first:
            attachment = fixtures.get(authenticated_client, row)
            assert attachment.status_code == 200
            assert attachment.data == content
        response = preview(authenticated_client, row)
        assert response.status_code == 200
        assert response.json['cues'] == [{'start_ms': 1000, 'end_ms': 2000, 'text': 'Hello <script>alert(1)</script>'}]
        assert response.json['total_cues'] == 1
        assert response.json['truncated'] is False
        assert response.json['result_id'] == row['id']
        assert response.json['search_id'] == row['search_id']
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        attachment = fixtures.get(authenticated_client, row)
        assert attachment.status_code == 200
        assert attachment.data == content
        assert calls == ['forced'] and sql == []
    finally:
        sa.event.remove(engine, 'before_cursor_execute', statement)
    assert {table.name: session.execute(sa.select(table)).all() for table in db.Base.metadata.sorted_tables} == before


@pytest.mark.parametrize('timing', [
    '00:00:01,000  -->  00:00:01,000',
    '00:00:02,000 --> 00:00:01,000 X1:10 X2:20 Y1:30 Y2:40',
    '00:60:01,000 --> 00:60:02,000 X1:10 X2:20 Y1:30 Y2:40',
    '00:00:01,000 --> 00:00:02,000 X1:invalid X2:20 Y1:30 Y2:40',
], ids=['zero-duration', 'reversed-positioned', 'invalid-minute', 'invalid-position'])
def test_invalid_srt_timing_syntax_remains_rejected_by_both_consumers(
    authenticated_client, choices, providers, retrieval_database, monkeypatch, timing,
):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]

    def download(subtitle):
        subtitle.content = f'1\n{timing}\nHello <i>world</i>\n\n'.encode()

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', download)
    response = preview(authenticated_client, row)
    assert response.status_code == 502
    assert response.json['reason'] == 'preview_failed'
    assert 'cues' not in response.json and response.json['recoverable']
    attachment = fixtures.get(authenticated_client, row)
    assert attachment.status_code == 502
    assert attachment.json['reason'] == 'download_failed'
    assert 'Content-Disposition' not in attachment.headers


def test_concurrent_preview_download_coalesce(authenticated_client, choices, providers, monkeypatch):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    from discover import download as shared
    entered, release, waiting = Event(), Event(), Event()
    original_wait = Event.wait

    def wait(event, timeout=None):
        if current_thread().name == 'coalesced_1' and any(
            flight.ready is event for flight in list(shared._inflight.values())
        ):
            waiting.set()
        return original_wait(event, timeout)
    calls = []

    def download(subtitle):
        calls.append(subtitle.worker_id)
        entered.set()
        assert release.wait(3)
        subtitle.content = LITERAL

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', download)
    monkeypatch.setattr(Event, "wait", wait)
    with ThreadPoolExecutor(2, thread_name_prefix="coalesced") as executor:
        first = executor.submit(preview, authenticated_client.application.test_client(), row)
        try:
            assert entered.wait(2), 'Preview must enter the guarded provider fetch'
            second = executor.submit(fixtures.get, authenticated_client.application.test_client(), row)
            assert waiting.wait(2), "Download must join the pending preview fetch"
        finally:
            release.set()
        assert first.result().status_code == 200
        assert second.result().data == LITERAL
    assert calls == ['full']


@pytest.mark.parametrize('transport', ['header', 'query', 'form', 'wrong-header-query', 'wrong-query-form'])
def test_validated_transports_share_scope(authenticated_client, choices, transport):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    assert preview(authenticated_client, row).status_code == 200
    params = {'result_id': row['id'], 'search_id': row['search_id']}
    headers, data = {}, {}
    if transport == 'header':
        headers['X-API-KEY'] = 'discover-test-key'
    elif transport in ('query', 'wrong-header-query'):
        params['apikey'] = 'discover-test-key'
        if transport == 'wrong-header-query':
            headers['X-API-KEY'] = 'wrong'
    else:
        data['apikey'] = 'discover-test-key'
        if transport == 'wrong-query-form':
            params['apikey'] = 'wrong'
    response = authenticated_client.get('/api/discover/download', query_string=params, headers=headers, data=data)
    assert response.status_code == 200 and choices[1] == ['full']


@pytest.mark.parametrize('invalidated', ['expiry', 'restart', 'wrong-search', 'rotation'])
def test_cached_bytes_never_bypass_authority_or_handle(authenticated_client, choices, monkeypatch, invalidated):
    from app.config import settings
    from compat import file_id_store
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    assert preview(authenticated_client, row).status_code == 200
    if invalidated == 'expiry':
        store = file_id_store.get_store()
        key = int(row['id'].split('.')[1])
        _, record = store._store[key]
        store._store[key] = (0, record)
    elif invalidated == 'restart':
        file_id_store.reset_store()
    elif invalidated == 'wrong-search':
        row = {**row, 'search_id': 'wrong'}
    else:
        monkeypatch.setattr(settings.auth, 'apikey', 'rotated-fixture-key')
    response = preview(authenticated_client, row)
    assert response.status_code == (401 if invalidated == 'rotation' else 410)
    assert choices[1] == ['full']
    if invalidated == 'rotation':
        response = authenticated_client.get('/api/discover/download', query_string={
            'result_id': row['id'], 'search_id': row['search_id']}, headers={'X-API-KEY': 'rotated-fixture-key'})
        assert response.status_code == 200 and choices[1] == ['full', 'full']


@pytest.mark.parametrize('during', ['rotation', 'expiry'])
def test_change_during_fetch_rejects_delivery(authenticated_client, choices, providers, monkeypatch, during):
    from app.config import settings
    from compat import file_id_store
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]

    def download(subtitle):
        subtitle.content = LITERAL
        if during == 'rotation':
            monkeypatch.setattr(settings.auth, 'apikey', 'rotated-fixture-key')
        else:
            file_id_store.reset_store()

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', download)
    response = preview(authenticated_client, row)
    assert response.status_code == (401 if during == 'rotation' else 410)
    assert 'cues' not in response.json


@pytest.mark.parametrize('content', [b'', b'<html>Error</html>', b'not subtitles', b'1\n00:00:02,000 --> 00:00:01,000\nWrong timing\n\n'])
def test_malformed_preview_has_recovery_not_fabricated_cues(authenticated_client, choices, providers, monkeypatch, content):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]

    def download(subtitle):
        subtitle.content = content

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', download)
    response = preview(authenticated_client, row)
    assert response.status_code == 502
    assert response.json['reason'] == 'preview_failed'
    assert 'cues' not in response.json and response.json['recoverable']


def test_normalized_vtt_preview_matches_download(authenticated_client, choices, providers, monkeypatch):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    calls = []

    def download(subtitle):
        calls.append(1)
        subtitle.content = b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n\n'

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', download)
    data = preview(authenticated_client, row)
    assert data.status_code == 200
    assert data.json['cues'][0] == {'start_ms': 1000, 'end_ms': 2000, 'text': 'Hello'}
    assert fixtures.get(authenticated_client, row).data == b'1\n00:00:01,000 --> 00:00:02,000\nHello\n\n'
    assert calls == [1]


@pytest.mark.parametrize('kind', ['cues', 'characters'])
def test_preview_bounds_do_not_truncate_attachment(authenticated_client, choices, providers, monkeypatch, kind):
    from discover import download
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    content = (b'1\n00:00:01,000 --> 00:00:02,000\n' + b'x' * (download.PREVIEW_MAX_CHARACTERS + 1) + b'\n\n'
               if kind == 'characters' else b''.join(
                   f'{i + 1}\n00:00:01,000 --> 00:00:02,000\nCue {i}\n\n'.encode()
                   for i in range(download.PREVIEW_MAX_CUES + 1)))

    def fetch(subtitle):
        subtitle.content = content

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', fetch)
    response = preview(authenticated_client, row)
    assert response.status_code == 200
    assert len(response.json['cues']) <= download.PREVIEW_MAX_CUES
    assert sum(len(cue['text']) for cue in response.json['cues']) <= download.PREVIEW_MAX_CHARACTERS
    assert response.json['truncated']
    assert fixtures.get(authenticated_client, row).data == content


@pytest.mark.parametrize('bound', ['entries', 'bytes', 'ttl'])
def test_bounded_cache_eviction_refetches_exact_result(authenticated_client, choices, monkeypatch, bound):
    import time
    from discover import download
    # Isolate limits from other tests without resetting the application handle store.
    with download._lock:
        download._cache.clear()
        download._cache_bytes = 0
    rows = post(authenticated_client, fixtures.CONTEXT).json['results']
    if bound == 'entries':
        monkeypatch.setattr(download, 'CACHE_MAX_ENTRIES', 1)
    elif bound == 'bytes':
        monkeypatch.setattr(download, 'CACHE_MAX_BYTES', max(len(fixtures.FULL_SRT), len(fixtures.FORCED_SRT)))
    else:
        monkeypatch.setattr(download, 'CACHE_TTL_SECONDS', 0.01)
    assert preview(authenticated_client, rows[0]).status_code == 200
    if bound == 'ttl':
        time.sleep(0.02)
    else:
        assert preview(authenticated_client, rows[1]).status_code == 200
    assert fixtures.get(authenticated_client, rows[0]).data == fixtures.FULL_SRT
    assert choices[1].count('full') == 2
    with download._lock:
        assert len(download._cache) <= download.CACHE_MAX_ENTRIES
        assert download._cache_bytes == sum(len(entry[1].content) for entry in download._cache.values())
        assert download._cache_bytes <= download.CACHE_MAX_BYTES


def test_fetch_waits_and_slots_are_bounded_without_late_cache_publication(authenticated_client, choices, providers, monkeypatch):
    import time
    from discover import download
    rows = post(authenticated_client, fixtures.CONTEXT).json['results']
    entered, release = Event(), Event()
    calls = []

    def fetch(subtitle):
        calls.append(subtitle.worker_id)
        entered.set()
        assert release.wait(3)
        subtitle.content = LITERAL

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', fetch)
    monkeypatch.setattr(download, 'FETCH_WAIT_SECONDS', 0.08)
    monkeypatch.setattr(download, 'FETCH_MAX_CONCURRENT', 1)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(preview, authenticated_client.application.test_client(), rows[0])
        try:
            assert entered.wait(2)
            assert preview(authenticated_client, rows[1]).status_code == 502
            started = time.monotonic()
            second = executor.submit(fixtures.get, authenticated_client.application.test_client(), rows[0])
            assert first.result(timeout=1).status_code == 502
            assert second.result(timeout=1).status_code == 502
            assert time.monotonic() - started < 1
        finally:
            release.set()
    deadline = time.monotonic() + 2
    while download._inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not download._inflight
    assert calls == ['full']
    assert not any(key[1] == rows[0]['id'] for key in download._cache)
    assert preview(authenticated_client, rows[0]).status_code == 200
    assert calls == ['full', 'full']


@pytest.mark.parametrize('during', ['rotation', 'expiry'])
def test_coalesced_waiters_recheck_authority_and_exact_expiry(authenticated_client, choices, providers, monkeypatch, during):
    from app.config import settings
    from compat import file_id_store
    from discover import download
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    entered, release, waiting = Event(), Event(), Event()
    original_wait = Event.wait

    def wait(event, timeout=None):
        if current_thread().name == "coalesced_1" and any(flight.ready is event for flight in list(download._inflight.values())):
            waiting.set()
        return original_wait(event, timeout)

    def fetch(subtitle):
        entered.set()
        assert release.wait(3)
        subtitle.content = LITERAL

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', fetch)
    with ThreadPoolExecutor(2, thread_name_prefix="coalesced") as executor:
        first = executor.submit(preview, authenticated_client.application.test_client(), row)
        try:
            assert entered.wait(2)
            monkeypatch.setattr(Event, 'wait', wait)
            second = executor.submit(fixtures.get, authenticated_client.application.test_client(), row)
            assert waiting.wait(2)
            if during == 'rotation':
                monkeypatch.setattr(settings.auth, 'apikey', 'rotation-wait-fixture')
            else:
                file_id_store.reset_store()
        finally:
            release.set()
        expected = 401 if during == 'rotation' else 410
        assert first.result().status_code == expected
        assert second.result().status_code == expected
    assert not any(key[1] == row['id'] for key in download._cache)


def test_distinct_handles_contexts_and_providers_never_share_bytes(authenticated_client, choices, providers, monkeypatch):
    from discover.handles import mint_result
    from provider_hub.protocol import candidate_from_worker
    other = candidate_from_worker('discover_second', {
        'id': 'full', 'language': {'alpha3': 'eng', 'forced': False},
        'release_info': 'Other', 'provider_payload': {},
    })
    providers.add('discover_second', [other])
    rows = post(authenticated_client, fixtures.CONTEXT).json['results']
    from subliminal_patch.extensions import provider_registry

    def fetch(self, subtitle):
        subtitle.content = LITERAL

    monkeypatch.setattr(provider_registry['discover_second'], 'download_subtitle', fetch)
    first = next(row for row in rows if row['provider'] == 'discover_download' and row['scope'] == 'full')
    second = next(row for row in rows if row['provider'] == 'discover_second')
    assert preview(authenticated_client, first).json['cues'][0]['text'] == 'Full dialogue'
    assert preview(authenticated_client, second).json['cues'][0]['text'].startswith('Hello')
    # Even the identical mutable provider candidate under a different full context is separate.
    context = {**fixtures.CONTEXT, 'episode': 2}
    handle, _ = mint_result(choices[0][0], context, 'another-context', 60)
    third = {'id': handle, 'search_id': 'another-context'}
    assert preview(authenticated_client, third).status_code == 200
    assert choices[1] == ['full', 'full']
    assert fixtures.get(authenticated_client, first).data == fixtures.FULL_SRT
    assert choices[1] == ['full', 'full']


def test_metadata_admission_expiry_does_not_retire_result(authenticated_client, choices, resolved_episode, monkeypatch):
    from discover import metadata
    row = post(authenticated_client, resolved_episode.context).json['results'][0]
    assert preview(authenticated_client, row).status_code == 200

    def forbidden(*args, **kwargs):
        pytest.fail('Already admitted result consulted metadata admission')

    monkeypatch.setattr(metadata, 'episode_details', forbidden)
    assert fixtures.get(authenticated_client, row).data == fixtures.FULL_SRT
    assert choices[1] == ['full']


def test_rotation_during_cache_admission_starts_no_obsolete_provider_work(authenticated_client, choices, monkeypatch):
    from app.config import settings
    from discover import download
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    captured = Event()
    original = download._record

    def record(*args):
        result = original(*args)
        captured.set()
        return result

    monkeypatch.setattr(download, '_record', record)
    with ThreadPoolExecutor(1) as executor:
        download._lock.acquire()
        try:
            future = executor.submit(preview, authenticated_client.application.test_client(), row)
            assert captured.wait(2)
            monkeypatch.setattr(settings.auth, 'apikey', 'admission-rotation-fixture')
        finally:
            download._lock.release()
        assert future.result().status_code == 401
    assert choices[1] == []


def test_rotation_while_capturing_validated_transport_never_adopts_new_key(authenticated_client, choices, monkeypatch):
    import importlib
    from app.config import settings
    route = importlib.import_module('api.discover.download')
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    compare = route._safe_apikey_compare

    def rotate(provided, expected):
        result = compare(provided, expected)
        if result:
            monkeypatch.setattr(settings.auth, 'apikey', 'capture-rotation-fixture')
        return result

    monkeypatch.setattr(route, '_safe_apikey_compare', rotate)
    assert preview(authenticated_client, row).status_code == 401
    assert choices[1] == []


def test_cache_lock_admission_wait_is_bounded(authenticated_client, choices, monkeypatch):
    import time
    from discover import download
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    monkeypatch.setattr(download, 'FETCH_WAIT_SECONDS', 0.05)
    with ThreadPoolExecutor(1) as executor:
        download._lock.acquire()
        try:
            started = time.monotonic()
            future = executor.submit(preview, authenticated_client.application.test_client(), row)
            assert future.result(timeout=1).status_code == 502
            assert time.monotonic() - started < 1
        finally:
            download._lock.release()
    assert choices[1] == []


@pytest.mark.parametrize('transport', ['missing', 'wrong', 'cookie'])
@pytest.mark.parametrize('endpoint', ['preview', 'download'])
def test_preview_authentication_rejects_before_fetch(authenticated_client, choices, transport, endpoint):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    if transport == 'cookie':
        authenticated_client.set_cookie('session', 'logged-in-fixture')
    response = authenticated_client.get('/api/discover/' + endpoint, query_string={
        'result_id': row['id'], 'search_id': row['search_id']},
        headers={'X-API-KEY': 'wrong'} if transport == 'wrong' else {})
    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert choices[1] == []


@pytest.mark.parametrize('missing', ['result_id', 'search_id', 'duplicate'])
def test_preview_rejects_missing_or_duplicate_exact_identity(authenticated_client, choices, missing):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    params = {'result_id': row['id'], 'search_id': row['search_id']}
    if missing == 'duplicate':
        params['search_id'] = [row['search_id'], row['search_id']]
    else:
        params.pop(missing)
    response = authenticated_client.get('/api/discover/preview', query_string=params,
                                        headers={'X-API-KEY': 'discover-test-key'})
    assert response.status_code == 410 and choices[1] == []


@pytest.mark.parametrize('post_redirect', [False, True])
def test_preview_preserves_shared_outbound_guards(authenticated_client, choices, providers, monkeypatch, post_redirect):
    from compat import service
    from utilities.url_guard import UnsafeURLError
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    choices[0][0].download_link = 'https://safe.example/sub.srt' if post_redirect else 'HTTP://127.0.0.1/sub.srt'
    guards = []

    def reject(url):
        guards.append(url)
        raise UnsafeURLError('fixture')

    if post_redirect:
        monkeypatch.setattr(service, 'resolve_safe_url', lambda url: url)
        original = providers.pool['discover_download'].download_subtitle

        def redirected(subtitle):
            original(subtitle)
            subtitle.download_link = 'http://127.0.0.1/redirect.srt'

        monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', redirected)
        monkeypatch.setattr(service, 'assert_safe_outbound', reject)
    else:
        monkeypatch.setattr(service, 'resolve_safe_url', reject)
    assert preview(authenticated_client, row).status_code == 502
    assert len(guards) == 1
    assert choices[1] == (['full'] if post_redirect else [])


def test_download_then_preview_uses_one_guarded_fetch(authenticated_client, choices):
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][1]
    assert fixtures.get(authenticated_client, row).data == fixtures.FORCED_SRT
    response = preview(authenticated_client, row)
    assert response.status_code == 200
    assert response.json['cues'][0]['text'] == 'Forced translation'
    assert choices[1] == ['forced']


def test_oversized_provider_content_is_rejected_without_caching(authenticated_client, choices, providers, monkeypatch):
    from discover import download
    row = post(authenticated_client, fixtures.CONTEXT).json['results'][0]
    monkeypatch.setattr(download, 'SUBTITLE_MAX_BYTES', len(LITERAL) - 1)

    def fetch(subtitle):
        subtitle.content = LITERAL

    monkeypatch.setattr(providers.pool['discover_download'], 'download_subtitle', fetch)
    assert preview(authenticated_client, row).status_code == 502
    assert not any(key[1] == row['id'] for key in download._cache)

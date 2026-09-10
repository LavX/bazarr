from dataclasses import replace
from threading import Event, Thread
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


def native_settings():
    return SimpleNamespace(
        general=SimpleNamespace(use_emby=True, use_silo=True),
        **{server: SimpleNamespace(url=f"http://{server}.example", apikey="synthetic-key", verify_ssl=True,
                                  path_mappings=[{"local_path": "/movies", "remote_path": "/media",
                                                  "library_id": "7"}]) for server in ("emby", "silo")})


IDS = {'emby': '01f0e1bc-68fe-4013-84b1-d4becad3e8fb', 'silo': '72d8b1db-9fe4-4d62-9678-f472b322ed78'}


def native_snapshots(settings):
    from media_servers.instances import ConnectionSnapshot
    return [ConnectionSnapshot(IDS[kind], kind, kind.capitalize(), True,
                               getattr(settings.general, 'use_' + kind),
                               getattr(settings, kind).url, getattr(settings, kind).apikey,
                               getattr(settings, kind).verify_ssl,
                               tuple(tuple(sorted(row.items())) for row in getattr(settings, kind).path_mappings))
            for kind in ('emby', 'silo')]


def apply_settings(configuration, settings):
    configuration.publish_masters(settings)
    for snapshot in native_snapshots(settings):
        current = configuration.read(snapshot.id)[1]
        if replace(snapshot, revision=current.revision) != current:
            configuration.publish(snapshot)


@pytest.fixture
def dispatch():
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    configuration = NativeConfiguration(native_settings(), snapshots=native_snapshots(native_settings()))
    calls = {"emby": [], "silo": []}
    # Emby resolves the item itself, so the media type it was asked for is the
    # only evidence that an episode publication actually reached the client.
    emby_items = []
    started, release = Event(), Event()
    failures = set()
    failed_paths = set()
    libraries = [{"id": "7", "type": "movies", "paths": ["/media"]}]

    class Client:
        def __init__(self, server, snapshot):
            self.server = server
            self.snapshot = snapshot

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get_libraries(self):
            return libraries

        def refresh_item(self, media_type, path, *, ensure_current):
            emby_items.append((media_type, path))
            return self.refresh_file(None, path, ensure_current=ensure_current)

        def refresh_file(self, library_id, path, *, ensure_current):
            from media_servers.http import MediaServerError
            calls[self.server].append((path, self.snapshot))
            if self.server == "silo" and len(calls["silo"]) == 1:
                started.set()
                assert release.wait(3)
            ensure_current()
            if self.server in failures or path in failed_paths:
                raise MediaServerError("scan_incomplete")
            return {"status": "confirmed" if self.server == "silo" else "requested"}

    dispatcher = RefreshDispatcher(configuration, client_factory=Client)
    yield SimpleNamespace(dispatcher=dispatcher, config=configuration, calls=calls, started=started,
                          release=release, failures=failures, libraries=libraries, failed_paths=failed_paths,
                          emby_items=emby_items)
    release.set()
    assert dispatcher.wait_idle(3)


def movie_event(**overrides):
    from media_servers.events import SubtitleMutation
    return replace(SubtitleMutation("movie", "/movies/A.mkv", "/movies/A.en.srt", "upload", 2), **overrides)


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_second_write_requires_followup_and_servers_are_independent(dispatch, media_type):
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.dispatcher.notify(movie_event(media_type=media_type))
    # The first worker in the process pays the one-time subsync engine import.
    assert dispatch.started.wait(10)
    assert dispatch.dispatcher.wait_idle(3, server=IDS["emby"])
    assert dispatch.emby_items == [(media_type, "/media/A.mkv")]
    dispatch.dispatcher.notify(movie_event(media_type=media_type))
    dispatch.release.set()
    assert dispatch.dispatcher.wait_idle(3)
    assert len(dispatch.calls["silo"]) == 2
    assert dispatch.dispatcher.status(IDS["silo"]) == {"pending": 0, "state": "confirmed", "error_code": None}


def test_failed_latest_target_retained_until_explicit_retry(dispatch):
    dispatch.failures.add("silo")
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.status(IDS["silo"]) == {
        "pending": 1, "state": "unconfirmed", "error_code": "scan_incomplete"}
    assert len(dispatch.calls["silo"]) == 1
    dispatch.failures.clear()
    assert dispatch.dispatcher.retry(IDS["silo"]) == 1
    assert dispatch.dispatcher.wait_idle(3)
    assert len(dispatch.calls["silo"]) == 2
    assert dispatch.dispatcher.status(IDS["silo"])["pending"] == 0


def test_overflow_is_sticky_and_preserves_existing_targets(dispatch):
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.started.wait(3)
    for number in range(1, 140):
        dispatch.dispatcher.notify(movie_event(video_path=f"/movies/{number}.mkv"))
    assert dispatch.dispatcher.status(IDS["silo"])["pending"] == 128
    assert dispatch.dispatcher.status(IDS["silo"])["error_code"] == "queue_overflow"
    dispatch.release.set()
    assert dispatch.dispatcher.wait_idle(3)
    assert len(dispatch.calls["silo"]) == 128
    assert dispatch.dispatcher.status(IDS["silo"])["error_code"] == "queue_overflow"


def test_configuration_change_invalidates_inflight_and_retry_uses_complete_snapshot(dispatch):
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.started.wait(3)
    new_settings = native_settings()
    new_settings.silo.url = "http://new.example"
    new_settings.silo.apikey = "synthetic-new-key"
    new_settings.silo.verify_ssl = False
    apply_settings(dispatch.config, new_settings)
    dispatch.release.set()
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.status(IDS["silo"])["error_code"] == "configuration_changed"
    assert dispatch.dispatcher.retry(IDS["silo"]) == 1
    assert dispatch.dispatcher.wait_idle(3)
    snapshot = dispatch.calls["silo"][-1][1]
    assert (snapshot.url, snapshot.apikey, snapshot.verify_ssl) == (
        "http://new.example", "synthetic-new-key", False)


def test_disabled_connection_retains_pending_without_new_requests(dispatch):
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.started.wait(3)
    settings = native_settings()
    settings.general.use_silo = False
    apply_settings(dispatch.config, settings)
    dispatch.release.set()
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.retry(IDS["silo"]) == 0
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.status(IDS["silo"])["pending"] == 1
    assert len(dispatch.calls["silo"]) == 1


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('operation', ['delete', 'sync', 'translate', 'combine', 'edit'])
def test_emby_only_consumes_download_and_upload(dispatch, operation, media_type):
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type=media_type, operation=operation))
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.calls["emby"] == []
    assert dispatch.emby_items == []


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('operation', ['download', 'upload'])
def test_emby_accepts_each_supported_publication_for_movies_and_episodes(dispatch, operation, media_type):
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type=media_type, operation=operation))
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.emby_items == [(media_type, '/media/A.mkv')]
    assert dispatch.dispatcher.status(IDS['emby']) == {'pending': 0, 'state': 'requested', 'error_code': None}


@pytest.mark.parametrize(("event", "code"), [
    (movie_event(subtitle_path="/elsewhere/A.en.srt"), "sidecar_unsupported"),
])
def test_invalid_silo_target_retained_without_request(dispatch, event, code):
    dispatch.release.set()
    dispatch.dispatcher.notify(event)
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.calls["silo"] == []
    assert dispatch.dispatcher.status(IDS["silo"])["error_code"] == code


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('timing', ['already_unconfirmed', 'coalesced'])
def test_supported_scan_keeps_unsupported_publication_pending(dispatch, media_type, timing):
    dispatcher = dispatch.dispatcher
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.release.set()
    unsupported = movie_event(media_type=media_type, subtitle_path='/movies/subtitles/A.hu.srt')
    supported = movie_event(media_type=media_type)
    warning = {'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}

    if timing == 'already_unconfirmed':
        dispatcher.notify(unsupported)
        assert dispatcher.wait_idle(10)
        assert dispatcher.status(IDS['silo']) == warning
        dispatcher.notify(supported)
    else:
        # Hold selection until both actual publication callbacks have returned.
        with dispatcher.condition:
            dispatcher.notify(unsupported)
            dispatcher.notify(supported)
    assert dispatcher.wait_idle(10)
    assert [path for path, _snapshot in dispatch.calls['silo']] == ['/media/A.mkv']
    assert dispatcher.status(IDS['silo']) == warning

    assert dispatcher.retry(IDS['silo']) == 1
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == 1
    assert dispatcher.status(IDS['silo']) == warning

    dispatcher.notify(replace(supported, subtitle_path='/movies/A.fr.srt'))
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == 2
    assert dispatcher.status(IDS['silo']) == warning


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_coalesced_unsupported_publication_keeps_queued_supported_work(dispatch, media_type):
    dispatcher = dispatch.dispatcher
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.release.set()
    with dispatcher.condition:
        dispatcher.notify(movie_event(media_type=media_type))
        dispatcher.notify(movie_event(media_type=media_type, subtitle_path='/movies/subtitles/A.hu.srt'))
    assert dispatcher.wait_idle(10)
    assert [path for path, _snapshot in dispatch.calls['silo']] == ['/media/A.mkv']
    assert dispatcher.status(IDS['silo']) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('later_supported', [False, True])
def test_unsupported_warning_survives_inflight_supported_generations(dispatch, media_type, later_supported):
    dispatcher = dispatch.dispatcher
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatcher.notify(movie_event(media_type=media_type))
    assert dispatch.started.wait(10)
    dispatcher.notify(movie_event(media_type=media_type, subtitle_path='/movies/subtitles/A.hu.srt'))
    if later_supported:
        dispatcher.notify(movie_event(media_type=media_type, subtitle_path='/movies/A.fr.srt'))
    assert dispatcher.status(IDS['silo'])['error_code'] == 'sidecar_unsupported'
    dispatch.release.set()
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == (2 if later_supported else 1)
    assert dispatcher.status(IDS['silo']) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_unsupported_warning_keeps_failed_supported_work_retryable(dispatch, media_type):
    dispatcher = dispatch.dispatcher
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.failures.add('silo')
    dispatcher.notify(movie_event(media_type=media_type))
    assert dispatch.started.wait(10)
    dispatcher.notify(movie_event(media_type=media_type, subtitle_path='/movies/subtitles/A.hu.srt'))
    dispatch.release.set()
    assert dispatcher.wait_idle(10)
    assert dispatcher.status(IDS['silo']) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'scan_incomplete'}
    dispatch.failures.clear()
    assert dispatcher.retry(IDS['silo']) == 1
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == 2
    warning = {'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}
    assert dispatcher.status(IDS['silo']) == warning
    assert dispatcher.retry(IDS['silo']) == 1
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == 2
    assert dispatcher.status(IDS['silo']) == warning


def test_unsupported_warning_capacity_counts_distinct_video_and_arr_owner_targets(dispatch):
    dispatcher = dispatch.dispatcher
    dispatch.release.set()
    with dispatcher.condition:
        for owner in range(128):
            dispatcher.notify(movie_event(arr_instance_id=owner, subtitle_path='/movies/subtitles/A.hu.srt'))
        # Many subtitle paths on one target must neither consume extra capacity
        # nor replace the warning associated with another source owner.
        for number in range(200):
            dispatcher.notify(movie_event(arr_instance_id=0, subtitle_path=f'/movies/subtitles/A.{number}.srt'))
    assert dispatcher.wait_idle(10)
    assert dispatcher.status(IDS['silo']) == {
        'pending': 128, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}
    assert dispatch.calls['silo'] == []
    with dispatcher.condition:
        for owner in range(128):
            dispatcher.notify(movie_event(arr_instance_id=owner))
        dispatcher.notify(movie_event(arr_instance_id=128))
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == 128
    assert dispatcher.status(IDS['silo']) == {
        'pending': 128, 'state': 'unconfirmed', 'error_code': 'queue_overflow'}
    assert dispatcher.retry(IDS['silo']) == 128
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls['silo']) == 128
    assert dispatcher.status(IDS['silo'])['pending'] == 128
    assert dispatcher.status(IDS['silo'])['error_code'] == 'queue_overflow'


def test_read_only_status_and_invalid_selection_never_start_worker(dispatch):
    from media_servers.http import MediaServerError
    assert dispatch.dispatcher.status(IDS["silo"]) == {"pending": 0, "state": "idle", "error_code": None}
    assert dispatch.dispatcher.retry(IDS["silo"]) == 0
    assert dispatch.dispatcher.worker_count == 0
    with pytest.raises(MediaServerError, match="not_found"):
        dispatch.dispatcher.status("jellyfin")
    with pytest.raises(MediaServerError, match="not_found"):
        dispatch.dispatcher.retry("jellyfin")


@pytest.mark.parametrize('operation', ['download', 'upload', 'delete', 'sync', 'translate', 'combine', 'edit'])
@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_silo_accepts_each_supported_publication_for_movies_and_episodes(dispatch, operation, media_type):
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type=media_type, operation=operation))
    assert dispatch.dispatcher.wait_idle(3)
    assert len(dispatch.calls['silo']) == 1
    assert dispatch.dispatcher.status(IDS['silo'])['state'] == 'confirmed'


@pytest.mark.parametrize('library', [
    {'id': '7', 'type': 'movies', 'paths': ['/media-other']},
    {'id': '8', 'type': 'movies', 'paths': ['/media']},
    {'id': '7', 'type': 'series', 'paths': ['/media']},
])
def test_silo_requires_matching_library_type_and_root(dispatch, library):
    dispatch.libraries[:] = [library]
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.calls['silo'] == []
    assert dispatch.dispatcher.status(IDS['silo'])['error_code'] == 'library_invalid'


def test_later_success_does_not_hide_retained_failure(dispatch):
    dispatch.failed_paths.add('/media/A.mkv')
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.started.wait(3)
    dispatch.dispatcher.notify(movie_event(video_path='/movies/B.mkv', subtitle_path='/movies/B.en.srt'))
    dispatch.release.set()
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.status(IDS['silo']) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'scan_incomplete'}


def test_saved_configuration_does_not_inherit_old_confirmation(dispatch):
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.status(IDS['silo'])['state'] == 'confirmed'
    settings = native_settings()
    settings.silo.url = 'http://different.example'
    apply_settings(dispatch.config, settings)
    assert dispatch.dispatcher.status(IDS['silo']) == {'pending': 0, 'state': 'idle', 'error_code': None}


def test_remote_refresh_waits_for_local_publication_cleanup(dispatch, tmp_path, monkeypatch):
    from subtitles.tools import subsync_engines
    from threading import current_thread
    settings = native_settings()
    settings.general.use_emby = False
    settings.silo.path_mappings[0]['local_path'] = str(tmp_path)
    apply_settings(dispatch.config, settings)
    event = movie_event(video_path=str(tmp_path / 'Video.mkv'), subtitle_path=str(tmp_path / 'Video.en.srt'))
    cleanup_started, worker_waiting, finish_cleanup = Event(), Event(), Event()
    original_locks = subsync_engines.subtitle_write_locks

    @contextmanager
    def observed_locks(*args):
        if current_thread().name == 'silo-subtitle-refresh':
            worker_waiting.set()
        with original_locks(*args) as state:
            yield state

    monkeypatch.setattr(subsync_engines, 'subtitle_write_locks', observed_locks)

    def cleanup():
        cleanup_started.set()
        assert finish_cleanup.wait(10)

    def publish():
        with subsync_engines.staged_subtitle_write(
                event.video_path, event.subtitle_path, on_publish=lambda path: dispatch.dispatcher.notify(event),
                after_publish=cleanup) as temporary:
            from pathlib import Path
            Path(temporary).write_text('Published')

    writer = Thread(target=publish)
    writer.start()
    try:
        assert cleanup_started.wait(3)
        assert worker_waiting.wait(3)
        assert dispatch.calls['silo'] == []
    finally:
        finish_cleanup.set()
        dispatch.release.set()
        writer.join(3)
    assert dispatch.dispatcher.wait_idle(3)
    assert len(dispatch.calls['silo']) == 1


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_incomplete_connection_still_excludes_disjoint_publications(kind):
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    settings = native_settings()
    snapshot = next(item for item in native_snapshots(settings) if item.kind == kind)
    snapshot = replace(snapshot, url='', configuration_error='invalid_url')
    configuration = NativeConfiguration(settings, snapshots=[snapshot])
    dispatcher = RefreshDispatcher(configuration, client_factory=lambda *args: pytest.fail('invalid connection requested'))
    dispatcher.notify(movie_event(video_path='/unrelated/A.mkv', subtitle_path='/unrelated/A.en.srt'))
    assert dispatcher.wait_idle(3)
    assert dispatcher.status(snapshot.id) == {'pending': 0, 'state': 'unconfirmed', 'error_code': 'invalid_url'}
    assert dispatcher.worker_count == 0
    dispatcher.notify(movie_event())
    assert dispatcher.wait_idle(3)
    assert dispatcher.status(snapshot.id) == {'pending': 1, 'state': 'unconfirmed', 'error_code': 'invalid_url'}

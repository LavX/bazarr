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
    from media_servers import resolution
    from media_servers.dispatcher import NativeConfiguration, RefreshDispatcher
    from media_servers.resolution import MediaMetadata
    configuration = NativeConfiguration(native_settings(), snapshots=native_snapshots(native_settings()))
    calls = {"emby": [], "silo": []}
    # Emby resolves the item itself, so the media type it was asked for is the
    # only evidence that an episode publication actually reached the client.
    emby_items = []
    # Every rung the dispatcher climbed, in order, per destination.
    rungs = []
    library_calls = []
    identifier_calls = []
    # Identifier rungs miss by default, which is what a server that has not
    # matched the item does; the library rung resolves, as a real one would.
    resolves = {resolution.LIBRARY}
    metadata = MediaMetadata(imdb_id="tt0017136", tmdb_id="19", tvdb_id=78874,
                             title="Metropolis", year=1927, season=1, episode=2)
    metadata_box = [metadata]
    started, release = Event(), Event()
    failures = set()
    failed_paths = set()
    missing_paths = set()
    # Non-empty means the clients raise from another module generation.
    generation = []
    # Every refusal Silo folds into request_rejected that is not the documented
    # 400: a refused event-stream handshake, a conflict, a rate limit.
    rejected_paths = set()
    libraries = [{"id": "7", "type": "movies", "paths": ["/media"]}]

    def server_error(code):
        from media_servers.http import MediaServerError
        return (generation[0] if generation else MediaServerError)(code)

    class Client:
        def __init__(self, server, snapshot):
            self.server = server
            self.snapshot = snapshot
            self.REFRESH_STEPS = (resolution.CHAIN if server == "emby"
                                  else (resolution.PATH, resolution.LIBRARY))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get_libraries(self):
            return libraries

        def _rung(self, name, ensure_current):
            rungs.append((self.server, name))
            ensure_current()
            return {"status": "requested"} if name in resolves else None

        def refresh_by_provider_id(self, media_type, media_metadata, *, ensure_current):
            identifier_calls.append((self.server, resolution.PROVIDER_ID, media_type, media_metadata, None))
            return self._rung(resolution.PROVIDER_ID, ensure_current)

        def refresh_by_title_year(self, media_type, media_metadata, video_path, *, ensure_current):
            identifier_calls.append((self.server, resolution.TITLE_YEAR, media_type, media_metadata, video_path))
            return self._rung(resolution.TITLE_YEAR, ensure_current)

        def refresh_library(self, *args, ensure_current, coalesce=None):
            # The real clients decide the library themselves and skip a scan
            # this pass already asked for, answering the same either way.
            rungs.append((self.server, resolution.LIBRARY))
            ensure_current()
            if resolution.LIBRARY not in resolves:
                return None
            library = args[-1] if self.server == "silo" else "emby-library"
            if coalesce is None or not coalesce(library):
                library_calls.append((self.server, args))
            return {"status": "requested"}

        def refresh_item(self, media_type, path, *, ensure_current):
            emby_items.append((media_type, path))
            return self.refresh_file(None, path, ensure_current=ensure_current)

        def refresh_file(self, library_id, path, *, ensure_current):
            rungs.append((self.server, resolution.PATH))
            calls[self.server].append((path, self.snapshot))
            if self.server == "silo" and len(calls["silo"]) == 1:
                started.set()
                assert release.wait(3)
            ensure_current()
            if path in rejected_paths:
                raise server_error("request_rejected")
            if path in missing_paths:
                if self.server == "emby":
                    raise server_error("item_missing")
                # Silo's file rung answers a path it cannot place with nothing.
                return None
            if self.server in failures or path in failed_paths:
                raise server_error("scan_incomplete")
            return {"status": "confirmed" if self.server == "silo" else "requested"}

    dispatcher = RefreshDispatcher(configuration, client_factory=Client,
                                   metadata_factory=lambda event: metadata_box[0])
    yield SimpleNamespace(dispatcher=dispatcher, config=configuration, calls=calls, started=started,
                          release=release, failures=failures, libraries=libraries, failed_paths=failed_paths,
                          emby_items=emby_items, rungs=rungs, resolves=resolves, library_calls=library_calls,
                          identifier_calls=identifier_calls, missing_paths=missing_paths,
                          rejected_paths=rejected_paths, metadata=metadata, metadata_box=metadata_box,
                          generation=generation)
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
@pytest.mark.parametrize('operation', ['download', 'upload', 'delete', 'sync', 'translate', 'combine', 'edit'])
def test_emby_consumes_every_publication_silo_does(dispatch, operation, media_type):
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type=media_type, operation=operation))
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.emby_items == [(media_type, '/media/A.mkv')]
    assert dispatch.dispatcher.status(IDS['emby']) == {'pending': 0, 'state': 'requested', 'error_code': None}


def test_no_publication_reaches_one_destination_and_not_the_other():
    from media_servers.dispatcher import _OPERATIONS
    assert _OPERATIONS == {'download', 'upload', 'delete', 'sync', 'translate', 'combine', 'edit'}


# ------------------------------------------------------------ resolution ladder


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
@pytest.mark.parametrize('resolving', ['provider_id', 'title_year', 'path', 'library'])
def test_emby_climbs_the_ladder_and_stops_at_the_first_rung_that_resolves(dispatch, resolving, media_type):
    from media_servers import resolution
    order = list(resolution.CHAIN)
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    dispatch.resolves.clear()
    dispatch.resolves.add(resolving)
    if resolving != resolution.PATH:
        dispatch.missing_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type=media_type))
    assert dispatch.dispatcher.wait_idle(3)
    climbed = [name for server, name in dispatch.rungs if server == 'emby']
    assert climbed == order[:order.index(resolving) + 1], 'a resolved rung must never reach a later one'
    assert dispatch.dispatcher.status(IDS['emby']) == {'pending': 0, 'state': 'requested', 'error_code': None}


@pytest.mark.parametrize('media_type', ['movie', 'episode'])
def test_emby_identifier_rungs_receive_the_stored_media_identifiers(dispatch, media_type):
    from media_servers import resolution
    dispatch.libraries[0]['type'] = 'movies' if media_type == 'movie' else 'series'
    # The exact path outranks the title, so the weak rung is only reached once
    # the file itself missed.
    dispatch.missing_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type=media_type))
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.identifier_calls == [
        ('emby', resolution.PROVIDER_ID, media_type, dispatch.metadata, None),
        ('emby', resolution.TITLE_YEAR, media_type, dispatch.metadata, '/media/A.mkv'),
    ]


def test_identifiers_are_never_read_for_a_destination_that_cannot_use_them(dispatch):
    reads = []
    dispatch.dispatcher.metadata_factory = lambda event: reads.append(event) or dispatch.metadata
    dispatch.missing_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert len(dispatch.calls['silo']) == 1
    assert [call[0] for call in dispatch.identifier_calls] == ['emby', 'emby']
    assert len(reads) == 1, 'Silo declares no identifier rungs, so it must not read the database'


def test_unreadable_identifiers_skip_straight_to_the_path(dispatch):
    from media_servers import resolution
    dispatch.metadata_box[0] = None
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.identifier_calls == []
    assert [name for server, name in dispatch.rungs if server == 'emby'] == [resolution.PATH]
    assert dispatch.emby_items == [('movie', '/media/A.mkv')]


def test_a_path_emby_has_not_indexed_falls_back_to_the_library_holding_it(dispatch):
    dispatch.missing_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert ('emby', ('movie', '/media/A.mkv')) in dispatch.library_calls
    assert dispatch.dispatcher.status(IDS['emby']) == {'pending': 0, 'state': 'requested', 'error_code': None}


def test_a_file_scan_silo_refuses_falls_back_to_scanning_the_whole_library(dispatch):
    dispatch.missing_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert ('silo', ('7',)) in dispatch.library_calls
    assert dispatch.dispatcher.status(IDS['silo']) == {'pending': 0, 'state': 'requested', 'error_code': None}


def other_generation():
    """A second execution of media_servers.http, which is what test isolation
    leaves behind: the same class by name, a different object by identity."""
    import importlib.util
    from media_servers.http import MediaServerError as live
    spec = importlib.util.find_spec('media_servers.http')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MediaServerError is not live
    return module.MediaServerError


@pytest.mark.parametrize(('lever', 'expected'), [
    ('missing_paths', {'pending': 0, 'state': 'requested', 'error_code': None}),
    ('failures', {'pending': 1, 'state': 'unconfirmed', 'error_code': 'scan_incomplete'}),
])
def test_a_refusal_from_another_module_generation_keeps_its_meaning(dispatch, lever, expected):
    """The dispatcher binds MediaServerError when it is imported and builds its
    clients lazily, at the first refresh, so nothing makes the two share a
    module generation. ``except MediaServerError`` catches a class, not a
    vocabulary: once they split, an honest miss stops falling through to the
    next rung and every refusal is reported as internal_error."""
    dispatch.generation.append(other_generation())
    if lever == 'missing_paths':
        dispatch.missing_paths.add('/media/A.mkv')
    else:
        dispatch.failures.add('emby')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.status(IDS['emby']) == expected


def test_a_refused_scan_request_never_escalates_to_the_whole_library(dispatch):
    """Silo folds a refused event-stream handshake, a 409 and a 429 into the
    same request_rejected as its documented 400. Reading all of them as "this
    path is not here" turns a broken destination into a stream of library scans
    reported as success, with no signal to the user at all."""
    dispatch.rejected_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.library_calls == [], 'a refused request is not a path Silo could not place'
    assert dispatch.dispatcher.status(IDS['silo']) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'request_rejected'}


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_one_library_scan_covers_every_target_the_pass_already_held(dispatch, kind):
    """A bulk mod or a season pack against a mapping that is slightly wrong
    lands every one of its targets on the library rung, and the scan there is
    recursive over the whole library. One per video is a hundred-odd identical
    rescans of the same library."""
    dispatcher = dispatch.dispatcher
    dispatch.release.set()
    with dispatcher.condition:
        for number in range(8):
            dispatch.missing_paths.add(f'/media/{number}.mkv')
            dispatcher.notify(movie_event(video_path=f'/movies/{number}.mkv'))
    assert dispatcher.wait_idle(10)
    assert len(dispatch.calls[kind]) == 8, 'every file is still asked for on its own'
    assert [call for call in dispatch.library_calls if call[0] == kind] == [
        (kind, ('movie', '/media/0.mkv') if kind == 'emby' else ('7',))]
    assert dispatcher.status(IDS[kind]) == {'pending': 0, 'state': 'requested', 'error_code': None}


def test_a_publication_that_arrived_after_a_scan_is_not_counted_as_covered(dispatch):
    """A recursive scan covers the files that were there when it was submitted.
    A subtitle written after that gets its own, or it waits for a scan nobody
    is going to ask for."""
    dispatcher = dispatch.dispatcher
    dispatch.missing_paths.update({'/media/A.mkv', '/media/B.mkv'})
    dispatcher.notify(movie_event())
    assert dispatch.started.wait(3)
    dispatcher.notify(movie_event(video_path='/movies/B.mkv'))
    dispatch.release.set()
    assert dispatcher.wait_idle(10)
    assert [call for call in dispatch.library_calls if call[0] == 'silo'] == [('silo', ('7',))] * 2


def test_a_scan_that_ran_and_fell_short_is_a_failure_not_a_missing_file(dispatch):
    dispatch.failures.add('silo')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.library_calls == [], 'a scan that ran is not a path Silo could not find'
    assert dispatch.dispatcher.status(IDS['silo']) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'scan_incomplete'}
    assert dispatch.dispatcher.status(IDS['emby'])['state'] == 'requested'


@pytest.mark.parametrize('kind', ['emby', 'silo'])
def test_a_ladder_that_resolves_nothing_reports_the_item_as_missing(dispatch, kind):
    dispatch.resolves.clear()
    dispatch.missing_paths.add('/media/A.mkv')
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.dispatcher.status(IDS[kind]) == {
        'pending': 1, 'state': 'unconfirmed', 'error_code': 'item_missing'}


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


@pytest.mark.parametrize('drain', ['retry', 'covering_scan'])
def test_a_moved_subtitle_lets_the_retained_unsupported_warning_drain(dispatch, tmp_path, drain):
    """The warning tells the user to move the subtitle beside the video and retry.

    That move publishes nothing of its own, so the retained event is the only
    record of the target. Replaying the path it was recorded with re-raises
    sidecar_unsupported however many times the user retries, and the covering
    scan of a later eligible publication does not release it either: the
    destination can never return to idle short of deleting the instance or
    restarting Bazarr.
    """
    dispatcher = dispatch.dispatcher
    settings = native_settings()
    settings.general.use_emby = False
    settings.silo.path_mappings[0]['local_path'] = str(tmp_path)
    apply_settings(dispatch.config, settings)
    dispatch.release.set()
    video = tmp_path / 'Video.mkv'
    video.touch()
    custom = tmp_path / 'subtitles'
    custom.mkdir()
    stray = custom / 'Video.hu.srt'
    stray.write_text('Stored in the configured custom subtitle folder')
    warning = {'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}

    dispatcher.notify(movie_event(video_path=str(video), subtitle_path=str(stray)))
    assert dispatcher.wait_idle(10)
    assert dispatch.calls['silo'] == []
    assert dispatcher.status(IDS['silo']) == warning

    stray.rename(video.parent / stray.name)

    if drain == 'retry':
        assert dispatcher.retry(IDS['silo']) == 1
    else:
        dispatcher.notify(movie_event(video_path=str(video),
                                      subtitle_path=str(video.with_suffix('.en.srt'))))
    assert dispatcher.wait_idle(10)
    assert [path for path, _snapshot in dispatch.calls['silo']] == ['/media/Video.mkv'] * (
        1 if drain == 'retry' else 2)
    assert dispatcher.status(IDS['silo']) == {'pending': 0, 'state': 'confirmed', 'error_code': None}


def test_a_retained_unsupported_warning_survives_a_subtitle_that_never_moved(dispatch, tmp_path):
    """Only an actual move releases it. A file still sitting where Silo cannot
    read it keeps the warning, however many times the user retries."""
    dispatcher = dispatch.dispatcher
    settings = native_settings()
    settings.general.use_emby = False
    settings.silo.path_mappings[0]['local_path'] = str(tmp_path)
    apply_settings(dispatch.config, settings)
    dispatch.release.set()
    video = tmp_path / 'Video.mkv'
    video.touch()
    custom = tmp_path / 'subtitles'
    custom.mkdir()
    stray = custom / 'Video.hu.srt'
    stray.write_text('Stored in the configured custom subtitle folder')
    warning = {'pending': 1, 'state': 'unconfirmed', 'error_code': 'sidecar_unsupported'}

    dispatcher.notify(movie_event(video_path=str(video), subtitle_path=str(stray)))
    assert dispatcher.wait_idle(10)
    assert dispatcher.status(IDS['silo']) == warning
    for _attempt in range(2):
        assert dispatcher.retry(IDS['silo']) == 1
        assert dispatcher.wait_idle(10)
        assert dispatch.calls['silo'] == []
        assert dispatcher.status(IDS['silo']) == warning


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


def test_a_rung_that_answers_with_the_wrong_status_is_not_a_refresh(dispatch):
    dispatch.resolves.add('provider_id')
    dispatch.release.set()
    original = dispatch.dispatcher._ladder

    def confused(client, server, event, mapped, guard, coalesce):
        return [(name, lambda: {'status': 'whatever'}, expected)
                for name, _call, expected in original(client, server, event, mapped, guard, coalesce)]

    dispatch.dispatcher._ladder = confused
    dispatch.dispatcher.notify(movie_event())
    assert dispatch.dispatcher.wait_idle(3)
    for kind in ('emby', 'silo'):
        assert dispatch.dispatcher.status(IDS[kind]) == {
            'pending': 1, 'state': 'unconfirmed', 'error_code': 'invalid_response'}


@pytest.mark.parametrize('operation', ['download', 'upload', 'delete'])
def test_a_sports_publication_refreshes_the_mapped_library_only(dispatch, operation):
    """A sports write reaches the same dispatcher as a movie or episode one,
    but the only rung it can climb is the library: a sports event carries no
    identifiers and no item type a server indexes, so the ladder offers just
    the whole-library scan for the path the mapping resolves."""
    from media_servers import resolution
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type='sports', operation=operation,
                                           video_path='/movies/sports/Event.mkv',
                                           subtitle_path='/movies/sports/Event.en.srt'))
    assert dispatch.dispatcher.wait_idle(3)
    assert set(dispatch.rungs) == {('emby', resolution.LIBRARY),
                                   ('silo', resolution.LIBRARY)}
    assert dispatch.emby_items == []
    assert dispatch.identifier_calls == []
    assert set(dispatch.library_calls) == {('emby', ('sports', '/media/sports/Event.mkv')),
                                           ('silo', ('7',))}
    assert dispatch.dispatcher.status(IDS['emby'])['state'] == 'requested'
    assert dispatch.dispatcher.status(IDS['silo'])['state'] == 'requested'


def test_a_sports_publication_outside_any_mapping_refreshes_nothing(dispatch):
    """No mapping covering the sports path means no destination has a
    configured library for it, so no refresh is asked of either server."""
    dispatch.release.set()
    dispatch.dispatcher.notify(movie_event(media_type='sports', operation='download',
                                           video_path='/elsewhere/Event.mkv',
                                           subtitle_path='/elsewhere/Event.en.srt'))
    assert dispatch.dispatcher.wait_idle(3)
    assert dispatch.rungs == []
    assert dispatch.library_calls == []
    assert dispatch.calls['silo'] == []
    assert dispatch.dispatcher.worker_count == 0


def test_a_sports_publication_accepts_either_mapped_silo_library_type(dispatch):
    """Silo only knows movie and series library types, and a sports root lives
    inside one of them, so the type check must not refuse a sports event."""
    for kind in ('movies', 'series'):
        dispatch.rungs.clear()
        dispatch.library_calls.clear()
        dispatch.libraries[0]['type'] = kind
        dispatch.release.set()
        dispatch.dispatcher.notify(movie_event(media_type='sports', operation='upload',
                                               video_path='/movies/sports/Event.mkv',
                                               subtitle_path='/movies/sports/Event.en.srt'))
        assert dispatch.dispatcher.wait_idle(3)
        assert dispatch.dispatcher.status(IDS['silo'])['state'] == 'requested'

# coding=utf-8

"""Lifecycle of the subtitle editor's HLS preview encoders.

Encoders are fake processes and the idle clock is fake, so these tests run in
milliseconds and never start ffmpeg.
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

PARTIAL_PLAYLIST = (
    '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n#EXT-X-MAP:URI="init.mp4"\n'
    '#EXTINF:4.000000,\nsegment_000.m4s\n#EXTINF:4.000000,\nsegment_001.m4s\n'
)
COMPLETE_PLAYLIST = PARTIAL_PLAYLIST + '#EXT-X-ENDLIST\n'


class FakeProcess:
    """Stands in for an ffmpeg Popen: runs until signalled or finished."""

    def __init__(self, cmd, ignore_term=False):
        self.args = cmd
        self.returncode = None
        self.ignore_term = ignore_term
        self.signals = []
        self.waited = False
        self._exited = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append('TERM')
        if not self.ignore_term:
            self.finish(255)

    def kill(self):
        self.signals.append('KILL')
        self.finish(-9)

    def finish(self, returncode=0):
        if self.returncode is None:
            self.returncode = returncode
        self._exited.set()

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.waited = True
        return self.returncode

    def communicate(self):
        self._exited.wait()
        return None, b''


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def hls(monkeypatch, tmp_path):
    from api.editor import editor

    root = tmp_path / 'hls'
    root.mkdir()
    clock = FakeClock()
    spawned = []
    behaviours = {}

    def fake_popen(cmd, **_kwargs):
        # What ffmpeg writes by the time the preview first plays. The playlist
        # path is the command's last argument.
        cache_dir = os.path.dirname(cmd[-1])
        for name in ('init.mp4', 'segment_000.m4s', 'segment_001.m4s'):
            Path(cache_dir, name).write_bytes(b'data')
        Path(cmd[-1]).write_text(PARTIAL_PLAYLIST)
        process = FakeProcess(cmd, ignore_term=behaviours.get('ignore_term', False))
        spawned.append(process)
        return process

    real_ensure_reaper = getattr(editor, '_ensure_hls_reaper_started', None)
    monkeypatch.setattr(editor, 'HLS_CACHE_DIR', str(root))
    monkeypatch.setattr(editor, '_hls_encoder_locks', {})
    monkeypatch.setattr(editor, '_probe_video', lambda _path: None)
    monkeypatch.setattr(editor, '_get_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr(editor.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(editor, 'HLS_FIRST_SEGMENT_WAIT_SECONDS', 0.2)
    # raising=False lets this setup run against code that lacks these hooks,
    # so each test there fails on its own assertion or missing hook.
    monkeypatch.setattr(editor, 'HLS_SEGMENT_WAIT_SECONDS', 0.2, raising=False)
    monkeypatch.setattr(editor, '_hls_encoders', {}, raising=False)
    monkeypatch.setattr(editor, '_hls_replaced_dirs', {}, raising=False)
    monkeypatch.setattr(editor, '_hls_stopping_dirs', set(), raising=False)
    monkeypatch.setattr(editor, '_now', clock, raising=False)
    monkeypatch.setattr(editor, 'HLS_ENCODER_STOP_GRACE_SECONDS', 0.05, raising=False)
    # Each test drives sweeps itself unless it starts the real reaper.
    monkeypatch.setattr(editor, '_ensure_hls_reaper_started', lambda: None, raising=False)
    monkeypatch.setattr(editor, '_hls_reaper_thread', None, raising=False)
    monkeypatch.setattr(editor, '_hls_reaper_stop', threading.Event(), raising=False)

    media_ids = {}

    def spawn(video, track=0, start=0.0, player='tab1'):
        media_id = media_ids.setdefault(video, len(media_ids) + 1)
        cache_dir = editor._hls_cache_dir('episode', media_id, None, track, start, 0)
        count = len(spawned)
        kwargs = {} if player is None else {'player': player}
        editor._spawn_hls_encoder(video, track, start, cache_dir, **kwargs)
        _wait_until(lambda: len(spawned) > count)
        return cache_dir

    state = SimpleNamespace(
        editor=editor, clock=clock, spawned=spawned, root=root, spawn=spawn,
        behaviours=behaviours, real_ensure_reaper=real_ensure_reaper, tmp_path=tmp_path,
    )
    yield state

    editor._hls_reaper_stop.set()
    thread = editor._hls_reaper_thread
    if thread is not None:
        thread.join(timeout=2)
    for process in spawned:
        process.finish()


def _live(hls):
    return {cache_dir for cache_dir, encoder in hls.editor._hls_encoders.items()
            if encoder.process.poll() is None}


def test_idle_encoder_is_reaped_without_any_request(hls, monkeypatch):
    """The preview closes and no request ever arrives again: the reaper still acts."""
    monkeypatch.setattr(hls.editor, 'HLS_SWEEP_INTERVAL_SECONDS', 0.01, raising=False)
    cache_dir = hls.spawn('/media/show/episode.mkv', track=1, start=19.286)
    process = hls.spawned[0]
    hls.real_ensure_reaper()

    hls.clock.advance(hls.editor.HLS_ENCODER_IDLE_TIMEOUT_SECONDS + 1)

    assert _wait_until(lambda: process.returncode is not None)
    assert process.signals == ['TERM']
    assert process.waited
    assert _wait_until(lambda: not os.path.exists(cache_dir))
    assert hls.editor._hls_encoders == {}


def test_requests_keep_an_encoder_alive_until_they_stop(hls):
    cache_dir = hls.spawn('/media/show/episode.mkv')
    process = hls.spawned[0]
    idle = hls.editor.HLS_ENCODER_IDLE_TIMEOUT_SECONDS

    hls.clock.advance(idle - 1)
    hls.editor._touch_hls_session(cache_dir)
    hls.clock.advance(idle - 1)
    hls.editor._sweep_hls_cache()
    assert process.returncode is None
    assert _live(hls) == {cache_dir}

    hls.clock.advance(2)
    hls.editor._sweep_hls_cache()
    assert process.signals == ['TERM']
    assert _live(hls) == set()


def test_reaping_deletes_the_segment_dir(hls):
    cache_dir = hls.spawn('/media/show/episode.mkv')
    assert os.path.isfile(os.path.join(cache_dir, 'segment_001.m4s'))
    assert os.path.isfile(os.path.join(cache_dir, '.encoding'))

    hls.clock.advance(hls.editor.HLS_ENCODER_IDLE_TIMEOUT_SECONDS + 1)
    hls.editor._sweep_hls_cache()

    assert not os.path.exists(cache_dir)
    assert os.listdir(hls.root) == []


def test_track_switch_terminates_the_previous_encoder(hls):
    video = '/media/show/11.22.63 S01E08.mkv'
    first = hls.spawn(video, track=0, start=0.0)
    second = hls.spawn(video, track=1, start=19.286)

    assert hls.spawned[0].signals == ['TERM']
    assert hls.spawned[0].waited
    assert not os.path.exists(first)
    assert _live(hls) == {second}

    third = hls.spawn(video, track=2, start=42.5)

    assert hls.spawned[1].signals == ['TERM']
    assert not os.path.exists(second)
    assert _live(hls) == {third}
    assert [p.returncode is None for p in hls.spawned] == [False, False, True]


def test_switching_to_another_file_stops_the_players_previous_encoder(hls):
    first = hls.spawn('/media/show/episode-1.mkv', track=1, start=20.0)
    second = hls.spawn('/media/show/episode-2.mkv')

    assert hls.spawned[0].signals == ['TERM']
    assert not os.path.exists(first)
    assert _live(hls) == {second}


def test_a_session_another_player_still_uses_survives_a_switch(hls):
    video = '/media/show/episode.mkv'
    shared = hls.spawn(video, player='tabA')
    # A second tab opened the same file at the start: same session directory.
    hls.editor._touch_hls_session(shared, 'tabB')

    switched = hls.spawn(video, track=1, start=9.0, player='tabA')

    assert hls.spawned[0].signals == []
    assert os.path.isdir(shared)
    assert _live(hls) == {shared, switched}


def test_clients_without_a_player_id_do_not_replace_each_other(hls):
    video = '/media/show/episode.mkv'
    first = hls.spawn(video, track=0, start=5.0, player=None)
    second = hls.spawn(video, track=1, start=5.0, player=None)

    assert [p.signals for p in hls.spawned] == [[], []]
    assert _live(hls) == {first, second}


def test_track_switch_in_one_player_leaves_another_player_alone(hls):
    video = '/media/show/episode.mkv'
    tab_a = hls.spawn(video, track=0, start=12.0, player='tabA')
    tab_b = hls.spawn(video, track=1, start=30.0, player='tabB')
    assert _live(hls) == {tab_a, tab_b}

    tab_a_switched = hls.spawn(video, track=1, start=14.0, player='tabA')

    assert hls.spawned[0].signals == ['TERM']
    assert hls.spawned[1].signals == []
    assert _live(hls) == {tab_a_switched, tab_b}


def test_global_encoder_cap_stops_the_least_recently_used(hls, monkeypatch):
    monkeypatch.setattr(hls.editor, 'HLS_MAX_ENCODERS', 2, raising=False)
    first = hls.spawn('/media/a.mkv', player='tabA')
    hls.clock.advance(5)
    second = hls.spawn('/media/b.mkv', player='tabB')
    hls.clock.advance(5)
    # The first session is still being watched, the second is not.
    hls.editor._touch_hls_session(first)

    third = hls.spawn('/media/c.mkv', player='tabC')

    assert hls.spawned[1].signals == ['TERM']
    assert not os.path.exists(second)
    assert _live(hls) == {first, third}
    assert len(hls.editor._hls_encoders) == 2


def test_stop_escalates_to_kill_and_always_waits(hls):
    hls.behaviours['ignore_term'] = True
    cache_dir = hls.spawn('/media/stubborn.mkv')
    process = hls.spawned[0]

    hls.clock.advance(hls.editor.HLS_ENCODER_IDLE_TIMEOUT_SECONDS + 1)
    hls.editor._sweep_hls_cache()

    assert process.signals == ['TERM', 'KILL']
    assert process.returncode == -9
    assert process.waited
    assert not os.path.exists(cache_dir)


def test_ffmpeg_never_closes_the_playlist_itself(hls):
    hls.spawn('/media/show/episode.mkv')
    cmd = hls.spawned[0].args

    assert cmd[cmd.index('-hls_flags') + 1] == 'omit_endlist'


def test_finished_encoder_closes_its_playlist_and_the_session_is_reused(hls):
    cache_dir = hls.spawn('/media/show/episode.mkv')
    hls.spawned[0].finish(0)

    assert _wait_until(lambda: hls.editor._hls_encoders == {})
    assert Path(cache_dir, 'playlist.m3u8').read_text() == COMPLETE_PLAYLIST
    assert not os.path.exists(os.path.join(cache_dir, '.encoding'))
    hls.editor._spawn_hls_encoder('/media/show/episode.mkv', 0, 0.0, cache_dir)

    assert len(hls.spawned) == 1


def test_encoder_stopped_from_outside_leaves_the_session_to_encode_again(hls):
    cache_dir = hls.spawn('/media/show/episode.mkv')
    hls.spawned[0].finish(255)  # ffmpeg's exit code after SIGTERM or SIGINT

    assert _wait_until(lambda: hls.editor._hls_encoders == {})
    assert Path(cache_dir, 'playlist.m3u8').read_text() == PARTIAL_PLAYLIST
    hls.editor._spawn_hls_encoder('/media/show/episode.mkv', 0, 0.0, cache_dir)

    assert len(hls.spawned) == 2


def test_encoder_error_ends_the_session_instead_of_looping(hls):
    """An error at the same point every run must not re-encode forever."""
    cache_dir = hls.spawn('/media/show/damaged.mkv')
    hls.spawned[0].finish(1)

    assert _wait_until(lambda: hls.editor._hls_encoders == {})
    assert Path(cache_dir, 'playlist.m3u8').read_text() == COMPLETE_PLAYLIST
    hls.editor._spawn_hls_encoder('/media/show/damaged.mkv', 0, 0.0, cache_dir)

    assert len(hls.spawned) == 1


def test_no_encoder_starts_in_a_directory_being_stopped(hls):
    cache_dir = hls.editor._hls_cache_dir('episode', 8, None, 0, 0.0, 0)
    hls.editor._hls_stopping_dirs.add(cache_dir)

    hls.editor._spawn_hls_encoder('/media/show/episode.mkv', 0, 0.0, cache_dir)

    assert hls.spawned == []
    assert not hls.editor._hls_segment_miss_needs_encoder(cache_dir)


def test_stale_marker_without_an_encoder_does_not_block_a_spawn(hls):
    cache_dir = hls.editor._hls_cache_dir('episode', 7, None, 0, 0.0, 0)
    os.makedirs(cache_dir)
    Path(cache_dir, '.encoding').write_text('1')
    Path(cache_dir, 'playlist.m3u8').write_text('#EXTM3U\n')

    hls.editor._spawn_hls_encoder('/media/show/episode.mkv', 0, 0.0, cache_dir)

    assert len(hls.spawned) == 1
    assert _live(hls) == {cache_dir}


def test_eviction_skips_a_live_session_however_old_its_dir(hls):
    cache_dir = hls.spawn('/media/show/episode.mkv')
    stamp = time.time() - hls.editor.HLS_IDLE_TTL_SECONDS - 60
    os.utime(cache_dir, (stamp, stamp))

    hls.editor._evict_stale_hls_dirs()

    assert os.path.isdir(cache_dir)


def test_startup_cleans_stale_dirs(hls):
    root = hls.root

    def session(name, playlist=None, marker=False, age=0):
        path = root / name
        path.mkdir()
        (path / 'segment_000.m4s').write_bytes(b'data')
        if playlist is not None:
            (path / 'playlist.m3u8').write_text(playlist)
        if marker:
            (path / '.encoding').write_text('1')
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    # Left mid-encode by the previous run: marker still there.
    encoding = session('encoding', playlist=PARTIAL_PLAYLIST, marker=True)
    # Its encoder was stopped, which closed the playlist, before its waiter ran.
    marked_closed = session('marked-closed', playlist=COMPLETE_PLAYLIST, marker=True)
    # Killed earlier: segments but no finished playlist.
    killed = session('killed')
    # Finished but untouched for longer than the cache TTL.
    expired = session('expired', playlist=COMPLETE_PLAYLIST,
                      age=hls.editor.HLS_IDLE_TTL_SECONDS + 60)
    # Finished and recent: still worth reusing.
    finished = session('finished', playlist=COMPLETE_PLAYLIST)
    # A damaged playlist must not stop the cleanup, or startup.
    damaged = root / 'damaged'
    damaged.mkdir()
    (damaged / 'playlist.m3u8').write_bytes(b'\xff\xfe\x00garbage')
    stray = root / 'not-a-session.txt'
    stray.write_text('x')

    hls.editor.start_hls_housekeeping()

    assert not encoding.exists()
    assert not marked_closed.exists()
    assert not killed.exists()
    assert not expired.exists()
    assert not damaged.exists()
    assert finished.exists()
    assert stray.exists()


# ---------------------------------------------------------------------------
# Through the request handler
# ---------------------------------------------------------------------------

@pytest.fixture
def handler(hls, monkeypatch):
    video = hls.tmp_path / 'Clip (2020).mkv'
    video.write_bytes(b'video')
    monkeypatch.setattr(
        hls.editor, '_resolve_video_path',
        lambda media_type, media_id, arr_instance_id=None: str(video),
    )
    app = Flask(__name__)

    def get(track, start, filename, player='tab1'):
        query = f'?player={player}' if player else ''
        url = f'/api/editor/hls/movie/5/{track}/{start}/{filename}{query}'
        with app.test_request_context(url):
            result = hls.editor.EditorHls.get.__wrapped__(
                hls.editor.EditorHls(), media_type='movie', media_id=5,
                audio_track=track, start_time=start, filename=filename,
            )
            if isinstance(result, tuple):
                return result[1], None
            result.direct_passthrough = False
            return result.status_code, result.get_data()

    def cache_dir(track, start):
        return hls.editor._hls_cache_dir('movie', 5, None, track, float(start), video.stat().st_mtime)

    return SimpleNamespace(get=get, cache_dir=cache_dir)


def test_playlist_and_segment_requests_keep_the_session_alive(hls, handler):
    idle = hls.editor.HLS_ENCODER_IDLE_TIMEOUT_SECONDS
    status, body = handler.get(1, '19.286', 'playlist.m3u8')
    assert status == 200
    assert b'segment_000.m4s?player=tab1' in body

    # Two of each in a row, so each kind of request has to keep it alive alone.
    for filename in ('segment_000.m4s', 'segment_001.m4s', 'playlist.m3u8', 'playlist.m3u8'):
        hls.clock.advance(idle - 1)
        assert handler.get(1, '19.286', filename)[0] == 200
        hls.editor._sweep_hls_cache()
        assert hls.spawned[0].returncode is None

    hls.clock.advance(idle + 1)
    hls.editor._sweep_hls_cache()
    assert hls.spawned[0].signals == ['TERM']
    assert not os.path.exists(handler.cache_dir(1, '19.286'))


def test_segment_request_after_eviction_encodes_the_session_again(hls, handler):
    """A long pause on a finished session: its cache is evicted, then playback resumes."""
    assert handler.get(0, '0.000', 'playlist.m3u8')[0] == 200
    cache_dir = handler.cache_dir(0, '0.000')
    Path(cache_dir, 'playlist.m3u8').write_text(COMPLETE_PLAYLIST)
    hls.spawned[0].finish(0)
    assert _wait_until(lambda: hls.editor._hls_encoders == {})
    hls.editor._remove_hls_dir(cache_dir)

    status, body = handler.get(0, '0.000', 'segment_001.m4s')

    assert status == 200
    assert body == b'data'
    assert len(hls.spawned) == 2
    assert _live(hls) == {cache_dir}


def test_late_segment_request_from_a_replaced_session_does_not_bring_it_back(hls, handler):
    assert handler.get(0, '0.000', 'playlist.m3u8')[0] == 200
    assert handler.get(1, '7.500', 'playlist.m3u8')[0] == 200
    new_session = handler.cache_dir(1, '7.500')
    assert _live(hls) == {new_session}

    status, _body = handler.get(0, '0.000', 'segment_001.m4s')

    assert status == 404
    assert len(hls.spawned) == 2
    assert _live(hls) == {new_session}

    # Once the idle timeout has passed, the old session may start again.
    hls.clock.advance(hls.editor.HLS_ENCODER_IDLE_TIMEOUT_SECONDS + 1)
    status, _body = handler.get(0, '0.000', 'segment_001.m4s')

    assert status == 200
    assert len(hls.spawned) == 3


def test_malformed_player_and_filename_are_rejected(hls, handler):
    status, body = handler.get(0, '0.000', 'playlist.m3u8', player='abc%0A')
    assert status == 200
    assert b'player=' not in body
    assert handler.get(0, '0.000', 'segment_001.m4s\n')[0] == 400

"""Cancellation through real repair, analysis children and persisted owned rows."""

# ruff: noqa: F811
import ast
import json
import os
import pickle
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from test_sportarr_indexer import indexed_library, row, sports  # noqa: F401
from test_sportarr_kind_migration import migration_engine  # noqa: F401


def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for analysis lifecycle"
        time.sleep(0.01)


def blocked_probe(path, monkeypatch):
    """A real executable pauses analysis, with a descendant requiring cleanup."""
    real = shutil.which("ffprobe")
    executable = path / "ffprobe"
    marker, release = path / "processes.json", path / "release"
    executable.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
from pathlib import Path
if '-version' in sys.argv:
    os.execv({real!r}, [{real!r}, *sys.argv[1:]])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path({str(marker)!r}).write_text(json.dumps({{'probe': os.getpid(), 'descendant': child.pid, 'worker': os.getppid(), 'inherited_secret': 'SPORTARR_TEST_SECRET' in os.environ, 'inherited_pythonpath': 'PYTHONPATH' in os.environ, 'inherited_config': any(key in os.environ for key in ('POSTGRES_ENABLED', 'POSTGRES_PASSWORD', 'BAZARR_CONFIG_DIR', 'HTTP_PROXY')), 'cwd': os.getcwd()}}))
try:
    deadline = time.monotonic() + 15
    while not Path({str(release)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
finally:
    child.terminate()
    child.wait()
os.execv({real!r}, [{real!r}, *sys.argv[1:]])
""")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SPORTARR_TEST_SECRET", "private-test-sentinel")
    from utilities import binaries

    monkeypatch.setattr(binaries, "get_binary", lambda name: str(executable))
    return marker, release


def repair(indexed_library, monkeypatch):
    from app import database as db
    from languages import get_languages, custom_lang
    from sportarr import identity, sse_client
    from sportarr.sync import events
    from sportarr.connection import connection_identity
    from test_sportarr_events import event, file, remote

    original, path = indexed_library
    session = scoped_session(
        sessionmaker(bind=original.get_bind(), expire_on_commit=False)
    )
    module = sports(monkeypatch, session)
    for target in (db, get_languages, custom_lang, identity, sse_client, events):
        monkeypatch.setattr(target, "database", session)
    remote(
        monkeypatch,
        events,
        [
            event(
                [
                    file(
                        filePath="/sports/event.mkv",
                        size=(path / "1/event.mkv").stat().st_size,
                    )
                ]
            )
        ],
    )

    def reconcile(owner_id, batch, *, cancel, expected_connection, http_get):
        events.sync_events(
            51, owner_id, cancel=cancel, expected_connection=expected_connection
        )

    client = sse_client.SportarrSSEClient(
        1,
        connection_identity(session.get(db.TableArrInstances, 1)),
        SimpleNamespace(),
        reconcile=reconcile,
        debounce=0,
    )
    worker = threading.Thread(target=client._work, daemon=True)
    client._threads = [worker]
    client.enqueue(full=True)
    return session, module, client, worker, events


def test_repair_stop_reaps_probe_and_retries_unchanged_metadata(
    indexed_library, monkeypatch
):
    _, path = indexed_library
    marker, release = blocked_probe(path, monkeypatch)
    session, module, client, worker, events = repair(indexed_library, monkeypatch)
    worker.start()
    try:
        eventually(marker.exists)
        processes = json.loads(marker.read_text())
        started = time.monotonic()
        stopped = client.stop(timeout=0.5)
        elapsed = time.monotonic() - started
        pending = row(session, 61)
        after_stop = pending.subtitles
        indexed = (
            pickle.loads(pending.ffprobe_cache).get("sports_indexed")
            if pending.ffprobe_cache
            else False
        )
    finally:
        release.touch()
        worker.join(10)
        client.stop()
    assert stopped and not worker.is_alive(), (
        "Stopped repair must finish without releasing its blocked probe"
    )
    assert elapsed < 0.5
    assert after_stop == "[]" and not indexed
    for pid in processes["probe"], processes["descendant"], processes["worker"]:
        assert not os.path.exists(f"/proc/{pid}"), (
            f"Analysis process {pid} was not reaped"
        )
    assert not processes["inherited_secret"] and not processes["inherited_pythonpath"]
    assert not processes["inherited_config"]
    assert processes["cwd"] != str(path)
    # The metadata has already committed. A later identical pass must index it.
    events.sync_events(51, 1)
    assert ["fr", None, None] in ast.literal_eval(row(session, 61).subtitles)
    assert pickle.loads(row(session, 61).ffprobe_cache)["sports_indexed"]
    assert row(session, 62).subtitles == "[]"
    print(
        json.dumps(
            {
                "stop_seconds": elapsed,
                "stopped": stopped,
                "reaped": True,
                "retried": True,
            }
        )
    )
    session.remove()


def test_analysis_deadline_reaps_a_real_blocked_tree(indexed_library, monkeypatch):
    from sportarr import analysis

    _, path = indexed_library
    marker, release = blocked_probe(path, monkeypatch)
    monkeypatch.setattr(analysis, "ANALYSIS_TIMEOUT", 2)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            analysis.parse_video_metadata(
                str(path / "1/event.mkv"), (path / "1/event.mkv").stat().st_size
            )
    finally:
        release.touch()
    assert time.monotonic() - started < 3
    assert marker.exists(), "The actual executable must reach its blocking boundary"
    processes = json.loads(marker.read_text())
    for pid in processes["probe"], processes["descendant"], processes["worker"]:
        assert not os.path.exists(f"/proc/{pid}")


def test_cancel_between_files_leaves_rest_of_batch_pending(
    indexed_library, monkeypatch
):
    from app.database import TableSportsEvents

    session, path = indexed_library
    shutil.copyfile(path / "1/event.mkv", path / "1/prelims.mkv")
    session.add(
        TableSportsEvents(
            id=63,
            arr_instance_id=1,
            league_id=51,
            sportarrEventId=8,
            file_id=10,
            partNumber=2,
            path="/sports/prelims.mkv",
            title="Prelims",
            file_size=(path / "1/prelims.mkv").stat().st_size,
            subtitles="[]",
            missing_subtitles="[]",
            audio_language="['French']",
            failedAttempts="[]",
        )
    )
    session.commit()
    module = sports(monkeypatch, session)
    cancel = threading.Event()
    monkeypatch.setattr(module, "notify", lambda ids: cancel.set())
    with pytest.raises(ValueError):
        module.refresh_sports_files([61, 63], 1, cancel=cancel)
    assert ["fr", None, None] in ast.literal_eval(row(session, 61).subtitles)
    assert row(session, 63).subtitles == "[]" and row(session, 63).ffprobe_cache is None
    assert row(session, 62).subtitles == "[]"
    monkeypatch.setattr(module, "notify", lambda ids: None)
    module.refresh_sports_files([61, 63], 1)
    assert ["fr", None, None] in ast.literal_eval(row(session, 63).subtitles)


def test_manager_replaces_owner_while_analysis_is_blocked(indexed_library, monkeypatch):
    from app.database import TableArrInstances
    from sportarr import sse_client
    from sportarr.connection import connection_identity

    _, path = indexed_library
    marker, release = blocked_probe(path, monkeypatch)
    session, module, old, worker, events = repair(indexed_library, monkeypatch)
    other = session.get(TableArrInstances, 2)
    other.enabled = 0
    session.commit()

    def replacement(owner, identity, arr_client):
        # Replacement can proceed only after the old owned analysis was reaped.
        assert not worker.is_alive()
        release.touch()
        client = sse_client.SportarrSSEClient(
            owner, identity, arr_client, reconcile=old.reconcile, debounce=0
        )

        def start():
            thread = threading.Thread(target=client._work, daemon=True)
            client._threads = [thread]
            client.enqueue(full=True)
            thread.start()

        client.start = start
        return client

    manager = sse_client.SportarrClientManager(client_factory=replacement)
    manager.clients[1] = old
    worker.start()
    try:
        eventually(marker.exists)
        previous_pids = json.loads(marker.read_text())
        owner = session.get(TableArrInstances, 1)
        owner.ip = "replacement.invalid"
        session.commit()
        expected = connection_identity(owner)
        started = time.monotonic()
        manager.refresh(session)
        assert time.monotonic() - started < 2
        assert manager.clients[1] is not old and manager.clients[1].identity == expected
        for pid in (
            previous_pids["probe"],
            previous_pids["descendant"],
            previous_pids["worker"],
        ):
            assert not os.path.exists(f"/proc/{pid}")
        eventually(lambda: bool(ast.literal_eval(row(session, 61).subtitles)))
        assert ["fr", None, None] in ast.literal_eval(row(session, 61).subtitles)
    finally:
        release.touch()
        manager.stop()
        worker.join(10)
        session.remove()
    assert not manager.clients and not worker.is_alive()


@pytest.mark.parametrize("stage", ["discovery", "publication"])
def test_cancel_after_slow_work_cannot_publish(indexed_library, monkeypatch, stage):
    from sqlalchemy import event as sa_event

    session, path = indexed_library
    module = sports(monkeypatch, session)
    cancel = threading.Event()
    if stage == "discovery":
        discover = module.search_external_subtitles

        def completed_discovery(*args, **kwargs):
            result = discover(*args, **kwargs)
            cancel.set()
            return result

        monkeypatch.setattr(module, "search_external_subtitles", completed_discovery)
    else:
        # Exercise the last transaction boundary, after SQL was flushed but
        # before commit. Cancellation must roll back those actual writes.
        def cancel_publication(session, context):
            if any(getattr(item, "subtitles", "[]") != "[]" for item in session.dirty):
                cancel.set()

        sa_event.listen(Session, "after_flush", cancel_publication)
    try:
        with pytest.raises(ValueError):
            module.store_subtitles_sports(61, 1, cancel=cancel)
    finally:
        if stage == "publication":
            sa_event.remove(Session, "after_flush", cancel_publication)
    assert row(session, 61).subtitles == "[]"
    assert not row(session, 61).ffprobe_cache or not pickle.loads(
        row(session, 61).ffprobe_cache
    ).get("sports_indexed")
    assert row(session, 62).subtitles == "[]"


def test_cancel_waiting_for_shared_output_lock(indexed_library, monkeypatch):
    from subtitles.tools.subsync_engines import subtitle_write_lock

    session, path = indexed_library
    module = sports(monkeypatch, session)
    cancel = threading.Event()
    held, release = threading.Event(), threading.Event()

    def writer():
        with subtitle_write_lock(str(path / "1/event.mkv"), str(path / "1")):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=writer)
    thread.start()
    eventually(held.is_set)
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(ValueError):
            module.store_subtitles_sports(61, 1, cancel=cancel)
        assert time.monotonic() - started < 0.5
        assert not release.is_set()
    finally:
        release.set()
        timer.join()
        thread.join(2)
    assert not thread.is_alive() and row(session, 61).subtitles == "[]"


def test_owned_analysis_preserves_metadata_with_measured_cost(indexed_library):
    from sportarr.analysis import parse_video_metadata as owned
    from utilities.video_analyzer import parse_video_metadata as legacy

    _, path = indexed_library
    file = str(path / "1/event.mkv")
    size = (path / "1/event.mkv").stat().st_size
    timings = {"legacy_seconds": [], "owned_seconds": []}
    for _ in range(3):
        started = time.monotonic()
        expected = legacy(file, size, use_cache=False)
        timings["legacy_seconds"].append(time.monotonic() - started)
        started = time.monotonic()
        actual = owned(file, size)
        timings["owned_seconds"].append(time.monotonic() - started)
        assert actual == expected
        assert actual["ffprobe"]["subtitle"][0]["language"].alpha3 == "fra"
    print(json.dumps(timings))


def test_strm_input_stays_private_and_analysis_errors_are_sanitized(
    indexed_library, monkeypatch
):
    from app.config import settings
    from sportarr import analysis

    _, path = indexed_library
    stream = path / "private.strm"
    stream.write_text(str(path / "1/event.mkv"))
    monkeypatch.setattr(settings.general, "enable_strm_support", True)
    popen = analysis.subprocess.Popen
    commands = []

    def launch(command, **kwargs):
        commands.append(command)
        return popen(command, **kwargs)

    monkeypatch.setattr(analysis.subprocess, "Popen", launch)
    result = analysis.parse_video_metadata(str(stream), stream.stat().st_size)
    assert result["ffprobe"]["subtitle"][0]["language"].alpha3 == "fra"
    assert all(
        str(stream) not in command and stream.read_text() not in command
        for command in commands
    )
    # An unsupported local protocol fails without contacting an external service.
    stream.write_text("unsupported-private-protocol://user:private-token@invalid/media")
    with pytest.raises(OSError) as error:
        analysis.parse_video_metadata(str(stream), stream.stat().st_size)
    assert "private-token" not in str(error.value) and "invalid/media" not in str(
        error.value
    )


def test_native_analysis_stall_cannot_strand_descendants(indexed_library, monkeypatch):
    """Native code holding the GIL must not block the process-tree supervisor."""
    import ctypes
    import knowit
    from pathlib import Path
    from sportarr import analysis

    _, path = indexed_library
    marker = path / "native-stall.json"
    package = path / "knowit"
    package.mkdir()
    source = Path(knowit.__file__)
    # Keep the installed executor implementation. Only the top-level analysis
    # function is replaced by a native-stall fault injector in this child.
    (package / "__init__.py").write_text(
        "__path__.append(" + repr(str(source.parent)) + ")\n" + source.read_text()
    )
    probe_code = f"""
import json, os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path({str(marker)!r}).write_text(json.dumps({{'analysis': os.getppid(), 'probe': os.getpid(), 'descendant': child.pid}}))
time.sleep(30)
"""
    (package / "api.py").write_text(f"""
import ctypes, sys, threading, time
from pathlib import Path
class KnowitException(Exception): pass
def know(*args, **kwargs):
    from knowit.providers.ffmpeg import check_output
    threading.Thread(target=lambda: check_output([sys.executable, '-c', {probe_code!r}]), daemon=True).start()
    while not Path({str(marker)!r}).exists(): time.sleep(0.001)
    ctypes.PyDLL(None).sleep(15)
    return {{}}
""")
    monkeypatch.syspath_prepend(str(path))
    cancel = threading.Event()
    errors = []

    def run():
        try:
            analysis.parse_video_metadata(str(path / "1/event.mkv"), 1, cancel=cancel)
        except Exception as error:
            errors.append(error)

    # Own orphaned fault-injection children even on a failing red run. The
    # private pytest process restores its normal state after cleanup.
    libc = ctypes.CDLL(None)
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    thread = threading.Thread(target=run)
    thread.start()
    try:
        eventually(marker.exists)
        pids = json.loads(marker.read_text())
        cancel.set()
        thread.join(2)
        assert not thread.is_alive()
        assert errors and isinstance(errors[0], ValueError)
        assert all(not os.path.exists(f"/proc/{pid}") for pid in pids.values()), (
            "Native analysis left an owned process behind"
        )
    finally:
        cancel.set()
        thread.join(3)
        if marker.exists():
            for pid in json.loads(marker.read_text()).values():
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
            for pid in json.loads(marker.read_text()).values():
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
        assert libc.prctl(36, 0, 0, 0, 0) == 0


def test_early_worker_failure_still_reaps_the_child(tmp_path, monkeypatch):
    from sportarr import analysis

    helper = tmp_path / "exited_reader.py"
    marker = tmp_path / "reader_closed"
    helper.write_text(f"""import os, time
from pathlib import Path
os.close(0)
Path({str(marker)!r}).touch()
time.sleep(0.2)
os._exit(2)
""")
    monkeypatch.setattr(analysis, "WORKER_PATH", helper)
    popen = analysis.subprocess.Popen
    processes = []

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        eventually(marker.exists)
        return process

    monkeypatch.setattr(analysis.subprocess, "Popen", launch)
    try:
        with pytest.raises(OSError):
            analysis.parse_video_metadata("unavailable.mkv", 1)
        assert processes[0].returncode == 2, (
            "A broken request pipe must still wait for its child"
        )
        assert not os.path.exists(f"/proc/{processes[0].pid}")
    finally:
        for process in processes:
            process.wait(2)


def test_parent_death_reaps_owned_analysis_tree(indexed_library, monkeypatch):
    import ctypes
    import subprocess
    from sportarr import analysis

    _, path = indexed_library
    marker, release = blocked_probe(path, monkeypatch)
    helper_pid = path / "helper.pid"
    launcher = path / "parent.py"
    launcher.write_text(f"""import subprocess, sys
from pathlib import Path
request = sys.stdin.buffer.readline()
process = subprocess.Popen([sys.executable, '-I', '-S', '-u', {str(analysis.WORKER_PATH)!r}], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path({str(helper_pid)!r}).write_text(str(process.pid))
process.stdin.write(request)
process.stdin.flush()
process.wait()
""")
    request = (
        json.dumps(
            {
                "sys_path": [p for p in sys.path if p and os.path.isabs(p)],
                "file": str(path / "1/event.mkv"),
                "file_size": 1,
                "parser": "ffprobe",
                "strm": False,
            }
        ).encode()
        + b"\n"
    )
    libc = ctypes.CDLL(None)
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    env = {"PATH": os.environ["PATH"], "HOME": str(path), "LANG": "C.UTF-8"}
    parent = subprocess.Popen(
        [sys.executable, "-I", "-S", "-u", str(launcher)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=path,
        env=env,
    )
    try:
        parent.stdin.write(request)
        parent.stdin.flush()
        eventually(marker.exists)
        processes = json.loads(marker.read_text())
        supervisor = int(helper_pid.read_text())
        parent.kill()
        parent.wait(2)
        eventually(
            lambda: os.waitpid(supervisor, os.WNOHANG)[0] == supervisor, timeout=2
        )
        for pid in processes["worker"], processes["probe"], processes["descendant"]:
            assert not os.path.exists(f"/proc/{pid}"), (
                "Parent death left an owned process behind"
            )
    finally:
        parent.stdin.close()
        release.touch()
        if parent.poll() is None:
            parent.kill()
        parent.wait(2)
        pids = (
            list(json.loads(marker.read_text()).values())[:3] if marker.exists() else []
        )
        if helper_pid.exists():
            pids.append(int(helper_pid.read_text()))
        for pid in pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        for pid in pids:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        assert libc.prctl(36, 0, 0, 0, 0) == 0

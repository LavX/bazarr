"""Owned, bounded metadata analysis without inheriting application state."""
import json
import os
import pickle
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sportarr.connection import check_cancelled


POLL_INTERVAL = 0.25
ANALYSIS_TIMEOUT = 120
MAX_RESULT = 32 * 1024 * 1024
WORKER_PATH = Path(__file__).with_name('analysis_worker.py')


def _stop(process):
    # EOF asks the worker to kill and reap its probe tree before exiting.
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.kill()
    process.wait(timeout=1)


def parse_video_metadata(file, file_size, use_cache=False, *, cancel=None):
    """Return the existing serialized metadata shape; persistence belongs to the caller."""
    from app.config import settings

    check_cancelled(cancel)
    request = json.dumps({'sys_path': [path for path in sys.path if path and os.path.isabs(path)],
                          'file': file, 'file_size': file_size,
                          'parser': settings.general.embedded_subtitles_parser,
                          'strm': bool(settings.general.enable_strm_support)}).encode() + b'\n'
    if len(request) > 1024 * 1024:
        raise ValueError('Sports analysis request is too large')
    with tempfile.TemporaryDirectory(prefix='bazarr-sports-analysis-') as directory:
        # Do not forward credentials, application config, Python startup hooks,
        # proxy settings or the user's home (including .netrc) to analysis.
        env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'WINDIR') if key in os.environ}
        env.update(HOME=directory, USERPROFILE=directory, TMPDIR=directory, TMP=directory, TEMP=directory,
                   LANG='C.UTF-8')
        with tempfile.TemporaryFile(dir=directory) as output:
            process = subprocess.Popen([sys.executable, '-I', '-S', '-u', str(WORKER_PATH)],
                                       stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                       cwd=directory, env=env, start_new_session=os.name != 'nt',
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                process.stdin.write(request)
                process.stdin.flush()
                deadline = time.monotonic() + ANALYSIS_TIMEOUT
                while process.poll() is None:
                    check_cancelled(cancel)
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Sports video analysis timed out')
                    # A quarter second, not 10ms. Each iteration polled the
                    # cancel signal twice, and that signal reads the database,
                    # so a 60-second analysis issued tens of thousands of
                    # queries purely to notice a cancellation it could equally
                    # notice a fraction of a second later.
                    if cancel is not None:
                        cancel.wait(POLL_INTERVAL)
                    else:
                        time.sleep(POLL_INTERVAL)
                check_cancelled(cancel)
                if process.returncode:
                    raise OSError('Could not analyze sports video')
                output.seek(0)
                data = output.read(MAX_RESULT + 1)
                if not data or len(data) > MAX_RESULT:
                    raise OSError('Invalid sports analysis result')
                result = pickle.loads(data)
                check_cancelled(cancel)
                return result
            finally:
                _stop(process)

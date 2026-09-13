"""Private analysis process. No application startup, settings or database imports."""
import json
import os
import pickle
import signal
import subprocess
import sys
import threading
import time


_children = set()
_children_lock = threading.Lock()
_stopping = threading.Event()


class AnalysisStopped(BaseException):
    pass


class NoApplicationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app' or fullname.startswith('app.'):
            raise ImportError('Application imports are forbidden in sports analysis')
        return None


def _own_windows_tree():
    # A job handle belongs only to this worker. Windows closes it on exit,
    # terminating descendants even if their immediate parent already exited.
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('minimum_working_set', ctypes.c_size_t),
                    ('maximum_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io_counters', ctypes.c_ulonglong * 6),
                    ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                    ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _kill_tree(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=1)


def _reap_descendants():
    # Linux containers do not necessarily have an init that reaps orphans.
    # This worker is a subreaper, so it also waits for probe grandchildren.
    if not sys.platform.startswith('linux'):
        return
    deadline = time.monotonic() + 1
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.001)


def _watch_parent():
    sys.stdin.buffer.read(1)
    _stopping.set()
    with _children_lock:
        for process in _children:
            _kill_tree(process)
        _reap_descendants()
    os._exit(2)


def analyze():
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, NoApplicationImports())
    request = json.loads(sys.stdin.buffer.readline(1024 * 1024 + 1))
    sys.path[:] = request['sys_path']
    from knowit.api import know
    from utilities.binaries import get_binary

    parser = request['parser']
    if parser not in ('ffprobe', 'mediainfo'):
        raise ValueError('Unsupported sports analyzer')
    binary = get_binary(parser)
    path = request['file']
    if request['strm'] and path.lower().endswith('.strm'):
        with open(path) as stream:
            path = stream.read().strip() or path
    provider = 'ffmpeg' if parser == 'ffprobe' else 'mediainfo'
    data = {'ffprobe': {}, 'mediainfo': {}, 'file_id': None, 'file_size': request['file_size']}
    data[parser] = know(video_path=path, context={'provider': provider, provider: binary})
    if not data[parser]:
        raise OSError('Could not analyze sports video')
    result = pickle.dumps(data, pickle.HIGHEST_PROTOCOL)
    if len(result) > 32 * 1024 * 1024:
        raise ValueError('Sports analysis result is too large')
    sys.stdout.buffer.write(result)
    sys.stdout.buffer.flush()


def main():
    # Keep supervision free of third-party/native code. Even a library call
    # holding the analyzer's GIL cannot block cancellation or descendant reaping.
    if os.name == 'nt':
        _own_windows_tree()
    if sys.platform.startswith('linux'):
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
            raise OSError('Could not own analysis descendants')
    request = sys.stdin.buffer.readline(1024 * 1024 + 1)
    if len(request) > 1024 * 1024 or not request.endswith(b'\n'):
        raise ValueError('Invalid sports analysis request')
    threading.Thread(target=_watch_parent, daemon=True).start()
    with _children_lock:
        if _stopping.is_set():
            raise AnalysisStopped()
        process = subprocess.Popen([sys.executable, '-I', '-S', '-u', __file__, 'analyze'],
                                   stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != 'nt')
        _children.add(process)
    try:
        process.communicate(input=request)
        if process.returncode:
            raise OSError('Could not analyze sports video')
    finally:
        with _children_lock:
            _kill_tree(process)
            _children.discard(process)
            _reap_descendants()


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['analyze']:
            analyze()
        else:
            main()
        os._exit(0)
    except BaseException:
        # Paths and .strm contents may contain private URLs. Errors crossing
        # this process boundary intentionally omit library diagnostics.
        os._exit(1)

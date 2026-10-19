# coding=utf-8
"""Tesseract through the tesserocr wheel, in a worker process per OCR run.

The Bazarr+ process never imports tesserocr. Since 2.10 it loads cysignals,
whose import works only in a main thread and takes over the process's
handlers for SIGINT, SIGHUP, SIGALRM and the crash signals, and a signal that
arrives while a job thread is inside Tesseract then kills the whole process.
tesseract_worker.py runs Tesseract in its own process instead: cysignals gets
that process's main thread, a crash in Tesseract or Leptonica ends only that
process, and a call that never returns is killed.

tesserocr ships wheels for Linux x86_64 and aarch64 (and macOS 15 and later,
installed by hand), so an installation without it keeps working and reports
OCR as unavailable instead.
"""

import base64
import importlib.util
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager

from .errors import OcrError, OcrUnavailable
from .tessdata import ensure_pack, tessdata_dir

UNAVAILABLE = ("OCR is not available on this installation: the tesserocr package is not installed or does not "
               "load. It ships for Linux x86_64 and aarch64, which the Docker image covers.")

WORKER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tesseract_worker.py")
# Starting the worker, loading a pack and reading a cue each take well under a
# second; this only stops a Tesseract call that never comes back.
ANSWER_TIMEOUT = 120
# An answer is one cue's text. A longer line means the worker is broken.
MAX_ANSWER_CHARS = 1 << 20
_STDERR_TAIL = 2000


# Tesseract reads the plain vertical stroke of a sans-serif I or l as a pipe,
# which no subtitle contains. A pipe standing alone (or before an apostrophe)
# was the pronoun I; one touching letters or another pipe was an l.
_LONE_BAR = re.compile(r"(?<![\w|])\|(?![\w|])")
_BAR = re.compile(r"\|")


def fix_bars(text):
    return _BAR.sub("l", _LONE_BAR.sub("I", text or ""))


def ocr_available():
    """Whether tesserocr is installed. Only the worker imports it, so an
    install that does not load is reported when a run starts."""
    return importlib.util.find_spec("tesserocr") is not None


def worker_command():
    # -P keeps the worker's folder off sys.path and -B writes no bytecode into
    # the application tree. Not -I or -s: outside a virtualenv the startup
    # installer puts tesserocr in the user's site-packages.
    return [sys.executable, "-P", "-B", WORKER_PATH]


def worker_environment():
    # OMP_THREAD_LIMIT is read by an OpenMP runtime when the library loads, so
    # one job takes one core; the 2.11.0 wheels are built without OpenMP, which
    # makes it a guard for a future build. CYSIGNALS_CRASH_NDEBUG stops
    # cysignals, when Tesseract crashes outside the calls it guards, from
    # starting its cysignals-CSI helper, which attaches gdb where it finds one.
    # Only the worker gets them, not Bazarr+.
    return dict(os.environ, OMP_THREAD_LIMIT="1", CYSIGNALS_CRASH_NDEBUG="1")


class TesseractWorker:
    """One worker process, asked one JSON line at a time."""

    def __init__(self):
        self._stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            env=worker_environment(), text=True, encoding="utf-8",
            # Its own session, so a Ctrl-C meant for Bazarr+ does not reach it
            # mid-cue; it exits when Bazarr+ closes its input.
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._answers = queue.Queue()
        self._reader = threading.Thread(target=self._read_answers, name="tesseract-worker-answers", daemon=True)
        self._reader.start()
        try:
            hello = self._receive()
        except BaseException:
            self.close()
            raise
        if not hello.get("ok"):
            self.close()
            raise OcrUnavailable(f"{UNAVAILABLE} ({hello.get('message')})")
        self.versions = hello

    def _read_answers(self):
        while True:
            line = self.process.stdout.readline(MAX_ANSWER_CHARS + 1)
            self._answers.put(line)
            if not line.endswith("\n"):
                return

    def _stop(self, reason):
        self.process.kill()
        self.process.wait()
        self._stderr.seek(0)
        tail = self._stderr.read()[-_STDERR_TAIL:].decode("utf-8", "replace").strip()
        raise OcrError(f"Tesseract {reason}" + (f": {tail}" if tail else ""))

    def _receive(self):
        try:
            line = self._answers.get(timeout=ANSWER_TIMEOUT)
        except queue.Empty:
            self._stop(f"did not answer within {ANSWER_TIMEOUT} s")
        if not line:
            try:
                code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
            self._stop(f"stopped with signal {-code}" if code and code < 0 else f"stopped (exit code {code})")
        if not line.endswith("\n"):
            self._stop("sent an answer that is too long")
        return json.loads(line)

    def ask(self, request):
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
        except OSError:
            pass  # it has stopped; _receive says how
        answer = self._receive()
        if not answer.get("ok"):
            raise OcrError(f"Tesseract failed: {answer.get('message')}")
        return answer

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._reader.join(timeout=5)
        self.process.stdout.close()
        self._stderr.close()


class TesseractEngine:
    """The one engine. ``pipeline.run_ocr`` takes any object with this shape."""

    name = "tesseract"

    def __init__(self, directory=None):
        self.directory = directory
        self.pack = None

    def prepare(self, language, progress=None):
        """Check the language, fetch its pack and load it once. Runs before anything is rendered."""
        if not ocr_available():
            raise OcrUnavailable(UNAVAILABLE)
        self.directory = self.directory or tessdata_dir()
        # ensure_pack names the pack before it touches the network, so an
        # unknown language fails here without a download.
        self.pack = ensure_pack(language, self.directory, progress=progress)
        # A wheel that does not load, or a pack Tesseract refuses, fails now
        # rather than after minutes of rendering. About 0.1 s.
        with self._worker():
            pass

    @contextmanager
    def _worker(self):
        worker = TesseractWorker()
        try:
            worker.ask({"op": "open", "tessdata": self.directory, "language": self.pack})
            yield worker
        finally:
            worker.close()

    @contextmanager
    def session(self):
        """Yields ``read(image) -> str`` backed by one worker with the pack loaded."""
        with self._worker() as worker:
            def read(image):
                # The bytes tesserocr's own SetImage would make from this image.
                buffer = io.BytesIO()
                image.save(buffer, "BMP")
                answer = worker.ask({"op": "read", "bmp": base64.b64encode(buffer.getvalue()).decode("ascii")})
                return fix_bars(answer["text"])
            yield read

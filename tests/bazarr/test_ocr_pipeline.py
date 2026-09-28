# coding=utf-8
"""The OCR run end to end: real ffmpeg, real Tesseract, and a stand-in engine."""

import ast
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pysubs2
import pytest
from PIL import Image, ImageDraw, ImageFont

from ocr_fixtures import BLACK, FAINT, FIXTURES, box_rows, build_sup, mux_mkv
from subtitles.tools.ocr import pipeline, tesseract
from subtitles.tools.ocr.errors import LanguagePackError, OcrError, OcrUnavailable, TooLittleText
from subtitles.tools.ocr.tesseract import TesseractEngine, fix_bars

HUNGARIAN_LETTERS = "áéíóöőúüűÁÉÍÓÖŐÚÜŰ"


@pytest.fixture(scope="module")
def ffmpeg():
    path = shutil.which("ffmpeg")
    assert path, "the OCR tests need ffmpeg on PATH (CI installs it with apt)"
    return path


@pytest.fixture(scope="module")
def media(ffmpeg, tmp_path_factory):
    folder = tmp_path_factory.mktemp("ocr-pipeline")
    return {
        "pgs": mux_mkv(ffmpeg, str(folder / "pgs.mkv"), [
            (os.path.join(FIXTURES, "eng_hd.sup"), "copy", "eng"),
            (os.path.join(FIXTURES, "hun_hd.sup"), "copy", "hun")]),
        "vobsub": mux_mkv(ffmpeg, str(folder / "vobsub.mkv"), [
            (os.path.join(FIXTURES, "hun_sd.sup"), "dvdsub", "hun")]),
    }


@pytest.fixture(scope="session")
def packs():
    """eng and hun from tessdata_fast, fetched once through the product's own path.

    Cached in ~/.cache/bazarr-tessdata (or $BAZARR_TEST_TESSDATA) so a local run
    downloads them once; a CI job downloads about 9.4 MB.
    """
    from subtitles.tools.ocr.tessdata import ensure_pack

    directory = os.environ.get("BAZARR_TEST_TESSDATA") or os.path.expanduser("~/.cache/bazarr-tessdata")
    for language in ("eng", "hun"):
        ensure_pack(language, directory)
    return directory


def _texts(srt):
    return [event.plaintext for event in pysubs2.SSAFile.from_string(srt)]


class FakeEngine:
    name = "fake"

    def __init__(self, answers, fail_prepare=None):
        self.answers = list(answers)
        self.fail_prepare = fail_prepare
        self.prepared = None

    def prepare(self, language, progress=None):
        if self.fail_prepare:
            raise self.fail_prepare
        self.prepared = language

    @contextmanager
    def session(self):
        answers = iter(self.answers)
        yield lambda image: next(answers)


def test_hungarian_pgs_round_trips_every_accented_letter(media, packs, ffmpeg, tmp_path):
    result = pipeline.run_ocr(media["pgs"], 1, "hun", TesseractEngine(packs), parent=str(tmp_path),
                              ffmpeg=ffmpeg)

    assert _texts(result.srt) == ["árvíztűrő tükörfúrógép", "ÁRVÍZTŰRŐ TÜKÖRFÚRÓGÉP",
                                  "Öt szép szűz lány őrült írót nyúz."]
    assert set(HUNGARIAN_LETTERS) <= set("".join(_texts(result.srt)))
    assert (result.cues, result.dropped, result.engine) == (3, 0, "tesseract")
    assert os.listdir(tmp_path) == []


def test_hungarian_vobsub_round_trips_every_accented_letter(media, packs, ffmpeg, tmp_path):
    result = pipeline.run_ocr(media["vobsub"], 0, "hun", TesseractEngine(packs), parent=str(tmp_path),
                              ffmpeg=ffmpeg)

    assert _texts(result.srt) == ["árvíztűrő tükörfúrógép", "ÁRVÍZTŰRŐ TÜKÖRFÚRÓGÉP",
                                  "Öt szép szűz lány őrült írót nyúz."]
    assert os.listdir(tmp_path) == []


def test_english_pgs_keeps_line_breaks_and_times(media, packs, ffmpeg, tmp_path):
    result = pipeline.run_ocr(media["pgs"], 0, "eng", TesseractEngine(packs), parent=str(tmp_path),
                              ffmpeg=ffmpeg)

    events = pysubs2.SSAFile.from_string(result.srt).events
    assert [e.plaintext for e in events] == ["Hello there, how are you?", "We need to leave\nbefore the storm.",
                                             "I don't know."]
    assert [(e.start, e.end) for e in events] == [(1000, 3500), (5000, 7250), (9000, 11000)]


def test_ocr_fixes_run_in_memory_on_the_recognised_text(media, ffmpeg, tmp_path):
    engine = FakeEngine(["Hello there, how are you?", "We need to leave\nbefore the storm.", "l don't know."])

    result = pipeline.run_ocr(media["pgs"], 0, "eng", engine, parent=str(tmp_path), ffmpeg=ffmpeg)

    assert _texts(result.srt)[-1] == "I don't know."
    assert engine.prepared == "eng"


def test_blank_cues_fail_the_drop_rate_guard_with_the_real_engine(packs, ffmpeg, tmp_path):
    # On screen, so ffmpeg renders them as cues, but with nothing to read.
    sup = tmp_path / "blank.sup"
    sup.write_bytes(build_sup((1920, 1080), [
        (1000 + 2000 * i, 2000 + 2000 * i, 700, 950, box_rows(400, 40, FAINT)) for i in range(4)]))
    video = mux_mkv(ffmpeg, str(tmp_path / "blank.mkv"), [(str(sup), "copy", "eng")], seconds=10)
    work = tmp_path / "work"

    with pytest.raises(TooLittleText, match="too little text"):
        pipeline.run_ocr(video, 0, "eng", TesseractEngine(packs), parent=str(work), ffmpeg=ffmpeg)
    assert os.listdir(work) == []


@pytest.mark.parametrize("answers,fails", [
    (["a", "b", "c", ""], False),       # one in four empty: at the limit, kept
    (["a", "b", "", ""], True),         # two in four: over the limit
])
def test_drop_rate_guard_threshold(ffmpeg, tmp_path, answers, fails):
    sup = tmp_path / "four.sup"
    sup.write_bytes(build_sup((1920, 1080), [
        (1000 + 2000 * i, 2000 + 2000 * i, 100, 900, box_rows(200 + 50 * i, 40)) for i in range(4)]))
    video = mux_mkv(ffmpeg, str(tmp_path / "four.mkv"), [(str(sup), "copy", "eng")], seconds=10)

    if fails:
        with pytest.raises(TooLittleText):
            pipeline.run_ocr(video, 0, "eng", FakeEngine(answers), parent=str(tmp_path / "w"), ffmpeg=ffmpeg)
    else:
        result = pipeline.run_ocr(video, 0, "eng", FakeEngine(answers), parent=str(tmp_path / "w"),
                                  ffmpeg=ffmpeg)
        assert (result.cues, result.dropped) == (3, 1)


def test_a_repainted_line_becomes_one_cue(ffmpeg, tmp_path):
    rows = box_rows(300, 40)
    grey = [[BLACK] * 300 for _ in range(40)]
    sup = tmp_path / "fade.sup"
    sup.write_bytes(build_sup((1920, 1080), [(1000, 1500, 100, 900, grey), (1500, 3000, 100, 900, rows)]))
    video = mux_mkv(ffmpeg, str(tmp_path / "fade.mkv"), [(str(sup), "copy", "eng")], seconds=4)

    result = pipeline.run_ocr(video, 0, "eng", FakeEngine(["Same line", "Same line"]),
                              parent=str(tmp_path / "w"), ffmpeg=ffmpeg)

    assert [(e.start, e.end, e.plaintext) for e in pysubs2.SSAFile.from_string(result.srt).events] == [
        (1000, 3000, "Same line")]


def test_empty_fade_steps_do_not_count_against_the_guard(ffmpeg, tmp_path):
    cues = [(1000, 1100, 100, 900, box_rows(100, 40)), (1100, 1200, 100, 900, box_rows(120, 40)),
            (1200, 3000, 100, 900, box_rows(140, 40)), (4000, 6000, 100, 900, box_rows(160, 40))]
    sup = tmp_path / "steps.sup"
    sup.write_bytes(build_sup((1920, 1080), cues))
    video = mux_mkv(ffmpeg, str(tmp_path / "steps.mkv"), [(str(sup), "copy", "eng")], seconds=7)

    result = pipeline.run_ocr(video, 0, "eng", FakeEngine(["", "", "Hello", "World"]),
                              parent=str(tmp_path / "w"), ffmpeg=ffmpeg)

    assert (result.cues, result.dropped) == (2, 0)


def test_a_run_of_only_short_empty_cues_still_fails(ffmpeg, tmp_path):
    # Every cue is under SHORT_CUE_MS and reads as nothing: none is counted,
    # but there is no text at all, so the run must not return an empty SRT.
    sup = tmp_path / "flashes.sup"
    sup.write_bytes(build_sup((1920, 1080), [
        (1000 + 1000 * i, 1100 + 1000 * i, 100, 900, box_rows(200 + 50 * i, 40)) for i in range(4)]))
    video = mux_mkv(ffmpeg, str(tmp_path / "flashes.mkv"), [(str(sup), "copy", "eng")], seconds=6)
    work = tmp_path / "w"

    with pytest.raises(TooLittleText, match="too little text"):
        pipeline.run_ocr(video, 0, "eng", FakeEngine([""] * 4), parent=str(work), ffmpeg=ffmpeg)
    assert os.listdir(work) == []


def test_progress_reports_pack_render_and_each_cue(media, ffmpeg, tmp_path):
    steps = []
    pipeline.run_ocr(media["pgs"], 0, "eng", FakeEngine(["a", "b", "c"]), parent=str(tmp_path),
                     ffmpeg=ffmpeg, progress=lambda *step: steps.append(step))

    assert steps[0] == ("pack", 1, 1)
    assert [s for s in steps if s[0] == "recognise"] == [("recognise", 1, 3), ("recognise", 2, 3),
                                                        ("recognise", 3, 3)]


def test_frames_are_removed_when_progress_raises_mid_recognition(media, ffmpeg, tmp_path):
    def cancel(step, value, maximum):
        if step == "recognise":
            raise RuntimeError("job cancelled")

    with pytest.raises(RuntimeError, match="job cancelled"):
        pipeline.run_ocr(media["pgs"], 0, "eng", FakeEngine(["a", "b", "c"]), parent=str(tmp_path),
                         ffmpeg=ffmpeg, progress=cancel)
    assert os.listdir(tmp_path) == []


def test_language_failure_happens_before_anything_is_rendered(media, tmp_path):
    engine = FakeEngine([], fail_prepare=LanguagePackError("No OCR language pack for xxx"))

    with pytest.raises(LanguagePackError):
        pipeline.run_ocr(media["pgs"], 0, "xxx", engine, parent=str(tmp_path / "w"), ffmpeg="/nonexistent")
    assert not (tmp_path / "w").exists()


@pytest.mark.parametrize("raw,clean", [
    ("Hello\n\n", "Hello"),
    ("  one \n two\n", "one\ntwo"),
    ("one\ntwo\nthree\n", "one\ntwo three"),
    ("\n \n", ""),
    (None, ""),
])
def test_recognised_text_is_capped_at_two_lines(raw, clean):
    assert pipeline.clean_text(raw) == clean


# A stand-in for tesserocr, put first on the worker's path, to drive the
# worker protocol without Tesseract: it chatters on stdout, crashes, hangs or
# raises what cysignals raises when told to, and answers with what it was
# given.
FAKE_TESSEROCR = """
import os, signal, time

MODE = os.environ.get("FAKE_TESSEROCR", "")
if MODE == "missing":
    raise ImportError("No module named 'cysignals'")
__version__ = "0.0"


class SignalError(BaseException):
    pass


def tesseract_version():
    return "tesseract 0.0.0\\n leptonica-0.0.0"


class PSM:
    SINGLE_BLOCK = 6


class OEM:
    LSTM_ONLY = 1


class PyTessBaseAPI:
    def __init__(self, path, lang, psm, oem):
        self.lang = lang

    def SetImageBytesBmp(self, data):
        self.size = len(data)

    def GetUTF8Text(self):
        if MODE == "crash":
            os.kill(os.getpid(), signal.SIGSEGV)
        if MODE == "hang":
            time.sleep(600)
        # What cysignals raises once it has jumped out of a call that crashed.
        if MODE == "segfault":
            raise SignalError("Segmentation fault")
        if MODE == "abort":
            raise RuntimeError("Aborted")
        print("chatter from Python")
        os.write(1, b"chatter from C\\n")
        return " ".join([self.lang, str(self.size), os.environ.get("OMP_THREAD_LIMIT", "-"),
                         os.environ.get("CYSIGNALS_CRASH_NDEBUG", "-")])

    def End(self):
        # A Tesseract interrupted mid-call can hang or crash when ended.
        if MODE in ("segfault", "abort"):
            time.sleep(600)
"""


@pytest.fixture
def fake_tesserocr(monkeypatch, tmp_path):
    folder = tmp_path / "fake"
    folder.mkdir()
    (folder / "tesserocr.py").write_text(FAKE_TESSEROCR)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(folder), os.environ.get("PYTHONPATH")])))
    engine = TesseractEngine(str(tmp_path))
    engine.pack = "eng"

    def use(mode=""):
        monkeypatch.setenv("FAKE_TESSEROCR", mode)
        return engine
    return use


def _bmp_size(image):
    buffer = io.BytesIO()
    image.save(buffer, "BMP")
    return len(buffer.getvalue())


def _caught_signals():
    for line in open("/proc/self/status"):
        if line.startswith("SigCgt:"):
            return int(line.split()[1], 16)
    return None


def test_the_worker_answers_through_its_own_channel_in_its_own_environment(fake_tesserocr, monkeypatch):
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    monkeypatch.delenv("CYSIGNALS_CRASH_NDEBUG", raising=False)
    image = Image.new("L", (40, 20), 255)

    with fake_tesserocr().session() as read:
        # Both kinds of stdout chatter went to stderr, not into the answers,
        # and the worker, only the worker, runs with both variables set.
        assert read(image) == f"eng {_bmp_size(image)} 1 1"
        assert read(image) == f"eng {_bmp_size(image)} 1 1"
    assert "OMP_THREAD_LIMIT" not in os.environ and "CYSIGNALS_CRASH_NDEBUG" not in os.environ


def test_a_worker_that_cannot_import_tesserocr_reports_ocr_unavailable(fake_tesserocr):
    with pytest.raises(OcrUnavailable, match="not available on this installation.*cysignals"):
        with fake_tesserocr("missing").session():
            pass


def test_missing_tesserocr_reports_ocr_unavailable_before_anything_starts(monkeypatch, tmp_path):
    real_find_spec = tesseract.importlib.util.find_spec
    monkeypatch.setattr(tesseract.importlib.util, "find_spec",
                        lambda name, *args: None if name == "tesserocr" else real_find_spec(name, *args))
    monkeypatch.setattr(tesseract, "TesseractWorker", None)  # never reached

    assert tesseract.ocr_available() is False
    with pytest.raises(OcrUnavailable, match="not available on this installation"):
        TesseractEngine(str(tmp_path)).prepare("eng")


def test_a_crash_in_tesseract_fails_the_run_and_not_bazarr(fake_tesserocr):
    # Outside the calls cysignals guards, a crash kills the worker.
    with pytest.raises(OcrError, match="Tesseract stopped with signal 11"):
        with fake_tesserocr("crash").session() as read:
            read(Image.new("L", (40, 20), 255))


@pytest.mark.parametrize("mode,error", [
    ("segfault", "SignalError: Segmentation fault"),  # SIGSEGV; SignalError is a BaseException
    ("abort", "RuntimeError: Aborted"),               # SIGABRT
])
def test_a_crash_inside_a_guarded_call_ends_the_worker_without_ending_tesseract(fake_tesserocr, monkeypatch,
                                                                               mode, error):
    workers = []
    real_worker = tesseract.TesseractWorker

    def recording_worker():
        workers.append(real_worker())
        return workers[-1]

    monkeypatch.setattr(tesseract, "TesseractWorker", recording_worker)
    monkeypatch.setattr(tesseract, "ANSWER_TIMEOUT", 10)

    with pytest.raises(OcrError, match=f"Tesseract failed: {error}"):
        with fake_tesserocr(mode).session() as read:
            read(Image.new("L", (40, 20), 255))
    # It answered and left without End(), which hangs here, so close() did
    # not have to kill it.
    assert workers[0].process.returncode == 1


def test_a_tesseract_call_that_never_returns_is_killed(fake_tesserocr, monkeypatch):
    workers = []
    real_worker = tesseract.TesseractWorker

    def recording_worker():
        workers.append(real_worker())
        return workers[-1]

    monkeypatch.setattr(tesseract, "TesseractWorker", recording_worker)
    monkeypatch.setattr(tesseract, "ANSWER_TIMEOUT", 2)

    with pytest.raises(OcrError, match="did not answer within 2 s"):
        with fake_tesserocr("hang").session() as read:
            read(Image.new("L", (40, 20), 255))
    assert workers[0].process.returncode is not None


# Starts a worker whose Tesseract call never returns and is then killed, as
# Bazarr+ can be while that call runs: the worker never sees its input end.
DYING_PARENT = """
import subprocess, sys, time

worker = subprocess.Popen([sys.executable, "-P", "-B", sys.argv[1]], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, start_new_session=True)
worker.stdin.write(b'{"op": "open", "tessdata": ".", "language": "eng"}\\n{"op": "read", "bmp": ""}\\n')
worker.stdin.flush()
worker.stdout.readline()  # the greeting
worker.stdout.readline()  # the pack is open, and the read is on its way
print(worker.pid, flush=True)
time.sleep(600)
"""


def _running(pid):
    """Whether the process exists and has not exited (a zombie has)."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_a_hung_worker_ends_when_the_process_that_started_it_is_gone(fake_tesserocr):
    fake_tesserocr("hang")
    parent = subprocess.Popen([sys.executable, "-c", DYING_PARENT, tesseract.WORKER_PATH],
                              stdout=subprocess.PIPE, text=True)
    worker = None
    try:
        worker = int(parent.stdout.readline())
        parent.kill()
        parent.wait()
        deadline = time.monotonic() + 15
        while _running(worker) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _running(worker), "the worker outlived the process that started it"
    finally:
        parent.kill()
        parent.wait()
        parent.stdout.close()
        if worker and _running(worker):
            os.kill(worker, signal.SIGKILL)

def _blocked_signals(pid, thread):
    with open(f"/proc/{pid}/task/{thread}/status") as handle:
        for line in handle:
            if line.startswith("SigBlk:"):
                return int(line.split()[1], 16)


def test_signals_sent_to_the_worker_still_reach_its_main_thread(fake_tesserocr):
    """cysignals must take a signal in the thread that is inside Tesseract,
    which is the main one; the thread that watches the parent blocks them."""
    fake_tesserocr()
    worker = tesseract.TesseractWorker()
    try:
        pid = worker.process.pid
        others = [int(thread) for thread in os.listdir(f"/proc/{pid}/task") if int(thread) != pid]
        assert len(others) == 1
        assert _blocked_signals(pid, pid) == 0
        blocked = _blocked_signals(pid, others[0])
        for number in (signal.SIGINT, signal.SIGHUP, signal.SIGALRM, signal.SIGTERM, signal.SIGQUIT):
            assert blocked & (1 << (number - 1)), signal.Signals(number).name
    finally:
        worker.close()

def test_a_cancelled_run_stops_its_worker(media, packs, ffmpeg, tmp_path, monkeypatch):
    workers = []
    real_worker = tesseract.TesseractWorker

    def recording_worker():
        workers.append(real_worker())
        return workers[-1]

    def cancel(step, value, maximum):
        if step == "recognise":
            raise RuntimeError("job cancelled")

    monkeypatch.setattr(tesseract, "TesseractWorker", recording_worker)
    with pytest.raises(RuntimeError, match="job cancelled"):
        pipeline.run_ocr(media["pgs"], 0, "eng", TesseractEngine(packs), parent=str(tmp_path), ffmpeg=ffmpeg,
                         progress=cancel)
    # One worker checked the pack in prepare(), one read the cues.
    assert len(workers) == 2
    assert all(worker.process.returncode is not None for worker in workers)
    assert os.listdir(tmp_path) == []


def test_a_job_thread_reads_with_the_latest_tesserocr_without_loading_it_into_bazarr(packs):
    image = Image.new("L", (700, 130), 255)
    ImageDraw.Draw(image).text((20, 20), "HELLO WORLD", fill=0, font=ImageFont.load_default(72))
    handler, caught = signal.getsignal(signal.SIGINT), _caught_signals()
    result = {}

    def job():
        try:
            engine = TesseractEngine(packs)
            engine.prepare("eng")
            with engine.session() as read:
                result["text"] = read(image).strip()
        except BaseException as error:
            result["error"] = repr(error)

    thread = threading.Thread(target=job)
    thread.start()
    thread.join()

    assert result == {"text": "HELLO WORLD"}
    # tesserocr 2.10+ loads cysignals, which would have failed in this thread
    # and taken over the process's signal handlers had it loaded here.
    assert "tesserocr" not in sys.modules and "cysignals" not in sys.modules
    assert signal.getsignal(signal.SIGINT) is handler
    assert _caught_signals() == caught


def test_the_worker_imports_nothing_but_the_standard_library_and_tesserocr():
    """It must run without Bazarr+ on its path, as an OCR plugin's worker will."""
    with open(tesseract.WORKER_PATH, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports in the worker"
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"tesserocr"}, imported


@pytest.mark.parametrize("raw,fixed", [
    ("| don't know.", "I don't know."),
    ("| think | can.", "I think I can."),
    ("|'m here", "I'm here"),
    ("-| know", "-I know"),
    ("Wi|| you", "Will you"),
    ("a|| right", "all right"),
    ("Hé|ló", "Hélló"),
    ("no bars", "no bars"),
])
def test_tesseract_pipes_become_the_letter_they_were(raw, fixed):
    assert fix_bars(raw) == fixed

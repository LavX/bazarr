# coding=utf-8
"""The OCR run end to end: real ffmpeg, real Tesseract, and a stand-in engine."""

import os
import shutil
from contextlib import contextmanager

import pysubs2
import pytest

from ocr_fixtures import BLACK, FAINT, FIXTURES, box_rows, build_sup, mux_mkv
from subtitles.tools.ocr import pipeline
from subtitles.tools.ocr.errors import LanguagePackError, TooLittleText
from subtitles.tools.ocr.tesseract import TesseractEngine, fix_bars, load_tesserocr

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


def test_tesserocr_import_sets_one_openmp_thread(monkeypatch):
    monkeypatch.setenv("OMP_THREAD_LIMIT", "unset below, restored after the test")
    monkeypatch.delenv("OMP_THREAD_LIMIT")

    load_tesserocr()

    assert os.environ["OMP_THREAD_LIMIT"] == "1"


def test_missing_tesserocr_reports_ocr_unavailable(monkeypatch):
    import builtins

    from subtitles.tools.ocr.errors import OcrUnavailable
    from subtitles.tools.ocr.tesseract import ocr_available

    real_import = builtins.__import__

    def no_tesserocr(name, *args, **kwargs):
        if name == "tesserocr":
            raise ImportError("No module named 'tesserocr'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tesserocr)
    with pytest.raises(OcrUnavailable, match="not available on this installation"):
        load_tesserocr()
    assert ocr_available() is False


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

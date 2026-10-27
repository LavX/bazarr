# coding=utf-8
"""One OCR run: language pack, render, recognise, assemble.

The engine is an argument so a second one can be added without touching this
file. An engine has ``name``, ``prepare(language, progress)``, which checks the
language and fetches what it needs before anything is rendered, and
``session()``, a context manager yielding ``read(image) -> str``.

``progress(step, value, maximum)`` is called with the steps ``pack``,
``render`` (position in the stream, called about once a second while ffmpeg
runs, new output or not), ``segment`` (one call per rendered frame read for
cues; maximum 0 once, as ffmpeg exits) and ``recognise`` (one call per cue).
An exception raised by it, a cancelled job for instance, aborts the run and
still removes the rendered frames.
"""

import logging
import os
from dataclasses import dataclass

from .decode import decode_cues, render_workspace, require_ffmpeg
from .errors import OcrError, TooLittleText
from .tessdata import official_alpha3

logger = logging.getLogger(__name__)

MAX_LINES = 2
MAX_DROP_RATE = 0.25
# Blu-ray fades repaint a cue several times a frame or two apart. A step that
# short which reads as nothing is a fade step, not a lost line.
SHORT_CUE_MS = 250
TOO_LITTLE_TEXT = "OCR produced too little text, wrong language pack or unreadable track"


@dataclass(frozen=True)
class OcrResult:
    srt: str
    cues: int
    dropped: int
    engine: str


def work_parent():
    from app.get_args import args

    return os.path.join(args.config_dir, "cache", "ocr")


def clean_text(text):
    """Tesseract's lines, blank lines removed, anything past the second joined onto it."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [" ".join(lines[MAX_LINES - 1:])]
    return "\n".join(lines)


def assemble(timed_texts, language, apply_fixes=True):
    """SRT text from ``[(start_ms, end_ms, text)]``, with the OCR Fixes mod applied in memory."""
    import pysubs2

    subs = pysubs2.SSAFile()
    for start, end, text in timed_texts:
        subs.append(pysubs2.SSAEvent(start=start, end=end, text=text.replace("\n", r"\N")))
    srt = subs.to_string("srt")
    if not apply_fixes:
        return srt

    from subzero.language import Language
    from subzero.modification import SubtitleModifications

    alpha3 = official_alpha3(language)
    if not alpha3:
        return srt
    modifications = SubtitleModifications()
    if not modifications.load(content=srt, language=Language(alpha3), mods=["OCR_fixes"]):
        return srt
    modifications.modify("OCR_fixes")
    return modifications.f.to_string("srt")


def run_ocr(video_path, stream_index, language, engine, *, progress=None, canvas_size=None,
            parent=None, ffmpeg=None):
    """OCR one bitmap subtitle stream of ``video_path`` into SRT text."""
    require_ffmpeg(ffmpeg)
    report = progress or (lambda step, value, maximum: None)
    engine.prepare(language, progress=report)
    report("pack", 1, 1)

    with render_workspace(parent or work_parent()) as work_dir:
        cues = decode_cues(video_path, stream_index, work_dir, canvas_size=canvas_size,
                           progress=report, ffmpeg=ffmpeg)
        if not cues:
            raise OcrError(f"Subtitle stream {stream_index} rendered no images to read")
        timed = []
        counted = dropped = 0
        with engine.session() as read:
            for index, cue in enumerate(cues, 1):
                text = clean_text(read(cue.image()))
                if text and timed and timed[-1][2] == text and cue.start_ms <= timed[-1][1]:
                    # The same line repainted (a fade or a palette change): one
                    # cue, already counted when it first appeared.
                    timed[-1] = (timed[-1][0], cue.end_ms, text)
                elif text:
                    timed.append((cue.start_ms, cue.end_ms, text))
                    counted += 1
                elif cue.end_ms - cue.start_ms >= SHORT_CUE_MS:
                    counted += 1
                    dropped += 1
                report("recognise", index, len(cues))

    # No text at all fails even when every empty cue was too short to count.
    if not timed or dropped > counted * MAX_DROP_RATE:
        raise TooLittleText(TOO_LITTLE_TEXT)
    logger.info("OCR read %d cues from stream %s of %s (%d empty)", len(timed), stream_index,
                video_path, dropped)
    return OcrResult(srt=assemble(timed, language), cues=len(timed), dropped=dropped, engine=engine.name)

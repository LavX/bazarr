# coding=utf-8
"""Tesseract, in process through the tesserocr wheel.

tesserocr is imported only when OCR runs. It ships wheels for Linux x86_64 and
aarch64 (and macOS 15 and later, installed by hand), so an installation
without it keeps working and reports OCR as unavailable instead.
"""

import os
import re
from contextlib import contextmanager

from .errors import OcrUnavailable
from .tessdata import ensure_pack, tessdata_dir

UNAVAILABLE = ("OCR is not available on this installation: the tesserocr package is not installed. "
               "It ships for Linux x86_64 and aarch64, which the Docker image covers.")


# Tesseract reads the plain vertical stroke of a sans-serif I or l as a pipe,
# which no subtitle contains. A pipe standing alone (or before an apostrophe)
# was the pronoun I; one touching letters or another pipe was an l.
_LONE_BAR = re.compile(r"(?<![\w|])\|(?![\w|])")
_BAR = re.compile(r"\|")


def fix_bars(text):
    return _BAR.sub("l", _LONE_BAR.sub("I", text or ""))


def load_tesserocr():
    """Import tesserocr, one OCR thread per job, or raise OcrUnavailable."""
    # Read by an OpenMP runtime when the library loads, so it has to be set
    # before the import. The pinned 2.9.2 wheels are built without OpenMP,
    # which makes this a guard for a future build rather than a change today.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    try:
        import tesserocr
    except ImportError as error:
        raise OcrUnavailable(UNAVAILABLE) from error
    return tesserocr


def ocr_available():
    try:
        load_tesserocr()
    except OcrUnavailable:
        return False
    return True


class TesseractEngine:
    """The one engine. ``pipeline.run_ocr`` takes any object with this shape."""

    name = "tesseract"

    def __init__(self, directory=None):
        self.directory = directory
        self.pack = None

    def prepare(self, language, progress=None):
        """Check the language and fetch its pack. Runs before anything is rendered."""
        load_tesserocr()
        self.directory = self.directory or tessdata_dir()
        # ensure_pack names the pack before it touches the network, so an
        # unknown language fails here without a download.
        self.pack = ensure_pack(language, self.directory, progress=progress)

    @contextmanager
    def session(self):
        """Yields ``read(image) -> str`` backed by one loaded Tesseract instance."""
        tesserocr = load_tesserocr()
        api = tesserocr.PyTessBaseAPI(path=self.directory, lang=self.pack,
                                      psm=tesserocr.PSM.SINGLE_BLOCK, oem=tesserocr.OEM.LSTM_ONLY)
        try:
            def read(image):
                api.SetImage(image)
                return fix_bars(api.GetUTF8Text())
            yield read
        finally:
            api.End()

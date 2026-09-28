# coding=utf-8
"""Tesseract for one OCR run, in a process of its own.

tesseract.py starts this file with Bazarr+'s interpreter. It imports only the
standard library and tesserocr, never Bazarr+, so it can become the worker of
an OCR plugin later without changes. One JSON object per line:

    <- {"ok": true, "tesserocr": "2.11.0", "tesseract": "5.5.1"}   on start
    -> {"op": "open", "tessdata": "/config/tessdata", "language": "eng"}
    <- {"ok": true}
    -> {"op": "read", "bmp": "<base64 of a BMP image>"}
    <- {"ok": true, "text": "..."}

A failure answers {"ok": false, "error": "unavailable" or "failed",
"message": "..."}, and the process then exits. "unavailable" replaces the
greeting when tesserocr cannot be imported. The process also exits when its
input closes.
"""

import base64
import json
import os
import sys


def main():
    # Answers go out on a copy of stdout, and stdout itself then points at
    # stderr, so text that Tesseract, Leptonica or Python prints cannot break
    # an answer.
    answers = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def send(message):
        answers.write(json.dumps(message) + "\n")
        answers.flush()

    try:
        import tesserocr
    except ImportError as error:
        send({"ok": False, "error": "unavailable", "message": str(error)})
        return 0
    send({"ok": True, "tesserocr": tesserocr.__version__,
          "tesseract": tesserocr.tesseract_version().split()[1]})

    api = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["op"] == "open":
                if api is not None:
                    api.End()
                api = tesserocr.PyTessBaseAPI(path=request["tessdata"], lang=request["language"],
                                              psm=tesserocr.PSM.SINGLE_BLOCK, oem=tesserocr.OEM.LSTM_ONLY)
                send({"ok": True})
            elif request["op"] == "read":
                # The same BMP bytes tesserocr's SetImage makes from a PIL image.
                api.SetImageBytesBmp(base64.b64decode(request["bmp"]))
                send({"ok": True, "text": api.GetUTF8Text()})
            else:
                raise ValueError(f"unknown operation {request['op']!r}")
        except BaseException as error:
            # A crash inside Recognize or GetUTF8Text arrives here as an
            # exception: cysignals jumps out of the native call and raises
            # SignalError (a BaseException) for SIGSEGV, SIGBUS and SIGILL,
            # RuntimeError for SIGABRT and FloatingPointError for SIGFPE.
            # That Tesseract must not be used or even ended again, and
            # Bazarr+ ends the run on any failed answer, so answer and leave:
            # os._exit skips both End() and the finalizer that would call it.
            try:
                send({"ok": False, "error": "failed", "message": f"{type(error).__name__}: {error}"})
            finally:
                os._exit(1)
    if api is not None:
        api.End()
    return 0


if __name__ == "__main__":
    sys.exit(main())

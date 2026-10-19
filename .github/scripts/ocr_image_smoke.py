"""Smoke-test OCR inside a built Bazarr+ image, the way an OCR job runs it:
from a worker thread, start the image's Tesseract worker process and read one
line with the pinned eng pack. Needs network access.

    docker run --rm -i --platform linux/arm64 --entrypoint python IMAGE - < .github/scripts/ocr_image_smoke.py

OCR_WORKER_PREFIX runs only the worker, where Tesseract runs, under a command
such as "qemu-x86_64 -cpu core2duo".
"""
import base64
import hashlib
import io
import json
import os
import runpy
import shlex
import subprocess
import sys
import tempfile
import threading
import urllib.request

from PIL import Image, ImageDraw, ImageFont

OCR = os.environ.get("OCR_PACKAGE", "/app/bazarr/bazarr/subtitles/tools/ocr")
table = runpy.run_path(os.environ.get("OCR_CHECKSUMS", os.path.join(OCR, "tessdata_checksums.py")))
size, sha256 = table["TESSDATA_CHECKSUMS"]["eng"]
url = f"https://raw.githubusercontent.com/{table['TESSDATA_REPOSITORY']}/{table['TESSDATA_TAG']}/eng.traineddata"
data = urllib.request.urlopen(url, timeout=120).read()
assert len(data) == size and hashlib.sha256(data).hexdigest() == sha256, "eng pack checksum mismatch"
packs = tempfile.mkdtemp()
with open(os.path.join(packs, "eng.traineddata"), "wb") as handle:
    handle.write(data)

image = Image.new("L", (700, 130), 255)
ImageDraw.Draw(image).text((20, 20), "HELLO WORLD", fill=0, font=ImageFont.load_default(72))
bmp = io.BytesIO()
image.save(bmp, "BMP")
requests = [{"op": "open", "tessdata": packs, "language": "eng"},
            {"op": "read", "bmp": base64.b64encode(bmp.getvalue()).decode("ascii")}]
command = shlex.split(os.environ.get("OCR_WORKER_PREFIX", "")) + [
    sys.executable, "-P", "-B", os.path.join(OCR, "tesseract_worker.py")]
result = {}


def job():
    try:
        worker = subprocess.run(command, input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, timeout=900,
                                env=dict(os.environ, OMP_THREAD_LIMIT="1"))
        result["answers"] = [json.loads(line) for line in worker.stdout.splitlines()]
        result["returncode"] = worker.returncode
        result["stderr"] = worker.stderr[-500:]
    except BaseException as error:
        result["error"] = repr(error)


thread = threading.Thread(target=job)
thread.start()
thread.join()
answers = result.get("answers", [])
if (result.get("returncode") != 0 or len(answers) != 3 or not all(a.get("ok") for a in answers)
        or answers[2]["text"].strip() != "HELLO WORLD" or "tesserocr" in sys.modules):
    sys.exit(f"OCR smoke failed: {result}")
print(f"OCR smoke passed on {os.uname().machine}: tesserocr {answers[0]['tesserocr']}, "
      f"tesseract {answers[0]['tesseract']}")

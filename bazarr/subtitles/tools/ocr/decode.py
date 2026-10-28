# coding=utf-8
"""Render a bitmap subtitle stream to images and cut it into cues.

ffmpeg draws the stream onto a transparent canvas and writes one PNG per
change on screen: the cue appearing, and a blank frame when it clears. Each
change is written twice in practice (the old picture one tick before, the new
one at the change), so a cue is a run of identical non-blank frames: it starts
at the first of them and ends at the first frame that differs. Timestamps come
from ffmpeg's own per-frame log in the stream's time base, which is 1/90000 in
ffmpeg 6.1 and 1/1000000 from 7.0, so nothing here assumes either.

Cue times count from the container's start time, the moment a player shows as
0:00. ffmpeg keeps the packets' own timestamps (-copyts) and the start time
read with ffprobe is subtracted here: without -copyts, ffmpeg re-bases an
MPEG-TS or .m2ts input to the first packet of the streams it reads, which for
a subtitle stream alone is its first cue, and every cue would move earlier.

The frames live in a temporary directory that is removed when the caller's
``render_workspace`` block ends, whether it succeeds or raises. Nothing is
cached between runs.
"""

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction

from .errors import OcrError, OcrUnavailable

logger = logging.getLogger(__name__)

FRAME_LOG = "frames.txt"
RENDER_TIMEOUT = 3 * 60 * 60
PROBE_TIMEOUT = 120
# The flush frame ffmpeg emits at end of stream carries a wrapped timestamp,
# 2**32 ms past the start. No real cue is anywhere near 2**31 ms (24 days).
WRAPPED_MS = 2 ** 31
# A cue still on screen when the stream ends has no blank frame to close it.
OPEN_CUE_MS = 5000
# Tesseract reads glyphs of about 30 px best. DVD crops are smaller.
UPSCALE_BELOW_PX = 60
UPSCALE_TARGET_PX = 90
PADDING_PX = 12
# A workspace this old was left by a run that never reached its cleanup (the
# process was killed); no OCR run takes this long.
STALE_WORKSPACE_S = 6 * 60 * 60
# -fps_mode arrived in ffmpeg 5.1 and -stats_enc_pre in 6.1. An older ffmpeg
# stops at the first of them it does not know, with these words.
_OPTION_ERRORS = ("Unrecognized option", "Option not found")


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    frame: str
    bbox: tuple

    def image(self):
        return prepare_image(self.frame, self.bbox)


def _sweep_stale_workspaces(parent):
    cutoff = time.time() - STALE_WORKSPACE_S
    for entry in os.scandir(parent):
        if entry.name.startswith("ocr-") and entry.is_dir(follow_symlinks=False) \
                and entry.stat(follow_symlinks=False).st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)


@contextmanager
def render_workspace(parent):
    """A fresh directory under ``parent``, removed on exit."""
    os.makedirs(parent, exist_ok=True)
    _sweep_stale_workspaces(parent)
    path = tempfile.mkdtemp(prefix="ocr-", dir=parent)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def render_command(ffmpeg, video_path, stream_index, out_dir, canvas_size=None):
    # -copyts: frame times stay the packets' own; read_frame_log subtracts the
    # container's start time (see the module docstring).
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
               "-threads", "1", "-filter_threads", "1", "-copyts"]
    if canvas_size:
        command += ["-canvas_size", f"{canvas_size[0]}x{canvas_size[1]}"]
    command += [
        "-i", video_path,
        "-filter_complex", f"[0:s:{stream_index}]copy[v]", "-map", "[v]",
        "-fps_mode", "passthrough",
        # Full-canvas frames are mostly transparent; PNG's default paeth
        # prediction costs four times the render itself and saves nothing here.
        "-threads", "1", "-c:v", "png", "-pred", "none", "-compression_level", "1",
        "-stats_enc_pre", os.path.join(out_dir, FRAME_LOG), "-stats_enc_pre_fmt", "{n} {pts} {tb}",
        "-f", "image2", "-start_number", "0", os.path.join(out_dir, "%08d.png"),
    ]
    return command


def require_ffmpeg(ffmpeg=None):
    """Resolve ffmpeg and check the version needed for per-frame timestamps."""
    if ffmpeg is None:
        from utilities.binaries import get_binary
        try:
            ffmpeg = get_binary("ffmpeg")
        except Exception as error:
            raise OcrUnavailable("OCR needs ffmpeg and ffprobe, and Bazarr+ could not find them") from error
    unreadable = "OCR needs ffmpeg 6.1 or newer, but could not read its version"
    try:
        completed = subprocess.run([ffmpeg, "-version"], stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, errors="replace", timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise OcrUnavailable(unreadable) from error
    version = re.search(r"^ffmpeg version n?(\d+\.\d+(?:\.\d+)?)", completed.stdout)
    if completed.returncode != 0 or version is None:
        raise OcrUnavailable(unreadable)
    found = version.group(1)
    if tuple(int(part) for part in found.split(".")[:2]) < (6, 1):
        raise OcrUnavailable(f"OCR needs ffmpeg 6.1 or newer, found {found}")
    return ffmpeg


def _ffprobe_beside(ffmpeg):
    """The ffprobe that ships with ``ffmpeg``, else the one on PATH."""
    sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if os.path.dirname(ffmpeg) and os.path.isfile(sibling):
        return sibling
    return shutil.which("ffprobe") or "ffprobe"


def input_start_ms(video_path, ffprobe):
    """The container's start time in ms, the moment players show as 0:00."""
    command = [ffprobe, "-v", "error", "-show_entries", "format=start_time",
               "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   errors="replace", timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise OcrError(f"ffprobe could not read the start time of the video: {error}") from error
    if completed.returncode != 0:
        reason = completed.stderr.strip().splitlines()[-1:] or ["no error output"]
        raise OcrError(f"ffprobe could not read the start time of the video: {reason[0]}")
    try:
        return round(Fraction(completed.stdout.strip()) * 1000)
    except (ValueError, ZeroDivisionError):
        return 0  # N/A: the container has no start time, so its timestamps count from zero


def progress_position(previous, sample, maximum):
    """The render position after a sample: never backward, never out of range."""
    if maximum > 0 and 0 <= sample <= maximum and sample > previous:
        return sample
    return previous


# No real subtitle stream or container runs longer than this; a larger number is
# a damaged tag, and render progress is only a hint.
_MAX_SPAN_SECONDS = 7 * 24 * 3600


def _seconds(value):
    """Seconds from ffprobe's number or an ``HH:MM:SS.fff`` tag, or None when it is not a sane time.

    "N/A", a tag that is not a time, infinity, NaN and absurdly large values all
    give None, as does anything negative.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if ":" in text:
            hours, minutes, seconds = text.split(":")
            seconds_total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        else:
            seconds_total = float(text)
    except (ValueError, OverflowError):
        return None  # "N/A", or a tag that is not a time
    if not math.isfinite(seconds_total) or not 0 <= seconds_total <= _MAX_SPAN_SECONDS:
        return None
    return seconds_total


def subtitle_clock(video_path, stream_index, ffprobe):
    """The subtitle stream's start and span in ms, for render progress.

    ``start_time`` is the stream's own first timestamp, not the container's;
    transport streams often start well after zero. Matroska does not store a
    stream's duration (ffprobe prints N/A), so the span falls back to the
    stream's DURATION tag, then to what is left of the container after the
    stream starts. A probe that fails returns (0, 0) and the job keeps a
    running message without a percentage.
    """
    command = [ffprobe, "-v", "error", "-select_streams", f"s:{stream_index}",
               "-show_entries", "stream=start_time,duration:stream_tags=DURATION:format=duration",
               "-of", "json", video_path]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, errors="replace", timeout=PROBE_TIMEOUT)
        if completed.returncode != 0:
            return 0, 0
        report = json.loads(completed.stdout or "{}")
        stream = (report.get("streams") or [{}])[0]
        start = _seconds(stream.get("start_time")) or 0
        duration = _seconds(stream.get("duration"))
        if not duration or duration <= 0:
            duration = _seconds((stream.get("tags") or {}).get("DURATION"))
        if not duration or duration <= 0:
            container = _seconds((report.get("format") or {}).get("duration"))
            duration = container - start if container else None
        if not duration or duration <= 0:
            return 0, 0
        return int(start * 1000), int(duration * 1000)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, OverflowError):
        return 0, 0


def last_frame_ms(log, start_ms=0):
    """The last rendered frame's time from the container's origin, in ms, or 0."""
    try:
        with open(log, encoding="utf-8", errors="replace") as handle:
            lines = [line for line in handle if line.strip()]
        if not lines:
            return 0
        _number, pts, timebase = lines[-1].split()[:3]
        numerator, denominator = timebase.split("/")
        return int(int(pts) * int(numerator) / int(denominator) * 1000) - start_ms
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        return 0


def render_frames(video_path, stream_index, out_dir, *, canvas_size=None, progress=None,
                  ffmpeg=None, ffprobe=None, timeout=RENDER_TIMEOUT):
    """Run ffmpeg; return ``[(time_ms, png_path)]`` in render order."""
    if ffmpeg is None:
        from utilities.binaries import get_binary
        try:
            ffmpeg = get_binary("ffmpeg")
            ffprobe = ffprobe or get_binary("ffprobe")
        except Exception as error:
            raise OcrUnavailable("OCR needs ffmpeg and ffprobe, and Bazarr+ could not find them") from error
    ffprobe = ffprobe or _ffprobe_beside(ffmpeg)
    clock_start, render_duration = subtitle_clock(video_path, stream_index, ffprobe) if progress else (0, 0)
    command = render_command(ffmpeg, video_path, stream_index, out_dir, canvas_size)
    log = os.path.join(out_dir, FRAME_LOG)
    last_position = 0
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True, errors="replace") as process:
        try:
            while True:
                try:
                    _, stderr = process.communicate(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() > deadline:
                        raise OcrError(f"Rendering subtitle stream {stream_index} took longer than "
                                       f"{timeout // 60} minutes")
                    if progress:
                        elapsed = last_frame_ms(log, clock_start)
                        # Keep the last valid position: a partial log line or a
                        # wrapped flush timestamp must not move the ring back or
                        # stop the cancellation check from running.
                        last_position = progress_position(last_position, elapsed, render_duration)
                        progress("render", last_position, render_duration)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    if process.returncode != 0:
        stderr = (stderr or "").strip()
        if any(marker in stderr for marker in _OPTION_ERRORS):
            logger.warning("OCR cannot use %s: %s", ffmpeg, stderr)
            raise OcrUnavailable("OCR needs ffmpeg 6.1 or newer; the ffmpeg Bazarr+ uses is older")
        reason = stderr.splitlines()[-1:] or ["no error output"]
        raise OcrError(f"ffmpeg could not render subtitle stream {stream_index}: {reason[0]}")
    return read_frame_log(log, out_dir, input_start_ms(video_path, ffprobe))


def read_frame_log(log, out_dir, start_ms=0):
    """``[(time_ms, png_path)]`` from the frame log, counted from ``start_ms``."""
    frames = []
    last = None
    try:
        with open(log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return frames
    for line in lines:
        try:
            number, pts, time_base = line.split()
            numerator, denominator = (int(part) for part in time_base.split("/"))
            time_ms = round(Fraction(int(pts) * numerator * 1000, denominator)) - start_ms
            number = int(number)
        except ValueError:
            continue
        if time_ms >= WRAPPED_MS or (last is not None and time_ms < last):
            logger.debug("OCR dropped frame %s at %s ms (end-of-stream flush)", number, time_ms)
            continue
        path = os.path.join(out_dir, f"{number:08d}.png")
        if os.path.exists(path):
            frames.append((time_ms, path))
            last = time_ms
    return frames


def _signature(path):
    from PIL import Image

    with Image.open(path) as frame:
        frame = frame.convert("RGBA")
        bbox = frame.getchannel("A").getbbox()
        if bbox is None:
            return None, None
        digest = hashlib.blake2b(frame.crop(bbox).tobytes(), digest_size=16).digest()
    return (bbox, digest), bbox


def segment(frames):
    """Cues from ``[(time_ms, png_path)]``: runs of one picture, closed by the next change."""
    cues = []
    current = None  # (start_ms, signature, path, bbox)
    for time_ms, path in frames:
        signature, bbox = _signature(path)
        if current is not None and signature == current[1]:
            continue
        if current is not None and time_ms > current[0]:
            cues.append(Cue(current[0], time_ms, current[2], current[3]))
        current = (time_ms, signature, path, bbox) if signature is not None else None
    if current is not None:
        cues.append(Cue(current[0], current[0] + OPEN_CUE_MS, current[2], current[3]))
    return cues


def prepare_image(path, bbox):
    """Crop, put the text dark on white, and enlarge small (DVD) crops."""
    from PIL import Image, ImageOps

    with Image.open(path) as frame:
        crop = frame.convert("RGBA").crop(bbox)
    flat = Image.new("RGBA", crop.size, (0, 0, 0, 255))
    flat.alpha_composite(crop)
    gray = ImageOps.invert(flat.convert("L"))
    if gray.height < UPSCALE_BELOW_PX:
        factor = max(2, math.ceil(UPSCALE_TARGET_PX / gray.height))
        gray = gray.resize((gray.width * factor, gray.height * factor), Image.Resampling.LANCZOS)
    return ImageOps.expand(gray, border=PADDING_PX, fill=255)


def decode_cues(video_path, stream_index, work_dir, *, canvas_size=None, progress=None, ffmpeg=None):
    frames = render_frames(video_path, stream_index, work_dir, canvas_size=canvas_size,
                           progress=progress, ffmpeg=ffmpeg)
    return segment(frames)

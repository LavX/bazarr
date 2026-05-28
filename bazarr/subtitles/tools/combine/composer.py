# coding=utf-8

import logging
from copy import copy
from io import StringIO

import pysubs2

from .aligner import AlignmentMode, detect_mode


def compose(primary_path, secondary_paths, format):
    """Compose primary + 1-2 secondaries into one SRT or ASS file.

    Returns the file contents as bytes (UTF-8, no BOM, LF line endings).
    Pure function: no disk writes here.
    """
    if format not in ("srt", "ass"):
        raise ValueError(f"invalid format: {format!r}")
    if not secondary_paths or len(secondary_paths) > 2:
        raise ValueError("secondary_paths must have 1 or 2 entries")

    primary = pysubs2.load(primary_path, encoding="utf-8")
    secondaries = [pysubs2.load(p, encoding="utf-8") for p in secondary_paths]

    modes = []
    aligned_secondaries = []
    for sec in secondaries:
        mode = detect_mode(primary.events, sec.events)
        modes.append(mode)
        logging.info(
            "BAZARR combine alignment: kind=%s offset=%dms",
            mode.kind, mode.offset_ms,
        )
        aligned_secondaries.append(_align(primary.events, sec.events, mode))

    if format == "srt":
        return _emit_srt(primary.events, aligned_secondaries)
    return _emit_ass(primary.events, aligned_secondaries)


def _align(primary_events, secondary_events, mode):
    """Return a list of secondary texts aligned to primary cues.
    Each entry corresponds to primary_events[i]; empty string when no match."""
    if mode.kind == "sibling":
        return _align_sibling(primary_events, secondary_events)
    if mode.kind == "offset":
        return _align_sibling(primary_events, _shift(secondary_events, mode.offset_ms))
    return _align_overlap(primary_events, secondary_events)


def _shift(events, offset_ms):
    shifted = []
    for e in events:
        c = copy(e)
        c.start = e.start - offset_ms
        c.end = e.end - offset_ms
        shifted.append(c)
    return shifted


def _align_sibling(primary_events, secondary_events):
    out = []
    for i, _ in enumerate(primary_events):
        if i < len(secondary_events):
            out.append(secondary_events[i].plaintext or "")
        else:
            out.append("")
    return out


def _align_overlap(primary_events, secondary_events):
    OVERLAP_MIN_MS = 100
    out = []
    for p in primary_events:
        best = None
        best_ovl = 0
        for s in secondary_events:
            ovl = max(0, min(p.end, s.end) - max(p.start, s.start))
            if ovl > best_ovl:
                best = s
                best_ovl = ovl
        if best is not None and best_ovl >= OVERLAP_MIN_MS:
            out.append(best.plaintext or "")
        else:
            out.append("")
    return out


def _emit_srt(primary_events, aligned_secondaries):
    buf = StringIO()
    for idx, p in enumerate(primary_events):
        buf.write(f"{idx + 1}\n")
        buf.write(f"{_ts(p.start)} --> {_ts(p.end)}\n")
        buf.write((p.plaintext or "").strip())
        for sec_texts in aligned_secondaries:
            text = sec_texts[idx].strip()
            if text:
                buf.write("\n")
                buf.write(text)
        buf.write("\n\n")
    return buf.getvalue().encode("utf-8")


def _ts(ms):
    total = int(ms)
    h, rem = divmod(total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _emit_ass(primary_events, aligned_secondaries):
    raise NotImplementedError("ASS output added in next task")

# coding=utf-8

import logging
from copy import copy
from io import StringIO

import pysubs2

from .aligner import detect_mode


def _load_subtitle(path):
    """Load a subtitle, detecting its on-disk encoding rather than assuming UTF-8.
    Bazarr indexes legacy-encoded externals (Windows-1250/1252, Latin-1) elsewhere,
    and a hard utf-8 load raised UnicodeDecodeError and failed the whole combine."""
    encoding = "utf-8"
    try:
        from charset_normalizer import from_path
        match = from_path(path).best()
        if match and match.encoding:
            encoding = match.encoding
    except Exception:
        logging.debug("BAZARR combine could not detect encoding for %s, using utf-8", path)
    return pysubs2.load(path, encoding=encoding)


def compose(primary_path, secondary_paths, format):
    """Compose primary + 1-2 secondaries into one SRT or ASS file.

    Returns the file contents as bytes (UTF-8, no BOM, LF line endings).
    Pure function: no disk writes here.
    """
    if format not in ("srt", "ass"):
        raise ValueError(f"invalid format: {format!r}")
    if not secondary_paths or len(secondary_paths) > 2:
        raise ValueError("secondary_paths must have 1 or 2 entries")

    primary = _load_subtitle(primary_path)
    secondaries = [_load_subtitle(p) for p in secondary_paths]

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
    out = []
    out.append("[Script Info]")
    out.append("ScriptType: v4.00+")
    out.append("WrapStyle: 0")
    out.append("ScaledBorderAndShadow: yes")
    out.append("YCbCr Matrix: TV.601")
    out.append("PlayResX: 384")
    out.append("PlayResY: 288")
    out.append("")
    out.append("[V4+ Styles]")
    out.append(
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    out.append(_style_line("Bottom", alignment=2))
    out.append(_style_line("Top", alignment=8))
    out.append(_style_line("Middle", alignment=5))
    out.append("")
    out.append("[Events]")
    out.append(
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text"
    )
    styles = ["Bottom", "Top", "Middle"]
    for idx, p in enumerate(primary_events):
        start = _ass_ts(p.start)
        end = _ass_ts(p.end)
        primary_text = (p.plaintext or "").strip().replace("\n", "\\N")
        if primary_text:
            out.append(f"Dialogue: 0,{start},{end},{styles[0]},,0,0,0,,{primary_text}")
        for si, sec_texts in enumerate(aligned_secondaries):
            text = sec_texts[idx].strip().replace("\n", "\\N")
            if text:
                out.append(
                    f"Dialogue: 0,{start},{end},{styles[si + 1]},,0,0,0,,{text}"
                )
    return ("\n".join(out) + "\n").encode("utf-8")


def _style_line(name, alignment):
    return (
        f"Style: {name},Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,2,0,{alignment},10,10,10,1"
    )


def _ass_ts(ms):
    total = int(ms)
    h, rem = divmod(total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    cs = ms // 10
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

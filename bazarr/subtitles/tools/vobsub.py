# coding=utf-8
"""The .idx half of a VobSub pair.

ffmpeg can read VobSub but has no muxer for it. A .sub is an MPEG-2 program
stream of subpicture packets, which ffmpeg's ``vob`` muxer writes from a
copied ``dvd_subtitle`` stream. The .idx is a text index a player reads first:
the stream's own header (frame size and palette, which Matroska and ffmpeg
keep as the stream's extradata) and one line per subpicture with its start
time and the offset of the pack it starts in.
"""

import json
import struct
import subprocess

from utilities.binaries import get_binary

INDEX_HEADER = "# VobSub index file, v7 (do not modify this line!)"


def subpicture_positions(data):
    """``(start_ms, pack_offset)`` for every subpicture packet in a program stream."""
    positions = []
    pack = None
    position = 0
    size = len(data)
    while position + 6 <= size:
        if data[position:position + 3] != b"\x00\x00\x01":
            position += 1
            continue
        code = data[position + 3]
        if code == 0xBA:
            pack = position
            if data[position + 4] >> 6 == 1:
                # MPEG-2 pack header: 14 bytes plus its stuffing.
                position += 14 + (data[position + 13] & 0x07)
            else:
                position += 12
            continue
        length = struct.unpack(">H", data[position + 4:position + 6])[0]
        # Private stream 1 carries the subpictures; only a packet with a PTS
        # starts one, the rest continue the one before.
        if code == 0xBD and pack is not None and data[position + 7] & 0x80:
            pts = data[position + 9:position + 14]
            ticks = (((pts[0] >> 1) & 0x07) << 30 | pts[1] << 22 | (pts[2] >> 1) << 15
                     | pts[3] << 7 | pts[4] >> 1)
            positions.append((ticks // 90, pack))
        position += 6 + length
    return positions


def _timestamp(ms):
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{ms:03d}"


def index_text(header, language, positions):
    """The .idx for one subpicture stream."""
    lines = [INDEX_HEADER]
    if header:
        lines.append(header.strip())
    lines += ["", f"id: {language}, index: 0"]
    lines += [f"timestamp: {_timestamp(ms)}, filepos: {offset:09x}" for ms, offset in positions]
    return "\n".join(lines) + "\n"


def stream_header(video_path, stream_index):
    """The idx header text a ``dvd_subtitle`` stream carries as extradata, or ""."""
    result = subprocess.run(
        [get_binary("ffprobe"), "-v", "error", "-select_streams", f"s:{stream_index}",
         "-show_entries", "stream=extradata", "-show_data", "-of", "json", video_path],
        capture_output=True, text=True, timeout=120)
    streams = json.loads(result.stdout or "{}").get("streams") or [{}]
    dump = streams[0].get("extradata") or ""
    # ffprobe prints a hex dump: an offset, eight groups of hex, then text.
    raw = b"".join(bytes.fromhex(line[10:50].replace(" ", "")) for line in dump.strip("\n").splitlines())
    return raw.decode("latin-1").replace("\x00", "")


def write_vobsub_index(video_path, stream_index, sub_path, index_path, language):
    """Write the .idx for ``sub_path``, a program stream copied from ``stream_index``."""
    with open(sub_path, "rb") as handle:
        positions = subpicture_positions(handle.read())
    with open(index_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(index_text(stream_header(video_path, stream_index), language[:2], positions))
    return index_path

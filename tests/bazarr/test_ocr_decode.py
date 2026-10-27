# coding=utf-8
"""Rendering bitmap subtitle streams with ffmpeg and cutting them into cues."""

import os
import shutil
import stat
import subprocess
import time

import pytest
from PIL import Image

from ocr_fixtures import ENGLISH_CUES, FIXTURES, HUNGARIAN_CUES, box_rows, build_sup, mux_mkv
from subtitles.tools.ocr import decode
from subtitles.tools.ocr.errors import OcrError, OcrUnavailable

FRAME_MS = 42  # one frame at 23.976 fps


@pytest.fixture(scope="module")
def ffmpeg():
    path = shutil.which("ffmpeg")
    assert path, "the OCR tests need ffmpeg on PATH (CI installs it with apt)"
    return path


@pytest.fixture(scope="module")
def media(ffmpeg, tmp_path_factory):
    folder = tmp_path_factory.mktemp("ocr-media")
    pgs = mux_mkv(ffmpeg, str(folder / "pgs.mkv"), [
        (os.path.join(FIXTURES, "eng_hd.sup"), "copy", "eng"),
        (os.path.join(FIXTURES, "hun_hd.sup"), "copy", "hun"),
    ])
    vobsub = mux_mkv(ffmpeg, str(folder / "vobsub.mkv"), [
        (os.path.join(FIXTURES, "hun_sd.sup"), "dvdsub", "hun"),
    ])
    return {"pgs": pgs, "vobsub": vobsub}


def _times(cues):
    return [(cue.start_ms, cue.end_ms) for cue in cues]


def _assert_within_a_frame(actual, expected):
    assert len(actual) == len(expected), (actual, expected)
    for (start, end), (want_start, want_end, _) in zip(actual, expected):
        assert abs(start - want_start) <= FRAME_MS and abs(end - want_end) <= FRAME_MS, (actual, expected)


def test_pgs_stream_yields_its_cues_with_their_times(ffmpeg, media, tmp_path):
    cues = decode.decode_cues(media["pgs"], 0, str(tmp_path), ffmpeg=ffmpeg)
    _assert_within_a_frame(_times(cues), ENGLISH_CUES)


def test_second_subtitle_stream_is_rendered_by_its_index(ffmpeg, media, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    english = decode.decode_cues(media["pgs"], 0, str(tmp_path / "a"), ffmpeg=ffmpeg)
    hungarian = decode.decode_cues(media["pgs"], 1, str(tmp_path / "b"), ffmpeg=ffmpeg)
    _assert_within_a_frame(_times(hungarian), HUNGARIAN_CUES)
    assert [c.bbox for c in english] != [c.bbox for c in hungarian]


def test_vobsub_stream_yields_its_cues_with_their_times(ffmpeg, media, tmp_path):
    cues = decode.decode_cues(media["vobsub"], 0, str(tmp_path), ffmpeg=ffmpeg)
    _assert_within_a_frame(_times(cues), HUNGARIAN_CUES)


def test_back_to_back_cues_split_on_the_change(ffmpeg, tmp_path):
    sup = tmp_path / "touching.sup"
    sup.write_bytes(build_sup((1920, 1080), [
        (1000, 2000, 100, 900, box_rows(300, 40)),
        (2000, 3000, 100, 900, box_rows(500, 40)),
    ]))
    video = mux_mkv(ffmpeg, str(tmp_path / "touching.mkv"), [(str(sup), "copy", "eng")], seconds=4)
    work = tmp_path / "work"
    work.mkdir()

    assert _times(decode.decode_cues(video, 0, str(work), ffmpeg=ffmpeg)) == [(1000, 2000), (2000, 3000)]


@pytest.mark.parametrize("line,expected", [
    ("0 90000 1/90000", 1000),
    ("0 314999 1/90000", 3500),
    ("0 3499999 1/1000000", 3500),
    ("0 1000 1/1000", 1000),
])
def test_frame_log_is_read_in_its_own_time_base(tmp_path, line, expected):
    (tmp_path / "00000000.png").write_bytes(b"")
    (tmp_path / decode.FRAME_LOG).write_text(line + "\n")

    assert decode.read_frame_log(str(tmp_path / decode.FRAME_LOG), str(tmp_path)) == [
        (expected, str(tmp_path / "00000000.png"))]


def test_frame_times_count_from_the_container_start(tmp_path):
    (tmp_path / "00000000.png").write_bytes(b"")
    (tmp_path / decode.FRAME_LOG).write_text("0 17400000 1/1000000\n")

    assert decode.read_frame_log(str(tmp_path / decode.FRAME_LOG), str(tmp_path), start_ms=11400) == [
        (6000, str(tmp_path / "00000000.png"))]


def test_transport_stream_cues_count_from_the_file_start(ffmpeg, tmp_path):
    # The file starts at 11.4 s (video) and the subtitles 5 s later than in the
    # .sup. Without -copyts ffmpeg would re-base the input to the first cue.
    video = str(tmp_path / "late.m2ts")
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-copyts",
                    "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=20",
                    "-itsoffset", "5", "-fix_sub_duration", "-i", os.path.join(FIXTURES, "eng_hd.sup"),
                    "-map", "0:v", "-map", "1:s", "-c:v", "mpeg4", "-c:s", "copy",
                    "-output_ts_offset", "10", "-f", "mpegts", "-mpegts_m2ts_mode", "1", video],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    work.mkdir()

    cues = decode.decode_cues(video, 0, str(work), ffmpeg=ffmpeg)

    _assert_within_a_frame(_times(cues), [(start + 5000, end + 5000, text) for start, end, text in ENGLISH_CUES])


@pytest.mark.parametrize("printed,expected", [("11.389089", 11389), ("0.000000", 0), ("-0.043000", -43),
                                              ("N/A", 0)])
def test_container_start_is_read_with_ffprobe(tmp_path, printed, expected):
    probe = _fake_ffmpeg(tmp_path, f"echo '{printed}'")

    assert decode.input_start_ms("/v.m2ts", probe) == expected


@pytest.mark.parametrize("flush", ["2 4294978295000 1/1000000", "2 386548046550 1/90000"])
def test_end_of_stream_flush_frame_is_dropped(tmp_path, flush):
    for number in range(3):
        (tmp_path / f"{number:08d}.png").write_bytes(b"")
    (tmp_path / decode.FRAME_LOG).write_text(f"0 1000 1/1000\n1 2000 1/1000\n{flush}\ngarbage line\n")

    frames = decode.read_frame_log(str(tmp_path / decode.FRAME_LOG), str(tmp_path))

    assert [t for t, _ in frames] == [1000, 2000]


def _frame(path, box=None, colour=(255, 255, 255, 255), size=(640, 360)):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    if box:
        image.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), colour), box[:2])
    image.save(path)
    return str(path)


def test_segment_closes_cues_on_blank_frames_and_on_changes(tmp_path):
    a, b = (10, 300, 200, 340), (10, 300, 400, 340)
    frames = [
        (1000, _frame(tmp_path / "0.png")),
        (1000, _frame(tmp_path / "1.png", a)),
        (2999, _frame(tmp_path / "2.png", a)),
        (3000, _frame(tmp_path / "3.png")),
        (5000, _frame(tmp_path / "4.png", b)),
        (6000, _frame(tmp_path / "5.png", a)),
        (7000, _frame(tmp_path / "6.png")),
    ]

    assert _times(decode.segment(frames)) == [(1000, 3000), (5000, 6000), (6000, 7000)]


def test_cue_still_open_at_the_end_gets_a_default_length(tmp_path):
    frames = [(1000, _frame(tmp_path / "0.png", (10, 10, 50, 50)))]

    assert _times(decode.segment(frames)) == [(1000, 1000 + decode.OPEN_CUE_MS)]


def test_prepared_image_is_cropped_inverted_and_padded(tmp_path):
    path = _frame(tmp_path / "f.png", (100, 200, 400, 280))
    image = Image.open(path).convert("RGBA")
    image.paste(Image.new("RGBA", (300, 4), (0, 0, 0, 255)), (100, 200))  # an outline row
    image.save(path)

    prepared = decode.prepare_image(path, (100, 200, 400, 280))

    assert prepared.mode == "L"
    assert prepared.size == (300 + 2 * decode.PADDING_PX, 80 + 2 * decode.PADDING_PX)
    assert prepared.getpixel((0, 0)) == 255  # padding is white
    assert prepared.getpixel((decode.PADDING_PX + 5, decode.PADDING_PX + 1)) == 255  # black outline turns white
    assert prepared.getpixel((decode.PADDING_PX + 5, decode.PADDING_PX + 40)) == 0  # white text turns black


def test_small_dvd_crops_are_upscaled(tmp_path):
    path = _frame(tmp_path / "f.png", (10, 10, 110, 30))

    prepared = decode.prepare_image(path, (10, 10, 110, 30))

    factor = -(-decode.UPSCALE_TARGET_PX // 20)
    assert prepared.size == (100 * factor + 2 * decode.PADDING_PX, 20 * factor + 2 * decode.PADDING_PX)


def test_crops_at_the_threshold_are_left_alone(tmp_path):
    path = _frame(tmp_path / "f.png", (10, 10, 110, 10 + decode.UPSCALE_BELOW_PX))

    prepared = decode.prepare_image(path, (10, 10, 110, 10 + decode.UPSCALE_BELOW_PX))

    assert prepared.height == decode.UPSCALE_BELOW_PX + 2 * decode.PADDING_PX


def test_workspace_is_removed_after_success_and_after_an_exception(tmp_path):
    with decode.render_workspace(str(tmp_path)) as work:
        (open(os.path.join(work, "00000000.png"), "wb")).close()
    assert not os.path.exists(work)

    with pytest.raises(RuntimeError):
        with decode.render_workspace(str(tmp_path)) as work:
            (open(os.path.join(work, "00000000.png"), "wb")).close()
            raise RuntimeError("boom")
    assert not os.path.exists(work)
    assert os.listdir(tmp_path) == []


def test_workspaces_left_by_a_killed_run_are_swept(tmp_path):
    stale = tmp_path / "ocr-stale"
    stale.mkdir()
    (stale / "00000000.png").write_bytes(b"")
    old = time.time() - decode.STALE_WORKSPACE_S - 60
    os.utime(stale, (old, old))
    recent = tmp_path / "ocr-running"
    recent.mkdir()
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()
    os.utime(unrelated, (old, old))

    with decode.render_workspace(str(tmp_path)):
        pass

    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep-me", "ocr-running"]


def test_canvas_size_is_an_input_option(tmp_path):
    command = decode.render_command("ffmpeg", "/v.mkv", 2, str(tmp_path), canvas_size=(720, 576))

    assert command.index("-canvas_size") < command.index("-i")
    assert command.index("-copyts") < command.index("-i")
    assert command[command.index("-canvas_size") + 1] == "720x576"
    assert "[0:s:2]copy[v]" in command
    assert "-canvas_size" not in decode.render_command("ffmpeg", "/v.mkv", 2, str(tmp_path))


def test_missing_stream_is_an_ocr_error(ffmpeg, media, tmp_path):
    with pytest.raises(OcrError, match="stream 5"):
        decode.decode_cues(media["pgs"], 5, str(tmp_path), ffmpeg=ffmpeg)


def _fake_ffmpeg(tmp_path, body):
    script = tmp_path / "ffmpeg"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.mark.parametrize("version", ["4.2.2", "5.1.1-static", "6.0"])
def test_ffmpeg_preflight_reports_the_installed_old_version(tmp_path, version):
    binary = _fake_ffmpeg(tmp_path, f"echo 'ffmpeg version {version} Copyright'")
    found = version.split("-")[0]

    with pytest.raises(OcrUnavailable, match=rf"OCR needs ffmpeg 6\.1 or newer, found {found}$"):
        decode.require_ffmpeg(binary)


@pytest.mark.parametrize("version", ["6.1-static", "6.1.1-ubuntu", "n6.1.1", "7.0", "8.1.3"])
def test_ffmpeg_preflight_accepts_supported_versions(tmp_path, version):
    binary = _fake_ffmpeg(tmp_path, f"test \"$1\" = '-version' || exit 1\necho 'ffmpeg version {version} Copyright'")

    assert decode.require_ffmpeg(binary) == binary


@pytest.mark.parametrize("body", ["exit 1", "echo 'not a version'",
                                "echo 'ffmpeg version 6.1 Copyright'; exit 1"])
def test_ffmpeg_preflight_refuses_an_unreadable_version(tmp_path, body):
    with pytest.raises(OcrUnavailable, match="could not read its version"):
        decode.require_ffmpeg(_fake_ffmpeg(tmp_path, body))


def test_ffmpeg_preflight_reports_a_missing_binary(tmp_path):
    with pytest.raises(OcrUnavailable, match="could not read its version"):
        decode.require_ffmpeg(str(tmp_path / "absent"))


@pytest.mark.parametrize("option", [
    "stats_enc_pre",  # ffmpeg 5.1 to 6.0 (Debian 12)
    "fps_mode",       # before 5.1 (Ubuntu 22.04, the 4.2.2 build get_binary downloads)
])
def test_ffmpeg_older_than_6_1_is_reported_as_unavailable(tmp_path, option):
    old = _fake_ffmpeg(tmp_path, "cat >&2 <<'EOF'\n"
                                 f"Unrecognized option '{option}'.\n"
                                 "Error splitting the argument list: Option not found\n"
                                 "EOF\nexit 1")

    with pytest.raises(OcrUnavailable, match="6.1"):
        decode.render_frames("/v.mkv", 0, str(tmp_path), ffmpeg=old)


def test_a_progress_exception_stops_ffmpeg_and_propagates(tmp_path):
    slow = _fake_ffmpeg(tmp_path, "exec sleep 30")

    def cancelled(step, value, maximum):
        raise RuntimeError("cancelled")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled"):
        decode.render_frames("/v.mkv", 0, str(tmp_path), ffmpeg=slow, progress=cancelled)
    assert time.monotonic() - started < 10


def test_committed_fixtures_are_small_pgs_streams():
    from ocr_fixtures import TEXT_FIXTURES

    for name in TEXT_FIXTURES:
        data = open(os.path.join(FIXTURES, name), "rb").read()
        assert data[:2] == b"PG" and len(data) < 200 * 1024, name

def _probe_says(tmp_path, report):
    import json

    return _fake_ffmpeg(tmp_path, f"printf '%s' '{json.dumps(report)}'")


def test_subtitle_clock_reads_start_and_duration(tmp_path):
    ffprobe = _probe_says(tmp_path, {"streams": [{"start_time": "6.0", "duration": "10.0"}],
                                     "format": {"duration": "99.0"}})
    assert decode.subtitle_clock("/v.mkv", 2, ffprobe) == (6000, 10000)


@pytest.mark.parametrize("report,expected", [
    # FFmpeg-written Matroska: DURATION is the track's end timestamp, so the span
    # is what is left after the start. The container's ENCODER identifies the writer.
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "00:00:11.500000000"}}],
      "format": {"duration": "13.0", "tags": {"ENCODER": "Lavf62.12.103"}}}, (1000, 10500)),
    ({"streams": [{"start_time": "31.0", "duration": "N/A", "tags": {"DURATION": "00:00:41.000000000"}}],
      "format": {"duration": "45.0", "tags": {"ENCODER": "Lavf62.12.103"}}}, (31000, 10000)),
    ({"streams": [{"start_time": "0.0", "duration": "N/A", "tags": {"DURATION": "00:00:41.000000000"}}],
      "format": {"duration": "45.0", "tags": {"ENCODER": "Lavf62.12.103"}}}, (0, 41000)),
    # An FFmpeg tag at or before the stream start leaves no span: use the next source.
    ({"streams": [{"start_time": "31.0", "duration": "N/A", "tags": {"DURATION": "00:00:10.000000000"}}],
      "format": {"duration": "45.0", "tags": {"ENCODER": "Lavf62.12.103"}}}, (31000, 14000)),
    ({"streams": [{"start_time": "31.0", "duration": "N/A", "tags": {"DURATION": "00:00:31.000000000"}}],
      "format": {"tags": {"ENCODER": "Lavf62.12.103"}}}, (0, 0)),
    # Other writers, including MKVToolNix, and absent writer information use the tag as a span.
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "00:00:10.000000000"}}],
      "format": {"duration": "13.0", "tags": {"ENCODER": "mkvmerge (MKVToolNix)"}}}, (1000, 10000)),
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "00:00:10.000000000"}}],
      "format": {"duration": "13.0", "tags": {"ENCODER": "unknown muxer"}}}, (1000, 10000)),
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "00:00:10.000000000"}}],
      "format": {"duration": "13.0"}}, (1000, 10000)),
    # Neither: what is left of the container after the stream starts.
    ({"streams": [{"start_time": "1.0", "duration": "N/A"}], "format": {"duration": "13.0"}}, (1000, 12000)),
    # Nothing usable: a running message without a percentage.
    ({"streams": [{"start_time": "1.0", "duration": "N/A"}], "format": {"duration": "N/A"}}, (0, 0)),
    ({"streams": [{"start_time": "N/A", "duration": "N/A", "tags": {"DURATION": "garbage"}}],
      "format": {}}, (0, 0)),
    ({"streams": []}, (0, 0)),
])
def test_subtitle_clock_finds_a_span_when_the_stream_has_no_duration(tmp_path, report, expected):
    assert decode.subtitle_clock("/v.mkv", 0, _probe_says(tmp_path, report)) == expected


@pytest.mark.parametrize("duration_key,writer_key,writer", [
    ("duration", "encoder", "Lavf62.12.103"),
    ("DuRaTiOn", "EnCoDeR", "FFmpeg 8.1.3"),
    ("duration", "encoder", "a muxer using FFmpeg 8.1.3"),
])
def test_subtitle_clock_matches_tag_names_case_insensitively(tmp_path, duration_key, writer_key, writer):
    report = {"streams": [{"start_time": "1.0", "duration": "N/A",
                           "tags": {duration_key: "00:00:11.000000000"}}],
              "format": {"duration": "13.0", "tags": {writer_key: writer}}}

    assert decode.subtitle_clock("/v.mkv", 0, _probe_says(tmp_path, report)) == (1000, 10000)


@pytest.mark.parametrize("report,expected", [
    # A value that is not a sane time counts as N/A: the next source stands in, or (0, 0).
    ({"streams": [{"start_time": "1.0", "duration": "inf"}], "format": {"duration": "13.0"}}, (1000, 12000)),
    ({"streams": [{"start_time": "1.0", "duration": "inf"}]}, (0, 0)),
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "1e999"}}],
      "format": {"tags": {"ENCODER": "Lavf62.12.103"}}}, (0, 0)),
    ({"streams": [{"start_time": "1.0", "duration": "N/A"}], "format": {"duration": "1e999"}}, (0, 0)),
    ({"streams": [{"start_time": "inf", "duration": "5.0"}]}, (0, 5000)),
    ({"streams": [{"start_time": "1e999", "duration": "N/A"}], "format": {"duration": "13.0"}}, (0, 13000)),
    ({"streams": [{"start_time": "nan", "duration": "nan"}], "format": {"duration": "nan"}}, (0, 0)),
    ({"streams": [{"start_time": "1.0", "duration": "1e12"}]}, (0, 0)),
    ({"streams": [{"start_time": "1.0", "duration": "-5"}], "format": {"duration": "-9"}}, (0, 0)),
    ({"streams": [{"start_time": "-2.0", "duration": "5.0"}]}, (0, 5000)),
])
def test_subtitle_clock_rejects_a_probe_value_that_is_not_a_sane_time(tmp_path, report, expected):
    # Infinity used to overflow int() and end the job with an unexpected error.
    assert decode.subtitle_clock("/v.mkv", 0, _probe_says(tmp_path, report)) == expected


def test_subtitle_clock_reads_the_span_of_a_real_matroska_track(ffmpeg, media):
    # eng_hd.sup starts at 1 s and ends at 11 s. FFmpeg tags the track 11 s,
    # its end timestamp, and ffprobe prints duration=N/A for the stream itself.
    assert decode.subtitle_clock(media["pgs"], 0, shutil.which("ffprobe")) == (1000, 10000)


def test_subtitle_clock_subtracts_the_start_of_a_late_real_matroska_track(ffmpeg, tmp_path):
    # The first cue shows at 3 s and the last one ends at 13 s: FFmpeg tags
    # the track 13 s, and the render counts from the first cue, so the span is 10 s.
    sup = tmp_path / "late.sup"
    sup.write_bytes(build_sup((1920, 1080), [
        (3000, 5000, 100, 900, box_rows(300, 40)),
        (9000, 13000, 100, 900, box_rows(500, 40)),
    ]))
    video = mux_mkv(ffmpeg, str(tmp_path / "late.mkv"), [(str(sup), "copy", "eng")], seconds=16)

    assert decode.subtitle_clock(video, 0, shutil.which("ffprobe")) == (3000, 10000)


def test_subtitle_clock_falls_back_to_zero(tmp_path):
    ffprobe = _fake_ffmpeg(tmp_path, "exit 1")
    assert decode.subtitle_clock("/v.mkv", 2, ffprobe) == (0, 0)


def test_the_render_reports_its_total_for_a_matroska_track(ffmpeg, media, tmp_path):
    # A slow render is not needed: the progress callback is first given the
    # clock's span, so a stand-in ffmpeg that takes a few polls shows it.
    seen = []
    slow = _fake_ffmpeg(tmp_path, "sleep 2.5; exit 1")
    with pytest.raises(OcrError):
        decode.render_frames(media["pgs"], 0, str(tmp_path), ffmpeg=slow, ffprobe=shutil.which("ffprobe"),
                             progress=lambda step, value, maximum: seen.append((step, maximum)))
    assert seen and all(step == "render" and maximum == 10000 for step, maximum in seen)


def test_last_frame_ms_reads_the_frame_log(tmp_path):
    log = tmp_path / "frames.log"
    log.write_text("0 90000 1/90000\n1 180000 1/90000\n")
    assert decode.last_frame_ms(str(log)) == 2000
    assert decode.last_frame_ms(str(log), start_ms=500) == 1500

def test_progress_position_never_moves_backward():
    assert decode.progress_position(0, 5000, 10000) == 5000
    assert decode.progress_position(5000, 0, 10000) == 5000
    assert decode.progress_position(5000, 6000, 10000) == 6000
    assert decode.progress_position(5000, 12000, 10000) == 5000
    assert decode.progress_position(0, 5000, 0) == 0



class _StopAtCall:
    """A progress callback that raises on its ``n``th call, like a job that was stopped."""

    def __init__(self, n):
        self.n, self.calls = n, 0

    def __call__(self, step, value, maximum):
        self.calls += 1
        if self.calls >= self.n:
            raise RuntimeError("cancelled")


@pytest.mark.parametrize("body", [
    # ffmpeg with nothing to say: a long idle stretch, the tail of a stream, a slow filter flush.
    "exec sleep 30",
    # ffmpeg that keeps logging frames.
    "while true; do echo '0 0 1/1000' >> \"$(dirname \"$0\")/frames.txt\"; sleep 0.1; done",
], ids=["silent", "chatty"])
def test_stop_while_ffmpeg_is_silent_or_chatty_ends_the_render_within_seconds(tmp_path, monkeypatch, body):
    monkeypatch.setattr(decode, "POLL_S", 0.1)
    ffmpeg = _fake_ffmpeg(tmp_path, body)
    stop = _StopAtCall(3)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled"):
        decode.render_frames("/v.mkv", 0, str(tmp_path), ffmpeg=ffmpeg, progress=stop)

    # Three polls is well under a second; the bound only has to beat the 30 s of silence.
    assert time.monotonic() - started < 10
    assert stop.calls == 3, "the stop check runs on a timer, not only when ffmpeg logs a frame"


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def test_stop_ends_ffmpegs_whole_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(decode, "POLL_S", 0.1)
    pidfile = tmp_path / "child.pid"
    ffmpeg = _fake_ffmpeg(tmp_path, f"sleep 40 &\necho $! > '{pidfile}'\nwait")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled"):
        decode.render_frames("/v.mkv", 0, str(tmp_path), ffmpeg=ffmpeg, progress=_StopAtCall(5))

    # A child left holding ffmpeg's stderr would keep the reap waiting for its 40 s.
    assert time.monotonic() - started < 20
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "a process ffmpeg started outlived the stop"


def _frames(folder, count):
    paths = []
    for number in range(count):
        path = os.path.join(folder, f"{number:08d}.png")
        Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(path)
        paths.append((number * 100, path))
    return paths


def test_segmenting_the_frames_checks_for_a_stop_between_frames(tmp_path):
    """After ffmpeg exits there is a long read of every frame; Stop must not wait for all of it."""
    frames = _frames(str(tmp_path), 30)
    seen = []

    def stop_at_the_fifth(step, value, maximum):
        seen.append((step, value, maximum))
        if value == 5:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        decode.segment(frames, progress=stop_at_the_fifth)

    assert seen[-1] == ("segment", 5, 30)


def test_the_gap_after_ffmpeg_exits_reports_a_step_so_stop_lands_there(tmp_path):
    ffmpeg = _fake_ffmpeg(tmp_path, "exit 0")  # no ffprobe beside it: reading the start time would fail
    seen = []

    def stop_at_segment(step, value, maximum):
        seen.append(step)
        if step == "segment":
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        decode.decode_cues("/v.mkv", 0, str(tmp_path), ffmpeg=ffmpeg, progress=stop_at_segment)
    assert seen[-1] == "segment"


def test_decode_cues_reports_each_frame_it_reads(media, ffmpeg, tmp_path):
    steps = []
    decode.decode_cues(media["pgs"], 0, str(tmp_path), ffmpeg=ffmpeg,
                       progress=lambda *step: steps.append(step))
    reads = [step for step in steps if step[0] == "segment" and step[2] > 0]
    assert reads and [step[1] for step in reads] == list(range(len(reads)))
    assert all(step[2] == len(reads) for step in reads)


def _wait_gone(pid, seconds=3):
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def test_the_exit_hook_ends_a_render_still_running_and_removes_its_frames(tmp_path, monkeypatch):
    """A terminal Ctrl+C no longer reaches ffmpeg (it has a session of its own), so
    the hook that runs when Bazarr shuts down must end it and clear its frames."""
    import threading

    monkeypatch.setattr(decode, "POLL_S", 0.1)
    pidfile = tmp_path / "child.pid"
    ffmpeg = _fake_ffmpeg(tmp_path, f"sleep 40 &\necho $! > '{pidfile}'\nwait")
    context = decode.render_workspace(str(tmp_path / "work"))
    work = context.__enter__()  # held open: only the hook may remove it below
    outcome = []

    def render():
        try:
            decode.render_frames("/v.mkv", 0, work, ffmpeg=ffmpeg)
        except BaseException as error:  # noqa: B036
            outcome.append(error)

    thread = threading.Thread(target=render)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        (open(os.path.join(work, "00000000.png"), "wb")).close()
        child = int(pidfile.read_text())
        assert _alive(child)

        decode.stop_live_renders()

        assert _wait_gone(child), "a process of the render outlived the exit hook"
        assert not os.path.exists(work), "the frames of the killed render were left behind"
        thread.join(10)
        assert not thread.is_alive()
        assert outcome and isinstance(outcome[0], OcrError), outcome
    finally:
        thread.join(10)
        context.__exit__(None, None, None)


def test_a_render_that_ended_is_no_longer_registered_for_the_exit_hook(tmp_path):
    ffmpeg = _fake_ffmpeg(tmp_path, "exit 1")
    with pytest.raises(OcrError):
        decode.render_frames("/v.mkv", 0, str(tmp_path), ffmpeg=ffmpeg)
    assert not decode._live_renders

    with decode.render_workspace(str(tmp_path / "work")) as work:
        assert decode._normal(work) in decode._live_workspaces
    assert not decode._live_workspaces

    decode.stop_live_renders()  # nothing registered: a no-op


def test_the_servers_shutdown_path_ends_a_render_registered_in_the_decode_module(monkeypatch):
    """Runs the real ``close_all`` body. Importing app.server starts a web server, so
    the function's source is executed on its own with its collaborators stubbed; the
    decode module is looked up by the key it is imported under, and a wrong key
    would find nothing and leave the render running."""
    import ast
    import sys
    import types

    path = os.path.join(os.path.dirname(decode.__file__), "..", "..", "..", "app", "server.py")
    source = open(path).read()
    close_all = next(node for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.FunctionDef) and node.name == "close_all")
    namespace = {"sys": sys, "close_database": lambda: None}
    exec(compile(ast.Module(body=[close_all], type_ignores=[]), path, "exec"), namespace)

    sportarr = types.ModuleType("sportarr")
    sse_client = types.ModuleType("sportarr.sse_client")
    sse_client.stop_sportarr_clients = lambda: None
    sportarr.sse_client = sse_client
    monkeypatch.setitem(sys.modules, "sportarr", sportarr)
    monkeypatch.setitem(sys.modules, "sportarr.sse_client", sse_client)
    assert sys.modules[decode.__name__] is decode

    render = subprocess.Popen(["sleep", "40"], start_new_session=True)
    with decode._registry_lock:
        decode._live_renders.add(render)
    try:
        namespace["close_all"](types.SimpleNamespace(server=None))
        assert _wait_gone(render.pid), "close_all did not end the render registered in the decode module"
    finally:
        with decode._registry_lock:
            decode._live_renders.discard(render)
        render.kill()
        render.wait()


def test_frames_left_by_a_process_that_is_gone_are_swept_when_the_next_render_starts(tmp_path):
    """After a killed run the folder is minutes old, not hours: the owner's pid says it is orphaned."""
    gone = subprocess.Popen(["true"])
    gone.wait()

    def folder(name, owner):
        path = tmp_path / name
        path.mkdir()
        (path / "00000000.png").write_bytes(b"")
        if owner is not None:
            (path / decode.OWNER_FILE).write_text(str(owner))
        return path

    dead = folder("ocr-dead-owner", gone.pid)
    # A restart in Docker gives Bazarr the same pid again; the folder is still not this run's.
    same_pid_not_live = folder("ocr-previous-run", os.getpid())
    other_process_alive = folder("ocr-other-instance", os.getppid())
    no_marker = folder("ocr-no-marker", None)

    with decode.render_workspace(str(tmp_path)) as mine:
        assert os.path.exists(os.path.join(mine, decode.OWNER_FILE))
        assert not dead.exists()
        assert not same_pid_not_live.exists()
        assert other_process_alive.exists()
        assert no_marker.exists(), "without an owner the age rule is the only one"
        assert os.path.isdir(mine), "a workspace live in this process is never swept"
        # A second run starting while the first is live must leave it alone.
        with decode.render_workspace(str(tmp_path)) as second:
            assert os.path.isdir(mine) and os.path.isdir(second)


def _orphan(parent, name="ocr-orphan"):
    """A folder a killed run left: its owner is a process that has ended."""
    gone = subprocess.Popen(["true"])
    gone.wait()
    path = os.path.join(parent, name)
    os.makedirs(path)
    with open(os.path.join(path, "00000000.png"), "wb"):
        pass
    with open(os.path.join(path, decode.OWNER_FILE), "w") as handle:
        handle.write(str(gone.pid))
    return path


@pytest.mark.parametrize("spelling", ["relative", "doubled-slash", "dot-segment"])
def test_a_second_render_does_not_sweep_the_first_when_the_parent_is_not_normalised(tmp_path, monkeypatch, spelling):
    """The work parent comes from the config directory as typed (``-c data``, ``/config//x``).
    mkdtemp returns a normalised path while the directory scan keeps the parent as
    given, so a live folder was compared by string and lost: this process's own pid
    in its owner file then marked it orphaned and the sweep deleted it mid-render."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cache").mkdir()
    parent = {
        "relative": os.path.join("cache", "ocr"),
        "doubled-slash": f"{tmp_path}//cache//ocr",
        "dot-segment": f"{tmp_path}/cache/./ocr",
    }[spelling]
    orphan = None
    with decode.render_workspace(parent) as first:
        open(os.path.join(first, "00000000.png"), "wb").close()
        orphan = _orphan(os.path.join(tmp_path, "cache", "ocr"))
        with decode.render_workspace(parent) as second:
            assert os.path.isdir(first), "the first render's frames were swept by the second render's start"
            assert os.path.exists(os.path.join(first, "00000000.png"))
            assert os.path.isdir(second)
            assert not os.path.exists(orphan), "a real orphan in the same parent must still be swept"


def _record_killpg(monkeypatch):
    sent = []
    monkeypatch.setattr(decode.os, "killpg", lambda group, sig: sent.append((group, sig)))
    return sent


def test_a_render_already_reaped_gets_no_group_signal(monkeypatch):
    """Once the render thread has reaped the leader its pid is free for anyone,
    and a group signal sent to it afterwards would hit whatever took it."""
    process = subprocess.Popen(["true"])
    process.wait()
    sent = _record_killpg(monkeypatch)

    decode._signal_tree(process)

    assert sent == []


def test_a_render_not_yet_reaped_still_gets_the_group_signal(monkeypatch):
    """An exited leader nobody has reaped keeps its pid, and a child of it may still
    hold the stderr pipe, so the group is still signalled; and signalling does not reap."""
    process = subprocess.Popen(["true"])
    deadline = time.monotonic() + 5
    while _alive(process.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    sent = _record_killpg(monkeypatch)
    try:
        decode._signal_tree(process)
        assert sent and sent[0][0] == process.pid
        assert process.returncode is None, "the stop path must leave the reaping to the render thread"
    finally:
        process.wait()


def test_a_marker_owner_that_cannot_be_signalled_is_alive(tmp_path, monkeypatch):
    """EPERM means the process exists and belongs to someone else (a second Bazarr
    under another user): its folder is not an orphan."""
    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(decode.os, "kill", kill)
    assert decode._pid_alive(4242) is True

    other = tmp_path / "ocr-other-user"
    other.mkdir()
    (other / decode.OWNER_FILE).write_text("4242")
    with decode.render_workspace(str(tmp_path)):
        assert other.exists(), "a folder whose owner exists but cannot be signalled was swept"


def test_a_marker_owner_that_does_not_exist_is_dead(monkeypatch):
    def kill(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(decode.os, "kill", kill)
    assert decode._pid_alive(4242) is False


def test_the_interpreters_exit_ends_a_render_still_running(tmp_path):
    """A source install ended by a plain interpreter exit (no signal handler runs)
    must still end its ffmpeg: the hook is registered with atexit at import."""
    import sys

    script = tmp_path / "exit_hook_child.py"
    script.write_text(
        "import subprocess, sys\n"
        "from subtitles.tools.ocr import decode\n"
        "render = subprocess.Popen(['sleep', '40'], start_new_session=True, stdin=subprocess.DEVNULL,\n"
        "                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "decode._live_renders.add(render)\n"
        "print(render.pid, flush=True)\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60)
    assert completed.returncode == 0, completed.stderr
    pid = int(completed.stdout.split()[0])
    try:
        assert _wait_gone(pid), "the render outlived the interpreter that started it"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.mark.parametrize("content", ["", "  \n", "garbage", "12abc", "0", "-5"])
def test_an_owner_file_that_names_no_process_means_no_marker_and_the_age_rule_decides(tmp_path, content):
    """A run killed while writing its marker leaves an empty one, and a damaged file
    can hold anything. Either way the folder is handled as one without a marker:
    kept while it is young, swept once it is older than the stale limit. 0 and
    negative numbers are not pids (os.kill would signal a whole group)."""
    def folder(name, age_s):
        path = tmp_path / name
        path.mkdir()
        (path / "00000000.png").write_bytes(b"")
        (path / decode.OWNER_FILE).write_text(content)
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    young = folder("ocr-young", 60)
    old = folder("ocr-old", decode.STALE_WORKSPACE_S + 3600)
    assert decode._owner_of(str(young)) is None

    with decode.render_workspace(str(tmp_path)):
        assert young.exists(), "an unreadable marker must not count as a dead owner"
        assert not old.exists(), "without a usable marker the age rule is the fallback"

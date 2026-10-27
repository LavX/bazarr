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
    # Matroska: no stream duration, the muxer's DURATION tag stands in for it.
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "00:00:11.500000000"}}],
      "format": {"duration": "13.0"}}, (1000, 11500)),
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


@pytest.mark.parametrize("report,expected", [
    # A value that is not a sane time counts as N/A: the next source stands in, or (0, 0).
    ({"streams": [{"start_time": "1.0", "duration": "inf"}], "format": {"duration": "13.0"}}, (1000, 12000)),
    ({"streams": [{"start_time": "1.0", "duration": "inf"}]}, (0, 0)),
    ({"streams": [{"start_time": "1.0", "duration": "N/A", "tags": {"DURATION": "1e999"}}],
      "format": {}}, (0, 0)),
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
    # eng_hd.sup starts at 1 s and the muxer tags the track 11 s long; ffprobe
    # prints duration=N/A for the stream itself.
    assert decode.subtitle_clock(media["pgs"], 0, shutil.which("ffprobe")) == (1000, 11000)


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
    assert seen and all(step == "render" and maximum == 11000 for step, maximum in seen)


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


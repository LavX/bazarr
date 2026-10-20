# coding=utf-8
"""Image-based embedded tracks are copied out of the video as they are.

Real ffmpeg on a real MKV built from the OCR fixtures: the English .sup as a
PGS stream, the same cues re-encoded as VobSub and as DVB, and an English SRT.
What lands is read back with ffprobe, packet by packet, because a copy that
only looks right (a .sub without a usable .idx) is exactly what a player
cannot load.
"""
import os
import shutil
import subprocess
import sys

import pytest

from ocr_fixtures import FIXTURES, mux_mkv

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n\n"

# What knowit reports for each stream, in stream order. knowit names no
# format for the DVB stream (test_the_tracks_are_what_knowit_reports).
TRACKS = [
    {"format": "PGS", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    {"format": "VobSub", "language": "hun", "name": "", "forced": False, "hearing_impaired": False},
    {"format": None, "language": "deu", "name": "", "forced": False, "hearing_impaired": False},
    {"format": "SubRip", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
]


@pytest.fixture(scope="module")
def ffmpeg():
    path = shutil.which("ffmpeg")
    assert path and shutil.which("ffprobe"), "these tests need ffmpeg and ffprobe on PATH (CI installs them)"
    return path


@pytest.fixture(scope="module")
def video(ffmpeg, tmp_path_factory):
    folder = tmp_path_factory.mktemp("bitmap-media")
    srt = folder / "english.srt"
    srt.write_text(SRT)
    sup = os.path.join(FIXTURES, "eng_hd.sup")
    return mux_mkv(ffmpeg, str(folder / "Film.mkv"), [
        (sup, "copy", "eng"), (sup, "dvdsub", "hun"), (sup, "dvbsub", "deu"), (str(srt), "srt", "eng"),
    ])


@pytest.fixture
def batch(monkeypatch):
    """The real probe lookup, fed TRACKS as the cached knowit answer of the
    movie row behind any path."""
    from types import SimpleNamespace

    import subtitles.tools.translate.batch as batch

    row = SimpleNamespace(movie_file_id=1, episode_file_id=1, file_size=1, id=1)
    monkeypatch.setattr(batch, "database", SimpleNamespace(
        execute=lambda statement: SimpleNamespace(first=lambda: row)))
    monkeypatch.setattr(batch.path_mappings, "path_replace_reverse_instance", lambda path, owner, kind: path)
    monkeypatch.setattr(batch, "parse_video_metadata", lambda path, size, **kwargs: {
        "ffprobe": {"subtitle": [dict(track) for track in TRACKS]}})
    monkeypatch.setattr(batch, "alpha3_from_alpha2", {"en": "eng", "hu": "hun", "de": "deu", "fr": "fra"}.get)
    monkeypatch.setattr(batch.settings.general, "default_und_embedded_subtitles_lang", "")
    # The tracks name their language by alpha3 already; no language table here.
    monkeypatch.setattr("utilities.video_analyzer._handle_alpha3", lambda track: track.get("language"))
    return batch


def _packets(path, stream="s:0"):
    """(pts, md5) of every packet ffprobe reads from one subtitle stream."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "packet=pts_time,data_hash", "-show_data_hash", "md5", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


def _codec(path):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s:0", "-show_entries",
                           "stream=codec_name", "-of", "csv=p=0", path],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_pgs_lands_as_the_same_sup(batch, video, tmp_path):
    written = batch.extract_embedded_bitmap(video, "en", "movie", str(tmp_path), codec="pgs")

    assert written == [str(tmp_path / "track.sup")]
    assert _codec(written[0]) == "hdmv_pgs_subtitle"
    # Segment for segment the fixture the stream was muxed from.
    assert _packets(written[0]) == _packets(os.path.join(FIXTURES, "eng_hd.sup"))


def test_vobsub_lands_as_an_idx_and_sub_pair_ffmpeg_reads_back(batch, video, tmp_path):
    written = batch.extract_embedded_bitmap(video, "hu", "movie", str(tmp_path), codec="vobsub")

    assert written == [str(tmp_path / "track.idx"), str(tmp_path / "track.sub")]
    index = (tmp_path / "track.idx").read_text()
    assert index.startswith("# VobSub index file, v7")
    assert "size: 1920x1080" in index and "palette: " in index
    assert "id: hu, index: 0" in index
    assert index.count("timestamp: ") == 3
    # ffmpeg opens the pair through the .idx, as players do, and finds the
    # very packets the MKV carried, at the same times.
    assert _codec(written[0]) == "dvd_subtitle"
    assert _packets(written[0]) == _packets(video, "s:1")


def test_dvb_lands_as_a_matroska_subtitle_file(batch, video, tmp_path):
    written = batch.extract_embedded_bitmap(video, "de", "movie", str(tmp_path), codec="dvbsub")

    assert written == [str(tmp_path / "track.mks")]
    assert _codec(written[0]) == "dvb_subtitle"
    assert _packets(written[0]) == _packets(video, "s:2")


def test_without_a_codec_the_bitmap_track_of_the_language_is_taken(batch, video, tmp_path):
    """A row indexed before the codec was stored carries none; English has an
    SRT and a PGS track, and the bitmap extractor only ever takes the PGS."""
    written = batch.extract_embedded_bitmap(video, "en", "movie", str(tmp_path), codec=None)

    assert written == [str(tmp_path / "track.sup")]


@pytest.mark.parametrize("language,codec", [("en", "vobsub"), ("fr", None), ("de", "pgs")])
def test_no_track_of_that_language_and_family_writes_nothing(batch, video, tmp_path, language, codec):
    assert batch.extract_embedded_bitmap(video, language, "movie", str(tmp_path), codec=codec) is None
    assert os.listdir(tmp_path) == []


def test_the_text_extractor_still_takes_the_srt_of_a_language_that_also_has_pgs(batch, video, tmp_path,
                                                                                 monkeypatch):
    monkeypatch.setattr(batch, "extracted_subtitles_dir", lambda: str(tmp_path))

    extracted = batch.extract_embedded_subtitle(video, "en", "movie")

    assert extracted and open(extracted).read().startswith("1\n00:00:01,000 --> 00:00:02,000\nHello there")


def test_a_variant_request_gets_that_variant_s_track_or_nothing(batch, video, tmp_path, monkeypatch):
    """English has a PGS and an SRT track here, neither of them forced. A
    forced English request is a row's own file: it finds no track, where
    translate's language-only fallback hands back the full SRT."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(batch, "extracted_subtitles_dir", lambda: str(cache))
    work = tmp_path / "work"
    work.mkdir()

    assert batch.extract_embedded_subtitle(video, "en", "movie", forced=True, exact=True) is None
    assert batch.extract_embedded_bitmap(video, "en", "movie", str(work), forced=True, codec="pgs") is None
    assert batch.extract_embedded_bitmap(video, "en", "movie", str(work), forced=True) is None
    assert os.listdir(work) == [] and not cache.exists()
    # Translate keeps its fallback.
    fallback = batch.extract_embedded_subtitle(video, "en", "movie", forced=True)
    assert open(fallback).read().startswith("1\n00:00:01,000 --> 00:00:02,000\nHello there")


def test_the_copy_can_be_stopped_before_ffmpeg_reads_the_video(batch, video, tmp_path):
    class Taken(Exception):
        pass

    families = []

    def refuse(family):
        families.append(family)
        raise Taken

    with pytest.raises(Taken):
        batch.extract_embedded_bitmap(video, "hu", "movie", str(tmp_path), before_copy=refuse)
    assert families == ["vobsub"]
    assert os.listdir(tmp_path) == []


def test_index_text_lines():
    from subtitles.tools.vobsub import index_text

    text = index_text("size: 720x576\npalette: 000000, ffffff\n", "hu",
                      [(1000, 0), (3_723_004, 0x2800)])

    assert text.splitlines() == [
        "# VobSub index file, v7 (do not modify this line!)",
        "size: 720x576",
        "palette: 000000, ffffff",
        "",
        "id: hu, index: 0",
        "timestamp: 00:00:01:000, filepos: 000000000",
        "timestamp: 01:02:03:004, filepos: 000002800",
    ]


@pytest.mark.parametrize("codec,family,extensions", [
    ("pgs", "pgs", (".sup",)), ("hdmv_pgs_subtitle", "pgs", (".sup",)),
    ("vobsub", "vobsub", (".idx", ".sub")), ("dvd_subtitle", "vobsub", (".idx", ".sub")),
    ("dvbsub", "dvb", (".mks",)), ("subrip", None, (".srt",)), ("ass", None, (".srt",)),
    ("mov text", None, (".srt",)), (None, None, (".srt",)), ("", None, (".srt",)),
])
def test_codec_families_and_their_files(codec, family, extensions):
    from subtitles.tools.embedded_streams import bitmap_family, output_extensions

    assert bitmap_family(codec) == family
    assert output_extensions(codec) == extensions


def test_the_row_s_text_codec_picks_between_text_tracks_of_one_key(batch, ffmpeg, tmp_path, monkeypatch):
    """An English SRT and an English ASS are two rows since the codec is
    stored. Each row gets its own track, cached under its own name. A row
    without a codec gets the first; a row whose named text codec no track has
    gets nothing, so another track's text cannot land under its name."""
    srt, ass = tmp_path / "srt.srt", tmp_path / "ass.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nFrom the SRT\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n\n")
    ass.write_text("1\n00:00:01,000 --> 00:00:02,000\nFrom the ASS\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n\n")
    video = mux_mkv(ffmpeg, str(tmp_path / "Two.mkv"), [(str(srt), "srt", "eng"), (str(ass), "ass", "eng")])
    monkeypatch.setattr(batch, "embedded_subtitle_tracks", lambda path, kind, owner=None: [
        {"format": "SubRip", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
        {"format": "ASS", "language": "eng", "name": "", "forced": False, "hearing_impaired": False},
    ])
    cache = tmp_path / "cache"
    monkeypatch.setattr(batch, "extracted_subtitles_dir", lambda: str(cache))

    def text(**kwargs):
        return open(batch.extract_embedded_subtitle(video, "en", "movie", exact=True, **kwargs)).read()

    assert "From the ASS" in text(codec="ass")
    assert "From the SRT" in text(codec="subrip")
    assert "From the SRT" in text()
    assert batch.extract_embedded_subtitle(video, "en", "movie", exact=True, codec="webvtt") is None
    # The ASS read did not take the plain key the other reads share.
    assert "From the ASS" in text(codec="ass")
    assert len(os.listdir(cache)) == 3


def test_the_tracks_are_what_knowit_reports(video):
    """TRACKS stands in for knowit's answer, so it must be knowit's answer:
    no format for the DVB stream, which is what made DVB unreachable."""
    from knowit.api import know

    found = know(video_path=video, context={"provider": "ffmpeg", "ffmpeg": shutil.which("ffprobe")})

    assert [track.get("format") for track in found["subtitle"]] == [track["format"] for track in TRACKS]


def test_a_missing_format_is_filled_from_ffprobe_s_codec_name(batch, video):
    tracks = batch.embedded_subtitle_tracks(video, "movie", 1)

    assert [track["format"] for track in tracks] == ["PGS", "VobSub", "dvb_subtitle", "SubRip"]
    assert TRACKS[2]["format"] is None, "the probe answer itself is never changed"


def test_a_dvb_track_ahead_of_a_text_track_of_its_language_is_passed_over(batch, ffmpeg, tmp_path, monkeypatch):
    """With no format the DVB stream read as text: a German row with no codec
    ran the text extractor on it and got nothing."""
    srt = tmp_path / "german.srt"
    srt.write_text(SRT.replace("Hello there", "Guten Tag"))
    sup = os.path.join(FIXTURES, "eng_hd.sup")
    german = mux_mkv(ffmpeg, str(tmp_path / "German.mkv"), [(sup, "dvbsub", "deu"), (str(srt), "srt", "deu")])
    monkeypatch.setattr(sys.modules[__name__], "TRACKS", [
        dict(TRACKS[2]), {"format": "SubRip", "language": "deu", "name": "", "forced": False,
                          "hearing_impaired": False}])
    monkeypatch.setattr(batch, "extracted_subtitles_dir", lambda: str(tmp_path / "cache"))

    text = batch.extract_embedded_subtitle(german, "de", "movie", exact=True)
    assert text and "Guten Tag" in open(text).read()
    work = tmp_path / "work"
    work.mkdir()
    assert batch.extract_embedded_bitmap(german, "de", "movie", str(work)) == [str(work / "track.mks")]

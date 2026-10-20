# coding=utf-8
"""Stream selection shared by the text extractor and OCR.

The parity cases run the real ``extract_embedded_subtitle`` with ffmpeg
mocked and read the stream it asked ffmpeg for, so the shared selector and the
extractor cannot drift apart until the extractor is switched over to it.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from subtitles.tools.embedded_streams import (is_bitmap_codec, is_ocr_codec, select_subtitle_stream,
                                              track_codec)


def _track(codec, language, name="", hi=False, forced=False):
    return {"format": codec, "language": language, "name": name, "hearing_impaired": hi, "forced": forced}


@pytest.fixture(autouse=True)
def plain_languages():
    with patch("utilities.video_analyzer._handle_alpha3", side_effect=lambda track: track.get("language")):
        yield


def _extractor_choice(tracks, alpha2, alpha3, hi, forced, tmp_path, **kwargs):
    from subtitles.tools.translate.batch import extract_embedded_subtitle

    commands = []

    def fake_ffmpeg(cmd, **kwargs):
        commands.append(cmd)
        with open(cmd[-1], "w") as handle:
            handle.write("1\n00:00:01,000 --> 00:00:02,000\nx\n")
        return MagicMock(returncode=0, stderr="")

    with (
        patch("subtitles.tools.translate.batch.database") as database,
        patch("subtitles.tools.translate.batch.parse_video_metadata",
              return_value={"ffprobe": {"subtitle": tracks}}),
        patch("subtitles.tools.translate.batch.alpha3_from_alpha2", return_value=alpha3),
        patch("subtitles.tools.translate.batch.get_binary", return_value="/usr/bin/ffmpeg"),
        patch("subtitles.tools.translate.batch.settings") as settings,
        patch("app.get_args.args") as args,
        patch("subprocess.run", side_effect=fake_ffmpeg),
    ):
        database.execute.return_value.first.return_value = MagicMock(movie_file_id=1, file_size=1)
        settings.general.default_und_embedded_subtitles_lang = ""
        args.config_dir = str(tmp_path)
        extract_embedded_subtitle(os.path.join(str(tmp_path), "movie.mkv"), alpha2, "movie", hi=hi, forced=forced,
                                  **kwargs)
    if not commands:
        return None
    mapping = commands[0][commands[0].index("-map") + 1]
    return int(mapping.split(":")[-1])


PARITY = [
    ([_track("subrip", "eng"), _track("subrip", "eng", hi=True)], "en", "eng", True, False, 1),
    ([_track("hdmv_pgs_subtitle", "eng"), _track("subrip", "eng")], "en", "eng", False, False, 1),
    ([_track("subrip", "hun", name="Forced"), _track("subrip", "hun")], "hu", "hun", False, False, 1),
    ([_track("subrip", "eng", name="Director's commentary"), _track("ass", "eng")], "en", "eng", False, False, 1),
    ([_track("subrip", "fra"), _track("ass", "eng", hi=True)], "en", "eng", False, False, 1),
    ([_track("subrip", "deu")], "en", "eng", False, False, None),
]


@pytest.mark.parametrize("tracks,alpha2,alpha3,hi,forced,expected", PARITY)
def test_shared_selector_matches_the_text_extractor(tmp_path, tracks, alpha2, alpha3, hi, forced, expected):
    shared = select_subtitle_stream(tracks, alpha3, hi=hi, forced=forced)

    assert shared == expected
    assert _extractor_choice(tracks, alpha2, alpha3, hi, forced, tmp_path) == expected


# exact=True is what the editor, the viewer, the download and Extract to file
# ask for: the track a row names or nothing, never the language-only fallback
# translate keeps.
EXACT = [
    # en:forced names a forced PGS track; the normal SRT must not stand in.
    ([_track("PGS", "eng", forced=True), _track("subrip", "eng")], "en", "eng", False, True, None),
    # Only an HI English track: a plain en request gets nothing.
    ([_track("subrip", "fra"), _track("ass", "eng", hi=True)], "en", "eng", False, False, None),
    # The exact track is text: exact changes nothing.
    ([_track("subrip", "eng"), _track("subrip", "eng", hi=True)], "en", "eng", True, False, 1),
]


@pytest.mark.parametrize("tracks,alpha2,alpha3,hi,forced,expected", EXACT)
def test_exact_selection_never_falls_back(tmp_path, tracks, alpha2, alpha3, hi, forced, expected):
    shared = select_subtitle_stream(tracks, alpha3, hi=hi, forced=forced, exact=True)

    assert shared == expected
    assert _extractor_choice(tracks, alpha2, alpha3, hi, forced, tmp_path, exact=True) == expected


@pytest.mark.parametrize("tracks,hi,expected", [
    ([_track("subrip", "eng"), _track("PGS", "eng"), _track("DVBSub", "eng")], False, 1),
    ([_track("DVBSub", "eng")], False, None),
    ([_track("VobSub", "eng", hi=True), _track("dvd_subtitle", "eng")], True, 0),
    ([_track("VobSub", "eng", hi=True), _track("dvd_subtitle", "eng")], False, 1),
])
def test_ocr_selection_counts_every_stream_and_skips_dvb(tracks, hi, expected):
    assert select_subtitle_stream(tracks, "eng", hi=hi, accept=is_ocr_codec) == expected


@pytest.mark.parametrize("codec,bitmap,ocr", [
    ("pgs", True, True), ("hdmv_pgs_subtitle", True, True), ("vobsub", True, True),
    ("dvd_subtitle", True, True), ("dvbsub", True, False),
    ("subrip", False, False), ("ass", False, False), ("", False, False), (None, False, False),
])
def test_codec_families(codec, bitmap, ocr):
    assert is_bitmap_codec(codec) is bitmap
    assert is_ocr_codec(codec) is ocr


def test_codec_falls_back_to_the_track_name_like_the_extractor():
    assert track_codec({"name": "PGS English"}) == "pgs english"
    assert track_codec({"format": "SubRip", "name": "PGS"}) == "subrip"

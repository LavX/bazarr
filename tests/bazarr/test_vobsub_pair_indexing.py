# coding=utf-8
"""The .sub of a VobSub pair is image data, not a text subtitle.

The external subtitle search lists every .sub, because a lone .sub can be a
MicroDVD text file. A .sub next to an .idx of the same name is the other
half of a VobSub pair: it is never listed, so its binary content is never
language-guessed and no tool is ever offered on it.
"""


def test_a_sub_with_a_sibling_idx_is_not_listed(tmp_path):
    from subliminal_patch.core import search_external_subtitles

    video = tmp_path / "Film.mkv"
    video.write_bytes(b"video")
    for name in ("Film.en.idx", "Film.en.sub", "Film.FR.IDX", "Film.fr.sub", "Film.de.sub", "Film.hu.srt",
                 "Film.en.sup"):
        (tmp_path / name).write_bytes(b"\x00\x00\x01\xba")

    found = search_external_subtitles(str(video))

    assert sorted(found) == ["Film.de.sub", "Film.hu.srt"]
    assert str(found["Film.de.sub"]) == "de"

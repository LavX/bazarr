# coding=utf-8
"""Extraction of subtitle files from uploaded archives (.zip/.rar/.7z).

See https://github.com/LavX/bazarr/issues/233 - users upload a compressed file
and Bazarr extracts the subtitle entries, discarding everything else.
"""
import zipfile
from io import BytesIO

import py7zr
import pytest

from subtitles.tools.archives import (
    ArchiveError,
    extract_subtitles_from_archive,
    is_archive,
)


def _zip(entries):
    """Build an in-memory zip. entries: list of (arcname, bytes|None);
    None marks a directory entry."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            if data is None:
                z.writestr(name + "/", b"")
            else:
                z.writestr(name, data)
    return buf.getvalue()


def _7z(entries):
    buf = BytesIO()
    with py7zr.SevenZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(data, name)
    return buf.getvalue()


def test_is_archive_by_extension():
    assert is_archive("pack.zip")
    assert is_archive("pack.RAR")
    assert is_archive("Season 1.7z")
    assert not is_archive("movie.srt")
    assert not is_archive("noext")


def test_zip_keeps_subtitles_and_discards_other_files():
    data = _zip([
        ("movie.srt", b"sub-one"),
        ("subs/movie.es.ass", b"sub-two"),
        ("poster.jpg", b"img"),
        ("info.nfo", b"nfo"),
    ])
    result = extract_subtitles_from_archive("pack.zip", data)
    assert sorted(n for n, _ in result) == ["movie.es.ass", "movie.srt"]
    by_name = dict(result)
    assert by_name["movie.srt"] == b"sub-one"
    assert by_name["movie.es.ass"] == b"sub-two"


def test_zip_directory_entries_are_skipped():
    data = _zip([("subs", None), ("subs/a.srt", b"x")])
    result = extract_subtitles_from_archive("pack.zip", data)
    assert [n for n, _ in result] == ["a.srt"]


def test_zip_slip_paths_reduced_to_basename():
    data = _zip([("../../etc/evil.srt", b"x")])
    result = extract_subtitles_from_archive("pack.zip", data)
    assert result == [("evil.srt", b"x")]


def test_macosx_resource_forks_discarded():
    data = _zip([("__MACOSX/._movie.srt", b"junk"), ("movie.srt", b"real")])
    result = extract_subtitles_from_archive("pack.zip", data)
    assert result == [("movie.srt", b"real")]


def test_zip_with_no_subtitles_returns_empty():
    data = _zip([("readme.md", b"x"), ("cover.png", b"y")])
    assert extract_subtitles_from_archive("pack.zip", data) == []


def test_sevenzip_keeps_only_subtitles():
    data = _7z([("a.srt", b"alpha"), ("b.png", b"beta")])
    result = extract_subtitles_from_archive("pack.7z", data)
    assert result == [("a.srt", b"alpha")]


def test_corrupt_archive_raises_archive_error():
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.zip", b"this is not a zip")


def test_unsupported_extension_raises_archive_error():
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.tar.gz", b"whatever")

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


def test_zip_total_size_cap_raises(monkeypatch):
    from subtitles.tools import archives

    monkeypatch.setattr(archives, "_MAX_TOTAL_BYTES", 16)
    data = _zip([("big.srt", b"x" * 64)])
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.zip", data)


def test_sevenzip_total_size_cap_raises(monkeypatch):
    from subtitles.tools import archives

    monkeypatch.setattr(archives, "_MAX_TOTAL_BYTES", 16)
    data = _7z([("big.srt", b"x" * 64)])
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.7z", data)


def test_zip_entry_cap_raises(monkeypatch):
    from subtitles.tools import archives

    monkeypatch.setattr(archives, "_MAX_ENTRIES", 1)
    data = _zip([("a.srt", b"a"), ("b.srt", b"b")])
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.zip", data)


def test_sevenzip_entry_cap_raises(monkeypatch):
    from subtitles.tools import archives

    monkeypatch.setattr(archives, "_MAX_ENTRIES", 1)
    data = _7z([("a.srt", b"a"), ("b.srt", b"b")])
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.7z", data)


def test_encrypted_sevenzip_raises_archive_error():
    import py7zr

    buf = BytesIO()
    with py7zr.SevenZipFile(buf, "w", password="secret") as z:
        z.writestr(b"hello", "a.srt")
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.7z", buf.getvalue())


def test_corrupt_archive_raises_archive_error():
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.zip", b"this is not a zip")


def test_unsupported_extension_raises_archive_error():
    with pytest.raises(ArchiveError):
        extract_subtitles_from_archive("pack.tar.gz", b"whatever")


# --- the archive upload route bounds what it reads -------------------------------
#
# Every other upload route answers an oversized file with 413 and the same
# "<what> is too large: the limit is N MiB." message; an archive over its own
# ceiling is refused the same way, and one right at it is still extracted.

def _post_archive(content, *, filename="pack.zip", declared=None):
    from flask import Flask

    from api.subtitles.archive import SubtitleArchive

    environ = {"CONTENT_LENGTH": str(declared)} if declared is not None else None
    with Flask(__name__).test_request_context("/api/subtitles/archive", method="POST",
                                              data={"file": (BytesIO(content), filename)},
                                              content_type="multipart/form-data",
                                              environ_overrides=environ):
        return SubtitleArchive.post.__wrapped__(SubtitleArchive())


@pytest.fixture
def archive_extractions(monkeypatch):
    """The archives the route handed to the extractor."""
    from api.subtitles import archive

    extracted = []

    def recording(filename, data):
        extracted.append((filename, data))
        return extract_subtitles_from_archive(filename, data)

    monkeypatch.setattr(archive, "extract_subtitles_from_archive", recording)
    return extracted


def test_the_archive_upload_ceiling_is_50_mib():
    from api.subtitles import archive

    assert archive.MAX_ARCHIVE_SIZE == 50 * 1024 * 1024


def test_an_archive_declared_over_the_ceiling_is_refused_as_too_large(archive_extractions):
    from api.subtitles.archive import MAX_ARCHIVE_SIZE
    from api.utils import UPLOAD_FORM_ALLOWANCE

    body, status = _post_archive(b"PK", declared=MAX_ARCHIVE_SIZE + UPLOAD_FORM_ALLOWANCE + 1)

    assert (body, status) == ("Archive is too large: the limit is 50 MiB.", 413)
    assert archive_extractions == []


def test_an_archive_over_the_ceiling_is_refused_as_too_large(archive_extractions, monkeypatch):
    from api.subtitles import archive
    from api.utils import upload_too_large_message

    data = _zip([("movie.srt", b"sub-one")])
    monkeypatch.setattr(archive, "MAX_ARCHIVE_SIZE", len(data) - 1)

    body, status = _post_archive(data)

    assert (body, status) == (upload_too_large_message("Archive", len(data) - 1), 413)
    assert archive_extractions == []


def test_an_archive_at_the_ceiling_is_extracted(archive_extractions, monkeypatch):
    # The declared length also counts the multipart framing around the file,
    # so an archive of exactly the ceiling arrives in a larger request.
    from api.subtitles import archive

    data = _zip([("movie.srt", b"sub-one")])
    monkeypatch.setattr(archive, "MAX_ARCHIVE_SIZE", len(data))

    response = _post_archive(data)

    assert response.status_code == 200
    assert response.get_json()["count"] == 1
    assert response.get_json()["files"][0]["name"] == "movie.srt"
    assert archive_extractions == [("pack.zip", data)]

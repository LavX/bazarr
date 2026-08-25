# coding=utf-8
"""Archive member matching: case-insensitive extensions, one source of truth for
what counts as a subtitle member, and the widened extension set.

Two behaviours are guarded here.

1. Members are matched on their extension alone, compared case-insensitively, so
   an archive whose only subtitle is named ``MOVIE.SRT`` still extracts. The rest
   of the member name is never lowercased: callers parse language and HI/forced
   tags out of it, and a lowercased copy leaking into that parsing is how
   ``Elephant.hi.srt`` ends up resolving as a language it never claimed.

2. The Provider Hub accepts the same eight extensions the rest of the codebase
   calls a subtitle. ``.txt`` is admitted but treated as ambiguous: scene
   archives ship release-info text files, so a ``.txt`` member is only ever
   used when the archive holds nothing better.
"""
import base64
import hashlib
import io
import logging
import os
import zipfile

import pytest

import provider_hub.protocol as proto
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from subliminal_patch.providers import utils
from subliminal_patch.providers.utils import get_archive_from_bytes

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
_MICRODVD = b"{0}{25}Hello|World\n{26}{50}Bye\n"
_MPL2 = b"[0][10] Hello\n[11][20] Bye\n"
_SAMI = (
    b"<SAMI><BODY>\n"
    b'<SYNC Start=0><P Class=ENUSCC>Hello\n'
    b'<SYNC Start=2000><P Class=ENUSCC>&nbsp;\n'
    b"</BODY></SAMI>\n"
)


def _zip(members):
    """``members`` is an ordered sequence of ``(name, payload)`` pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return buf.getvalue()


def _archive(members):
    return get_archive_from_bytes(_zip(members))


class _Sub:
    class language:
        forced = False

    content = None
    format = "srt"
    encoding = None
    _guessed_encoding = None


def _download(body, **extra):
    payload = {
        "archive_b64": base64.b64encode(body).decode("ascii"),
        "archive_sha256": hashlib.sha256(body).hexdigest(),
    }
    payload.update(extra)
    sub = _Sub()
    proto.worker_download_to_content(sub, payload)
    return sub


# --------------------------------------------------------------------------
# Case-insensitive member matching
# --------------------------------------------------------------------------


def test_uppercase_extension_member_is_extracted():
    # Older uploads on several providers ship the subtitle as .SRT. A
    # case-sensitive comparison misses it, the download raises, and the provider
    # is throttled for ten minutes.
    archive = _archive([("MOVIE.SRT", _SRT)])
    assert utils.get_subtitle_from_archive(archive) is not None


@pytest.mark.parametrize("name", ["Movie.SRT", "Movie.Ass", "Movie.VtT", "Movie.SuB"])
def test_mixed_case_extensions_are_matched(name):
    assert utils.is_subtitle_member(name, SUBTITLE_EXTENSIONS)


def test_extension_is_matched_in_isolation_not_by_substring():
    # Lowercasing the whole name and substring-testing it is how a video named
    # "Elephant" made "Elephant.hi.srt" resolve as Chinese Traditional. Only the
    # extension may take part in the comparison.
    assert not utils.is_subtitle_member("Trailer.srt.mkv", SUBTITLE_EXTENSIONS)
    assert not utils.is_subtitle_member("srt", SUBTITLE_EXTENSIONS)
    assert not utils.is_subtitle_member("Making.Of.assets.mp4", SUBTITLE_EXTENSIONS)


def test_matched_member_name_keeps_its_original_case():
    # The picked name feeds language/HI/forced parsing and the format lookup, so
    # it must come back byte-identical to the archive entry.
    archive = _archive([("Elephant.HI.SRT", _SRT)])
    assert utils.list_subtitle_members(archive, SUBTITLE_EXTENSIONS) == ["Elephant.HI.SRT"]


def test_hub_download_extracts_uppercase_member():
    sub = _download(_zip([("MOVIE.SRT", _SRT)]))
    assert b"Hello" in sub.content


# --------------------------------------------------------------------------
# One source of truth: the listing path and the selection path must agree
# --------------------------------------------------------------------------


def test_listing_and_selection_agree_on_members():
    members = [
        ("Show.S01E01.ENG.SRT", _SRT),
        ("show.s01e01.fre.srt", _SRT),
        (".hidden.srt", _SRT),
        ("notes.txt", b"release info"),
        ("poster.jpg", b"binary"),
    ]
    archive = _archive(members)
    listed = proto._list_archive_members(archive)
    selectable = utils.list_subtitle_members(archive, proto.ARCHIVE_MEMBER_EXTENSIONS)
    assert listed == selectable


def test_hub_uses_the_canonical_extension_list():
    assert tuple(proto.ARCHIVE_MEMBER_EXTENSIONS) == tuple(SUBTITLE_EXTENSIONS)


def test_no_module_reintroduces_its_own_extension_copy():
    # A caller that hardcodes its own tuple is exactly how the listing path and
    # the selection path drifted apart. Fail if one comes back.
    import re

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    literal = re.compile(
        r"""["']\.(srt|sub|smi|ssa|ass|mpl|vtt)["']\s*,\s*["']\.(srt|sub|smi|ssa|ass|mpl|vtt)["']"""
    )
    targets = [os.path.join(root, "bazarr", "subtitles", "tools", "archives.py")]
    for dirpath, _dirnames, filenames in os.walk(os.path.join(root, "bazarr", "provider_hub")):
        targets.extend(
            os.path.join(dirpath, name) for name in filenames if name.endswith(".py")
        )

    offenders = []
    for path in targets:
        with open(path, encoding="utf-8") as handle:
            if literal.search(handle.read()):
                offenders.append(os.path.relpath(path, root))
    assert not offenders, f"hardcoded subtitle-extension list in: {offenders}"


# --------------------------------------------------------------------------
# Diagnosability: a failed extraction must name the members it saw
# --------------------------------------------------------------------------


def test_failed_extraction_logs_the_member_names(caplog):
    archive = _archive([("poster.jpg", b"binary"), ("Trailer.mkv", b"binary")])
    with caplog.at_level(logging.INFO, logger="subliminal_patch.providers.utils"):
        assert utils.get_subtitle_from_archive(archive) is None
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "poster.jpg" in logged
    assert "Trailer.mkv" in logged


def test_archive_without_any_subtitle_member_still_fails_loudly():
    with pytest.raises(proto.WorkerProtocolError):
        _download(_zip([("poster.jpg", b"binary")]))


# --------------------------------------------------------------------------
# Widened extension set, and the release-info text file guard
# --------------------------------------------------------------------------


def test_microdvd_text_member_extracts():
    # ex-Yugoslav providers ship MicroDVD under a plain .txt extension.
    sub = _download(_zip([("Film.2019.txt", _MICRODVD)]))
    assert b"Hello" in sub.content


def test_microdvd_text_member_keeps_its_real_format():
    # Resolving .txt to nothing and defaulting to srt writes MicroDVD bytes into
    # a file claiming to be SubRip.
    sub = _download(_zip([("Film.2019.txt", _MICRODVD)]))
    assert sub.format == "microdvd"


@pytest.mark.parametrize(
    "name,payload,expected_format",
    [
        ("Film.smi", _SAMI, "sami"),
        ("Film.mpl", _MPL2, "mpl2"),
        ("Film.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n", "vtt"),
        ("Film.sub", _MICRODVD, "microdvd"),
    ],
)
def test_newly_admitted_extensions_extract_and_map_to_their_format(
    name, payload, expected_format
):
    # Member pinned by name, so the format comes from the extension table rather
    # than from sniffing the bytes: an extension the table does not cover resolves
    # to nothing and the member is saved claiming to be SubRip.
    sub = _download(_zip([(name, payload)]), member=name)
    assert sub.content is not None
    assert sub.format == expected_format


@pytest.mark.parametrize(
    "payload,expected_format",
    [(_MICRODVD, "microdvd"), (b"00:00:00:Hello\n00:00:05:Bye\n", "tmp")],
)
def test_text_member_format_comes_from_its_content(payload, expected_format):
    # ".txt" names no format at all, so the bytes have to settle it. The two kinds
    # that actually turn up under it must not be conflated.
    sub = _download(_zip([("Film.txt", payload)]), member="Film.txt")
    assert sub.format == expected_format


def test_format_table_covers_every_admitted_extension():
    for extension in SUBTITLE_EXTENSIONS:
        assert proto._format_from_member("Film" + extension) is not None, extension


def test_release_info_text_file_never_wins_over_a_real_subtitle_alphabetically():
    # "aaa.txt" sorts first, the subtitle last. Nothing in the selector sorts today
    # and this asserts nothing ever starts to.
    sub = _download(_zip([("zzz.srt", _SRT), ("aaa.txt", b"release info")]))
    assert b"Hello" in sub.content
    assert sub.format == "srt"


def test_release_info_text_file_never_wins_over_a_real_subtitle_in_archive_order():
    # "zzz.txt" sorts last but comes first in the archive's own member order, which
    # is what namelist() yields and what the movie selector would otherwise take.
    sub = _download(_zip([("zzz.txt", b"release info"), ("movie.srt", _SRT)]))
    assert b"Hello" in sub.content
    assert sub.format == "srt"


def test_ambiguous_member_is_dropped_while_an_unambiguous_one_exists():
    archive = _archive([("info.txt", b"release info"), ("movie.srt", _SRT)])
    assert utils.list_subtitle_members(archive, SUBTITLE_EXTENSIONS) == ["movie.srt"]


def test_ambiguous_member_is_used_when_it_is_all_there_is():
    archive = _archive([("info.txt", _MICRODVD)])
    assert utils.list_subtitle_members(archive, SUBTITLE_EXTENSIONS) == ["info.txt"]


# --------------------------------------------------------------------------
# Widening what we accept from an untrusted download widens what reaches disk.
# The 7z path is the only one that materialises a member on the filesystem.
# --------------------------------------------------------------------------


class _FakeSevenZip:
    """Stands in for py7zr and restores the member as a symlink, the way a
    hostile archive would if the library's own escape check were bypassed."""

    def __init__(self, target):
        self._target = target

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract(self, path=None, targets=None):
        os.symlink(self._target, os.path.join(path, "movie.srt"))


def test_seven_zip_symlink_member_is_not_followed(tmp_path, monkeypatch):
    secret = tmp_path / "secret.conf"
    secret.write_text("APIKEY=hunter2")

    archive = proto._SevenZipArchive(b"")
    monkeypatch.setattr(archive, "_open", lambda: _FakeSevenZip(str(secret)))

    with pytest.raises(proto.WorkerProtocolError):
        archive.read("movie.srt")


def test_extracted_file_guard_rejects_symlinks_and_escapes(tmp_path):
    root = str(tmp_path / "extracted")
    os.makedirs(root)
    outside = tmp_path / "outside.conf"
    outside.write_text("APIKEY=hunter2")

    regular = os.path.join(root, "movie.srt")
    with open(regular, "wb") as handle:
        handle.write(_SRT)
    assert proto._is_safe_extracted_file(regular, root)

    link = os.path.join(root, "link.srt")
    os.symlink(str(outside), link)
    assert not proto._is_safe_extracted_file(link, root)

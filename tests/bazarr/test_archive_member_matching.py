# coding=utf-8
"""Archive member matching: case-insensitive extensions, one source of truth for
what counts as a subtitle member, and the widened extension set.

Two behaviours are guarded here.

1. Members are matched on their extension alone, compared case-insensitively, so
   an archive whose only subtitle is named ``MOVIE.SRT`` still extracts. The rest
   of the member name is never lowercased: callers parse language and HI/forced
   tags out of it, and a lowercased copy leaking into that parsing is how
   ``Elephant.hi.srt`` ends up resolving as a language it never claimed.

2. The member list a plugin language-pins from and the member the selector picks
   come from one call, so the two can never disagree about a given archive.
"""
import base64
import hashlib
import io
import logging
import zipfile

import pytest

import provider_hub.protocol as proto
from subliminal_patch.core import SUBTITLE_EXTENSIONS
from subliminal_patch.providers import utils
from subliminal_patch.providers.utils import get_archive_from_bytes

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"


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
    assert listed == ["Show.S01E01.ENG.SRT", "show.s01e01.fre.srt"]




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

"""Host-side archive member listing + the select_member (pin/defer/reject) branch.

Lets the host list zip/rar/7z members and call back into the worker to language-pin one,
so multilingual rar/7z archives no longer cause silent wrong-language downloads.
"""
import base64
import io
import zipfile

import pytest

import provider_hub.protocol as proto
from subliminal_patch.providers.utils import get_archive_from_bytes

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nx\n"


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name in names:
            archive.writestr(name, _SRT)
    return buf.getvalue()


class _Sub:
    class language:
        forced = False

    content = None
    format = "srt"
    encoding = None
    _guessed_encoding = None


def _run(body, payload, cb):
    sub = _Sub()
    proto.worker_download_to_content(sub, payload, select_member_cb=cb)
    return sub


def _archive_payload(body, **extra):
    payload = {
        "archive_b64": base64.b64encode(body).decode("ascii"),
        "archive_sha256": __import__("hashlib").sha256(body).hexdigest(),
    }
    payload.update(extra)
    return payload


def test_list_archive_members_filters_to_subtitles():
    body = _zip(["a.eng.srt", "b.fre.srt", ".hidden.srt", "notes.txt"])
    archive = get_archive_from_bytes(body)
    assert sorted(proto._list_archive_members(archive)) == ["a.eng.srt", "b.fre.srt"]


def test_select_member_pin_extracts_named_member():
    body = _zip(["show.eng.srt", "show.fre.srt"])
    sub = _run(body, _archive_payload(body, select_member=True),
               lambda members: {"member": "show.fre.srt", "decision": "pin"})
    assert sub.content is not None and b"00:00:01" in sub.content


def test_select_member_pin_unknown_member_raises():
    body = _zip(["show.eng.srt"])
    with pytest.raises(proto.WorkerProtocolError):
        _run(body, _archive_payload(body, select_member=True),
             lambda members: {"member": "../evil.srt", "decision": "pin"})


def test_select_member_reject_raises():
    body = _zip(["show.eng.srt", "show.fre.srt"])
    with pytest.raises(proto.WorkerProtocolError):
        _run(body, _archive_payload(body, select_member=True),
             lambda members: {"member": None, "decision": "reject"})


def test_select_member_defer_uses_episode_pick():
    body = _zip(["only.srt"])
    sub = _run(body, _archive_payload(body, select_member=True, episode=None),
               lambda members: {"member": None, "decision": "defer"})
    assert sub.content is not None and b"00:00:01" in sub.content


def test_select_member_callback_receives_listed_members():
    body = _zip(["show.eng.srt", "show.fre.srt", "notes.txt"])
    seen = {}

    def cb(members):
        seen["members"] = sorted(members)
        return {"member": "show.eng.srt", "decision": "pin"}

    _run(body, _archive_payload(body, select_member=True), cb)
    assert seen["members"] == ["show.eng.srt", "show.fre.srt"]

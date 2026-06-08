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


def test_worker_runner_dispatches_select_archive_member():
    from provider_hub import worker_runner

    class P:
        def select_archive_member(self, provider_payload, language, members, config):
            return {"member": members[1], "decision": "pin"}

    out = worker_runner._handle(P(), "select_archive_member", {
        "members": ["a.srt", "b.srt"], "language": {"alpha3": "fra"},
        "provider_payload": {}, "config": {},
    })
    assert out == {"member": "b.srt", "decision": "pin"}


def test_worker_runner_forwards_episode_context_to_selector():
    # The host sends season/episode at the top level of the op payload (the registry derives
    # them from the requested subtitle). The runner must surface them on provider_payload so a
    # selector can disambiguate season-pack members even when the search payload omitted them.
    from provider_hub import worker_runner

    seen = {}

    class P:
        def select_archive_member(self, provider_payload, language, members, config):
            seen["payload"] = provider_payload
            return {"member": members[0], "decision": "pin"}

    worker_runner._handle(P(), "select_archive_member", {
        "members": ["a.srt"], "language": {"alpha3": "fra"},
        "provider_payload": {"url": "x"}, "config": {},
        "season": 1, "episode": 2,
    })
    assert seen["payload"]["season"] == 1
    assert seen["payload"]["episode"] == 2
    assert seen["payload"]["url"] == "x"


def test_worker_runner_select_archive_member_rejects_when_unimplemented():
    from provider_hub import worker_runner

    class P:
        pass

    out = worker_runner._handle(P(), "select_archive_member", {"members": ["a.srt"]})
    assert out == {"member": None, "decision": "reject"}


def test_worker_runner_select_archive_member_coerces_bad_decision():
    from provider_hub import worker_runner

    class P:
        def select_archive_member(self, provider_payload, language, members, config):
            return {"member": None, "decision": "weird"}

    out = worker_runner._handle(P(), "select_archive_member", {"members": ["a.srt"]})
    assert out["decision"] == "reject"


def test_select_member_callback_receives_listed_members():
    body = _zip(["show.eng.srt", "show.fre.srt", "notes.txt"])
    seen = {}

    def cb(members):
        seen["members"] = sorted(members)
        return {"member": "show.eng.srt", "decision": "pin"}

    _run(body, _archive_payload(body, select_member=True), cb)
    assert seen["members"] == ["show.eng.srt", "show.fre.srt"]

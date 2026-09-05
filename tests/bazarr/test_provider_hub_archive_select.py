"""Host-side archive member listing + the select_member (pin/defer/reject) branch.

Lets the host list zip/rar/7z members and call back into the worker to language-pin one,
so multilingual rar/7z archives no longer cause silent wrong-language downloads.
"""
import base64
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

import provider_hub.protocol as proto
from subliminal_patch.exceptions import SubtitleCandidateRejected
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
    # Design note: ".txt" IS one of the extensions the hub accepts, because
    # several providers ship MicroDVD subtitles under it, and the worker is the
    # thing that can tell a Serbian subtitle from a release-info file by looking
    # at the language tag. So it is offered, ordered last: the guard that a
    # release-info file can never be picked ahead of a real subtitle comes from
    # that ordering, not from withholding the member. Dot-files stay out.
    body = _zip(["a.eng.srt", "b.fre.srt", ".hidden.srt", "notes.txt"])
    archive = get_archive_from_bytes(body)
    members = proto._list_archive_members(archive)
    assert members == ["a.eng.srt", "b.fre.srt", "notes.txt"]


def test_list_archive_members_offers_text_member_when_it_is_all_there_is():
    # The other half of the same rule: with no unambiguous member the ".txt" is
    # what the archive has, so the worker must get the chance to pin it.
    body = _zip(["film.txt"])
    archive = get_archive_from_bytes(body)
    assert proto._list_archive_members(archive) == ["film.txt"]


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
    with pytest.raises(SubtitleCandidateRejected, match="No matching subtitle language"):
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


@pytest.mark.parametrize("context,expected", [
    ({}, {"season": 99, "episode": 99}),
    ({"season": None}, {"season": None, "episode": 99}),
    ({"episode": None}, {"season": 99, "episode": None}),
    ({"season": None, "episode": None}, {"season": None, "episode": None}),
    ({"season": 3}, {"season": 3, "episode": 99}),
    ({"episode": 1}, {"season": 99, "episode": 1}),
    ({"season": 3, "episode": None}, {"season": 3, "episode": None}),
    ({"season": None, "episode": 1}, {"season": None, "episode": 1}),
    ({"season": 3, "episode": 1}, {"season": 3, "episode": 1}),
])
def test_selector_context_distinguishes_absent_and_null_without_mutating_payload(context, expected):
    from provider_hub import worker_runner

    opaque = {"season": 99, "episode": 99, "id": "stored-candidate"}

    class Provider:
        def select_archive_member(self, provider_payload, **kwargs):
            assert provider_payload == {**expected, "id": "stored-candidate"}
            provider_payload["id"] = "selector-local"
            return {"decision": "defer"}

    result = worker_runner._handle(Provider(), "select_archive_member", {
        "provider_payload": opaque, **context,
    })

    assert result == {"decision": "defer", "member": None}
    assert opaque == {"season": 99, "episode": 99, "id": "stored-candidate"}


@pytest.fixture
def selector_worker(tmp_path):
    from provider_hub.worker import ProviderWorkerClient, worker_command

    # The bundle uses only stdlib and the actual isolated worker entry point.
    (tmp_path / "provider.py").write_text('''
class Provider:
    def search(self, video, languages, config):
        return [{
            "id": "fixture", "language": languages[0],
            "provider_payload": {"archive": config["archive"], **config["opaque"]},
            "display": {"season": 99, "episode": 99},
        }]

    def download(self, provider_payload, language, config):
        return provider_payload["archive"]

    def select_archive_member(self, provider_payload, language, members, config):
        # A provider may use these fields to accept, pin or defer an archive.
        # Incorrect context must be observable even when the host would defer.
        context = {key: provider_payload.get(key) for key in ("season", "episode")}
        if context != config["requested"]:
            return {"decision": "pin", "member": "Stale.S99E99.srt"}
        decision = config["decision"]
        return {"decision": decision, "member": members[0] if decision == "pin" else None}
''', encoding="utf-8")
    runner = Path(__file__).parents[2] / "bazarr" / "provider_hub" / "worker_runner.py"
    client = ProviderWorkerClient(
        worker_command(sys.executable, runner), cwd=tmp_path,
        env={"BAZARR_PROVIDER_HUB_BUNDLE": str(tmp_path),
             "BAZARR_PROVIDER_HUB_MANIFEST": json.dumps({"entry_module": "provider", "entry_class": "Provider"})},
    )
    try:
        yield client
    finally:
        process = client.process
        client.stop()
        if process is not None:
            assert process.poll() is not None


@pytest.mark.parametrize("context,expected", [
    ({}, {"season": 99, "episode": 99}),
    ({"season": None}, {"season": None, "episode": 99}),
    ({"episode": None}, {"season": 99, "episode": None}),
])
def test_real_worker_preserves_legacy_context_only_for_absent_keys(selector_worker, context, expected):
    response = selector_worker.select_archive_member({
        "provider_payload": {"season": 99, "episode": 99},
        "members": ["Requested.srt", "Stale.S99E99.srt"],
        "config": {"requested": expected, "decision": "pin"},
        **context,
    }, timeout=5)
    assert response.payload == {"decision": "pin", "member": "Requested.srt"}


@pytest.mark.parametrize("kind,season,episode,member", [
    ("movie", None, None, "Film.srt"),
    ("season_only", 3, None, "Show.S03E01.srt"),
    ("unknown_season", None, 1, "Show.S04E01.srt"),
])
@pytest.mark.parametrize("opaque", [{}, {"season": None, "episode": None}, {"season": 99, "episode": 99}],
                         ids=["missing", "null", "stale"])
@pytest.mark.parametrize("decision", ["pin", "defer", "reject"])
def test_registry_selector_preserves_empty_context_through_real_worker(
        selector_worker, kind, season, episode, member, opaque, decision):
    from provider_hub.registry import HubProxyProvider
    from subzero.language import Language
    from subliminal_patch.core import Episode, Movie

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, _SRT)
        archive.writestr("Stale.S99E99.srt", _SRT.replace(b"\nx\n", b"\nwrong member\n"))
    payload = _archive_payload(buffer.getvalue(), select_member=True, season=99, episode=99)
    provider = HubProxyProvider(worker_client=selector_worker, archive=payload, opaque=opaque,
                                requested={"season": season, "episode": episode}, decision=decision)
    provider.provider_name = "fixture"
    video = (Movie("/fixtures/Film.mkv", "Film", year=2024) if kind == "movie" else
             Episode("/fixtures/Show.mkv", "Show", season, episode))
    subtitle = provider.list_subtitles(video, {Language("eng")})[0]
    stored = {"archive": payload, **opaque}
    assert subtitle.provider_payload == stored

    # Repeat the download to expose mutation of the stored opaque payload.
    for _ in range(2):
        if decision == "reject":
            with pytest.raises(SubtitleCandidateRejected, match="No matching subtitle language"):
                provider.download_subtitle(subtitle)
        else:
            assert provider.download_subtitle(subtitle) is True
            assert subtitle.content == _SRT
        assert subtitle.provider_payload == stored


def test_worker_runner_select_archive_member_rejects_when_unimplemented():
    from provider_hub import worker_runner

    class P:
        pass

    with pytest.raises(ValueError, match="not implemented"):
        worker_runner._handle(P(), "select_archive_member", {"members": ["a.srt"]})


def test_worker_runner_select_archive_member_rejects_bad_decision():
    from provider_hub import worker_runner

    class P:
        def select_archive_member(self, provider_payload, language, members, config):
            return {"member": None, "decision": "weird"}

    with pytest.raises(ValueError, match="invalid decision"):
        worker_runner._handle(P(), "select_archive_member", {"members": ["a.srt"]})


def test_select_member_callback_receives_listed_members():
    # Design note: see test_list_archive_members_filters_to_subtitles. The worker
    # gets every member it could reasonably pin, ".txt" included and ordered
    # last, because only the worker can tell movie.sr.txt from notes.txt.
    body = _zip(["show.eng.srt", "show.fre.srt", "notes.txt"])
    seen = {}

    def cb(members):
        seen["members"] = list(members)
        return {"member": "show.eng.srt", "decision": "pin"}

    _run(body, _archive_payload(body, select_member=True), cb)
    assert seen["members"] == ["show.eng.srt", "show.fre.srt", "notes.txt"]

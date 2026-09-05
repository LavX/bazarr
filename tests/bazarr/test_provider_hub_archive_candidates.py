"""Archive mismatch rejects one download without disabling its provider."""
import base64
import hashlib
import io
import logging
import stat
import zipfile

import pytest
from subzero.language import Language

from provider_hub import protocol, worker_runner
from subliminal_patch import core, core_persistent
from subliminal_patch.providers.utils import get_archive_from_bytes, get_subtitle_from_archive


SRT = b"1\n00:00:01,000 --> 00:00:02,000\nFixture\n"


def archive_payload(names, **extra):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, SRT)
    body = buffer.getvalue()
    return {"archive_b64": base64.b64encode(body).decode("ascii"),
            "archive_sha256": hashlib.sha256(body).hexdigest(), "episode": 1, **extra}


def candidate(identifier="result"):
    subtitle = protocol.HubWorkerSubtitle("fixture", "fixture", identifier, Language("eng"), {})
    subtitle.episode = 1
    subtitle.season = 3
    return subtitle


def pool_for(monkeypatch, payload, selector=None):
    downloads = []
    throttled = []

    class Provider:
        def initialize(self):
            pass

        def terminate(self):
            pass

        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            result = payload if len(downloads) == 1 else archive_payload(["Show.S03E01.srt"])
            protocol.worker_download_to_content(subtitle, result, select_member_cb=selector)

    monkeypatch.setattr(core, "provider_registry", {"fixture": Provider})
    pool = core.SZProviderPool(["fixture"], {}, throttle_callback=lambda *a, **kw: throttled.append(a))
    return pool, downloads, throttled


@pytest.mark.parametrize("payload,selector", [
    (archive_payload(["Show.S03E03.srt", "Show.S03E03.cyr.srt"]), None),
    (archive_payload(["Show.S03E03.srt"]), None),
    (archive_payload(["poster.jpg"]), None),
    (archive_payload(["Show.S03E01.srt"], member="missing.srt"), None),
    (archive_payload(["Show.S03E01.srt"], select_member=True),
     lambda members: {"decision": "pin", "member": "missing.srt"}),
    (archive_payload(["Show.S03E01.srt"], select_member=True),
     lambda members: {"decision": "reject", "member": None}),
    (archive_payload(["Show.S03E03.srt"], select_member=True),
     lambda members: {"decision": "defer", "member": None}),
])
def test_rejected_archive_allows_next_candidate_from_same_provider(monkeypatch, payload, selector):
    pool, downloads, throttled = pool_for(monkeypatch, payload, selector)
    rejected, accepted = candidate("first"), candidate("second")
    assert pool.download_subtitle(rejected) is False
    assert pool.download_subtitle(accepted) is True
    assert accepted.content == SRT
    assert rejected.content is None
    assert downloads == [rejected, accepted]
    assert throttled == []
    assert pool.discarded_providers == set()


@pytest.mark.parametrize("names,extra", [
    (["Show.S03E01.srt"], {}),
    (["subtitle.srt"], {}),
    (["Show.S03E03.srt"], {"first_subtitle": True}),
    (["Show.S03E03.srt"], {"member": "Show.S03E03.srt"}),
    (["Show.S03E03.srt", "Show.S03E01.srt"], {}),
])
def test_legitimate_selection_and_explicit_overrides_still_download(names, extra):
    subtitle = candidate()
    assert protocol.worker_download_to_content(subtitle, archive_payload(names, **extra)) is True
    assert subtitle.content == SRT


def test_authoritative_selector_pin_is_not_overridden_by_host_episode():
    subtitle = candidate()
    assert protocol.worker_download_to_content(
        subtitle, archive_payload(["Show.S03E03.srt"], select_member=True),
        select_member_cb=lambda members: {"decision": "pin", "member": members[0]}) is True


def test_legacy_archive_helper_keeps_single_member_behavior():
    payload = archive_payload(["Show.S03E03.srt"])
    archive = get_archive_from_bytes(base64.b64decode(payload["archive_b64"]))
    assert get_subtitle_from_archive(archive, episode=1) == SRT


@pytest.mark.parametrize('name', ['Show.S03E01E02.srt', 'Show.S03E01-E02.srt',
                                  'Show.S03E01-02.srt', 'Show.3x01-02.srt'])
@pytest.mark.parametrize('episode', [1, 2, 3])
def test_single_member_with_multiple_episodes_matches_any_included_episode(name, episode):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    subtitle = candidate()
    payload = archive_payload([name], episode=episode)
    if episode == 3:
        with pytest.raises(SubtitleCandidateRejected):
            protocol.worker_download_to_content(subtitle, payload)
    else:
        assert protocol.worker_download_to_content(subtitle, payload) is True
        assert subtitle.content == SRT


@pytest.mark.parametrize("response", [None, [], "reject", {}, {"decision": "unknown"},
                                        {"decision": "pin"}, {"decision": "pin", "member": []},
                                        {"decision": "reject", "member": []},
                                        {"decision": "defer", "member": "unexpected.srt"},
                                        {"decision": "pin", "member": "../unsafe.srt"}])
def test_malformed_selector_keeps_provider_error_handling(monkeypatch, response):
    pool, downloads, throttled = pool_for(
        monkeypatch, archive_payload(["valid.srt"], select_member=True), lambda members: response)
    assert pool.download_subtitle(candidate()) is False
    assert pool.download_subtitle(candidate("second")) is False
    assert len(downloads) == len(throttled) == 1
    assert pool.discarded_providers == {"fixture"}


@pytest.mark.parametrize("response", [None, [], {}, {"decision": "unknown"}])
def test_runner_does_not_turn_malformed_selector_into_candidate_rejection(response):
    class Provider:
        def select_archive_member(self, **kwargs):
            return response

    with pytest.raises(ValueError):
        worker_runner._handle(Provider(), "select_archive_member", {})


@pytest.mark.parametrize("payload", [
    [], {"archive_b64": 123}, {"archive_b64": "!invalid!"},
    archive_payload(["valid.srt"], archive_sha256="0" * 64),
    {"archive_b64": base64.b64encode(b"invalid archive").decode("ascii")},
    archive_payload(["valid.srt"], member=[]),
    archive_payload(["valid.srt"], episode="1"),
    archive_payload(["valid.srt"], first_subtitle="yes"),
    archive_payload(["valid.srt"], select_member=True),
    archive_payload(["../unsafe.srt"]),
    archive_payload(["/absolute.srt"]),
])
def test_protocol_integrity_and_unsafe_payloads_still_discard_provider(monkeypatch, payload):
    pool, downloads, throttled = pool_for(monkeypatch, payload)
    assert pool.download_subtitle(candidate()) is False
    assert len(downloads) == len(throttled) == 1
    assert pool.discarded_providers == {"fixture"}


def test_archive_bomb_is_not_an_ordinary_candidate_rejection(monkeypatch):
    monkeypatch.setattr(protocol, "_MAX_MEMBER_BYTES", 1)
    pool, downloads, throttled = pool_for(monkeypatch, archive_payload(["wrong.S03E03.srt"]))
    assert pool.download_subtitle(candidate()) is False
    assert len(downloads) == len(throttled) == 1
    assert pool.discarded_providers == {"fixture"}


def test_unsafe_link_is_checked_before_language_or_episode_rejection(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        member = zipfile.ZipInfo("Show.S03E03.srt")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "private-target")
    payload = {"archive_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
               "episode": 1, "select_member": True}
    pool, _, throttled = pool_for(monkeypatch, payload, lambda names: {"decision": "reject"})
    assert pool.download_subtitle(candidate()) is False
    assert len(throttled) == 1
    assert pool.discarded_providers == {"fixture"}


def test_rejection_diagnostic_is_bounded_and_excludes_paths_urls_and_contents(monkeypatch, caplog):
    names = [f"private-folder/Show.S03E03.variant{index}.srt" for index in range(30)]
    pool, _, _ = pool_for(monkeypatch, archive_payload(names))
    with caplog.at_level(logging.WARNING, logger='subliminal_patch'):
        assert pool.download_subtitle(candidate("https://user:password@example.invalid/?token=secret")) is False
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Show.S03E03.variant0.srt" in messages
    assert "private-folder" not in messages
    assert "password" not in messages
    assert "token=secret" not in messages
    assert "Fixture" not in messages
    assert len(messages) < 1600


def selection_candidate(identifier, payload=None, selector=None, language="eng", provider="fixture"):
    subtitle = candidate(identifier)
    subtitle.provider_name = provider
    subtitle.language = Language(language)
    subtitle.matches = {"series", "season", "episode"}
    subtitle.release_info = identifier
    subtitle.provider_payload = {"result": payload if payload is not None else archive_payload(["Show.S03E01.srt"]),
                                 "selector": selector}
    return subtitle


def selection_pool_for(monkeypatch):
    downloads, throttled = [], []

    class Provider:
        def initialize(self):
            pass

        def terminate(self):
            pass

        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            protocol.worker_download_to_content(subtitle, subtitle.provider_payload["result"],
                                                select_member_cb=subtitle.provider_payload["selector"])

    monkeypatch.setattr(core, "provider_registry", {"fixture": Provider, "other": Provider})
    pool = core.SZProviderPool(["fixture", "other"], {}, throttle_callback=lambda *a, **kw: throttled.append(a))
    return pool, downloads, throttled


def select_best(pool, subtitles, only_one=True, languages=None, existing_languages=None):
    video = core.Episode("/fixtures/Show.S03E01.mkv", "Show", 3, 1,
                         subtitle_languages=existing_languages or set())
    listed_languages, sink = [], []

    def list_subtitles(video, languages, **kwargs):
        listed_languages.append(languages)
        return subtitles

    pool.list_subtitles_prioritized = list_subtitles
    result = core_persistent.download_best_subtitles(
        {video}, languages or {Language("eng")}, pool, only_one=only_one, candidate_sink=sink)
    return result.get(video, []), listed_languages, sink


@pytest.mark.parametrize("only_one", [False, True])
@pytest.mark.parametrize("payload,selector", [
    (archive_payload(["Show.S03E03.srt", "Show.S03E03.alt.srt"]), None),
    (archive_payload(["Show.S03E03.srt"]), None),
    (archive_payload(["poster.jpg"]), None),
    (archive_payload(["Show.S03E01.srt"], member="missing.srt"), None),
    (archive_payload(["Show.S03E01.srt"], select_member=True),
     lambda members: {"decision": "pin", "member": "missing.srt"}),
    (archive_payload(["Show.S03E01.srt"], select_member=True),
     lambda members: {"decision": "reject"}),
    (archive_payload(["Show.S03E03.srt"], select_member=True),
     lambda members: {"decision": "defer"}),
])
def test_selection_loop_continues_after_archive_rejection(monkeypatch, only_one, payload, selector):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    rejected = selection_candidate("first", payload, selector)
    accepted = selection_candidate("second")

    result, _, sink = select_best(pool, [rejected, accepted], only_one=only_one)

    assert result == [accepted]
    assert accepted.content == SRT
    assert rejected.content is None
    assert downloads == [rejected, accepted]
    assert [row["downloaded"] for row in sink] == [False, True]
    assert throttled == []
    assert pool.discarded_providers == set()


def test_single_subtitle_selection_stops_after_first_success(monkeypatch):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    first, duplicate = selection_candidate("first"), selection_candidate("duplicate")
    other_language = selection_candidate("other-language", language="fra")

    result, _, sink = select_best(pool, [first, duplicate, other_language],
                                 languages={Language("eng"), Language("fra")})

    assert result == downloads == [first]
    assert first.content == SRT
    assert duplicate.content is other_language.content is None
    assert [row["downloaded"] for row in sink] == [True, False, False]
    assert not throttled and not pool.discarded_providers


def test_single_subtitle_selection_exhausts_rejected_candidates(monkeypatch):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    subtitles = [selection_candidate(str(index), archive_payload(["Show.S03E03.srt"]))
                 for index in range(3)]

    result, _, sink = select_best(pool, subtitles)

    assert result == []
    assert downloads == subtitles
    assert not any(row["downloaded"] for row in sink)
    assert not throttled and not pool.discarded_providers


def test_single_subtitle_selection_continues_after_invalid_subtitle_content(monkeypatch):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    invalid = selection_candidate("invalid", {"content_b64": base64.b64encode(b"not subtitles").decode("ascii")})
    accepted = selection_candidate("accepted")

    result, _, _ = select_best(pool, [invalid, accepted])

    assert result == [accepted]
    assert downloads == [invalid, accepted]
    assert accepted.content == SRT
    assert not throttled and not pool.discarded_providers


@pytest.mark.parametrize("payload,selector", [
    (archive_payload(["Show.S03E01.srt"], select_member=True), lambda members: {"decision": "unknown"}),
    (archive_payload(["Show.S03E01.srt"], archive_sha256="0" * 64), None),
    (archive_payload(["../unsafe.srt"]), None),
])
def test_single_subtitle_selection_preserves_provider_failure_boundary(monkeypatch, payload, selector):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    rejected = selection_candidate("first", payload, selector)
    same_provider = selection_candidate("same-provider")
    other_provider = selection_candidate("other-provider", provider="other")

    result, _, sink = select_best(pool, [rejected, same_provider, other_provider])

    assert result == [other_provider]
    assert downloads == [rejected, other_provider]
    assert len(throttled) == 1
    assert pool.discarded_providers == {"fixture"}
    assert same_provider.content is None
    assert [row["downloaded"] for row in sink] == [False, False, True]


def test_multiple_subtitle_selection_keeps_language_deduplication(monkeypatch):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    first, duplicate = selection_candidate("first"), selection_candidate("duplicate")
    other_language = selection_candidate("other-language", language="fra")

    result, _, _ = select_best(pool, [first, duplicate, other_language], only_one=False,
                              languages={Language("eng"), Language("fra")})

    assert result == downloads == [first, other_language]
    assert duplicate.content is None
    assert not throttled and not pool.discarded_providers


@pytest.mark.parametrize("only_one", [False, True])
def test_existing_requested_language_avoids_selection(monkeypatch, only_one):
    pool, downloads, throttled = selection_pool_for(monkeypatch)

    result, listed_languages, sink = select_best(pool, [selection_candidate("unused")], only_one=only_one,
                                                existing_languages={Language("eng")})

    assert result == downloads == listed_languages == sink == throttled == []


def test_existing_language_is_excluded_from_the_caller_search(monkeypatch):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    accepted = selection_candidate("missing-language", language="fra")

    result, listed_languages, _ = select_best(pool, [accepted], only_one=False,
                                             languages={Language("eng"), Language("fra")},
                                             existing_languages={Language("eng")})

    assert result == downloads == [accepted]
    assert listed_languages == [{Language("fra")}]
    assert not throttled and not pool.discarded_providers

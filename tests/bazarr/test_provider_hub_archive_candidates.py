"""Archive mismatch rejects one download without disabling its provider."""
import base64
import hashlib
import io
import logging
import stat
from types import SimpleNamespace
import zipfile

import pytest
from subzero.language import Language

from provider_hub import protocol, registry, worker_runner
from subliminal_patch import core, core_persistent
from subliminal_patch.providers.utils import get_archive_from_bytes, get_subtitle_from_archive


SRT = b"1\n00:00:01,000 --> 00:00:02,000\nFixture\n"


def archive_payload(names, **extra):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        members = names.items() if isinstance(names, dict) else ((name, SRT) for name in names)
        for name, content in members:
            archive.writestr(name, content)
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


@pytest.mark.parametrize("name", ["Show.S03E01E02.srt", "Show.S03E01-E02.srt",
                                  "Show.S03E01-02.srt", "Show.3x01-02.srt"])
@pytest.mark.parametrize("episode", [1, 2, 3])
@pytest.mark.parametrize("defer", [False, True])
def test_multi_member_combined_episode_selects_the_included_member(name, episode, defer):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    subtitle = candidate()
    subtitle.episode = episode
    payload = archive_payload({"Show.S03E09.srt": SRT.replace(b"Fixture", b"Wrong episode"), name: SRT},
                              episode=episode, select_member=defer)
    selector = (lambda members: {"decision": "defer"}) if defer else None
    if episode == 3:
        with pytest.raises(SubtitleCandidateRejected):
            protocol.worker_download_to_content(subtitle, payload, select_member_cb=selector)
        assert subtitle.content is None
    else:
        assert protocol.worker_download_to_content(subtitle, payload, select_member_cb=selector) is True
        assert subtitle.content == SRT


@pytest.mark.parametrize("names", [["Show.S04E01.srt"], ["Show.S04E01.srt", "Show.S05E01.srt"]])
@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize("only_one", [False, True])
def test_wrong_season_continues_to_next_valid_candidate(monkeypatch, names, defer, only_one):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    selector = (lambda members: {"decision": "defer"}) if defer else None
    rejected = selection_candidate("wrong-season", archive_payload(names, select_member=defer), selector)
    accepted = selection_candidate("accepted")

    result, _, sink = select_best(pool, [rejected, accepted], only_one=only_one)

    assert result == downloads[-1:] == [accepted]
    assert downloads == [rejected, accepted]
    assert rejected.content is None and accepted.content == SRT
    assert [row["downloaded"] for row in sink] == [False, True]
    assert throttled == [] and pool.discarded_providers == set()


def test_multi_member_wrong_season_cannot_win_title_ranking():
    subtitle = candidate()
    payload = archive_payload({"Show.S04E01 - Mondo Magic.srt": SRT.replace(b"Fixture", b"Wrong season"),
                               "Show.S03E01.srt": SRT}, episode_title="Mondo Magic")
    assert protocol.worker_download_to_content(subtitle, payload) is True
    assert subtitle.content == SRT


def test_host_matching_keeps_same_season_episode_title_priority():
    subtitle = candidate()
    subtitle.episode = 17
    payload = archive_payload({"Show.S03E17.srt": SRT.replace(b"Fixture", b"Numeric match"),
                               "Show.S03E35 - Mondo Magic.srt": SRT}, episode=17, episode_title="Mondo Magic")
    assert protocol.worker_download_to_content(subtitle, payload) is True
    assert subtitle.content == SRT


def test_host_matching_keeps_forced_filtering():
    subtitle = candidate()
    payload = archive_payload({"Show.S03E01.forced.srt": SRT.replace(b"Fixture", b"Forced match"),
                               "Show.S03E01.srt": SRT})
    assert protocol.worker_download_to_content(subtitle, payload) is True
    assert subtitle.content == SRT


@pytest.mark.parametrize("names", [["Show.S04E01.srt"], ["Show.S04E01.srt", "Show.S05E01.srt"]])
def test_unknown_season_does_not_reject_episode_match(names):
    subtitle = candidate()
    subtitle.season = None
    assert protocol.worker_download_to_content(subtitle, archive_payload(names)) is True
    assert subtitle.content == SRT


def test_legacy_matching_keeps_first_episode_and_ignores_season_context():
    payload = archive_payload({"Show.S03E01E02.srt": SRT.replace(b"Fixture", b"Combined match"),
                               "Show.S04E02.srt": SRT}, episode=2)
    archive = get_archive_from_bytes(base64.b64decode(payload["archive_b64"]))
    assert get_subtitle_from_archive(archive, episode=2, season=3) == SRT


@pytest.mark.parametrize("name", ["subtitle.srt", "Show.E01.srt", "Show.S03.srt", "Show.S03E01.srt"])
def test_single_member_missing_context_markers_remains_usable(name):
    subtitle = candidate()
    assert protocol.worker_download_to_content(subtitle, archive_payload([name])) is True
    assert subtitle.content == SRT


def test_wire_season_is_used_when_subtitle_has_no_season():
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    subtitle = candidate()
    subtitle.season = None
    with pytest.raises(SubtitleCandidateRejected):
        protocol.worker_download_to_content(subtitle, archive_payload(["Show.S04E01.srt"], season=3))


@pytest.mark.parametrize("member_season", [3, 4])
def test_subtitle_season_takes_precedence_over_conflicting_wire_hint(member_season):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    subtitle = candidate()
    payload = archive_payload([f"Show.S0{member_season}E01.srt"], season=4)
    if member_season == 4:
        with pytest.raises(SubtitleCandidateRejected):
            protocol.worker_download_to_content(subtitle, payload)
    else:
        assert protocol.worker_download_to_content(subtitle, payload) is True
        assert subtitle.content == SRT


class ArchiveSearchWorker:
    def __init__(self, payload, display=None, selector=None):
        self.payload = payload
        self.display = display or {}
        self.selector = selector or {"decision": "defer"}
        self.selections = []
        self.searches = []

    def request(self, operation, request, timeout):
        if operation == "search":
            self.searches.append(request["video"])
            payload = self.payload(request["video"]) if callable(self.payload) else self.payload
            return SimpleNamespace(payload={"candidates": [{
                "provider": "fixture", "id": str(len(self.searches)), "language": {"alpha3": "eng"},
                "provider_payload": {"result": payload, "season": 99, "episode": 99},
                "display": self.display, "matches": ["series", "season", "episode"],
            }]})
        assert operation == "download"
        assert request["provider_payload"]["season"] == request["provider_payload"]["episode"] == 99
        return SimpleNamespace(payload=request["provider_payload"]["result"])

    def select_archive_member(self, request, timeout):
        self.selections.append(request)
        return SimpleNamespace(payload=self.selector)


def registry_archive_search(payload, display=None, selector=None, season=3, episode=1, absolute_episode=None):
    worker = ArchiveSearchWorker(payload, display, selector)
    provider = registry.HubProxyProvider(timeout=120, worker_client=worker)
    provider.provider_name = "fixture"
    video = core.Episode("/fixtures/Show.mkv", "Show", season, episode)
    video.absolute_episode = absolute_episode
    subtitles = provider.list_subtitles(video, {Language("eng")})
    assert len(subtitles) == 1
    assert worker.searches[0]["season"] == season and worker.searches[0]["episode"] == episode
    assert worker.searches[0]["absolute_episode"] == absolute_episode
    return provider, worker, subtitles[0]


@pytest.mark.parametrize("name", ["Show.S03E01.srt", "49.srt", "Show.E49.srt", "Show.S03E48E49.srt"])
@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize("episode_hint", [None, 49, 99])
def test_registry_archive_accepts_relative_or_absolute_request(name, multiple, defer, episode_hint):
    names = {"Show.S03E07.srt": SRT.replace(b"Fixture", b"Unrelated")} if multiple else {}
    names[name] = SRT
    payload = archive_payload(names, episode=episode_hint, select_member=defer)
    provider, worker, subtitle = registry_archive_search(payload, absolute_episode=49)

    assert provider.download_subtitle(subtitle) is True
    assert subtitle.content == SRT
    if defer:
        assert [(item["season"], item["episode"]) for item in worker.selections] == [(3, 1)]


@pytest.mark.parametrize("names", [["48.srt"], ["Show.S04E49.srt"],
                                   ["Show.S04E49.srt", "Show.S04E01.srt"]])
@pytest.mark.parametrize("defer", [False, True])
def test_registry_absolute_context_rejects_wrong_episode_and_season(names, defer):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    provider, _, subtitle = registry_archive_search(
        archive_payload(names, episode=49, select_member=defer), absolute_episode=49)
    with pytest.raises(SubtitleCandidateRejected):
        provider.download_subtitle(subtitle)
    assert subtitle.content is None


@pytest.mark.parametrize("defer", [False, True])
def test_registry_absolute_context_cannot_be_replaced_by_display_or_wire(defer):
    payload = archive_payload({"50.srt": SRT.replace(b"Fixture", b"Forged absolute"), "49.srt": SRT},
                              episode=50, select_member=defer)
    display = {"episode": 50, "absolute_episode": 50,
               "_requested_archive_context": {"episode": 50, "absolute_episode": 50}}
    provider, _, subtitle = registry_archive_search(payload, display=display, absolute_episode=49)
    assert provider.download_subtitle(subtitle) is True
    assert subtitle.content == SRT
    assert subtitle.episode == subtitle.absolute_episode == 50


@pytest.mark.parametrize("absolute_episode", [None, True, "49", -49])
def test_registry_unrequested_absolute_number_is_not_invented(absolute_episode):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    provider, _, subtitle = registry_archive_search(
        archive_payload(["49.srt"], episode=49),
        display={"absolute_episode": 49}, absolute_episode=absolute_episode)
    with pytest.raises(SubtitleCandidateRejected):
        provider.download_subtitle(subtitle)


@pytest.mark.parametrize("episode,absolute_episode,name", [(None, 49, "49.srt"), (1, 1, "Show.S03E01.srt")])
def test_registry_absolute_only_or_duplicate_numbering(episode, absolute_episode, name):
    provider, _, subtitle = registry_archive_search(archive_payload([name], episode=None),
                                                   episode=episode, absolute_episode=absolute_episode)
    assert provider.download_subtitle(subtitle) is True
    assert subtitle.content == SRT


def test_registry_absolute_context_is_kept_per_candidate_across_searches():
    def payload(video):
        return archive_payload([f"{video['absolute_episode']}.srt"], episode=None, select_member=True)

    provider, worker, first = registry_archive_search(payload, absolute_episode=49)
    video = core.Episode("/fixtures/Other.mkv", "Show", 3, 2)
    video.absolute_episode = 50
    second = provider.list_subtitles(video, {Language("eng")})[0]
    video.absolute_episode = 51
    assert provider.download_subtitle(first) is True
    assert provider.download_subtitle(second) is True
    assert first.content == second.content == SRT
    assert [(item["season"], item["episode"]) for item in worker.selections] == [(3, 1), (3, 2)]


@pytest.mark.parametrize("override,selector", [
    ({"member": "Show.S04E99.srt"}, None),
    ({"first_subtitle": True}, None),
    ({"select_member": True}, {"decision": "pin", "member": "Show.S04E99.srt"}),
])
def test_registry_absolute_context_keeps_authoritative_pins(override, selector):
    provider, _, subtitle = registry_archive_search(archive_payload(["Show.S04E99.srt"], **override),
                                                   selector=selector, absolute_episode=49)
    assert provider.download_subtitle(subtitle) is True
    assert subtitle.content == SRT


@pytest.mark.parametrize("names", [["Show.S04E01.srt"], ["Show.S04E01.srt", "Show.S04E02.srt"]])
@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize("episode_hint", ["absent", None, 1])
def test_registry_request_context_rejects_wrong_season(names, defer, episode_hint):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    payload = archive_payload(names, episode=episode_hint, select_member=defer)
    if episode_hint == "absent":
        payload.pop("episode")
    provider, _, subtitle = registry_archive_search(payload)
    with pytest.raises(SubtitleCandidateRejected):
        provider.download_subtitle(subtitle)
    assert subtitle.content is None


@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize("episode_hint", [None, 9])
def test_registry_request_overrides_display_and_wire_context(defer, episode_hint):
    display = {"season": 4, "episode": 9,
               "_requested_archive_context": {"season": 4, "episode": 9}}
    payload = archive_payload({"Show.S04E09.srt": SRT.replace(b"Fixture", b"Display match"),
                               "Show.S03E01.srt": SRT}, episode=episode_hint, season=4, select_member=defer)
    provider, worker, subtitle = registry_archive_search(payload, display)
    assert subtitle.season == 4 and subtitle.episode == 9
    assert provider.download_subtitle(subtitle) is True
    assert subtitle.content == SRT
    assert subtitle.season == 4 and subtitle.episode == 9
    if defer:
        assert [(r["season"], r["episode"]) for r in worker.selections] == [(3, 1)]


def test_registry_request_context_is_kept_per_candidate_across_searches():
    def payload(video):
        return archive_payload([f"Show.S0{video['season']}E01.srt"], episode=None, select_member=True)

    provider, worker, first = registry_archive_search(payload)
    second = provider.list_subtitles(core.Episode("/fixtures/Other.mkv", "Show", 4, 1), {Language("eng")})[0]
    assert provider.download_subtitle(first) is True
    assert provider.download_subtitle(second) is True
    assert first.content == second.content == SRT
    assert [(r["season"], r["episode"]) for r in worker.selections] == [(3, 1), (4, 1)]


@pytest.mark.parametrize("override,selector", [
    ({"member": "Show.S04E09.srt"}, None),
    ({"first_subtitle": True}, None),
    ({"select_member": True}, {"decision": "pin", "member": "Show.S04E09.srt"}),
])
def test_registry_request_context_keeps_authoritative_overrides(override, selector):
    provider, _, subtitle = registry_archive_search(archive_payload(["Show.S04E09.srt"], **override),
                                                    selector=selector)
    assert provider.download_subtitle(subtitle) is True
    assert subtitle.content == SRT


@pytest.mark.parametrize("matched", [False, True])
def test_host_season_only_context_filters_before_movie_shortcut(matched):
    from subliminal_patch.exceptions import SubtitleCandidateRejected

    subtitle = candidate()
    subtitle.episode = None
    names = {"Show.S04E01.srt": SRT.replace(b"Fixture", b"Wrong season"),
             "Show.S03E02.srt" if matched else "Show.S04E02.srt": SRT}
    payload = archive_payload(names, episode=None)
    if matched:
        assert protocol.worker_download_to_content(subtitle, payload) is True
        assert subtitle.content == SRT
    else:
        with pytest.raises(SubtitleCandidateRejected):
            protocol.worker_download_to_content(subtitle, payload)


@pytest.mark.parametrize("season", [True, "3", -1, [], {}])
def test_malformed_wire_season_remains_a_protocol_error(season):
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.worker_download_to_content(candidate(), archive_payload(["Show.S03E01.srt"], season=season))


@pytest.mark.parametrize("override", [{"first_subtitle": True}, {"member": "Show.S04E01.srt"}])
def test_authoritative_download_override_keeps_wrong_season_member(override):
    subtitle = candidate()
    assert protocol.worker_download_to_content(subtitle, archive_payload(["Show.S04E01.srt"], **override)) is True
    assert subtitle.content == SRT


@pytest.mark.parametrize("member", ["Show.S04E01.srt", "Show.S04E01.txt"])
def test_authoritative_selector_pin_keeps_offered_wrong_season_member(member):
    subtitle = candidate()
    assert protocol.worker_download_to_content(
        subtitle, archive_payload([member], select_member=True),
        select_member_cb=lambda members: {"decision": "pin", "member": member}) is True
    assert subtitle.content == SRT


@pytest.mark.parametrize("member", ["poster.jpg", ".hidden.srt", "missing.srt"])
def test_out_of_offer_selector_pin_keeps_provider_error_boundary(monkeypatch, member):
    pool, downloads, throttled = selection_pool_for(monkeypatch)
    offered = []

    def selector(members):
        offered.extend(members)
        members.append(member)
        return {"decision": "pin", "member": member}

    rejected = selection_candidate("invalid-pin", archive_payload(
        ["Show.S03E01.srt", "poster.jpg", ".hidden.srt"], select_member=True), selector)
    same_provider = selection_candidate("same-provider")
    other_provider = selection_candidate("other-provider", provider="other")

    result, _, sink = select_best(pool, [rejected, same_provider, other_provider])

    assert offered == ["Show.S03E01.srt"]
    assert result == [other_provider]
    assert downloads == [rejected, other_provider]
    assert rejected.content is same_provider.content is None
    assert len(throttled) == 1 and pool.discarded_providers == {"fixture"}
    assert [row["downloaded"] for row in sink] == [False, False, True]


@pytest.mark.parametrize("response", [None, [], "reject", {}, {"decision": "unknown"},
                                        {"decision": "pin"}, {"decision": "pin", "member": []},
                                        {"decision": "reject", "member": []},
                                        {"decision": "defer", "member": "unexpected.srt"},
                                        {"decision": "pin", "member": "missing.srt"},
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
def test_selection_loop_keeps_absolute_context_after_rejected_archive(monkeypatch, only_one):
    provider, worker, rejected = registry_archive_search(
        archive_payload(["48.srt"], episode=49, select_member=True), absolute_episode=49)
    worker.payload = archive_payload(["49.srt"], episode=None, select_member=True)
    video = core.Episode("/fixtures/Show.mkv", "Show", 3, 1)
    video.absolute_episode = 49
    accepted = provider.list_subtitles(video, {Language("eng")})[0]
    downloads, throttled = [], []

    class Provider:
        def initialize(self):
            pass

        def terminate(self):
            pass

        def download_subtitle(self, subtitle):
            downloads.append(subtitle)
            return provider.download_subtitle(subtitle)

    monkeypatch.setattr(core, "provider_registry", {"fixture": Provider})
    pool = core.SZProviderPool(["fixture"], {}, throttle_callback=lambda *a, **kw: throttled.append(a))
    result, _, sink = select_best(pool, [rejected, accepted], only_one=only_one)

    assert result == [accepted]
    assert accepted.content == SRT and rejected.content is None
    assert downloads == [rejected, accepted]
    assert [row["downloaded"] for row in sink] == [False, True]
    assert [(item["season"], item["episode"]) for item in worker.selections] == [(3, 1), (3, 1)]
    assert throttled == [] and pool.discarded_providers == set()


@pytest.mark.parametrize("only_one", [False, True])
@pytest.mark.parametrize("payload,selector", [
    (archive_payload(["Show.S03E03.srt", "Show.S03E03.alt.srt"]), None),
    (archive_payload(["Show.S03E03.srt"]), None),
    (archive_payload(["poster.jpg"]), None),
    (archive_payload(["Show.S03E01.srt"], member="missing.srt"), None),
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

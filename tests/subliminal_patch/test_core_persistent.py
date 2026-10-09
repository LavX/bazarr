from collections import defaultdict  # noqa: F401
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from subliminal.exceptions import DownloadLimitExceeded

from subliminal_patch import core
from subliminal_patch.core_persistent import (
    download_best_subtitles,
    list_all_subtitles,
)
from subliminal_patch.exceptions import SubtitleCandidateRejected


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.list_subtitles_prioritized.side_effect = (
        lambda *args, report_stop=False, **kwargs: ([], None) if report_stop else [])
    pool.list_subtitles.return_value = []
    pool.download_best_subtitles.return_value = []
    return pool


@pytest.fixture
def mock_video():
    video = MagicMock()
    video.subtitle_languages = set()
    return video


def test_uses_prioritized_listing_by_default(mock_pool, mock_video):
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
        )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    mock_pool.list_subtitles.assert_not_called()


def test_uses_prioritized_listing_when_enabled(mock_pool, mock_video):
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
            use_provider_priority=True,
        )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    mock_pool.list_subtitles.assert_not_called()


def test_uses_regular_listing_when_disabled(mock_pool, mock_video):
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
            use_provider_priority=False,
        )

    mock_pool.list_subtitles.assert_called_once()
    mock_pool.list_subtitles_prioritized.assert_not_called()


def test_list_all_subtitles_requests_exhaustive(mock_pool, mock_video):
    """Why: Manual search must query every provider; the exhaustive flag is the
    only thing that disables the early-exit in list_subtitles_prioritized.
    What: list_all_subtitles calls list_subtitles_prioritized with exhaustive=True.
    Test: Inspect the call kwargs and assert exhaustive=True.
    """
    languages = {MagicMock()}

    list_all_subtitles(
        videos={mock_video},
        languages=languages,
        pool_instance=mock_pool,
        min_score=80,
    )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    _, kwargs = mock_pool.list_subtitles_prioritized.call_args
    assert kwargs.get("exhaustive") is True
    assert kwargs.get("min_score") == 80


def test_download_best_subtitles_does_not_request_exhaustive(mock_pool, mock_video):
    """Why: Auto/scheduled downloads must keep the early-exit so we stop after
    the first provider that satisfies all languages above min_score.
    What: download_best_subtitles never passes exhaustive=True.
    Test: Inspect call_args and assert exhaustive is False (and definitely not True).
    """
    languages = {MagicMock()}

    with patch("subliminal_patch.core_persistent.check_video", return_value=True):
        download_best_subtitles(
            videos={mock_video},
            languages=languages,
            pool_instance=mock_pool,
        )

    mock_pool.list_subtitles_prioritized.assert_called_once()
    _, kwargs = mock_pool.list_subtitles_prioritized.call_args
    assert kwargs.get("exhaustive", False) is False


# The waterfall below runs a real SZProviderPool against fake providers, so the
# listing, the download loop and the continuation are the production ones.

ENG = core.Language("eng")
FRA = core.Language("fra")


class _Candidate:
    hash_verifiable = False
    hearing_impaired = False
    hearing_impaired_verifiable = False
    release_info = None

    def __init__(self, provider_name, sub_id, language=ENG, outcome="ok",
                 matches=("series", "year", "season", "episode")):
        self.provider_name = provider_name
        self.id = sub_id
        self.language = language
        self.outcome = outcome
        self._matches = set(matches)

    def get_matches(self, video):
        return set(self._matches)

    def is_valid(self):
        return self.outcome != "invalid"

    def normalize(self):
        pass

    def __repr__(self):
        return f"<_Candidate {self.id}>"


class _Waterfall:
    """A pool whose providers list and download from the given candidates."""

    def __init__(self, monkeypatch, listings, on_list=None, provider_languages=None):
        self.listings = listings
        self.on_list = on_list or {}
        self.listed = []
        self.downloads = []
        self.throttled = []
        self.provider_languages = set(provider_languages) if provider_languages else {ENG, FRA}
        monkeypatch.setattr(core, "provider_registry",
                            {name: self._provider_class(name) for name in listings})
        monkeypatch.setattr("subliminal_patch.core_persistent.check_video", lambda *args, **kwargs: True)
        self.pool = core.SZProviderPool(
            list(listings), {},
            throttle_callback=lambda name, error, **context: self.throttled.append(name))

    def _provider_class(self, name):
        fake = self

        class Provider:
            languages = set(fake.provider_languages)

            def __init__(self, **config):
                pass

            @classmethod
            def check(cls, video):
                return True

            def initialize(self):
                pass

            def terminate(self):
                pass

            def list_subtitles(self, video, languages):
                wanted = sorted(language.alpha3 for language in languages)
                fake.listed.append((name, wanted))
                if name in fake.on_list:
                    fake.on_list[name]()
                return [c for c in fake.listings[name] if c.language.alpha3 in wanted]

            def download_subtitle(self, subtitle):
                fake.downloads.append(subtitle.id)
                if subtitle.outcome == "quota":
                    raise DownloadLimitExceeded("fixture quota")
                if subtitle.outcome == "rejected":
                    raise SubtitleCandidateRejected("fixture rejected")

        return Provider

    def search(self, languages=(ENG,), **kwargs):
        video = core.Episode("/m/Show.S01E01.mkv", "Show", 1, 1)
        found = download_best_subtitles({video}, set(languages), self.pool, min_score=1, **kwargs)
        return sorted(subtitle.id for subtitle in found[video])


def _messages(caplog, text):
    return [record.getMessage() for record in caplog.records if text in record.getMessage()]


@pytest.mark.parametrize("failure", ["quota", "rejected", "invalid"])
def test_the_search_moves_on_when_the_provider_that_satisfied_it_cannot_deliver(monkeypatch, caplog, failure):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome=failure), _Candidate("a", "a2", outcome=failure),
              _Candidate("a", "a3", outcome=failure)],
        "b": [_Candidate("b", "b1")],
    })
    sink = []

    with caplog.at_level(logging.INFO):
        assert fake.search(candidate_sink=sink) == ["b1"]

    assert fake.listed == [("a", ["eng"]), ("b", ["eng"])]
    assert [record["provider_name"] for record in sink] == ["a", "a", "a", "b"]
    assert len(_messages(caplog, "continuing with b")) == 1
    if failure == "quota":
        # The quota discards the provider; its other candidates are skipped
        # with one line instead of a warning each.
        assert fake.throttled == ["a"]
        assert "a" in fake.pool.discarded_providers
        assert fake.downloads == ["a1", "b1"]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "is discarded" in r.getMessage()]
        assert len(_messages(caplog, "remaining candidates of provider")) == 1
    else:
        assert fake.downloads == ["a1", "a2", "a3", "b1"]


def test_candidates_of_a_provider_another_search_discarded_are_skipped_quietly(monkeypatch, caplog):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1"), _Candidate("a", "a2")],
        "b": [_Candidate("b", "b1")],
    }, on_list={"a": lambda: fake.pool.discarded_providers.add("a")})

    with caplog.at_level(logging.INFO):
        assert fake.search() == ["b1"]

    assert fake.downloads == ["b1"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "is discarded" in r.getMessage()]
    assert len(_messages(caplog, "remaining candidates of provider")) == 1


def test_two_searches_move_on_when_one_spends_the_quota_the_other_listed(monkeypatch):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1")],
    })
    listed_a = threading.Barrier(2, timeout=5)
    first_moved_on = threading.Event()
    fake.on_list["b"] = lambda: threading.current_thread().name == "first" and first_moved_on.set()
    held = set()
    download_round = fake.pool.download_best_subtitles

    def download_best_subtitles(*args, **kwargs):
        name = threading.current_thread().name
        if name not in held:
            held.add(name)
            # Both searches have listed a before either downloads from it.
            listed_a.wait()
            if name == "second":
                # The second search downloads only once the first has spent
                # a's quota and gone on to b.
                assert first_moved_on.wait(5)
        return download_round(*args, **kwargs)

    monkeypatch.setattr(fake.pool, "download_best_subtitles", download_best_subtitles)
    results = {}

    def search():
        try:
            results[threading.current_thread().name] = fake.search()
        except Exception as error:
            results[threading.current_thread().name] = error

    threads = [threading.Thread(target=search, name=name, daemon=True) for name in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert results == {"first": ["b1"], "second": ["b1"]}
    assert fake.downloads == ["a1", "b1", "b1"]
    assert fake.throttled == ["a"]
    assert sorted(fake.listed) == [("a", ["eng"])] * 2 + [("b", ["eng"])] * 2


def test_a_provider_that_delivers_still_ends_the_search(monkeypatch):
    fake = _Waterfall(monkeypatch, {"a": [_Candidate("a", "a1")], "b": [_Candidate("b", "b1")]})

    assert fake.search() == ["a1"]
    assert fake.listed == [("a", ["eng"])]


def test_the_search_ends_once_every_provider_was_asked(monkeypatch):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1", outcome="quota")],
    })

    assert fake.search() == []
    assert fake.listed == [("a", ["eng"]), ("b", ["eng"])]
    assert fake.throttled == ["a", "b"]


@pytest.mark.parametrize("b_outcome, expected", [("ok", ["a-eng", "b-fra"]), ("quota", ["a-eng"])])
def test_only_the_missing_language_is_searched_again(monkeypatch, b_outcome, expected):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a-eng", ENG), _Candidate("a", "a-fra", FRA, outcome="rejected")],
        "b": [_Candidate("b", "b-eng", ENG), _Candidate("b", "b-fra", FRA, outcome=b_outcome)],
    })

    assert fake.search(languages=(ENG, FRA)) == expected
    assert fake.listed == [("a", ["eng", "fra"]), ("b", ["fra"])]
    assert "b-eng" not in fake.downloads


def test_the_second_variant_of_a_language_is_searched_when_the_first_lands(monkeypatch):
    """A profile can ask for two languages that share one alpha3 code, plain
    English and English (GB). The whole language is compared, not just the
    code, or the search would stop for both variants as soon as one landed and
    the other would never be tried again."""
    eng_gb = core.Language("eng", country="GB")
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a-eng", ENG), _Candidate("a", "a-eng-gb", eng_gb, outcome="quota")],
        "b": [_Candidate("b", "b-eng-gb", eng_gb)],
    }, provider_languages={ENG, eng_gb})

    assert fake.search(languages=(ENG, eng_gb)) == ["a-eng", "b-eng-gb"]
    assert fake.listed == [("a", ["eng", "eng"]), ("b", ["eng"])]


@pytest.mark.parametrize("b_outcome, expected, downloads", [
    ("ok", ["b1"], ["a1", "b1"]),
    ("quota", ["w1"], ["a1", "b1", "w1"]),
])
def test_whisper_runs_only_after_the_whole_waterfall(monkeypatch, b_outcome, expected, downloads):
    # Listed first, but a candidate that names no episode satisfies nothing.
    whisper = _Candidate("whisperai", "w1", matches=("series", "year"))
    fake = _Waterfall(monkeypatch, {
        "whisperai": [whisper],
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1", outcome=b_outcome)],
    })

    assert fake.search(fallback_allowed=True) == expected
    assert fake.downloads == downloads


def test_whisper_stays_out_of_the_later_rounds(monkeypatch):
    """Whisper never runs between two rounds: a round that starts because the
    provider that satisfied the listing could not deliver does not list it,
    even when it sits after that provider in the order, so its transcription
    cost is never reached mid-waterfall."""
    whisper = _Candidate("whisperai", "w1", matches=("series", "year"))
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota")],
        "whisperai": [whisper],
        "b": [_Candidate("b", "b1", outcome="quota")],
    })

    assert fake.search(fallback_allowed=False) == []
    # Round one lists the first provider only; the second round lists b, not
    # the whisper provider sitting between them.
    assert [name for name, _ in fake.listed] == ["a", "b"]
    assert fake.downloads == ["a1", "b1"]


def test_a_whisper_a_round_already_asked_is_not_consulted_again(monkeypatch):
    """A whisper that sat before the provider that satisfied the first round
    was asked in that round, even when it came up empty. Its candidates never
    reached the rounds' listings, but asking it again in the fallback would
    break the every-provider-asked-at-most-once promise, so the fallback
    leaves it out."""
    fake = _Waterfall(monkeypatch, {
        "whisperai": [],
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1", outcome="quota")],
    })

    assert fake.search(fallback_allowed=True) == []
    # Round one asks the whisper provider, which lists nothing, then a; the
    # second round asks b. The fallback consults none of them again.
    assert [name for name, _ in fake.listed] == ["whisperai", "a", "b"]


def test_the_final_fallback_still_consults_a_whisper_the_rounds_skipped(monkeypatch):
    """Whisper sits out the later rounds, but the final fallback is the moment
    that exclusion refers to: a whisper ordered after the provider that
    satisfied the listing is still consulted there. Its candidates reach the
    fallback only through the rounds' listings, so a whisper the rounds never
    listed is listed once, for the fallback."""
    whisper = _Candidate("whisperai", "w1", matches=("series", "year"))
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota")],
        "whisperai": [whisper],
        "b": [_Candidate("b", "b1", outcome="quota")],
    })

    assert fake.search(fallback_allowed=True) == ["w1"]
    # The rounds list a then b; whisper is listed once, for the fallback.
    assert [name for name, _ in fake.listed] == ["a", "b", "whisperai"]
    assert fake.downloads == ["a1", "b1", "w1"]


def test_the_whisper_download_is_flagged_in_the_sink(monkeypatch):
    whisper = _Candidate("whisperai", "w1", matches=("series", "year"))
    fake = _Waterfall(monkeypatch, {
        "whisperai": [whisper],
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1", outcome="quota")],
    })
    sink = []

    assert fake.search(fallback_allowed=True, candidate_sink=sink) == ["w1"]

    by_provider = {record["provider_name"]: record for record in sink}
    # Every candidate a round scored is reported, and the one the final
    # fallback downloaded carries the flag, not a second record for it.
    assert len(sink) == 3
    assert by_provider["whisperai"]["downloaded"] is True
    assert by_provider["a"]["downloaded"] is False
    assert by_provider["b"]["downloaded"] is False


def test_one_skip_line_per_provider_per_round_not_per_attempt(monkeypatch, caplog):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota"), _Candidate("a", "a2", outcome="quota")],
        "b": [_Candidate("b", "b1", outcome="quota"), _Candidate("b", "b2", outcome="quota")],
        "c": [_Candidate("c", "c1")],
    })

    with caplog.at_level(logging.INFO):
        assert fake.search() == ["c1"]

    assert fake.downloads == ["a1", "b1", "c1"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "is discarded" in r.getMessage()]
    skipped = [r.getMessage() for r in caplog.records if "remaining candidates of provider" in r.getMessage()]
    assert len(skipped) == 2
    assert any("'a'" in message for message in skipped)
    assert any("'b'" in message for message in skipped)
    assert len(_messages(caplog, "continuing with")) == 2


def test_the_provider_order_is_captured_once_per_search(monkeypatch):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1")],
    })

    def a_new_provider_arrived():
        # A settings save while round one is downloading must neither make
        # the search revisit a provider nor reach one it never held.
        fake.pool.providers.insert(1, "z")

    fake.on_list["a"] = a_new_provider_arrived

    assert fake.search() == ["b1"]
    assert fake.listed == [("a", ["eng"]), ("b", ["eng"])]


def test_a_sports_search_runs_the_waterfall_too(monkeypatch):
    fake = _Waterfall(monkeypatch, {
        "a": [_Candidate("a", "a1", outcome="quota")],
        "b": [_Candidate("b", "b1")],
    })
    contexts = []
    fake.pool.throttle_callback = (
        lambda name, error, **context: (fake.throttled.append(name), contexts.append(context)))

    video = core.Episode("/m/Match.S01E01.mkv", "Match", 1, 1)
    video.sports_context = {"stage": "group"}
    found = download_best_subtitles({video}, {ENG}, fake.pool, min_score=1)

    assert sorted(subtitle.id for subtitle in found[video]) == ["b1"]
    assert fake.listed == [("a", ["eng"]), ("b", ["eng"])]
    # The sports context rides on the candidate into the download's report.
    assert contexts[0].get("sports_context") == {"stage": "group"}

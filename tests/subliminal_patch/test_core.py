from pathlib import Path
from unittest.mock import MagicMock

import pytest

from subliminal_patch import core


def _library(tmpdir, name):
    """A file under a directory we name ourselves.

    guessit parses the parent directory as well as the filename, and pytest's
    own tmpdir carries a run counter (pytest-849). From pytest-10 onward that
    counter makes guessit drop the title and raise GuessingError, so the test
    passes on a fresh machine and fails on one that has run pytest ten times.
    """
    library = Path(tmpdir, "Media")
    library.mkdir(exist_ok=True)
    video_path = library / name
    video_path.touch()
    return str(video_path)


def test_scan_video_movie(tmpdir):
    result = core.scan_video(_library(tmpdir, "Taxi Driver 1976 Bluray 720p x264.mkv"))
    assert isinstance(result, core.Movie)


def test_scan_video_episode(tmpdir):
    result = core.scan_video(_library(tmpdir, "The Wire S01E01 Bluray 720p x264.mkv"))
    assert isinstance(result, core.Episode)


@pytest.fixture
def pool_instance():
    yield core.SZProviderPool({"opensubtitlescom"}, {})


def _names(pool):
    """The pool's provider names as a set.

    SZProviderPool.providers is an ordered list here, not the set upstream used:
    provider priority reads that order. These tests are about membership, and
    test_pool_keeps_its_providers_ordered_and_unique covers the rest.
    """
    assert len(set(pool.providers)) == len(pool.providers), \
        f"duplicate provider names: {pool.providers}"
    return set(pool.providers)


def test_pool_update_w_nothing(pool_instance):
    pool_instance.update({}, {}, [], {})
    assert _names(pool_instance) == set()
    assert pool_instance.discarded_providers == set()


def test_pool_update_w_multiple_providers(pool_instance):
    assert _names(pool_instance) == {"opensubtitlescom"}
    pool_instance.update({"opensubtitlescom", "subf2m"}, {}, [], {})
    assert _names(pool_instance) == {"opensubtitlescom", "subf2m"}


def test_pool_update_discarded_providers(pool_instance):
    assert _names(pool_instance) == {"opensubtitlescom"}

    # Provider was discarded internally
    pool_instance.discarded_providers = {"opensubtitlescom"}

    assert pool_instance.discarded_providers == {"opensubtitlescom"}

    # Provider is set to be used again
    pool_instance.update({"opensubtitlescom", "subf2m"}, {}, [], {})

    assert _names(pool_instance) == {"subf2m", "opensubtitlescom"}

    # Provider should disappear from discarded providers
    assert pool_instance.discarded_providers == set()


def test_pool_update_discarded_providers_2(pool_instance):
    assert _names(pool_instance) == {"opensubtitlescom"}

    # Provider was discarded internally
    pool_instance.discarded_providers = {"opensubtitlescom"}

    assert pool_instance.discarded_providers == {"opensubtitlescom"}

    # Provider is not set to be used again
    pool_instance.update({"subf2m"}, {}, [], {})

    assert _names(pool_instance) == {"subf2m"}

    # Provider should not disappear from discarded providers
    assert pool_instance.discarded_providers == {"opensubtitlescom"}


def test_language_equals_init():
    assert core._LanguageEquals([(core.Language("spa"), core.Language("spa", "MX"))])


def test_language_equals_init_invalid():
    with pytest.raises(ValueError):
        assert core._LanguageEquals([(core.Language("spa", "MX"),)])


def test_language_equals_init_empty_list_gracefully():
    assert core._LanguageEquals([]) == []


@pytest.mark.parametrize(
    "langs",
    [
        [(core.Language("spa"), core.Language("spa", "MX"))],
        [(core.Language("por"), core.Language("por", "BR"))],
        [(core.Language("zho"), core.Language("zho", "TW"))],
    ],
)
def test_language_equals_check_set(langs):
    equals = core._LanguageEquals(langs)
    lang_set = {langs[0]}
    assert equals.check_set(lang_set) == set(langs)


def test_language_equals_check_set_do_nothing():
    equals = core._LanguageEquals([(core.Language("eng"), core.Language("spa"))])
    lang_set = {core.Language("spa")}
    assert equals.check_set(lang_set) == {core.Language("spa")}


def test_language_equals_check_set_do_nothing_w_forced():
    equals = core._LanguageEquals(
        [(core.Language("spa", forced=True), core.Language("spa", "MX"))]
    )
    lang_set = {core.Language("spa")}
    assert equals.check_set(lang_set) == {core.Language("spa")}


class _StubSubtitle:
    """Only what the pool's language handling touches."""

    provider_name = "stub"

    def __init__(self, language):
        self.language = language
        self.id = f"stub-{language}"
        # The pool reads these off every subtitle it collects.
        self.release_info = "Dune.2021.1080p.WEBRip.DD5.1.x264-SHITBOX"
        self.matches = set()
        self.hearing_impaired = False


class _StubProvider:
    """Returns one subtitle per language it is asked for.

    These tests are about the pool mapping languages through language_equals,
    not about any real provider. They used to drive opensubtitlescom, which
    needs credentials, so they failed with ConfigurationError on every machine
    that has none: a test that cannot pass in CI protects nothing.
    """

    languages = {core.Language("spa"), core.Language("spa", "MX")}
    video_types = (core.Movie, core.Episode)
    subtitle_class = _StubSubtitle

    def __init__(self, **kwargs):
        self.initialized = False

    def initialize(self):
        self.initialized = True

    def terminate(self):
        self.initialized = False

    @staticmethod
    def check(video):
        return True

    def list_subtitles(self, video, languages):
        return [_StubSubtitle(language) for language in languages]


@pytest.fixture
def stub_provider(monkeypatch):
    from subliminal_patch.extensions import provider_registry

    monkeypatch.setitem(provider_registry.providers, "stub", _StubProvider)
    return _StubProvider


def _pool(equals):
    return core.SZProviderPool({"stub"}, language_equals=equals)


def test_language_equals_maps_the_requested_language(stub_provider, movies):
    """spa is asked for, spa-MX is what the pool must come back with."""
    pool = _pool([(core.Language("spa"), core.Language("spa", "MX"))])

    subs = pool.list_subtitles(movies["dune"], {core.Language("spa")})

    assert subs
    assert all(sub.language == core.Language("spa", "MX") for sub in subs)


def test_language_equals_maps_in_the_other_direction(stub_provider, movies):
    pool = _pool([(core.Language("spa", "MX"), core.Language("spa"))])

    subs = pool.list_subtitles(movies["dune"], {core.Language("spa")})

    assert subs
    assert all(sub.language == core.Language("spa") for sub in subs)


def test_without_language_equals_the_language_is_left_alone(stub_provider, movies):
    pool = _pool(None)

    subs = pool.list_subtitles(movies["dune"], {core.Language("spa")})

    assert subs
    assert all(sub.language == core.Language("spa") for sub in subs)


def test_pool_keeps_its_providers_ordered_and_unique():
    """The fork stores providers as an ordered list rather than the set upstream
    used, because provider priority reads that order. Deduplicated on the way in,
    since a name arriving twice would be searched twice."""
    pool = core.SZProviderPool(["b", "a", "b", "c"], {})

    assert pool.providers == ["b", "a", "c"]


def test_a_subtitle_mapped_to_another_language_is_not_downloaded(stub_provider, movies):
    """spa was requested and language_equals maps it to eng, so what comes back
    is an English subtitle. download_best_subtitles must not hand that to a
    caller who asked for Spanish, whatever it scores."""
    pool = _pool([
        (core.Language("spa", "MX"), core.Language("eng")),
        (core.Language("spa"), core.Language("eng")),
    ])

    subs = pool.list_subtitles(movies["dune"], {core.Language("spa")})

    assert not pool.download_best_subtitles(subs, movies["dune"], {core.Language("spa")})


def test_language_hook_none_keeps_requested_languages(monkeypatch, movies):
    calls = []

    class HookedProvider:
        languages = {core.Language("eng"), core.Language("spa")}

        @classmethod
        def check(cls, video):
            return True

        def initialize(self):
            pass

        def list_subtitles(self, video, languages):
            calls.append(languages)
            return []

    original = core.provider_registry.providers.copy()
    core.provider_registry.providers.clear()
    core.provider_registry.register("hooked", HookedProvider)

    try:
        pool = core.SZProviderPool(["hooked"], language_hook=lambda provider: None)
        pool.list_subtitles(
            movies["dune"],
            {core.Language("eng"), core.Language("spa")},
        )
    finally:
        core.provider_registry.providers.clear()
        core.provider_registry.providers.update(original)

    assert calls == [{core.Language("eng"), core.Language("spa")}]


def test_language_hook_excludes_configured_languages(monkeypatch, movies):
    calls = []

    class HookedProvider:
        languages = {core.Language("eng"), core.Language("spa")}

        @classmethod
        def check(cls, video):
            return True

        def initialize(self):
            pass

        def list_subtitles(self, video, languages):
            calls.append(languages)
            return []

    original = core.provider_registry.providers.copy()
    core.provider_registry.providers.clear()
    core.provider_registry.register("hooked", HookedProvider)

    try:
        pool = core.SZProviderPool(
            ["hooked"],
            language_hook=lambda provider: {core.Language("eng")},
        )
        pool.list_subtitles(
            movies["dune"],
            {core.Language("eng"), core.Language("spa")},
        )
    finally:
        core.provider_registry.providers.clear()
        core.provider_registry.providers.update(original)

    assert calls == [{core.Language("spa")}]


# ---- list_subtitles_prioritized: exhaustive flag behavior ----

def _make_fake_subtitle(language):
    """Why: list_subtitles_prioritized requires real-looking subtitle objects
    (filters out anything lacking get_matches) and reads ``subtitle.language.alpha3``.
    What: Build a MagicMock that satisfies both requirements.
    Test: Used by the exhaustive-flag tests below.
    """
    sub = MagicMock()
    sub.language = language
    sub.get_matches = MagicMock(return_value=set())
    return sub


def _fixed_score(value):
    """Why: Decouple the exhaustive-flag test from the real scoring formula so
    we can deterministically place subtitles above or below min_score.
    What: Returns a (score, _) tuple matching the compute_score contract.
    Test: Pass as compute_score= to list_subtitles_prioritized.
    """
    return lambda matches, subtitle, video, hearing_impaired: (value, None)


@pytest.fixture
def two_provider_pool():
    """SZProviderPool with two providers in a deterministic order so we can
    assert which providers were queried after the early-exit decision."""
    yield core.SZProviderPool(["provider_a", "provider_b"], {})


def test_list_subtitles_prioritized_early_exit_when_not_exhaustive(
    two_provider_pool, monkeypatch
):
    """Why: Auto-download must stop after the first provider that satisfies all
    requested languages above min_score - otherwise we waste provider quota.
    What: With exhaustive=False (default), provider_b must NOT be queried when
    provider_a already returns a high-scoring subtitle for the only language.
    Test: Patch list_subtitles_provider, count invocations per provider.
    """
    lang = core.Language("eng")
    sub_a = _make_fake_subtitle(lang)

    call_log = []

    def fake_list(self, provider, video, languages):
        call_log.append(provider)
        if provider == "provider_a":
            return [sub_a]
        return []  # provider_b would return something too, but we should never get here

    monkeypatch.setattr(
        core.SZProviderPool, "list_subtitles_provider", fake_list
    )

    video = MagicMock()
    result = two_provider_pool.list_subtitles_prioritized(
        video, {lang}, min_score=80,
        compute_score=_fixed_score(100),  # above min_score -> satisfied
    )

    assert call_log == ["provider_a"]
    assert result == [sub_a]


def test_list_subtitles_prioritized_no_early_exit_when_exhaustive(
    two_provider_pool, monkeypatch
):
    """Why: Manual search must show every provider's candidates even when the
    first one already satisfies min_score - users want the full picture.
    What: With exhaustive=True, both providers are queried even though
    provider_a alone satisfies all requested languages above min_score.
    Test: Patch list_subtitles_provider, assert both providers appear in
    call_log and both subtitles appear in the result.
    """
    lang = core.Language("eng")
    sub_a = _make_fake_subtitle(lang)
    sub_b = _make_fake_subtitle(lang)

    call_log = []

    def fake_list(self, provider, video, languages):
        call_log.append(provider)
        if provider == "provider_a":
            return [sub_a]
        return [sub_b]

    monkeypatch.setattr(
        core.SZProviderPool, "list_subtitles_provider", fake_list
    )

    video = MagicMock()
    result = two_provider_pool.list_subtitles_prioritized(
        video, {lang}, min_score=80,
        compute_score=_fixed_score(100),
        exhaustive=True,
    )

    assert call_log == ["provider_a", "provider_b"]
    assert sub_a in result and sub_b in result

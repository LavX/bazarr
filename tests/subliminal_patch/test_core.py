from pathlib import Path

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

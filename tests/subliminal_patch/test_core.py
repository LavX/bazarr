from pathlib import Path
from unittest.mock import MagicMock

import pytest

from subliminal_patch import core


def test_scan_video_movie(tmpdir):
    video_path = Path(tmpdir, "Taxi Driver 1976 Bluray 720p x264.mkv")
    video_path.touch()

    result = core.scan_video(str(video_path))
    assert isinstance(result, core.Movie)


def test_scan_video_episode(tmpdir):
    video_path = Path(tmpdir, "The Wire S01E01 Bluray 720p x264.mkv")
    video_path.touch()

    result = core.scan_video(str(video_path))
    assert isinstance(result, core.Episode)


@pytest.fixture
def pool_instance():
    yield core.SZProviderPool({"opensubtitlescom"}, {})


def test_pool_update_w_nothing(pool_instance):
    pool_instance.update({}, {}, [], {})
    assert pool_instance.providers == set()
    assert pool_instance.discarded_providers == set()


def test_pool_update_w_multiple_providers(pool_instance):
    assert pool_instance.providers == {"opensubtitlescom"}
    pool_instance.update({"opensubtitlescom", "subf2m"}, {}, [], {})
    assert pool_instance.providers == {"opensubtitlescom", "subf2m"}


def test_pool_update_discarded_providers(pool_instance):
    assert pool_instance.providers == {"opensubtitlescom"}

    # Provider was discarded internally
    pool_instance.discarded_providers = {"opensubtitlescom"}

    assert pool_instance.discarded_providers == {"opensubtitlescom"}

    # Provider is set to be used again
    pool_instance.update({"opensubtitlescom", "subf2m"}, {}, [], {})

    assert pool_instance.providers == {"subf2m", "opensubtitlescom"}

    # Provider should disappear from discarded providers
    assert pool_instance.discarded_providers == set()


def test_pool_update_discarded_providers_2(pool_instance):
    assert pool_instance.providers == {"opensubtitlescom"}

    # Provider was discarded internally
    pool_instance.discarded_providers = {"opensubtitlescom"}

    assert pool_instance.discarded_providers == {"opensubtitlescom"}

    # Provider is not set to be used again
    pool_instance.update({"subf2m"}, {}, [], {})

    assert pool_instance.providers == {"subf2m"}

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


@pytest.fixture
def language_equals_pool_intance():
    equals = [(core.Language("spa"), core.Language("spa", "MX"))]
    yield core.SZProviderPool({"opensubtitlescom"}, language_equals=equals)


def test_language_equals_pool_intance_list_subtitles(
    language_equals_pool_intance, movies
):
    subs = language_equals_pool_intance.list_subtitles(
        movies["dune"], {core.Language("spa")}
    )
    assert subs
    assert all(sub.language == core.Language("spa", "MX") for sub in subs)


def test_language_equals_pool_intance_list_subtitles_reversed(movies):
    equals = [(core.Language("spa", "MX"), core.Language("spa"))]
    language_equals_pool_intance = core.SZProviderPool(
        {"opensubtitlescom"}, language_equals=equals
    )
    subs = language_equals_pool_intance.list_subtitles(
        movies["dune"], {core.Language("spa")}
    )
    assert subs
    assert all(sub.language == core.Language("spa") for sub in subs)


def test_language_equals_pool_intance_list_subtitles_empty_lang_equals(movies):
    language_equals_pool_intance = core.SZProviderPool(
        {"opensubtitlescom"}, language_equals=None
    )
    subs = language_equals_pool_intance.list_subtitles(
        movies["dune"], {core.Language("spa")}
    )
    assert subs
    assert not all(sub.language == core.Language("spa", "MX") for sub in subs)


def test_language_equals_pool_intance_list_subtitles_return_nothing(movies):
    equals = [
        (core.Language("spa", "MX"), core.Language("eng")),
        (core.Language("spa"), core.Language("eng")),
    ]
    language_equals_pool_intance = core.SZProviderPool(
        {"opensubtitlescom"}, language_equals=equals
    )
    subs = language_equals_pool_intance.list_subtitles(
        movies["dune"], {core.Language("spa")}
    )
    assert not language_equals_pool_intance.download_best_subtitles(
        subs, movies["dune"], {core.Language("spa")}
    )


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

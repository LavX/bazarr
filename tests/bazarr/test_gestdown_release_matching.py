# coding=utf-8
"""The scene-style release name must not leak into release-group matching.

``GestdownSubtitle`` formats its releases into ``Series.SxxEyy.version`` so the
UI shows something recognisable. ``self.releases`` is a different thing: it is
what ``get_matches`` searches for the video's release group. Injecting the
series title there lets a group name that happens to occur inside the show's own
title score a match it did not earn, and release_group is worth real points.
"""

import sys

import pytest

sys.path.insert(0, "custom_libs")


def _subtitle(version, series, season, episode):
    from subliminal_patch.providers.gestdown import GestdownSubtitle
    from subzero.language import Language

    return GestdownSubtitle(
        Language("eng"),
        {
            "hearingImpaired": False,
            "downloadUri": "/x",
            "subtitleId": "1",
            "version": version,
            "qualities": [],
        },
        series=series,
        season=season,
        episode=episode,
    )


def test_the_series_title_never_reaches_release_group_matching():
    # "LOL" is both this show's title and a real release group.
    sub = _subtitle("WEB-DL", series="LOL Last One Laughing", season=1, episode=2)

    assert all("LOL" not in r for r in sub.releases), (
        f"the series title leaked into releases {sub.releases!r}; get_matches "
        "searches those for the video's release group, so a group named after "
        "the show would score a free match"
    )


def test_release_info_still_shows_the_scene_style_name():
    sub = _subtitle("WEB-DL", series="Better Call Saul", season=6, episode=4)

    assert "Better.Call.Saul.S06E04" in sub.release_info, (
        f"the display string lost its scene-style name: {sub.release_info!r}"
    )


def test_a_version_that_already_names_the_episode_is_left_alone():
    sub = _subtitle("Suits.S05E06.Bluray.x264-DEFLATE", series="Suits", season=5, episode=6)

    assert sub.releases == ["Suits.S05E06.Bluray.x264-DEFLATE"]
    assert "S05E06" in sub.release_info


@pytest.mark.parametrize("season,episode", [(None, None), (1, None)])
def test_without_episode_context_the_version_is_untouched(season, episode):
    sub = _subtitle("WEB-DL, HDTV", series="Show", season=season, episode=episode)

    assert sub.releases == ["WEB-DL", "HDTV"]


def _episode(release_group):
    from subliminal_patch.core import Episode

    return Episode(
        "LOL.Last.One.Laughing.S01E02.1080p.WEB-DL.mkv",
        "LOL Last One Laughing",
        1,
        2,
        release_group=release_group,
    )


def test_a_group_named_after_the_show_scores_no_release_group_match():
    """The whole point of keeping ``releases`` raw, checked end to end.

    ``update_matches`` searches whatever string it is handed for the video's
    release group, so handing it the title-prefixed display text scores the
    match just as surely as the explicit loop below it would have.
    """
    sub = _subtitle("WEB-DL", series="LOL Last One Laughing", season=1, episode=2)

    assert "release_group" not in sub.get_matches(_episode("LOL"))


def test_a_group_the_subtitle_really_names_still_matches():
    sub = _subtitle("Bluray.x264-DEFLATE", series="LOL Last One Laughing", season=1, episode=2)

    assert "release_group" in sub.get_matches(_episode("DEFLATE"))


@pytest.mark.parametrize("version", [
    "Show_S01E02_1080p_WEB-DL",
    "Show S01E02 1080p WEB-DL",
    "Show.1x02.1080p.WEB-DL",
    # Unpadded is just as common a spelling, and the point of the check is to
    # leave a name that already says which episode it is alone. These carry no
    # series title, so the episode test is the only thing standing between them
    # and a prefixed second copy of the same episode number.
    "S01E2.1080p.WEB-DL",
    "S1E2.1080p.WEB-DL",
    "1x2.1080p.WEB-DL",
    # Multi-episode releases name this episode too, in every spelling of it.
    "S01E01-E02.1080p.WEB-DL",
    "S01E01E02.1080p.WEB-DL",
    "S01E01-02.1080p.WEB-DL",
    "1x01-02.1080p.WEB-DL",
])
def test_a_release_that_already_names_the_episode_is_never_duplicated(version):
    """Underscores separate release tokens as surely as dots do, but they are
    regex word characters, so a plain \\b boundary reads _S01E02_ as no episode
    at all and prefixes a second copy of the name onto the display string."""
    sub = _subtitle(version, series="Show", season=1, episode=2)

    assert sub.release_info == version


@pytest.mark.parametrize("version", [
    "S01E03.1080p.WEB-DL",
    "S02E02.1080p.WEB-DL",
    "S01E03-E04.1080p.WEB-DL",
    "2x02.1080p.WEB-DL",
    "1920x1080.WEB-DL",
])
def test_a_release_naming_another_episode_still_gets_the_scene_style_name(version):
    """The other half of the same rule: only a name that really says S01E02 is
    left alone, or the prefix stops being a reliable label."""
    sub = _subtitle(version, series="Show", season=1, episode=2)

    assert sub.release_info == f"Show.S01E02.{version}"

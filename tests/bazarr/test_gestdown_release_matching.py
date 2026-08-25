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

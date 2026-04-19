# -*- coding: utf-8 -*-
"""Offline unit tests for subdl anime_mode gating and correctness hardening.

These tests never hit the subdl API. They cover:
- `_coerce_int` helper (episode_from / episode_end int coercion).
- `_parse_episode_range_from_releases` release-name range parsing.
- `SubdlProvider.__init__` anime_mode plumbing.
- `SubdlSubtitle.get_matches` absolute-episode / pack branches.
- Merge-path KeyError safety for malformed subdl rows missing 'name'.
- Gating: non-anime_mode uses the original single-call path.
"""

from unittest.mock import MagicMock, patch

import pytest
from subliminal_patch.core import Episode
from subliminal_patch.exceptions import APIThrottled
from subzero.language import Language

from subliminal_patch.providers.subdl import (
    SubdlProvider,
    SubdlSubtitle,
    _coerce_int,
)


def _episode(**overrides):
    defaults = dict(
        name="Dummy.Show.S01E03.mkv",
        series="Dummy Show",
        season=1,
        episode=3,
        year=2024,
        series_imdb_id="tt0000001",
    )
    defaults.update(overrides)
    return Episode(**defaults)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        (7, 7),
        ("7", 7),
        (" 12 ", 12),
        ("not-a-number", None),
        ("", None),
        ({}, None),
    ],
)
def test_coerce_int_variants(raw, expected):
    assert _coerce_int(raw) == expected


def test_provider_stores_anime_mode_flag():
    default_provider = SubdlProvider(api_key="fake")
    anime_provider = SubdlProvider(api_key="fake", anime_mode=True)

    assert default_provider.anime_mode is False
    assert anime_provider.anime_mode is True


def test_provider_requires_api_key():
    with pytest.raises(Exception):
        SubdlProvider(api_key=None)


def test_parse_episode_range_from_releases_detects_pack():
    releases = ["Some.Anime.EP0264-0336.1080p"]
    ep_from, ep_end = SubdlProvider._parse_episode_range_from_releases(releases)
    assert ep_from == 264
    assert ep_end == 336


def test_parse_episode_range_returns_none_for_single_episode():
    releases = ["Some.Show.S01E05.1080p.WEB-DL"]
    ep_from, ep_end = SubdlProvider._parse_episode_range_from_releases(releases)
    assert ep_from is None
    assert ep_end is None


def test_subtitle_get_matches_pack_with_absolute_episode():
    video = _episode(season=11, episode=5)
    # Patch video to carry absolute_episode (guessit-populated at runtime)
    video.absolute_episode = 310

    sub = SubdlSubtitle(
        language=Language("eng"),
        forced=False,
        hearing_impaired=False,
        page_link="https://subdl.com/dummy",
        download_link="https://subdl.com/download/dummy.zip",
        file_id="dummy",
        release_names=["Anime.Pack.EP0264-0336"],
        uploader="tester",
        season=9,  # arc-numbered season, differs from Sonarr's S11
        episode=None,
        absolute_episode=310,
        is_pack=True,
    )
    matches = sub.get_matches(video)
    # Pack branch should yield season+episode matches despite season mismatch
    assert "season" in matches
    assert "episode" in matches


def test_subtitle_get_matches_non_pack_exact():
    video = _episode(season=1, episode=3)
    sub = SubdlSubtitle(
        language=Language("eng"),
        forced=False,
        hearing_impaired=False,
        page_link="https://subdl.com/dummy",
        download_link="https://subdl.com/download/dummy.zip",
        file_id="dummy",
        release_names=["Dummy.Show.S01E03"],
        uploader="tester",
        season=1,
        episode=3,
    )
    matches = sub.get_matches(video)
    assert "season" in matches
    assert "episode" in matches


def _mock_response(payload, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r


def test_query_does_not_keyerror_on_malformed_rows():
    """Malformed subdl rows missing 'name' must be filtered, not crash the query."""
    provider = SubdlProvider(api_key="fake", anime_mode=False)
    video = _episode()

    payload = {
        "success": True,
        "subtitles": [
            {"name": "valid.srt", "url": "/u/1", "language": "EN",
             "subtitlePage": "/s/1", "releases": ["Dummy.Show.S01E03"]},
            # Malformed row: no 'name'
            {"url": "/u/2", "language": "EN"},  # noqa: missing 'name'
        ],
    }

    with patch.object(provider, "retry", return_value=_mock_response(payload)):
        subs = provider.query({Language("eng")}, video)

    # At minimum should not raise; valid row filtered by language converter may or may not
    # survive, but no KeyError must leak.
    assert isinstance(subs, list)


def test_query_non_anime_mode_makes_single_call():
    """Non-anime-mode must not fan out extra episode/season/title searches."""
    provider = SubdlProvider(api_key="fake", anime_mode=False)
    video = _episode()

    payload = {"success": True, "subtitles": []}
    with patch.object(provider, "retry", return_value=_mock_response(payload)) as retry_mock:
        provider.query({Language("eng")}, video)

    # Exactly one upstream call in non-anime mode.
    assert retry_mock.call_count == 1


def test_query_anime_mode_fires_extra_season_call():
    """Anime-mode enables the season-only search to find cour-split subtitles."""
    provider = SubdlProvider(api_key="fake", anime_mode=True)
    video = _episode()

    payload = {"success": True, "subtitles": []}
    with patch.object(provider, "retry", return_value=_mock_response(payload)) as retry_mock:
        provider.query({Language("eng")}, video)

    # Without absolute_episode, anime_mode adds: primary + season-only + title-fallback = 3
    assert retry_mock.call_count == 3


def test_query_non_anime_mode_skips_packs():
    """Non-anime users must still see packs filtered out as before."""
    provider = SubdlProvider(api_key="fake", anime_mode=False)
    video = _episode(season=1, episode=3)

    payload = {
        "success": True,
        "subtitles": [
            {
                "name": "pack.srt", "url": "/u/1", "language": "EN",
                "subtitlePage": "/s/1", "releases": ["Dummy.Show.S01E01-E10"],
                "episode_from": 1, "episode_end": 10,
            },
        ],
    }
    with patch.object(provider, "retry", return_value=_mock_response(payload)):
        subs = provider.query({Language("eng")}, video)

    # Non-anime mode skips packs entirely (preserves pre-patch behavior).
    assert subs == []


def test_query_anime_mode_accepts_pack_covering_target():
    """Anime mode accepts a pack subtitle whose range covers the target episode."""
    provider = SubdlProvider(api_key="fake", anime_mode=True)
    video = _episode(season=1, episode=3)

    payload = {
        "success": True,
        "subtitles": [
            {
                "name": "pack.srt", "url": "/u/1", "language": "EN",
                "subtitlePage": "/s/1", "releases": ["Dummy.Show.S01E01-E10"],
                "episode_from": 1, "episode_end": 10,
            },
        ],
    }
    with patch.object(provider, "retry", return_value=_mock_response(payload)):
        subs = provider.query({Language("eng")}, video)

    assert len(subs) == 1
    assert subs[0].is_pack is True


def test_query_anime_mode_skips_pack_not_covering_target():
    provider = SubdlProvider(api_key="fake", anime_mode=True)
    video = _episode(season=1, episode=3)

    payload = {
        "success": True,
        "subtitles": [
            {
                "name": "pack.srt", "url": "/u/1", "language": "EN",
                "subtitlePage": "/s/1", "releases": ["Dummy.Show.S01E05-E10"],
                "episode_from": 5, "episode_end": 10,
            },
        ],
    }
    with patch.object(provider, "retry", return_value=_mock_response(payload)):
        subs = provider.query({Language("eng")}, video)

    assert subs == []


def test_query_anime_mode_merges_fallbacks_when_primary_reports_cant_find():
    """Primary 'cant find' must not short-circuit: anime_mode fallbacks must still run
    and their results must flow through (covers Codex review P1)."""
    provider = SubdlProvider(api_key="fake", anime_mode=True)
    video = _episode(season=1, episode=3)

    primary = _mock_response({
        "status": False,
        "error": "Sorry, we can't find any subtitles.",
    })
    # Season-only fallback returns the real match
    season_fallback = _mock_response({
        "success": True,
        "subtitles": [
            {
                "name": "found_in_fallback.srt", "url": "/u/99", "language": "EN",
                "subtitlePage": "/s/99", "releases": ["Dummy.Show.S01E03"],
                "season": 1, "episode": 3,
            },
        ],
    })
    empty = _mock_response({"success": True, "subtitles": []})

    # Sequence: [primary, season_fallback, title_fallback_empty]
    # (no absolute_episode on this video, so res_absolute is skipped.)
    with patch.object(provider, "retry", side_effect=[primary, season_fallback, empty]):
        subs = provider.query({Language("eng")}, video)

    assert len(subs) == 1
    assert subs[0].page_link.endswith("/s/99")


def test_query_non_anime_mode_short_circuits_on_cant_find():
    """Non-anime mode preserves original early-return on 'cant find' error."""
    provider = SubdlProvider(api_key="fake", anime_mode=False)
    video = _episode()

    primary = _mock_response({
        "status": False,
        "error": "Sorry, we can't find any subtitles.",
    })
    with patch.object(provider, "retry", return_value=primary) as retry_mock:
        subs = provider.query({Language("eng")}, video)

    assert subs == []
    # Exactly 1 call: no fallbacks fired.
    assert retry_mock.call_count == 1


def test_query_raises_on_fallback_throttle():
    """Extra anime-mode searches hitting 429 must raise APIThrottled instead of
    silently dropping the response (covers Codex review P2)."""
    provider = SubdlProvider(api_key="fake", anime_mode=True)
    video = _episode()

    primary = _mock_response({"success": True, "subtitles": []})
    throttled = MagicMock()
    throttled.status_code = 429

    with patch.object(provider, "retry", side_effect=[primary, throttled]):
        with pytest.raises(APIThrottled):
            provider.query({Language("eng")}, video)


def test_query_still_raises_non_can_find_primary_error():
    """A non-'cant find' primary error must still raise ProviderError even in anime_mode."""
    from subliminal.exceptions import ProviderError
    provider = SubdlProvider(api_key="fake", anime_mode=True)
    video = _episode()

    bad = _mock_response({"status": False, "error": "Something broke internally"})
    with patch.object(provider, "retry", return_value=bad):
        with pytest.raises(ProviderError):
            provider.query({Language("eng")}, video)

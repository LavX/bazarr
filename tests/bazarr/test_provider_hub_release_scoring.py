# coding=utf-8
"""Hub candidates must be scored on their release information, not just their ids.

The download scorer reuses a subtitle's match set when it is already populated
rather than recomputing it. A Provider Hub candidate arrives from the worker
with a lean set already in place: title, year, imdb id. So the recomputation
that adds source, resolution and release-group matches never ran on that path,
every hub candidate scored identically, and the first one listed won. A user
searching for a 2160p WEB release could be handed a Blu-ray subtitle for a
different release group.

The priority-ordered listing path calls get_matches itself, which is why this
only showed up with Provider Priority disabled and why manual search looked
right.
"""

import pytest
from subzero.language import Language

import app.database  # noqa: F401


VIDEO_RELEASE = "Show.S01E01.2160p.WEB-DL.DDP5.1.H.265-NTb"
OTHER_RELEASE = "Show.S01E01.1080p.BluRay.x264-GRP"


def _video():
    from subliminal_patch.core import Episode

    video = Episode(VIDEO_RELEASE + ".mkv", "Show", 1, 1)
    video.release_group = "NTb"
    video.resolution = "2160p"
    video.source = "Web"
    video.year = 2019
    return video


class _LeanSubtitle:
    """A candidate that arrives with a lean match set, like a hub worker sends."""

    provider_name = "hub-provider"
    hearing_impaired_verifiable = False
    hash_verifiable = False

    def __init__(self, release_info, matches_need_video):
        self.language = Language("eng")
        self.release_info = release_info
        self.matches = {"series", "year"}
        self.hearing_impaired = False
        self.matches_need_video = matches_need_video
        self.get_matches_calls = 0

    def get_matches(self, video):
        from subliminal_patch.providers.utils import update_matches

        self.get_matches_calls += 1
        matches = {"series", "year"}
        update_matches(matches, video, self.release_info)
        self.matches = set(matches)
        return matches


def _score(subtitle, video):
    from subliminal_patch.score import compute_score

    matches = (subtitle.get_matches(video)
               if getattr(subtitle, "matches_need_video", False) or not subtitle.matches
               else subtitle.matches)
    return compute_score(matches, subtitle, video, "don't prefer")[0]


def test_the_hub_subtitle_class_declares_that_it_needs_the_video():
    """The capability is declared on the candidate rather than inferred from its
    class name: a rename or a subclass would silently put the bug back."""
    from provider_hub.protocol import HubWorkerSubtitle

    assert getattr(HubWorkerSubtitle, "matches_need_video", False) is True


def test_the_download_scorer_recomputes_for_a_candidate_that_asks_for_it():
    from subliminal_patch.core import SZProviderPool

    video = _video()
    matching = _LeanSubtitle(VIDEO_RELEASE, matches_need_video=True)
    mismatching = _LeanSubtitle(OTHER_RELEASE, matches_need_video=True)

    scored = SZProviderPool._score_subtitles(
        [matching, mismatching], video, [Language("eng")], "don't prefer")

    assert [s for s, *_ in scored][0] is matching, (
        "the candidate matching the video's source, resolution and release group "
        "did not come first"
    )
    assert scored[0][1] > scored[1][1]


def test_a_candidate_that_does_not_ask_is_scored_on_what_it_arrived_with():
    """Every other provider computes its matches when it lists. Recomputing for
    them would change behaviour well outside this fix."""
    from subliminal_patch.core import SZProviderPool

    video = _video()
    ordinary = _LeanSubtitle(VIDEO_RELEASE, matches_need_video=False)

    SZProviderPool._score_subtitles([ordinary], video, [Language("eng")], "don't prefer")

    assert ordinary.get_matches_calls == 0


def test_recomputing_twice_gives_the_same_answer():
    """The priority-enabled path already calls get_matches while listing, so the
    download path calls it a second time. The set is derived from a frozen base
    each time, so the second call has to agree with the first."""
    video = _video()
    subtitle = _LeanSubtitle(VIDEO_RELEASE, matches_need_video=True)

    first = subtitle.get_matches(video)
    second = subtitle.get_matches(video)

    assert first == second


@pytest.mark.parametrize("release_info,expected", [
    (VIDEO_RELEASE, True),
    (OTHER_RELEASE, False),
])
def test_release_information_is_what_separates_the_candidates(release_info, expected):
    video = _video()
    subtitle = _LeanSubtitle(release_info, matches_need_video=True)

    matches = subtitle.get_matches(video)

    assert ("release_group" in matches) is expected


def test_a_worker_cannot_switch_the_capability_off_through_display():
    """The display dictionary is cosmetic worker-supplied data. A plugin that
    happened to send matches_need_video=False would put the scoring bug back for
    itself, silently, and the host would have no idea."""
    from provider_hub import protocol

    assert "matches_need_video" in protocol._RESERVED_DISPLAY_ATTRS

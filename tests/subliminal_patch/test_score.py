from types import SimpleNamespace

import pytest

from subliminal_patch import score
from subliminal_patch.providers.karagarga import KaragargaSubtitle


# def __call__(self, matches, subtitle, video, hearing_impaired=None):


def test_compute_score_set_var(movies, languages):
    subtitle = KaragargaSubtitle(languages["en"], "", "", "")
    score.compute_score({"hash"}, subtitle, movies["dune"])


def test_compute_score_set_var_w_episode(episodes, languages):
    subtitle = KaragargaSubtitle(languages["en"], "", "", "")
    score.compute_score({"hash"}, subtitle, episodes["breaking_bad_s01e01"])


def test_compute_score_defaults():
    assert score.ComputeScore()._scores == score.DEFAULT_SCORES


def test_compute_score_custom_invalid():
    assert (
        score.ComputeScore({"movie": {"hash": 120}, "episode": {"hash": 321}})._scores
        == score.DEFAULT_SCORES
    )


def test_compute_score_custom_valid():
    scores_copy = score.DEFAULT_SCORES.copy()
    scores_copy["movie"]["release_group"] = 12
    scores_copy["movie"]["source"] = 8

    scores_ = score.ComputeScore(scores_copy)
    assert scores_._scores["movie"]["release_group"] == 12
    assert scores_._scores["movie"]["source"] == 8


def _scorer(monkeypatch, modifier=None, penalty=None):
    # Installed on the class as staticmethods, the way the host installs them.
    monkeypatch.setattr(score.ComputeScore, "modifier",
                        None if modifier is None else staticmethod(lambda name: modifier))
    monkeypatch.setattr(score.ComputeScore, "ai_translated_penalty",
                        None if penalty is None else staticmethod(lambda: penalty))
    return score.ComputeScore()


def _subtitle(ai_translated):
    return SimpleNamespace(provider_name="subdl", ai_translated=ai_translated)


@pytest.mark.parametrize("modifier, penalty, ai_translated, expected", [
    # The penalty lands on an AI-translated subtitle only.
    (None, 30, True, -30),
    (None, 30, False, 0),
    # A truthy value that is not True is not a flag the provider set.
    (None, 30, "yes", 0),
    # It combines with the provider's own modifier before the clamp.
    (10, 30, True, -20),
    (10, 30, False, 10),
    (-90, 30, True, -100),
    # The penalty itself is held to 0..100, and an unusable one counts as 0.
    (None, 150, True, -100),
    (None, -20, True, 0),
    (5, float("nan"), True, 5),
    (5, True, True, 5),
    (5, None, True, 5),
])
def test_modifier_for_applies_the_ai_translated_penalty(monkeypatch, modifier, penalty, ai_translated, expected):
    scorer = _scorer(monkeypatch, modifier, penalty)
    assert scorer._modifier_for(_subtitle(ai_translated)) == expected


def test_a_failing_ai_translated_penalty_counts_as_zero(monkeypatch):
    scorer = _scorer(monkeypatch, modifier=5)

    def broken():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(score.ComputeScore, "ai_translated_penalty", staticmethod(broken))
    assert scorer._modifier_for(_subtitle(True)) == 5

# coding=utf-8
"""Per-provider score modifier (inbound LavX/bazarr#301).

A user keeping WhisperAI as a last-resort fallback has to drop the global
minimum score to about sixty for generated subtitles to be accepted, and that
threshold then admits bad subtitles from every other provider. Generated
subtitles structurally cannot score well: the plugin returns identifier matches
and a synthetic release description, so source, resolution and release group
are never available to the scorer.

The modifier is a signed percentage, on the same scale as the minimum-score
setting, applied to a provider's candidates before the minimum-score gate.
"""
import pytest


class _Subtitle:
    """Enough of a subtitle for ComputeScore: a provider and no hash."""

    def __init__(self, provider_name, hearing_impaired=False):
        self.provider_name = provider_name
        self.hearing_impaired = hearing_impaired
        self.hash_verifiable = False

    def __repr__(self):
        return f'<_Subtitle {self.provider_name}>'


@pytest.fixture
def episode_video():
    from subliminal_patch.core import Episode

    return Episode('/m/Show.S01E01.mkv', 'Show', 1, 1)


@pytest.fixture
def scorer():
    """A ComputeScore with no modifier installed, restored afterwards."""
    from subliminal_patch.score import ComputeScore

    return ComputeScore()


# --- the arithmetic -------------------------------------------------------

def test_no_modifier_leaves_the_score_exactly_as_it_was():
    from subliminal_patch.score import apply_score_modifier

    assert apply_score_modifier(200, 358, 0) == 200


def test_a_positive_modifier_adds_that_percentage_of_the_maximum():
    from subliminal_patch.score import apply_score_modifier

    # 10% of 358 is 35.8, rounded to 36.
    assert apply_score_modifier(200, 358, 10) == 236


def test_a_negative_modifier_subtracts_it():
    from subliminal_patch.score import apply_score_modifier

    assert apply_score_modifier(200, 358, -10) == 164


def test_a_positive_modifier_cannot_push_a_score_past_the_maximum():
    from subliminal_patch.score import apply_score_modifier

    assert apply_score_modifier(350, 358, 50) == 358


def test_a_negative_modifier_cannot_push_a_score_below_zero():
    from subliminal_patch.score import apply_score_modifier

    assert apply_score_modifier(20, 358, -50) == 0


def test_a_hash_verified_score_above_the_maximum_is_not_dragged_down_to_it():
    """A hash match scores above the 100% mark by design, because MAX_SCORES
    excludes the hash. A negative modifier should move such a score by the
    amount asked for, not collapse it to the ceiling."""
    from subliminal_patch.score import apply_score_modifier

    assert apply_score_modifier(400, 358, -10) == 364


# --- through the scorer ---------------------------------------------------

def test_a_provider_without_a_modifier_scores_exactly_as_before(scorer, episode_video):
    subtitle = _Subtitle('opensubtitles')
    matches = {'series', 'season', 'episode'}

    before = scorer(set(matches), subtitle, episode_video)
    scorer.modifier = lambda provider: 0
    after = scorer(set(matches), subtitle, episode_video)

    assert after == before


def test_the_modifier_lifts_the_score_of_the_provider_it_names(scorer, episode_video):
    from subliminal_patch.score import MAX_SCORES

    subtitle = _Subtitle('whisperai')
    matches = {'series', 'season', 'episode'}

    plain, _ = scorer(set(matches), subtitle, episode_video)
    scorer.modifier = lambda provider: 20 if provider == 'whisperai' else 0
    lifted, _ = scorer(set(matches), subtitle, episode_video)

    assert lifted == plain + round(20 * MAX_SCORES['episode'] / 100)


def test_another_provider_is_untouched_by_that_modifier(scorer, episode_video):
    subtitle = _Subtitle('opensubtitles')
    matches = {'series', 'season', 'episode'}

    plain, _ = scorer(set(matches), subtitle, episode_video)
    scorer.modifier = lambda provider: 20 if provider == 'whisperai' else 0
    after, _ = scorer(set(matches), subtitle, episode_video)

    assert after == plain


def test_the_score_without_hash_carries_the_modifier_too(scorer, episode_video):
    """The prioritized listing and the manual search read that one, so a
    modifier applied only to the first score would disagree with itself."""
    subtitle = _Subtitle('whisperai')
    matches = {'series', 'season', 'episode'}

    _, plain = scorer(set(matches), subtitle, episode_video)
    scorer.modifier = lambda provider: 20
    _, modified = scorer(set(matches), subtitle, episode_video)

    assert modified > plain


def test_a_modifier_that_raises_does_not_take_the_search_down(scorer, episode_video):
    """The modifier reads live settings. A broken value there must cost the
    user a modifier, not every search on the instance."""
    def explode(provider):
        raise ValueError('bad setting')

    subtitle = _Subtitle('whisperai')
    matches = {'series', 'season', 'episode'}

    plain = scorer(set(matches), subtitle, episode_video)
    scorer.modifier = explode

    assert scorer(set(matches), subtitle, episode_video) == plain


def test_a_subtitle_without_a_provider_name_is_scored_unmodified(scorer, episode_video):
    subtitle = _Subtitle('whisperai')
    del subtitle.provider_name
    matches = {'series', 'season', 'episode'}

    scorer.modifier = lambda provider: 20
    modified, _ = scorer(set(matches), subtitle, episode_video)

    scorer.modifier = None
    plain, _ = scorer(set(matches), subtitle, episode_video)

    assert modified == plain


# --- the setting ----------------------------------------------------------

def test_the_configured_modifier_is_read_for_that_provider(monkeypatch):
    from app.config import settings
    from app.get_providers import get_provider_score_modifier

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 25}, raising=False)

    assert get_provider_score_modifier('whisperai') == 25


def test_a_provider_with_nothing_configured_gets_no_modifier(monkeypatch):
    from app.config import settings
    from app.get_providers import get_provider_score_modifier

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 25}, raising=False)

    assert get_provider_score_modifier('opensubtitles') == 0


def test_no_modifiers_at_all_is_not_an_error(monkeypatch):
    from app.config import settings
    from app.get_providers import get_provider_score_modifier

    monkeypatch.setattr(settings.general, 'provider_score_modifiers', {}, raising=False)

    assert get_provider_score_modifier('whisperai') == 0


def test_a_value_that_is_not_a_number_is_ignored_rather_than_raising(monkeypatch):
    """The setting arrives as JSON from the browser, so it is not guaranteed."""
    from app.config import settings
    from app.get_providers import get_provider_score_modifier

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 'quite a lot'}, raising=False)

    assert get_provider_score_modifier('whisperai') == 0


def test_a_change_to_the_setting_applies_without_reinstalling_anything(monkeypatch):
    """The acceptance criterion is that editing the setting takes effect
    without a restart, so the reader must not cache."""
    from app.config import settings
    from app.get_providers import get_provider_score_modifier

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 10}, raising=False)
    assert get_provider_score_modifier('whisperai') == 10

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 30}, raising=False)
    assert get_provider_score_modifier('whisperai') == 30


def test_the_shared_scorer_asks_the_settings_for_the_modifier(monkeypatch):
    """The wiring: the ComputeScore instance every search path uses has the
    settings-backed reader installed, so no call site can miss it."""
    from app.config import settings
    from subliminal_patch.score import compute_score

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 15}, raising=False)

    assert compute_score.modifier is not None
    assert compute_score.modifier('whisperai') == 15
    assert compute_score.modifier('opensubtitles') == 0


# --- the hook has to reach every scorer, not just the shared one ----------

def test_a_freshly_built_scorer_also_carries_the_modifier(monkeypatch):
    """The compat endpoint builds its own ComputeScore to project scores for
    external clients. A hook installed only on the shared instance would leave
    that surface disagreeing with every native search path."""
    from app.config import settings
    import app.get_providers  # noqa: F401  installs the hook
    from subliminal_patch.score import ComputeScore

    monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                        {'whisperai': 15}, raising=False)

    assert ComputeScore().modifier('whisperai') == 15


def test_a_not_a_number_modifier_does_not_take_the_scorer_down(scorer, episode_video):
    """NaN and the infinities are floats, so a type check lets them past, and
    round() then raises on them outside the hook's own guard."""
    subtitle = _Subtitle('whisperai')
    matches = {'series', 'season', 'episode'}

    plain = scorer(set(matches), subtitle, episode_video)
    for bad in (float('nan'), float('inf'), float('-inf')):
        scorer.modifier = lambda provider, value=bad: value
        assert scorer(set(matches), subtitle, episode_video) == plain


def test_the_setting_reader_rejects_a_not_a_number_value(monkeypatch):
    from app.config import settings
    from app.get_providers import get_provider_score_modifier

    for bad in (float('nan'), float('inf'), float('-inf')):
        monkeypatch.setattr(settings.general, 'provider_score_modifiers',
                            {'whisperai': bad}, raising=False)
        assert get_provider_score_modifier('whisperai') == 0

# coding=utf-8

from __future__ import absolute_import
import logging
import math

from subliminal.video import Episode, Movie
from subliminal.score import get_scores

logger = logging.getLogger(__name__)


FPS_EQUALITY = ((23.976, 23.98, 24.0),)


def framerate_equal(source, check):
    if source == check:
        return True

    source = float(source)
    check = float(check)
    if source == check:
        return True

    for equal_fps_tuple in FPS_EQUALITY:
        if check in equal_fps_tuple and source in equal_fps_tuple:
            return True

    return False


DEFAULT_SCORES = {
    "episode": {
        "hash": 359,
        "series": 160,
        "year": 90,
        "season": 30,
        "episode": 30,
        "source": 25,
        "release_group": 20,
        "audio_codec": 1,
        "resolution": 1,
        "video_codec": 1,
        "hearing_impaired": 1,
        "streaming_service": 1,
    },
    "movie": {
        "hash": 179,
        "title": 60,
        "year": 40,
        "source": 30,
        "edition": 30,
        "release_group": 15,
        "audio_codec": 1,
        "resolution": 1,
        "video_codec": 1,
        "hearing_impaired": 1,
        "streaming_service": 1,
    },
}

MAX_SCORES = {
    "episode": sum(v for k, v in DEFAULT_SCORES["episode"].items() if k != "hash"),
    "movie": sum(v for k, v in DEFAULT_SCORES["movie"].items() if k != "hash"),
}


def apply_score_modifier(score, max_score, modifier_percent):
    """Adjust a raw score by a signed percentage of the maximum.

    The percentage is the scale the minimum-score setting uses, which is the
    scale the user thinks in, so a modifier of 20 means "count this provider as
    twenty points of percentage better than it scored".

    The ceiling is the higher of the maximum and the score handed in. A hash
    match scores above the maximum by design, because MAX_SCORES leaves the
    hash out, and a modifier has no business collapsing such a score down to
    the ceiling. What the ceiling does prevent is a positive modifier inventing
    a score above 100% for a subtitle that did not earn one.
    """
    if not modifier_percent:
        return score

    adjusted = score + round(modifier_percent * max_score / 100)
    return max(0, min(max(max_score, score), adjusted))


def _check_hash_sum(scores: dict):
    hash_val = scores["hash"]
    rest_sum = sum(val for key, val in scores.items() if key != "hash")
    logger.debug("Hash value: %s -> Rest sum: %s", hash_val, rest_sum)
    return rest_sum - 1 == hash_val


class ComputeScore:
    """The scorer every search path goes through.

    ``modifier`` is a hook the host installs: given a provider name it returns
    a signed percentage to apply to that provider's candidates. Applying it
    here rather than at the call sites is deliberate. The automatic download,
    the priority-ordered listing's early-exit and the manual search all compute
    scores separately, and the minimum-score gate, the search listing and the
    recorded history percentage have to agree or the interface contradicts
    itself. subliminal_patch on its own has no such policy, so the hook stays
    None and nothing changes.
    """

    modifier = None

    def __init__(self, scores=None):
        if scores:
            valid = True
            for val in scores.values():
                if not _check_hash_sum(val):
                    logger.debug("Scores sum - 1 is not equal to hash. Using defaults")
                    self._scores = DEFAULT_SCORES
                    valid = False
                    break

            if valid is True:
                self._scores = scores
        else:
            self._scores = DEFAULT_SCORES

        # Hash values should be the same. Update from defaults to ensure it
        for key in self._scores.keys():
            self._scores[key]["hash"] = DEFAULT_SCORES[key]["hash"]

    def __call__(self, matches, subtitle, video, hearing_impaired=None):
        scores = self._scores[video.__class__.__name__.lower()]
        logger.debug("Scores to use for %s: %s", video, scores)

        is_episode = isinstance(video, Episode)
        is_movie = isinstance(video, Movie)

        episode_hash_valid_if = {"series", "season", "episode", "source"}
        movie_hash_valid_if = {"video_codec", "source"}

        orig_matches = matches.copy()

        # on hash match, discard everything else
        if subtitle.hash_verifiable and "hash" in matches:
            # hash is error-prone, try to fix that
            hash_valid_if = episode_hash_valid_if if is_episode else movie_hash_valid_if

            # don't validate hashes of specials, as season and episode tend to be wrong
            if is_movie or not video.is_special:
                if hash_valid_if <= set(matches):
                    # series, season and episode matched, hash is valid
                    logger.debug(
                        "%r: Using valid hash, as %s are correct (%r) and (%r)",
                        subtitle,
                        hash_valid_if,
                        matches,
                        video,
                    )
                    matches &= {"hash"}
                else:
                    # no match, invalidate hash
                    logger.debug(
                        "%r: Ignoring hash as other matches are wrong (missing: %r) and (%r)",
                        subtitle,
                        hash_valid_if - matches,
                        video,
                    )
                    matches -= {"hash"}
        elif "hash" in matches:
            logger.debug(
                "%r: Hash not verifiable for this provider. Keeping it", subtitle
            )
            matches &= {"hash"}

        # handle equivalent matches
        eq_matches = set()
        if is_episode:
            _episode_checks(video, eq_matches, matches)
        elif is_movie and "imdb_id" in matches:
            logger.debug("Adding imdb_id match equivalents")
            eq_matches |= {"title", "year"}

        matches |= eq_matches

        # handle hearing impaired
        if (
            hearing_impaired is not None
            and subtitle.hearing_impaired == hearing_impaired
        ):
            logger.debug("Matched hearing_impaired")
            matches.add("hearing_impaired")
            orig_matches.add("hearing_impaired")

        # compute the score
        score = sum((scores.get(match, 0) for match in matches))
        logger.info(
            "%r: Computed score %r with final matches %r", subtitle, score, matches
        )

        score_without_hash = sum(
            (
                scores.get(match, 0)
                for match in orig_matches | eq_matches
                if match != "hash"
            )
        )

        modifier_percent = self._modifier_for(subtitle)
        if modifier_percent:
            max_score = MAX_SCORES[video.__class__.__name__.lower()]
            score = apply_score_modifier(score, max_score, modifier_percent)
            score_without_hash = apply_score_modifier(
                score_without_hash, max_score, modifier_percent)
            logger.info("%r: Provider modifier %r%% applied, score is now %r",
                        subtitle, modifier_percent, score)

        return score, score_without_hash

    def _modifier_for(self, subtitle):
        """The modifier for this subtitle's provider, or nothing.

        The hook reads live settings, so a value the user has just broken must
        cost them a modifier rather than every search on the instance.
        """
        if self.modifier is None:
            return 0
        provider_name = getattr(subtitle, "provider_name", None)
        if not provider_name:
            return 0
        try:
            modifier = self.modifier(provider_name) or 0
        except Exception:
            logger.exception("Provider score modifier failed for %r", provider_name)
            return 0
        # NaN and the infinities are floats, so a type check upstream lets them
        # through, and round() raises on both. That would happen outside this
        # guard and take the whole search down with it.
        if not isinstance(modifier, (int, float)) or isinstance(modifier, bool) \
                or not math.isfinite(modifier):
            logger.warning("Ignoring unusable score modifier %r for %r", modifier, provider_name)
            return 0
        return modifier


def _episode_checks(video, eq_matches, matches):
    if "title" in matches:
        logger.debug("Adding title match equivalent")
        eq_matches.add("episode")
    if "series_imdb_id" in matches:
        logger.debug("Adding series_imdb_id match equivalent")
        eq_matches |= {"series", "year"}
    if "imdb_id" in matches:
        logger.debug("Adding imdb_id match equivalents")
        eq_matches |= {"series", "year", "season", "episode"}
    if "tvdb_id" in matches:
        logger.debug("Adding tvdb_id match equivalents")
        eq_matches |= {"series", "year", "season", "episode", "title"}
    if "series_tvdb_id" in matches:
        logger.debug("Adding series_tvdb_id match equivalents")
        eq_matches |= {"series", "year"}

    # specials
    if (
        video.is_special
        and "title" in matches
        and "series" in matches
        and "year" in matches
    ):
        logger.debug("Adding special title match equivalent")
        eq_matches |= {"season", "episode"}


compute_score = ComputeScore()

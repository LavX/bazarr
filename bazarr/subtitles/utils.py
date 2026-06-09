# coding=utf-8
# fmt: off

import logging
import os
import json

from urllib.parse import unquote

from subzero.language import Language
from subzero.video import parse_video
from guessit.jsonutils import GuessitEncoder
from subliminal_patch.score import MAX_SCORES, DEFAULT_SCORES

from app.config import settings
from languages.custom_lang import CustomLanguage
from app.database import get_profiles_list

from .refiners import registered as registered_refiners


def get_video(path, title, sceneName, providers=None, media_type="movie"):
    """
    Construct `Video` instance
    :param path: path to video
    :param title: series/movie title
    :param sceneName: sceneName
    :param providers: provider list for selective hashing
    :param media_type: movie/series
    :return: `Video` instance
    """
    hints = {"title": title, "type": "movie" if media_type == "movie" else "episode"}

    try:
        logging.debug(f'BAZARR guessing video object using video file path: {path}')  # noqa: G004
        skip_hashing = settings.general.skip_hashing
        video = parse_video(path, hints=hints, skip_hashing=skip_hashing, dry_run=False, providers=providers)
        if sceneName != "None":
            # Refine the video object using the sceneName. A scene name we can't parse
            # (e.g. a URL-encoded release title from some indexers) must never discard the
            # good on-disk parse and abort the search (LavX/bazarr#198): _parse_scenename_video
            # returns None on failure so we keep the video parsed from the file path.
            scenename_video = _parse_scenename_video(sceneName, os.path.splitext(path)[1], hints)
            if scenename_video is not None:
                refine_video_with_scenename(initial_video=video, scenename_video=scenename_video)
                logging.debug('BAZARR resulting video object once refined using scene name: %s',
                              json.dumps(vars(video), cls=GuessitEncoder, indent=4, ensure_ascii=False))

        for key, refiner in registered_refiners.items():
            logging.debug("Running refiner: %s", key)
            refiner(path, video)

        logging.debug('BAZARR is using these video object properties: %s', json.dumps(vars(video),
                                                                                      cls=GuessitEncoder, indent=4,
                                                                                      ensure_ascii=False))
        return video

    except Exception as error:
        logging.exception("BAZARR Error (%s) trying to get video information for this file: %s", error, path)


def _parse_scenename_video(scene_name, extension, hints):
    """Parse a scene name into a Video for refinement, degrading gracefully.

    Some indexers store a URL-encoded release title as the scene name (e.g.
    ``Rick%20and%20Morty%20S09E02%20...``). guessit can't make sense of that and
    subliminal raises, which previously bubbled up and made ``get_video`` return None,
    discarding the good on-disk parse and aborting subtitle search (LavX/bazarr#198).
    Try the scene name as-is, then URL-decoded; if both fail, return None so the caller
    keeps the video parsed from the on-disk filename.
    """
    candidates = [scene_name]
    decoded = unquote(scene_name)
    if decoded != scene_name:
        candidates.append(decoded)

    last_error = None
    for candidate in candidates:
        scenename_with_extension = candidate + extension
        try:
            logging.debug('BAZARR guessing video object using scene name: %s', scenename_with_extension)
            return parse_video(scenename_with_extension, hints=hints, dry_run=True)
        except Exception as error:
            last_error = error

    logging.warning("BAZARR could not parse the scene name %r (%s); falling back to the on-disk "
                    "filename for subtitle search", scene_name, last_error)
    return None


def _get_download_code3(subtitle):
    custom = CustomLanguage.from_value(subtitle.language, "language")
    if custom is None:
        return subtitle.language.alpha3
    return custom.alpha3


def _get_lang_obj(alpha3):
    sub = CustomLanguage.from_value(alpha3, "alpha3")
    if sub is None:
        return Language(alpha3)

    return sub.subzero_language()


def _get_scores(media_type, min_movie=None, min_ep=None):
    series = "series" == media_type
    handler = DEFAULT_SCORES['episode'] if series else DEFAULT_SCORES['movie']

    max_score = MAX_SCORES['episode' if series else 'movie']

    min_movie = min_movie or (max_score / 2)
    min_ep = min_ep or (2/3 * max_score)
    min_score = int(min_ep if series else min_movie)

    return (
        max_score * min_score / 100,
        max_score,
        handler.keys(),
    )


def get_ban_list(profile_id):
    if profile_id:
        profile = get_profiles_list(profile_id)
        if profile:
            return {'must_contain': profile['mustContain'] or [],
                    'must_not_contain': profile['mustNotContain'] or []}
    return None


def _set_forced_providers(pool, also_forced=False, forced_required=False):
    # TODO: maybe a separate pool for forced configs? also_foreign/only_foreign is hardcoded
    # in get_providers and this causes updating the pool on every call
    if also_forced and forced_required:
        logging.debug('also_forced and forced_required cannot be both True. also_forced will prevail.')
        forced_required = False
    pool.provider_configs.update(
        {
            "podnapisi": {'also_foreign': also_forced, "only_foreign": forced_required}
        }
    )


def refine_video_with_scenename(initial_video, scenename_video):
    for key, value in vars(scenename_video).items():
        if value and getattr(initial_video, key) in [None, (), {}, []]:
            setattr(initial_video, key, value)
    return initial_video

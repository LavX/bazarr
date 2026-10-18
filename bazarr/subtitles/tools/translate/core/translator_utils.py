# coding=utf-8

import logging
import os
import srt
import datetime

from typing import Union
from types import SimpleNamespace

from app.config import settings
from subzero.language import Language
from subliminal_patch.core import get_subtitle_path
from subliminal_patch.score import MAX_SCORES
from languages.custom_lang import CustomLanguage
from languages.get_languages import alpha3_from_alpha2, language_from_alpha2, language_from_alpha3  # noqa: F401
from subtitles.processing import ProcessSubtitlesResult
from subtitles.indexer.utils import get_subtitle_destination_path
from utilities.path_mappings import path_mappings

from app.database import TableShows, TableEpisodes, TableMovies, database, select

logger = logging.getLogger(__name__)


def validate_translation_params(video_path, source_srt_file, from_lang, to_lang):
    """Validate translation parameters."""
    if not os.path.exists(source_srt_file):
        raise FileNotFoundError(f"Source subtitle file not found: {source_srt_file}")

    if not from_lang or not to_lang:
        raise ValueError("Source and target languages must be specified")

    return True


def convert_language_codes(to_lang, forced=False, hi=False):
    """Convert and validate language codes."""
    orig_to_lang = to_lang
    to_lang = alpha3_from_alpha2(to_lang)

    try:
        lang_obj = Language(to_lang)
    except ValueError:
        custom_lang_obj = CustomLanguage.from_value(to_lang, "alpha3")
        if custom_lang_obj:
            lang_obj = CustomLanguage.subzero_language(custom_lang_obj)
        else:
            raise ValueError(f'Unable to translate to {to_lang}')

    if forced:
        lang_obj = Language.rebuild(lang_obj, forced=True)
    if hi:
        lang_obj = Language.rebuild(lang_obj, hi=True)

    return lang_obj, orig_to_lang


def translated_destination_path(video_path, to_lang, forced, hi):
    """The folder-resolved path a translation targeting ``to_lang`` writes to.

    One resolution names the file everywhere it is named: the translate write
    path computes its destination here, and the sports profile operation binds
    its expected destination here too, because the sports write guard compares
    the two and refuses a translation whose names disagree.

    The name carries the regional tag when the target is a custom language
    that has one (es-419 for Spanish Latino), never the language's internal
    code, with the forced and HI tags in their usual place behind it.
    """
    lang_obj, _ = convert_language_codes(to_lang, forced, hi)
    naming_language = lang_obj if isinstance(lang_obj, Language) else lang_obj.subzero_language()
    custom_lang_obj = CustomLanguage.from_value(alpha3_from_alpha2(to_lang), "alpha3")
    if custom_lang_obj is not None and custom_lang_obj.regional_tag is not None:
        naming_language = SimpleNamespace(basename=custom_lang_obj.regional_tag)

    dest_srt_file_if_alongside_video = get_subtitle_path(
        video_path,
        language=naming_language,
        extension='.srt',
        forced_tag=forced,
        hi_tag=hi
    )

    # Resolved as a write target, not a lookup: the file does not exist yet on a
    # first translation, and the lookup helper answered None for custom folders.
    return get_subtitle_destination_path(
        file=video_path,
        subtitle=os.path.basename(dest_srt_file_if_alongside_video)
    )


def translation_write_kwargs(translator, job_id):
    """The staged_subtitle_write arguments this translation's own context adds.

    A sports operation supplies its publication guard, its bound destination
    versions and its cancel signal. An auto-queued translation supplies the
    destination versions its queueing sighting captured, so a subtitle that
    landed while the job waited is refused rather than replaced. The two
    never mix: an operation binds its own versions, which already carry the
    same guarantee from the moment it bound them.
    """
    if getattr(translator, 'sports_operation', None) is not None:
        from sportarr.profile_hooks import sports_write_kwargs

        return dict(sports_write_kwargs(translator, job_id))
    captured = getattr(translator, 'expected_destination_versions', ())
    return {'expected_versions': captured} if captured else {}


def create_process_result(message, video_path, orig_to_lang, forced, hi, dest_srt_file, media_type, sports_context=None):
    """Create a ProcessSubtitlesResult object with common parameters."""
    if sports_context is not None:
        from sportarr.subtitles import reverse_path
        def prr(path):
            return reverse_path(sports_context, path)
        score = int((settings.translator.default_score / 100) * MAX_SCORES['movie'])
    elif media_type == 'episode':
        prr = path_mappings.path_replace_reverse
        score = int((settings.translator.default_score / 100) * MAX_SCORES['episode'])
    else:
        prr = path_mappings.path_replace_reverse_movie
        score = int((settings.translator.default_score / 100) * MAX_SCORES['movie'])

    return ProcessSubtitlesResult(
        message=message,
        reversed_path=prr(video_path),
        downloaded_language_code2=orig_to_lang,
        downloaded_provider=None,
        score=score,
        forced=forced,
        subtitle_id=None,
        reversed_subtitles_path=prr(dest_srt_file),
        hearing_impaired=hi
    )


def add_translator_info(dest_srt_file, info):
    if settings.translator.translator_info:
        # Load the SRT content
        with open(dest_srt_file, "r", encoding="utf-8") as f:
            srt_content = f.read()

        # Parse subtitles
        subtitles = list(srt.parse(srt_content))

        if subtitles:
            # Find the last subtitle's end time and add the info after it
            last_end = max(sub.end for sub in subtitles)
            start_time = last_end + datetime.timedelta(milliseconds=100)
            end_time = start_time + datetime.timedelta(seconds=4)
        else:
            # If no subtitles exist, add at the end of a typical video
            start_time = datetime.timedelta(hours=2)
            end_time = start_time + datetime.timedelta(seconds=4)

        # Add the info subtitle at the end
        new_sub = srt.Subtitle(
            index=len(subtitles) + 1,
            start=start_time,
            end=end_time,
            content=info
        )
        subtitles.append(new_sub)

        # Re-index (no sorting needed since we're adding at the end)
        for i, sub in enumerate(subtitles, start=1):
            sub.index = i

        with open(dest_srt_file, "w", encoding="utf-8") as f:
            f.write(srt.compose(subtitles))


# The callers do not agree on how to spell this: the manual translate endpoint
# sends "movie" while the batch and mass-operation paths send "movies". Both mean
# the same thing, and matching only one of them silently produced an empty title
# and an empty prompt description for every movie translated from its own page.
MOVIE_MEDIA_TYPES = ('movie', 'movies')


def is_movie_media_type(media_type) -> bool:
    return str(media_type or '').strip().lower() in MOVIE_MEDIA_TYPES


def _scope(statement, table, arr_instance_id):
    """Narrow a lookup to the owning arr instance (#156).

    ``radarrId`` and ``sonarrSeriesId`` are unique only together with
    ``arr_instance_id``, so an unscoped query can return a sibling instance's
    media. Callers that genuinely have no owner (older paths, and the tests that
    seed a single instance) pass None and keep the previous behaviour.
    """
    if arr_instance_id is None:
        return statement
    return statement.where(table.arr_instance_id == arr_instance_id)


def get_description(media_type, radarr_id, sonarr_series_id, arr_instance_id=None, sports_context=None):
    if sports_context is not None:
        from sportarr.profile_hooks import metadata
        return metadata(sports_context)[1]
    try:
        if is_movie_media_type(media_type):
            movie = database.execute(
                _scope(
                    select(TableMovies.title, TableMovies.imdbId, TableMovies.year, TableMovies.overview)
                    .where(TableMovies.radarrId == radarr_id),
                    TableMovies, arr_instance_id)
            ).first()

            if movie:
                return (f"You will translate movie that is called {movie.title} from {movie.year} "
                        f"and it has IMDB ID = {movie.imdbId}. Its overview: {movie.overview}")
            else:
                logger.info(f"No movie found for this radarr_id: {radarr_id}")  # noqa: G004
                return ""

        else:
            series = database.execute(
                _scope(
                    select(TableShows.title, TableShows.imdbId, TableShows.year, TableShows.overview)
                    .where(TableShows.sonarrSeriesId == sonarr_series_id),
                    TableShows, arr_instance_id)
            ).first()

            if series:
                return (f"You will translate TV show that is called {series.title} from {series.year} "
                        f"and it has IMDB ID = {series.imdbId}. Its overview: {series.overview}")
            else:
                logger.info(f"No series found for this sonarr_series_id: {sonarr_series_id}")  # noqa: G004
                return ""
    except Exception:
        logger.exception("Problem with getting media info")
        return ""


def get_title(
        media_type: str,
        radarr_id: Union[int, None] = None,
        sonarr_series_id: Union[int, None] = None,
        sonarr_episode_id: Union[int, None] = None,
        arr_instance_id: Union[int, None] = None,
        sports_context=None
) -> str:
    if sports_context is not None:
        from sportarr.profile_hooks import metadata
        return metadata(sports_context)[0]
    try:
        if is_movie_media_type(media_type):
            if radarr_id is None:
                return ""

            movie_row = database.execute(
                _scope(select(TableMovies.title).where(TableMovies.radarrId == radarr_id),
                       TableMovies, arr_instance_id)
            ).first()

            if movie_row is None:
                return ""

            title_attr = getattr(movie_row, "title", None)
            if title_attr is None:
                return ""

            movie_title = str(title_attr).strip()
            if movie_title == "":
                return ""

            return movie_title

        # Handle series
        if sonarr_series_id is None:
            return ""

        series_row = database.execute(
            _scope(select(TableShows.title).where(TableShows.sonarrSeriesId == sonarr_series_id),
                   TableShows, arr_instance_id)
        ).first()

        if series_row is None:
            return ""

        series_title_attr = getattr(series_row, "title", None)
        if series_title_attr is None:
            return ""

        series_title = str(series_title_attr).strip()
        if series_title == "":
            return ""

        # If episode ID is provided, get episode details and format as "Series - S##E## - Episode Title"
        if sonarr_episode_id is not None:
            episode_row = database.execute(
                _scope(
                    select(TableEpisodes.season, TableEpisodes.episode, TableEpisodes.title)
                    .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
                    TableEpisodes, arr_instance_id)
            ).first()

            if episode_row is not None:
                season = getattr(episode_row, "season", None)
                episode = getattr(episode_row, "episode", None)
                episode_title = getattr(episode_row, "title", None)

                if season is not None and episode is not None:
                    season_str = f"S{season:02d}"
                    episode_str = f"E{episode:02d}"

                    full_title = f"{series_title} - {season_str}{episode_str}"

                    if episode_title and str(episode_title).strip():
                        full_title += f" - {str(episode_title).strip()}"

                    return full_title

        return series_title

    except Exception:
        logger.exception("Problem with getting title")
        return ""

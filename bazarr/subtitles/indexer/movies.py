# coding=utf-8

import gc
import os
import logging
import ast
import time  # noqa: F401

from subliminal_patch import core, search_external_subtitles

from languages.custom_lang import CustomLanguage
from app.database import get_profiles_list, get_profile_cutoff, TableMovies, TableHistoryMovie, \
    get_audio_profile_languages, database, update, select
from languages.get_languages import alpha2_from_alpha3, get_language_set
from app.config import settings
from utilities.helper import get_subtitle_destination_folder
from utilities.path_mappings import path_mappings
from utilities.video_analyzer import embedded_subs_reader
from app.event_handler import event_stream
from subtitles.indexer.utils import add_sync_engine_outputs, add_combined_outputs, guess_external_subtitles, \
    get_external_subtitles_path, normalize_subtitle_language_variant, subtitle_language_with_sync_modifier, \
    subtitle_language_with_combined_modifier
from subtitles.processing import ProcessSubtitlesResult
from subtitles.utils import _get_scores
from radarr.history import history_log_movie
from app.jobs_queue import jobs_queue
from subtitles.adaptive_searching import is_search_given_up

gc.enable()


def store_subtitles_movie(original_path, reversed_path, use_cache=True):
    logging.debug(f'BAZARR started subtitles indexing for this file: {reversed_path}')  # noqa: G004
    actual_subtitles = []
    # Languages of embedded subtitle tracks detected on this pass. Used after
    # indexing to record a source-quality history entry (action=7) per language.
    embedded_languages = []
    if os.path.exists(reversed_path):
        if settings.general.use_embedded_subs:
            logging.debug("BAZARR is trying to index embedded subtitles.")
            item = database.execute(
                select(TableMovies.movie_file_id, TableMovies.file_size)
                .where(TableMovies.path == original_path)) \
                .first()
            if not item:
                logging.exception(f"BAZARR error when trying to select this movie from database: {reversed_path}")  # noqa: G004
            else:
                try:
                    subtitle_languages = embedded_subs_reader(reversed_path,
                                                              file_size=item.file_size,
                                                              movie_file_id=item.movie_file_id,
                                                              use_cache=use_cache)
                    for subtitle_language, subtitle_forced, subtitle_hi, subtitle_codec in subtitle_languages:
                        try:
                            if (settings.general.ignore_pgs_subs and subtitle_codec.lower() == "pgs") or \
                                    (settings.general.ignore_vobsub_subs and subtitle_codec.lower() ==
                                     "vobsub") or \
                                    (settings.general.ignore_ass_subs and subtitle_codec.lower() ==
                                     "ass"):
                                logging.debug("BAZARR skipping %s sub for language: %s" % (subtitle_codec, alpha2_from_alpha3(subtitle_language)))  # noqa: G002
                                continue

                            if alpha2_from_alpha3(subtitle_language) is not None:
                                lang = normalize_subtitle_language_variant(
                                    alpha2_from_alpha3(subtitle_language),
                                    forced=subtitle_forced,
                                    hi=subtitle_hi)
                                logging.debug(f"BAZARR embedded subtitles detected: {lang}")  # noqa: G004
                                actual_subtitles.append([lang, None, None])
                                embedded_languages.append(lang)
                        except Exception as error:
                            logging.debug(f"BAZARR unable to index this unrecognized language: {subtitle_language} "  # noqa: G004
                                          f"({error})")
                except Exception:
                    logging.exception(
                        f"BAZARR error when trying to analyze this {os.path.splitext(reversed_path)[1]} file: "  # noqa: G004
                        f"{reversed_path}")

        try:
            dest_folder = get_subtitle_destination_folder()
            core.CUSTOM_PATHS = [dest_folder] if dest_folder else []

            # get previously indexed subtitles that haven't changed:
            item = database.execute(
                select(TableMovies.subtitles)
                .where(TableMovies.path == original_path))\
                .first()
            if not item:
                previously_indexed_subtitles_to_exclude = []
            else:
                previously_indexed_subtitles = ast.literal_eval(item.subtitles) if item.subtitles else []
                previously_indexed_subtitles_to_exclude = [x for x in previously_indexed_subtitles
                                                           if len(x) == 3 and
                                                           x[1] and
                                                           os.path.isfile(path_mappings.path_replace(x[1])) and
                                                           os.stat(path_mappings.path_replace(x[1])).st_size == x[2]]

            subtitles = search_external_subtitles(reversed_path, languages=get_language_set(),
                                                  only_one=settings.general.single_language)
            full_dest_folder_path = os.path.dirname(reversed_path)
            if dest_folder:
                if settings.general.subfolder == "absolute":
                    full_dest_folder_path = dest_folder
                elif settings.general.subfolder == "relative":
                    full_dest_folder_path = os.path.join(os.path.dirname(reversed_path), dest_folder)
            subtitles = add_sync_engine_outputs(full_dest_folder_path, subtitles)
            subtitles = add_combined_outputs(full_dest_folder_path, subtitles)
            subtitles = guess_external_subtitles(full_dest_folder_path, subtitles, "movie",
                                                 previously_indexed_subtitles_to_exclude)
        except Exception as e:
            logging.exception(f"BAZARR unable to index external subtitles for this file {reversed_path}: {repr(e)}")  # noqa: G004
        else:
            for subtitle, language in subtitles.items():
                valid_language = False
                if language:
                    if hasattr(language, 'alpha3'):
                        valid_language = alpha2_from_alpha3(language.alpha3)
                else:
                    logging.debug(f"Skipping subtitles because we are unable to define language: {subtitle}")  # noqa: G004
                    continue

                if not valid_language:
                    logging.debug(f'{language.alpha3} is an unsupported language code.')  # noqa: G004
                    continue

                subtitle_path = get_external_subtitles_path(reversed_path, subtitle)

                try:
                    subtitle_size = os.stat(subtitle_path).st_size
                except FileNotFoundError:
                    logging.debug(f"BAZARR skipping missing subtitle file: {subtitle_path}")  # noqa: G004
                    continue

                custom = CustomLanguage.found_external(subtitle, subtitle_path)

                if custom is not None:
                    actual_subtitles.append([custom, path_mappings.path_replace_reverse_movie(subtitle_path),
                                             subtitle_size])

                elif str(language.basename) != 'und':
                    if language.forced:
                        language_str = f'{language}:forced'
                    elif language.hi:
                        language_str = f'{language}:hi'
                    else:
                        language_str = str(language)
                    language_str = subtitle_language_with_sync_modifier(language_str, subtitle)
                    language_str = subtitle_language_with_combined_modifier(language_str, subtitle)
                    logging.debug(f"BAZARR external subtitles detected: {language_str}")  # noqa: G004
                    actual_subtitles.append([language_str, path_mappings.path_replace_reverse_movie(subtitle_path),
                                             subtitle_size])

        database.execute(
            update(TableMovies)
            .values(subtitles=str(actual_subtitles))
            .where(TableMovies.path == original_path))
        matching_movies = database.execute(
            select(TableMovies.radarrId)
            .where(TableMovies.path == original_path))\
            .all()

        for movie in matching_movies:
            if movie:
                logging.debug(f"BAZARR storing those languages to DB: {actual_subtitles}")  # noqa: G004
                list_missing_subtitles_movies(no=movie.radarrId)
                if embedded_languages:
                    # Pass the DB-side path (original_path), not the local
                    # filesystem path. history_log_movie stores result.path
                    # verbatim, and download history rows store DB-side paths
                    # via path_replace_reverse_movie. Embedded rows must match
                    # so path comparisons line up in path-mapped installs.
                    _log_embedded_history_movie(movie.radarrId, embedded_languages, original_path)
            else:
                logging.debug(f"BAZARR haven't been able to update existing subtitles to DB: {actual_subtitles}")  # noqa: G004
    else:
        logging.debug("BAZARR this file doesn't seems to exist or isn't accessible.")

    logging.debug(f'BAZARR ended subtitles indexing for this file: {reversed_path}')  # noqa: G004

    return actual_subtitles


def _log_embedded_history_movie(radarr_id, embedded_languages, reversed_path):
    """Record source-quality history entries for newly detected embedded subtitles.

    Why: Embedded subtitle tracks come from disc/streaming rips and are source
    quality, but previously had no score in history so they appeared as unknown
    quality and could be wrongly targeted for upgrade.
    What: For each embedded language not already recorded, writes a
    TableHistoryMovie entry with action=7 (EmbeddedSource) and score=score_out_of
    (100%). A history lookup deduplicates so re-indexing the same movie does not
    duplicate entries.
    Test: Index a movie with an embedded track twice; assert exactly one action=7
    row exists for that movie+language with score == score_out_of.
    """
    try:
        _, score_out_of, _ = _get_scores('movie')

        for lang in embedded_languages:
            lang = normalize_subtitle_language_variant(lang)
            # Dedup: skip if we already logged action=7 for this movie+language.
            # Not atomic under AUTOCOMMIT. Concurrent indexing of the same movie
            # could produce duplicates, but this is rare in practice and consistent
            # with how other parts of Bazarr dedup history entries.
            existing = database.execute(
                select(TableHistoryMovie.id)
                .where(TableHistoryMovie.radarrId == radarr_id)
                .where(TableHistoryMovie.language == lang)
                .where(TableHistoryMovie.action == 7)).first()
            if existing:
                continue

            result = ProcessSubtitlesResult(
                message=f"{lang} embedded subtitles detected.",
                reversed_path=reversed_path,
                downloaded_language_code2=lang.split(':')[0],
                downloaded_provider="embedded",
                score=score_out_of,
                forced=lang.endswith(':forced'),
                subtitle_id=None,
                reversed_subtitles_path=None,
                hearing_impaired=lang.endswith(':hi'))
            history_log_movie(action=7, radarr_id=radarr_id, result=result,
                              fake_provider="embedded", fake_score=score_out_of)
    except Exception:
        logging.exception("BAZARR error writing embedded subtitle history for movie %s", radarr_id)


def list_missing_subtitles_movies(no=None):
    stmt = select(TableMovies.radarrId,
                  TableMovies.subtitles,
                  TableMovies.failedAttempts,
                  TableMovies.profileId,
                  TableMovies.audio_language)

    if no:
        movies_subtitles = database.execute(stmt.where(TableMovies.radarrId == no)).all()
    else:
        movies_subtitles = database.execute(stmt).all()

    use_embedded_subs = settings.general.use_embedded_subs

    matches_audio = lambda language: any(x['code2'] == language['language'] for x in get_audio_profile_languages(  # noqa: E731
                                movie_subtitles.audio_language))

    for movie_subtitles in movies_subtitles:
        missing_subtitles_text = '[]'
        if movie_subtitles.profileId:
            # get desired subtitles
            desired_subtitles_temp = get_profiles_list(profile_id=movie_subtitles.profileId)
            desired_subtitles_list = []
            if desired_subtitles_temp:
                for language in desired_subtitles_temp['items']:
                    if language['audio_exclude'] == "True":
                        if matches_audio(language):
                            continue
                    if language['audio_only_include'] == "True":
                        if not matches_audio(language):
                            continue
                    desired_subtitles_list.append({'language': language['language'],
                                                   'forced': language['forced'],
                                                   'hi': language['hi']})

            # get existing subtitles
            actual_subtitles_list = []
            if movie_subtitles.subtitles is not None:
                if use_embedded_subs:
                    actual_subtitles_temp = ast.literal_eval(movie_subtitles.subtitles)
                else:
                    actual_subtitles_temp = [x for x in ast.literal_eval(movie_subtitles.subtitles) if x[1]]

                for subtitles in actual_subtitles_temp:
                    subtitles = subtitles[0].split(':')
                    lang = subtitles[0]
                    forced = False
                    hi = False
                    if len(subtitles) > 1:
                        if subtitles[1] == 'forced':
                            forced = True
                            hi = False
                        elif subtitles[1] == 'hi':
                            forced = False
                            hi = True
                    actual_subtitles_list.append({'language': lang,
                                                  'forced': str(forced),
                                                  'hi': str(hi)})

            # check if cutoff is reached and skip any further check
            cutoff_met = False
            cutoff_temp_list = get_profile_cutoff(profile_id=movie_subtitles.profileId)

            if cutoff_temp_list:
                for cutoff_temp in cutoff_temp_list:
                    cutoff_language = {'language': cutoff_temp['language'],
                                       'forced': cutoff_temp['forced'],
                                       'hi': cutoff_temp['hi']}
                    if cutoff_temp['audio_only_include'] == 'True' and not matches_audio(cutoff_temp):
                        # We don't want subs in this language unless it matches
                        # the audio. Don't use it to meet the cutoff.
                        continue
                    elif cutoff_temp['audio_exclude'] == 'True' and matches_audio(cutoff_temp):
                        # The cutoff is met through one of the audio tracks.
                        cutoff_met = True
                    elif cutoff_language in actual_subtitles_list:
                        cutoff_met = True
                    # HI is considered as good as normal
                    elif (cutoff_language and
                          {'language': cutoff_language['language'],
                           'forced': 'False',
                           'hi': 'True'} in actual_subtitles_list):
                        cutoff_met = True

            if cutoff_met:
                missing_subtitles_text = str([])
            else:
                # get difference between desired and existing subtitles
                missing_subtitles_list = []
                for item in desired_subtitles_list:
                    if item not in actual_subtitles_list:
                        missing_subtitles_list.append(item)  # noqa: PERF401

                    # remove missing that have forced or hi subtitles for this language in existing
                for item in actual_subtitles_list:
                    if item['hi'] == 'True':
                        try:
                            missing_subtitles_list.remove({'language': item['language'],
                                                           'forced': 'False',
                                                           'hi': 'False'})
                        except ValueError:
                            pass

                # make the missing languages list looks like expected
                missing_subtitles_output_list = []
                for item in missing_subtitles_list:
                    if is_search_given_up(item['language'], movie_subtitles.failedAttempts):
                        continue
                    lang = item['language']
                    if item['forced'] == 'True':
                        lang += ':forced'
                    elif item['hi'] == 'True':
                        lang += ':hi'
                    missing_subtitles_output_list.append(lang)

                missing_subtitles_text = str(missing_subtitles_output_list)

        database.execute(
            update(TableMovies)
            .values(missing_subtitles=missing_subtitles_text)
            .where(TableMovies.radarrId == movie_subtitles.radarrId))

        event_stream(type='movie', payload=movie_subtitles.radarrId)
        event_stream(type='movie-wanted', action='update', payload=movie_subtitles.radarrId)
    event_stream(type='badges')


def movies_full_scan_subtitles(job_id=None, use_cache=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Indexing all existing movies subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    if use_cache is None:
        use_cache = settings.radarr.use_ffprobe_cache

    movies = database.execute(
        select(TableMovies.path, TableMovies.title))\
        .all()

    jobs_queue.update_job_progress(job_id=job_id, progress_max=len(movies), progress_message='Indexing')
    for i, movie in enumerate(movies, start=1):
        jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_message=movie.title)
        store_subtitles_movie(movie.path, path_mappings.path_replace_movie(movie.path), use_cache=use_cache)

    logging.info('BAZARR All existing movie subtitles indexed from disk.')

    jobs_queue.update_job_name(job_id=job_id, new_job_name="Indexed all existing movies subtitles")

    gc.collect()


def movies_scan_subtitles(no):
    movies = database.execute(
        select(TableMovies.path)
        .where(TableMovies.radarrId == no)
        .order_by(TableMovies.radarrId)) \
        .all()

    for movie in movies:
        store_subtitles_movie(movie.path, path_mappings.path_replace_movie(movie.path), use_cache=False)

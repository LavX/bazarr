# coding=utf-8
# fmt: off

import logging
import operator
import ast

from datetime import datetime, timedelta
from functools import reduce
from sqlalchemy import and_, exists, false, or_
from sqlalchemy.orm import aliased

from app.config import settings
from app.database import get_exclusion_clause, get_audio_profile_languages, TableShows, TableEpisodes, TableMovies, \
    TableHistory, TableHistoryMovie, database, select, func, get_profiles_list
from app.jobs_queue import jobs_queue
from app.get_providers import get_providers
from app.notifier import send_notifications, send_notifications_movie
from radarr.history import history_log_movie
from sonarr.history import history_log
from subtitles.indexer.movies import store_subtitles_movie
from subtitles.indexer.series import store_subtitles
from subtitles.score_override import MANUAL_SCORE_ACTION
from utilities.path_mappings import path_mappings
from .download import generate_subtitles
from app.event_handler import event_stream


def upgrade_subtitles(wait_for_completion=False):
    use_sonarr = settings.general.use_sonarr
    use_radarr = settings.general.use_radarr

    if use_sonarr:
        upgrade_episodes_subtitles(wait_for_completion=wait_for_completion)

    if use_radarr:
        upgrade_movies_subtitles(wait_for_completion=wait_for_completion)

    logging.info('BAZARR Finished searching for Subtitles to upgrade. Check History for more information.')


def upgrade_episodes_subtitles(job_id=None, sonarr_series_ids=None, sonarr_series_filters=None,
                               wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Trying to upgrade episodes subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    episodes_to_upgrade = get_upgradable_episode_subtitles()

    query = select(TableHistory.id,
               TableShows.title.label('seriesTitle'),
               TableEpisodes.season,
               TableEpisodes.episode,
               TableEpisodes.title,
               TableHistory.language,
               TableEpisodes.audio_language,
               TableHistory.video_path,
               TableEpisodes.sceneName,
               TableHistory.score,
               TableHistory.sonarrEpisodeId,
               TableHistory.sonarrSeriesId,
               TableHistory.arr_instance_id,
               TableHistory.subtitles_path,
               TableEpisodes.path,
               TableShows.profileId,
               TableEpisodes.subtitles.label('external_subtitles')) \
        .select_from(TableHistory) \
        .join(TableShows, onclause=and_(TableHistory.sonarrSeriesId == TableShows.sonarrSeriesId,
                                  TableHistory.arr_instance_id == TableShows.arr_instance_id)) \
        .join(TableEpisodes, onclause=and_(TableHistory.sonarrEpisodeId == TableEpisodes.sonarrEpisodeId,
                                     TableHistory.arr_instance_id == TableEpisodes.arr_instance_id))

    if sonarr_series_filters:
        query = query.where(or_(*[
            and_(TableHistory.sonarrSeriesId == series_id,
                 TableHistory.arr_instance_id == arr_instance_id)
            if arr_instance_id is not None else TableHistory.sonarrSeriesId == series_id
            for series_id, arr_instance_id in sonarr_series_filters
        ]))
    elif sonarr_series_ids:
        query = query.where(TableHistory.sonarrSeriesId.in_(sonarr_series_ids))

    # Streamed rather than .all(): the Row objects and the dicts built from them
    # would otherwise both be held at once, over a history table that is never
    # pruned.
    #
    # The video_path filter deliberately stays in Python rather than moving into
    # the join. SQL equality is not NULL safe, so a NULL on either side drops the
    # row instead of comparing equal, and this file already works around exactly
    # that elsewhere with is_not_distinct_from. Today TableEpisodes.path is NOT
    # NULL so the two agree, but that is a constraint sixty lines away in another
    # module, and it keeps the episode side symmetrical with the movie side below,
    # where the same comparison has to stay in Python so a dropped candidate can
    # be explained in the log.
    episodes_data = [{
        'id': x.id,
        'seriesTitle': x.seriesTitle,
        'season': x.season,
        'episode': x.episode,
        'title': x.title,
        'language': x.language,
        'audio_language': x.audio_language,
        'video_path': x.video_path,
        'sceneName': x.sceneName,
        'score': x.score,
        'sonarrEpisodeId': x.sonarrEpisodeId,
        'sonarrSeriesId': x.sonarrSeriesId,
        'arr_instance_id': x.arr_instance_id,
        'subtitles_path': x.subtitles_path,
        'path': x.path,
        'profileId': x.profileId,
        'external_subtitles': [y[1] for y in ast.literal_eval(x.external_subtitles) if y[1]],
    } for x in database.execute(query)
    if _language_still_desired(x.language, x.profileId) and
       x.video_path == x.path
    ]

    # Only candidates go on; the rest were dropped below anyway, and the
    # extraction lineage is then read once for exactly those.
    episodes_data = [item for item in episodes_data if item['id'] in episodes_to_upgrade]
    extracted = _extracted_paths(TableHistory, TableHistory.sonarrEpisodeId,
                                 {(item['sonarrEpisodeId'], item['arr_instance_id']) for item in episodes_data})

    for item in episodes_data:
        lineage = (item['sonarrEpisodeId'], item['arr_instance_id'])
        if item['subtitles_path'] in item['external_subtitles'] and (*lineage, item['subtitles_path']) in extracted:
            logging.debug(f"Upgrade candidate {item['id']} dropped: its file was since extracted from an "  # noqa: G004
                          f"embedded track ({item['subtitles_path']})")
            continue
        # do not consider subtitles that do not exist on disk anymore
        if item['subtitles_path'] not in item['external_subtitles']:
            current_sub = _find_current_subtitle_for_language(item['language'], item['external_subtitles'])
            if current_sub and (*lineage, current_sub) in extracted:
                logging.debug(f"Upgrade candidate {item['id']} dropped: the current subtitle for its language "  # noqa: G004
                              f"({current_sub}) was extracted from an embedded track")
                continue
            if current_sub:
                logging.debug(f"Upgrade candidate {item['id']} ({item['seriesTitle']} S{item['season']:02d}E"  # noqa: G004
                              f"{item['episode']:02d}): history path no longer on disk, using current subtitle "
                              f"for same language ({current_sub})")
                item['subtitles_path'] = current_sub
            else:
                continue

        # Mark upgradable and get original_id
        item.update({'original_id': episodes_to_upgrade.get(item['id'])})
        item.update({'upgradable': item['id'] in episodes_to_upgrade.keys()})

        # cleanup the unused attributes
        del item['path']
        del item['external_subtitles']

    # Make sure to keep only upgradable episode subtitles
    episodes_data = [x for x in episodes_data if 'upgradable' in x and x['upgradable']]

    count_episode_to_upgrade = len(episodes_data)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_episode_to_upgrade)

    if count_episode_to_upgrade == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    for i, episode in enumerate(episodes_data, start=1):
        providers_list = get_providers()

        jobs_queue.update_job_progress(job_id=job_id, progress_value=i,
                                       progress_message=f'{episode["seriesTitle"]} - S{episode["season"]:02d}E'
                                                        f'{episode["episode"]:02d} - {episode["title"]}')

        if not providers_list:
            logging.info("BAZARR All providers are throttled")
            return

        language, is_forced, is_hi = parse_language_string(episode['language'])
        if is_hi and not _is_hi_required(language, episode['profileId']):
            is_hi = 'False'

        audio_language_list = get_audio_profile_languages(episode['audio_language'])
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = 'None'

        result = list(generate_subtitles(path_mappings.path_replace(episode['video_path']),
                                         [(language, is_hi, is_forced)],
                                         audio_language,
                                         str(episode['sceneName']),
                                         episode['seriesTitle'],
                                         'series',
                                         episode['profileId'],
                                         forced_minimum_score=int(episode['score'] or 0) + 1,
                                         is_upgrade=True,
                                         previous_subtitles_to_delete=path_mappings.path_replace(
                                             episode['subtitles_path']),
                                         job_id=job_id,
                                         arr_instance_id=episode['arr_instance_id']))

        if result:
            if isinstance(result, list) and len(result):
                result = result[0]
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles(episode['video_path'], path_mappings.path_replace_instance(episode['video_path'], episode['arr_instance_id'], 'episode'),
                            arr_instance_id=episode['arr_instance_id'])
            history_log(3, episode['sonarrSeriesId'], episode['sonarrEpisodeId'], result,
                        upgraded_from_id=episode['original_id'],
                        arr_instance_id=episode['arr_instance_id'])
            send_notifications(episode['sonarrSeriesId'], episode['sonarrEpisodeId'], result.message,
                               arr_instance_id=episode['arr_instance_id'])
            event_stream(type="episode-history")
    jobs_queue.update_job_name(job_id=job_id, new_job_name='Tried to upgrade episodes subtitles')


def upgrade_movies_subtitles(job_id=None, radarr_ids=None, radarr_filters=None, wait_for_completion=False):
    if not job_id:
        jobs_queue.add_job_from_function("Trying to upgrade movies subtitles", is_progress=True,
                                         wait_for_completion=wait_for_completion)
        return

    movies_to_upgrade = get_upgradable_movies_subtitles()

    query = select(TableHistoryMovie.id,
               TableMovies.title,
               TableHistoryMovie.language,
               TableMovies.audio_language,
               TableHistoryMovie.video_path,
               TableMovies.sceneName,
               TableHistoryMovie.score,
               TableHistoryMovie.radarrId,
               TableHistoryMovie.arr_instance_id,
               TableHistoryMovie.subtitles_path,
               TableMovies.path,
               TableMovies.profileId,
               TableMovies.subtitles.label('external_subtitles')) \
        .select_from(TableHistoryMovie) \
        .join(TableMovies, onclause=and_(TableHistoryMovie.radarrId == TableMovies.radarrId,
                                  TableHistoryMovie.arr_instance_id == TableMovies.arr_instance_id))

    if radarr_filters:
        query = query.where(or_(*[
            and_(TableHistoryMovie.radarrId == radarr_id,
                 TableHistoryMovie.arr_instance_id == arr_instance_id)
            if arr_instance_id is not None else TableHistoryMovie.radarrId == radarr_id
            for radarr_id, arr_instance_id in radarr_filters
        ]))
    elif radarr_ids:
        query = query.where(TableHistoryMovie.radarrId.in_(radarr_ids))

    # Streamed rather than .all(): the diagnostics below explain why each
    # candidate was dropped, so the filtering stays in Python, but there is no
    # reason to hold every Row of the history join in memory at once while the
    # dicts for the survivors are being built alongside them.
    movies_data = []
    for x in database.execute(query):
        if not _language_still_desired(x.language, x.profileId):
            if x.id in movies_to_upgrade:
                logging.debug(f"Upgrade candidate {x.id} ({x.title}) dropped: language {x.language} no longer desired "  # noqa: G004
                              f"in profile {x.profileId}")
            continue
        if x.video_path != x.path:
            if x.id in movies_to_upgrade:
                logging.debug(f"Upgrade candidate {x.id} ({x.title}) dropped: video_path mismatch "  # noqa: G004
                              f"(history={x.video_path} != current={x.path})")
            continue
        movies_data.append({
            'id': x.id,
            'title': x.title,
            'language': x.language,
            'audio_language': x.audio_language,
            'video_path': x.video_path,
            'sceneName': x.sceneName,
            'score': x.score,
            'radarrId': x.radarrId,
            'arr_instance_id': x.arr_instance_id,
            'path': x.path,
            'profileId': x.profileId,
            'subtitles_path': x.subtitles_path,
            'external_subtitles': [y[1] for y in ast.literal_eval(x.external_subtitles) if y[1]],
        })

    # Only candidates need the lineage check; it is read once for them.
    extracted = _extracted_paths(TableHistoryMovie, TableHistoryMovie.radarrId,
                                 {(item['radarrId'], item['arr_instance_id']) for item in movies_data
                                  if item['id'] in movies_to_upgrade})

    for item in movies_data:
        lineage = (item['radarrId'], item['arr_instance_id'])
        if item['id'] in movies_to_upgrade and item['subtitles_path'] in item['external_subtitles'] \
                and (*lineage, item['subtitles_path']) in extracted:
            logging.debug(f"Upgrade candidate {item['id']} ({item['title']}) dropped: its file was since "  # noqa: G004
                          f"extracted from an embedded track ({item['subtitles_path']})")
            continue
        # do not consider subtitles that do not exist on disk anymore
        if item['subtitles_path'] not in item['external_subtitles']:
            # try to find a current subtitle for the same language (file may have been renamed/re-downloaded)
            current_sub = _find_current_subtitle_for_language(item['language'], item['external_subtitles'])
            if item['id'] in movies_to_upgrade and current_sub and (*lineage, current_sub) in extracted:
                logging.debug(f"Upgrade candidate {item['id']} ({item['title']}) dropped: the current subtitle "  # noqa: G004
                              f"for its language ({current_sub}) was extracted from an embedded track")
                continue
            if current_sub:
                logging.debug(f"Upgrade candidate {item['id']} ({item['title']}): history path no longer on disk, "  # noqa: G004
                              f"using current subtitle for same language ({current_sub})")
                item['subtitles_path'] = current_sub
            else:
                if item['id'] in movies_to_upgrade:
                    logging.debug(f"Upgrade candidate {item['id']} ({item['title']}) dropped: no subtitle for language "  # noqa: G004
                                  f"{item['language']} found on disk")
                continue

        # Mark upgradable and get original_id
        item.update({'original_id': movies_to_upgrade.get(item['id'])})
        item.update({'upgradable': item['id'] in movies_to_upgrade.keys()})

        # cleanup the unused attributes
        del item['path']
        del item['external_subtitles']

    # Make sure to keep only upgradable movie subtitles
    movies_data = [x for x in movies_data if 'upgradable' in x and x['upgradable']]

    count_movie_to_upgrade = len(movies_data)
    jobs_queue.update_job_progress(job_id=job_id, progress_max=count_movie_to_upgrade)

    if count_movie_to_upgrade == 0:
        jobs_queue.update_job_progress(job_id=job_id, progress_value='max')

    for i, movie in enumerate(movies_data, start=1):
        providers_list = get_providers()

        jobs_queue.update_job_progress(job_id=job_id, progress_value=i, progress_message=movie['title'])

        if not providers_list:
            logging.info("BAZARR All providers are throttled")
            return

        language, is_forced, is_hi = parse_language_string(movie['language'])
        if is_hi and not _is_hi_required(language, movie['profileId']):
            is_hi = 'False'

        audio_language_list = get_audio_profile_languages(movie['audio_language'])
        if len(audio_language_list) > 0:
            audio_language = audio_language_list[0]['name']
        else:
            audio_language = 'None'

        result = list(generate_subtitles(path_mappings.path_replace_movie(movie['video_path']),
                                         [(language, is_hi, is_forced)],
                                         audio_language,
                                         str(movie['sceneName']),
                                         movie['title'],
                                         'movie',
                                         movie['profileId'],
                                         forced_minimum_score=int(movie['score'] or 0) + 1,
                                         is_upgrade=True,
                                         previous_subtitles_to_delete=path_mappings.path_replace_movie(
                                             movie['subtitles_path']),
                                         job_id=job_id,
                                         arr_instance_id=movie['arr_instance_id']))
        if result:
            if isinstance(result, list) and len(result):
                result = result[0]
            if isinstance(result, tuple) and len(result):
                result = result[0]
            store_subtitles_movie(movie['video_path'],
                                  path_mappings.path_replace_instance(movie['video_path'], movie['arr_instance_id'], 'movie'),
                                  arr_instance_id=movie['arr_instance_id'])
            history_log_movie(3, movie['radarrId'], result, upgraded_from_id=movie['original_id'],
                              arr_instance_id=movie['arr_instance_id'])
            send_notifications_movie(movie['radarrId'], result.message,
                                     arr_instance_id=movie['arr_instance_id'])
            event_stream(type="movie-history")
    jobs_queue.update_job_name(job_id=job_id, new_job_name='Tried to upgrade movies subtitles')


def get_queries_condition_parameters():
    days_to_upgrade_subs = settings.general.days_to_upgrade_subs
    minimum_timestamp = (datetime.now() - timedelta(days=int(days_to_upgrade_subs)))

    # action=4 (Upload), action=7 (EmbeddedSource) and action=8 (Extracted) are
    # never candidates: an upload is the user's final file, and an embedded
    # track and a file copied out of one are source quality. Manual downloads
    # (2) and translations (6) are opt-ins; both default off on a new install.
    query_actions = [1]
    if getattr(settings.general, 'upgrade_manual', False):
        query_actions.append(2)
    query_actions.append(3)
    if getattr(settings.general, 'upgrade_translated', False):
        query_actions.append(6)

    return [minimum_timestamp, query_actions]


# An extracted file is the current subtitle for its language from the moment
# it is written. It is never a candidate itself, and it also ends the candidacy
# of any older row of the same video and language: that row's file is gone (the
# extraction refuses to overwrite one), and the candidate path would otherwise
# fall back to the extracted file and replace it with a download.
EXTRACTED_ACTION = 8

# A hand upload replaces the file at its path, so it is final in the same way:
# it is never a candidate, whatever upgrade_manual says. In both modes it also
# ends the candidacy of any older row of the same video and language, and
# retires an older translation of that language. With upgrade_manual off (the
# default on a new install), a hand upload also retires an older download or
# translation of the same item and language.
UPLOAD_ACTION = 4


def latest_row_actions(query_actions):
    """The actions that decide which row is the latest for a video and language.

    Extractions and hand uploads always count here, though neither is ever a
    candidate, so a newer one ends the candidacy of an older download.
    """
    return [*query_actions, UPLOAD_ACTION, EXTRACTED_ACTION]


# An Embedded Source row records that the video carries a track of its
# language. It is never a candidate itself, and it also ends the candidacy of
# any older row of the same video and language: from then on the language is
# there at source quality, and the file at the older row's path need no longer
# be that row's download. A tool that copies the track out next to the video
# and strips it from the container leaves exactly that, the track as an
# ordinary file at the download's path, which the upgrade would replace.
EMBEDDED_SOURCE_ACTION = 7


def newer_embedded_source(table):
    """Holds for a history row when a newer Embedded Source row covers its language.

    The Embedded Source row is for the same video and owner. Plain and HI
    count as one language, as they do in the fallback to the current file of
    a language, so a track flagged HI ends a plain download too. Forced stays
    apart: a forced track covers only part of the dialogue. This is a
    condition of its own, not an action in latest_row_actions(), because that
    keys on the exact language.

    The row counts only while the track does. With embedded subtitles switched
    off no track covers a language, so nothing holds; the indexer deletes the
    row of a track the user hides or an ignore switch skips (see
    retract_embedded_history()), so those stop holding too, while a track that
    is merely gone from the container still does.
    """
    if not settings.general.use_embedded_subs:
        return false()
    embedded = aliased(table)
    return exists().where(
        embedded.action == EMBEDDED_SOURCE_ACTION,
        embedded.video_path == table.video_path,
        embedded.arr_instance_id.is_not_distinct_from(table.arr_instance_id),
        embedded.timestamp > table.timestamp,
        or_(embedded.language == table.language,
            embedded.language == table.language + ':hi',
            embedded.language + ':hi' == table.language))


# Every action that puts a different file at a path, or removes it.
_FILE_WRITING_ACTIONS = (0, 1, 2, 3, 4, 6, EXTRACTED_ACTION)


def _extracted_paths(table, media_column, media):
    """The ``(media id, owner, path)`` triples whose file has extraction lineage.

    A file extracted from an embedded track is source quality, and the
    upgrade must never adopt it as the file it replaces: through the fallback
    for a candidate whose file is gone (which takes the current file of the
    language and treats HI and plain as one), or through a candidate's own
    path. latest_row_actions() only covers the same language key.

    Lineage is read from history alone: a path is extracted while the newest
    row that put a file there is an extraction. Edits, mods and syncs write
    no such row, so they keep it; a later download, upload, translation or
    deletion at the path ends it. Nothing about the file's content is
    consulted, so nothing that changes or cannot read it drops the
    protection. ``media`` is the ``(media id, owner)`` pairs to look at.
    """
    if not media:
        return set()
    ids = sorted({media_id for media_id, _owner in media})
    newest = {}
    for start in range(0, len(ids), 900):
        rows = database.execute(
            select(media_column.label('media_id'), table.arr_instance_id, table.subtitles_path, table.action)
            .where(media_column.in_(ids[start:start + 900]), table.action.in_(_FILE_WRITING_ACTIONS),
                   table.subtitles_path.is_not(None))
            .order_by(table.timestamp.desc(), table.id.desc())).all()
        for row in rows:
            newest.setdefault((row.media_id, row.arr_instance_id, row.subtitles_path), row.action)
    return {key for key, action in newest.items()
            if action == EXTRACTED_ACTION and (key[0], key[1]) in media}


def _retirement_evidence_actions(query_actions):
    """The actions whose rows retire a translated row: real subtitle files.

    That is every candidate action except the translation (6), plus a hand
    upload (UPLOAD_ACTION) in both modes of upgrade_manual. The manual download
    actions only appear here when the user opted into upgrading them.
    """
    return [action for action in query_actions if action != 6] + [UPLOAD_ACTION]


def _retired_translated_rows(table, rows, provider_actions, minimum_timestamp):
    """History ids of translated rows a real subtitle has already replaced.

    A translated row is always an upgrade candidate: it carries
    translator.default_score (50% of the scale) or no score at all, so the bar
    it sets is beaten by any real listing. Nothing used to retire it either. The
    upgrade records the language of the subtitle the provider returned, which
    need not be the variant string of the row it replaced: a "prefer HI" profile
    translates into nl:hi while the provider result lands as plain nl, so no
    second row is ever written in that group and the translated row stays the
    newest row of it for good.

    The wanted scan then translates that language again as soon as the upgrade
    removes the translated file it replaced, which hands the next cycle the same
    50% baseline, and the identical listing is downloaded again: same provider,
    same score, every upgrade run, which is the loop this returns the ids to
    break.

    One row of provider_actions for the same video, instance and language is
    what retires the translated row (see _retirement_evidence_actions for what
    those are: a hand upload always counts, whatever upgrade_manual says), so a
    translation is replaced by a real subtitle once rather than once per cycle.
    The language comparison ignores the :hi and :forced variant and runs here,
    in Python: no portable SQL expression for it reads the same on SQLite and
    PostgreSQL. The timestamp bound only keeps the read inside the upgrade
    window the candidates themselves are drawn from.
    """
    translated_rows = [row for row in rows if row.action == 6]
    if not translated_rows:
        return set()

    provider_rows = database.execute(
        select(table.video_path,
               table.arr_instance_id,
               table.language)
        .where(and_(table.action.in_(provider_actions),
                    table.video_path.in_({row.video_path for row in translated_rows}),
                    table.timestamp > minimum_timestamp))) \
        .all()

    retired = set()
    for row in translated_rows:
        language = (row.language or '').split(':')[0]
        for provider_row in provider_rows:
            if provider_row.video_path != row.video_path:
                continue
            if provider_row.arr_instance_id != row.arr_instance_id:
                continue
            if (provider_row.language or '').split(':')[0] != language:
                continue
            retired.add(row.id)
            break
    return retired


def _manually_pinned_rows(table, rows):
    """History ids of candidate rows whose subtitle has a manual score.

    A manual score is a row of MANUAL_SCORE_ACTION for the same video, instance,
    language and subtitle file, at least as new as the candidate. Such a
    subtitle is not upgraded: the user has fixed its score. A download written
    after the manual score is newer, so the candidate then comes back.
    """
    if not rows:
        return set()

    overrides = database.execute(
        select(table.video_path,
               table.arr_instance_id,
               table.language,
               table.subtitles_path,
               table.timestamp)
        .where(and_(table.action == MANUAL_SCORE_ACTION,
                    table.video_path.in_({row.video_path for row in rows})))).all()

    pinned = set()
    for row in rows:
        for override in overrides:
            if (override.video_path == row.video_path
                    and override.arr_instance_id == row.arr_instance_id
                    and override.language == row.language
                    and override.subtitles_path == row.subtitles_path
                    and override.timestamp >= row.timestamp):
                pinned.add(row.id)
                break
    return pinned


def _find_current_subtitle_for_language(language_string, external_subtitles):
    """Find a current subtitle file on disk that matches the language from history.

    When a subtitle was re-downloaded or renamed (e.g. .hu.hi.srt -> .hu.srt),
    the history still references the old path. This finds the current file for
    the same language so upgrades can still proceed.
    """
    lang_code = language_string.split(':')[0]
    is_hi = language_string.endswith(':hi')
    is_forced = language_string.endswith(':forced')

    for sub_path in external_subtitles:
        sub_lower = sub_path.lower()
        # Check if language code is in the filename
        if f'.{lang_code.lower()}.' not in sub_lower and not sub_lower.endswith(f'.{lang_code.lower()}'):
            continue
        # Match HI/forced flags
        has_hi = '.hi.' in sub_lower or sub_lower.endswith('.hi.srt')
        has_forced = '.forced.' in sub_lower or sub_lower.endswith('.forced.srt')
        if is_hi == has_hi and is_forced == has_forced:
            return sub_path
        # If we wanted HI but only non-HI exists (or vice versa), still a candidate
        if not is_forced and not has_forced and lang_code.lower() in sub_lower:
            return sub_path
    return None


def parse_language_string(language_string):
    if language_string.endswith('forced'):
        language = language_string.split(':')[0]
        is_forced = "True"
        is_hi = "False"
    elif language_string.endswith('hi'):
        language = language_string.split(':')[0]
        is_forced = "False"
        is_hi = "True"
    else:
        language = language_string.split(':')[0]
        is_forced = "False"
        is_hi = "False"

    return [language, is_forced, is_hi]


def _ai_translated_subtitles_are_upgrade_candidates():
    """Only enable the score-independent AI candidate rule for a valid penalty."""
    penalty = getattr(settings.general, 'ai_translated_score_penalty', 0)
    return type(penalty) is int and 0 < penalty <= 100


def get_upgradable_episode_subtitles(history_id_list=None):
    if not settings.general.upgrade_subs:
        # return an empty set of rows
        logging.debug("Subtitles upgrade is disabled so we wont go further.")
        return {}

    logging.debug("Determining upgradable episode subtitles")
    minimum_timestamp, query_actions = get_queries_condition_parameters()
    # Group the latest-timestamp pick by arr_instance_id too: in multi-instance
    # the same video_path can be shared across instances (shared library), and
    # an unscoped (video_path, language) grouping would let one instance's
    # newer row shadow another instance's distinct upgrade candidate. NULL owner
    # (legacy single-instance) groups together exactly as before.
    max_id_timestamp = select(TableHistory.video_path,
                              TableHistory.language,
                              TableHistory.arr_instance_id,
                              func.max(TableHistory.timestamp).label('timestamp')) \
        .where(TableHistory.action.in_(latest_row_actions(query_actions))) \
        .group_by(TableHistory.video_path, TableHistory.language,
                  TableHistory.arr_instance_id) \
        .distinct() \
        .subquery()

    logging.debug(f"Minimum timestamp used for subtitles upgrade: {minimum_timestamp}")  # noqa: G004
    logging.debug(f"These actions are considered for subtitles upgrade: {query_actions}")  # noqa: G004

    upgradable_episodes_conditions = [(TableHistory.action.in_(query_actions)),
                                      (TableHistory.timestamp > minimum_timestamp),
                                      ~newer_embedded_source(TableHistory),
                                      ]
    episode_score_conditions = [and_(TableHistory.score.is_(None), TableHistory.action == 6),
                                (TableHistory.score < TableHistory.score_out_of - 3)]
    if _ai_translated_subtitles_are_upgrade_candidates():
        episode_score_conditions.append(TableHistory.ai_translated.is_(True))
    upgradable_episodes_conditions.append(or_(*episode_score_conditions))
    upgradable_episodes_conditions += get_exclusion_clause('series')
    subtitles_to_upgrade = database.execute(
        select(TableHistory.id,
               TableHistory.action,
               TableHistory.video_path,
               TableHistory.language,
               TableHistory.arr_instance_id,
               TableHistory.upgradedFromId,
               TableHistory.subtitles_path,
               TableHistory.timestamp)
        .select_from(TableHistory)
        .join(TableShows, onclause=and_(TableHistory.sonarrSeriesId == TableShows.sonarrSeriesId,
                                  TableHistory.arr_instance_id == TableShows.arr_instance_id))
        .join(TableEpisodes, onclause=and_(TableHistory.sonarrEpisodeId == TableEpisodes.sonarrEpisodeId,
                                     TableHistory.arr_instance_id == TableEpisodes.arr_instance_id))
        .join(max_id_timestamp, onclause=and_(
            TableHistory.video_path == max_id_timestamp.c.video_path,
            TableHistory.language == max_id_timestamp.c.language,
            TableHistory.arr_instance_id.is_not_distinct_from(max_id_timestamp.c.arr_instance_id),
            max_id_timestamp.c.timestamp == TableHistory.timestamp))
        .where(reduce(operator.and_, upgradable_episodes_conditions))
        .order_by(TableHistory.timestamp.desc())) \
        .all()
    logging.debug(f"{len(subtitles_to_upgrade)} subtitles are candidates and we've selected the latest timestamp for "  # noqa: G004
                  f"each of them.")

    retired_translated_ids = _retired_translated_rows(
        TableHistory, subtitles_to_upgrade, _retirement_evidence_actions(query_actions), minimum_timestamp)
    pinned_ids = _manually_pinned_rows(TableHistory, subtitles_to_upgrade)

    upgradable_episode_subtitles = {}
    # The history page passes every id of one show. Membership against the
    # list walked every candidate row past thousands of ids; a set does not.
    history_id_set = set(history_id_list) if history_id_list else None
    for subtitle_to_upgrade in subtitles_to_upgrade:
        # exclude subtitles that are not in history_id_list if provided
        if history_id_set is not None and subtitle_to_upgrade.id not in history_id_set:
            continue

        # exclude a translated row a real subtitle has already replaced, so the
        # upgrade keeps running against that subtitle instead of the translation
        if subtitle_to_upgrade.id in retired_translated_ids:
            logging.debug(f"TableHistory ID {subtitle_to_upgrade.id} is a translation a real subtitle has already "  # noqa: G004
                          f"replaced for language {subtitle_to_upgrade.language} so we'll skip it.")
            continue

        # exclude a subtitle whose score the user set by hand
        if subtitle_to_upgrade.id in pinned_ids:
            logging.debug(f"TableHistory ID {subtitle_to_upgrade.id} has a manual score so we'll skip it.")  # noqa: G004
            continue

        # exclude subtitles with ID that as been "upgraded from" and shouldn't be considered
        if any(x.upgradedFromId == subtitle_to_upgrade.id for x in subtitles_to_upgrade):
            logging.debug(f"TableHistory ID {subtitle_to_upgrade.id} is the original subtitles event and has already "  # noqa: G004
                          f"been upgraded so we'll skip it.")
            continue

        # check if we have the original subtitles ID in database
        if subtitle_to_upgrade.upgradedFromId:
            upgradable_episode_subtitles.update({subtitle_to_upgrade.id: subtitle_to_upgrade.upgradedFromId})
            logging.debug(f"The original subtitles ID for TableHistory ID {subtitle_to_upgrade.id} stored in DB is: "  # noqa: G004
                          f"{subtitle_to_upgrade.upgradedFromId}")
            continue
        else:
            upgradable_episode_subtitles.update({subtitle_to_upgrade.id: None})
            logging.debug(f"It seems to be the first time we've seen this TableHistory ID {subtitle_to_upgrade.id}.")  # noqa: G004
            continue

    logging.debug(f"We've found {len(upgradable_episode_subtitles)} episode subtitles IDs to be upgradable")  # noqa: G004
    return upgradable_episode_subtitles


def get_upgradable_movies_subtitles(history_id_list=None):
    if not settings.general.upgrade_subs:
        # return an empty set of rows
        logging.debug("Subtitles upgrade is disabled so we won't go further.")
        return {}

    logging.debug("Determining upgradable movie subtitles")
    minimum_timestamp, query_actions = get_queries_condition_parameters()
    # Group by arr_instance_id too (see the episode path): a shared library
    # video_path under two instances must keep each instance's distinct latest
    # candidate. NULL owner (legacy) groups exactly as before.
    max_id_timestamp = select(TableHistoryMovie.video_path,
                              TableHistoryMovie.language,
                              TableHistoryMovie.arr_instance_id,
                              func.max(TableHistoryMovie.timestamp).label('timestamp')) \
        .where(TableHistoryMovie.action.in_(latest_row_actions(query_actions))) \
        .group_by(TableHistoryMovie.video_path, TableHistoryMovie.language,
                  TableHistoryMovie.arr_instance_id) \
        .distinct() \
        .subquery()

    logging.debug(f"Minimum timestamp used for subtitles upgrade: {minimum_timestamp}")  # noqa: G004
    logging.debug(f"These actions are considered for subtitles upgrade: {query_actions}")  # noqa: G004

    upgradable_movies_conditions = [(TableHistoryMovie.action.in_(query_actions)),
                                    (TableHistoryMovie.timestamp > minimum_timestamp),
                                    ~newer_embedded_source(TableHistoryMovie),
                                    ]
    movie_score_conditions = [and_(TableHistoryMovie.score.is_(None), TableHistoryMovie.action == 6),
                              (TableHistoryMovie.score < TableHistoryMovie.score_out_of - 3)]
    if _ai_translated_subtitles_are_upgrade_candidates():
        movie_score_conditions.append(TableHistoryMovie.ai_translated.is_(True))
    upgradable_movies_conditions.append(or_(*movie_score_conditions))
    upgradable_movies_conditions += get_exclusion_clause('movie')
    subtitles_to_upgrade = database.execute(
        select(TableHistoryMovie.id,
               TableHistoryMovie.action,
               TableHistoryMovie.video_path,
               TableHistoryMovie.language,
               TableHistoryMovie.arr_instance_id,
               TableHistoryMovie.upgradedFromId,
               TableHistoryMovie.subtitles_path,
               TableHistoryMovie.timestamp)
        .select_from(TableHistoryMovie)
        .join(TableMovies, onclause=and_(TableHistoryMovie.radarrId == TableMovies.radarrId,
                                  TableHistoryMovie.arr_instance_id == TableMovies.arr_instance_id))
        .join(max_id_timestamp, onclause=and_(
            TableHistoryMovie.video_path == max_id_timestamp.c.video_path,
            TableHistoryMovie.language == max_id_timestamp.c.language,
            TableHistoryMovie.arr_instance_id.is_not_distinct_from(max_id_timestamp.c.arr_instance_id),
            max_id_timestamp.c.timestamp == TableHistoryMovie.timestamp))
        .where(reduce(operator.and_, upgradable_movies_conditions))
        .order_by(TableHistoryMovie.timestamp.desc())) \
        .all()
    logging.debug(f"{len(subtitles_to_upgrade)} subtitles are candidates and we've selected the latest timestamp for "  # noqa: G004
                  f"each of them.")

    retired_translated_ids = _retired_translated_rows(
        TableHistoryMovie, subtitles_to_upgrade, _retirement_evidence_actions(query_actions), minimum_timestamp)
    pinned_ids = _manually_pinned_rows(TableHistoryMovie, subtitles_to_upgrade)

    upgradable_movie_subtitles = {}
    # The history page passes every id of one movie. Membership against the
    # list walked every candidate row past thousands of ids; a set does not.
    history_id_set = set(history_id_list) if history_id_list else None
    for subtitle_to_upgrade in subtitles_to_upgrade:
        # exclude subtitles that are not in history_id_list if provided
        if history_id_set is not None and subtitle_to_upgrade.id not in history_id_set:
            continue

        # exclude a translated row a real subtitle has already replaced, so the
        # upgrade keeps running against that subtitle instead of the translation
        if subtitle_to_upgrade.id in retired_translated_ids:
            logging.debug(f"TableHistoryMovie ID {subtitle_to_upgrade.id} is a translation a real subtitle has "  # noqa: G004
                          f"already replaced for language {subtitle_to_upgrade.language} so we'll skip it.")
            continue

        # exclude a subtitle whose score the user set by hand
        if subtitle_to_upgrade.id in pinned_ids:
            logging.debug(f"TableHistoryMovie ID {subtitle_to_upgrade.id} has a manual score so we'll skip it.")  # noqa: G004
            continue

        # exclude subtitles with ID that as been "upgraded from" and shouldn't be considered
        if any(x.upgradedFromId == subtitle_to_upgrade.id for x in subtitles_to_upgrade):
            logging.debug(f"TableHistoryMovie ID {subtitle_to_upgrade.id} is the original subtitles event and has "  # noqa: G004
                          f"already been upgraded so we'll skip it.")
            continue

        # check if we have the original subtitles ID in database
        if subtitle_to_upgrade.upgradedFromId:
            upgradable_movie_subtitles.update({subtitle_to_upgrade.id: subtitle_to_upgrade.upgradedFromId})
            logging.debug(f"The original subtitles ID for TableHistoryMovie ID {subtitle_to_upgrade.id} stored in DB "  # noqa: G004
                          f"is: {subtitle_to_upgrade.upgradedFromId}")
            continue
        else:
            upgradable_movie_subtitles.update({subtitle_to_upgrade.id: None})
            logging.debug(f"It seems to be the first time we've seen this TableHistoryMovie ID "  # noqa: G004
                          f"{subtitle_to_upgrade.id}.")
            continue

    logging.debug(f"We've found {len(upgradable_movie_subtitles)} movie subtitles IDs to be upgradable")  # noqa: G004
    return upgradable_movie_subtitles


def _language_still_desired(language, profile_id):
    if not profile_id:
        return False

    profile = get_profiles_list(profile_id)
    if profile and language in _language_from_items(profile['items']):
        return True
    else:
        return False


def _language_from_items(items):
    results = []
    for item in items:
        if item['forced'] == 'True':
            results.append(f'{item["language"]}:forced')
        elif item['hi'] == 'True':
            results.append(f'{item["language"]}:hi')
        else:
            results.append(item['language'])
            results.append(f'{item["language"]}:hi')
    return results


def _is_hi_required(language, profile_id):
    profile = get_profiles_list(profile_id=profile_id)
    for item in profile['items']:
        if language.split(':')[0] == item['language'] and item['hi'] == 'True':
            return True
    return False

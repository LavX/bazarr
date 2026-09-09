"""Automatic provider selection for an immutable, owned sports file."""

import ast
import logging
import os
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread

from sqlalchemy import select
from subliminal_patch.core_persistent import download_best_subtitles

from app.config import settings
from app.jobs_queue import JobCancelled
from app.database import (
    database,
    get_audio_profile_languages,
    get_profiles_list,
    TableSportsEvents,
    TableSportsLeagues,
)
from sportarr.connection import check_cancelled
from sportarr.identity import resolve_event_in_session
from sportarr.settings import get_sports_settings
from sportarr.subtitles import (
    bind_candidate,
    candidate_signature,
    save_sports_subtitle,
    validate_context,
)
from sportarr.sync.leagues import require_sportarr
from subtitles.adaptive_searching import is_search_active, updateFailedAttempts
from subtitles.download import _get_language_obj
from subtitles.indexer.sports import _missing, store_subtitles_sports
from subtitles.pool import _init_pool
from subtitles.utils import get_video, _get_scores

_provider_slots = BoundedSemaphore(4)


def eligibility(session, context):
    instance = validate_context(context, session)
    event = session.get(TableSportsEvents, context.event_id, populate_existing=True)
    league = session.get(TableSportsLeagues, context.league_id, populate_existing=True)
    options = get_sports_settings(instance)
    if options["only_monitored"] and (
        event.monitored != "True" or league.monitored != "True"
    ):
        return "Event or league is not monitored"
    if set(str(tag) for tag in ast.literal_eval(league.tags or "[]")) & set(
        options["excluded_tags"]
    ):
        return "League has an excluded tag"
    if league.sport in options["excluded_sports"]:
        return "Sport is excluded"
    if not context.profile_id or not get_profiles_list(context.profile_id):
        return "No language profile is assigned"
    return None


def _provider_result(video, languages, pool, minimum, profile, cancel):
    # A private pool keeps a cancelled search from changing a later search's state.
    # Set here for the same reason subtitles/download.py:38 and manual.py:165
    # set it: subliminal reads it out of the environment at download time. The
    # sports automatic path never did, so encoding was whatever a previous
    # non-sports search happened to leave behind.
    os.environ["SZ_KEEP_ENCODING"] = "" if settings.general.utf8_encode else "True"

    # Providers finish under their own timeout; abandoned results cannot publish.
    while not _provider_slots.acquire(timeout=0.1):
        check_cancelled(cancel)
    result = Queue(maxsize=1)

    def run():
        try:
            result.put(
                (
                    True,
                    download_best_subtitles(
                        videos={video},
                        languages=languages,
                        pool_instance=pool,
                        min_score=minimum,
                        hearing_impaired="force HI"
                        if all(language.hi for language in languages)
                        else "don't prefer",
                        use_original_format=profile["originalFormat"]
                        in (1, "1", True, "True"),
                        use_provider_priority=settings.general.use_provider_priority,
                        fallback_allowed=settings.general.use_whisper_fallback,
                    ),
                )
            )
        except Exception as exc:
            result.put((False, exc))
        finally:
            try:
                pool.terminate()
            finally:
                _provider_slots.release()

    Thread(target=run, name="sports-provider-search", daemon=True).start()
    while True:
        check_cancelled(cancel)
        try:
            success, value = result.get(timeout=0.1)
        except Empty:
            continue
        check_cancelled(cancel)
        if not success:
            raise OSError("Sports provider search failed; see the log") from value
        return value.get(video, [])


def search_event(
    event_id,
    arr_instance_id,
    *,
    language=None,
    minimum_score=None,
    upgraded_from_id=None,
    job_id=None,
    cancel=None,
    adaptive=False,
    previous_artifact=None,
    replacement_state=None,
):
    check_cancelled(cancel)
    context = resolve_event_in_session(database, event_id, arr_instance_id)
    reason = eligibility(database, context)
    if reason:
        return {"status": "skipped", "message": reason, "downloads": 0}
    # Capture before probing and searching. Never bind a late result to a fresh file.
    signature = candidate_signature(context)
    if replacement_state is not None and replacement_state.signature != signature:
        return {
            "status": "skipped",
            "message": "Replacement skipped because the sports file changed",
            "downloads": 0,
        }
    instance = require_sportarr(database, arr_instance_id)
    threshold = int(
        _get_scores("sports", get_sports_settings(instance)["minimum_score"])[0]
    )
    if minimum_score is not None:
        threshold = max(threshold, minimum_score)
    row = database.get(TableSportsEvents, event_id, populate_existing=True)
    profile = get_profiles_list(context.profile_id)
    if language is None:
        store_subtitles_sports(event_id, arr_instance_id, cancel=cancel)
        row = database.get(TableSportsEvents, event_id, populate_existing=True)
        languages = ast.literal_eval(row.missing_subtitles or "[]")
    else:
        # Upgrades and replacements still follow current profile audio exclusions.
        languages = (
            [language]
            if language in _missing(context.profile_id, [], row.audio_language, "[]")
            else []
        )
    if adaptive:
        languages = [
            code for code in languages if is_search_active(code, row.failedAttempts)
        ]
    if not languages:
        return {
            "status": "skipped",
            "message": "No eligible missing language",
            "downloads": 0,
        }
    downloads = 0
    for code in languages:
        check_cancelled(cancel)
        if candidate_signature(context) != signature:
            raise ValueError("Sports file changed before automatic search")
        reason = eligibility(database, context)
        if reason:
            return {"status": "skipped", "message": reason, "downloads": downloads}
        # Re-check cutoff after each publication, including another job's indexing.
        if language is None:
            row = database.get(TableSportsEvents, event_id, populate_existing=True)
            if code not in _missing(
                context.profile_id,
                ast.literal_eval(row.subtitles or "[]"),
                row.audio_language,
                row.failedAttempts,
            ):
                continue
        # Translate before searching, the way the series and movies wanted
        # scans do. Sports only ever translated from a fresh download, so an
        # event whose source subtitle came off disk never got its translation
        # and was re-searched by every wanted scan forever.
        if language is None:
            from sportarr.profile_hooks import translate_from_existing

            try:
                if translate_from_existing(context, code, cancel=cancel):
                    downloads += 1
                    continue
            except JobCancelled:
                raise
            except Exception:
                logging.exception(
                    "BAZARR sports auto-translate failed for %s, falling back to "
                    "a provider search", code,
                )
        language_set = _get_language_obj(
            [
                (
                    code.split(":")[0],
                    str(code.endswith(":hi")),
                    str(code.endswith(":forced")),
                )
            ]
        )
        pool = _init_pool("sports", context.profile_id, context=context)
        if not pool.providers:
            pool.terminate()
            return {
                "status": "no_result",
                "message": "No providers are available",
                "downloads": downloads,
            }
        try:
            video = get_video(
                context.mapped_path,
                row.title,
                row.sceneName or "None",
                providers=pool.providers,
                media_type="sports",
                context=context,
                cancel=cancel,
            )
        except BaseException:
            pool.terminate()
            raise
        if not video:
            pool.terminate()
            raise OSError("Could not analyze sports video")
        selected = _provider_result(
            video, language_set, pool, threshold, profile, cancel
        )
        for subtitle in selected:
            candidate = bind_candidate(context, subtitle, signature)
            if eligibility(database, context):
                raise ValueError("Sports search eligibility changed before publication")
            audio = get_audio_profile_languages(row.audio_language)
            from sportarr.artifacts import ReplacementDestinationChanged

            try:
                saved = save_sports_subtitle(
                    video,
                    subtitle,
                    candidate,
                    audio[0]["name"] if audio else "None",
                    is_manual=False,
                    is_upgrade=minimum_score is not None,
                    upgraded_from_id=upgraded_from_id,
                    job_id=job_id,
                    cancel=cancel,
                    previous_artifact=previous_artifact,
                    replacement_state=replacement_state,
                )
            except ReplacementDestinationChanged as exc:
                return {
                    "status": "skipped",
                    "message": str(exc),
                    "downloads": downloads,
                }
            downloads += int(saved.publication["published"])
            if saved.publication["status"] == "published_with_warnings":
                return {
                    "status": "published_with_warnings",
                    "message": saved.publication["message"],
                    "downloads": downloads,
                    "publication": saved.publication,
                    "cancelled": saved.publication["cancelled"],
                }
        if not selected and adaptive:
            from sportarr.db import sports_transaction

            with sports_transaction(database) as session:
                validate_context(context, session)
                if candidate_signature(context, session) != signature:
                    raise ValueError("Sports file changed during automatic search")
                current = session.execute(
                    select(TableSportsEvents)
                    .where(
                        TableSportsEvents.id == event_id,
                        TableSportsEvents.arr_instance_id == arr_instance_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one()
                check_cancelled(cancel)
                current.failedAttempts = updateFailedAttempts(
                    code, current.failedAttempts
                )
                session.flush()
    message = (
        f"Downloaded {downloads} sports subtitle(s)"
        if downloads
        else "No eligible subtitle met the configured threshold"
    )
    logging.info("%s for sports event %s, owner %s", message, event_id, arr_instance_id)
    return {
        "status": "downloaded" if downloads else "no_result",
        "message": message,
        "downloads": downloads,
    }

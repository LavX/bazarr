"""Sports subtitle operations bound to an exact local file and its owner."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import os
import logging

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from subzero.language import Language
from subliminal_patch.score import MAX_SCORES

from app.database import (
    database,
    get_audio_profile_languages,
    get_profiles_list,
    TableBlacklistSports,
    TableHistorySports,
    TableSportsEvents,
)
from app.get_providers import get_providers
from sportarr.connection import check_cancelled
from sportarr.db import SportsTransactionOutcome, sports_transaction
from sportarr.identity import SportsEventContext, resolve_event_in_session
from sportarr.output import (
    SportsOutputNamespace,
    lock_output_owners,
    output_policy,
    validate_output_path,
)
from sportarr.sync.leagues import require_sportarr
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings


@dataclass(frozen=True)
class SportsCandidate:
    context: SportsEventContext
    signature: tuple
    subtitle: object


def _signature(context, instance):
    stat = os.stat(context.mapped_path)
    return (
        context,
        instance.path_mappings,
        os.path.realpath(context.mapped_path),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        output_policy(context),
    )


def validate_context(context, session=None):
    session = database if session is None else session
    if not isinstance(context, SportsEventContext):
        raise ValueError("An explicit sports event context is required")
    current = resolve_event_in_session(
        session, context.event_id, context.arr_instance_id
    )
    if current != context:
        raise ValueError("Sports event changed. Please search again.")
    return require_sportarr(session, context.arr_instance_id)


def bind_candidate(context, subtitle, signature=None):
    instance = validate_context(context)
    current = _signature(context, instance)
    if signature is not None and signature != current:
        raise ValueError("Sports file changed. Please search again.")
    return SportsCandidate(context, current, deepcopy(subtitle))


def candidate_signature(context, session=None):
    return _signature(context, validate_context(context, session))


def validate_candidate(candidate, context, session=None, cancel=None):
    check_cancelled(cancel)
    if not isinstance(candidate, SportsCandidate) or candidate.context != context:
        raise ValueError(
            "Subtitle result belongs to a different sports event. Please search again."
        )
    instance = validate_context(context, session)
    try:
        signature = _signature(context, instance)
    except OSError as exc:
        raise ValueError("Sports file is unavailable. Please search again.") from exc
    if signature != candidate.signature:
        raise ValueError("Sports file changed. Please search again.")
    if (
        candidate.subtitle.provider_name,
        str(candidate.subtitle.id),
    ) in get_blacklist_sports(context, session):
        raise ValueError("Subtitle has been blacklisted. Please search again.")
    check_cancelled(cancel)
    return instance


def get_blacklist_sports(context, session=None):
    session = database if session is None else session
    validate_context(context, session)
    return [
        (row.provider, str(row.subs_id))
        for row in session.execute(
            select(TableBlacklistSports.provider, TableBlacklistSports.subs_id).where(
                TableBlacklistSports.arr_instance_id == context.arr_instance_id
            )
        ).all()
    ]


@contextmanager
def sports_file_publication(
    context, signature, cancel=None, extra_validate=None, *, outcome=None
):
    """Short owned publication boundary for provider and nonprovider file work."""
    namespace = SportsOutputNamespace(context, database)
    try:
        with sports_transaction(database, nowait=True, outcome=outcome) as session:
            lock_output_owners(session, context.arr_instance_id)

            def validate():
                check_cancelled(cancel)
                instance = validate_context(context, session)
                if _signature(context, instance) != signature:
                    raise ValueError("Sports file changed. Please try again.")
                if extra_validate is not None:
                    extra_validate(session)
                check_cancelled(cancel)
                return instance

            validate()
            session.execute(
                select(TableSportsEvents)
                .where(
                    TableSportsEvents.id == context.event_id,
                    TableSportsEvents.arr_instance_id == context.arr_instance_id,
                )
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            ).scalar_one()
            validate()
            namespace.validate(session)
            yield session, validate
            session.flush()
            validate()
            namespace.validate(session)
    except OperationalError as exc:
        raise ValueError("Subtitle destination owners are busy. Please retry.") from exc


@contextmanager
def sports_publication(candidate, cancel=None, *, outcome=None):
    with sports_file_publication(
        candidate.context,
        candidate.signature,
        cancel,
        lambda session: validate_candidate(
            candidate, candidate.context, session, cancel
        ),
        outcome=outcome,
    ) as publication:
        yield publication


def sports_history(
    session, context, result, action=2, upgraded_from_id=None, artifact=None
):
    validate_context(context, session)
    row = TableHistorySports(
        event_id=context.event_id,
        league_id=context.league_id,
        arr_instance_id=context.arr_instance_id,
        timestamp=datetime.now(),
        action=action,
        description=result.message,
        video_path=result.path,
        language=result.language_code,
        provider=result.provider,
        score=result.score,
        score_out_of=MAX_SCORES["movie"],
        subs_id=result.subs_id,
        subtitles_path=result.subs_path,
        matched=str(result.matched),
        not_matched=str(result.not_matched),
        upgradedFromId=upgraded_from_id,
        artifact=artifact,
    )
    session.add(row)
    return row


def save_sports_subtitle(
    video,
    subtitle,
    candidate,
    audio_language,
    *,
    is_manual=True,
    is_upgrade=False,
    upgraded_from_id=None,
    job_id=None,
    cancel=None,
    previous_artifact=None,
    replacement_state=None,
):
    """Reusable save entrypoint for manual and automatic sports provider downloads."""
    from app.notifier import send_notifications_sports
    from subtitles.manual import _save_downloaded_subtitles
    from subtitles.processing import process_subtitle
    from subtitles.tools.mods import get_subzero_mods
    from subtitles.tools.subsync_engines import subtitle_write_locks
    from utilities.helper import get_target_folder

    context = candidate.context
    path = context.mapped_path
    if (
        video.original_path != path
        or subtitle.provider_name != candidate.subtitle.provider_name
        or subtitle.id != candidate.subtitle.id
    ):
        raise ValueError("Sports save inputs do not match the bound candidate")
    destination = os.path.join(
        get_target_folder(path) or os.path.dirname(path), ".destination"
    )

    pending_replacement = replacement_state is not None
    publication_destination = None

    def validate(destination=None):
        nonlocal publication_destination
        if destination is not None:
            validate_output_path(context, destination)
            publication_destination = destination
        return validate_candidate(candidate, context, cancel=cancel)

    @contextmanager
    def publication_guard():
        nonlocal pending_replacement
        with sports_publication(candidate, cancel) as publication:
            if pending_replacement:
                from sportarr.artifacts import validate_replacement_state

                validate_replacement_state(replacement_state, publication_destination)
            yield publication
        pending_replacement = False

    with subtitle_write_locks(path, destination, cancel=cancel):
        validate()
        if pending_replacement:
            from sportarr.artifacts import validate_replacement_state

            validate_replacement_state(replacement_state)
        if previous_artifact is not None:
            from sportarr.artifacts import capture_artifact

            previous_path, previous_proof = previous_artifact
            if (
                capture_artifact(context, previous_path, candidate.signature, cancel)
                != previous_proof
            ):
                raise ValueError(
                    "Sports subtitle artifact changed during upgrade search"
                )
        subtitle.mods = get_subzero_mods(context.arr_instance_id)
        from sportarr.publication import SportsSaveResult, publication_cancelled
        from sportarr.artifacts import capture_artifact, validate_artifact_stat
        from sportarr.profile_hooks import capture_profile_operation, trigger_saved

        outcome = SportsSaveResult()
        state = outcome.publication
        written_paths = []
        phase = "publication"
        history_outcome = SportsTransactionOutcome()
        history_started = False
        try:
            saved = _save_downloaded_subtitles(
                video,
                subtitle,
                path,
                validate=validate,
                publication_guard=publication_guard,
                written_paths=written_paths,
            )
            if not saved:
                raise OSError("Could not save sports subtitles")
            state["published"] = True
            phase = "processing"
            state[phase] = "running"
            result = process_subtitle(
                saved[0],
                "sports",
                audio_language,
                path,
                MAX_SCORES["movie"],
                is_manual=is_manual,
                is_upgrade=is_upgrade,
                job_id=job_id,
                context=context,
                validate=validate,
                cancel=cancel,
                publication_guard=publication_guard,
            )
            if isinstance(result, tuple):
                result = result[0]
            if not result or not os.path.isfile(saved[0].storage_path):
                raise OSError("Sports subtitle processing did not leave a saved file")
            outcome.result = result
            state[phase] = "completed"
            phase = "artifact"
            artifact = capture_artifact(
                context, saved[0].storage_path, candidate.signature, cancel
            )
            state[phase] = "completed"
            phase = "history"
            state[phase] = "running"
            with sports_publication(candidate, cancel, outcome=history_outcome) as (
                session,
                check,
            ):
                history_started = True
                validate_artifact_stat(artifact, saved[0].storage_path)
                sports_history(
                    session,
                    context,
                    result,
                    action=3 if is_upgrade else 2 if is_manual else 1,
                    upgraded_from_id=upgraded_from_id,
                    artifact=artifact,
                )
            state[phase] = "committed"
            # Series and movies notify as soon as the download is recorded;
            # sports never did, so with Apprise configured every sports
            # download, upgrade and translate was invisible. Guarded because a
            # notifier fault must not push this state machine into the
            # "published but history uncertain" branch below.
            try:
                send_notifications_sports(
                    context.event_id, result.message,
                    arr_instance_id=context.arr_instance_id,
                )
            except Exception:
                logging.exception("BAZARR could not send a sports notification")
            phase = "index"
            outcome.refresh(candidate, database, cancel)
            if state["index"] != "completed":
                state["failed_phase"] = phase
                if state["index"] == "failed":
                    outcome.refresh(candidate, database, cancel)
                return outcome.finish()
            phase = "profile processing"
            profile_operation = capture_profile_operation(
                context, candidate.signature, saved[0].storage_path, artifact, cancel
            )
        except Exception:
            if not written_paths:
                raise
            state["published"] = True
            state["failed_phase"] = phase
            state["cancelled"] = publication_cancelled(cancel)
            if phase == "history":
                state[phase] = (
                    "failed"
                    if not history_started or history_outcome.rollback_confirmed
                    else "uncertain"
                )
            elif phase in ("processing", "artifact"):
                state[phase] = "cancelled" if state["cancelled"] else "failed"
            logging.exception(
                "Sports subtitle published, but %s did not complete", phase
            )
            outcome.refresh(candidate, database, cancel)
            return outcome.finish()
    try:
        trigger_saved(
            profile_operation,
            saved[0].storage_path,
            result.language_code.split(":")[0],
            result.score * 100 / MAX_SCORES["movie"],
            subtitle.language.forced,
            cancel,
        )
    except Exception:
        state["failed_phase"] = "profile processing"
        state["cancelled"] = publication_cancelled(cancel)
        logging.exception(
            "Sports subtitle published, but profile processing did not complete"
        )
    return outcome.finish()


def manual_search_sports(
    event_id, language, hi=False, forced=False, arr_instance_id=None, *, cancel=None
):
    from languages.get_languages import alpha3_from_alpha2
    from subtitles.manual import manual_search
    from subtitles.utils import _get_lang_obj

    check_cancelled(cancel)
    context = resolve_event_in_session(database, event_id, arr_instance_id)
    row = database.get(TableSportsEvents, event_id, populate_existing=True)
    code = (
        alpha3_from_alpha2(language)
        if isinstance(language, str) and len(language) == 2
        else language
    )
    if (
        not isinstance(code, str)
        or len(code) != 3
        or type(hi) is not bool
        or type(forced) is not bool
    ):
        raise ValueError("A valid subtitle language and boolean variants are required")
    try:
        requested = Language.rebuild(_get_lang_obj(code), hi=hi, forced=forced)
    except (ValueError, KeyError) as exc:
        raise ValueError("Unknown subtitle language") from exc
    profile = get_profiles_list(context.profile_id) if context.profile_id else None
    result = manual_search(
        context.mapped_path,
        context.profile_id,
        get_providers(),
        row.sceneName or "None",
        row.title,
        "sports",
        context=context,
        language_set={requested},
        original_format=bool(
            profile and profile["originalFormat"] in (1, "1", True, "True")
        ),
        cancel=cancel,
    )
    check_cancelled(cancel)
    if isinstance(result, str):
        raise OSError(result)
    return result


def manual_download_sports(event_id, candidate, arr_instance_id=None, *, cancel=None):
    from subtitles.cache import subtitle_cache
    from subtitles.manual import manual_download_subtitle

    context = resolve_event_in_session(database, event_id, arr_instance_id)
    if not isinstance(candidate, dict) or not isinstance(
        candidate.get("subtitle"), str
    ):
        raise ValueError("A cached subtitle result is required")
    cached = subtitle_cache.get(candidate["subtitle"])
    validate_candidate(cached, context, cancel=cancel)
    if candidate.get("provider") != cached.subtitle.provider_name:
        raise ValueError("Subtitle provider does not match its cached result")
    row = database.get(TableSportsEvents, event_id, populate_existing=True)
    languages = get_audio_profile_languages(row.audio_language)
    result = manual_download_subtitle(
        context.mapped_path,
        languages[0]["name"] if languages else "None",
        str(cached.subtitle.language.hi),
        str(cached.subtitle.language.forced),
        candidate["subtitle"],
        cached.subtitle.provider_name,
        row.sceneName or "None",
        row.title,
        "sports",
        cached.subtitle.use_original_format,
        context.profile_id,
        arr_instance_id=context.arr_instance_id,
        context=context,
        cancel=cancel,
    )
    if isinstance(result, str) or not result:
        raise OSError(result or "Could not download sports subtitle")
    return result


def reverse_path(context, path, session=None):
    instance = validate_context(context, session)
    return apply_sports_mapping(
        path, read_sports_mappings(instance.path_mappings), reverse=True
    )

"""Immutable profile operations and nonprovider sports publication."""

import ast
from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
import logging
import os
import stat

from sqlalchemy import select

from app.config import settings
from app.database import (
    database,
    TableLanguagesProfiles,
    TableSportsEvents,
    TableSportsLeagues,
)
from app.jobs_queue import JobCancelled
from sportarr.connection import check_cancelled
from sportarr.identity import SportsEventContext
from sportarr.output import validate_output_path
from sportarr.subtitles import (
    candidate_signature,
    sports_file_publication,
    sports_history,
)
from subtitles.language_profiles import profile_item_language_code
from utilities.path_mappings import apply_sports_mapping, read_sports_mappings
from subtitles.tools.subsync_engines import (
    capture_subtitle_versions,
    subtitle_write_locks,
    validate_subtitle_versions,
)


def _profile(context, session, lock=False):
    query = select(TableLanguagesProfiles).where(
        TableLanguagesProfiles.profileId == context.profile_id
    )
    if lock:
        # Assignment first, then contents, in the existing owner publication boundary.
        session.execute(
            select(TableSportsLeagues.id)
            .where(
                TableSportsLeagues.id == context.league_id,
                TableSportsLeagues.arr_instance_id == context.arr_instance_id,
            )
            .with_for_update(nowait=True)
        ).scalar_one()
        query = query.with_for_update(nowait=True)
    row = session.execute(
        query.execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        return None
    return json.dumps(
        {
            column.name: getattr(row, column.name)
            for column in TableLanguagesProfiles.__table__.columns
        },
        sort_keys=True,
    )


def _profile_value(snapshot):
    if snapshot is None:
        return None
    profile = json.loads(snapshot)
    profile["items"] = json.loads(profile["items"])
    profile["combine"] = json.loads(profile["combine"]) if profile["combine"] else None
    return profile


def _source_path(context, path):
    from utilities.helper import get_target_folder

    roots = {
        os.path.realpath(os.path.dirname(context.mapped_path)),
        os.path.realpath(
            get_target_folder(context.mapped_path, create=False)
            or os.path.dirname(context.mapped_path)
        ),
    }
    stem = os.path.splitext(os.path.basename(context.mapped_path))[0].lower()
    if (
        os.path.islink(path)
        or os.path.realpath(os.path.dirname(path)) not in roots
        or not os.path.basename(path).lower().startswith(stem + ".")
        or not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
    ):
        raise ValueError("Sports subtitle source does not belong to its event")


@dataclass(frozen=True)
class SportsProfileOperation:
    context: SportsEventContext
    signature: tuple
    profile_snapshot: str | None
    anchors: tuple = ()
    sources: tuple = ()
    destination: str | None = None
    versions: tuple = ()
    target: str | None = None
    source_language: str | None = None
    source_score: float | None = None

    @property
    def profile(self):
        return _profile_value(self.profile_snapshot)

    def validate(self, session=None, *, lock=False, wanted=False, cancel=None):
        session = database if session is None else session
        check_cancelled(cancel)
        if self.profile_snapshot != _profile(self.context, session, lock):
            raise ValueError("Sports language profile rules changed")
        if candidate_signature(self.context, session) != self.signature:
            raise ValueError("Sports media changed during profile processing")
        validate_subtitle_versions(self.anchors)
        validate_subtitle_versions(
            tuple(version for version in self.versions if version.path in self.sources)
        )
        for path in self.sources:
            _source_path(self.context, path)
        if self.destination is not None:
            validate_output_path(self.context, self.destination)
            if os.path.islink(self.destination):
                raise ValueError("Sports subtitle destination is a symlink")
        if self.target:
            if (
                self.source_score is not None
                and self.source_score < settings.translator.min_source_score
            ):
                raise ValueError(
                    "Sports translation source is below the score threshold"
                )
            if (wanted or lock) and self.target not in missing_languages(
                self.context, session
            ):
                raise ValueError("Sports translation target is already satisfied")
        check_cancelled(cancel)

    def bind(
        self,
        sources,
        destination,
        *,
        target=None,
        source_language=None,
        source_score=None,
        cancel=None,
        extra_destinations=(),
    ):
        self.validate(cancel=cancel)
        for path in sources:
            _source_path(self.context, path)
        validate_output_path(self.context, destination)
        for path in extra_destinations:
            validate_output_path(self.context, path)
        versions = capture_subtitle_versions(
            self.context.mapped_path,
            (*sources, destination, *extra_destinations),
            cancel,
        )
        return replace(
            self,
            sources=tuple(sources),
            destination=destination,
            versions=versions,
            target=target,
            source_language=source_language,
            source_score=source_score,
        )

    @contextmanager
    def publication(self, cancel=None):
        with sports_file_publication(
            self.context,
            self.signature,
            cancel,
            lambda session: self.validate(session, lock=True, cancel=cancel),
        ) as guard:
            yield guard


def capture_profile_operation(
    context, signature, source=None, artifact=None, cancel=None
):
    from sportarr.artifacts import validate_artifact_stat

    anchors = ()
    if source is not None:
        with subtitle_write_locks(context.mapped_path, source, cancel=cancel):
            _source_path(context, source)
            if artifact is not None:
                validate_artifact_stat(artifact, source)
            anchors = capture_subtitle_versions(context.mapped_path, (source,), cancel)
    operation = SportsProfileOperation(
        context, signature, _profile(context, database), anchors
    )
    operation.validate(cancel=cancel)
    return operation


def missing_languages(context, session=None):
    from subtitles.indexer.sports import _missing

    session = database if session is None else session
    row = session.execute(
        select(TableSportsEvents)
        .where(
            TableSportsEvents.id == context.event_id,
            TableSportsEvents.arr_instance_id == context.arr_instance_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one()
    return _missing(
        context.profile_id,
        ast.literal_eval(row.subtitles or "[]"),
        row.audio_language,
        row.failedAttempts,
        profile=_profile_value(_profile(context, session)),
    )


def translation_destination(video_path, language, forced, hi):
    from subliminal_patch.core import get_subtitle_path
    from subtitles.indexer.utils import get_subtitle_destination_path
    from subtitles.tools.translate.core.translator_utils import convert_language_codes

    lang, _ = convert_language_codes(language, forced, hi)
    filename = get_subtitle_path(
        video_path, language=lang, extension=".srt", forced_tag=forced, hi_tag=hi
    )
    return get_subtitle_destination_path(
        file=video_path, subtitle=os.path.basename(filename)
    )


def queue_translations(operation, source, downloaded_lang, score, forced, cancel=None):
    from subtitles.tools.translate.main import translate_subtitles_file

    if (
        forced
        or not operation.profile
        or (score is not None and score < settings.translator.min_source_score)
    ):
        return
    operation.validate(cancel=cancel)
    missing = set(missing_languages(operation.context))
    for item in operation.profile["items"]:
        target = profile_item_language_code(item)
        if (
            item.get("translate_from") != downloaded_lang
            or item["language"] == downloaded_lang
            or target not in missing
        ):
            continue
        hi, target_forced = item.get("hi") == "True", item.get("forced") == "True"
        destination = translation_destination(
            operation.context.mapped_path, item["language"], target_forced, hi
        )
        bound = operation.bind(
            (source,),
            destination,
            target=target,
            source_language=downloaded_lang,
            source_score=score,
            cancel=cancel,
        )
        translate_subtitles_file(
            video_path=operation.context.mapped_path,
            source_srt_file=source,
            from_lang=downloaded_lang,
            to_lang=item["language"],
            forced=target_forced,
            hi=hi,
            media_type="sports",
            sonarr_series_id=None,
            sonarr_episode_id=None,
            radarr_id=None,
            metadata=None,
            arr_instance_id=operation.context.arr_instance_id,
            sports_operation=bound,
        )


def translate_from_existing(context, target_code, cancel=None):
    """Translate a missing sports language from a subtitle already on disk.

    The series and movies wanted scans do this: when the profile says a
    language is translated from another and that source already exists, they
    translate instead of searching providers. Sports only ever translated from
    a FRESH download (queue_translations, via trigger_saved), so an event whose
    source subtitle was indexed from disk rather than downloaded by Bazarr
    never got its translation and stayed missing forever, re-searched by every
    subsequent wanted scan.

    Returns True when a translation was queued, in which case the caller must
    not fall through to a provider search for this language.
    """
    from subtitles.tools.translate.main import translate_subtitles_file
    from subtitles.wanted.utils import _find_existing_subtitle_path

    operation = capture_profile_operation(context, candidate_signature(context), cancel=cancel)
    if not operation.profile:
        return False

    item = next(
        (
            entry
            for entry in operation.profile["items"]
            if profile_item_language_code(entry) == target_code
            and entry.get("translate_from")
            and entry.get("translate_from") != entry["language"]
        ),
        None,
    )
    if item is None:
        return False

    source_lang = item["translate_from"]
    row = database.get(TableSportsEvents, context.event_id, populate_existing=True)
    source_srt = _find_existing_subtitle_path(
        row.subtitles,
        source_lang,
        path_replace_fn=lambda path: apply_sports_mapping(
            path, read_sports_mappings(_instance_mappings(context))
        ),
    )
    if not source_srt:
        return False

    if _source_score_below_threshold(context, source_lang):
        return False
    if _already_translated_on_disk(context, target_code):
        return False

    hi, forced = item.get("hi") == "True", item.get("forced") == "True"
    destination = translation_destination(
        context.mapped_path, item["language"], forced, hi
    )
    bound = operation.bind(
        (source_srt,),
        destination,
        target=target_code,
        source_language=source_lang,
        source_score=None,
        cancel=cancel,
    )
    translate_subtitles_file(
        video_path=context.mapped_path,
        source_srt_file=source_srt,
        from_lang=source_lang,
        to_lang=item["language"],
        forced=forced,
        hi=hi,
        media_type="sports",
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=None,
        metadata=None,
        arr_instance_id=context.arr_instance_id,
        sports_operation=bound,
    )
    return True


def _instance_mappings(context):
    from sportarr.sync.leagues import require_sportarr

    return require_sportarr(database, context.arr_instance_id).path_mappings


def _source_score_below_threshold(context, source_lang):
    """Mirror of the series guard: too poor a source makes a poor translation.

    No history row means the subtitle was placed by hand or predates history
    tracking. The series path treats that as exactly at threshold and proceeds
    rather than silently falling back to a provider search, so this does too.
    """
    from app.database import TableHistorySports

    record = database.execute(
        select(TableHistorySports.score)
        .where(TableHistorySports.sportsEventId == context.event_id)
        .where(TableHistorySports.arr_instance_id == context.arr_instance_id)
        .where(TableHistorySports.language.like(f"{source_lang}%"))
        .where(TableHistorySports.score.is_not(None))
        .order_by(TableHistorySports.timestamp.desc())
        .limit(1)
    ).first()
    if not record or not record.score:
        return False
    from subtitles.utils import MAX_SCORES

    pct = round((record.score / MAX_SCORES["movie"]) * 100, 1)
    return pct < settings.translator.min_source_score


def _already_translated_on_disk(context, target_code):
    """A completed translation blocks re-queuing only while its file survives.

    Checking the history row alone would suppress the replacement forever if
    the translated subtitle was later deleted or moved.
    """
    from app.database import TableHistorySports

    record = database.execute(
        select(TableHistorySports.subtitles_path)
        .where(TableHistorySports.sportsEventId == context.event_id)
        .where(TableHistorySports.arr_instance_id == context.arr_instance_id)
        .where(TableHistorySports.language == target_code)
        .where(TableHistorySports.action == 6)
        .order_by(TableHistorySports.timestamp.desc())
        .limit(1)
    ).first()
    if not record or not record.subtitles_path:
        return False
    local = apply_sports_mapping(
        record.subtitles_path, read_sports_mappings(_instance_mappings(context))
    )
    return bool(local and os.path.exists(local))


def sports_write_kwargs(translator, job_id):
    operation = translator.sports_operation
    if operation is None:
        return {}
    operation.validate(wanted=True, cancel=translator.cancel)
    target = profile_item_language_code(
        dict(
            language=translator.orig_to_lang,
            forced=str(translator.forced),
            hi=str(translator.hi),
        )
    )
    if (
        translator.video_path != operation.context.mapped_path
        or translator.source_srt_file not in operation.sources
        or translator.dest_srt_file != operation.destination
        or translator.media_type != "sports"
        or translator.arr_instance_id != operation.context.arr_instance_id
        or translator.from_lang != operation.source_language
        or target != operation.target
        or any(
            value is not None
            for value in (
                translator.radarr_id,
                translator.sonarr_series_id,
                translator.sonarr_episode_id,
            )
        )
    ):
        raise ValueError("Translation inputs do not match their sports operation")
    return dict(
        publication_guard=lambda: operation.publication(translator.cancel),
        expected_versions=operation.versions,
        cancel=translator.cancel,
        after_write=lambda: setattr(
            translator,
            "published_versions",
            capture_subtitle_versions(
                translator.video_path, (translator.dest_srt_file,), translator.cancel
            ),
        ),
    )


def finalize(operation, path, result=None, cancel=None, published_versions=()):
    from sportarr.artifacts import capture_artifact, validate_artifact_stat
    from subtitles.indexer.sports import store_subtitles_sports
    from utilities.post_processing import set_chmod

    with subtitle_write_locks(operation.context.mapped_path, path, cancel=cancel):
        try:
            validate_subtitle_versions(published_versions)
            operation.validate(cancel=cancel)
            set_chmod(path)
            artifact = capture_artifact(
                operation.context, path, operation.signature, cancel
            )
            with operation.publication(cancel) as (session, _):
                validate_artifact_stat(artifact, path)
                if result is not None:
                    sports_history(
                        session, operation.context, result, action=6, artifact=artifact
                    )
            store_subtitles_sports(
                operation.context.event_id,
                operation.context.arr_instance_id,
                cancel=cancel,
            )
        except JobCancelled:
            raise
        except Exception as exc:
            raise OSError(
                f"Sports subtitle published at {path}; finalization or refresh failed: {exc}"
            ) from exc


def finish_translation(translator, result):
    if translator.sports_operation is None:
        return False
    if getattr(translator, "partial_error", None):
        result.message += " Partial result."
        if translator.partial_error not in result.message:
            result.message += (
                f" Some lines remain in the source language. {translator.partial_error}"
            )
    finalize(
        translator.sports_operation,
        translator.dest_srt_file,
        result,
        translator.cancel,
        translator.published_versions,
    )
    return True


def metadata(context):
    row = database.execute(
        select(
            TableSportsEvents.title,
            TableSportsEvents.partNumber,
            TableSportsEvents.eventDate,
            TableSportsLeagues.title.label("league"),
            TableSportsLeagues.overview,
        )
        .join(
            TableSportsLeagues,
            TableSportsLeagues.id == TableSportsEvents.league_id,
        )
        .where(
            TableSportsEvents.id == context.event_id,
            TableSportsEvents.arr_instance_id == context.arr_instance_id,
        )
    ).one()
    title = " - ".join(
        str(value) for value in (row.league, row.title, row.eventDate) if value
    )
    if row.partNumber:
        title += f" - Part {row.partNumber}"
    return title, f"{title}. {row.overview or ''}".strip()


def trigger_saved(operation, source, language, score, forced, cancel=None):
    from subtitles.processing import _trigger_auto_translation, _trigger_combine

    try:
        _trigger_auto_translation(
            language,
            source,
            operation.context.mapped_path,
            "sports",
            source_score_percent=score,
            forced=forced,
            sports_operation=operation,
            cancel=cancel,
        )
        _trigger_combine(
            operation.context.mapped_path,
            "sports",
            None,
            None,
            None,
            sports_operation=operation,
            cancel=cancel,
        )
    except JobCancelled:
        raise
    except Exception:
        check_cancelled(cancel)
        logging.exception("Sports source saved, but profile processing failed")

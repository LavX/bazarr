# coding=utf-8

import logging
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from subtitles.tools.subsync_engines import (capture_subtitle_versions, local_subtitle_owner_is_clear,
                                             plan_sync_output_cleanup, quarantine_sync_outputs_after_mutation,
                                             read_regular_subtitle,
                                             regular_subtitle_version, staged_subtitle_write,
                                             subtitle_mutation, sync_output_owner_is_unique)
from app.jobs_queue import JobCancelled
from media_servers.events import publication_callback
from sportarr.connection import check_cancelled

from .composer import compose
from .naming import compose_combined_filename, external_subtitles_dir
from .rules import resolve_source_paths

_TWO_LETTER_RE = re.compile(r"^[a-z]{2}$")


@dataclass(frozen=True)
class CombineResult:
    status: str            # "built" | "skipped" | "failed"
    path: str = ""
    alignment: str = ""    # for "built" only
    reason: str = ""       # for "skipped" only
    # Set on "failed", and also on a "built" whose owned publication could not
    # be finalised: the file exists and is correct, but the sports bookkeeping
    # that should have followed it did not run. A consumer that reports a
    # built-with-error as a clean build hides a half-finished publication.
    error: str = ""


def try_combine_for_video(video_path, media_type, sonarr_series_id=None,
                            sonarr_episode_id=None, radarr_id=None,
                            languages=None, format=None, arr_instance_id=None,
                            sports_operation=None, cancel=None, source=None):
    """Single entry point: build (or rebuild) a combined subtitle file
    for the given video. Best-effort, never raises.

    languages, format: when both provided, use as an ad-hoc override.
    Otherwise read the profile's combine rule.
    """
    try:
        owner_kwargs = {}
        if source is not None:
            from media_sources.indexer import SourcePublicationOwnerIndex
            if sports_operation is not None or arr_instance_id is not None:
                raise ValueError('A source composition requires source media metadata')
            owner_kwargs['ownership_index'] = SourcePublicationOwnerIndex()

        if media_type == 'sports' and source is None:
            if sports_operation is None or video_path != sports_operation.context.mapped_path:
                raise ValueError('Sports composition requires its captured profile operation')
            if languages is not None or format is not None:
                raise ValueError('Sports composition requires its assigned profile rule')
            sports_operation.validate(cancel=cancel)
            rule = get_combine_rule(sports_operation.profile)
        else:
            rule = _resolve_rule(
                media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
                override_languages=languages, override_format=format,
                video_path=video_path, arr_instance_id=arr_instance_id,
                **({'source': source} if source is not None else {}),
            )
        if rule is None:
            return CombineResult(status="skipped", reason="no rule")

        # Validate the resolved languages for both the profile and the ad-hoc
        # override paths. The profile path is already validated on save, but
        # ad-hoc requests reach here unchecked: reject duplicates (which would
        # otherwise resolve the same file as primary and secondary and emit a
        # nonsense Movie.en.combined-en.srt), out-of-range counts, and codes that
        # are not plain 2-letter lowercase (defends compose_combined_filename
        # against a crafted code reaching the on-disk filename).
        langs = _normalize_language_codes(rule["languages"])
        if not (2 <= len(langs) <= 3) or len(set(langs)) != len(langs):
            return CombineResult(
                status="failed",
                error=f"combine requires 2 to 3 distinct languages, got {langs}",
            )
        if not all(isinstance(c, str) and _TWO_LETTER_RE.match(c) for c in langs):
            return CombineResult(
                status="failed",
                error=f"combine languages must be 2-letter lowercase codes, got {langs}",
            )
        if rule["format"] not in ("srt", "ass"):
            return CombineResult(
                status="failed",
                error=f"invalid combine format: {rule['format']!r}",
            )

        sources = resolve_source_paths(
            video_path=video_path,
            languages=langs,
        )
        if sources is None:
            return CombineResult(
                status="skipped",
                reason=f"missing source(s) for {langs}",
            )

        inputs = (sources.primary, *sources.secondaries)
        input_versions = {}
        if source is not None:
            input_versions = {path: regular_subtitle_version(path) for path in inputs}
            if any(version is None or not sync_output_owner_is_unique(video_path, path, **owner_kwargs)
                   for path, version in input_versions.items()):
                return CombineResult(status="skipped", reason="combined subtitle input has no unique regular-file owner")

        out_path = compose_combined_filename(
            video_path=video_path,
            primary=langs[0],
            secondaries=langs[1:],
            format=rule["format"],
        )

        # Path-injection barrier: the combined filename is built from the DB video
        # path plus already-validated 2-letter language codes, so it must resolve
        # inside this video's external-subtitles directory. Re-assert it here so a
        # crafted code can never steer makedirs/open/remove outside that folder.
        safe_root = os.path.realpath(external_subtitles_dir(video_path))
        if os.path.islink(out_path) or not sync_output_owner_is_unique(video_path, out_path, **owner_kwargs):
            return CombineResult(status="skipped", reason="combined output has no unique regular-file owner")
        out_path = os.path.realpath(out_path)
        if os.path.commonpath([safe_root, out_path]) != safe_root:
            return CombineResult(
                status="failed",
                error=f"combined output path escaped the subtitles directory: {out_path!r}",
            )

        try:
            out_dir = os.path.dirname(out_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            write_kwargs = {}
            read_kwargs = {}
            notify = publication_callback(media_type, video_path, 'combine', arr_instance_id,
                                          **({'source': source} if source is not None else {}))
            published_versions = ()
            if sports_operation is not None:
                from sportarr.profile_hooks import finalize
                sports_operation = sports_operation.bind(
                    (sources.primary, *sources.secondaries), out_path, cancel=cancel,
                    extra_destinations=_combined_format_siblings(out_path))
                write_kwargs = dict(publication_guard=lambda: sports_operation.publication(cancel),
                                     expected_versions=sports_operation.versions, cancel=cancel,
                                     after_write=lambda: capture_published())

            def after_publish():
                # The media-server notification belongs on the sibling sweep too:
                # removing a stale combined file changes what the server should
                # see, exactly as publishing the new one does.
                if sports_operation is not None:
                    _remove_stale_combined_siblings(out_path, video_path, on_publish=notify,
                                                    ownership_verified=True)
                elif source is None:
                    _remove_stale_combined_siblings(out_path, video_path, on_publish=notify, **owner_kwargs)

            if source is not None:
                owners = owner_kwargs['ownership_index'] = SourcePublicationOwnerIndex()

                def prepare():
                    if not sync_output_owner_is_unique(video_path, out_path, **owner_kwargs):
                        raise ValueError('The combined output lost its unique owner during composition')
                    for path, version in input_versions.items():
                        if (regular_subtitle_version(path) != version
                                or not sync_output_owner_is_unique(video_path, path, **owner_kwargs)):
                            raise ValueError('A combined subtitle input changed or lost its unique owner')
                    return _plan_source_alternates(out_path, video_path, owners)

                @contextmanager
                def publication():
                    with owners.publication(source, prepare, destination=out_path, cleanup_outputs=True) as (
                            current, _, alternates):
                        if current is None or current.path != video_path:
                            raise ValueError('The source item moved or was removed during composition')
                        yield
                        _retire_source_alternates(video_path, alternates, owners, notify)

                write_kwargs['publication_guard'] = publication
                write_kwargs['invalidate_outputs'] = False
                read_kwargs['read_bytes'] = lambda path: read_regular_subtitle(path, input_versions[path])

            def capture_published():
                nonlocal published_versions
                published_versions = capture_subtitle_versions(video_path, (out_path,), cancel)

            with staged_subtitle_write(
                    video_path, out_path, source_paths=inputs,
                    on_publish=notify,
                    after_publish=after_publish, **owner_kwargs, **write_kwargs) as temporary:
                check_cancelled(cancel)
                content = compose(primary_path=sources.primary, secondary_paths=sources.secondaries,
                                  format=rule["format"], **read_kwargs)
                check_cancelled(cancel)
                with open(temporary, "wb") as fh:
                    fh.write(content)
        except JobCancelled:
            raise
        except Exception as e:
            check_cancelled(cancel)
            logging.exception("BAZARR combine could not publish %s", out_path)
            return CombineResult(status="failed", error=str(e))

        if sports_operation is not None:
            try:
                finalize(sports_operation, out_path, cancel=cancel, published_versions=published_versions)
            except JobCancelled:
                raise
            except Exception as exc:
                check_cancelled(cancel)
                return CombineResult(status='built', path=out_path, alignment='ok', error=str(exc))
        else:
            _post_write(out_path, video_path, media_type,
                         sonarr_episode_id, radarr_id, arr_instance_id=arr_instance_id,
                         **({'source': source} if source is not None else {}))

        logging.info(
            "BAZARR combine built %s for %s", out_path, video_path,
        )
        return CombineResult(status="built", path=out_path, alignment="ok")
    except JobCancelled:
        raise
    except Exception as e:
        check_cancelled(cancel)
        logging.exception("BAZARR combine top-level failure")
        return CombineResult(status="failed", error=str(e))


def _normalize_language_codes(languages):
    """Map custom-language codes back to the 2-letter codes combine works with.

    A combined artifact built for zt is indexed through CustomLanguage, so its
    row reads "zh-TW:combined-en" and the API reports code2 "zh-TW". Rebuild
    posts that value back here, where the 2-letter check rejected it outright,
    leaving every custom-language combined file unrebuildable. Fold "zh-TW"
    back to "zt" (and "pt-BR" to "pb", "es-MX" to "ea") first, so the check
    still guards the on-disk filename against anything else.
    """
    try:
        from languages.custom_lang import CustomLanguage
    except Exception:
        logging.debug("BAZARR combine: CustomLanguage unavailable, cannot "
                      "normalize custom-language codes", exc_info=True)
        return list(languages)

    normalized = []
    for code in languages:
        if isinstance(code, str):
            custom = CustomLanguage.from_value(code, attr="language")
            if custom is not None:
                code = custom.alpha2
        normalized.append(code)
    return normalized


_COMBINED_OUTPUT_EXTS = (".srt", ".ass", ".ssa")


def _combined_format_siblings(out_path):
    root, extension = os.path.splitext(out_path)
    return tuple(root + alternate for alternate in _COMBINED_OUTPUT_EXTS if alternate != extension)


def _plan_source_alternates(out_path, video_path, owners):
    alternates = []
    for sibling in _combined_format_siblings(out_path):
        version = regular_subtitle_version(sibling)
        if version is None and os.path.lexists(sibling):
            continue
        generated = plan_sync_output_cleanup(video_path, sibling, owners, include_combined=True)
        if generated or (version is not None and sync_output_owner_is_unique(video_path, sibling, owners)):
            alternates.append((sibling, version, generated))
    return alternates


def _retire_source_alternates(video_path, alternates, owners, notify):
    for sibling, version, generated in alternates:
        try:
            with subtitle_mutation(video_path, sibling, invalidate_outputs=False):
                if not local_subtitle_owner_is_clear(video_path, sibling, owners):
                    continue
                if version is None:
                    if os.path.lexists(sibling):
                        continue
                elif regular_subtitle_version(sibling) == version:
                    os.remove(sibling)
                    if notify:
                        notify(sibling)
                else:
                    continue
            quarantine_sync_outputs_after_mutation(video_path, sibling, owners, prepared=generated)
        except OSError:
            logging.exception('BAZARR combine could not remove stale sibling %s', sibling)


def _remove_stale_combined_siblings(out_path, video_path, on_publish=None, ownership_verified=False,
                                    invalidate_outputs=True, **owner_kwargs):
    """Remove combined-output siblings that share this output's stem but use a
    different subtitle extension (e.g. a stale `.ass` left next to a freshly
    written `.srt`). Best-effort: never raises.

    Safety: only operate inside this video's external-subtitles directory, so a
    crafted output path can never steer os.remove outside that folder."""
    safe_dir = os.path.realpath(external_subtitles_dir(video_path))
    out_real = os.path.realpath(out_path)
    if (os.path.dirname(out_real) != safe_dir
            or (not ownership_verified and not sync_output_owner_is_unique(video_path, out_path, **owner_kwargs))):
        return []
    removed = []
    for sibling in _combined_format_siblings(out_real):
        try:
            if os.path.commonpath([safe_dir, sibling]) != safe_dir:
                continue
            if stat.S_ISREG(os.lstat(sibling).st_mode):
                with subtitle_mutation(video_path, sibling, invalidate_outputs=invalidate_outputs, **owner_kwargs):
                    os.remove(sibling)
                    removed.append(sibling)
                    if on_publish:
                        on_publish(sibling)
                logging.info("BAZARR combine removed stale sibling %s", sibling)
        except FileNotFoundError:
            continue
        except OSError:
            logging.exception(
                "BAZARR combine could not remove stale sibling %s", sibling)
    return removed


def _resolve_rule(media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
                   override_languages, override_format, video_path=None, arr_instance_id=None, source=None):
    if override_languages and override_format:
        return {"languages": list(override_languages), "format": override_format}
    profile = _profile_for(
        media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
        video_path=video_path, arr_instance_id=arr_instance_id,
        **({'source': source} if source is not None else {}),
    )
    return get_combine_rule(profile) if profile else None


def _profile_for(media_type, sonarr_series_id, sonarr_episode_id, radarr_id,
                 video_path=None, arr_instance_id=None, source=None):
    from app.database import get_profiles_list
    metadata = _metadata_for(video_path, media_type, sonarr_episode_id, radarr_id, arr_instance_id,
                             **({'source': source} if source is not None else {}))
    if metadata is None:
        return None
    profile_id = metadata.profile_id if source is not None else metadata.profileId
    return get_profiles_list(profile_id=profile_id) if profile_id else None


def _metadata_for(video_path, media_type, sonarr_episode_id, radarr_id, arr_instance_id, source=None):
    if source is not None:
        from media_sources.context import resolve_same_item

        return resolve_same_item(source) if source.path == video_path else None

    from app.database import TableEpisodes, TableShows, TableMovies, database, select
    from arr_instances.resolution import scoped
    from utilities.path_mappings import path_mappings
    from media_servers.paths import media_paths_equal
    if media_type in ('movies', 'movie'):
        query = select(TableMovies.path, TableMovies.subtitles, TableMovies.imdbId, TableMovies.tmdbId,
                       TableMovies.arr_instance_id, TableMovies.profileId).where(TableMovies.radarrId == radarr_id)
        owner = TableMovies.arr_instance_id
        kind = 'movie'
    elif media_type in ('series', 'episode'):
        query = select(TableEpisodes.path, TableEpisodes.sonarrSeriesId, TableEpisodes.subtitles,
                       TableEpisodes.season, TableEpisodes.episode, TableShows.imdbId, TableShows.tvdbId,
                       TableShows.profileId, TableEpisodes.arr_instance_id).join(TableShows).where(
                           TableEpisodes.sonarrEpisodeId == sonarr_episode_id)
        owner = TableEpisodes.arr_instance_id
        kind = 'episode'
    else:
        return None
    # Upstream IDs select candidates only. The actual file and explicit owner
    # must agree before profile resolution or postprocessing can use a row.
    rows = database.execute(scoped(query, owner, arr_instance_id)).all()
    matches = [row for row in rows if media_paths_equal(
        path_mappings.path_replace_instance(row.path, row.arr_instance_id, kind), video_path)]
    return matches[0] if len(matches) == 1 else None


def _post_write(out_path, video_path, media_type, sonarr_episode_id, radarr_id, arr_instance_id=None,
                source=None):
    """Hook into the same postprocess chain a downloaded subtitle uses.

    postprocess_subtitles uses the 'episode'/'movie' media-type convention and
    its episode branch keys off the value being exactly 'episode'. The combine
    pipeline uses the 'series'/'movies' convention, so map it here. It also
    dereferences the metadata row (sonarrSeriesId/imdbId/...) to emit events and
    refresh the media-server library, so build the same metadata the editor
    endpoint does instead of passing None."""
    try:
        from api.subtitles.subtitles import postprocess_subtitles
        if source is not None:
            current = _metadata_for(video_path, media_type, None, None, None, source=source)
            if current is not None:
                postprocess_subtitles(out_path, video_path, current.publication_type, None, current.item_id,
                                      source=current)
            return
        is_movie = media_type in ("movies", "movie")
        metadata = _metadata_for(video_path, media_type, sonarr_episode_id, radarr_id, arr_instance_id)
        if metadata is not None:
            postprocess_subtitles(out_path, video_path, "movie" if is_movie else "episode", metadata,
                                  radarr_id if is_movie else sonarr_episode_id,
                                  arr_instance_id=metadata.arr_instance_id)
    except Exception:
        logging.exception("BAZARR combine post-write hook failed")


# Late import to avoid circular at module load.
from subtitles.language_profiles import get_combine_rule  # noqa: E402

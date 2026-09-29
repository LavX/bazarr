# coding=utf-8
"""Request-handling logic for the arr_instances CRUD API (#156).

Each function takes an explicit SQLAlchemy session plus parsed request args and
returns a ``(body, status_code)`` tuple. Keeping the logic here (rather than in
the Flask resources) makes it unit-testable without the heavy ``api`` import
chain, and keeps the resources to thin parse/commit glue.

The session is flushed but NOT committed here; the HTTP boundary owns the
transaction and commits on success.
"""
import ast
import json
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.database import TableLanguagesProfiles, TableMovies, TableShows
from utilities.sql_limits import in_chunks

from .media_defaults import (instance_default_profile, merge_media_defaults_into_options,
                             read_media_defaults, validate_media_defaults)
from .repository import VALID_KINDS, ArrInstanceRepository, InstanceOwnsRows, to_safe_dict
from .subtitle_settings import merge_subtitle_settings_into_options, validate_subtitle_settings

_CONFLICT_MESSAGE = "An instance with these connection properties already exists."

# Per-kind sync-job id prefix used by the scheduler fan-out (scheduler.py
# __sonarr_update_task / __radarr_update_task register update_<noun>_<id>).
_SYNC_JOB_PREFIX = {"sonarr": "update_series", "radarr": "update_movies", "sportarr": "update_sports"}


def event_stream(*args, **kwargs):
    """Lazy indirection to ``app.event_handler.event_stream``.

    Defined at module scope (rather than imported) so this service stays
    importable without the heavy Flask-SocketIO chain that event_handler pulls
    in, while tests can still monkeypatch ``service.event_stream``.
    """
    from app.event_handler import event_stream as _event_stream
    _event_stream(*args, **kwargs)


def mirror_scalar_config_from_default(session, kind):
    """Mirror the default instance's connection details into the scalar
    ``settings.<kind>.*`` config so the single-instance compat paths target the
    real server (#276).

    Several legacy paths (the health-check rootfolder validation, the shared
    ``get_<kind>_info`` version probe via ``url_<kind>()``, and the SignalR /
    scheduler fall-back) read the scalar config whenever there is exactly ONE
    instance of a kind, on the documented assumption that the scalar config
    equals the default instance. That holds for installs upgraded via
    ``backfill`` (which built the instance FROM the scalar config), but NOT for
    instances created directly through the API (the onboarding wizard and the
    Connections page), which never populated the scalar config. Those installs
    then poll the default ``127.0.0.1:8989`` / ``:7878`` and spam
    connection-refused errors even though the per-instance sync works fine.
    Re-establish the invariant by copying the default instance back into the
    scalar config after every create/update.

    Best-effort and called only AFTER the row is committed, so a failure here
    must never fail the CRUD request (the caller guards it).
    """
    if kind not in ("sonarr", "radarr"):
        return
    repo = ArrInstanceRepository(session)
    default = repo.get_default(kind)
    if default is None:
        return
    from app.config import settings, write_config
    section = getattr(settings, kind)
    before = (section.ip, section.port, section.base_url, bool(section.ssl),
              bool(section.verify_ssl), int(section.http_timeout or 60), section.apikey)
    section.ip = default.ip or "127.0.0.1"
    if default.port:
        section.port = int(default.port)
    section.base_url = default.base_url or "/"
    section.ssl = bool(default.ssl)
    section.verify_ssl = bool(default.verify_ssl)
    section.http_timeout = int(default.http_timeout or 60)
    api_key = repo.get_decrypted_api_key(default.id)
    if api_key is not None:
        section.apikey = api_key
    after = (section.ip, section.port, section.base_url, bool(section.ssl),
             bool(section.verify_ssl), int(section.http_timeout or 60), section.apikey)
    # Only persist when something actually changed: on the steady-state boot the
    # scalar config already matches the default instance, and an unconditional
    # write_config() would rewrite config.yaml on every startup for nothing.
    if before != after:
        logging.info(
            "Mirrored default %s instance (%s:%s) into the scalar config (#276)",
            kind, section.ip, section.port)
        write_config()


def refresh_runtime(kind, instance_id=None, removed=False):
    """Rebuild scheduler sync jobs and re-fan-out the affected kind's SignalR
    feed after an instance create/update/delete (#156).

    The Flask CRUD handlers call this AFTER the row is committed. Without it,
    scheduled sync jobs and live SignalR fan-out keep using the OLD instance set
    until a restart or an unrelated settings save (the same refresh save_settings
    performs, scoped to the changed kind).

    Caveats honored here:

    * ``update_configurable_tasks`` only adds/replaces jobs (add_job
      replace_existing=True); it never REMOVES one. On delete (and when an
      instance is disabled) the orphaned per-instance ``update_series_<id>`` /
      ``update_movies_<id>`` job must be removed explicitly, or the rebuild
      leaves a stale job firing against a missing/disabled instance. Pass
      ``removed=True`` to drop it; a non-existent job is ignored.
    * Kind-scoped: a Sonarr change re-fans-out only the Sonarr SignalR feed and
      never bounces Radarr (and vice versa).
    * The whole refresh is best-effort: the row is already committed, so a
      transient arr/SignalR failure during the restart must not fail the CRUD
      API response. Mirrors the try/except guard in config.save_settings.
    """
    # Per-instance subtitle settings may have changed; drop the resolver cache
    # so the next read reflects the edit (#227). Cheap and kind-agnostic, so do
    # it before the kind guard returns.
    from .resolution import clear_media_defaults_cache, clear_subtitle_settings_cache
    clear_subtitle_settings_cache()
    # Same for the per-instance default language profile: an edited override
    # must take effect on the next sync, not on the next restart.
    clear_media_defaults_cache()

    if kind == "sportarr":
        try:
            from sportarr.scheduler import refresh_sports_runtime
            refresh_sports_runtime()
        except Exception:
            logging.exception("BAZARR failed to refresh Sportarr runtime after instance change")
        try:
            event_stream(type="sports")
        except Exception:
            logging.exception("BAZARR failed to notify Sportarr clients after instance change")
        return
    if kind not in ("sonarr", "radarr"):
        return

    # Keep the scalar settings.<kind>.* config mirroring the default instance so
    # the single-instance compat paths (health check, get_<kind>_info version
    # probe, scheduler/SignalR fall-back) target the real server, not the default
    # 127.0.0.1:8989 / :7878 (#276). Best-effort, like the SignalR restart below.
    try:
        from app.database import database
        mirror_scalar_config_from_default(database, kind)
    except Exception:
        logging.exception("Failed to mirror scalar config from default %s instance", kind)

    try:
        from app.scheduler import scheduler

        # Drop the orphaned per-instance job BEFORE the rebuild: the rebuild only
        # adds/replaces jobs, so a deleted/disabled instance's job would survive.
        if removed and instance_id is not None:
            from apscheduler.jobstores.base import JobLookupError
            job_id = f"{_SYNC_JOB_PREFIX[kind]}_{instance_id}"
            try:
                scheduler.aps_scheduler.remove_job(job_id)
            except JobLookupError:
                # Already gone (never registered, or no_tasks mode) - fine.
                pass

        scheduler.update_configurable_tasks()

        # Kind-scoped SignalR re-fan-out, guarded on its own so a transient arr
        # connection failure during the restart cannot fail the committed CRUD
        # request (mirrors config.save_settings ~1227-1230).
        from app import signalr_client
        try:
            if kind == "sonarr":
                signalr_client.restart_sonarr_signalr()
            elif kind == "radarr":
                signalr_client.restart_radarr_signalr()
        except Exception:
            logging.exception(
                "BAZARR failed to restart %s SignalR after instance change", kind)

        event_stream(type="task")
    except Exception:
        # Belt-and-suspenders: the row is committed; never let a refresh error
        # surface as a failed CRUD response.
        logging.exception(
            "BAZARR failed to refresh runtime after %s instance change", kind)


def whole_number(value):
    """Request parser type for a port or a timeout: a whole number, never a boolean.

    int() read JSON true as 1 and 1.9 as 1, and both then passed the range checks
    below as port 1 or a one second timeout.
    """
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("must be a whole number")
    return int(value)


def _connection_arg_error(args):
    """Return a validation message for out-of-range connection args, else None.

    Mirrors the scalar config validators (port 1-65535, positive timeout) so the
    API rejects bad values instead of storing them or silently coercing them.
    A boolean is refused too, since Python counts True as 1.
    """
    port = args.get("port")
    if port is not None and (isinstance(port, bool) or not (1 <= port <= 65535)):
        return "port must be between 1 and 65535"
    timeout = args.get("http_timeout")
    if timeout is not None and (isinstance(timeout, bool) or timeout <= 0):
        return "http_timeout must be a positive number of seconds"
    return None


def test_connection(args, http_get=None):
    """Probe an instance described by raw body params.

    Reads connection details (including the plaintext API key) only from the
    request body, never from the URL/query. Returns ``(result, 200)`` where
    result carries ok/version or a structured error; an invalid kind is 400.
    """
    from .client import ArrClientFactory

    kind = args.get("kind")
    if kind not in VALID_KINDS:
        return {"ok": False, "error": "invalid", "message": "invalid kind"}, 400
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"ok": False, "error": "invalid", "message": arg_error}, 400

    client = ArrClientFactory().from_params(
        kind=kind,
        ip=args.get("ip") or "127.0.0.1",
        port=args.get("port"),
        base_url=args.get("base_url") or "/",
        ssl=bool(args.get("ssl")),
        verify_ssl=bool(args.get("verify_ssl")),
        api_key=args.get("api_key") or "",
        http_timeout=args.get("http_timeout") or 60,
        http_get=http_get,
    )
    return client.test_connection(), 200


def test_connection_for_instance(session, instance_id, args=None, http_get=None):
    """Probe a SAVED instance using its STORED (decrypted) API key.

    The card "Test" and the edit-modal "Keep current key" mode never hold the
    plaintext key in the browser (stored keys never leave the server), so they
    cannot use ``test_connection`` which reads the key from the request body.
    This loads the row, decrypts the stored key server-side, and probes
    /system/status.

    Optional ``args`` carry connection overrides (ip/port/base_url/ssl/
    verify_ssl/http_timeout) from an unsaved edit form, so the test reflects the
    values on screen while still using the stored key. Returns ``(result, 200)``,
    a 404 for an unknown id, or 400 for out-of-range overrides.
    """
    from secret_store import decrypt_secret

    from .client import ArrClientFactory

    args = args or {}
    repo = ArrInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return {"ok": False, "error": "not_found", "message": "instance not found"}, 404
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"ok": False, "error": "invalid", "message": arg_error}, 400

    def pick(key, fallback):
        value = args.get(key)
        return fallback if value is None else value

    try:
        api_key = decrypt_secret(row.api_key or "")
    except ValueError:
        # Master key rotated/changed: surface a clean structured error instead of
        # a 500. The user must re-enter the key.
        return {"ok": False, "error": "decrypt_failed",
                "message": "Stored API key could not be decrypted (the master key "
                           "changed). Re-enter the API key and save."}, 200

    client = ArrClientFactory().from_params(
        kind=row.kind,
        ip=args.get("ip") or row.ip,
        port=pick("port", row.port),
        base_url=args.get("base_url") or row.base_url or "/",
        ssl=bool(pick("ssl", row.ssl)),
        verify_ssl=bool(pick("verify_ssl", row.verify_ssl)),
        api_key=api_key,
        http_timeout=pick("http_timeout", row.http_timeout) or 60,
        http_get=http_get,
    )
    return client.test_connection(), 200


def list_instances(session, kind=None):
    repo = ArrInstanceRepository(session)
    return [to_safe_dict(i) for i in repo.list(kind=kind)], 200


def get_instance(session, instance_id):
    repo = ArrInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return {"error": "not_found"}, 404
    return to_safe_dict(row), 200


def create_instance(session, args):
    if args.get('kind') == 'sportarr':
        from sportarr.db import needs_sports_transaction, sports_transaction
        if needs_sports_transaction(session):
            with sports_transaction(session) as transaction:
                result = create_instance(transaction, args)
                if result[1] >= 400:
                    transaction.rollback()
            session.expire_all()
            return result
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"error": "invalid", "message": arg_error}, 400
    try:
        ss_blob = validate_subtitle_settings(args.get("subtitle_settings"))
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    try:
        md_blob = validate_media_defaults(
            args.get("media_defaults"), known_profile_ids=_known_profile_ids(session))
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    from sportarr.settings import merge_sports_settings, validate_sports_settings
    from utilities.path_mappings import validate_sports_mappings
    try:
        mappings = None
        if 'path_mappings' in args:
            if args.get('kind') != 'sportarr':
                raise ValueError('path_mappings require a Sportarr instance')
            validated_mappings = validate_sports_mappings(args['path_mappings'])
            mappings = json.dumps(validated_mappings) if validated_mappings else None
        sports = validate_sports_settings(args.get('sports_settings'))
        if sports and args.get('kind') != 'sportarr':
            raise ValueError('sports_settings require a Sportarr instance')
    except ValueError as exc:
        return {'error': 'invalid', 'message': str(exc)}, 400
    options = merge_media_defaults_into_options(
        merge_subtitle_settings_into_options(None, ss_blob), md_blob)
    if args.get('kind') == 'sportarr':
        options = merge_sports_settings(options, sports)
    repo = ArrInstanceRepository(session)
    try:
        row = repo.create(
            args.get("kind"),
            args.get("name"),
            api_key=args.get("api_key") or "",
            ip=args.get("ip") or "127.0.0.1",
            port=args.get("port"),
            base_url=args.get("base_url") or "/",
            ssl=bool(args.get("ssl")),
            verify_ssl=bool(args.get("verify_ssl")),
            http_timeout=args.get("http_timeout") or 60,
            enabled=True if args.get("enabled") is None else bool(args.get("enabled")),
            is_default=args.get("is_default"),
            options=options,
            path_mappings=mappings,
        )
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    except IntegrityError:
        session.rollback()
        return {"error": "conflict", "message": _CONFLICT_MESSAGE}, 409
    return to_safe_dict(row), 201


def update_instance(session, instance_id, args):
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"error": "invalid", "message": arg_error}, 400
    repo = ArrInstanceRepository(session)
    existing = repo.get(instance_id)
    if existing is None:
        return {"error": "not_found"}, 404
    if existing.kind == 'sportarr':
        from sportarr.db import needs_sports_transaction, sports_transaction
        if needs_sports_transaction(session):
            with sports_transaction(session) as transaction:
                result = update_instance(transaction, instance_id, args)
                if result[1] >= 400:
                    transaction.rollback()
            session.expire_all()
            return result

    kwargs = {}
    if 'path_mappings' in args:
        from utilities.path_mappings import validate_sports_mappings
        try:
            if existing.kind != 'sportarr':
                raise ValueError('path_mappings require a Sportarr instance')
            mappings = validate_sports_mappings(args['path_mappings'])
            kwargs['path_mappings'] = json.dumps(mappings) if mappings else None
        except ValueError as exc:
            return {'error': 'invalid', 'message': str(exc)}, 400
    for field in ("name", "ip", "port", "base_url", "ssl", "verify_ssl",
                  "http_timeout", "enabled", "is_default"):
        if args.get(field) is not None:
            kwargs[field] = args[field]
    if args.get("clear_api_key"):
        kwargs["clear_api_key"] = True
    elif args.get("api_key") is not None:
        kwargs["api_key"] = args["api_key"]

    if args.get("subtitle_settings") is not None:
        try:
            ss_blob = validate_subtitle_settings(args.get("subtitle_settings"))
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}, 400
        kwargs["options"] = merge_subtitle_settings_into_options(existing.options, ss_blob)

    if args.get("media_defaults") is not None:
        try:
            md_blob = validate_media_defaults(
                args.get("media_defaults"), known_profile_ids=_known_profile_ids(session))
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}, 400
        # Merge onto whatever the subtitle_settings edit above already produced,
        # so a request carrying both blobs does not drop one of them.
        kwargs["options"] = merge_media_defaults_into_options(
            kwargs.get("options", existing.options), md_blob)

    if args.get('sports_settings') is not None:
        from sportarr.settings import merge_sports_settings
        try:
            if existing.kind != 'sportarr':
                raise ValueError('sports_settings require a Sportarr instance')
            kwargs['options'] = merge_sports_settings(
                kwargs.get('options', existing.options), args['sports_settings'])
        except ValueError as exc:
            return {'error': 'invalid', 'message': str(exc)}, 400

    try:
        row = repo.update(instance_id, **kwargs)
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    except IntegrityError:
        session.rollback()
        return {"error": "conflict", "message": _CONFLICT_MESSAGE}, 409
    return to_safe_dict(row), 200


def delete_instance(session, instance_id, remove_library=False):
    """Delete an instance, or refuse with 409.

    A Sonarr or Radarr instance that owns library rows is refused by default.
    The refusal says what it holds and that ``remove_library`` deletes it
    together with that library, so the caller can offer that as a separate,
    explicit choice. Any Sonarr or Radarr delete, with or without the library,
    is refused while the instance's library sync is running or queued: the
    sync would write rows for an instance that is gone. That holds for an
    instance that owns nothing yet too, such as one whose first sync has built
    its client and not written a row. It is refused as well while a job that
    names the instance is running, such as a search its sync queued, or while
    a missing-subtitles search of one series, episode or movie of its kind
    runs without naming one: that job has read its item and records what it
    finds under it. Other jobs that name no instance are not waited for: the
    wanted search, the upgrade run and the mass operations, which can run for
    hours, and the search for one language or the manual download of a picked
    subtitle. The item one of them is on when the delete commits is one more
    inline write of the kind described next.

    Only queued jobs are covered. A single-item write (a download or upgrade,
    an exclusion, a SignalR event, a webhook, a one-series refresh) runs
    inline, so one already past its instance lookup when the delete commits
    still lands afterwards: that one item's rows, a history entry, say, or a
    series with its episodes. Those rows stay, naming an id no instance
    added later takes: new ids are chosen above every deleted one.

    A row naming the gone instance does not bring it back. Deleting a kind's
    last instance through the API switches the kind off, after the commit
    and best effort, and with the kind off the startup backfill ignores rows
    that name an instance. A row with no owner still counts: for that one it
    builds the instance again from the stored connection settings. Two
    writers can leave such a row after the removal. An exclusion whose
    episode or movie row is already gone is stored without an owner when its
    caller names none, and an unscoped one-item sync, such as the webhook URL
    without an instance key, that was already past its check for an enabled
    default when the delete committed finds none and leaves the owner empty.
    One that starts later writes nothing unless an enabled default owns it.

    Only Sonarr and Radarr have a library sync to wait for or a library to
    remove. Any other kind takes the plain delete, without holding up the job
    queue.
    """
    repo = ArrInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None or row.kind not in _LIBRARY_SYNC_JOBS:
        return _delete(repo, instance_id, remove_library=False)
    from app.jobs_queue import jobs_queue

    # One step for the job queue, from the check to the commit. Queueing a job
    # and starting one both take this lock, so no sync of this instance can be
    # queued or start in between. One that did would build its client while
    # the instance still existed, then write rows naming it once it was gone.
    # A sync queued meanwhile runs afterwards and finds no instance to sync.
    # The removal is a few indexed statements, so the queue waits only briefly,
    # and no job queues work from inside an open database transaction, so a
    # writer never waits on this lock while holding what the removal needs.
    with jobs_queue._queue_lock:
        row = repo.get(instance_id)
        if row is not None and _library_sync_active(row):
            return {"error": "sync_in_progress",
                    "message": "A library sync of this instance is running or queued. "
                               "Wait for it to finish, then delete the instance again."}, 409
        if row is not None and _subtitle_job_running(row):
            return {"error": "job_in_progress",
                    "message": "A subtitle job for this instance is running. "
                               "Wait for it to finish, then delete the instance again."}, 409
        body, status = _delete(repo, instance_id, remove_library=remove_library)
        if status < 400:
            session.commit()
        return body, status


def _delete(repo, instance_id, remove_library):
    try:
        ok = repo.delete(instance_id, remove_library=remove_library)
    except InstanceOwnsRows as exc:
        row = repo.get(instance_id)
        return {"error": "conflict", "message": str(exc), "can_remove_library": True,
                "library": repo.owned_row_counts(instance_id),
                "last_of_kind": row is not None and repo.last_of_kind(row)}, 409
    except ValueError as exc:
        return {"error": "conflict", "message": str(exc)}, 409
    if not ok:
        return {"error": "not_found"}, 404
    return "", 204


# The bulk sync of one instance, and the whole-kind sync that carries no
# instance id and writes to the kind's default.
_LIBRARY_SYNC_JOBS = {
    "sonarr": ("sonarr.sync.series", ("update_series_for_instance", "update_series")),
    "radarr": ("radarr.sync.movies", ("update_movies_for_instance", "update_movies")),
}


def _library_sync_active(row):
    """Whether a sync that may write this instance's library is queued or
    running: its own, or a whole-kind sync of its kind. A whole-kind sync
    resolves the default once, when it starts, so it counts for every
    instance of the kind, whichever one is the default now. Call it holding
    the job queue lock, or a job can move between the two queues unseen."""
    if row.kind not in _LIBRARY_SYNC_JOBS:
        return False
    from app.jobs_queue import jobs_queue

    module, funcs = _LIBRARY_SYNC_JOBS[row.kind]
    targets = {row.id, None}
    jobs = list(jobs_queue.jobs_pending_queue) + list(jobs_queue.jobs_running_queue)
    return any(job.module == module and job.func in funcs
               and (job.kwargs or {}).get("arr_instance_id") in targets for job in jobs)


# Subtitle searches that look their item up by its upstream id, in every
# instance of the kind when they name none.
_SUBTITLE_SEARCH_JOBS = {
    "sonarr": ("subtitles.mass_download.series",
               ("series_download_subtitles", "episode_download_subtitles")),
    "radarr": ("subtitles.mass_download.movies", ("movies_download_subtitles",)),
}


def _subtitle_job_running(row):
    """Whether a running job may still write rows naming this instance: one
    that names it, such as a search the sync queued or a translation, or one
    of the searches in _SUBTITLE_SEARCH_JOBS for its kind that names no
    instance and may have read one of its items. Such a job has read its item
    already, and once it finds a subtitle it records the history under the
    instance it read, whether or not that instance is still there. A queued
    search is no reason to wait: it reads its item when it starts, and finds
    nothing once the instance is gone. A queued translation carries its ids
    and still runs, so its history entry names the gone instance, an id no
    later instance takes. Other jobs that name no instance are not
    recognised; delete_instance says which and why.
    Call it holding the job queue lock, or a job can start unseen."""
    from app.jobs_queue import jobs_queue

    module, funcs = _SUBTITLE_SEARCH_JOBS[row.kind]
    for job in list(jobs_queue.jobs_running_queue):
        owner = (job.kwargs or {}).get("arr_instance_id")
        if owner == row.id or (owner is None and job.module == module and job.func in funcs):
            return True
    return False


def after_instance_deleted(session, kind, removed_library=False):
    """Follow a committed Sonarr or Radarr instance delete. Best effort: the
    delete is already committed, so nothing here may fail the request."""
    if kind not in ("sonarr", "radarr"):
        return
    if removed_library:
        try:
            event_stream(type="series" if kind == "sonarr" else "movie", action="delete")
            event_stream(type="badges")
        except Exception:
            logging.exception("BAZARR failed to notify clients after removing a %s library", kind)
    try:
        switch_off_kind_without_instances(session, kind)
    except Exception:
        logging.exception("BAZARR failed to switch %s off after deleting its last instance", kind)


def switch_off_kind_without_instances(session, kind):
    """Turn ``use_<kind>`` off once a Sonarr or Radarr kind has no instance left.

    Otherwise the startup backfill rebuilds an instance from the scalar
    connection settings, which still describe the one just deleted, and syncs
    its library straight back in. Written through save_settings, the same path
    as the Settings page, so the scheduler and the SignalR feeds follow it.
    Returns True when it switched the kind off.
    """
    if kind not in ("sonarr", "radarr"):
        return False
    if ArrInstanceRepository(session).list(kind=kind):
        return False
    from app import config

    if not getattr(config.settings.general, f"use_{kind}"):
        return False
    config.save_settings([(f"settings-general-use_{kind}", ["false"])])
    event_stream(type="settings")
    logging.info("Switched %s off: its last instance was deleted", kind)
    return True


def _known_profile_ids(session):
    """The set of language profile ids that currently exist.

    Used to reject an override naming a profile that is not there, so a dangling
    ``profileId`` is never persisted in the first place. The sync still guards
    against a profile deleted AFTER the override was saved.
    """
    return {row.profileId for row in session.execute(
        select(TableLanguagesProfiles.profileId)).all()}


def _excluded_profile_tags(kind):
    """Tags that mean "no profile", or an empty set when tag handling is off."""
    from app.config import settings

    enabled = {
        "sonarr": settings.general.serie_tag_enabled,
        "radarr": settings.general.movie_tag_enabled,
        "sportarr": settings.general.sports_tag_enabled,
    }.get(kind, False)
    if not enabled:
        return frozenset()

    return frozenset(settings.general.remove_profile_tags or ())


def _tags_exclude_a_profile(stored_tags, excluded_tags):
    """True when this row's tags intersect the no-profile list.

    ``tags`` is stored as the repr of a Python list, which is what the sync
    writes, so it is read back the same way. An unreadable value excludes
    nothing: refusing to apply the default because a tag blob is malformed
    would be a worse failure than applying it.
    """
    if not excluded_tags or not stored_tags:
        return False

    try:
        tags = ast.literal_eval(stored_tags)
    except (ValueError, SyntaxError):
        return False

    if not isinstance(tags, (list, tuple, set)):
        return False

    return bool(set(tags) & excluded_tags)


def reindex_after_default_profile(kind, upstream_ids, arr_instance_id, job_id=None):
    """Recompute what is missing for the items apply_default_profile changed.

    Runs as a queued job rather than inside the Apply request: one index pass
    per item scans every episode and emits events, so a library of a few
    thousand unprofiled items would hold a web worker for minutes and can
    outlive a proxy timeout, with nothing to show for it in the UI meanwhile.

    Failures are logged, not raised: the profiles are already committed, and the
    scheduled indexer picks up anything missed here.
    """
    if kind == 'sportarr':
        from sportarr.library import refresh_league_profiles
        refresh_league_profiles(upstream_ids, arr_instance_id)
        return
    if not upstream_ids or kind not in ("sonarr", "radarr"):
        return

    if kind == "sonarr":
        from subtitles.indexer.series import list_missing_subtitles as index_one
        event_type = "series"
    else:
        from subtitles.indexer.movies import list_missing_subtitles_movies as index_one
        event_type = "movie"

    for upstream_id in upstream_ids:
        try:
            index_one(no=upstream_id, arr_instance_id=arr_instance_id)
            event_stream(type=event_type, payload=upstream_id)
        except Exception:
            logging.exception(
                "BAZARR failed to refresh missing subtitles for %s %s on instance %s",
                kind, upstream_id, arr_instance_id)

    event_stream(type="badges")


def apply_default_profile(session, instance_id):
    """Assign this instance's default language profile to its media that has no
    profile yet. Opt-in, and deliberately narrow.

    Only rows owned by ``instance_id`` whose ``profileId`` is NULL are touched,
    so hand-picked profiles are never overwritten: a silent bulk reassignment is
    not recoverable, and wholesale reassignment already exists as the mass-edit
    profile selector in the Series and Movies views.

    Returns ``(body, status)``. The body carries the upstream ids that changed so
    the HTTP layer can re-run the missing-subtitles indexer for exactly those
    items.
    """
    repo = ArrInstanceRepository(session)
    row = repo.get(instance_id)
    if row is None:
        return {"error": "not_found"}, 404

    if row.kind == 'sportarr':
        from sportarr.library import apply_instance_default_profile
        try:
            return apply_instance_default_profile(session, instance_id), 200
        except ValueError as exc:
            return {'error': 'invalid', 'message': str(exc)}, 400

    if row.kind not in ("sonarr", "radarr"):
        return {"error": "unsupported",
                "message": "Applying default profiles is not supported for this instance kind yet."}, 400

    has_override, profile = instance_default_profile(read_media_defaults(row.options))
    if not has_override or profile is None:
        return {"error": "invalid",
                "message": "This instance has no default language profile to apply."}, 400
    if profile not in _known_profile_ids(session):
        return {"error": "invalid",
                "message": f"Language profile {profile} no longer exists."}, 400

    table, upstream_column = {
        'sonarr': (TableShows, TableShows.sonarrSeriesId),
        'radarr': (TableMovies, TableMovies.radarrId),
    }[row.kind]

    # Collect the targets before the write: the UPDATE's own WHERE stops
    # matching them once profileId is set, and the caller needs the ids.
    targets = session.execute(
        select(upstream_column, table.tags)
        .where(table.arr_instance_id == instance_id, table.profileId.is_(None))).all()

    # A row a tag rule deliberately excluded is not "unset yet", it is "kept
    # out": the sync parsers set profileId to None on purpose when the item
    # carries one of remove_profile_tags. Filling those in here would silently
    # undo the rule the user configured, on their whole library at once.
    excluded_tags = _excluded_profile_tags(row.kind)
    upstream_ids = [t[0] for t in targets
                    if not _tags_exclude_a_profile(t[1], excluded_tags)]
    # One statement per chunk: this binds a variable per item, and SQLite built
    # with the legacy limit rejects more than 999 in a statement. A library with
    # that many unprofiled items is ordinary, and the failure would be the Apply
    # button erroring with nothing updated.
    for chunk in in_chunks(upstream_ids):
        session.execute(
            update(table)
            .values(profileId=profile)
            .where(table.arr_instance_id == instance_id,
                   table.profileId.is_(None),
                   upstream_column.in_(chunk)))

    logging.info("Assigned language profile %s to %s unprofiled %s items on instance %s",
                 profile, len(upstream_ids), row.kind, instance_id)
    return {"updated": len(upstream_ids), "profileId": profile, "kind": row.kind,
            "upstream_ids": upstream_ids}, 200


def reconcile_sportarr_enable_flag(session):
    """Turn on ``general.use_sportarr`` once, for an install that already had a
    working Sportarr server before the flag existed.

    Before the master toggle, whether the Sports pages existed was derived from
    "any enabled Sportarr instance exists". The flag defaults to False, so
    without this an upgrading install boots with sports silently gone.

    "Has the operator ever set this?" is not a question dynaconf can answer: a
    must_exist validator writes its default at first load, so the key looks
    explicitly set from the moment it exists. Hence the separate marker. It
    burns on the first run whatever the outcome, so an operator who later turns
    the flag off keeps it off, and an instance added afterwards does not
    retroactively flip a flag nobody asked for.

    Returns True when it turned the flag on. Best-effort; the caller guards it.
    """
    from app import config

    if config.settings.sportarr.enable_reconciled:
        return False
    enabled_any = bool(ArrInstanceRepository(session).list('sportarr', enabled_only=True))
    if enabled_any:
        config.settings.general.use_sportarr = True
    config.settings.sportarr.enable_reconciled = True
    config.write_config()
    if enabled_any:
        # The one-time flip must also register the sports jobs, not just the
        # pages. The scheduler builds its jobs before migration runs, so it saw
        # the pre-reconcile flag and registered nothing; without this the first
        # upgraded boot shows Sports but schedules no sync, scan or search until
        # a later restart or settings save. Best-effort: the flag and marker are
        # already committed, so a refresh hiccup only defers the jobs.
        try:
            from sportarr.scheduler import refresh_sports_runtime
            refresh_sports_runtime()
        except Exception:
            logging.exception("Refresh after Sportarr enable reconcile failed; continuing startup")
    return enabled_any

# coding=utf-8
"""Request-handling logic for the arr_instances CRUD API (#156).

Each function takes an explicit SQLAlchemy session plus parsed request args and
returns a ``(body, status_code)`` tuple. Keeping the logic here (rather than in
the Flask resources) makes it unit-testable without the heavy ``api`` import
chain, and keeps the resources to thin parse/commit glue.

The session is flushed but NOT committed here; the HTTP boundary owns the
transaction and commits on success.
"""
import logging

from sqlalchemy.exc import IntegrityError

from .repository import VALID_KINDS, ArrInstanceRepository, to_safe_dict
from .subtitle_settings import merge_subtitle_settings_into_options, validate_subtitle_settings

_CONFLICT_MESSAGE = "An instance with these connection properties already exists."

# Per-kind sync-job id prefix used by the scheduler fan-out (scheduler.py
# __sonarr_update_task / __radarr_update_task register update_<noun>_<id>).
_SYNC_JOB_PREFIX = {"sonarr": "update_series", "radarr": "update_movies"}


def event_stream(*args, **kwargs):
    """Lazy indirection to ``app.event_handler.event_stream``.

    Defined at module scope (rather than imported) so this service stays
    importable without the heavy Flask-SocketIO chain that event_handler pulls
    in, while tests can still monkeypatch ``service.event_stream``.
    """
    from app.event_handler import event_stream as _event_stream
    _event_stream(*args, **kwargs)


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
    from .resolution import clear_subtitle_settings_cache
    clear_subtitle_settings_cache()

    if kind not in VALID_KINDS:
        return
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


def _connection_arg_error(args):
    """Return a validation message for out-of-range connection args, else None.

    Mirrors the scalar config validators (port 1-65535, positive timeout) so the
    API rejects bad values instead of storing them or silently coercing them.
    """
    port = args.get("port")
    if port is not None and not (1 <= port <= 65535):
        return "port must be between 1 and 65535"
    timeout = args.get("http_timeout")
    if timeout is not None and timeout <= 0:
        return "http_timeout must be a positive number of seconds"
    return None


def test_connection(args, http_get=None):
    """Probe a Sonarr/Radarr instance described by raw body params.

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
    arg_error = _connection_arg_error(args)
    if arg_error:
        return {"error": "invalid", "message": arg_error}, 400
    try:
        ss_blob = validate_subtitle_settings(args.get("subtitle_settings"))
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    options = merge_subtitle_settings_into_options(None, ss_blob)
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

    kwargs = {}
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

    try:
        row = repo.update(instance_id, **kwargs)
    except ValueError as exc:
        return {"error": "invalid", "message": str(exc)}, 400
    except IntegrityError:
        session.rollback()
        return {"error": "conflict", "message": _CONFLICT_MESSAGE}, 409
    return to_safe_dict(row), 200


def delete_instance(session, instance_id):
    repo = ArrInstanceRepository(session)
    try:
        ok = repo.delete(instance_id)
    except ValueError as exc:
        return {"error": "conflict", "message": str(exc)}, 409
    if not ok:
        return {"error": "not_found"}, 404
    return "", 204
